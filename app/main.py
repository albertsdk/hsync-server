import time

from .api import events, files, health
from .config import HSYNC_BUCKETS
from .file_hashes import set_md5_hashes
from .routers import auth_router, public_router
from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from logging import getLogger
from prometheus_fastapi_instrumentator import Instrumentator


LOG = getLogger("main")
start = time.time()

app = FastAPI(
    title="HSYNC - HTTP Sync file server",
    version="0.1.0",
)
app.include_router(public_router)
app.include_router(auth_router)
app.include_router(events.public_router)
app.include_router(files.auth_router)
app.include_router(health.public_router)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Store MD5 files checksum
LOG.info(f"Loading MD5 file hashes from: {HSYNC_BUCKETS}")
md5_files = set_md5_hashes(HSYNC_BUCKETS)
LOG.info(f"{md5_files} files stored.")

#
# Prometheus Metrics
#
Instrumentator(
    excluded_handlers=["/docs", "/metrics", "/health"],
).instrument(
    app
).expose(app, tags=["Public Router"], endpoint="/metrics")

#
# Startup info
#
# Logging level
LOG.debug("Available logging: DEBUG")
LOG.info("Available logging: INFO")
LOG.warning("Available logging: WARNING")

tot_time = round(time.time() - start, 3)
LOG.info(f"HSYNC Server started in: {tot_time} sec.")
