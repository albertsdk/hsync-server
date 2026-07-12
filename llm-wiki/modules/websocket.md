# HSYNC Server — WebSocket (`api/events.py` + `api/events_handlers/`)

Real-time event fan-out. One WS endpoint; events received from one client are
applied to disk and broadcast to the other subscribers of the same bucket.

## Endpoint

`WS /api/v1/buckets/{bucket}/events/{cid}` → `ws_events` (`events.py:62`), declared
on `public_router` (WS can't use header auth; auth is the first frame).

## Handshake (shared SSOT)

Uses `hsync_common.protocol.handshake` (imported `events.py:28`):
- Client sends an auth frame; server verifies the account token via
  `verify_token` (`auth.py`, scrypt + KDF cache) — run on an asyncio worker
  thread (`asyncio.to_thread` in `_dispatching_authenticate`, `events.py`)
  so the CPU-heavy scrypt never blocks the loop during a handshake — and
  **hard-fails on `protocol_version` mismatch** against `PROTOCOL_VERSION_WS`.
- After auth, the server checks bucket existence + membership; failure closes
  with `4009` (indistinguishable from "bucket not found").
- Replies with `build_ws_auth_ok` / `build_ws_auth_error`; client capabilities
  via `decode_capabilities`; the reply advertises the server's **honest**
  capability set (ADR-0024): `tombstones` + `vector_clocks` unconditionally,
  `compression` when `get_compression_enabled()`, `delta` when
  `get_delta_enabled()`.
- Auth must arrive within `get_ws_auth_timeout()` (`config.py:452`); otherwise the
  connection is dropped.

## Connection registry (`api/connection_manager.py`)

| Object | Location | Role |
| ------ | -------- | ---- |
| `ConnectionManager` | `connection_manager.py:45` | per-bucket `{cid: WebSocket}`; `disconnect`/`send_message`/`send_presence` |
| `BucketConnectionManager` | `connection_manager.py:112` | per-bucket pools + global slot accounting for DoS protection + `presence_counts` |
| `bucket_manager` (global) | `connection_manager.py:187` | the singleton used by REST and WS to broadcast |

`_send_to_all` (`connection_manager.py:57`) is the shared broadcast path: it
re-checks each recipient's authz (account active + bucket access) and evicts
dead sockets per-connection. `send_message` (`:118`) skips the originating
`cid` so senders don't echo; `send_presence` (`:126`) has no sender filter.
`presence_counts` (`:143`) computes the display-ready `(connected, total)` pair:
`total` = non-admin bucket members (`AuthStore.get_share_member_ids`), `connected`
= those members with a live socket on this worker. `ws_events` broadcasts a
`presence` frame on every join/leave (`events.py:_broadcast_presence`), and the
REST endpoint `GET /api/v1/buckets/{bucket}/presence` exposes the same pair
(`api/presence.py`). Slot caps: `get_ws_max_connections` (`config.py:457`) and
`get_ws_max_connections_per_ip` (`config.py:462`).

**Deployment contract**: the server supports a **single worker only**. The
connection registry and presence counters are in-process, so a multi-worker
deployment would under-count `connected` (and break broadcast fan-out). If a
multi-worker setup is ever introduced, the registry must move to shared storage
first.

## Heartbeat & message rate

- Ping/pong using `get_ws_heartbeat_interval` (`config.py:442`) and
  `get_ws_heartbeat_timeout` (`config.py:447`).
- Per-connection message budget via a `TokenBucket` from
  `create_ws_message_bucket` (`rate_limiter.py:230`, `TokenBucket` `:186`).

## Event handlers (`api/events_handlers/`)

`event_handlers.py` is a thin re-export shim (`event_handlers.py:23`); real logic is
in `events_handlers/handlers.py`:

| Event (`EventName`) | Handler | Line |
| ------------------- | ------- | ---- |
| `added` | `handle_added_event` | `handlers.py:44` |
| `modified` | `handle_modified_event` | `handlers.py:95` |
| `deleted` | `handle_deleted_event` | `handlers.py:143` |
| `renamed` | `handle_renamed_event` | `handlers.py:220` |
| `symlink` | `handle_symlink_event` | `handlers.py:278` |

`EventName` comes from `hsync_common.models.event` (`events.py:27`). Each handler
mutates the bucket on disk, keeps the hash cache / tombstones consistent, then the
event is rebroadcast.

## Path safety (`api/events_handlers/validation.py`)

- `is_path_within_bucket` (`validation.py:17`) — containment guard for every
  write/delete target.
- Symlink targets are checked with `is_safe_symlink_target` (SSOT in
  `hsync_common.path_validation`) — blocks symlink escapes outside the bucket.
- Wire-path gate in `events.py` (`_event_paths_safe`): every `src`/`dst`
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

`handle_deleted_event` records a tombstone (`tombstones.py:49`) so the deletion
propagates and survives peer reconciliation; uploads of the same path later clear
it (`clear_deletion`, `tombstones.py:66`). For a recursive directory `deleted`,
the descendant walk + hashing (`_record_tombstones_for_tree`) and the `rmtree`
run **synchronously on the loop on purpose**: offloading them would let an
upload publish a new descendant mid-walk that the `rmtree` then removes without
a tombstone (a resurrection regression). Full loop-freedom here needs a bucket/
tree mutation lock shared with upload — a documented follow-up.

> Human-readable WS sequence diagram (WebSocket Event Flow): `../docs/architecture.md`.
> The machine-readable WS contract is the generated schema in `hsync-common`
> (`protocol/ws/ws-schema.json`).
