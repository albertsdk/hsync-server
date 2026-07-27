# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Copyright (C) 2025 Alberto Basso

"""
Configuration module for HSYNC Server.

This module uses pydantic-settings for type-safe configuration management.
All configuration values are loaded from environment variables with sensible
defaults. Environment variables can be set via dev.env or prod.env files.
"""

from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from hsync_common.constants import DELTA_TOKEN_TTL
from hsync_common.env import load_env_file as _load_env_file
from hsync_common.logger import (
    DEFAULT_LOG_COMPRESSION,
    DEFAULT_LOG_RETENTION,
    DEFAULT_LOG_ROTATION,
)
from hsync_common.models.tombstone import TOMBSTONE_TTL_DAYS_DEFAULT
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# =============================================================================
# Constants
# =============================================================================

# Valid log levels (Python standard logging levels)
VALID_LOG_LEVELS: Final[tuple[str, ...]] = (
    "debug",
    "info",
    "warning",
    "error",
    "critical",
)

# Valid log output formats
VALID_LOG_FORMATS: Final[tuple[str, ...]] = ("text", "json")

# Application root path - resolved absolute path to the project directory
_HSYNC_PATH: str = str(Path(__file__).parent.parent.resolve())


# =============================================================================
# Environment File Loading
# =============================================================================


def load_env_file(file_path: str) -> None:
    """Load env vars from a file and clear the server settings cache.

    Thin wrapper around :func:`hsync_common.env.load_env_file` that binds
    the local cache invalidation callback.
    """
    _load_env_file(file_path, on_loaded=get_settings.cache_clear)


# =============================================================================
# Pydantic Settings
# =============================================================================


