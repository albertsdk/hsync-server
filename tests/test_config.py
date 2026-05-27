# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
#
# Tests for config module.

import os
from pathlib import Path

import pytest

from hsync_server import config


class TestLoadEnvFile:
    """Tests for load_env_file function."""

    def test_loads_existing_env_file(self, tmp_path):
        """Test that environment variables are loaded from a file."""
        env_file = tmp_path / ".env"
        env_file.write_text("HSYNC_PORT=9999\nHSYNC_LOG_LEVEL=debug\n")

        config.load_env_file(str(env_file))

        # Verify the settings reflect the loaded values
        settings = config.get_settings()
        assert settings.port == 9999
        assert settings.log_level == "debug"

    def test_raises_file_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing files."""
        with pytest.raises(FileNotFoundError):
            config.load_env_file(str(tmp_path / "nonexistent.env"))

    def test_cli_variables_take_precedence(self, tmp_path):
        """Test that existing environment variables are not overwritten."""
        # Set environment variable before loading
        os.environ["HSYNC_PORT"] = "1234"

        env_file = tmp_path / ".env"
        env_file.write_text("HSYNC_PORT=9999\n")

        config.load_env_file(str(env_file))

        settings = config.get_settings()
        assert settings.port == 1234

        # Cleanup
        del os.environ["HSYNC_PORT"]


class TestSettings:
    """Tests for Settings class."""

    def test_default_values(self):
        """Test that default values are applied."""
        # Clear cache to ensure fresh settings
        config.get_settings.cache_clear()

        # Clear environment variable that might override default
        env_backup = os.environ.get("HSYNC_LOG_LEVEL")
        if "HSYNC_LOG_LEVEL" in os.environ:
            del os.environ["HSYNC_LOG_LEVEL"]

        try:
            settings = config.Settings()

            assert settings.port == 8765
            assert settings.workers == 1
            assert settings.tz == "UTC"
            assert settings.log_level == "info"
            assert settings.cors_enabled is False
            assert settings.ssl_enabled is False
            assert settings.compression_enabled is True
            assert settings.max_upload_size == 10 * 1024 * 1024 * 1024  # 10GB
        finally:
            # Restore environment variable
            if env_backup:
                os.environ["HSYNC_LOG_LEVEL"] = env_backup

    def test_custom_values(self):
        """Test that custom values are applied."""
        settings = config.Settings(
            port=8080,
            workers=4,
            tz="Europe/Rome",
            log_level="debug",
            cors_enabled=True,
            cors_origins="http://localhost,http://example.com",
        )

        assert settings.port == 8080
        assert settings.workers == 4
        assert settings.tz == "Europe/Rome"
        assert settings.log_level == "debug"
        assert settings.cors_enabled is True

    def test_cors_origins_list_property(self):
        """Test that cors_origins_list parses comma-separated origins."""
        settings = config.Settings(
            cors_origins="http://localhost:3000, http://example.com ,  https://test.com"
        )

        origins = settings.cors_origins_list
        assert origins == [
            "http://localhost:3000",
            "http://example.com",
            "https://test.com",
        ]

    def test_cors_origins_list_empty(self):
        """Test that cors_origins_list returns empty list for empty string."""
        settings = config.Settings(cors_origins="")

        assert settings.cors_origins_list == []

    def test_path_property(self):
        """Test that path property returns absolute path."""
        settings = config.Settings()

        assert settings.path is not None
        assert Path(settings.path).is_absolute()

    def test_buckets_path_property(self):
        """Test that buckets_path returns path in app directory."""
        settings = config.Settings()

        assert settings.buckets_path.endswith("/buckets")

    def test_validate_log_level_normalizes_case(self):
        """Test that log level is normalized to lowercase."""
        settings = config.Settings(log_level="DEBUG")  # type: ignore[arg-type]

        assert settings.log_level == "debug"

    def test_port_range_validation(self):
        """Test that port is validated within range."""
        with pytest.raises(Exception):
            config.Settings(port=0)  # Below valid range

        with pytest.raises(Exception):
            config.Settings(port=70000)  # Above valid range

    def test_max_upload_size_non_negative(self):
        """Test that max upload size must be non-negative."""
        with pytest.raises(Exception):
            config.Settings(max_upload_size=-1)

    def test_workers_minimum(self):
        """Test that workers must be at least 1."""
        with pytest.raises(Exception):
            config.Settings(workers=0)


class TestGetSettings:
    """Tests for get_settings function."""

    def test_returns_settings_instance(self):
        """Test that get_settings returns a Settings instance."""
        settings = config.get_settings()

        assert isinstance(settings, config.Settings)

    def test_is_cached(self):
        """Test that get_settings is cached (LRU cache)."""
        # Call twice - should return same instance
        settings1 = config.get_settings()
        settings2 = config.get_settings()

        assert settings1 is settings2

    def test_cache_cleared_after_load_env_file(self, tmp_path):
        """Test that cache is cleared after load_env_file."""
        settings1 = config.get_settings()

        env_file = tmp_path / ".env"
        env_file.write_text("HSYNC_PORT=5555\n")

        config.load_env_file(str(env_file))
        settings2 = config.get_settings()

        assert settings1 is not settings2


class TestGetterFunctions:
    """Tests for getter functions (late binding)."""

    def test_get_port(self):
        """Test get_port returns port value."""
        port = config.get_port()

        assert isinstance(port, int)
        assert 1 <= port <= 65535

    def test_get_log_level(self):
        """Test get_log_level returns valid level."""
        level = config.get_log_level()

        assert level in config.VALID_LOG_LEVELS

    def test_get_ssl_enabled(self):
        """Test get_ssl_enabled returns boolean."""
        assert isinstance(config.get_ssl_enabled(), bool)

    def test_get_compression_enabled(self):
        """Test get_compression_enabled returns boolean."""
        assert isinstance(config.get_compression_enabled(), bool)


class TestModuleLevelConstants:
    """Tests for module-level exported constants."""

    def test_hsync_path_is_absolute(self):
        """Test that HSYNC_PATH is an absolute path."""
        assert Path(config.HSYNC_PATH).is_absolute()

    def test_runtime_getters_are_defined(self):
        """Test that all late-binding getters are exposed by the module."""
        for name in (
            "get_path",
            "get_port",
            "get_workers",
            "get_tz",
            "get_api_key",
            "get_buckets_path",
            "get_max_upload_size",
            "get_cors_enabled",
            "get_cors_origins",
            "get_log_level",
            "get_compression_enabled",
            "get_ssl_enabled",
            "get_ssl_certfile",
            "get_ssl_keyfile",
            "get_ssl_keyfile_password",
            "get_ssl_cert_dir",
        ):
            assert callable(getattr(config, name, None)), f"missing getter: {name}"
