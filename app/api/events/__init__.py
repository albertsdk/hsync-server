from app.api import set_permissions
from app.config import HSYNC_API_KEY, HSYNC_BUCKETS
from app.file_hashes import del_md5_hash
from app.model.dir import Dir
from app.model.event import Event, EventName
from app.model.file import File
from app.routers import public_router
from fastapi import Query, WebSocket, WebSocketDisconnect
from json import loads, JSONDecodeError
from logging import getLogger
from os import makedirs, rename, remove
from os.path import exists
from shutil import rmtree
from typing import Dict


LOG = getLogger("main")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        """Connect a client and accept the WebSocket connection."""
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        """Disconnect a client and remove their WebSocket connection."""
        self.active_connections.pop(client_id, None)

    async def send_message(self, event: Event):
        """
        Broadcast message to all clients except sender.

        :param event: The message event to send.
        """
        for cid, connection in self.active_connections.items():
            if cid != event.cid:
                await connection.send_json(event.to_dict())


# NOTE: Websocket doesent support Header Authentication 
#       and FastAPI auto-docs
@public_router.websocket("/events/v1/{bucket}/{cid}")
async def ws_events(
    websocket: WebSocket, bucket: str, cid: str, token: str = Query(...)
):
    """Manage remote events.

    Args:
        websocket (WebSocket): _description_
        bucket (str): _description_
        cid (str): Client ID
        token (str, optional): _description_. Defaults to Query(...).

    Raises:
        ValueError: _description_
    """
    try:
        if token != HSYNC_API_KEY:
            message = "Invalid API KEY."
            LOG.warning(message)
            raise ValueError(message)

        await manager.connect(websocket, cid)
        try:
            while True:
                data = await websocket.receive_text()
                LOG.debug(f"[WS] Received message: {data}")
                try:
                    message = loads(data)
                    if message.get("name") not in {
                        EventName.added.value,
                        EventName.modified.value,
                        EventName.renamed.value,
                        EventName.deleted.value,
                    }:
                        LOG.error(f"Invalid event name: {message.get("name")}")
                        continue

                    src = message.get("src", None)
                    dst = message.get("dst", None)
                    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
                    event_name = EventName(message.get("name"))

                    # Added
                    if event_name == EventName.added:
                        # Dir
                        if src and "path" in src:
                            dir = Dir().from_dict(src, bucket_dir)
                            LOG.info(f'[WS] Added dir "{dir.path}"')
                            makedirs(dir.path, exist_ok=True)
                            await set_permissions(dir.path, dir.permissions)
                            event = Event(cid, bucket_dir, bucket, EventName.added, dir)
                        # File
                        elif src and "filepath" in src:
                            file = File().from_dict(src, bucket_dir)
                            LOG.info(f'[WS] Added file "{file.filepath}"')
                            event = Event(
                                cid, bucket_dir, bucket, EventName.added, file
                            )
                    # Modified
                    elif event_name == EventName.modified:
                        # Dir
                        if src and "path" in src:
                            dir = Dir().from_dict(src, bucket_dir)
                            LOG.info(f'[WS] Modified dir "{dir.path}"')
                            await set_permissions(dir.path, dir.permissions)
                            event = Event(
                                cid, bucket_dir, bucket, EventName.modified, dir
                            )
                        # File
                        elif src and "filepath" in src:
                            file = File().from_dict(src, bucket_dir)
                            LOG.info(f'[WS] Modified file "{file.filepath}"')
                            event = Event(
                                cid, bucket_dir, bucket, EventName.modified, file
                            )
                    # Delete
                    elif event_name == EventName.deleted:
                        # Dir
                        if src and "path" in src:
                            dir = Dir().from_dict(src, bucket_dir)
                            if not exists(dir.path):
                                LOG.warning(f'[WS] Dir: "{dir.path}" not found.')
                            else:
                                LOG.info(f'[WS] Deleted dir "{dir.path}"')
                                rmtree(dir.path, ignore_errors=False)
                            event = Event(
                                cid, bucket_dir, bucket, EventName.deleted, dir
                            )
                        # File
                        elif src and "filepath" in src:
                            file = File().from_dict(src, bucket_dir, False)
                            if not exists(file.filepath):
                                LOG.warning(f'[WS] File: "{dir.path}" not found.')
                            else:
                                LOG.info(f'[WS] Deleted file "{file.filepath}"')
                                remove(file.filepath)
                            del_md5_hash(file.filepath)
                            event = Event(
                                cid, bucket_dir, bucket, EventName.deleted, file
                            )
                    # Rename
                    elif event_name == EventName.renamed:
                        # Dir
                        if src and src.get("path", None):
                            src_dir = Dir().from_dict(src, bucket_dir)
                            dst_dir = Dir().from_dict(dst, bucket_dir)
                            LOG.info(
                                f'[WS] Renamed "{src_dir.path}" -> "{dst_dir.path}"'
                            )
                            rename(src_dir.path, dst_dir.path)
                            event = Event(
                                cid,
                                bucket_dir,
                                bucket,
                                EventName.renamed,
                                src_dir,
                                dst_dir,
                            )
                        # File
                        elif src and src.get("filepath", None):
                            src_file = File().from_dict(src, bucket_dir, False)
                            dst_file = File().from_dict(dst, bucket_dir, False)
                            LOG.info(
                                f'[WS] Renamed "{src_file.filepath}" -> "{dst_file.filepath}"'
                            )
                            rename(src_file.filepath, dst_file.filepath)
                            event = Event(
                                cid,
                                bucket_dir,
                                bucket,
                                EventName.renamed,
                                src_file,
                                dst_file,
                            )

                    await manager.send_message(event)
                except JSONDecodeError:
                    LOG.error(f"[WS] Invalid JSON received: {data}")
                except Exception as e:
                    LOG.error(f"[WS] Exception data: {data}")
                    LOG.error(f"[WS] Exception: {e}", exc_info=True)
        except WebSocketDisconnect:
            manager.disconnect(cid)
            LOG.warning(f"[WS] Disconnected (CID: {cid})")
        except Exception as e:
            manager.disconnect(cid)
            LOG.warning(f"[WS] Disconnected (CID: {cid}) ({e})")
            await websocket.close()
    except:
        LOG.warning(f"[WS] Disconnected (CID: {cid})")
        await websocket.close()


manager = ConnectionManager()


LOG.info(f"Module {__name__} loaded")
