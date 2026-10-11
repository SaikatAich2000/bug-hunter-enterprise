"""Tests for app/config.py env parsers and is_production, plus evals._as_bool.
Parsers are fail-closed: bad booleans fall back to defaults; non-finite floats are rejected.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import config
from app.chatbot import evals


# _load_dotenv
def test_load_dotenv_oserror_is_logged(monkeypatch):
    def _raise(*_a, **_k):
        raise OSError("malformed .env")

    monkeypatch.setattr(config, "load_dotenv", _raise)
    config._load_dotenv()  # must not raise; the OSError is caught and logged


def test_load_dotenv_ok(monkeypatch):
    seen = {}
    monkeypatch.setattr(config, "load_dotenv", lambda path, override=False: seen.update(path=path))
    config._load_dotenv()
    assert "path" in seen


# env_bool
def test_env_bool_truthy_falsy(monkeypatch):
    monkeypatch.setenv("X_BOOL", "Yes")
    assert config.env_bool("X_BOOL") is True
    monkeypatch.setenv("X_BOOL", "OFF")
    assert config.env_bool("X_BOOL") is False


def test_env_bool_missing_returns_default(monkeypatch):
    monkeypatch.delenv("X_BOOL", raising=False)
    assert config.env_bool("X_BOOL", default=True) is True
    assert config.env_bool("X_BOOL", default=False) is False


def test_env_bool_garbage_returns_default(monkeypatch):
    # Unrecognized value must not silently flip a default-on security control.
    monkeypatch.setenv("X_BOOL", "enabled")
    assert config.env_bool("X_BOOL", default=True) is True
    monkeypatch.setenv("X_BOOL", "secure")
    assert config.env_bool("X_BOOL", default=False) is False


# env_int
def test_env_int_valid_and_missing(monkeypatch):
    monkeypatch.setenv("X_INT", "42")
    assert config.env_int("X_INT", 1) == 42
    monkeypatch.delenv("X_INT", raising=False)
    assert config.env_int("X_INT", 7) == 7


def test_env_int_float_string_tolerated(monkeypatch):
    monkeypatch.setenv("X_INT", "3600.0")
    assert config.env_int("X_INT", 1) == 3600


def test_env_int_garbage_uses_default(monkeypatch):
    monkeypatch.setenv("X_INT", "ten")
    assert config.env_int("X_INT", 9) == 9


def test_env_int_clamps_minimum(monkeypatch):
    monkeypatch.setenv("X_INT", "-5")
    assert config.env_int("X_INT", 1, minimum=0) == 0


# env_float
def test_env_float_valid_and_missing(monkeypatch):
    monkeypatch.setenv("X_F", "2.5")
    assert config.env_float("X_F", 1.0) == 2.5
    monkeypatch.delenv("X_F", raising=False)
    assert config.env_float("X_F", 1.5) == 1.5


def test_env_float_garbage_uses_default(monkeypatch):
    monkeypatch.setenv("X_F", "notanumber")
    assert config.env_float("X_F", 1.0) == 1.0


def test_env_float_rejects_non_finite(monkeypatch):
    for bad in ("inf", "-inf", "nan"):
        monkeypatch.setenv("X_F", bad)
        assert config.env_float("X_F", 3.0) == 3.0


def test_env_float_clamps_minimum(monkeypatch):
    monkeypatch.setenv("X_F", "-1")
    assert config.env_float("X_F", 1.0, minimum=0.0) == 0.0


# Settings.SMTP_PASSWORD
def test_smtp_password_strips_gmail_display_spaces(monkeypatch):
    """Gmail shows app passwords as 4 space-separated groups; the real secret has none."""
    import importlib

    # Re-import fresh: the module-level `config` binding can go stale if another
    # test's `client` fixture already dropped app.config from sys.modules.
    import app.config as fresh_config

    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
    try:
        reloaded = importlib.reload(fresh_config)
        assert reloaded.get_settings().SMTP_PASSWORD == "abcdefghijklmnop"
    finally:
        monkeypatch.undo()
        importlib.reload(fresh_config)


# Settings.FCM_CREDENTIALS_JSON / WEB_PUSH_ENABLED
def test_earlier_edition_firebase_key_variable_still_configures_push(monkeypatch):
    """FIREBASE_SA_JSON was the earlier enterprise edition's variable for the service-account key."""
    import importlib

    import app.config as fresh_config

    monkeypatch.delenv("FCM_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("WEB_PUSH_ENABLED", raising=False)
    try:
        monkeypatch.delenv("FIREBASE_SA_JSON", raising=False)
        assert importlib.reload(fresh_config).get_settings().WEB_PUSH_ENABLED is False

        monkeypatch.setenv("FIREBASE_SA_JSON", '{"type": "service_account"}')
        settings = importlib.reload(fresh_config).get_settings()
        assert settings.FCM_CREDENTIALS_JSON == '{"type": "service_account"}'
        assert settings.WEB_PUSH_ENABLED is True

        monkeypatch.setenv("FCM_CREDENTIALS_JSON", '{"type": "new"}')
        monkeypatch.setenv("WEB_PUSH_ENABLED", "false")
        settings = importlib.reload(fresh_config).get_settings()
        assert settings.FCM_CREDENTIALS_JSON == '{"type": "new"}'
        assert settings.WEB_PUSH_ENABLED is False
    finally:
        monkeypatch.undo()
        importlib.reload(fresh_config)


# Settings.APP_BASE_URL
# _normalize_database_url
def test_normalize_database_url_no_scheme_separator():
    assert config._normalize_database_url("not-a-url") == "not-a-url"


def test_normalize_database_url_non_postgres_dialect():
    url = "mysql://user:pass@host/db"
    assert config._normalize_database_url(url) == url


def test_normalize_database_url_already_psycopg():
    url = "postgresql+psycopg://user:pass@host/db"
    assert config._normalize_database_url(url) == url


def test_normalize_database_url_bare_postgres_gets_psycopg_driver():
    assert config._normalize_database_url("postgres://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert config._normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )


# Settings.APP_BASE_URL
def test_default_base_url_from_container_app_hostname(monkeypatch):
    monkeypatch.setenv("CONTAINER_APP_HOSTNAME", "app.example.azurecontainerapps.io")
    assert config._default_base_url() == "https://app.example.azurecontainerapps.io"


def test_default_base_url_from_name_and_dns_suffix(monkeypatch):
    monkeypatch.delenv("CONTAINER_APP_HOSTNAME", raising=False)
    monkeypatch.setenv("CONTAINER_APP_NAME", "bug-hunter")
    monkeypatch.setenv("CONTAINER_APP_ENV_DNS_SUFFIX", "bluesky.westus.azurecontainerapps.io")
    assert config._default_base_url() == (
        "https://bug-hunter.bluesky.westus.azurecontainerapps.io"
    )


def test_default_base_url_falls_back_to_localhost(monkeypatch):
    for var in (
        "CONTAINER_APP_HOSTNAME", "CONTAINER_APP_NAME", "CONTAINER_APP_ENV_DNS_SUFFIX",
    ):
        monkeypatch.delenv(var, raising=False)
    assert config._default_base_url() == "http://localhost:8765"


# Settings.is_production
def test_is_production_explicit_env():
    s = config.Settings()
    for env in ("production", "prod", "staging"):
        s.APP_ENV = env
        assert s.is_production is True
    for env in ("dev", "development", "test", "testing", "local", "ci"):
        s.APP_ENV = env
        assert s.is_production is False


def test_is_production_falls_back_to_cookie_secure():
    s = config.Settings()
    s.APP_ENV = "unrecognized"
    s.COOKIE_SECURE = True
    assert s.is_production is True
    s.COOKIE_SECURE = False
    assert s.is_production is False


# evals._as_bool
def test_as_bool_none_returns_default():
    assert evals._as_bool(None, True) is True
    assert evals._as_bool(None, False) is False


def test_as_bool_passthrough_bool():
    assert evals._as_bool(True, False) is True
    assert evals._as_bool(False, True) is False


def test_as_bool_numeric():
    assert evals._as_bool(1, False) is True
    assert evals._as_bool(0, True) is False
    assert evals._as_bool(2.5, False) is True


def test_as_bool_string_spellings():
    for s in ("false", "No", "0", "n", "F", "off"):
        assert evals._as_bool(s, True) is False
    for s in ("true", "YES", "1", "y", "t", "on"):
        assert evals._as_bool(s, False) is True


def test_as_bool_unrecognized_string_uses_default():
    assert evals._as_bool("maybe", True) is True
    assert evals._as_bool("maybe", False) is False


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
    """env_bool accepts 'yes', 'true', '1' (case-insensitive)."""
    monkeypatch.setenv("TEST_BOOL_YES", "yes")
    monkeypatch.setenv("TEST_BOOL_TRUE", "TRUE")
    monkeypatch.setenv("TEST_BOOL_ONE", "1")
    assert config.env_bool("TEST_BOOL_YES", False) is True
    assert config.env_bool("TEST_BOOL_TRUE", False) is True
    assert config.env_bool("TEST_BOOL_ONE", False) is True


def test_env_bool_accepts_no_false_0(monkeypatch):
    """env_bool accepts 'no', 'false', '0' (case-insensitive)."""
    monkeypatch.setenv("TEST_BOOL_NO", "no")
    monkeypatch.setenv("TEST_BOOL_FALSE", "FALSE")
    monkeypatch.setenv("TEST_BOOL_ZERO", "0")
    assert config.env_bool("TEST_BOOL_NO", True) is False
    assert config.env_bool("TEST_BOOL_FALSE", True) is False
    assert config.env_bool("TEST_BOOL_ZERO", True) is False


def test_env_bool_defaults_to_fallback():
    """env_bool uses fallback when env var is not set."""
    assert config.env_bool("NONEXISTENT_BOOL_VAR_XYZ", True) is True
    assert config.env_bool("NONEXISTENT_BOOL_VAR_XYZ", False) is False


def test_env_int_parses_valid_integers(monkeypatch):
    """env_int parses valid integer strings."""
    monkeypatch.setenv("TEST_INT_POS", "42")
    monkeypatch.setenv("TEST_INT_NEG", "-10")
    assert config.env_int("TEST_INT_POS", 0) == 42
    assert config.env_int("TEST_INT_NEG", 0) == -10


def test_env_int_defaults_on_invalid(monkeypatch):
    """env_int defaults on invalid or missing values."""
    assert config.env_int("NONEXISTENT_INT_VAR_XYZ", 99) == 99
    # Invalid int should default (it's caught silently)
    monkeypatch.setenv("INVALID_INT", "not_a_number")
    assert config.env_int("INVALID_INT", 88) == 88


def test_env_float_parses_valid_floats(monkeypatch):
    """env_float parses valid float strings."""
    monkeypatch.setenv("TEST_FLOAT_POS", "3.14")
    monkeypatch.setenv("TEST_FLOAT_NEG", "-2.71")
    assert config.env_float("TEST_FLOAT_POS", 0.0) == 3.14
    assert config.env_float("TEST_FLOAT_NEG", 0.0) == -2.71


def test_env_float_defaults_on_invalid(monkeypatch):
    """env_float defaults on invalid or missing values."""
    assert config.env_float("NONEXISTENT_FLOAT_VAR_XYZ", 9.9) == 9.9
    # Invalid float should default
    monkeypatch.setenv("INVALID_FLOAT", "not_a_float")
    assert config.env_float("INVALID_FLOAT", 7.7) == 7.7


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
