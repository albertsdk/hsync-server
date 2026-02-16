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

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
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
    """
    Load environment variables from a file using python-dotenv.

    The file format supports:
    - KEY=value pairs (one per line)
    - Comments starting with #
    - Quoted values (single or double quotes)
    - export KEY=value syntax
    - Multiline values with quotes

    Environment variables already set (e.g., from CLI or shell) take
    precedence and are NOT overwritten by the file.

    Args:
        file_path: Path to the environment file.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Environment file not found: {file_path}")

    # override=False ensures CLI/shell variables take precedence
    load_dotenv(dotenv_path=path, override=False)
    # Clear cached settings to reload from new env
    get_settings.cache_clear()


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
    api_key: SecretStr = Field(default=SecretStr(""), description="API key for authentication")

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
    ssl_cert_dir: str = Field(
        default=f"{_HSYNC_PATH}/certs",
        description="Directory for auto-generated certificates",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Normalize log level to lowercase."""
        if isinstance(v, str):
            return v.lower()
        return v

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
    return Settings()


# =============================================================================
# Module-level exports for backward compatibility
# =============================================================================

# These are evaluated at import time. For late binding after env file loading,
# use get_settings() or the getter functions below.


def _get_settings() -> Settings:
    """Get current settings (uncached, for backward compatibility)."""
    return Settings()


# Backward compatible module-level constants
# Note: These are evaluated at import time
HSYNC_PATH: str = _HSYNC_PATH
HSYNC_PORT: int = _get_settings().port
HSYNC_WORKERS: int = _get_settings().workers
HSYNC_TZ: str = _get_settings().tz
HSYNC_API_KEY: str = _get_settings().api_key.get_secret_value()
HSYNC_BUCKETS: str = _get_settings().buckets_path
HSYNC_MAX_UPLOAD_SIZE: int = _get_settings().max_upload_size
HSYNC_CORS_ENABLED: bool = _get_settings().cors_enabled
HSYNC_CORS_ORIGINS: list[str] = _get_settings().cors_origins_list
HSYNC_SSL_ENABLED: bool = _get_settings().ssl_enabled
HSYNC_SSL_CERTFILE: str = _get_settings().ssl_certfile
HSYNC_SSL_KEYFILE: str = _get_settings().ssl_keyfile
HSYNC_SSL_CERT_DIR: str = _get_settings().ssl_cert_dir
HSYNC_LOG_LEVEL: str = _get_settings().log_level
HSYNC_COMPRESSION_ENABLED: bool = _get_settings().compression_enabled


# =============================================================================
# Getter Functions (for late binding after env file loading)
# =============================================================================


def get_port() -> int:
    """Get server port from environment (late binding)."""
    return get_settings().port


def get_log_level() -> str:
    """Get log level from environment (late binding)."""
    return get_settings().log_level


def get_ssl_enabled() -> bool:
    """Get SSL enabled flag from environment (late binding)."""
    return get_settings().ssl_enabled


def get_ssl_certfile() -> str:
    """Get SSL certificate file path from environment (late binding)."""
    return get_settings().ssl_certfile


def get_ssl_keyfile() -> str:
    """Get SSL key file path from environment (late binding)."""
    return get_settings().ssl_keyfile


def get_ssl_cert_dir() -> str:
    """Get SSL certificate directory from environment (late binding)."""
    return get_settings().ssl_cert_dir


def get_compression_enabled() -> bool:
    """Get compression enabled flag from environment (late binding)."""
    return get_settings().compression_enabled
