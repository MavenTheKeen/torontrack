"use strict";
// Moving trains, buses and streetcars between data refreshes (docs/map.md#moving-vehicles).
// Plain functions of the data and the time, with no page or map dependencies, so they are
// tested in a headless browser (tests/test_motion.py).
//
// - Subway trains follow TTC's predicted arrival at the next station: a train runs from
//   the previous station to the next over the scheduled run time, arriving at the
//   predicted time, and waits there until a refresh says it has left.
// - Buses and streetcars start from their last GPS report and move along their route's
//   stop pattern at the route's usual pace for the hour, after holding still for
//   STOPPED_HOLD_S if they reported standing still. Only the movement is added to the
//   reported position, which keeps the report's own offset from the straight lines between
//   stops. After MAX_MOVE_S since the report the estimate stops, and the vehicle fades once
//   its report is FADE_AFTER_S old. Tested against 630,000 later reports, this roughly
//   halved the map's position error (docs/map.md).
// - Regional trains follow their timetable track, sent ahead by the server.
//
// When fresh data moves a vehicle's estimate, the vehicle glides to it over CORRECT_MS
// instead of jumping, and its heading arrow turns at up to TURN_DEG_PER_S instead of
// snapping at each corner of its route.

const STOPPED_HOLD_S = 20;
const MAX_MOVE_S = 120;
const FADE_AFTER_S = 150;
const CORRECT_MS = 2500;
const TURN_DEG_PER_S = 180;

// ---- Subway -------------------------------------------------------------------------

// A train's position as a fractional index into its line's station list at `nowMs`.
function trainPosition(train, nowMs) {
  if (train.from_index === null || train.from_index === undefined || !train.segment_seconds || !train.arrives_at) {
    return train.position;
  }
  const eta = (Date.parse(train.arrives_at) - nowMs) / 1000;
  const f = Math.max(0, Math.min(1, eta / train.segment_seconds));
  return train.next_index + (train.from_index - train.next_index) * f;
}

// ---- Buses and streetcars --------------------------------------------------------------

// A pattern as sent by /api/patterns ([lon, lat, timetable seconds] per stop), indexed once.
function preparePattern(points) {
  return { lon: points.map((p) => p[0]), lat: points.map((p) => p[1]), offset: points.map((p) => p[2]) };
}

function offsetAt(pattern, position) {
  const n = pattern.offset.length;
  const i = Math.min(Math.floor(position), n - 1);
  const j = Math.min(i + 1, n - 1);
  return pattern.offset[i] + (position - i) * (pattern.offset[j] - pattern.offset[i]);
}

function positionAtOffset(pattern, offset) {
  const o = pattern.offset;
  if (offset >= o[o.length - 1]) return o.length - 1;
  for (let i = 0; i < o.length - 1; i++) {
    if (o[i] <= offset && offset <= o[i + 1]) return i + (o[i + 1] === o[i] ? 0 : (offset - o[i]) / (o[i + 1] - o[i]));
  }
  return 0;
}

function patternPoint(pattern, position) {
  const n = pattern.lon.length;
  const i = Math.max(0, Math.min(n - 2, Math.floor(position)));
  const f = Math.max(0, Math.min(1, position - i));
  return [pattern.lon[i] + f * (pattern.lon[i + 1] - pattern.lon[i]), pattern.lat[i] + f * (pattern.lat[i + 1] - pattern.lat[i])];
}

// Where a vehicle is estimated to be at `nowMs`: { coord, bearing, fade }. `v` has lon, lat,
// bearing, reported (epoch seconds), stopped, position and pace; `pattern` is prepared.
function vehiclePlace(v, pattern, nowMs) {
  const age = nowMs / 1000 - v.reported;
  const fade = age > FADE_AFTER_S ? 0.35 : 1;
  if (!pattern || v.position === null || v.position === undefined) return { coord: [v.lon, v.lat], bearing: v.bearing, fade };
  const moving = Math.max(0, Math.min(MAX_MOVE_S, age) - (v.stopped ? STOPPED_HOLD_S : 0));
  if (moving <= 0) return { coord: [v.lon, v.lat], bearing: v.bearing, fade };
  const from = patternPoint(pattern, v.position);
  const ahead = positionAtOffset(pattern, offsetAt(pattern, v.position) + v.pace * moving);
  const to = patternPoint(pattern, ahead);
  const coord = [v.lon + to[0] - from[0], v.lat + to[1] - from[1]];
  // Heading: along the stretch of route the vehicle is now on.
  const i = Math.max(0, Math.min(pattern.lon.length - 2, Math.floor(ahead)));
  const dx = (pattern.lon[i + 1] - pattern.lon[i]) * Math.cos((coord[1] * Math.PI) / 180);
  const dy = pattern.lat[i + 1] - pattern.lat[i];
  const bearing = dx || dy ? Math.round(((Math.atan2(dx, dy) * 180) / Math.PI + 360) % 360) : v.bearing;
  return { coord, bearing, fade };
}

// ---- Regional trains -------------------------------------------------------------------

// A position along a timetable track ([lon, lat, epoch seconds], ascending) at `nowMs`.
function trackPoint(track, nowMs) {
  if (!track || !track.length) return null;
  const t = nowMs / 1000;
  if (t <= track[0][2]) return [track[0][0], track[0][1]];
  for (let k = 0; k < track.length - 1; k++) {
    const [a, b] = [track[k], track[k + 1]];
    if (t <= b[2]) {
      const f = b[2] === a[2] ? 0 : (t - a[2]) / (b[2] - a[2]);
      return [a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])];
    }
  }
  const last = track[track.length - 1];
  return [last[0], last[1]];
}

// ---- Gliding to corrected positions -------------------------------------------------------

// Tracks what each vehicle was last drawn at. After new data (a new `seq`), the difference
// between where a vehicle was drawn and its new estimate shrinks to nothing over CORRECT_MS,
// easing in and out so the vehicle neither lurches at the start nor stops abruptly.
function newGlide() {
  return { seq: 0, items: new Map() };
}

function glide(state, id, target, nowMs) {
  let item = state.items.get(id);
  if (!item) {
    item = { seq: state.seq, start: nowMs, dx: 0, dy: 0, coord: target };
    state.items.set(id, item);
  } else if (item.seq !== state.seq) {
    item.dx = item.coord[0] - target[0];
    item.dy = item.coord[1] - target[1];
    item.start = nowMs;
    item.seq = state.seq;
  }
  const p = Math.min(1, Math.max(0, (nowMs - item.start) / CORRECT_MS));
  const ease = 1 - p * p * (3 - 2 * p);
  item.coord = [target[0] + item.dx * ease, target[1] + item.dy * ease];
  item.seen = nowMs;
  return item.coord;
}

// A heading that turns toward `target` (degrees) at up to TURN_DEG_PER_S, the shorter way
// round. Call after glide() for the same id.
function turn(state, id, target, nowMs) {
  const item = state.items.get(id);
  if (!item || target === null || target === undefined) return target;
  if (item.bearing === undefined) {
    item.bearing = target;
  } else {
    const diff = ((((target - item.bearing) % 360) + 540) % 360) - 180;
    const most = (TURN_DEG_PER_S * Math.max(0, nowMs - item.turned)) / 1000;
    item.bearing = (item.bearing + Math.max(-most, Math.min(most, diff)) + 360) % 360;
  }
  item.turned = nowMs;
  return item.bearing;
}

// Forgets vehicles not drawn for a minute (gone from the feed).
function pruneGlide(state, nowMs) {
  for (const [id, item] of state.items) if (nowMs - item.seen > 60000) state.items.delete(id);
}
