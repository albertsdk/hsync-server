# hsync-server — WebSocket (`api/events.py` + `api/events_handlers/`)

Real-time event fan-out. One WS endpoint; events received from one client are
applied to disk and broadcast to the other subscribers of the same bucket.

## Endpoint

`WS /api/v1/buckets/{bucket}/events/{cid}` → `ws_events` (`events.py:62`), declared
on `public_router` (WS can't use header auth; auth is the first frame).

## Handshake (shared SSOT)

Uses `hsync_common.protocol.handshake` (imported `events.py:28`):
- Client sends an auth frame; server validates token (`hmac.compare_digest` vs
  `get_api_key()`) and **hard-fails on `protocol_version` mismatch** against
  `PROTOCOL_VERSION_WS`.
- Replies with `build_ws_auth_ok` / `build_ws_auth_error`; capabilities via
  `decode_capabilities` / `Capability` enum.
- Auth must arrive within `get_ws_auth_timeout()` (`config.py:452`); otherwise the
  connection is dropped.

## Connection registry (`api/connection_manager.py`)

| Object | Location | Role |
| ------ | -------- | ---- |
| `ConnectionManager` | `connection_manager.py:22` | per-bucket `{cid: WebSocket}`; `connect`/`disconnect`/`send_message` |
| `BucketConnectionManager` | `connection_manager.py:71` | per-bucket pools + global slot accounting for DoS protection |
| `bucket_manager` (global) | `connection_manager.py:166` | the singleton used by REST and WS to broadcast |

`send_message` (`connection_manager.py:58`) skips the originating `cid`
(`event.cid`) so senders don't echo. Slot caps: `get_ws_max_connections`
(`config.py:457`) and `get_ws_max_connections_per_ip` (`config.py:462`).

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
- `is_safe_symlink_target` (`validation.py:48`) — blocks symlink escapes outside
  the bucket.

## Deleted-event semantics

`handle_deleted_event` records a tombstone (`tombstones.py:49`) so the deletion
propagates and survives peer reconciliation; uploads of the same path later clear
it (`clear_deletion`, `tombstones.py:66`).

> Human-readable WS message shapes + sequence diagrams: `../docs/api.md`,
> `../docs/sync-flow.md`.
