"""Settings.from_env: defaults, validation at startup, and secrets given as files."""

import pytest
from psycopg.conninfo import conninfo_to_dict

from transit.config import Settings

URL = "postgresql://transit@postgres:5432/transit"


def test_defaults_and_overrides():
    defaults = Settings.from_env({"DATABASE_URL": URL})
    assert defaults.ttc_poll_seconds == 30 and defaults.web_rate_limit_per_minute == 120
    assert defaults.ticketmaster_api_key is None and defaults.map_dir is None
    tuned = Settings.from_env(
        {"DATABASE_URL": URL, "TTC_FAST_POLL_SECONDS": "5", "MAP_DIR": "/srv/map", "NTFY_URL": " "}
    )
    assert tuned.ttc_fast_poll_seconds == 5.0 and tuned.map_dir == "/srv/map"
    assert tuned.ntfy_url is None  # Blank is unset.


def test_invalid_values_fail_at_startup_naming_the_variable():
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings.from_env({})
    assert Settings.from_env({}, require_database=False).database_url is None
    with pytest.raises(ValueError, match="TTC_POLL_SECONDS must be a number"):
        Settings.from_env({"DATABASE_URL": URL, "TTC_POLL_SECONDS": "fast"})
    with pytest.raises(ValueError, match="WEB_RATE_LIMIT_PER_MINUTE must be positive"):
        Settings.from_env({"DATABASE_URL": URL, "WEB_RATE_LIMIT_PER_MINUTE": "0"})


def test_secrets_can_come_from_files_and_stay_out_of_repr(tmp_path):
    (tmp_path / "db").write_text("p@ss word\n")
    (tmp_path / "tm").write_text("tm-key\n")
    settings = Settings.from_env(
        {
            "DATABASE_URL": URL,
            "DATABASE_PASSWORD_FILE": str(tmp_path / "db"),
            "TICKETMASTER_API_KEY_FILE": str(tmp_path / "tm"),
        }
    )
    assert conninfo_to_dict(settings.database_url)["password"] == "p@ss word"
    assert settings.ticketmaster_api_key == "tm-key"
    assert "p@ss" not in repr(settings) and "tm-key" not in repr(settings)
    with pytest.raises(ValueError, match="NTFY_URL_FILE: cannot read"):
        Settings.from_env({"DATABASE_URL": URL, "NTFY_URL_FILE": str(tmp_path / "missing")})
