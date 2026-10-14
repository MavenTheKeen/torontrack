"""Airflow's health endpoint can return HTTP 200 while components are unhealthy."""

import json
import sys
from urllib.request import urlopen


def main():
    try:
        with urlopen("http://127.0.0.1:8080/api/v2/monitor/health", timeout=5) as response:
            health = json.load(response)
        components = ("metadatabase", "scheduler", "dag_processor", "triggerer")
        return int(any(health.get(name, {}).get("status") != "healthy" for name in components))
    except (OSError, ValueError, TypeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
