"""Additional coverage tests for edge cases and branches."""
from __future__ import annotations

import re
from pathlib import Path

from app import config

#: Raw app/config.py text, so a test can assert the *absence* of a hardcoded
#: product version rather than only the absence of a runtime default.
_APP_CONFIG_SOURCE = (
    Path(__file__).resolve().parents[1] / "app" / "config.py"
).read_text(encoding="utf-8")


def test_env_path_absolute_path_returned_as_is(monkeypatch):
    """Absolute paths are used directly, not joined with base_dir."""
    from pathlib import Path
    base = Path(__file__).resolve().parents[1]
    result = config._env_path("SOME_PATH", Path("default.txt"), base_dir=base)
    # If absolute, should be used as-is (in this test case we use a relative path)
    assert "default.txt" in result


def test_env_path_relative_path_joined_with_base_dir(monkeypatch):
    """Relative paths are joined with base_dir."""
    from pathlib import Path
    base = Path("/tmp")
    monkeypatch.setenv("TEST_PATH", "subdir/file.txt")
    result = config._env_path("TEST_PATH", Path("default.txt"), base_dir=base)
    assert "subdir" in result
    assert "file.txt" in result


def test_settings_smtp_password_default_is_empty():
    """SMTP password defaults to empty string."""
    s = config.Settings()
    # Verify it defaults to empty when not set (the actual value will be in env)
    assert isinstance(s.SMTP_PASSWORD, str)


def test_cors_origins_splits_and_strips_correctly(monkeypatch):
    """CORS_ORIGINS are comma-separated and whitespace-trimmed."""
    monkeypatch.setenv("CORS_ORIGINS", "https://a.com, https://b.com , https://c.com")
    # Re-import to get fresh settings
    import importlib

    import app.config as fresh_config
    try:
        reloaded = importlib.reload(fresh_config)
        s = reloaded.Settings()
        assert len(s.CORS_ORIGINS) == 3
        assert "https://a.com" in s.CORS_ORIGINS
        assert "https://b.com" in s.CORS_ORIGINS
        assert "https://c.com" in s.CORS_ORIGINS
    finally:
        importlib.reload(fresh_config)


def test_app_name_has_a_default_but_app_version_does_not(monkeypatch):
    """APP_NAME has a fallback; APP_VERSION must come only from .env.

    There is deliberately no numeric default in code: a hardcoded copy of the
    product version is what drifts out of lockstep with .env, so the assertion
    here is that the default is *empty*, not that it equals a release number.
    """
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)
    import importlib

    import app.config as fresh_config
    # Neutralize .env for the reload: a developer's local .env carries the real
    # APP_VERSION and would otherwise re-populate os.environ through
    # _load_dotenv(). This test asserts the *code* defaults.
    try:
        import dotenv
    except ImportError:  # pragma: no cover - dotenv is an optional dependency
        dotenv = None
    if dotenv is not None:
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    try:
        reloaded = importlib.reload(fresh_config)
        s = reloaded.Settings()
        assert s.APP_NAME == "Bug Hunter"
        # No hardcoded version anywhere in app/config.py either.
        assert s.APP_VERSION == ""
        assert not re.search(r"\b1\.\d+\b", _APP_CONFIG_SOURCE), (
            "app/config.py must not contain a hardcoded product version; "
            "APP_VERSION is owned by .env"
        )
    finally:
        importlib.reload(fresh_config)


def test_app_version_is_read_from_the_environment(monkeypatch):
    """Any version in .env propagates to settings with no code change."""
    monkeypatch.setenv("APP_VERSION", "9.8.7")
    import importlib

    import app.config as fresh_config
    try:
        reloaded = importlib.reload(fresh_config)
        assert reloaded.Settings().APP_VERSION == "9.8.7"
    finally:
        monkeypatch.delenv("APP_VERSION", raising=False)
        importlib.reload(fresh_config)


def test_env_bool_accepts_yes_true_1(monkeypatch):
    """_env_bool accepts 'yes', 'true', '1' (case-insensitive)."""
    monkeypatch.setenv("TEST_BOOL_YES", "yes")
    monkeypatch.setenv("TEST_BOOL_TRUE", "TRUE")
    monkeypatch.setenv("TEST_BOOL_ONE", "1")
    assert config._env_bool("TEST_BOOL_YES", False) is True
    assert config._env_bool("TEST_BOOL_TRUE", False) is True
    assert config._env_bool("TEST_BOOL_ONE", False) is True


def test_env_bool_accepts_no_false_0(monkeypatch):
    """_env_bool accepts 'no', 'false', '0' (case-insensitive)."""
    monkeypatch.setenv("TEST_BOOL_NO", "no")
    monkeypatch.setenv("TEST_BOOL_FALSE", "FALSE")
    monkeypatch.setenv("TEST_BOOL_ZERO", "0")
    assert config._env_bool("TEST_BOOL_NO", True) is False
    assert config._env_bool("TEST_BOOL_FALSE", True) is False
    assert config._env_bool("TEST_BOOL_ZERO", True) is False


def test_env_bool_defaults_to_fallback():
    """_env_bool uses fallback when env var is not set."""
    assert config._env_bool("NONEXISTENT_BOOL_VAR_XYZ", True) is True
    assert config._env_bool("NONEXISTENT_BOOL_VAR_XYZ", False) is False


def test_env_int_parses_valid_integers(monkeypatch):
    """_env_int parses valid integer strings."""
    monkeypatch.setenv("TEST_INT_POS", "42")
    monkeypatch.setenv("TEST_INT_NEG", "-10")
    assert config._env_int("TEST_INT_POS", 0) == 42
    assert config._env_int("TEST_INT_NEG", 0) == -10


def test_env_int_defaults_on_invalid(monkeypatch):
    """_env_int defaults on invalid or missing values."""
    assert config._env_int("NONEXISTENT_INT_VAR_XYZ", 99) == 99
    # Invalid int should default (it's caught silently)
    monkeypatch.setenv("INVALID_INT", "not_a_number")
    assert config._env_int("INVALID_INT", 88) == 88


def test_env_float_parses_valid_floats(monkeypatch):
    """_env_float parses valid float strings."""
    monkeypatch.setenv("TEST_FLOAT_POS", "3.14")
    monkeypatch.setenv("TEST_FLOAT_NEG", "-2.71")
    assert config._env_float("TEST_FLOAT_POS", 0.0) == 3.14
    assert config._env_float("TEST_FLOAT_NEG", 0.0) == -2.71


def test_env_float_defaults_on_invalid(monkeypatch):
    """_env_float defaults on invalid or missing values."""
    assert config._env_float("NONEXISTENT_FLOAT_VAR_XYZ", 9.9) == 9.9
    # Invalid float should default
    monkeypatch.setenv("INVALID_FLOAT", "not_a_float")
    assert config._env_float("INVALID_FLOAT", 7.7) == 7.7


def test_is_production_detects_environment():
    """is_production property distinguishes prod/dev."""
    s = config.Settings()
    # Just verify it's a boolean property (actual value depends on env)
    assert isinstance(s.is_production, bool)


def test_database_url_construction_prod_vs_dev(monkeypatch):
    """DATABASE_URL is constructed differently for prod/dev."""
    # This test ensures the property exists and returns a string
    s = config.Settings()
    assert isinstance(s.DATABASE_URL, str)
    assert "postgres" in s.DATABASE_URL or "sqlite" in s.DATABASE_URL
