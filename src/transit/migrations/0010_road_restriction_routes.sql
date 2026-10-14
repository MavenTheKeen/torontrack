-- Which bus and streetcar routes each road restriction lies along (transit.road_routes).

-- Every restriction path checked against a schedule version, matched or not, so only new
-- or changed paths are checked again.
CREATE TABLE normalized.road_restriction_checks (
    restriction_id text NOT NULL,
    feed_version text NOT NULL,
    path_sha256 text NOT NULL,
    checked_at timestamptz NOT NULL,
    PRIMARY KEY (restriction_id, feed_version)
);

-- A restriction lies along a route direction when enough of it runs close to the route.
CREATE TABLE normalized.road_restriction_routes (
    restriction_id text NOT NULL,
    feed_version text NOT NULL,
    route_id text NOT NULL,
    direction_id smallint NOT NULL,
    overlap_m integer NOT NULL,
    PRIMARY KEY (restriction_id, feed_version, route_id, direction_id)
);
CREATE INDEX ON normalized.road_restriction_routes (feed_version, route_id);
