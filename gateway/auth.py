"""Keycloak JWT validation for synchronization callers."""

from dataclasses import dataclass
from time import monotonic

import httpx
import jwt
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials


@dataclass(frozen=True)
class JwtSettings:
    """OpenID Connect settings required to validate a caller token."""

    issuer: str
    jwks_url: str
    client_id: str
    required_role: str
    cache_ttl_seconds: int = 300


class JwksValidator:
    """Validate Keycloak access tokens with a time-bounded JWKS cache."""

    def __init__(self, settings: JwtSettings) -> None:
        """Initialize a validator for the configured Keycloak realm."""
        self.settings = settings
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires_at = 0.0

    def validate(self, credentials: HTTPAuthorizationCredentials) -> dict:
        """Validate a bearer token and return its claims or raise HTTP 401/403."""
        try:
            token = credentials.credentials
            header = jwt.get_unverified_header(token)
            key = self._get_key(header["kid"])
            claims = jwt.decode(token, key.key, algorithms=[header["alg"]], issuer=self.settings.issuer, options={"verify_aud": False})
        except (jwt.InvalidTokenError, KeyError, httpx.HTTPError) as error:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token") from error
        if claims.get("azp") != self.settings.client_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "unexpected OAuth client")
        if self.settings.required_role not in claims.get("roles", []):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing gateway role")
        return claims

    def _get_key(self, key_id: str) -> jwt.PyJWK:
        """Return the named signing key, refreshing JWKS when necessary."""
        if monotonic() >= self._expires_at or key_id not in self._keys:
            response = httpx.get(self.settings.jwks_url, timeout=5.0)
            response.raise_for_status()
            self._keys = {
                item["kid"]: jwt.PyJWK.from_dict(item)
                for item in response.json()["keys"]
                if item.get("kid") and item.get("use") == "sig"
            }
            self._expires_at = monotonic() + self.settings.cache_ttl_seconds
        if key_id not in self._keys:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown signing key")
        return self._keys[key_id]