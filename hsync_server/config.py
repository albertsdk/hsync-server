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

from hsync_common.env import load_env_file as _load_env_file
from hsync_common.models.tombstone import TOMBSTONE_TTL_DAYS_DEFAULT
from pydantic import Field, SecretStr, field_validator, model_validator
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
    )

    # Server Configuration
    port: int = Field(default=8765, ge=1, le=65535, description="Server port")
    workers: int = Field(default=1, ge=1, description="Number of uvicorn workers")
    tz: str = Field(default="UTC", description="Timezone in IANA format")
    log_level: Literal["debug", "info", "warning", "error", "critical"] = Field(
        default="info", description="Log level"
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

    # CORS Configuration
    cors_enabled: bool = Field(default=False, description="Enable CORS middleware")
    cors_origins: str = Field(default="", description="Comma-separated allowed origins")

    # Compression Configuration
    compression_enabled: bool = Field(
        default=True, description="Enable zstd compression for file transfers"
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
    def buckets_path(self) -> str:
        """Path to buckets storage directory."""
        return f"{_HSYNC_PATH}/buckets"


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
# Module-level constants
# =============================================================================

# Application root path - resolved at import time because it is derived from
# the module location, not from environment variables.
HSYNC_PATH: str = _HSYNC_PATH


# =============================================================================
# Getter Functions (for late binding after env file loading)
# =============================================================================


def get_path() -> str:
    """Get application root path (late binding)."""
    return get_settings().path


def get_port() -> int:
    """Get server port from environment (late binding)."""
    return get_settings().port


def get_workers() -> int:
    """Get number of uvicorn workers from environment (late binding)."""
    return get_settings().workers


def get_tz() -> str:
    """Get timezone from environment (late binding)."""
    return get_settings().tz


def get_log_level() -> str:
    """Get log level from environment (late binding)."""
    return get_settings().log_level


def get_api_key() -> str:
    """Get API key from environment (late binding)."""
    return get_settings().api_key.get_secret_value()


def get_buckets_path() -> str:
    """Get buckets storage path from environment (late binding)."""
    return get_settings().buckets_path


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


def get_ws_heartbeat_interval() -> int:
    """Get WebSocket heartbeat interval in seconds (late binding)."""
    return get_settings().ws_heartbeat_interval


def get_ws_heartbeat_timeout() -> int:
    """Get WebSocket heartbeat timeout in seconds (late binding)."""
    return get_settings().ws_heartbeat_timeout


def get_reload() -> bool:
    """Get uvicorn auto-reload flag (late binding)."""
    return get_settings().reload


def get_tombstone_ttl_days() -> int:
    """Get tombstone retention window in days from environment (late binding)."""
    return get_settings().tombstone_ttl_days
