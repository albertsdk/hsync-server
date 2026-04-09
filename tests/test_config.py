# HSYNC Server - HTTP-based file synchronization server
# Copyright (C) 2025 Alberto Basso
#
# Tests for config module.

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

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

    def test_cli_variables_take_precedence(self, tmp_path, monkeypatch):
        """Test that existing environment variables are not overwritten."""
        # Set environment variable before loading (monkeypatch garantisce il cleanup)
        monkeypatch.setenv("HSYNC_PORT", "1234")

        env_file = tmp_path / ".env"
        env_file.write_text("HSYNC_PORT=9999\n")

        config.load_env_file(str(env_file))

        settings = config.get_settings()
        assert settings.port == 1234


class TestSettings:
    """Tests for Settings class."""

    def test_default_values(self, monkeypatch):
        """Test that default values are applied."""
        # Clear cache to ensure fresh settings
        config.get_settings.cache_clear()

        # Clear environment variables that might override defaults (monkeypatch garantisce il cleanup)
        monkeypatch.delenv("HSYNC_LOG_LEVEL", raising=False)
        monkeypatch.delenv("HSYNC_PORT", raising=False)

        settings = config.Settings()

        assert settings.port == 9753
        assert settings.workers == 1
        assert settings.tz == "UTC"
        assert settings.log_level == "info"
        assert settings.log_format == "text"
        assert settings.log_file == ""
        assert settings.cors_enabled is False
        assert settings.ssl_enabled is False
        assert settings.compression_enabled is True
        assert settings.max_upload_size == 10 * 1024 * 1024 * 1024  # 10GB
        assert settings.buckets_path.endswith("/buckets")
        assert settings.min_free_disk_space == 500 * 1024 * 1024
        assert settings.rate_limit_default == "500/minute"
        assert settings.rate_limit_upload == "200/minute"
        assert settings.rate_limit_download == "1000/minute"
        assert settings.rate_limit_websocket == "50/minute"
        assert settings.rate_limit_ws_messages == "10/second"
        assert settings.trusted_proxies_set == frozenset()

    def test_custom_values(self):
        """Test that custom values are applied."""
        settings = config.Settings(
            port=8080,
            workers=4,
            tz="Europe/Rome",
            log_level="debug",
            cors_enabled=True,
            cors_origins="http://localhost,http://example.com",
            buckets_path="/tmp/hsync-buckets",
            trusted_proxies="127.0.0.1, 10.0.0.1 ",
        )

        assert settings.port == 8080
        assert settings.workers == 4
        assert settings.tz == "Europe/Rome"
        assert settings.log_level == "debug"
        assert settings.cors_enabled is True
        assert settings.buckets_path == "/tmp/hsync-buckets"
        assert settings.trusted_proxies_set == frozenset({"127.0.0.1", "10.0.0.1"})

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

    def test_buckets_path_default(self):
        """Test that buckets_path defaults to the app directory."""
        settings = config.Settings()

        assert settings.buckets_path.endswith("/buckets")

    def test_hsync_buckets_env_alias(self, monkeypatch, tmp_path):
        """HSYNC_BUCKETS is the documented env var for buckets_path."""
        monkeypatch.setenv("HSYNC_BUCKETS", str(tmp_path))

        settings = config.Settings()

        assert settings.buckets_path == str(tmp_path)

    def test_validate_log_level_normalizes_case(self):
        """Test that log level is normalized to lowercase."""
        settings = config.Settings(log_level="DEBUG")  # type: ignore[arg-type]

        assert settings.log_level == "debug"

    def test_port_range_validation(self):
        """Test that port is validated within range.

        The pydantic ValidationError type (and its range message) is part of
        the public fail-fast contract: a generic Exception catch would pass
        for any unrelated error (wrong constructor signature, TypeError …)."""
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            config.Settings(port=0)  # Below valid range

        with pytest.raises(ValidationError, match="less than or equal to 65535"):
            config.Settings(port=70000)  # Above valid range

    def test_max_upload_size_non_negative(self):
        """Test that max upload size must be non-negative."""
        with pytest.raises(ValidationError, match="greater than or equal to 0"):
            config.Settings(max_upload_size=-1)

    def test_workers_minimum(self):
        """Test that workers must be at least 1."""
        with pytest.raises(ValidationError, match="greater than or equal to 1"):
            config.Settings(workers=0)


class TestAdminKeyValidator:
    """Tests for the admin_key field_validator (fail-fast at config load)."""

    def test_none_admin_key_accepted(self):
        settings = config.Settings(admin_key=None)
        assert settings.admin_key is None

    def test_empty_admin_key_accepted_as_none(self):
        settings = config.Settings(admin_key=SecretStr(""))
        assert settings.admin_key is None or settings.admin_key.get_secret_value() == ""

    def test_valid_hex_admin_key_accepted(self):
        key = "0123456789abcdef" * 4  # 64 hex chars
        settings = config.Settings(admin_key=SecretStr(key))
        assert settings.admin_key.get_secret_value() == key

    def test_malformed_admin_key_rejected(self):
        with pytest.raises(ValidationError, match="64 lowercase hex"):
            config.Settings(admin_key=SecretStr("not-hex"))

    def test_short_admin_key_rejected(self):
        with pytest.raises(ValidationError, match="64 lowercase hex"):
            config.Settings(admin_key=SecretStr("0123456789abcdef"))


class TestSslKeyfilePasswordValidator:
    """Tests for the model_validator enforcing ssl password when SSL is on."""

    def test_ssl_password_required_when_ssl_enabled(self):
        with pytest.raises(ValidationError, match="HSYNC_SSL_KEYFILE_PASSWORD is required"):
            config.Settings(ssl_enabled=True, ssl_keyfile_password="")

    def test_ssl_password_not_required_when_ssl_disabled(self):
        settings = config.Settings(ssl_enabled=False, ssl_keyfile_password="")
        assert settings.ssl_enabled is False
        assert settings.ssl_keyfile_password.get_secret_value() == ""

    def test_ssl_password_accepted_when_ssl_enabled(self):
        settings = config.Settings(ssl_enabled=True, ssl_keyfile_password="some-strong-password")
        assert settings.ssl_enabled is True
        assert settings.ssl_keyfile_password.get_secret_value() == "some-strong-password"


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

    def test_runtime_getters_are_defined(self):
        """Test that all late-binding getters are exposed by the module."""
        for name in (
            "get_port",
            "get_workers",
            "get_admin_key",
            "get_buckets_path",
            "get_state_dir",
            "get_rate_limit_default",
            "get_rate_limit_upload",
            "get_rate_limit_download",
            "get_rate_limit_websocket",
            "get_rate_limit_ws_messages",
            "get_rate_limit_admin",
            "get_trusted_proxies",
            "get_max_upload_size",
            "get_cors_enabled",
            "get_cors_origins",
            "get_log_level",
            "get_log_format",
            "get_log_file",
            "get_compression_enabled",
            "get_ssl_enabled",
            "get_ssl_certfile",
            "get_ssl_keyfile",
            "get_ssl_keyfile_password",
            "get_ssl_cert_dir",
        ):
            assert callable(getattr(config, name, None)), f"missing getter: {name}"
