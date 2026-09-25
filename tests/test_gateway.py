"""Tests for source-independent family reconciliation decisions."""

from uuid import uuid4

from fastapi.testclient import TestClient

from gateway.main import create_app
from gateway.repository import InMemoryFamilyRepository


class AllowAllValidator:
    """Test-only validator that accepts every syntactically valid bearer header."""

    def validate(self, _credentials):
        """Return fixed gateway claims for unit tests."""
        return {"azp": "phrasea-sync-gateway", "roles": ["sync-gateway-access"]}


def client() -> TestClient:
    """Create a test client with in-memory identity persistence."""
    return TestClient(create_app(repository=InMemoryFamilyRepository(), validator=AllowAllValidator()))


def headers() -> dict[str, str]:
    """Return a test authorization header."""
    return {"Authorization": "Bearer test-token"}


def family(source: str, path: str, sha256: str, document_id: str | None = None) -> dict:
    """Return a complete RAW/JPEG/XMP family description for tests."""
    return {
        "source": source,
        "family_path": path,
        "document_id": document_id,
        "metadata_fingerprint": "b" * 64,
        "files": [
            {"path": path + ".dng", "role": "main", "size": 100, "mtime": 10, "sha256": sha256},
            {"path": path + ".jpg", "role": "jpeg", "size": 10, "mtime": 10, "sha256": "c" * 64},
            {"path": path + ".xmp", "role": "xmp", "size": 1, "mtime": 10, "sha256": "d" * 64},
        ],
    }


def test_first_source_receives_full_family_plan() -> None:
    """A new family gets one ordered plan instead of separate client logic."""
    response = client().post("/v1/check", json=family("storagebox", "2012/IMG_0001", "a" * 64), headers=headers())
    assert [item["action"] for item in response.json()["operations"]] == ["upload-main", "upload-rendition", "update-metadata"]


def test_second_source_is_recognized_by_hash() -> None:
    """NAS and Storage Box copies converge on one Databox asset by content hash."""
    api = client()
    original = family("storagebox", "2012/IMG_0001", "a" * 64, "xmp.did:abc")
    asset_id = str(uuid4())
    assert api.put("/v1/entries", json={**original, "asset_id": asset_id}, headers=headers()).status_code == 200

    second = family("nas", "2012/moved/IMG_0001", "a" * 64, "xmp.did:abc")
    response = api.post("/v1/check", json=second, headers=headers()).json()
    assert response["asset_id"] == asset_id
    assert response["operations"][0]["action"] == "record-alias"


def test_changed_xmp_requests_metadata_update() -> None:
    """A known image with changed XMP does not trigger a main reupload."""
    api = client()
    original = family("storagebox", "2012/IMG_0001", "a" * 64)
    asset_id = str(uuid4())
    api.put("/v1/entries", json={**original, "asset_id": asset_id}, headers=headers())
    changed = family("nas", "2012/IMG_0001", "a" * 64)
    changed["metadata_fingerprint"] = "e" * 64
    actions = [item["action"] for item in api.post("/v1/check", json=changed, headers=headers()).json()["operations"]]
    assert actions == ["record-alias", "update-metadata", "upload-rendition"]


def test_rejects_incomplete_family() -> None:
    """Reject a request that would make identity decisions without a main file."""
    response = client().post("/v1/check", json={"source": "nas", "family_path": "bad", "files": [{"path": "bad.xmp", "role": "xmp", "size": 1, "mtime": 1, "sha256": "a" * 64}]}, headers=headers())
    assert response.status_code == 422
