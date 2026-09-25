"""Persistence for NAS path to Databox asset mappings."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class Entry:
    """A persisted mapping from a NAS path to a Databox asset."""

    path: str
    asset_id: UUID
    size: int
    mtime: float


class EntryRepository(Protocol):
    """Storage contract used by the HTTP service."""

    def get(self, path: str) -> Entry | None:
        """Return a mapping by its NAS-relative path."""

    def upsert(self, entry: Entry) -> None:
        """Create or replace a mapping after a successful Databox operation."""

    def delete(self, path: str) -> Entry | None:
        """Remove and return a mapping by path."""


class PostgresEntryRepository:
    """PostgreSQL-backed repository for synchronization mappings."""

    def __init__(self, database_url: str) -> None:
        """Create a connection pool for the configured gateway database."""
        self.pool = ConnectionPool(conninfo=database_url, kwargs={"row_factory": dict_row})

    def initialize(self) -> None:
        """Create the gateway table when it does not already exist."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_entries (
                    path TEXT PRIMARY KEY,
                    asset_id UUID NOT NULL,
                    size BIGINT NOT NULL CHECK (size >= 0),
                    mtime DOUBLE PRECISION NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

    def get(self, path: str) -> Entry | None:
        """Return a mapping by its NAS-relative path."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            row = cursor.execute("SELECT path, asset_id, size, mtime FROM sync_entries WHERE path = %s", (path,)).fetchone()
        return None if row is None else Entry(**row)

    def upsert(self, entry: Entry) -> None:
        """Create or replace a mapping after a successful Databox operation."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO sync_entries (path, asset_id, size, mtime) VALUES (%s, %s, %s, %s)
                ON CONFLICT (path) DO UPDATE SET asset_id = EXCLUDED.asset_id, size = EXCLUDED.size,
                    mtime = EXCLUDED.mtime, updated_at = NOW()
            """, (entry.path, entry.asset_id, entry.size, entry.mtime))

    def delete(self, path: str) -> Entry | None:
        """Remove and return a mapping by path."""
        with self.pool.connection() as connection, connection.cursor() as cursor:
            row = cursor.execute("DELETE FROM sync_entries WHERE path = %s RETURNING path, asset_id, size, mtime", (path,)).fetchone()
        return None if row is None else Entry(**row)


class InMemoryEntryRepository:
    """In-memory repository used by unit tests."""

    def __init__(self) -> None:
        """Initialize an empty mapping store."""
        self.entries: dict[str, Entry] = {}

    def get(self, path: str) -> Entry | None:
        """Return a mapping by path."""
        return self.entries.get(path)

    def upsert(self, entry: Entry) -> None:
        """Store a mapping by path."""
        self.entries[entry.path] = entry

    def delete(self, path: str) -> Entry | None:
        """Remove and return a mapping by path."""
        return self.entries.pop(path, None)