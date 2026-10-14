"""Bus and streetcar stop passages from live positions.

After each vehicle snapshot, every vehicle is placed on its route's stop pattern (see
transit.ttc.matching). Comparing that position with the vehicle's previous one gives the
measurement stops it passed since, each timed by linear interpolation between the two
position reports. Consecutive passages at a stop are headways, which is what bus bunching
and long gaps are measured from.

A vehicle's progress resets, recording nothing, when it changes pattern (a new trip), goes
unmatched (detour, garage), jumps implausibly far, or reports after a long silence; a
small step backwards is GPS noise and is ignored.
"""

from datetime import datetime, timedelta

from transit.ttc.matching import locate, pattern_index

# Positions further apart than this are not interpolated between.
MAX_REPORT_GAP = timedelta(minutes=4)
# More stops than this between two reports is a mismatch, not a fast bus.
MAX_STOPS_PER_STEP = 12
# Steps backwards smaller than this many stops are position noise.
BACKWARD_NOISE = 1.0
VEHICLE_FRESH = timedelta(minutes=5)
FORGET_AFTER = timedelta(minutes=30)


def crossings(previous: dict | None, current: dict, pattern: dict) -> list[tuple]:
    """(stop index, passed_at, report gap in seconds) for measurement stops passed between
    two placements on the same pattern, or [] if the step cannot be trusted."""
    if previous is None or previous["pattern_id"] != pattern["pattern_id"]:
        return []
    gap = current["reported_at"] - previous["reported_at"]
    if gap <= timedelta(0) or gap > MAX_REPORT_GAP:
        return []
    start, end = previous["position"], current["position"]
    if end <= start or end - start > MAX_STOPS_PER_STEP:
        return []
    out = []
    for k in pattern["measured"]:
        if start < k <= end:
            fraction = (k - start) / (end - start)
            out.append((k, previous["reported_at"] + gap * fraction, int(gap.total_seconds())))
    return out


def record(conn, version: str, now: datetime) -> dict:
    """Record passages since the last call and update each vehicle's progress."""
    routes = pattern_index(conn, version)
    if not routes:
        return {"passages": 0, "tracked": 0}
    progress = {
        r["vehicle_id"]: r
        for r in conn.execute(
            "SELECT * FROM ops.ttc_vehicle_progress WHERE feed_version = %s", (version,)
        )
    }
    vehicles = conn.execute(
        """SELECT vehicle_id, route_id, lat, lon, bearing, reported_at
           FROM normalized.ttc_vehicles WHERE reported_at > %s""",
        (now - VEHICLE_FRESH,),
    ).fetchall()
    passages, updates, resets = [], [], []
    for v in vehicles:
        previous = progress.get(v["vehicle_id"])
        if previous and previous["reported_at"] >= v["reported_at"]:
            continue  # No new report since last time.
        match = locate(v["lat"], v["lon"], v["bearing"], routes.get(v["route_id"], []))
        if match is None:
            resets.append(v["vehicle_id"])
            continue
        pattern, position = match
        current = {"pattern_id": pattern["pattern_id"], "position": position, **v}
        same = previous is not None and previous["pattern_id"] == pattern["pattern_id"]
        if same and 0 < previous["position"] - position < BACKWARD_NOISE:
            # Jitter: keep the furthest position so no stop is crossed twice.
            current["position"] = previous["position"]
        for k, passed_at, gap in crossings(previous, current, pattern):
            passages.append(
                (
                    passed_at,
                    v["route_id"],
                    pattern["direction_id"],
                    pattern["stops"][k],
                    v["vehicle_id"],
                    pattern["pattern_id"],
                    gap,
                )
            )
        updates.append(
            (v["vehicle_id"], version, pattern["pattern_id"], current["position"], v["reported_at"])
        )
    with conn.transaction(), conn.cursor() as cur:
        if passages:
            with cur.copy(
                """COPY normalized.ttc_surface_passages (passed_at, route_id, direction_id,
                   stop_id, vehicle_id, pattern_id, report_gap_seconds) FROM STDIN"""
            ) as copy:
                for row in passages:
                    copy.write_row(row)
        cur.executemany(
            """INSERT INTO ops.ttc_vehicle_progress
               (vehicle_id, feed_version, pattern_id, position, reported_at)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (vehicle_id) DO UPDATE SET feed_version = EXCLUDED.feed_version,
                   pattern_id = EXCLUDED.pattern_id, position = EXCLUDED.position,
                   reported_at = EXCLUDED.reported_at""",
            updates,
        )
        cur.execute(
            """DELETE FROM ops.ttc_vehicle_progress
               WHERE vehicle_id = ANY(%s) OR reported_at < %s OR feed_version <> %s""",
            (resets, now - FORGET_AFTER, version),
        )
    return {"passages": len(passages), "tracked": len(updates)}
