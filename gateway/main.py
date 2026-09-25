"""HTTP service that stores NAS-to-Databox synchronization metadata."""

import os
from contextlib import asynccontextmanager
from math import isfinite
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth import JwksValidator, JwtSettings
from gateway.models import CheckResponse, EntryIdentity, EntryResponse, EntryUpsert
from gateway.repository import Entry, EntryRepository, PostgresEntryRepository

bearer_scheme = HTTPBearer(auto_error=True)


def create_app(repository: EntryRepository | None = None, validator: JwksValidator | None = None) -> FastAPI:
    """Create the synchronization gateway application."""
    if repository is None:
        repository = PostgresEntryRepository(_required_setting("DATABASE_URL"))
    if validator is None:
        validator = JwksValidator(JwtSettings(
            issuer=_required_setting("OIDC_ISSUER"),
            jwks_url=_required_setting("OIDC_JWKS_URL"),
            client_id=os.getenv("OIDC_CLIENT_ID", "phrasea-sync-gateway"),
            required_role=os.getenv("OIDC_REQUIRED_ROLE", "sync-gateway-access"),
        ))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if isinstance(repository, PostgresEntryRepository):
            repository.initialize()
        yield

    app = FastAPI(title="Phrasea NAS Sync Gateway", version="0.1.0", lifespan=lifespan)

    def require_gateway_token(credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)]) -> dict:
        """Authorize callers carrying the designated Keycloak gateway token."""
        return validator.validate(credentials)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        """Report that the HTTP process is accepting requests."""
        return {"status": "ok"}

    @app.post("/v1/check", response_model=CheckResponse)
    def check(entry: EntryIdentity, _: dict = Depends(require_gateway_token)) -> CheckResponse:
        """Report whether a NAS file matches the last synchronized metadata."""
        _validate_mtime(entry.mtime)
        stored = repository.get(entry.path)
        unchanged = stored is not None and stored.size == entry.size and stored.mtime == entry.mtime
        return CheckResponse(unchanged=unchanged, asset_id=stored.asset_id if unchanged else None)

    @app.put("/v1/entries", status_code=status.HTTP_204_NO_CONTENT)
    def put_entry(entry: EntryUpsert, _: dict = Depends(require_gateway_token)) -> None:
        """Persist a mapping after a successful Databox upload or move."""
        _validate_mtime(entry.mtime)
        repository.upsert(Entry(**entry.model_dump()))

    @app.get("/v1/entries", response_model=EntryResponse)
    def get_entry(path: str, _: dict = Depends(require_gateway_token)) -> EntryResponse:
        """Return the mapping needed before a Databox move or soft delete."""
        stored = repository.get(EntryIdentity(path=path, size=0, mtime=0).path)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "path is not synchronized")
        return EntryResponse(**stored.__dict__)

    @app.delete("/v1/entries", response_model=EntryResponse)
    def delete_entry(path: str, _: dict = Depends(require_gateway_token)) -> EntryResponse:
        """Remove and return the mapping after a Databox soft delete succeeds."""
        stored = repository.delete(EntryIdentity(path=path, size=0, mtime=0).path)
        if stored is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "path is not synchronized")
        return EntryResponse(**stored.__dict__)

    return app


def create_production_app() -> FastAPI:
    """Create the configured application for the production ASGI server."""
    return create_app()


def _required_setting(name: str) -> str:
    """Return a required configuration value or fail during startup."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def _validate_mtime(value: float) -> None:
    """Reject invalid filesystem timestamps before writing synchronization state."""
    if not isfinite(value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "mtime must be finite")