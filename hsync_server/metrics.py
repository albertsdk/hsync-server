# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2025 Alberto Basso

"""
Prometheus metrics for delta transfer operations.

This module defines metrics for monitoring delta transfer performance,
success/failure rates, and resource usage. Metrics are exposed via the
/metrics endpoint (when prometheus_client is available).
"""

from fastapi import Response
from hsync_common.protocol.rest import REST_METRICS_PATH
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from hsync_server.routers import auth_router

# =============================================================================
# Delta Transfer Metrics
# =============================================================================

# Counter for delta transfer operations (success/failure)
hsync_delta_transfer_total = Counter(
    "hsync_delta_transfer_total",
    "Total number of delta transfer operations",
    ["direction", "status"],
)

# Counter for bytes saved by delta transfer (vs full transfer)
hsync_delta_bytes_saved = Counter(
    "hsync_delta_bytes_saved",
    "Total bytes saved by delta transfer (vs full file transfer)",
    ["direction"],
)

# Histogram for delta transfer duration (by phase)
hsync_delta_transfer_duration_seconds = Histogram(
    "hsync_delta_transfer_duration_seconds",
    "Duration of delta transfer operations in seconds",
    ["direction", "phase"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# Histogram for block change ratio (how much of the file changed)
hsync_delta_block_change_ratio = Histogram(
    "hsync_delta_block_change_ratio",
    "Ratio of changed blocks in delta transfer (0.0 = no change, 1.0 = all changed)",
    ["direction"],
    buckets=(0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# Gauge for active delta tokens (currently valid download tokens)
hsync_delta_active_tokens = Gauge(
    "hsync_delta_active_tokens",
    "Number of currently active delta download tokens",
)

# Gauge for delta temp file storage usage (bytes)
hsync_delta_temp_storage_bytes = Gauge(
    "hsync_delta_temp_storage_bytes",
    "Total bytes used by delta transfer temporary files",
)

# =============================================================================
# Helper Functions
# =============================================================================


def record_delta_transfer(direction: str, status: str) -> None:
    """Record a delta transfer operation.

    Args:
        direction: "upload" or "download"
        status: "success", "failure", "fallback", or "rejected"
    """
    hsync_delta_transfer_total.labels(direction=direction, status=status).inc()


def record_bytes_saved(direction: str, bytes_saved: int) -> None:
    """Record bytes saved by delta transfer.

    Args:
        direction: "upload" or "download"
        bytes_saved: Number of bytes saved (original_size - delta_size)
    """
    if bytes_saved > 0:
        hsync_delta_bytes_saved.labels(direction=direction).inc(bytes_saved)


def record_transfer_duration(direction: str, phase: str, duration_seconds: float) -> None:
    """Record the duration of a delta transfer phase.

    Args:
        direction: "upload" or "download"
        phase: "signatures", "match", "apply", "data", or "total"
        duration_seconds: Duration in seconds
    """
    hsync_delta_transfer_duration_seconds.labels(direction=direction, phase=phase).observe(
        duration_seconds
    )


def record_change_ratio(direction: str, ratio: float) -> None:
    """Record the block change ratio for a delta transfer.

    Args:
        direction: "upload" or "download"
        ratio: Ratio of changed blocks (0.0 to 1.0)
    """
    hsync_delta_block_change_ratio.labels(direction=direction).observe(ratio)


def update_active_tokens(count: int) -> None:
    """Update the gauge for active delta tokens.

    Args:
        count: Current number of active tokens
    """
    hsync_delta_active_tokens.set(count)


def update_temp_storage(bytes_used: int) -> None:
    """Update the gauge for delta temp storage usage.

    Args:
        bytes_used: Total bytes used by temp files
    """
    hsync_delta_temp_storage_bytes.set(bytes_used)


# =============================================================================
# /metrics endpoint (authenticated)
# =============================================================================


@auth_router.get(REST_METRICS_PATH, name="Monitoring - Prometheus metrics")
async def metrics_endpoint() -> Response:
    """Prometheus metrics endpoint.

    Authenticated: the endpoint was previously exposed publicly
    by the instrumentator; it now requires a valid account token.
    """
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
