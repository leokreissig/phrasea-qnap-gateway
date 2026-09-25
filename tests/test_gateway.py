"""Unit tests for synchronization metadata behavior."""

from uuid import uuid4

from fastapi.testclient import TestClient

from gateway.main import create_app
from gateway.repository import InMemoryEntryRepository


class AllowAllValidator:
    """Test-only validator that accepts any bearer credential."""

    def validate(self, _credentials):
        """Return a fixed set of gateway claims for tests."""
        return {"azp": "phrasea-sync-gateway", "roles": ["sync-gateway-access"]}


def make_client() -> TestClient:
    """Build a gateway client with in-memory storage and token validation."""
    return TestClient(create_app(repository=InMemoryEntryRepository(), validator=AllowAllValidator()))


def headers() -> dict[str, str]:
    """Return a syntactically valid bearer header for the test validator."""
    return {"Authorization": "Bearer test-token"}


def test_entry_lifecycle() -> None:
    """Persist, compare, retrieve, and remove a synchronization entry."""
    client = make_client()
    asset_id = str(uuid4())
    payload = {"path": "2026/RAW/image.dng", "asset_id": asset_id, "size": 42, "mtime": 123.5}
    assert client.post("/v1/check", json={"path": payload["path"], "size": 42, "mtime": 123.5}, headers=headers()).json() == {"unchanged": False, "asset_id": None}
    assert client.put("/v1/entries", json=payload, headers=headers()).status_code == 204
    assert client.post("/v1/check", json={"path": payload["path"], "size": 42, "mtime": 123.5}, headers=headers()).json() == {"unchanged": True, "asset_id": asset_id}
    assert client.get("/v1/entries", params={"path": payload["path"]}, headers=headers()).status_code == 200
    assert client.delete("/v1/entries", params={"path": payload["path"]}, headers=headers()).status_code == 200
    assert client.get("/v1/entries", params={"path": payload["path"]}, headers=headers()).status_code == 404


def test_rejects_parent_paths() -> None:
    """Reject paths that could escape the synchronized archive root."""
    response = make_client().post("/v1/check", json={"path": "../outside", "size": 1, "mtime": 1}, headers=headers())
    assert response.status_code == 422