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
// When fresh data moves a vehicle's estimate, the difference is made up evenly over
// CORRECT_MS, the time until the next refresh: a vehicle drawn behind runs a little faster
// for the whole interval rather than lurching forward in a burst, and one drawn ahead
// waits instead of backing up. A correction over FAR_DEG (a detour, a report long missing)
// glides over QUICK_MS instead. Heading arrows turn at up to TURN_DEG_PER_S instead of
// snapping at each corner of the route.

const STOPPED_HOLD_S = 20;
const MAX_MOVE_S = 120;
const FADE_AFTER_S = 150;
const CORRECT_MS = 10000; // The map refreshes trains and vehicles every 10 s.
const QUICK_MS = 1200;
const FAR_DEG = 0.0025; // About 250 m.
const HOLD_DEG = 0.0015; // A vehicle up to ~150 m ahead of its estimate waits for it.
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
// between where a vehicle is drawn and its new estimate shrinks to nothing: evenly over
// CORRECT_MS, or eased over QUICK_MS when far.
function newGlide() {
  return { seq: 0, items: new Map() };
}

// Distance in degrees of latitude, with longitude scaled for Toronto's latitude.
function degrees(dx, dy) {
  return Math.hypot(dx * 0.72, dy);
}

function glide(state, id, target, nowMs) {
  let item = state.items.get(id);
  if (!item) {
    item = { seq: state.seq, start: nowMs, dx: 0, dy: 0, span: CORRECT_MS, quick: false, coord: target, dir: null };
    state.items.set(id, item);
  } else if (item.seq !== state.seq) {
    item.dx = item.coord[0] - target[0];
    item.dy = item.coord[1] - target[1];
    item.start = nowMs;
    item.seq = state.seq;
    item.quick = degrees(item.dx, item.dy) > FAR_DEG;
    item.span = item.quick ? QUICK_MS : CORRECT_MS;
  }
  const p = Math.min(1, Math.max(0, (nowMs - item.start) / item.span));
  const left = item.quick ? 1 - p * p * (3 - 2 * p) : 1 - p;
  let coord = [target[0] + item.dx * left, target[1] + item.dy * left];
  const step = [coord[0] - item.coord[0], coord[1] - item.coord[1]];
  if (!item.quick && item.dir && step[0] * item.dir[0] + step[1] * item.dir[1] < 0 && degrees(item.coord[0] - target[0], item.coord[1] - target[1]) < HOLD_DEG) {
    // The correction would move it backwards: it waits where it is, and what remains is
    // made up from here once the estimate comes level.
    coord = item.coord;
    item.dx = coord[0] - target[0];
    item.dy = coord[1] - target[1];
    item.start = nowMs;
  } else if (step[0] || step[1]) {
    item.dir = step;
  }
  item.coord = coord;
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
