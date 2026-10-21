"""All runtime configuration, read once at startup into one Settings object.

Commands and the site read the environment only here (Settings.from_env) and pass the
result to the code that needs it, so tests build Settings directly instead of setting
environment variables. Each secret can be given as NAME or as NAME_FILE, the path of a
file holding it, which is how Docker Compose secrets reach a container (docs/deploy.md).
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from psycopg.conninfo import make_conninfo

DISCOVERY_URL = "https://toronto.publicbikesystem.net/customer/gbfs/v3.0/gbfs.json"
STALE_SECONDS = 1800
FUTURE_TOLERANCE_SECONDS = 300
# Read-only login for the public site, created by init-db when WEB_DB_PASSWORD is set.
WEB_DB_ROLE = "transit_web"


@dataclass(frozen=True)
class Settings:
    # Secrets, and the database URL that can carry one, are left out of repr.
    database_url: str | None = field(default=None, repr=False)
    discovery_url: str = DISCOVERY_URL
    web_db_password: str | None = field(default=None, repr=False)
    ticketmaster_api_key: str | None = field(default=None, repr=False)
    # Live regional trains (docs/regional.md#live-trains); optional.
    metrolinx_api_key: str | None = field(default=None, repr=False)
    ntfy_url: str | None = field(default=None, repr=False)
    heartbeat_url: str | None = field(default=None, repr=False)
    # TTC collector (docs/ttc.md).
    ttc_poll_seconds: float = 30
    ttc_fast_poll_seconds: float = 10
    ttc_raw_retention_days: int = 3
    ttc_event_retention_days: int = 90
    ttc_vehicle_raw_retention_hours: int = 1
    # Public site.
    web_rate_limit_per_minute: int = 120
    web_plan_rate_limit_per_minute: int = 20
    export_dir: str | None = None
    map_dir: str | None = None
    planner_dir: str | None = None
    planner_url: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ, require_database=True):
        """Settings from environment variables. Invalid values fail here, at startup,
        naming the variable, rather than when first used."""

        def text(name):
            return environ.get(name, "").strip() or None

        def secret(name):
            path = text(f"{name}_FILE")
            if path is None:
                return text(name)
            try:
                return Path(path).read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                raise ValueError(f"{name}_FILE: cannot read {path} ({exc.strerror})") from None

        def number(name, kind):
            value = text(name)
            if value is None:
                return getattr(cls, name.lower())
            try:
                parsed = kind(value)
            except ValueError:
                raise ValueError(f"{name} must be a number, not {value!r}") from None
            if parsed <= 0:
                raise ValueError(f"{name} must be positive, not {value!r}")
            return parsed

        url = text("DATABASE_URL")
        if url is None and require_database:
            raise ValueError("Set DATABASE_URL before running database commands")
        # With Compose secrets the URL carries no password; it is added from its own file.
        password = secret("DATABASE_PASSWORD")
        if url is not None and password is not None:
            url = make_conninfo(url, password=password)
        return cls(
            database_url=url,
            discovery_url=text("GBFS_DISCOVERY_URL") or DISCOVERY_URL,
            web_db_password=secret("WEB_DB_PASSWORD"),
            ticketmaster_api_key=secret("TICKETMASTER_API_KEY"),
            metrolinx_api_key=secret("METROLINX_API_KEY"),
            ntfy_url=secret("NTFY_URL"),
            heartbeat_url=secret("HEARTBEAT_URL"),
            ttc_poll_seconds=number("TTC_POLL_SECONDS", float),
            ttc_fast_poll_seconds=number("TTC_FAST_POLL_SECONDS", float),
            ttc_raw_retention_days=number("TTC_RAW_RETENTION_DAYS", int),
            ttc_event_retention_days=number("TTC_EVENT_RETENTION_DAYS", int),
            ttc_vehicle_raw_retention_hours=number("TTC_VEHICLE_RAW_RETENTION_HOURS", int),
            web_rate_limit_per_minute=number("WEB_RATE_LIMIT_PER_MINUTE", int),
            web_plan_rate_limit_per_minute=number("WEB_PLAN_RATE_LIMIT_PER_MINUTE", int),
            export_dir=text("EXPORT_DIR"),
            map_dir=text("MAP_DIR"),
            planner_dir=text("PLANNER_DIR"),
            planner_url=text("PLANNER_URL"),
        )
