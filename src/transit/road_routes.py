"""Match road restrictions to the bus and streetcar routes they lie along.

A route's path is drawn stop to stop along its main patterns (see transit.ttc.matching);
stops sit on the street the route uses, so the straight segments between them follow it
closely except at turns. A restriction lies along a route direction when at least
MIN_OVERLAP_M of it runs within NEAR_M of that path. A cross street passing straight over
the route has up to 2 x NEAR_M (70 m) within NEAR_M of it, one side and the other, so the
threshold sits above that: crossings do not count, a block along the route does. A
restriction given only as a point counts if it is within NEAR_M.

Only restrictions whose path is new or changed, or not yet checked against the current
schedule, are matched again.
"""

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime

from transit.ttc.matching import _xy, pattern_index

NEAR_M = 35
MIN_OVERLAP_M = 100
SAMPLE_M = 10
CELL_M = 200


def _segments(routes: dict[str, list[dict]]):
    """Grid of route segments: cell -> [(route_id, direction_id, ax, ay, bx, by)]."""
    grid = defaultdict(list)
    for route_id, patterns in routes.items():
        for pattern in patterns:
            xy = pattern["xy"]
            for (ax, ay), (bx, by) in zip(xy, xy[1:], strict=False):
                segment = (route_id, pattern["direction_id"], ax, ay, bx, by)
                x0, x1 = sorted((ax, bx))
                y0, y1 = sorted((ay, by))
                for cx in range(int((x0 - NEAR_M) // CELL_M), int((x1 + NEAR_M) // CELL_M) + 1):
                    for cy in range(int((y0 - NEAR_M) // CELL_M), int((y1 + NEAR_M) // CELL_M) + 1):
                        grid[(cx, cy)].append(segment)
    return grid


def _distance(x, y, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    f = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
    return math.hypot(x - (ax + f * dx), y - (ay + f * dy))


def _samples(path: list[list[float]]):
    """(x, y, metres represented) every SAMPLE_M along the path; a point is one sample."""
    xy = [_xy(lat, lon) for lon, lat in path]
    if len(xy) == 1:
        return [(*xy[0], 0.0)]
    out = []
    for (ax, ay), (bx, by) in zip(xy, xy[1:], strict=False):
        length = math.hypot(bx - ax, by - ay)
        steps = max(1, math.ceil(length / SAMPLE_M))
        for i in range(steps):
            f = (i + 0.5) / steps
            out.append((ax + f * (bx - ax), ay + f * (by - ay), length / steps))
    return out


def overlaps(path: list[list[float]], grid) -> dict[tuple, int]:
    """(route_id, direction_id) -> metres of the path within NEAR_M of that route."""
    near: dict[tuple, float] = defaultdict(float)
    samples = _samples(path)
    point = len(samples) == 1
    for x, y, metres in samples:
        hit = set()
        for segment in grid.get((int(x // CELL_M), int(y // CELL_M)), ()):
            key = segment[:2]
            if key not in hit and _distance(x, y, *segment[2:]) <= NEAR_M:
                hit.add(key)
        for key in hit:
            near[key] += metres
    if point:
        return dict.fromkeys(near, 0)
    return {key: round(m) for key, m in near.items() if m >= MIN_OVERLAP_M}


def match(conn, version: str, now: datetime) -> dict:
    routes = pattern_index(conn, version)
    if not routes:
        return {"checked": 0, "matched": 0}
    checked = {
        r["restriction_id"]: r["path_sha256"]
        for r in conn.execute(
            """SELECT restriction_id, path_sha256 FROM normalized.road_restriction_checks
               WHERE feed_version = %s""",
            (version,),
        )
    }
    todo = []
    for r in conn.execute(
        "SELECT restriction_id, path FROM normalized.road_restrictions WHERE removed_at IS NULL"
    ):
        digest = hashlib.sha256(json.dumps(r["path"]).encode()).hexdigest()
        if checked.get(r["restriction_id"]) != digest:
            todo.append((r["restriction_id"], r["path"], digest))
    if not todo:
        return {"checked": 0, "matched": 0}
    grid = _segments(routes)
    rows = [
        (rid, version, route_id, direction_id, metres)
        for rid, path, _ in todo
        for (route_id, direction_id), metres in overlaps(path, grid).items()
    ]
    ids = [rid for rid, _, _ in todo]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """DELETE FROM normalized.road_restriction_routes
               WHERE feed_version = %s AND restriction_id = ANY(%s)""",
            (version, ids),
        )
        cur.executemany(
            """INSERT INTO normalized.road_restriction_routes
               (restriction_id, feed_version, route_id, direction_id, overlap_m)
               VALUES (%s, %s, %s, %s, %s)""",
            rows,
        )
        cur.executemany(
            """INSERT INTO normalized.road_restriction_checks
               (restriction_id, feed_version, path_sha256, checked_at) VALUES (%s, %s, %s, %s)
               ON CONFLICT (restriction_id, feed_version) DO UPDATE
               SET path_sha256 = EXCLUDED.path_sha256, checked_at = EXCLUDED.checked_at""",
            [(rid, version, digest, now) for rid, _, digest in todo],
        )
    return {"checked": len(todo), "matched": len({row[0] for row in rows})}
