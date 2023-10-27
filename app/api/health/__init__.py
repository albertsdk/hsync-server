from app.routers import public_router
from fastapi.responses import PlainTextResponse
from logging import getLogger

LOG = getLogger("main")


@public_router.get(
    "/health",
    name="Monitoring - Health check endpoint",
    response_class=PlainTextResponse,
)
async def root() -> str:
    return ""


LOG.info(f"Module {__name__} loaded")
