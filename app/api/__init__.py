import aiofiles
import stat

from app.config import HSYNC_BUCKETS
from app.model.dir import Dir
from asyncio import to_thread
from hashlib import md5
from logging import getLogger
from os import chmod, stat as os_stat
from pathlib import Path


LOG = getLogger("main")


def is_valid_bucket(value) -> bool:
    return value.isalnum()


def is_valid_dir(target_path: str):
    reference = Path(HSYNC_BUCKETS).resolve()
    target = Path(f"{HSYNC_BUCKETS}/{target_path}").resolve()
    try:
        target.relative_to(reference)
        return True
    except ValueError:
        return False


async def compute_md5(filepath, chunk_size=8192):
    hash_md5 = md5()
    async with aiofiles.open(filepath, "rb") as f:
        while chunk := await f.read(chunk_size):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


async def get_permissions(obj_path) -> str:
    obj_stat = await to_thread(os_stat, obj_path)
    permissions = obj_stat.st_mode
    return stat.filemode(permissions)


async def set_permissions(file_path, permissions: str):
    LOG.debug(f'Set permissions: "{file_path}" ({permissions})') 
    mode = 0
    permission_types = {"r": 4, "w": 2, "x": 1}
    for i, section in enumerate(
        [permissions[1:4], permissions[4:7], permissions[7:10]]
    ):
        for char in section:
            if char in permission_types:
                mode |= permission_types[char] << (6 - (i * 3))
    await to_thread(chmod, file_path, mode)


async def get_dir_info(path):
    permissions = await get_permissions(path)
    return Dir(
        path,
        permissions,
    )
