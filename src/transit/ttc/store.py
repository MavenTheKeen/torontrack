"""PostgreSQL persistence for TTC data. Raw snapshots commit before normalization, so a
parser or validation fix can be replayed from stored protobuf without refetching."""

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from psycopg.types.json import Jsonb

from transit import locks
from transit.bikeshare.parsing import FeedValidationError
from transit.ttc import realtime

EVENT_LOCK_ID = locks.TTC_EVENT_MATCHING
# A train revisits a platform at most once per round trip (20+ minutes), so a prediction
# within 10 minutes of an existing visit for the same train and platform is that visit.
VISIT_MATCH_WINDOW = timedelta(minutes=10)
LOOKBACK = timedelta(hours=3)
# A dropped platform counts as passed only if the train was due within this time when
# last listed, and only for visits listed recently (older ones span a collection gap).
PASSED_DUE = timedelta(minutes=2)
PASSED_RECENT = timedelta(minutes=10)
# TTC sometimes publishes a trip-updates snapshot listing no trains (or about half of
# them) between two complete ones. Taken at face value it would blank the live site and
# mark every train due within PASSED_DUE as arrived. A snapshot listing fewer than
# DROPOUT_SHARE of the trains seen in the last DROPOUT_WINDOW is skipped; if the feed
# really stays empty, nothing was seen within the window and it is accepted after that.
DROPOUT_SHARE = 0.5
DROPOUT_WINDOW = timedelta(minutes=2)
DROPOUT_MIN_TRAINS = 10
DROPOUT_MIN_VEHICLES = 100
# Vehicle positions outside this box, or older than VEHICLE_STALE, are not shown.
VEHICLE_BOUNDS = (43.4, 44.1, -79.95, -78.9)  # south, north, west, east
VEHICLE_STALE = timedelta(minutes=10)


def active_feed_version(conn) -> str | None:
    row = conn.execute(
        """SELECT feed_version FROM normalized.ttc_gtfs_versions
           ORDER BY loaded_at DESC, feed_version LIMIT 1"""
    ).fetchone()
    return row["feed_version"] if row else None


def stop_lookup(conn, feed_version: str) -> dict[str, dict]:
    rows = conn.execute(
        """SELECT stop_id, route_id, direction_id, stop_order
           FROM normalized.ttc_line_stops WHERE feed_version = %s""",
        (feed_version,),
    ).fetchall()
    return {r["stop_id"]: r for r in rows}


def store_snapshot(conn, feed: str, url: str, payload: bytes, fetched_at: datetime):
    """Return (snapshot_id, is_new). An already-stored feed timestamp is not duplicated."""
    snapshot = realtime.decode(payload)
    with conn.transaction():
        row = conn.execute(
            """INSERT INTO raw.ttc_realtime_snapshots
               (feed, feed_url, feed_timestamp, fetched_at, payload_sha256, payload, entity_count)
               VALUES (%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (feed, feed_timestamp) DO NOTHING RETURNING snapshot_id""",
            (
                feed,
                url,
                snapshot.feed_timestamp,
                fetched_at,
                hashlib.sha256(payload).hexdigest(),
                payload,
                len(snapshot.message.entity),
            ),
        ).fetchone()
        if row:
            return row["snapshot_id"], True
        existing = conn.execute(
            """SELECT snapshot_id FROM raw.ttc_realtime_snapshots
               WHERE feed = %s AND feed_timestamp = %s""",
            (feed, snapshot.feed_timestamp),
        ).fetchone()
        return existing["snapshot_id"], False


