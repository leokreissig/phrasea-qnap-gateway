"""Request and response schemas for the synchronization gateway."""

from uuid import UUID

from pydantic import BaseModel, Field, field_validator


def validate_archive_path(value: str) -> str:
    """Validate a relative archive path accepted by the synchronization protocol."""
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("path must be a non-empty relative path")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("path must not contain empty, current, or parent segments")
    return value


class EntryIdentity(BaseModel):
    """Identity data used to decide whether an archive entry changed."""

    path: str
    size: int = Field(ge=0)
    mtime: float

    _validate_path = field_validator("path")(validate_archive_path)


class EntryUpsert(EntryIdentity):
    """Metadata persisted after a successful Databox upload or move."""

    asset_id: UUID


class CheckResponse(BaseModel):
    """Result of comparing a NAS file with the persisted synchronization state."""

    unchanged: bool
    asset_id: UUID | None = None


class EntryResponse(EntryUpsert):
    """Persisted synchronization metadata returned to a caller."""