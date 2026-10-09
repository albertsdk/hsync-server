# HSYNC Server — WebSocket (`api/events.py` + `api/events_handlers/`)

Real-time event fan-out. One WS endpoint; events received from one client are
applied to disk and broadcast to the other subscribers of the same bucket.

## Endpoint

`WS /api/v1/buckets/{bucket}/events/{cid}` → `ws_events` (`api/events.py::ws_events`), declared
on `public_router` (WS can't use header auth; auth is the first frame).

## Handshake (shared SSOT)

Uses `hsync_common.protocol.handshake` (imported in `api/events.py`):
- Client sends an auth frame; server verifies the account token via
  `verify_token` (`auth.py`, scrypt + KDF cache) — run on an asyncio worker
  thread (`asyncio.to_thread` in `_dispatching_authenticate`,
  `api/events.py::_dispatching_authenticate`)
  so the CPU-heavy scrypt never blocks the loop during a handshake — and
  **hard-fails on `protocol_version` mismatch** against `PROTOCOL_VERSION_WS`.
- After auth, the server checks bucket existence + membership; failure closes
  with `4009` (indistinguishable from "bucket not found").
- Replies with `build_ws_auth_ok` / `build_ws_auth_error`; client capabilities
  via `decode_capabilities`; the reply advertises the server's **honest**
  capability set (ADR-0024): `tombstones` + `vector_clocks` unconditionally,
  `compression` when `get_compression_enabled()`, `delta` when
  `get_delta_enabled()`.
- Auth must arrive within `get_ws_auth_timeout()` (`config.py::get_ws_auth_timeout`); otherwise the
  connection is dropped.

## Connection registry (`api/connection_manager.py`)

| Object | Location | Role |
| ------ | -------- | ---- |
| `ConnectionManager` | `api/connection_manager.py::ConnectionManager` | per-bucket `{cid: WebSocket}`; `disconnect`/`send_message`/`send_presence` |
| `BucketConnectionManager` | `api/connection_manager.py::BucketConnectionManager` | per-bucket pools + global slot accounting for DoS protection + `presence_counts` |
| `bucket_manager` (global) | `api/connection_manager.py::bucket_manager` | the singleton used by REST and WS to broadcast |

`_send_to_all` (`api/connection_manager.py::ConnectionManager._send_to_all`) is the shared broadcast path: it
re-checks each recipient's authz (account active + bucket access) and evicts
dead sockets per-connection. `send_message` (`api/connection_manager.py::ConnectionManager.send_message`) skips the originating
`cid` so senders don't echo; `send_presence` (`api/connection_manager.py::ConnectionManager.send_presence`) has no sender filter.
`presence_counts` (`api/connection_manager.py::BucketConnectionManager.presence_counts`) computes the display-ready `(connected, total)` pair:
`total` = non-admin bucket members (`AuthStore.get_share_member_ids`), `connected`
= those members with a live socket on this worker. `ws_events` broadcasts a
`presence` frame on every join/leave (`api/events.py::_broadcast_presence`), and the
REST endpoint `GET /api/v1/buckets/{bucket}/presence` exposes the same pair
(`api/presence.py`). Slot caps: `get_ws_max_connections` (`config.py::get_ws_max_connections`) and
`get_ws_max_connections_per_ip` (`config.py::get_ws_max_connections_per_ip`).

**Deployment contract**: the server supports a **single worker only**. The
connection registry and presence counters are in-process, so a multi-worker
deployment would under-count `connected` (and break broadcast fan-out). If a
multi-worker setup is ever introduced, the registry must move to shared storage
first.

## Heartbeat & message rate

- Ping/pong using `get_ws_heartbeat_interval` (`config.py::get_ws_heartbeat_interval`) and
  `get_ws_heartbeat_timeout` (`config.py::get_ws_heartbeat_timeout`).
- Per-connection message budget via a `TokenBucket` from
  `create_ws_message_bucket` (`rate_limiter.py::create_ws_message_bucket`, `TokenBucket` `rate_limiter.py::TokenBucket`).

## Event handlers (`api/events_handlers/`)

`event_handlers.py` is a thin re-export shim (`api/events_handlers/event_handlers.py`); real logic is
in `events_handlers/handlers.py`:

| Event (`EventName`) | Handler | Location |
| ------------------- | ------- | -------- |
| `added` | `handle_added_event` | `api/events_handlers/handlers.py::handle_added_event` |
| `modified` | `handle_modified_event` | `api/events_handlers/handlers.py::handle_modified_event` |
| `deleted` | `handle_deleted_event` | `api/events_handlers/handlers.py::handle_deleted_event` |
| `renamed` | `handle_renamed_event` | `api/events_handlers/handlers.py::handle_renamed_event` |
| `symlink` | `handle_symlink_event` | `api/events_handlers/handlers.py::handle_symlink_event` |

`EventName` comes from `hsync_common/models/event.py::EventName` (imported in `api/events.py`). Each handler
mutates the bucket on disk, keeps the hash cache / tombstones consistent, then the
event is rebroadcast. `renamed` additionally carries the causal version entries
across the rename (`versions.py::move_version` / `versions.py::move_version_prefix`), drops any stale
deletion markers at the destination (`tombstones.py::clear_deletion` / `tombstones.py::clear_deletion_tree`)
and treats this bookkeeping as best-effort so it never aborts the broadcast;
`deleted` records the causal tombstone through the shared
`tombstones.py::record_path_deletion`.

## Path safety (`api/events_handlers/validation.py`)

- `is_path_within_bucket` (`api/events_handlers/validation.py::is_path_within_bucket`) — containment guard for every
  write/delete target.
- Symlink targets are checked with `is_safe_symlink_target` (SSOT in
  `hsync_common.path_validation`) — blocks symlink escapes outside the bucket.
- Wire-path gate in `events.py` (`api/events.py::_event_paths_safe`): every `src`/`dst`
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

`handle_deleted_event` records a tombstone (`tombstones.py::record_deletion`) so the deletion
propagates and survives peer reconciliation; uploads of the same path later clear
it (`clear_deletion`, `tombstones.py::clear_deletion`). For a recursive directory `deleted`,
the descendant walk + hashing (`_record_tombstones_for_tree`,
`api/events_handlers/handlers.py::_record_tombstones_for_tree`) and the `rmtree`
run **synchronously on the loop on purpose**: offloading them would let an
upload publish a new descendant mid-walk that the `rmtree` then removes without
a tombstone (a resurrection regression). Full loop-freedom here needs a bucket/
tree mutation lock shared with upload — a documented follow-up.

> Human-readable WS sequence diagram (WebSocket Event Flow): `../docs/internal/architecture.md`.
> The machine-readable WS contract is the generated schema in `hsync-common`
> (`protocol/ws/ws-schema.json`).
