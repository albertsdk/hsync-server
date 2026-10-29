"""Tests for the account-based authentication system (hsync_server.auth)."""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from hsync_server.auth import AuthContext, require_admin, verify_api_key
from hsync_server.auth_store import get_auth_store
from hsync_server.error_handlers import register_exception_handlers


def _make_app() -> FastAPI:
    """Create a minimal FastAPI app with exception handlers registered."""
    app = FastAPI()
    register_exception_handlers(app)
    return app


class TestTokenVerification:
    """Tests for token parsing and verification."""

    def test_valid_token_accepted(self, test_token):
        """A valid admin token authenticates successfully."""
        app = _make_app()

        @app.get("/test")
        async def test_endpoint(ctx: Annotated[AuthContext, Depends(verify_api_key)]):
            return {"id": ctx.id, "name": ctx.name, "role": ctx.role}

        client = TestClient(app)
        r = client.get("/test", headers={"Authorization": f"Bearer {test_token}"})
        assert r.status_code == 200
        assert r.json()["role"] == "admin"

    def test_missing_bearer_returns_401(self):
        """Missing Authorization header returns 401."""
        app = _make_app()

        @app.get("/test")
        async def test_endpoint(ctx: Annotated[AuthContext, Depends(verify_api_key)]):
            return {"id": ctx.id}

        client = TestClient(app)
        r = client.get("/test")
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, test_token):
        """An invalid token returns 401."""
        app = _make_app()

        @app.get("/test")
        async def test_endpoint(ctx: Annotated[AuthContext, Depends(verify_api_key)]):
            return {"id": ctx.id}

        client = TestClient(app)
        r = client.get("/test", headers={"Authorization": "Bearer hs_invalid_token_here"})
        assert r.status_code == 401

    def test_malformed_token_returns_401(self):
        """A non-hs_ token returns 401."""
        app = _make_app()

        @app.get("/test")
        async def test_endpoint(ctx: Annotated[AuthContext, Depends(verify_api_key)]):
            return {"id": ctx.id}

        client = TestClient(app)
        r = client.get("/test", headers={"Authorization": "Bearer not-a-valid-token"})
        assert r.status_code == 401


class TestRequireAdmin:
    """Tests for the require_admin dependency."""

    def test_admin_can_access_admin_endpoint(self, test_token):
        """Admin role can access admin-only endpoints."""
        app = _make_app()

        @app.get("/admin")
        async def admin_endpoint(ctx: Annotated[AuthContext, Depends(require_admin)]):
            return {"role": ctx.role}

        client = TestClient(app)
        r = client.get("/admin", headers={"Authorization": f"Bearer {test_token}"})
        assert r.status_code == 200
        assert r.json()["role"] == "admin"

    def test_non_admin_rejected_from_admin_endpoint(self):
        """A client role account gets 403 on admin endpoints."""

        store = get_auth_store()
        _, token = store.create_account("test-client", "client")

        app = _make_app()

        @app.get("/admin")
        async def admin_endpoint(ctx: Annotated[AuthContext, Depends(require_admin)]):
            return {"role": ctx.role}

        client = TestClient(app)
        r = client.get("/admin", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403


class TestTokenParsing:
    """Tests for the token format parser."""

    def test_valid_token_parsed(self):
        from hsync_server.auth_store import parse_token

        result = parse_token("hs_HsyncAdminRendezvous01_" + "0" * 64)
        assert result is not None
        assert result[0] == "HsyncAdminRendezvous01"
        assert result[1] == "0" * 64

    def test_invalid_prefix_rejected(self):
        from hsync_server.auth_store import parse_token

        assert parse_token("xx_HsyncAdminRendezvous01_" + "0" * 64) is None

    def test_missing_secret_rejected(self):
        from hsync_server.auth_store import parse_token

        assert parse_token("hs_HsyncAdminRendezvous01") is None

    def test_short_secret_rejected(self):
        from hsync_server.auth_store import parse_token

        assert parse_token("hs_HsyncAdminRendezvous01_short") is None

    def test_non_hex_secret_rejected(self):
        from hsync_server.auth_store import parse_token

        assert parse_token("hs_HsyncAdminRendezvous01_" + "g" * 64) is None
