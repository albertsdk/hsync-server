# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If received, this code can be found at the source:
# https://github.com/albertsdk/hsync

"""Row models and store-specific exceptions for the auth store."""

from __future__ import annotations

from dataclasses import dataclass

from hsync_server.exceptions import ResourceNotFoundError

__all__ = ["AccountRow", "MemberRow", "TargetNotFoundError"]


@dataclass(frozen=True)
class AccountRow:
    """A row from the ``clients`` table (never contains the secret)."""

    id: str
    name: str
    role: str
    status: str
    key_hash: str
    key_prefix: str
    key_generation: int
    key_expires_at: int | None
    prior_key_hash: str | None
    prior_expires_at: int | None
    created_at: int
    last_used_at: int | None


@dataclass(frozen=True)
class MemberRow:
    """A row from the ``bucket_members`` table."""

    bucket: str
    client_id: str
    role: str


class TargetNotFoundError(ResourceNotFoundError):
    """A referenced account or bucket does not exist (HTTP 404)."""
