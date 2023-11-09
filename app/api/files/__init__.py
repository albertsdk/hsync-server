from app.api import compute_md5, is_valid_bucket, is_valid_dir, set_permissions
from app.api.events import manager
from app.config import HSYNC_BUCKETS
from app.file_hashes import get_md5_hash, set_md5_hash
from app.model.file import File as LocalFile
from app.model.event import Event, EventName
from app.routers import auth_router
from aiofiles.ospath import isdir, isfile
from aiofiles.tempfile import NamedTemporaryFile
from datetime import datetime, timezone
from fastapi import File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from logging import getLogger
from os import makedirs, remove, utime, walk
from os.path import (
    abspath,
    commonpath,
    dirname,
    exists,
    getatime,
    getctime,
    getmtime,
)
from re import match
from shutil import move, rmtree


LOG = getLogger("main")


def is_safe_path(base_path: str, target_path: str) -> bool:
    base_path = abspath(base_path)
    target_path = abspath(target_path)
    return commonpath([base_path]) == commonpath([base_path, target_path])


def is_valid_permission_format(permissions: str) -> bool:
    return bool(match(r"^[\-rwx]{1}[rwx-]{9}$", permissions))


def update_file_timestamp(file_location: str, ctime: str = None, mtime: str = None):
    # Set ctime and mtime if provided
    if ctime or mtime:     
        if ctime.isdigit():
            _ctime = int(
                datetime.fromtimestamp(int(ctime), timezone.utc).timestamp()
            )
        else:
            _ctime = int(datetime.fromisoformat(ctime).timestamp())
        if mtime.isdigit():
            _mtime = int(
                datetime.fromtimestamp(int(mtime), timezone.utc).timestamp()
            )
        else:
            _mtime = int(datetime.fromisoformat(mtime).timestamp())
        LOG.debug(f"Update timestamp: {file_location} ({ctime}, {mtime})")   
        utime(file_location, (_ctime or _mtime, _mtime or _ctime))


@auth_router.get(
    "/bucket/v1/{bucket}",
    name="Get bucket information",
    summary="Get bucket information",
    description="""Get bucket information.""",
)
async def get_bucket(bucket: str):
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")

        bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"

        if not exists(bucket_dir):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
            )

        # Fetch and format access, creation and modification times
        times = {
            "adate": getatime(bucket_dir),
            "cdate": getctime(bucket_dir),
            "mdate": getmtime(bucket_dir),
        }
        formatted_times = {
            key: datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
            for key, value in times.items()
        }

        return JSONResponse(
            status_code=status.HTTP_200_OK, content={"name": bucket, **formatted_times}
        )
    except HTTPException as e:
        LOG.error(f"Failed to get bucket information: {bucket}: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Failed to get bucket information: {bucket}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@auth_router.post("/bucket/v1")
async def create_bucket(bucket: str):
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")

        bucket_directory = f"{HSYNC_BUCKETS}/{bucket}"

        if exists(bucket_directory):
            LOG.warning(f"Bucket already exists: {bucket_directory}")
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "info": "Bucket already exists",
                    "bucket": bucket,
                },
            )
        else:
            makedirs(bucket_directory, exist_ok=True)
            LOG.info(f'Bucket: "{bucket}" created')

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "info": "Bucket created successfully",
                "bucket": bucket,
            },
        )
    except Exception as e:
        LOG.error(f"Bucket creation failed: {bucket}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@auth_router.delete("/bucket/v1/{bucket}")
async def delete_bucket(bucket: str):
    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
    try:
        if not is_valid_bucket(bucket):
            LOG.error(f"Invalid bucket name: {bucket}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bucket name"
            )
        if not exists(bucket_dir):
            LOG.warning(f"Bucket not found: {bucket_dir}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
            )
        rmtree(bucket_dir)
        LOG.info(f'Bucket: "{bucket}" deleted successfully')
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "info": "Bucket deleted successfully",
                "bucket": bucket,
            },
        )
    except HTTPException as e:
        LOG.error(f"Failed to delete bucket: {bucket}: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(
            f"Unexpected error while deleting bucket {bucket}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete bucket",
        )


