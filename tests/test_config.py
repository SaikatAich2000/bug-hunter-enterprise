"""Tests for app/config.py env parsers and is_production, plus evals._as_bool.
Parsers are fail-closed: bad booleans fall back to defaults; non-finite floats are rejected.
"""
from __future__ import annotations

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


# _env_bool
def test_env_bool_truthy_falsy(monkeypatch):
    monkeypatch.setenv("X_BOOL", "Yes")
    assert config._env_bool("X_BOOL") is True
    monkeypatch.setenv("X_BOOL", "OFF")
    assert config._env_bool("X_BOOL") is False


def test_env_bool_missing_returns_default(monkeypatch):
    monkeypatch.delenv("X_BOOL", raising=False)
    assert config._env_bool("X_BOOL", default=True) is True
    assert config._env_bool("X_BOOL", default=False) is False


def test_env_bool_garbage_returns_default(monkeypatch):
    # Unrecognized value must not silently flip a default-on security control.
    monkeypatch.setenv("X_BOOL", "enabled")
    assert config._env_bool("X_BOOL", default=True) is True
    monkeypatch.setenv("X_BOOL", "secure")
    assert config._env_bool("X_BOOL", default=False) is False


# _env_int
def test_env_int_valid_and_missing(monkeypatch):
    monkeypatch.setenv("X_INT", "42")
    assert config._env_int("X_INT", 1) == 42
    monkeypatch.delenv("X_INT", raising=False)
    assert config._env_int("X_INT", 7) == 7


def test_env_int_float_string_tolerated(monkeypatch):
    monkeypatch.setenv("X_INT", "3600.0")
    assert config._env_int("X_INT", 1) == 3600


def test_env_int_garbage_uses_default(monkeypatch):
    monkeypatch.setenv("X_INT", "ten")
    assert config._env_int("X_INT", 9) == 9


def test_env_int_clamps_minimum(monkeypatch):
    monkeypatch.setenv("X_INT", "-5")
    assert config._env_int("X_INT", 1, minimum=0) == 0


# _env_float
def test_env_float_valid_and_missing(monkeypatch):
    monkeypatch.setenv("X_F", "2.5")
    assert config._env_float("X_F", 1.0) == 2.5
    monkeypatch.delenv("X_F", raising=False)
    assert config._env_float("X_F", 1.5) == 1.5


def test_env_float_garbage_uses_default(monkeypatch):
    monkeypatch.setenv("X_F", "notanumber")
    assert config._env_float("X_F", 1.0) == 1.0


def test_env_float_rejects_non_finite(monkeypatch):
    for bad in ("inf", "-inf", "nan"):
        monkeypatch.setenv("X_F", bad)
        assert config._env_float("X_F", 3.0) == 3.0


def test_env_float_clamps_minimum(monkeypatch):
    monkeypatch.setenv("X_F", "-1")
    assert config._env_float("X_F", 1.0, minimum=0.0) == 0.0


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