def normalize_snapshot(conn, snapshot_id: int) -> dict:
    """(Re)build every normalized row derived from one raw snapshot. Safe to repeat."""
    raw = conn.execute(
        "SELECT * FROM raw.ttc_realtime_snapshots WHERE snapshot_id = %s", (snapshot_id,)
    ).fetchone()
    if not raw:
        raise ValueError(f"Unknown snapshot_id {snapshot_id}")
    feed_version = raw["feed_version"] or active_feed_version(conn)
    if not feed_version:
        raise FeedValidationError("Load static GTFS (ttc-gtfs-refresh) before normalizing")
    stops = stop_lookup(conn, feed_version)
    snapshot = realtime.decode(bytes(raw["payload"]))
    issues = realtime.feed_issues(snapshot, raw["fetched_at"])
    with conn.transaction():
        conn.execute("DELETE FROM ops.ttc_record_issues WHERE snapshot_id = %s", (snapshot_id,))
        if raw["feed"] == "trips_subway":
            accepted, record_issues = _normalize_trips(conn, raw, snapshot, stops)
        elif raw["feed"] == "vehicles":
            accepted, record_issues = _normalize_vehicles(conn, raw, snapshot)
        else:
            accepted, record_issues = _normalize_alerts(conn, raw, snapshot, stops)
        issues += record_issues
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO ops.ttc_record_issues
                   (snapshot_id, feed, issue, action, entity_id, stop_id, detail)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        snapshot_id,
                        raw["feed"],
                        i.issue,
                        i.action,
                        i.entity_id,
                        i.stop_id,
                        Jsonb(i.detail),
                    )
                    for i in issues
                ],
            )
        conn.execute(
            """UPDATE raw.ttc_realtime_snapshots SET feed_version = %s, normalized_at = now()
               WHERE snapshot_id = %s""",
            (feed_version, snapshot_id),
        )
    return {
        "snapshot_id": snapshot_id,
        "feed": raw["feed"],
        "accepted": accepted,
        "rejected": sum(i.action == "rejected" for i in issues),
        "flagged": sum(i.action == "flagged" for i in issues),
    }


def _claim_newest(conn, raw) -> bool:
    """Advance the feed's current snapshot unless a newer one was already normalized."""
    return bool(
        conn.execute(
            """INSERT INTO ops.ttc_feed_state AS s (feed, snapshot_id, feed_timestamp)
               VALUES (%s,%s,%s) ON CONFLICT (feed) DO UPDATE
               SET snapshot_id = excluded.snapshot_id, feed_timestamp = excluded.feed_timestamp
               WHERE excluded.feed_timestamp >= s.feed_timestamp RETURNING feed""",
            (raw["feed"], raw["snapshot_id"], raw["feed_timestamp"]),
        ).fetchone()
    )


def _dropout(conn, seen_at, listed: int) -> realtime.Issue | None:
    recent = conn.execute(
        """SELECT count(DISTINCT train_id) AS trains FROM normalized.ttc_train_stop_events
           WHERE last_seen_at < %s AND last_seen_at >= %s""",
        (seen_at, seen_at - DROPOUT_WINDOW),
    ).fetchone()["trains"]
    if recent < DROPOUT_MIN_TRAINS or listed >= recent * DROPOUT_SHARE:
        return None
    return realtime.Issue(
        "feed_dropout", "rejected", detail={"trains_listed": listed, "trains_recent": recent}
    )


