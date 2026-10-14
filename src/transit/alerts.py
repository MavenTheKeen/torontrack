"""Phone alerts for pipeline health.

Every minute, the same checks as the public status page (transit.web.pipeline) decide
whether each TTC feed, Bike Share collection and the dbt build is ok. A component that
stays unhealthy for SUSTAIN sends one notification, and one more when it recovers, so a
single slow poll never alerts. Notifications go to an ntfy topic (NTFY_URL). Each
successful check also pings HEARTBEAT_URL, if set, so an outside monitor such as
healthchecks.io can alert when this process, Docker or the whole machine stops.
"""

import json
import os
import signal
import threading
from datetime import UTC, datetime, timedelta

import httpx

from transit.db import connect
from transit.web.pipeline import LIMITS, pipeline_status

SUSTAIN = timedelta(minutes=3)
CHECK_SECONDS = 60
# A gap this long between checks means monitoring itself was down.
MONITOR_GAP = timedelta(minutes=5)
LABELS = {
    "trips_subway": "TTC train predictions",
    "alerts_subway": "TTC service alerts",
    "alerts_accessibility": "TTC elevator alerts",
    "vehicles": "TTC bus and streetcar positions",
    "bikeshare": "Bike Share collection",
    "dbt": "dbt build",
    "weather": "Weather load",
    "roads": "Road restrictions load",
}


def _log(record: dict) -> None:
    print(json.dumps({"at": datetime.now(UTC).isoformat(), **record}), flush=True)


def _duration(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes / 60:.1f} h"


def components(conn, now: datetime) -> dict[str, dict]:
    status = pipeline_status(conn, now)
    found = {f["feed"]: f for f in status["ttc"]["feeds"]}
    found["bikeshare"] = status["bikeshare"]
    found["dbt"] = status["dbt"]
    found["weather"] = status["weather"]
    found["roads"] = status["roads"]
    return found


def evaluate(conn, now: datetime) -> list[dict]:
    """Update ops.alert_state and return the notifications that are now due."""
    previous = {r["component"]: r for r in conn.execute("SELECT * FROM ops.alert_state")}
    due = []
    last_check = max((r["checked_at"] for r in previous.values()), default=None)
    if last_check is not None and now - last_check > MONITOR_GAP:
        due.append(
            {
                "component": "monitor",
                "status": "ok",
                "title": "Monitoring resumed",
                "message": f"No checks for {_duration(now - last_check)} (since "
                f"{last_check:%Y-%m-%d %H:%M} UTC): the machine, Docker or this service was "
                "down, and nothing was collected then.",
            }
        )
    for name, part in components(conn, now).items():
        state = previous.get(name)
        status = part["status"]
        # Delayed and failing are one incident: moving between them keeps its start.
        same = state and (state["status"] == "ok") == (status == "ok")
        since = state["since"] if same else now
        notified = state["notified_status"] if state else "ok"
        conn.execute(
            """INSERT INTO ops.alert_state (component, status, since, notified_status, checked_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (component) DO UPDATE SET status = EXCLUDED.status,
                   since = EXCLUDED.since, checked_at = EXCLUDED.checked_at""",
            (name, status, since, notified, now),
        )
        label = LABELS[name]
        age = part["age_seconds"]
        last = (
            "never succeeded"
            if age is None
            else f"last success {_duration(timedelta(seconds=age))} ago"
        )
        if status != "ok" and notified == "ok" and now - since >= SUSTAIN:
            due.append(
                {
                    "component": name,
                    "status": status,
                    "title": f"{label}: {status}",
                    "message": f"{label} is {status}: {last} (expected every "
                    f"{_duration(LIMITS[name])} or less).",
                }
            )
        elif status == "ok" and notified != "ok":
            due.append(
                {
                    "component": name,
                    "status": "ok",
                    "title": f"{label}: recovered",
                    "message": f"{label} is healthy again ({last}).",
                }
            )
    return due


def send(client: httpx.Client, url: str, alert: dict) -> None:
    response = client.post(
        url,
        content=alert["message"].encode(),
        headers={
            "Title": alert["title"],
            "Priority": "default" if alert["status"] == "ok" else "high",
            "Tags": "white_check_mark" if alert["status"] == "ok" else "warning",
        },
    )
    response.raise_for_status()


def check(database_url: str, client: httpx.Client, ntfy_url: str | None, now: datetime) -> list:
    """One check: record state, deliver what is due, and record each attempt."""
    with connect(database_url) as conn:
        due = evaluate(conn, now)
        for alert in due:
            error = None
            if ntfy_url:
                try:
                    send(client, ntfy_url, alert)
                except httpx.HTTPError as exc:
                    error = type(exc).__name__
            else:
                error = "NTFY_URL not set"
            conn.execute(
                """INSERT INTO ops.alerts (component, status, title, message, delivered, error)
                   VALUES (%(component)s, %(status)s, %(title)s, %(message)s, %(delivered)s,
                           %(error)s)""",
                {**alert, "delivered": error is None, "error": error},
            )
            # Undelivered alerts stay due and are retried at the next check.
            if error is None and alert["component"] != "monitor":
                conn.execute(
                    "UPDATE ops.alert_state SET notified_status = %s WHERE component = %s",
                    (alert["status"], alert["component"]),
                )
    return due


def run(database_url: str, once: bool = False) -> None:
    ntfy_url = os.environ.get("NTFY_URL") or None
    heartbeat_url = os.environ.get("HEARTBEAT_URL") or None
    if not ntfy_url:
        _log({"warning": "NTFY_URL is not set: alerts are recorded in ops.alerts, not sent"})
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with httpx.Client(timeout=15) as client:
        while not stop.is_set():
            try:
                for alert in check(database_url, client, ntfy_url, datetime.now(UTC)):
                    _log({"alert": alert["title"]})
                if heartbeat_url:
                    client.get(heartbeat_url)
            except Exception as exc:  # Keep monitoring; the heartbeat stops meanwhile.
                _log({"error": f"Check failed: {type(exc).__name__}"})
            if once:
                return
            stop.wait(CHECK_SECONDS)
