"""
OAuth 2.0 Authorization Server — minimal in-memory implementation.

• Single pre-registered client (from OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET).
• Access tokens:  in-memory, 1 h TTL.
• Refresh tokens: persisted to tokens.json — survive server restarts.
• /authorize:     auto-approves without user interaction.
"""

import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

_TOKENS_FILE = Path(__file__).parent / "tokens.json"


class SimpleOAuthProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        base_url: str,
    ) -> None:
        self._base_url = base_url
        self._clients: dict[str, OAuthClientInformationFull] = {
            client_id: OAuthClientInformationFull(
                client_id=client_id,
                client_id_issued_at=0,
                client_secret=client_secret,
                client_secret_expires_at=None,
                redirect_uris=[AnyUrl(redirect_uri)],
                token_endpoint_auth_method="client_secret_post",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="mcp",
            )
        }
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._load()

    # ── Persistence ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not _TOKENS_FILE.exists():
            return
        try:
            data = json.loads(_TOKENS_FILE.read_text())
            for tok, info in data.items():
                self._refresh_tokens[tok] = RefreshToken(
                    token=tok,
                    client_id=info["client_id"],
                    scopes=info["scopes"],
                )
        except Exception:
            pass

    def _save(self) -> None:
        try:
            data = {
                tok: {"client_id": rt.client_id, "scopes": rt.scopes}
                for tok, rt in self._refresh_tokens.items()
            }
            _TOKENS_FILE.write_text(json.dumps(data))
            _TOKENS_FILE.chmod(0o600)
        except Exception:
            pass

    # ── Client registry ────────────────────────────────────────────────────────
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError("Dynamic client registration is disabled")

    # ── Authorization: auto-approve ────────────────────────────────────────────
    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        code = secrets.token_urlsafe(24)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or ["mcp"],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
        )
        parsed = urlparse(str(params.redirect_uri))
        qs = parse_qs(parsed.query)
        qs["code"] = [code]
        if params.state:
            qs["state"] = [params.state]
        return urlunparse(
            parsed._replace(query=urlencode({k: v[0] for k, v in qs.items()}))
        )

    # ── Authorization codes ────────────────────────────────────────────────────
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        del self._auth_codes[authorization_code.code]
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 3600
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=expires_at,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
        )
        self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=3600,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    # ── Refresh tokens ─────────────────────────────────────────────────────────
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        del self._refresh_tokens[refresh_token.token]
        old = [t for t, a in self._access_tokens.items() if a.client_id == client.client_id]
        for tok in old:
            del self._access_tokens[tok]
        access_token = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 3600
        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=expires_at,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh,
            client_id=client.client_id,
            scopes=scopes,
        )
        self._save()
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=3600,
            refresh_token=new_refresh,
            scope=" ".join(scopes),
        )

    # ── Access tokens ──────────────────────────────────────────────────────────
    async def load_access_token(self, token: str) -> AccessToken | None:
        at = self._access_tokens.get(token)
        if at is None:
            return None
        if at.expires_at is not None and at.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return at

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._access_tokens.pop(token.token, None)
        else:
            self._refresh_tokens.pop(token.token, None)
            self._save()