def _normalize_trips(conn, raw, snapshot, stops):
    predictions, issues = realtime.parse_trip_updates(snapshot, stops)
    seen_at, snapshot_id = raw["feed_timestamp"], raw["snapshot_id"]
    trains = sorted({p["train_id"] for p in predictions})
    # Serialize visit matching so a concurrent replay cannot create duplicate visits.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENT_LOCK_ID,))
    dropout = _dropout(conn, seen_at, len(trains))
    if dropout:
        return 0, [*issues, dropout]
    visits = defaultdict(list)
    for row in conn.execute(
        """SELECT event_id, train_id, stop_id, predicted_arrival, first_seen_at,
                  last_seen_at, passed_at
           FROM normalized.ttc_train_stop_events
           WHERE train_id = ANY(%s) AND predicted_arrival > %s""",
        (trains, seen_at - LOOKBACK),
    ):
        visits[(row["train_id"], row["stop_id"])].append(row)
    inserts, updates = [], []
    for p in predictions:
        candidates = [
            v
            for v in visits[(p["train_id"], p["stop_id"])]
            if abs(v["predicted_arrival"] - p["predicted_arrival"]) <= VISIT_MATCH_WINDOW
        ]
        if not candidates:
            inserts.append(
                (
                    realtime.service_date(p["predicted_arrival"]),
                    p["train_id"],
                    p["stop_id"],
                    p["route_id"],
                    p["direction_id"],
                    p["stop_order"],
                    p["predicted_arrival"],
                    seen_at,
                    seen_at,
                    snapshot_id,
                    p["trip_id"],
                )
            )
            continue
        v = min(candidates, key=lambda c: abs(c["predicted_arrival"] - p["predicted_arrival"]))
        newer = seen_at >= v["last_seen_at"]
        # Listed again after being marked passed: the earlier mark was premature.
        relisted = v["passed_at"] is not None and seen_at >= v["passed_at"]
        updates.append(
            {
                "event_id": v["event_id"],
                "seen": seen_at,
                "newer": newer,
                "relisted": relisted,
                "predicted_arrival": p["predicted_arrival"],
                "snapshot_id": snapshot_id,
                "trip_id": p["trip_id"],
            }
        )
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO normalized.ttc_train_stop_events
               (service_date, train_id, stop_id, route_id, direction_id, stop_order,
                predicted_arrival, first_seen_at, last_seen_at, last_snapshot_id, last_trip_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            inserts,
        )
        # The latest prediction by feed time wins, so replaying an older snapshot after a
        # newer one cannot move an arrival backwards.
        cur.executemany(
            """UPDATE normalized.ttc_train_stop_events SET
                 first_seen_at = least(first_seen_at, %(seen)s),
                 predicted_arrival = CASE WHEN %(newer)s
                   THEN %(predicted_arrival)s ELSE predicted_arrival END,
                 last_snapshot_id = CASE WHEN %(newer)s
                   THEN %(snapshot_id)s ELSE last_snapshot_id END,
                 last_trip_id = CASE WHEN %(newer)s THEN %(trip_id)s ELSE last_trip_id END,
                 passed_at = CASE WHEN %(relisted)s THEN NULL ELSE passed_at END,
                 last_seen_at = greatest(last_seen_at, %(seen)s)
               WHERE event_id = %(event_id)s""",
            updates,
        )
        # A visit no longer listed has passed if the train was due there when last listed,
        # whether or not the train is still in the feed (it may have reached its terminal
        # or changed label). A platform dropped well before its predicted time was a
        # withdrawn prediction (short turn, reroute), not an arrival.
        cur.execute(
            """UPDATE normalized.ttc_train_stop_events
               SET passed_at = least(coalesce(passed_at, %(seen)s), %(seen)s)
               WHERE last_seen_at < %(seen)s AND last_seen_at > %(seen)s - %(recent)s
                 AND predicted_arrival <= last_seen_at + %(due)s""",
            {"seen": seen_at, "recent": PASSED_RECENT, "due": PASSED_DUE},
        )
        if _claim_newest(conn, raw):
            cur.execute("DELETE FROM normalized.ttc_current_predictions")
            cur.executemany(
                """INSERT INTO normalized.ttc_current_predictions
                   (train_id, trip_id, stop_id, route_id, direction_id, direction_label,
                    stop_order, stop_sequence, predicted_arrival, snapshot_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        p["train_id"],
                        p["trip_id"],
                        p["stop_id"],
                        p["route_id"],
                        p["direction_id"],
                        p["direction_label"],
                        p["stop_order"],
                        p["stop_sequence"],
                        p["predicted_arrival"],
                        snapshot_id,
                    )
                    for p in predictions
                ],
            )
    return len(predictions), issues


def _normalize_alerts(conn, raw, snapshot, stops):
    alerts, issues = realtime.parse_alerts(snapshot)
    seen_at, snapshot_id, feed = raw["feed_timestamp"], raw["snapshot_id"], raw["feed"]
    for a in alerts:
        stop_ids = sorted({e["stop_id"] for e in a["informed_entities"] if e["stop_id"]})
        route_ids = {e["route_id"] for e in a["informed_entities"] if e["route_id"]}
        # Accessibility alerts name only platforms; derive their lines from static GTFS.
        route_ids |= {stops[s]["route_id"] for s in stop_ids if s in stops}
        conn.execute(
            """INSERT INTO normalized.ttc_alerts AS a
               (feed, alert_id, first_seen_at, last_seen_at, last_snapshot_id,
                current_version_hash)
               VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (feed, alert_id) DO UPDATE SET
                 first_seen_at = least(a.first_seen_at, excluded.first_seen_at),
                 last_snapshot_id = CASE WHEN excluded.last_seen_at >= a.last_seen_at
                   THEN excluded.last_snapshot_id ELSE a.last_snapshot_id END,
                 current_version_hash = CASE WHEN excluded.last_seen_at >= a.last_seen_at
                   THEN excluded.current_version_hash ELSE a.current_version_hash END,
                 last_seen_at = greatest(a.last_seen_at, excluded.last_seen_at)""",
            (feed, a["alert_id"], seen_at, seen_at, snapshot_id, a["version_hash"]),
        )
        conn.execute(
            """INSERT INTO normalized.ttc_alert_versions AS v
               (feed, alert_id, version_hash, first_seen_at, last_seen_at, first_snapshot_id,
                cause, effect, header_text, description_text, url, active_periods,
                informed_entities, route_ids, stop_ids, effect_status, text_status,
                derived_status, status_mismatch, advance_notice)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (feed, alert_id, version_hash) DO UPDATE SET
                 -- Derived fields are recomputed so a replay applies improved rules.
                 route_ids = excluded.route_ids,
                 effect_status = excluded.effect_status,
                 text_status = excluded.text_status,
                 derived_status = excluded.derived_status,
                 status_mismatch = excluded.status_mismatch,
                 advance_notice = excluded.advance_notice,
                 first_snapshot_id = CASE WHEN excluded.first_seen_at < v.first_seen_at
                   THEN excluded.first_snapshot_id ELSE v.first_snapshot_id END,
                 first_seen_at = least(v.first_seen_at, excluded.first_seen_at),
                 last_seen_at = greatest(v.last_seen_at, excluded.last_seen_at)""",
            (
                feed,
                a["alert_id"],
                a["version_hash"],
                seen_at,
                seen_at,
                snapshot_id,
                a["cause"],
                a["effect"],
                a["header_text"],
                a["description_text"],
                a["url"],
                Jsonb(a["active_periods"]),
                Jsonb(a["informed_entities"]),
                sorted(route_ids),
                stop_ids,
                a["effect_status"],
                a["text_status"],
                a["derived_status"],
                a["status_mismatch"],
                a["advance_notice"],
            ),
        )
    _claim_newest(conn, raw)
    return len(alerts), issues


def _normalize_vehicles(conn, raw, snapshot):
    """Replace the current position of every bus and streetcar in revenue service.
    Vehicles without a route are out of service and are not listed."""
    issues, rows, seen = [], [], set()
    south, north, west, east = VEHICLE_BOUNDS
    for entity in snapshot.message.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.trip.route_id:
            continue
        vehicle_id = v.vehicle.id or v.vehicle.label
        if not vehicle_id or vehicle_id in seen:
            issues.append(realtime.Issue("missing_or_duplicate_vehicle", "rejected", entity.id))
            continue
        seen.add(vehicle_id)
        lat, lon = v.position.latitude, v.position.longitude
        if not (south <= lat <= north and west <= lon <= east):
            issues.append(
                realtime.Issue(
                    "position_outside_toronto",
                    "rejected",
                    entity.id,
                    detail={"lat": lat, "lon": lon},
                )
            )
            continue
        reported = (
            datetime.fromtimestamp(v.timestamp, UTC) if v.timestamp else raw["feed_timestamp"]
        )
        age = (raw["feed_timestamp"] - reported).total_seconds()
        if age > VEHICLE_STALE.total_seconds():
            issues.append(
                realtime.Issue(
                    "stale_vehicle_position",
                    "rejected",
                    entity.id,
                    detail={"age_seconds": int(age)},
                )
            )
            continue
        rows.append(
            (
                vehicle_id,
                v.trip.route_id,
                v.trip.trip_id or None,
                lat,
                lon,
                round(v.position.bearing) % 360 if v.position.HasField("bearing") else None,
                v.position.speed if v.position.HasField("speed") else None,
                v.stop_id or None,
                realtime.gtfs_realtime_pb2.VehiclePosition.VehicleStopStatus.Name(v.current_status)
                if v.HasField("current_status")
                else None,
                realtime.gtfs_realtime_pb2.VehiclePosition.OccupancyStatus.Name(v.occupancy_status)
                if v.HasField("occupancy_status")
                else None,
                reported,
                raw["snapshot_id"],
            )
        )
    # As with subway trips, a snapshot listing far fewer vehicles than are on the map
    # now is a feed dropout; keep the current positions rather than blank the map.
    current = conn.execute(
        "SELECT count(*) AS n FROM normalized.ttc_vehicles WHERE reported_at > %s",
        (raw["feed_timestamp"] - DROPOUT_WINDOW,),
    ).fetchone()["n"]
    if current >= DROPOUT_MIN_VEHICLES and len(rows) < DROPOUT_SHARE * current:
        issues.append(
            realtime.Issue(
                "feed_dropout", "rejected", detail={"listed": len(rows), "recent": current}
            )
        )
        return 0, issues
    if _claim_newest(conn, raw):
        conn.execute("DELETE FROM normalized.ttc_vehicles")
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO normalized.ttc_vehicles
                   (vehicle_id, route_id, trip_id, lat, lon, bearing, speed_mps, stop_id, status,
                    occupancy, reported_at, snapshot_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                rows,
            )
    return len(rows), issues


def apply_retention(
    conn, raw_days: int, event_days: int, poll_days: int = 30, vehicle_raw_hours: int = 6
) -> dict:
    """Raw protobuf is short-lived; derived history lives longer. Never deletes the
    snapshot a feed currently points at. Vehicle snapshots are ~130 KB every 30 seconds,
    so they are kept for hours rather than days."""
    now = datetime.now(UTC)
    with conn.transaction():
        raw = conn.execute(
            """DELETE FROM raw.ttc_realtime_snapshots
               WHERE (fetched_at < %s OR (feed = 'vehicles' AND fetched_at < %s))
                 AND snapshot_id NOT IN (SELECT snapshot_id FROM ops.ttc_feed_state)""",
            (now - timedelta(days=raw_days), now - timedelta(hours=vehicle_raw_hours)),
        ).rowcount
        events = conn.execute(
            "DELETE FROM normalized.ttc_train_stop_events WHERE predicted_arrival < %s",
            (now - timedelta(days=event_days),),
        ).rowcount
        polls = conn.execute(
            "DELETE FROM ops.ttc_poll_runs WHERE started_at < %s",
            (now - timedelta(days=poll_days),),
        ).rowcount
        surface = conn.execute(
            "DELETE FROM normalized.ttc_surface_passages WHERE passed_at < %s",
            (now - timedelta(days=event_days),),
        ).rowcount
    return {
        "raw_snapshots": raw,
        "train_stop_events": events,
        "poll_runs": polls,
        "surface_passages": surface,
    }


def compact_event_indexes(conn) -> dict:
    """Rebuild the train visit indexes without blocking writes. Every poll updates the
    predicted arrival of each visit in the feed, and those updates leave the indexes about
    four times larger than a fresh build (77 MB against 17 MB after four days)."""
    size = "SELECT pg_indexes_size('normalized.ttc_train_stop_events') AS bytes"
    before = conn.execute(size).fetchone()["bytes"]
    conn.execute("REINDEX TABLE CONCURRENTLY normalized.ttc_train_stop_events")
    after = conn.execute(size).fetchone()["bytes"]
    return {"index_bytes_before": before, "index_bytes_after": after}
