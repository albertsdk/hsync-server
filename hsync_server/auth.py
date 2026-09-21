# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

"""
Authentication and authorization for HSYNC Server.

Provides:
- ``AuthContext``: the identity populated on ``request.state`` after verify.
- ``verify_token``: shared token verification (REST + WS), with a KDF cache.
- ``verify_api_key``: FastAPI dependency (Bearer → ``AuthContext``).
- ``require_admin``: FastAPI dependency for admin-only endpoints.

Consumers: ``hsync_server.routers`` (auth_router), ``hsync_server.api``
(buckets, files, events, admin, members), ``hsync_server.api.events`` (WS).
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger as log

from hsync_server.auth_store import (
    _DUMMY_PHC,
    AccountRow,
    AuthStore,
    _verify_secret,
    get_auth_store,
    parse_token,
)
from hsync_server.exceptions import (
    ForbiddenError,
    InvalidApiKeyError,
    MissingApiKeyError,
)

__all__ = [
    "AuthContext",
    "require_admin",
    "verify_api_key",
    "verify_token",
]

# =============================================================================
# AuthContext
# =============================================================================


@dataclass(frozen=True)
class AuthContext:
    """The authenticated identity attached to ``request.state.auth``."""

    id: str
    name: str
    role: str
    key_generation: int


# =============================================================================
# KDF cache (in-process, keyed by SHA-256(token))
# =============================================================================

_CACHE_MAX_ENTRIES = 10000
_cache: dict[str, tuple[str, int, str]] = {}
_cache_lock = threading.Lock()


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(token: str) -> tuple[str, int, str] | None:
    with _cache_lock:
        return _cache.get(_cache_key(token))


def _cache_put(token: str, account_id: str, generation: int, key_kind: str) -> None:
    with _cache_lock:
        # Bound the cache to avoid unbounded growth.
        if len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.clear()
        _cache[_cache_key(token)] = (account_id, generation, key_kind)


def _cache_evict_account(account_id: str) -> None:
    """Evict all cache entries for an account (on rotate/disable/delete)."""
    with _cache_lock:
        to_remove = [k for k, (aid, _, _) in _cache.items() if aid == account_id]
        for k in to_remove:
            _cache.pop(k, None)


def reset_auth_cache() -> None:
    """Clear the KDF cache (tests)."""
    with _cache_lock:
        _cache.clear()


# =============================================================================
# Token verification (shared REST + WS)
# =============================================================================


def verify_token(token: str, store: AuthStore) -> AuthContext:
    """Verify a bearer/ws token and return the :class:`AuthContext`.

    Raises ``InvalidApiKeyError`` on any failure.  The KDF cache stores only
    the scrypt result ``(account_id, generation_at_verify, key_kind)``.  On
    every call the account row is re-read from the DB to check status,
    generation, and expiry.
    """
    parsed = parse_token(token)
    if parsed is None:
        _dummy_verify()
        raise InvalidApiKeyError()

    account_id, secret = parsed

    # Try cache first.
    cached = _cache_get(token)
    if cached is not None:
        cached_id, gen_at_verify, key_kind = cached
        if cached_id == account_id:
            account = store.get_account(account_id)
            if account is not None:
                ctx = _check_account(account, gen_at_verify, key_kind)
                if ctx is not None:
                    return ctx
            # Cache stale — fall through to full verify.

    # Full verify (scrypt).
    account = store.get_account(account_id)
    if account is None:
        _dummy_verify()
        raise InvalidApiKeyError()

    verified_kind: str | None = store.verify_secret_with_kind(secret, account)
    if verified_kind is None:
        log.warning(
            f"[SECURITY] Invalid API key for account {account.id} "
            f"(name={account.name}, prefix={account.key_prefix})"
        )
        raise InvalidApiKeyError()

    # Cache the KDF result.
    _cache_put(token, account_id, account.key_generation, verified_kind)

    ctx = _check_account(account, account.key_generation, verified_kind)
    if ctx is None:
        raise InvalidApiKeyError()
    return ctx


def _check_account(account: AccountRow, gen_at_verify: int, key_kind: str) -> AuthContext | None:
    """Check status/generation/expiry after hash verification.

    Returns ``AuthContext`` on success, ``None`` on failure.
    """
    now = int(time.time())

    # For prior-key verifies, check the grace window.
    if key_kind == "prior":
        if not account.prior_expires_at or now >= account.prior_expires_at:
            return None
    # Active key: generation must match.
    elif gen_at_verify != account.key_generation:
        return None

    if account.status != "active":
        log.warning(f"[SECURITY] Account {account.id} (name={account.name}) is disabled")
        return None

    if account.key_expires_at is not None and now >= account.key_expires_at:
        log.warning(f"[SECURITY] Key for account {account.id} (name={account.name}) has expired")
        return None

    return AuthContext(
        id=account.id,
        name=account.name,
        role=account.role,
        key_generation=account.key_generation,
    )


def _dummy_verify() -> None:
    """Burn CPU to match a real scrypt verify (anti-enumeration).

    Uses the precomputed dummy PHC hash from auth_store for a single
    scrypt verify — exactly the same cost as a real verify.
    """
    _verify_secret("hsync-dummy", _DUMMY_PHC)


# =============================================================================
# FastAPI dependencies
# =============================================================================

_bearer = HTTPBearer(auto_error=False)


def verify_api_key(
    request: Request,
    token: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthContext:
    """FastAPI dependency: Bearer token → ``AuthContext``.

    Missing header → 401 (FastAPI/Starlette default when auto_error=False
    and we raise HTTPException).  Empty credentials → ``MissingApiKeyError``
    (403).  Invalid token → ``InvalidApiKeyError`` (401).
    """
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credentials = token.credentials
    if not credentials:
        log.warning("Authentication attempt with empty Bearer credentials")
        raise MissingApiKeyError()

    store = get_auth_store()
    ctx = verify_token(credentials, store)
    request.state.auth = ctx

    # Throttled last_used_at update.
    store.touch_last_used(ctx.id)

    return ctx


def require_admin(
    ctx: Annotated[AuthContext, Depends(verify_api_key)],
) -> AuthContext:
    """Dependency: caller must be ``role=admin``."""
    if ctx.role != "admin":
        raise ForbiddenError()
    return ctx
