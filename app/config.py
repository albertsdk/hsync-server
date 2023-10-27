import os

from logging import getLogger
from pathlib import Path
from uuid import uuid4

LOG = getLogger("main")

# Server port
HSYNC_PORT = int(os.environ.get("HSYNC_PORT", 8765))

# Application path
HSYNC_PATH = str(Path(os.path.dirname(__file__) + "/..").resolve())
  
# Server API Key
# https://it-tools.tech/token-generator?symbols=true
HSYNC_API_KEY = os.environ.get("HSYNC_API_KEY", str(uuid4()))

# Local buckets path
HSYNC_BUCKETS = f"{HSYNC_PATH}/buckets"
