"""Source-independent identity persistence for synchronization families."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gateway.models import FamilyCheck, FamilyCommit, FamilyFile, Source


@dataclass(frozen=True)
class CanonicalAsset:
    """Canonical identity shared by every source alias in an asset family."""

    id: UUID
    asset_id: UUID | None
    family_path: str
    main_sha256: str | None
    document_id: str | None
    metadata_fingerprint: str | None


class FamilyRepository(Protocol):
    """Persistence contract for central family reconciliation."""

    def resolve(self, family: FamilyCheck) -> CanonicalAsset | None:
        """Find a canonical family by alias, document ID, or content hash."""

    def commit(self, family: FamilyCommit) -> CanonicalAsset:
        """Persist a completed Databox result and every submitted source alias."""

    def get_alias(self, source: Source, path: str) -> CanonicalAsset | None:
        """Return the canonical family stored for a source path."""

    def remove_alias(self, source: Source, path: str) -> CanonicalAsset | None:
        """Remove one source alias while retaining the canonical family record."""


class PostgresFamilyRepository:
    """PostgreSQL persistence for canonical families and source aliases."""

    def __init__(self, database_url: str) -> None:
        """Create a connection pool for the configured gateway database."""
        self.pool = ConnectionPool(conninfo=database_url, kwargs={"row_factory": dict_row})

    def initialize(self) -> None:
        """Create identity tables without modifying existing operational data."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_canonical_assets (
                    id UUID PRIMARY KEY,
                    asset_id UUID UNIQUE,
                    family_path TEXT NOT NULL,
                    main_sha256 TEXT UNIQUE,
                    document_id TEXT,
                    metadata_fingerprint TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS sync_canonical_assets_document_id_idx
                ON sync_canonical_assets (document_id) WHERE document_id IS NOT NULL
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_source_aliases (
                    source TEXT NOT NULL,
                    path TEXT NOT NULL,
                    canonical_id UUID NOT NULL REFERENCES sync_canonical_assets(id) ON DELETE CASCADE,
                    sha256 TEXT NOT NULL,
                    role TEXT NOT NULL,
                    size BIGINT NOT NULL CHECK (size >= 0),
                    mtime DOUBLE PRECISION NOT NULL,
                    metadata_fingerprint TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (source, path)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS sync_source_aliases_sha256_idx ON sync_source_aliases (sha256)")

    def resolve(self, family: FamilyCheck) -> CanonicalAsset | None:
        """Resolve aliases first, then stable IDs and finally content hashes."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            for file in family.files:
                asset = self._from_row(cursor.execute("""
                    SELECT asset.* FROM sync_source_aliases alias
                    JOIN sync_canonical_assets asset ON asset.id = alias.canonical_id
                    WHERE alias.source = %s AND alias.path = %s
                """, (family.source.value, file.path)).fetchone())
                if asset:
                    return asset
            if family.document_id:
                asset = self._from_row(cursor.execute("""
                    SELECT * FROM sync_canonical_assets WHERE document_id = %s
                    ORDER BY updated_at DESC LIMIT 1
                """, (family.document_id,)).fetchone())
                if asset:
                    return asset
            if family.main_file:
                asset = self._from_row(cursor.execute(
                    "SELECT * FROM sync_canonical_assets WHERE main_sha256 = %s",
                    (family.main_file.sha256,),
                ).fetchone())
                if asset:
                    return asset
            for file in family.files:
                asset = self._from_row(cursor.execute("""
                    SELECT asset.* FROM sync_source_aliases alias
                    JOIN sync_canonical_assets asset ON asset.id = alias.canonical_id
                    WHERE alias.sha256 = %s ORDER BY asset.updated_at DESC LIMIT 1
                """, (file.sha256,)).fetchone())
                if asset:
                    return asset
        return None

    def commit(self, family: FamilyCommit) -> CanonicalAsset:
        """Upsert a canonical record and aliases after a successful operation."""
        existing = self.resolve(family)
        canonical_id = existing.id if existing else uuid4()
        main = family.main_file
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO sync_canonical_assets (id, asset_id, family_path, main_sha256, document_id, metadata_fingerprint)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET asset_id = EXCLUDED.asset_id,
                    family_path = EXCLUDED.family_path,
                    main_sha256 = COALESCE(EXCLUDED.main_sha256, sync_canonical_assets.main_sha256),
                    document_id = COALESCE(EXCLUDED.document_id, sync_canonical_assets.document_id),
                    metadata_fingerprint = COALESCE(EXCLUDED.metadata_fingerprint, sync_canonical_assets.metadata_fingerprint),
                    updated_at = NOW()
            """, (canonical_id, family.asset_id, family.family_path, main.sha256 if main else None, family.document_id, family.metadata_fingerprint))
            for file in family.files:
                cursor.execute("""
                    INSERT INTO sync_source_aliases (source, path, canonical_id, sha256, role, size, mtime, metadata_fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, path) DO UPDATE SET canonical_id = EXCLUDED.canonical_id,
                        sha256 = EXCLUDED.sha256, role = EXCLUDED.role, size = EXCLUDED.size,
                        mtime = EXCLUDED.mtime, metadata_fingerprint = EXCLUDED.metadata_fingerprint,
                        updated_at = NOW()
                """, (family.source.value, file.path, canonical_id, file.sha256, file.role.value, file.size, file.mtime, file.metadata_fingerprint))
            row = cursor.execute("SELECT * FROM sync_canonical_assets WHERE id = %s", (canonical_id,)).fetchone()
        asset = self._from_row(row)
        if asset is None:
            raise RuntimeError("canonical asset disappeared during commit")
        return asset

    def get_alias(self, source: Source, path: str) -> CanonicalAsset | None:
        """Return a canonical record stored under one source path."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            row = cursor.execute("""
                SELECT asset.* FROM sync_source_aliases alias
                JOIN sync_canonical_assets asset ON asset.id = alias.canonical_id
                WHERE alias.source = %s AND alias.path = %s
            """, (source.value, path)).fetchone()
        return self._from_row(row)

    def remove_alias(self, source: Source, path: str) -> CanonicalAsset | None:
        """Remove only the specified source alias after a successful soft delete."""
        asset = self.get_alias(source, path)
        if asset is None:
            return None
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM sync_source_aliases WHERE source = %s AND path = %s", (source.value, path))
        return asset

    @staticmethod
    def _from_row(row: dict | None) -> CanonicalAsset | None:
        """Convert a database row into an immutable canonical identity."""
        return CanonicalAsset(**row) if row else None


class InMemoryFamilyRepository:
    """In-memory central identity persistence used by focused unit tests."""

    def __init__(self) -> None:
        """Initialize canonical and alias indexes."""
        self.assets: dict[UUID, CanonicalAsset] = {}
        self.aliases: dict[tuple[Source, str], tuple[UUID, FamilyFile]] = {}

    def resolve(self, family: FamilyCheck) -> CanonicalAsset | None:
        """Resolve aliases, stable IDs, and hashes with the production order."""
        for file in family.files:
            alias = self.aliases.get((family.source, file.path))
            if alias:
                return self.assets[alias[0]]
        for asset in self.assets.values():
            if family.document_id and family.document_id == asset.document_id:
                return asset
            if family.main_file and family.main_file.sha256 == asset.main_sha256:
                return asset
        for canonical_id, stored in self.aliases.values():
            if any(stored.sha256 == incoming.sha256 for incoming in family.files):
                return self.assets[canonical_id]
        return None

    def commit(self, family: FamilyCommit) -> CanonicalAsset:
        """Persist one completed family and all submitted aliases in memory."""
        existing = self.resolve(family)
        asset = CanonicalAsset(
            id=existing.id if existing else uuid4(),
            asset_id=family.asset_id,
            family_path=family.family_path,
            main_sha256=family.main_file.sha256 if family.main_file else None,
            document_id=family.document_id or (existing.document_id if existing else None),
            metadata_fingerprint=family.metadata_fingerprint or (existing.metadata_fingerprint if existing else None),
        )
        self.assets[asset.id] = asset
        for file in family.files:
            self.aliases[(family.source, file.path)] = (asset.id, file)
        return asset

    def get_alias(self, source: Source, path: str) -> CanonicalAsset | None:
        """Return the canonical record stored under a source path."""
        alias = self.aliases.get((source, path))
        return self.assets[alias[0]] if alias else None

    def remove_alias(self, source: Source, path: str) -> CanonicalAsset | None:
        """Remove a source alias while retaining its canonical record."""
        alias = self.aliases.pop((source, path), None)
        return self.assets[alias[0]] if alias else None
