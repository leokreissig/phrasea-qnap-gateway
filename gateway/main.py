"""Central reconciliation API for NAS and Storage Box clients."""

import os
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth import JwksValidator, JwtSettings
from gateway.models import (
    CheckResponse,
    FamilyCheck,
    FamilyCommit,
    FamilyFile,
    FamilyRecord,
    FileRole,
    OperationAction,
    ReconciliationOperation,
    Source,
)
from gateway.repository import CanonicalAsset, FamilyRepository, PostgresFamilyRepository

bearer_scheme = HTTPBearer(auto_error=True)


def create_app(repository: FamilyRepository | None = None, validator: JwksValidator | None = None) -> FastAPI:
    """Create the source-independent synchronization decision service."""
    if repository is None:
        repository = PostgresFamilyRepository(database_url())
    if validator is None:
        validator = JwksValidator(JwtSettings(
            issuer=_required_setting("OIDC_ISSUER"),
            jwks_url=_required_setting("OIDC_JWKS_URL"),
            client_id=os.getenv("OIDC_CLIENT_ID", "phrasea-sync-gateway"),
            required_role=os.getenv("OIDC_REQUIRED_ROLE", "sync-gateway-access"),
        ))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if isinstance(repository, PostgresFamilyRepository):
            repository.initialize()
        yield

    app = FastAPI(title="Phrasea NAS Sync Gateway", version="0.2.0", lifespan=lifespan)

    def require_gateway_token(credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)]) -> dict:
        """Authorize a NAS or migration client carrying the designated JWT."""
        return validator.validate(credentials)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        """Report that the HTTP process is ready to make reconciliation decisions."""
        return {"status": "ok"}

    @app.post("/v1/check", response_model=CheckResponse)
    def check(family: FamilyCheck, _: dict = Depends(require_gateway_token)) -> CheckResponse:
        """Return the sole source-side operation plan for a complete file family."""
        _validate_family(family)
        return _plan_operations(family, repository.resolve(family))

    @app.put("/v1/entries", response_model=FamilyRecord)
    def commit(family: FamilyCommit, _: dict = Depends(require_gateway_token)) -> FamilyRecord:
        """Persist completed Databox work and all aliases for future reconciliation."""
        _validate_family(family)
        return _record(repository.commit(family))

    @app.get("/v1/entries", response_model=FamilyRecord)
    def get_entry(source: Source, path: str, _: dict = Depends(require_gateway_token)) -> FamilyRecord:
        """Resolve a source path before a move or soft-delete operation."""
        asset = repository.get_alias(source, _validated_path(path))
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "path is not synchronized")
        return _record(asset)

    @app.delete("/v1/entries", response_model=FamilyRecord)
    def delete_entry(source: Source, path: str, _: dict = Depends(require_gateway_token)) -> FamilyRecord:
        """Remove one alias after the caller completes Databox soft deletion."""
        asset = repository.remove_alias(source, _validated_path(path))
        if asset is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "path is not synchronized")
        return _record(asset)

    return app


def _plan_operations(family: FamilyCheck, existing: CanonicalAsset | None) -> CheckResponse:
    """Derive deterministic Databox actions from one central family identity."""
    if existing is None:
        return CheckResponse(operations=_initial_operations(family))

    operations = [ReconciliationOperation(
        action=OperationAction.RECORD_ALIAS,
        asset_id=existing.asset_id,
        detail="canonical content hash or document ID matched",
    )]
    if family.metadata_fingerprint and family.metadata_fingerprint != existing.metadata_fingerprint:
        operations.append(ReconciliationOperation(
            action=OperationAction.UPDATE_METADATA,
            asset_id=existing.asset_id,
            detail="sidecar metadata changed",
        ))
    for file in family.files:
        if file.role is FileRole.JPEG:
            operations.append(ReconciliationOperation(
                action=OperationAction.UPLOAD_RENDITION,
                path=file.path,
                role=file.role,
                asset_id=existing.asset_id,
            ))
        elif file.role is FileRole.XML:
            operations.append(ReconciliationOperation(
                action=OperationAction.ATTACH,
                path=file.path,
                role=file.role,
                asset_id=existing.asset_id,
            ))
    return CheckResponse(asset_id=existing.asset_id, operations=operations)


def _initial_operations(family: FamilyCheck) -> list[ReconciliationOperation]:
    """Return the ordered plan for a family never observed from any source."""
    actions = {
        FileRole.MAIN: OperationAction.UPLOAD_MAIN,
        FileRole.JPEG: OperationAction.UPLOAD_RENDITION,
        FileRole.XMP: OperationAction.UPDATE_METADATA,
        FileRole.XML: OperationAction.ATTACH,
    }
    return [ReconciliationOperation(action=actions[file.role], path=file.path, role=file.role) for file in family.files]


def _validate_family(family: FamilyCheck) -> None:
    """Reject incomplete or ambiguous families before making a decision."""
    if sum(file.role is FileRole.MAIN for file in family.files) != 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "a family must contain exactly one main file")


def _validated_path(path: str) -> str:
    """Validate a query path through the canonical family-file model."""
    return FamilyFile(path=path, role=FileRole.MAIN, size=0, mtime=0, sha256="0" * 64).path


def _record(asset: CanonicalAsset) -> FamilyRecord:
    """Expose a canonical record only after Databox supplied an asset ID."""
    if asset.asset_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "asset upload has not completed")
    return FamilyRecord(asset_id=asset.asset_id, family_path=asset.family_path, document_id=asset.document_id)


def create_production_app() -> FastAPI:
    """Create the configured application for the production ASGI server."""
    return create_app()


def database_url() -> str:
    """Return an explicit database URL or construct one from injected DB settings."""
    if value := os.getenv("DATABASE_URL"):
        return value
    return "postgresql://{user}:{password}@{host}:{port}/{database}".format(
        user=quote(_required_setting("DB_USER"), safe=""),
        password=quote(_required_setting("DB_PASSWORD"), safe=""),
        host=_required_setting("DB_HOST"),
        port=os.getenv("DB_PORT", "5432"),
        database=_required_setting("DB_NAME"),
    )


def _required_setting(name: str) -> str:
    """Return a required configuration value or fail during startup."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value
