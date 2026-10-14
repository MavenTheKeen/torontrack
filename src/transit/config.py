"""Small explicit configuration; secrets come only from environment variables."""

import os
from dataclasses import dataclass

DISCOVERY_URL = "https://toronto.publicbikesystem.net/customer/gbfs/v3.0/gbfs.json"
STALE_SECONDS = 1800
FUTURE_TOLERANCE_SECONDS = 300
# Read-only login for the public site, created by init-db when WEB_DB_PASSWORD is set.
WEB_DB_ROLE = "transit_web"


@dataclass(frozen=True)
class Settings:
    database_url: str
    discovery_url: str = DISCOVERY_URL

    @classmethod
    def from_env(cls):
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise ValueError("Set DATABASE_URL before running database commands")
        return cls(url, os.environ.get("GBFS_DISCOVERY_URL", DISCOVERY_URL))