@auth_router.post("/upload/v1/{bucket}")
async def upload_file(
    cid: str = Form(),
    bucket: str = "",
    file: UploadFile = File(...),
    path: str = Form(""),
    ctime: str = Form(""),
    mtime: str = Form(""),
    md5sum: str = Form(""),
    permissions: str = Form("-rw-rw-r--"),
) -> JSONResponse:
    file_location_tmp = None
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")
        if not is_valid_dir(path):
            raise ValueError("Invalid path")

        # Validate permissions
        if permissions and not is_valid_permission_format(permissions):
            raise ValueError("Invalid permission format")

        # Define file paths
        file_directory = f"{HSYNC_BUCKETS}/{bucket}/{path}"
        file_location = f"{file_directory}/{file.filename}"

        # Per-check MD5 file checksum if already exists
        if md5sum and exists(file_location) and md5sum == get_md5_hash(file_location):
            LOG.warning(f"File already present: {file_location}")
            # Update ctime and mtime if provided
            update_file_timestamp(file_location, ctime, mtime)
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "info": "File already present",
                    "file": file_location,
                },
            )

        # Ensure directory exists
        makedirs(file_directory, exist_ok=True)

        # Write file to a temporary location
        async with NamedTemporaryFile(delete=False) as temp_file:
            file_location_tmp = temp_file.name
            while chunk := await file.read(8192):
                await temp_file.write(chunk)

        update_file_timestamp(file_location_tmp, ctime, mtime)

        # Permissions
        if permissions:
            await set_permissions(file_location_tmp, permissions)

        # Size
        # if size and size != (size_tmp := getsize(file_location_tmp)):
        #    error = f"Partial file content. Expected size: {size}, {size_tmp} given"
        #    LOG.error(error)
        #    raise HTTPException(
        #        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=error
        #    )

        # Verify md5 checksum
        md5sum_tmp = await compute_md5(file_location_tmp)
        if md5sum and md5sum != md5sum_tmp:
            error = f"Invalid checksum: expected {md5sum}, {md5sum_tmp} given"
            LOG.error(error)
            raise ValueError(error)

        # Store MD5 for future requests
        set_md5_hash(file_location, md5sum_tmp)

        # Move the file to the final destination
        move(file_location_tmp, file_location)

        # Send WebSocket message
        src = LocalFile().get(file_location)
        local_bucket = f"{HSYNC_BUCKETS}/{bucket}"
        await manager.send_message(
            Event(cid, local_bucket, bucket, EventName.added, src)
        )

        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "info": "File uploaded successfully",
                "file": f"{bucket}/{path}/{file.filename}",
            },
        )
    except Exception as e:
        LOG.error(f"Upload failed for file {file.filename}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    finally:
        if file_location_tmp and exists(file_location_tmp):
            remove(file_location_tmp)


@auth_router.get("/download/v1/{bucket}")
async def download_file(bucket: str, filename: str) -> FileResponse:
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")
        if not is_valid_dir(filename):
            raise ValueError("Invalid path")

        file = f"{HSYNC_BUCKETS}/{bucket}/{filename}"

        if not await isfile(file):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )
        return FileResponse(file, filename=filename)
    except HTTPException as e:
        LOG.error(f"Failed to download file: {bucket}:{filename} {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Download failed {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@auth_router.get("/files/v1/{bucket}")
async def get_files(
    bucket: str = "", path: str = None, sync_order: str = "mtime"
) -> JSONResponse:
    try:
        if not is_valid_bucket(bucket):
            raise ValueError("Invalid bucket name")
        if path and not is_valid_dir(path):
            raise ValueError("Invalid path")

        valid_orders = ["mtime", "ctime"]
        if sync_order not in valid_orders:
            raise ValueError(
                f"Invalid order '{sync_order}'. Must be one of {valid_orders}."
            )

        dir = f"{HSYNC_BUCKETS}/{bucket}/{path or ''}".rstrip("/")

        if not exists(dir):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Bucket not found: {bucket}",
            )

        file_list = []
        for root, dirs, files in walk(dir):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            for file in files:
                # Skip hidden files
                if file.startswith("."):
                    continue
                file_info = LocalFile().get(file, root).to_dict()
                file_data = (
                    LocalFile()
                    .from_dict(file_info, f"{HSYNC_BUCKETS}/{bucket}")
                    .to_dict()
                )
                file_list.append(file_data)

        # Order
        file_list.sort(key=lambda x: x.get(sync_order))

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=file_list,
        )
    except HTTPException as e:
        LOG.error(f"Files list failed: {e}", exc_info=True)
        raise e
    except Exception as e:
        LOG.error(f"Files list failed: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@auth_router.delete("/file/v1/{bucket}")
async def delete_file(
    cid: str = Form(),
    bucket: str = "",
    filename: str = Form(),
) -> JSONResponse:
    if not is_valid_bucket(bucket):
        LOG.error(f"Invalid bucket name: {bucket}")
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid bucket name"
        )
    bucket_dir = f"{HSYNC_BUCKETS}/{bucket}"
    if not exists(bucket_dir):
        LOG.warning(f"Bucket not found: {bucket_dir}")
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Bucket not found"
        )

    file = f"{bucket_dir}/{filename}"
    if not is_valid_dir(dirname(file)):
        LOG.error(f"Invalid path: {bucket}")
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid path"
        )

    try:
        if not exists(file):
            LOG.warning(f"File not found: {file}")
            return HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )

        # Send WebSocket message
        src = LocalFile().get(file)
        local_bucket = f"{HSYNC_BUCKETS}/{bucket}"
        await manager.send_message(
            Event(cid, local_bucket, bucket, EventName.deleted, src)
        )

        remove(file)
        LOG.info(f'File: "{file}" deleted successfully')

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "info": "File deleted successfully",
                "file": file,
            },
        )
    except Exception as e:
        LOG.error(f"Unexpected error while deleting file {file}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete file",
        )


LOG.info(f"Module {__name__} loaded")
