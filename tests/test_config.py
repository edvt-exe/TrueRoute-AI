import inspect
import logging
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.config as config
from app.config import (
    DEFAULT_ENV_FILE,
    QUIET_LOGGERS,
    ConfigError,
    Settings,
    configure_logging,
    load_settings,
)
from app.services.llm_agent import ALLOWED_EFFORTS

MARKER = "SECRET-MARKER"
KEY = f"sk-ant-api03-{MARKER}"
UA = "TrueRoute-AI/0.1 (you@example.com)"
NAMES = (
    "ANTHROPIC_API_KEY",
    "LLM_MODEL",
    "LLM_EFFORT",
    "APP_USER_AGENT",
    "NOMINATIM_BASE_URL",
    "OVERPASS_BASE_URL",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # the developer shell may still export the real key, keep it out of every test
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


def env_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def minimal(tmp_path: Path, extra: str = "") -> Path:
    return env_file(tmp_path, f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={UA}\n{extra}")


def config_error(path: Path | None) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        load_settings(path)
    return info.value


# loading


def test_defaults(tmp_path: Path) -> None:
    s = load_settings(minimal(tmp_path))
    assert s.anthropic_api_key.get_secret_value() == KEY
    assert s.app_user_agent == UA
    assert (s.llm_model, s.llm_effort, s.log_level) == ("claude-sonnet-5-5", "medium", "INFO")
    assert s.nominatim_base_url == "https://nominatim.openstreetmap.org"
    assert s.overpass_base_url == "https://overpass-api.de/api"


def test_all_values_from_file(tmp_path: Path) -> None:
    path = minimal(
        tmp_path,
        "LLM_MODEL=claude-haiku-5-5\nLLM_EFFORT=low\nLOG_LEVEL=warning\n"
        "NOMINATIM_BASE_URL=https://geo.example.org/\nOVERPASS_BASE_URL=https://ovp.example.org/api\n",
    )
    s = load_settings(path)
    assert (s.llm_model, s.llm_effort, s.log_level) == ("claude-haiku-5-5", "low", "WARNING")
    assert s.nominatim_base_url == "https://geo.example.org"
    assert s.overpass_base_url == "https://ovp.example.org/api"


def test_names_are_case_insensitive(tmp_path: Path) -> None:
    s = load_settings(env_file(tmp_path, f"anthropic_api_key={KEY}\napp_user_agent={UA}\n"))
    assert s.app_user_agent == UA


def test_environment_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-5-5")
    s = load_settings(minimal(tmp_path, "LLM_MODEL=claude-opus-5-5\n"))
    assert s.llm_model == "claude-haiku-5-5"


def test_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("APP_USER_AGENT", UA)
    assert load_settings(None).app_user_agent == UA


def test_missing_file_is_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("APP_USER_AGENT", UA)
    assert load_settings(tmp_path / "missing.env").llm_effort == "medium"


def test_comments_and_quotes_in_file(tmp_path: Path) -> None:
    text = f'# my settings\nANTHROPIC_API_KEY="{KEY}"\nAPP_USER_AGENT="{UA}"\n\n'
    assert load_settings(env_file(tmp_path, text)).app_user_agent == UA


def test_default_env_file_is_repo_root() -> None:
    assert DEFAULT_ENV_FILE == Path(config.__file__).resolve().parent.parent / ".env"
    assert inspect.signature(load_settings).parameters["env_file"].default == DEFAULT_ENV_FILE


def test_env_example_matches_settings() -> None:
    # .env.example documents exactly the settings the app reads
    example = DEFAULT_ENV_FILE.with_name(".env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0].strip() for line in example.splitlines() if "=" in line and not line.lstrip().startswith("#")}
    assert keys == {name.upper() for name in Settings.model_fields}
    assert set(NAMES) == keys


# required and invalid values


def test_missing_required(tmp_path: Path) -> None:
    exc = config_error(env_file(tmp_path, "LLM_EFFORT=low\n"))
    assert "ANTHROPIC_API_KEY: Field required" in exc.message
    assert "APP_USER_AGENT: Field required" in exc.message


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("LLM_EFFORT=max", "LLM_EFFORT"),
        ("LLM_EFFORT=MEDIUM", "LLM_EFFORT"),
        ("LLM_MODEL=Claude Sonnet", "LLM_MODEL"),
        ("LLM_MODEL=", "LLM_MODEL"),
        ("LOG_LEVEL=TRACE", "LOG_LEVEL"),
        ("NOMINATIM_BASE_URL=http://nominatim.openstreetmap.org", "NOMINATIM_BASE_URL"),
        ("OVERPASS_BASE_URL=https://user:pass@overpass-api.de/api", "OVERPASS_BASE_URL"),
    ],
)
def test_invalid_value(tmp_path: Path, line: str, name: str) -> None:
    exc = config_error(minimal(tmp_path, line + "\n"))
    assert exc.message.startswith("invalid settings: ")
    assert f"{name}: " in exc.message


@pytest.mark.parametrize("agent", ["", "python-requests/2.32", "curl/8.5", "Mozilla/5.0"])
def test_invalid_user_agent(tmp_path: Path, agent: str) -> None:
    exc = config_error(env_file(tmp_path, f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={agent}\n"))
    assert "APP_USER_AGENT: " in exc.message


@pytest.mark.parametrize("key", ["", '"sk-ant has space"', '"sk-ant\\ttab"'])
def test_invalid_key(tmp_path: Path, key: str) -> None:
    exc = config_error(env_file(tmp_path, f"ANTHROPIC_API_KEY={key}\nAPP_USER_AGENT={UA}\n"))
    assert "ANTHROPIC_API_KEY: " in exc.message


def test_unknown_key_rejected(tmp_path: Path) -> None:
    exc = config_error(minimal(tmp_path, "LLM_MODLE=claude-haiku-5-5\n"))
    assert "LLM_MODLE: Extra inputs are not permitted" in exc.message


def test_every_effort_level_allowed(tmp_path: Path) -> None:
    for effort in ALLOWED_EFFORTS:
        path = minimal(tmp_path, f"LLM_EFFORT={effort}\n")
        assert load_settings(path).llm_effort == effort


@pytest.mark.parametrize("level", ["debug", "INFO", " Warning ", "error"])
def test_log_level_normalized(tmp_path: Path, level: str) -> None:
    s = load_settings(minimal(tmp_path, f"LOG_LEVEL={level}\n"))
    assert s.log_level == level.strip().upper()


def test_log_level_from_environment_is_trimmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the dotenv parser trims file values, real environment values arrive as typed
    monkeypatch.setenv("LOG_LEVEL", " warning ")
    assert load_settings(minimal(tmp_path)).log_level == "WARNING"


def test_settings_are_frozen(tmp_path: Path) -> None:
    s = load_settings(minimal(tmp_path))
    with pytest.raises(ValidationError):
        setattr(s, "llm_model", "claude-haiku-5-5")


# secrets never leak


def test_key_masked_in_every_rendering(tmp_path: Path) -> None:
    s = load_settings(minimal(tmp_path))
    for text in (repr(s), str(s), str(s.model_dump()), s.model_dump_json(), str(s.anthropic_api_key)):
        assert MARKER not in text


@pytest.mark.parametrize(
    "text",
    [
        # key pasted into the wrong setting
        f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={UA}\nLLM_MODEL={KEY} x\n",
        f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={UA}\nLOG_LEVEL={KEY}\n",
        f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={KEY} x\n",
        f'ANTHROPIC_API_KEY="{KEY} x"\nAPP_USER_AGENT={UA}\n',
        # key used as a setting name
        f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={UA}\n{KEY}=1\n",
    ],
    ids=["model", "log_level", "user_agent", "key_with_space", "key_as_name"],
)
def test_errors_never_show_values(tmp_path: Path, text: str) -> None:
    exc = config_error(env_file(tmp_path, text))
    assert MARKER not in exc.message
    assert exc.__cause__ is None
    assert exc.__suppress_context__
    assert MARKER not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize(
    "line",
    [
        f"APP_USER_AGENT=TrueRoute-AI/0.1 ({KEY})",
        "APP_USER_AGENT=TrueRoute-AI/0.1 (SK-ANT-abc)",
        f"NOMINATIM_BASE_URL=https://geo.example.org/{KEY}",
        f"OVERPASS_BASE_URL=https://ovp.example.org/api/{KEY}",
    ],
)
def test_key_never_goes_to_third_parties(tmp_path: Path, line: str) -> None:
    text = f"ANTHROPIC_API_KEY={KEY}\nAPP_USER_AGENT={UA}\n{line}\n"
    exc = config_error(env_file(tmp_path, text))
    assert "must not contain an API key" in exc.message
    assert MARKER not in exc.message


def test_odd_names_are_hidden(tmp_path: Path) -> None:
    exc = config_error(minimal(tmp_path, f"{KEY}=1\n"))
    assert "an unrecognized line: Extra inputs are not permitted" in exc.message


def test_bare_secret_line_is_ignored(tmp_path: Path) -> None:
    s = load_settings(minimal(tmp_path, f"{KEY}\n"))
    assert s.app_user_agent == UA


def test_pydantic_errors_hide_input_too(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=minimal(tmp_path, f"LLM_MODEL={KEY} x\n"))  # type: ignore[call-arg]
    assert MARKER not in str(info.value)


# logging


@pytest.fixture
def restore_logging() -> Iterator[None]:
    names = ("", *QUIET_LOGGERS)
    saved = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "ERROR"])
def test_configure_logging(tmp_path: Path, level: str, restore_logging: None) -> None:
    configure_logging(load_settings(minimal(tmp_path, f"LOG_LEVEL={level}\n")))
    assert logging.getLogger().level == getattr(logging, level)
    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_quiet_loggers_cover_known_noise() -> None:
    assert set(QUIET_LOGGERS) == {"anthropic", "httpx2", "httpx", "httpcore"}


def test_sdk_debug_is_suppressed(tmp_path: Path, restore_logging: None, caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(load_settings(minimal(tmp_path, "LOG_LEVEL=DEBUG\n")))
    logging.getLogger("anthropic._base_client").debug("request body with prompt")
    logging.getLogger("app.anything").debug("app debug line")
    assert "request body with prompt" not in caplog.text
    assert "app debug line" in caplog.text