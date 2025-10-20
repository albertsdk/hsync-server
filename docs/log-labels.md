# Log Labels

`hsync-server` emits bracketed action labels at the start of log lines
(e.g. `[UPLOAD]`, `[WS]`) to tag REST and WebSocket operations. This page lists
every label that can appear in the server logs.

> The complete, cross-project catalog (descriptions, color categories, and which
> project emits each label) lives in
> [`hsync-common/docs/log-labels.md`](https://github.com/albertobasso/hsync-common/blob/main/docs/log-labels.md).
> This page is the server-specific view: only the labels this server emits.

Labels are colored by **semantic category** (transfer=cyan, upload/create=green,
modify/rename=blue, delete=red, security=red, infrastructure=magenta). Color is
applied when stdout is a TTY and `--no-color`/`NO_COLOR` is not set.

## REST file operations

Emitted on the success path of the file endpoints.

| Label | Category | Meaning |
|-------|----------|---------|
| `[UPLOAD]` | upload | A file was uploaded (`POST /api/v1/buckets/{bucket}/files/upload`) |
| `[DOWNLOAD]` | transfer | A file was downloaded (`GET .../files/download`), with size and optional `zstd` |
| `[DELETE]` | delete | A file (and its backup, if any) was deleted (`DELETE .../files/delete`) |

## WebSocket event relay

Emitted by the WS event handlers when relaying a broadcast event received from
a client to the other connected clients in the bucket.

| Label | Category | Meaning |
|-------|----------|---------|
| `[CREATE]` | create | Relayed a directory/file creation event |
| `[MODIFY]` | modify | Relayed a file modification event. The trigger `(closed)`/`(modified)` shows `IN_CLOSE_WRITE` (reliable) vs `on_modified` debounce fallback |
| `[RENAME]` | rename | Relayed a rename/move event |
| `[DELETE]` | delete | Relayed a deletion event from a peer |
| `[SYMLINK]` | create | Relayed a symbolic-link creation event |

## Security

| Label | Category | Meaning |
|-------|----------|---------|
| `[SECURITY]` | security | Rejected an unsafe inbound path (traversal, absolute, internal name) on a relayed event |

## Infrastructure

| Label | Category | Meaning |
|-------|----------|---------|
| `[WS]` | infrastructure | WebSocket transport message (e.g. dropping a dead connection) |

## Related

- [REST API](api.md)
- [Operations](operations.md)
- [Canonical label catalog](https://github.com/albertobasso/hsync-common/blob/main/docs/log-labels.md)
