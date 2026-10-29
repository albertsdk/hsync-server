# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""Account-secret primitives: id/secret minting, token parsing, scrypt hashing.

Pure, stateless helpers shared by the store mixins. Nothing here touches the
database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime

from hsync_server.constants import ACCOUNT_ID_ALPHABET, ACCOUNT_ID_LEN, mint_alnum_id

__all__ = [
    "ACCOUNT_ID_ALPHABET",
    "ACCOUNT_ID_LEN",
    "RENDEZVOUS_ADMIN_ID",
    "RENDEZVOUS_ADMIN_NAME",
    "KEY_PREFIX_LEN",
    "LAST_USED_THROTTLE",
    "SALT_LEN",
    "SCRYPT_DKLEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SECRET_HEX_LEN",
    "TOKEN_PREFIX",
    "mint_account_id",
    "mint_secret",
    "parse_expiry",
    "parse_token",
    "validate_name",
]

TOKEN_PREFIX = "hs_"
SECRET_HEX_LEN = 64  # secrets.token_hex(32)
KEY_PREFIX_LEN = 8  # first 8 hex chars of the active secret

# Precompiled account-id pattern derived from the SSOT alphabet: one C-level
# fullmatch replaces the 21-iteration Python generator that parse_token ran on
# the per-request auth hot path. The id length is pinned by ACCOUNT_ID_LEN
# (regular accounts); the reserved 22-char RENDEZVOUS_ADMIN_ID is special-cased
# in parse_token, so the pattern itself stays on the standard length.
_ACCOUNT_ID_RE = re.compile(f"^[{re.escape(ACCOUNT_ID_ALPHABET)}]{{{ACCOUNT_ID_LEN}}}$")

# scrypt parameters (named, not magic numbers; overridable for tests via env).
# Production: N=2**17, r=8, p=1 (~128 MB, sub-second verify).
# Tests: set HSYNC_SCRYPT_N=16 in env for speed.
SCRYPT_N = int(os.environ.get("HSYNC_SCRYPT_N", str(2**17)))
SCRYPT_R = int(os.environ.get("HSYNC_SCRYPT_R", "8"))
SCRYPT_P = int(os.environ.get("HSYNC_SCRYPT_P", "1"))
SCRYPT_DKLEN = 32
SALT_LEN = 16
# scrypt requires ~128*N*r bytes; with N=2^17, r=8 → ~128 MB.
SCRYPT_MAXMEM = int(os.environ.get("HSYNC_SCRYPT_MAXMEM", str(256 * 1024 * 1024)))

# Rendezvous
RENDEZVOUS_ADMIN_ID = "HsyncAdminRendezvous01"
RENDEZVOUS_ADMIN_NAME = "admin"

# Name validation
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# last_used_at throttle window (seconds)
LAST_USED_THROTTLE = 300  # seconds (5 minutes)
# Number of `$`-separated fields in a PHC scrypt hash: scrypt$<params>$<salt>$<hash>.
_SCRYPT_PHC_PARTS = 5


def mint_account_id() -> str:
    """Generate a 21-char alphanumeric account id (same style as ``get_server_cid``).

    The reserved rendezvous id ``RENDEZVOUS_ADMIN_ID`` is never minted here.
    """
    while True:
        cid = mint_alnum_id()
        if cid != RENDEZVOUS_ADMIN_ID:
            return cid


def mint_secret() -> str:
    """Generate a 64-char lowercase hex secret (``secrets.token_hex(32)``)."""
    return secrets.token_hex(32)


def parse_token(token: str) -> tuple[str, str] | None:
    """Parse ``hs_<account_id>_<secret 64 hex>`` → ``(id, secret)``.

    Standard account ids are 21 alnum chars (``ACCOUNT_ID_LEN``); the reserved
    rendezvous admin id (``RENDEZVOUS_ADMIN_ID``) is 22 chars and is accepted
    as the single length exception — it is never minted for regular accounts,
    so no other 22-char id is valid.  Returns ``None`` if the format is
    invalid.  The caller must run a dummy scrypt on the failure path to keep
    timing uniform.
    """
    if not token.startswith(TOKEN_PREFIX):
        return None
    rest = token.removeprefix(TOKEN_PREFIX)
    sep = rest.find("_")
    if sep != ACCOUNT_ID_LEN and sep != len(RENDEZVOUS_ADMIN_ID):
        return None
    account_id = rest[:sep]
    secret = rest[sep + 1 :]
    if len(secret) != SECRET_HEX_LEN:
        return None
    if account_id != RENDEZVOUS_ADMIN_ID and not _ACCOUNT_ID_RE.fullmatch(account_id):
        return None
    # Length is already validated above (== SECRET_HEX_LEN), so the precompiled
    # {64} pattern is equivalent to the ad-hoc `[0-9a-f]+` fullmatch compiled
    # here on every request.
    if not _SECRET_HEX_RE.fullmatch(secret):
        return None
    return account_id, secret


def _hash_secret(secret: str, salt: bytes | None = None) -> str:
    """Return a PHC-style scrypt hash string for *secret*.

    Format: ``$scrypt$ln=<log2(N)>,r=<r>,p=<p>$<salt b64>$<hash b64>``
    """
    if salt is None:
        salt = secrets.token_bytes(SALT_LEN)
    derived = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return (
        f"$scrypt$ln={SCRYPT_N.bit_length() - 1},r={SCRYPT_R},p={SCRYPT_P}"
        f"${base64.b64encode(salt).decode()}"
        f"${base64.b64encode(derived).decode()}"
    )


def _verify_secret(secret: str, phc_hash: str) -> bool:
    """Verify *secret* against a PHC-style scrypt hash (constant-time)."""
    try:
        parts = phc_hash.split("$")
        if len(parts) != _SCRYPT_PHC_PARTS or parts[1] != "scrypt":
            return False
        params = dict(p.split("=") for p in parts[2].split(","))
        n = 1 << int(params["ln"])
        r = int(params["r"])
        p = int(params["p"])
        salt = base64.b64decode(parts[3])
        expected = base64.b64decode(parts[4])
        derived = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(derived, expected)
    except (KeyError, ValueError, TypeError):
        return False


# Pre-computed dummy hash for timing-safe "account unknown" path.
_DUMMY_PHC = _hash_secret("hsync-dummy", secrets.token_bytes(SALT_LEN))


def _key_prefix(secret: str) -> str:
    """First ``KEY_PREFIX_LEN`` hex chars of the secret (for logs/audit)."""
    return secret[:KEY_PREFIX_LEN]


def validate_name(name: str) -> str:
    """Validate and return a normalised account name."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid account name '{name}': 1-64 chars, "
            "alphanumeric with . _ - (not starting with . _ -)."
        )
    return name


def parse_expiry(expires_at: str | None, valid_days: int | None) -> int | None:
    """Convert ``expires_at`` (RFC3339 UTC) or ``valid_days`` to a Unix UTC timestamp.

    Raises ``ValueError`` when both are given, or when ``expires_at`` is not a
    valid timezone-aware timestamp. Returns ``None`` when neither is given.

    Consumers: ``hsync_server.api.admin`` (REST), ``hsync_server.__main__`` (CLI).
    """
    if expires_at is not None and valid_days is not None:
        raise ValueError("Provide at most one of 'expires_at' or 'valid_days'.")
    if expires_at is not None:
        try:
            dt = datetime.fromisoformat(expires_at)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid expires_at: {e}") from None
        if dt.tzinfo is None:
            raise ValueError("Timestamp must include timezone (UTC).")
        return int(dt.timestamp())
    if valid_days is not None:
        return int(time.time()) + valid_days * 86400
    return None
