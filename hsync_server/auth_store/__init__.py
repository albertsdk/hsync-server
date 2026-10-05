# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

"""Server-only account and membership store backed by ``state DB``.

This package is the single DRY path for REST, WS, CLI and bootstrap auth
state.  The implementation is split by responsibility:

* ``credentials`` — id/secret minting, token parsing, scrypt hashing (pure)
* ``models`` — ``AccountRow``/``MemberRow`` and ``TargetNotFoundError``
* ``db`` — SQLite connection, schema and transactions (``DbMixin``)
* ``accounts`` — account lifecycle (``AccountsMixin``)
* ``membership`` — membership/ownership (``MembershipMixin``)
* ``buckets`` — bucket inventory and deletion (``BucketsMixin``)
* ``store`` — the composed ``AuthStore`` and its process-wide cache

Every name historically exported by ``hsync_server.auth_store`` is re-exported
here, so ``from hsync_server.auth_store import <name>`` keeps working unchanged.

Consumers: ``hsync_server.auth`` (verify + AuthContext), ``hsync_server.api``
(buckets, admin, members, events), ``hsync_server.__main__`` (CLI admin),
``hsync_server.main`` (lifespan bootstrap).
"""

from hsync_server.exceptions import ConflictError, ForbiddenError

from .accounts import AccountsMixin
from .buckets import BucketsMixin, _iter_bucket_dirs
from .credentials import (
    _DUMMY_PHC,
    ACCOUNT_ID_ALPHABET,
    ACCOUNT_ID_LEN,
    BOOTSTRAP_ADMIN_ID,
    BOOTSTRAP_ADMIN_NAME,
    KEY_PREFIX_LEN,
    LAST_USED_THROTTLE,
    SALT_LEN,
    SCRYPT_DKLEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    SECRET_HEX_LEN,
    TOKEN_PREFIX,
    _hash_secret,
    _key_prefix,
    _verify_secret,
    mint_account_id,
    mint_secret,
    parse_expiry,
    parse_token,
    validate_name,
)
from .db import DbMixin
from .membership import MembershipMixin
from .models import AccountRow, MemberRow, TargetNotFoundError
from .store import AuthStore, get_auth_store, reset_auth_cache

__all__ = [
    "ACCOUNT_ID_ALPHABET",
    "ACCOUNT_ID_LEN",
    "BOOTSTRAP_ADMIN_ID",
    "BOOTSTRAP_ADMIN_NAME",
    "KEY_PREFIX_LEN",
    "LAST_USED_THROTTLE",
    "SALT_LEN",
    "SCRYPT_DKLEN",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "SECRET_HEX_LEN",
    "TOKEN_PREFIX",
    "AccountRow",
    "AccountsMixin",
    "AuthStore",
    "BucketsMixin",
    "ConflictError",
    "DbMixin",
    "ForbiddenError",
    "MemberRow",
    "MembershipMixin",
    "TargetNotFoundError",
    "_DUMMY_PHC",
    "_hash_secret",
    "_iter_bucket_dirs",
    "_key_prefix",
    "_verify_secret",
    "get_auth_store",
    "mint_account_id",
    "mint_secret",
    "parse_expiry",
    "parse_token",
    "reset_auth_cache",
    "validate_name",
]