class Settings(BaseSettings):
    """
    Server configuration using pydantic-settings.

    All settings are loaded from environment variables with HSYNC_ prefix.
    """

    model_config = SettingsConfigDict(
        env_prefix="HSYNC_",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Server Configuration
    port: int = Field(default=8765, ge=1, le=65535, description="Server port")
    workers: int = Field(default=1, ge=1, description="Number of uvicorn workers")
    tz: str = Field(default="UTC", description="Timezone in IANA format")
    log_level: Literal["debug", "info", "warning", "error", "critical"] = Field(
        default="info", description="Log level"
    )
    log_format: Literal["text", "json"] = Field(default="text", description="Log output format")
    log_file: str = Field(default="", description="Optional log file path")
    log_rotation: str = Field(default=DEFAULT_LOG_ROTATION, description="Log file rotation policy")
    log_retention: str = Field(
        default=DEFAULT_LOG_RETENTION, description="Log file retention policy"
    )
    log_compression: str = Field(
        default=DEFAULT_LOG_COMPRESSION,
        description="Compression format for rotated log files",
    )

    # Authentication
    api_key: SecretStr = Field(
        ...,
        description="API key for authentication (required, min 16 chars)",
    )

    # Upload Limits
    max_upload_size: int = Field(
        default=10 * 1024 * 1024 * 1024,  # 10GB
        ge=0,
        description="Maximum file upload size in bytes",
    )

    # Storage Configuration
    buckets_path: str = Field(
        default=f"{_HSYNC_PATH}/buckets",
        validation_alias=AliasChoices("HSYNC_BUCKETS", "HSYNC_BUCKETS_PATH"),
        description="Path to buckets storage directory",
    )

    # CORS Configuration
    cors_enabled: bool = Field(default=False, description="Enable CORS middleware")
    cors_origins: str = Field(default="", description="Comma-separated allowed origins")

    # Compression Configuration
    compression_enabled: bool = Field(
        default=True, description="Enable zstd compression for file transfers"
    )

    # Delta Transfer Configuration
    delta_enabled: bool = Field(
        default=True, description="Enable rsync-style delta transfer endpoints"
    )
    delta_token_ttl: int = Field(
        default=DELTA_TOKEN_TTL,
        ge=30,
        description="Seconds a literal-payload download token stays valid",
    )
    rate_limit_delta: str = Field(
        default="100/minute",
        description="Rate limit for delta transfer endpoints",
    )

    # SSL/TLS Configuration
    ssl_enabled: bool = Field(default=False, description="Enable HTTPS/WSS")
    ssl_certfile: str = Field(default="", description="Path to SSL certificate file")
    ssl_keyfile: str = Field(default="", description="Path to SSL private key file")
    ssl_keyfile_password: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Password used to encrypt/decrypt the SSL private key on disk. "
            "Required when ssl_enabled=True: keys are never stored in clear text."
        ),
    )
    ssl_cert_dir: str = Field(
        default=f"{_HSYNC_PATH}/certs",
        description="Directory for auto-generated certificates",
    )

    # Disk Space Configuration
    min_free_disk_space: int = Field(
        default=500 * 1024 * 1024,  # 500 MB
        ge=0,
        description="Minimum free disk space in bytes before accepting uploads (0 = disabled)",
    )

    # Rate Limiting Configuration
    rate_limit_default: str = Field(
        default="500/minute",
        description="Default API rate limit for general endpoints",
    )
    rate_limit_upload: str = Field(
        default="200/minute",
        description="Rate limit for upload endpoints",
    )
    rate_limit_download: str = Field(
        default="1000/minute",
        description="Rate limit for download endpoints",
    )
    rate_limit_websocket: str = Field(
        default="50/minute",
        description="Rate limit for WebSocket connections",
    )
    rate_limit_ws_messages: str = Field(
        default="10/second",
        description="Rate limit for WebSocket event messages per connection",
    )
    trusted_proxies: str = Field(
        default="",
        description="Comma-separated trusted reverse-proxy IPs for X-Forwarded-For",
    )

    # WebSocket Configuration
    ws_heartbeat_interval: int = Field(
        default=30,
        ge=5,
        description="WebSocket heartbeat interval in seconds",
    )
    ws_heartbeat_timeout: int = Field(
        default=10,
        ge=3,
        description="WebSocket heartbeat timeout in seconds",
    )
    ws_auth_timeout: int = Field(
        default=10,
        ge=1,
        description="Seconds to wait for the WebSocket auth message before closing",
    )
    ws_max_connections: int = Field(
        default=1000,
        ge=1,
        description="Maximum number of concurrent WebSocket connections (all buckets)",
    )
    ws_max_connections_per_ip: int = Field(
        default=10,
        ge=1,
        description="Maximum number of concurrent WebSocket connections per client IP",
    )

    # Tombstone Configuration
    tombstone_ttl_days: int = Field(
        default=TOMBSTONE_TTL_DAYS_DEFAULT,
        ge=0,
        description="Days to retain deletion markers before garbage collection (0 = forever)",
    )

    # API Documentation
    enable_docs: bool = Field(
        default=False,
        description="Expose /docs (Swagger UI) and /redoc endpoints",
    )

    # Development
    reload: bool = Field(
        default=False,
        description="Enable uvicorn auto-reload (development only)",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Normalize log level to lowercase."""
        if isinstance(v, str):
            return v.lower()
        return v

    @field_validator("log_format", mode="before")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        """Normalize log format, falling back to text for unknown values."""
        if isinstance(v, str):
            lowered = v.lower()
            return lowered if lowered in VALID_LOG_FORMATS else "text"
        return v

    @field_validator("api_key", mode="after")
    @classmethod
    def validate_api_key(cls, v: SecretStr) -> SecretStr:
        """Reject empty or too-short API keys at config load time."""
        raw = v.get_secret_value()
        if not raw:
            raise ValueError(
                "HSYNC_API_KEY must be set to a secure random string. "
                "Generate one at: https://it-tools.tech/token-generator"
            )
        if len(raw) < 16:
            raise ValueError(
                f"HSYNC_API_KEY must be at least 16 characters (got {len(raw)}). "
                "Use 32+ characters for better security."
            )
        return v

    @model_validator(mode="after")
    def validate_ssl_password_when_enabled(self) -> "Settings":
        """When SSL is enabled, the keyfile password must be configured."""
        if self.ssl_enabled and not self.ssl_keyfile_password.get_secret_value():
            raise ValueError(
                "HSYNC_SSL_KEYFILE_PASSWORD is required when HSYNC_SSL_ENABLED=True. "
                "SSL private keys must never be stored in clear text."
            )
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def path(self) -> str:
        """Application root path."""
        return _HSYNC_PATH

    @property
    def trusted_proxies_set(self) -> frozenset[str]:
        """Parse comma-separated trusted reverse-proxy IPs into a set."""
        return frozenset(ip.strip() for ip in self.trusted_proxies.split(",") if ip.strip())


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses LRU cache for performance. Call load_env_file() to reload
    settings after loading a new environment file.
    """
    # api_key (and other required fields) are sourced from environment
    # variables by pydantic-settings; the static checker cannot see this,
    # hence the explicit ignore for the "missing argument" false positive.
    return Settings()  # type: ignore[call-arg]


# =============================================================================
# Getter Functions (for late binding after env file loading)
# =============================================================================


def get_port() -> int:
    """Get server port from environment (late binding)."""
    return get_settings().port


def get_workers() -> int:
    """Get number of uvicorn workers from environment (late binding)."""
    return get_settings().workers


def get_log_level() -> str:
    """Get log level from environment (late binding)."""
    return get_settings().log_level


def get_log_format() -> str:
    """Get log output format from environment (late binding)."""
    return get_settings().log_format


def get_log_file() -> str:
    """Get optional log file path from environment (late binding)."""
    return get_settings().log_file


def get_api_key() -> str:
    """Get API key from environment (late binding)."""
    return get_settings().api_key.get_secret_value()


def get_buckets_path() -> str:
    """Get buckets storage path from environment (late binding)."""
    return get_settings().buckets_path


def get_rate_limit_default() -> str:
    """Get default API rate limit from environment (late binding)."""
    return get_settings().rate_limit_default


def get_rate_limit_upload() -> str:
    """Get upload endpoint rate limit from environment (late binding)."""
    return get_settings().rate_limit_upload


def get_rate_limit_download() -> str:
    """Get download endpoint rate limit from environment (late binding)."""
    return get_settings().rate_limit_download


def get_rate_limit_websocket() -> str:
    """Get WebSocket connection rate limit from environment (late binding)."""
    return get_settings().rate_limit_websocket


def get_rate_limit_ws_messages() -> str:
    """Get WebSocket message rate limit from environment (late binding)."""
    return get_settings().rate_limit_ws_messages


def get_trusted_proxies() -> frozenset[str]:
    """Get trusted reverse-proxy IPs from environment (late binding)."""
    return get_settings().trusted_proxies_set


def get_max_upload_size() -> int:
    """Get maximum upload size in bytes from environment (late binding)."""
    return get_settings().max_upload_size


def get_cors_enabled() -> bool:
    """Get CORS enabled flag from environment (late binding)."""
    return get_settings().cors_enabled


def get_cors_origins() -> list[str]:
    """Get parsed CORS origins list from environment (late binding)."""
    return get_settings().cors_origins_list


def get_ssl_enabled() -> bool:
    """Get SSL enabled flag from environment (late binding)."""
    return get_settings().ssl_enabled


def get_ssl_certfile() -> str:
    """Get SSL certificate file path from environment (late binding)."""
    return get_settings().ssl_certfile


def get_ssl_keyfile() -> str:
    """Get SSL key file path from environment (late binding)."""
    return get_settings().ssl_keyfile


def get_ssl_keyfile_password() -> str:
    """Get SSL key file password from environment (late binding)."""
    return get_settings().ssl_keyfile_password.get_secret_value()


def get_ssl_cert_dir() -> str:
    """Get SSL certificate directory from environment (late binding)."""
    return get_settings().ssl_cert_dir


def get_compression_enabled() -> bool:
    """Get compression enabled flag from environment (late binding)."""
    return get_settings().compression_enabled


def get_delta_enabled() -> bool:
    """Get delta transfer enabled flag from environment (late binding)."""
    return get_settings().delta_enabled


def get_delta_token_ttl() -> int:
    """Get literal-payload token TTL in seconds from environment (late binding)."""
    return get_settings().delta_token_ttl


def get_rate_limit_delta() -> str:
    """Get delta endpoint rate limit from environment (late binding)."""
    return get_settings().rate_limit_delta


def get_ws_heartbeat_interval() -> int:
    """Get WebSocket heartbeat interval in seconds (late binding)."""
    return get_settings().ws_heartbeat_interval


def get_ws_heartbeat_timeout() -> int:
    """Get WebSocket heartbeat timeout in seconds (late binding)."""
    return get_settings().ws_heartbeat_timeout


def get_ws_auth_timeout() -> int:
    """Get WebSocket authentication timeout in seconds (late binding)."""
    return get_settings().ws_auth_timeout


def get_ws_max_connections() -> int:
    """Get the global WebSocket connection cap (late binding)."""
    return get_settings().ws_max_connections


def get_ws_max_connections_per_ip() -> int:
    """Get the per-IP WebSocket connection cap (late binding)."""
    return get_settings().ws_max_connections_per_ip


def get_reload() -> bool:
    """Get uvicorn auto-reload flag (late binding)."""
    return get_settings().reload


def get_tombstone_ttl_days() -> int:
    """Get tombstone retention window in days from environment (late binding)."""
    return get_settings().tombstone_ttl_days
