"""Family identity and decision schemas for the synchronization gateway."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class Source(StrEnum):
    """Archive sources participating in central reconciliation."""

    NAS = "nas"
    STORAGEBOX = "storagebox"


class FileRole(StrEnum):
    """Semantic role of a physical file in a media family."""

    MAIN = "main"
    JPEG = "jpeg"
    XMP = "xmp"
    XML = "xml"


class OperationAction(StrEnum):
    """Source-side actions ordered by the gateway."""

    SKIP = "skip"
    UPLOAD_MAIN = "upload-main"
    UPDATE_MAIN = "update-main"
    UPLOAD_RENDITION = "upload-rendition"
    UPDATE_METADATA = "update-metadata"
    ATTACH = "attach"
    RECORD_ALIAS = "record-alias"
    MOVE = "move"
    SOFT_DELETE = "soft-delete"
    NEEDS_RECONCILE = "needs-reconcile"


def validate_archive_path(value: str) -> str:
    """Validate a relative path accepted by the synchronization protocol."""
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("path must be a non-empty relative path")
    if any(segment in {"", ".", ".."} for segment in value.split("/")):
        raise ValueError("path must not contain empty, current, or parent segments")
    return value


class FamilyFile(BaseModel):
    """A cryptographically identified source file submitted by a client."""

    path: str
    role: FileRole
    size: int = Field(ge=0)
    mtime: float
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    document_id: str | None = Field(default=None, max_length=255)
    metadata_fingerprint: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")

    _validate_path = field_validator("path")(validate_archive_path)

    @field_validator("sha256", "metadata_fingerprint")
    @classmethod
    def normalize_hash(cls, value: str | None) -> str | None:
        """Normalize hexadecimal hashes before persistence or comparison."""
        return value.lower() if value else value


class FamilyCheck(BaseModel):
    """A complete RAW/JPEG/XMP/XML family from either archive source."""

    source: Source
    family_path: str
    files: list[FamilyFile] = Field(min_length=1)
    document_id: str | None = Field(default=None, max_length=255)
    metadata_fingerprint: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")

    _validate_family_path = field_validator("family_path")(validate_archive_path)

    @field_validator("metadata_fingerprint")
    @classmethod
    def normalize_family_hash(cls, value: str | None) -> str | None:
        """Normalize an optional family metadata fingerprint."""
        return value.lower() if value else value

    @property
    def main_file(self) -> FamilyFile | None:
        """Return the sole leading source file when present."""
        return next((item for item in self.files if item.role is FileRole.MAIN), None)


class ReconciliationOperation(BaseModel):
    """One action a source client must perform after a gateway decision."""

    action: OperationAction
    path: str | None = None
    role: FileRole | None = None
    asset_id: UUID | None = None
    detail: str | None = None


class CheckResponse(BaseModel):
    """Source-independent reconciliation plan for a submitted family."""

    asset_id: UUID | None = None
    operations: list[ReconciliationOperation]


class FamilyCommit(FamilyCheck):
    """Acknowledgement of a completed Databox operation and its asset ID."""

    asset_id: UUID


class FamilyRecord(BaseModel):
    """Canonical Databox asset identity returned for move and delete operations."""

    asset_id: UUID
    family_path: str
    document_id: str | None = None
