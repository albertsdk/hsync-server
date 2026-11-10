# HSYNC Server — WebSocket (`api/events.py` + `api/events_handlers/`)

Real-time event fan-out. One WS endpoint; events received from one client are
applied to disk and broadcast to the other subscribers of the same bucket.

## Endpoint

`WS /api/v1/buckets/{bucket}/events/{cid}` → `ws_events` (`hsync_server/api/events.py::ws_events`), declared
on `public_router` (WS can't use header auth; auth is the first frame).

## Handshake (shared SSOT)

Uses `hsync_common.protocol.handshake` (imported in `api/events.py`):
- Client sends an auth frame; server verifies the account token via
  `verify_token` (`auth.py`, scrypt + KDF cache) — run on an asyncio worker
  thread (`asyncio.to_thread` in `_dispatching_authenticate`,
  `hsync_server/api/events.py::_dispatching_authenticate`)
  so the CPU-heavy scrypt never blocks the loop during a handshake — and
  **hard-fails on `protocol_version` mismatch** against `PROTOCOL_VERSION_WS`.
- After auth, the server checks bucket existence + membership; failure closes
  with `4009` (indistinguishable from "bucket not found").
- Replies with `build_ws_auth_ok` / `build_ws_auth_error`; client capabilities
  via `decode_capabilities`; the reply advertises the server's **honest**
  capability set (ADR-0024): `tombstones` + `vector_clocks` unconditionally,
  `compression` when `get_compression_enabled()`, `delta` when
  `get_delta_enabled()`.
- Auth must arrive within `get_ws_auth_timeout()` (`hsync_server/config.py::get_ws_auth_timeout`); otherwise the
  connection is dropped.

## Connection registry (`api/connection_manager.py`)

| Object | Location | Role |
| ------ | -------- | ---- |
| `ConnectionManager` | `hsync_server/api/connection_manager.py::ConnectionManager` | per-bucket `{cid: WebSocket}`; `disconnect`/`send_message`/`send_presence` |
| `BucketConnectionManager` | `hsync_server/api/connection_manager.py::BucketConnectionManager` | per-bucket pools + global slot accounting for DoS protection + `presence_counts` |
| `bucket_manager` (global) | `hsync_server/api/connection_manager.py::bucket_manager` | the singleton used by REST and WS to broadcast |

`_send_to_all` (`hsync_server/api/connection_manager.py::ConnectionManager._send_to_all`) is the shared broadcast path: it
re-checks each recipient's authz (account active + bucket access) and evicts
dead sockets per-connection. `send_message` (`hsync_server/api/connection_manager.py::ConnectionManager.send_message`) skips the originating
`cid` so senders don't echo; `send_presence` (`hsync_server/api/connection_manager.py::ConnectionManager.send_presence`) has no sender filter.
`presence_counts` (`hsync_server/api/connection_manager.py::BucketConnectionManager.presence_counts`) computes the display-ready `(connected, total)` pair:
`total` = non-admin bucket members (`AuthStore.get_share_member_ids`), `connected`
= those members with a live socket on this worker. `ws_events` broadcasts a
`presence` frame on every join/leave (`hsync_server/api/events.py::_broadcast_presence`), and the
REST endpoint `GET /api/v1/buckets/{bucket}/presence` exposes the same pair
(`api/presence.py`). Slot caps: `get_ws_max_connections` (`hsync_server/config.py::get_ws_max_connections`) and
`get_ws_max_connections_per_ip` (`hsync_server/config.py::get_ws_max_connections_per_ip`).

**Deployment contract**: the server supports a **single worker only**. The
connection registry and presence counters are in-process, so a multi-worker
deployment would under-count `connected` (and break broadcast fan-out). If a
multi-worker setup is ever introduced, the registry must move to shared storage
first.

## Heartbeat & message rate

- Ping/pong using `get_ws_heartbeat_interval` (`hsync_server/config.py::get_ws_heartbeat_interval`) and
  `get_ws_heartbeat_timeout` (`hsync_server/config.py::get_ws_heartbeat_timeout`).
- Per-connection message budget via a `TokenBucket` from
  `create_ws_message_bucket` (`hsync_server/rate_limiter.py::create_ws_message_bucket`, `TokenBucket` `hsync_server/rate_limiter.py::TokenBucket`).

## Event handlers (`api/events_handlers/`)

The handlers live in `events_handlers/handlers.py`:

| Event (`EventName`) | Handler | Location |
| ------------------- | ------- | -------- |
| `added` | `handle_added_event` | `hsync_server/api/events_handlers/handlers.py::handle_added_event` |
| `modified` | `handle_modified_event` | `hsync_server/api/events_handlers/handlers.py::handle_modified_event` |
| `deleted` | `handle_deleted_event` | `hsync_server/api/events_handlers/handlers.py::handle_deleted_event` |
| `renamed` | `handle_renamed_event` | `hsync_server/api/events_handlers/handlers.py::handle_renamed_event` |
| `symlink` | `handle_symlink_event` | `hsync_server/api/events_handlers/handlers.py::handle_symlink_event` |

`EventName` comes from `hsync-common/hsync_common/models/event.py::EventName` (imported in `api/events.py`). Each handler
mutates the bucket on disk, keeps the hash cache / tombstones consistent, then the
event is rebroadcast. `renamed` additionally carries the causal version entries
across the rename (`hsync_server/versions.py::move_version` / `hsync_server/versions.py::move_version_prefix`), drops any stale
deletion markers at the destination (`hsync_server/tombstones.py::clear_deletion` / `hsync_server/tombstones.py::clear_deletion_tree`)
and treats this bookkeeping as best-effort so it never aborts the broadcast;
`deleted` records the causal tombstone through the shared
`hsync_server/tombstones.py::record_path_deletion`.

`renamed` is **idempotent for a moved subtree**: a directory move is reported
both for the directory and for its descendants (the polling watcher and the
scan-based pairing do that), and the ancestor event is applied first. A
descendant whose source is already gone while its destination exists is an
already-applied rename, skipped at debug level without a rebroadcast or an error
(the ancestor's broadcast already covers it); a missing source **without** a
destination still raises, so genuine failures are not hidden. The success
`RENAME` line is logged only after the on-disk rename succeeds, so a failed
rename never reads as "Renamed" in the log.

## Path safety (`api/events_handlers/validation.py`)

- `is_path_within_bucket` (`hsync_server/api/events_handlers/validation.py::is_path_within_bucket`) — containment guard for every
  write/delete target.
- Symlink targets are checked with `is_safe_symlink_target` (SSOT in
  `hsync_common.path_validation`) — blocks symlink escapes outside the bucket.
- Wire-path gate in `events.py` (`hsync_server/api/events.py::_event_paths_safe`): every `src`/`dst`
  `filepath`/`path` is validated with `validate_remote_relpath` before dispatch
  (traversal, absolute paths, backslashes, internal engine artifacts rejected);
  when a payload carries both keys, *all* of them must validate, so a
  malicious second key cannot smuggle past the gate;
  the connection `cid` is validated with `is_valid_cid` at accept time, and
  the `{bucket}` URL segment with `is_valid_bucket` (a `..` segment via
  `%2e%2e` would otherwise escape the storage root).
- Connection registry (`connection_manager.py`): a reconnecting cid explicitly
  evicts the stale registration (no silent hijack); `disconnect(cid, websocket)`
  only drops the entry if it still belongs to the closing socket; broadcast
  (`send_message`) iterates a snapshot and evicts dead sockets per-connection
  instead of aborting mid-broadcast.

## Deleted-event semantics

`handle_deleted_event` records a tombstone (`hsync_server/tombstones.py::record_deletion`) so the deletion
propagates and survives peer reconciliation; uploads of the same path later clear
it (`clear_deletion`, `hsync_server/tombstones.py::clear_deletion`). For a recursive directory `deleted`,
the descendant walk + hashing (`record_deletion_tree`,
`hsync_server/tombstones.py::record_deletion_tree`, shared with the
rename endpoint) and the `rmtree`
run **synchronously on the loop on purpose**: offloading them would let an
upload publish a new descendant mid-walk that the `rmtree` then removes without
a tombstone (a resurrection risk). Full loop-freedom here needs a bucket/
tree mutation lock shared with upload — a documented follow-up.

> Human-readable WS sequence diagram (WebSocket Event Flow): `../docs/internal/architecture.md`.
> The machine-readable WS contract is the generated schema in `hsync-common`
> (`hsync-common/hsync_common/protocol/ws/ws-schema.json`).
