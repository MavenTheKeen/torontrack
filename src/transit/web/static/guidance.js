"use strict";
// Trip guidance (docs/navigation.md), in two parts that know nothing of each other:
//
// 1. Location sources report positions through one small interface:
//      source.start(onFix, onError)   onFix({ lon, lat, accuracy, heading, speed, at })
//      source.stop()
//      source.kind                    "gps" or "simulated"
//    `at` is the fix time in milliseconds; guidance compares it with the timetable, so a
//    simulated source can run its own clock. The browser's GPS is one source; a native
//    app's background location can be another without changing the engine.
//
// 2. The engine turns an itinerary from /api/plan and a fix into what to show next. It is
//    pure: no map, no page, no timers.

const WALK_SPEED_MS = 1.3;
const BIKE_SPEED_MS = 4.5; // For the minutes left on a Bike Share ride.
const ARRIVE_WALK_M = 25; // A walking leg is done this close to its end.
const ARRIVE_RIDE_M = 70; // A ride is done this close to the stop where you get off.
const BOARDED_M = 120; // Past this far along a transit leg, you are on board.
const OFF_ROUTE_M = 50; // Farther than this from a walking route...
const OFF_ROUTE_FIXES = 3; // ...for this many fixes in a row means off route.

// ---- Location sources ------------------------------------------------------------------

const LOCATION_UNSUPPORTED = "Location needs a secure (HTTPS) connection and a browser that supports it.";

function locationErrorText(error) {
  if (error.code === 1) return "Location permission was declined. You can allow it in your browser's site settings.";
  if (error.code === 3) return "Finding your location took too long. Try again, or choose a point on the map.";
  return "This device could not determine its position. On a computer, check that location services are on in the system settings; computers without Wi-Fi often cannot be located. You can choose a point on the map instead.";
}

// Watches the device position. High accuracy (GPS) is tried first; if the device cannot
// provide it, or times out, the watch falls back to network positioning, which is all a
// desktop computer usually has. Permission errors are reported at once.
function watchLocation(onPosition, onError, { maximumAge = 2000 } = {}) {
  let id = null;
  let stopped = false;
  const begin = (highAccuracy) => {
    id = navigator.geolocation.watchPosition(onPosition, (error) => {
      if (stopped) return;
      navigator.geolocation.clearWatch(id);
      if (highAccuracy && error.code !== 1) return begin(false);
      onError(error);
    }, highAccuracy
      ? { enableHighAccuracy: true, maximumAge, timeout: 15000 }
      : { enableHighAccuracy: false, maximumAge: 600000, timeout: 30000 });
  };
  begin(true);
  return {
    stop() {
      stopped = true;
      if (id !== null) navigator.geolocation.clearWatch(id);
    },
  };
}

function browserLocationSource() {
  let watch = null;
  return {
    kind: "gps",
    start(onFix, onError) {
      if (!("geolocation" in navigator) || !window.isSecureContext) {
        onError(new Error(LOCATION_UNSUPPORTED));
        return;
      }
      watch = watchLocation(
        (p) => onFix({
          lon: p.coords.longitude, lat: p.coords.latitude, accuracy: p.coords.accuracy,
          heading: Number.isFinite(p.coords.heading) ? p.coords.heading : null,
          speed: Number.isFinite(p.coords.speed) ? p.coords.speed : null, at: p.timestamp,
        }),
        (e) => onError(new Error(locationErrorText(e))),
      );
    },
    stop() {
      watch?.stop();
      watch = null;
    },
  };
}

// Replays a trip on its own timetable, faster than real time: walking and riding at the
// scheduled pace, waiting at stops until the scheduled departure. For trying guidance on a
// computer, and for demonstrations.
function simulatedLocationSource(trip, { speedup = 10, intervalMs = 1000 } = {}) {
  const prepared = prepareTrip(trip);
  const begin = Date.parse(trip.start);
  const finish = Date.parse(trip.end);
  let timer = null;
  let clock = begin;
  return {
    kind: "simulated",
    start(onFix) {
      const tick = () => {
        const at = scheduledPosition(prepared, clock);
        onFix({ lon: at.point[0], lat: at.point[1], accuracy: 5, heading: at.heading, speed: at.speed, at: clock });
        if (clock >= finish) return;
        clock = Math.min(finish, clock + intervalMs * speedup);
        timer = setTimeout(tick, intervalMs);
      };
      tick();
    },
    stop() {
      clearTimeout(timer);
      timer = null;
    },
  };
}

// Where the timetable puts the traveller at time `at`.
function scheduledPosition(prepared, at) {
  const legs = prepared.legs;
  for (const leg of legs) {
    const start = Date.parse(leg.start);
    const end = Date.parse(leg.end);
    if (at < start) return { point: leg.path[0], heading: null, speed: 0 };
    if (at <= end) {
      const f = end > start ? (at - start) / (end - start) : 1;
      const along = f * leg.length;
      return { point: pointAt(leg, along), heading: headingAt(leg, along), speed: leg.length / Math.max(1, (end - start) / 1000) };
    }
  }
  const last = legs[legs.length - 1];
  return { point: last.path[last.path.length - 1], heading: null, speed: 0 };
}

// ---- Geometry ----------------------------------------------------------------------------

const EARTH_M = 6371000;

function toXY(origin, [lon, lat]) {
  const k = Math.PI / 180;
  return [(lon - origin[0]) * k * EARTH_M * Math.cos(origin[1] * k), (lat - origin[1]) * k * EARTH_M];
}

// A leg's path with the distance from its start at every vertex, in metres.
function measurePath(path) {
  const origin = path[0];
  const xy = path.map((p) => toXY(origin, p));
  const cumulative = [0];
  for (let i = 1; i < xy.length; i++) cumulative.push(cumulative[i - 1] + Math.hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]));
  return { origin, xy, cumulative, length: cumulative[cumulative.length - 1] };
}

// The nearest point of the path to `point`: how far along it is, and how far off it.
function project(measured, point) {
  const [px, py] = toXY(measured.origin, point);
  let best = { along: 0, off: Infinity };
  const xy = measured.xy;
  if (xy.length === 1) return { along: 0, off: Math.hypot(px - xy[0][0], py - xy[0][1]) };
  for (let i = 1; i < xy.length; i++) {
    const [ax, ay] = xy[i - 1];
    const [bx, by] = xy[i];
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    const f = len2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
    const off = Math.hypot(px - (ax + f * dx), py - (ay + f * dy));
    if (off < best.off) best = { along: measured.cumulative[i - 1] + f * Math.sqrt(len2), off };
  }
  return best;
}

function segmentAt(leg, along) {
  const c = leg.cumulative;
  let i = 1;
  while (i < c.length - 1 && c[i] < along) i++;
  return i;
}

function pointAt(leg, along) {
  if (leg.path.length === 1) return leg.path[0];
  const i = segmentAt(leg, along);
  const span = leg.cumulative[i] - leg.cumulative[i - 1];
  const f = span ? Math.max(0, Math.min(1, (along - leg.cumulative[i - 1]) / span)) : 0;
  const a = leg.path[i - 1];
  const b = leg.path[i];
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
}

function headingAt(leg, along) {
  if (leg.path.length === 1) return null;
  const i = segmentAt(leg, along);
  const [ax, ay] = leg.xy[i - 1];
  const [bx, by] = leg.xy[i];
  return ((Math.atan2(bx - ax, by - ay) * 180) / Math.PI + 360) % 360;
}

// ---- Engine ------------------------------------------------------------------------------

// Measure each leg once, and place its walking steps and passed stops along it.
function prepareTrip(trip) {
  const legs = trip.legs.map((leg) => {
    const path = leg.path.length ? leg.path : [leg.from_point, leg.to_point];
    const measured = measurePath(path);
    const withPath = { ...leg, path, ...measured };
    return {
      ...withPath,
      steps: (leg.steps || []).map((s) => ({ ...s, along: project(measured, s.point).along })),
      stops: (leg.stops || []).map((s) => ({ ...s, along: project(measured, s.point).along })),
    };
  });
  return { trip, legs };
}

function newGuidance(trip) {
  return { prepared: prepareTrip(trip), leg: 0, boarded: false, offCount: 0, done: false, last: null };
}

function isTransit(leg) {
  return Boolean(leg.network);
}

// Advance the guidance state with a fix and return what to show. State only moves forward:
// a noisy fix can delay the next instruction but never undo progress.
function updateGuidance(state, fix) {
  const legs = state.prepared.legs;
  const point = [fix.lon, fix.lat];
  while (!state.done) {
    const leg = legs[state.leg];
    const { along, off } = project(leg, point);
    const remaining = leg.length - along;
    const arrived = remaining <= (isTransit(leg) ? ARRIVE_RIDE_M : ARRIVE_WALK_M);
    if (isTransit(leg) && along > BOARDED_M) state.boarded = true;
    if (arrived && (!isTransit(leg) || state.boarded)) {
      state.leg += 1;
      state.boarded = false;
      state.offCount = 0;
      if (state.leg >= legs.length) state.done = true;
      continue;
    }
    const tolerance = Math.max(OFF_ROUTE_M, (fix.accuracy || 0) * 1.5);
    state.offCount = !isTransit(leg) && off > tolerance ? state.offCount + 1 : 0;
    state.last = { fix, along, off, remaining };
    return describe(state, leg, fix, along, remaining);
  }
  state.last = { fix, along: 0, off: 0, remaining: 0 };
  return { phase: "arrived", icon: "arrive", title: "You have arrived", detail: state.prepared.trip.legs.at(-1).to || "Destination", legIndex: legs.length - 1, offRoute: false };
}

function describe(state, leg, fix, along, remaining) {
  const base = { legIndex: state.leg, offRoute: state.offCount >= OFF_ROUTE_FIXES, leg };
  if (!isTransit(leg)) {
    const next = leg.steps.find((s) => s.along > along + 8);
    const target = leg.to || (state.leg === state.prepared.legs.length - 1 ? "your destination" : "the stop");
    const bike = leg.mode === "bicycle";
    const minutes = Math.max(1, Math.round(remaining / (bike ? BIKE_SPEED_MS : WALK_SPEED_MS) / 60));
    if (next && next.action !== "depart") {
      return {
        ...base, phase: "walk", icon: STEP_ICONS[next.action] || "straight",
        title: stepText(next), distance: next.along - along,
        detail: `Then ${bike ? "ride" : "walk"} to ${target} · ${minutes} min`,
      };
    }
    if (bike) return { ...base, phase: "walk", icon: "bike", title: `Ride to ${target}`, distance: remaining, detail: `${minutes} min by Bike Share` };
    return { ...base, phase: "walk", icon: "walk", title: `Walk to ${target}`, distance: remaining, detail: `${minutes} min walk` };
  }
  const route = leg.route || "";
  if (!state.boarded) {
    return {
      ...base, phase: "wait", icon: leg.mode === "rail" ? "regional" : leg.mode === "subway" ? "subway" : leg.mode === "tram" ? "streetcar" : "bus",
      title: `Board ${leg.mode === "subway" ? `Line ${route}` : route}${leg.headsign ? ` towards ${leg.headsign}` : ""}`,
      detail: `At ${leg.from}${leg.entrance ? ` · enter by ${leg.entrance.name}` : ""} · scheduled ${new Date(leg.start).toLocaleTimeString("en-CA", { timeZone: "America/Toronto", hour: "numeric", minute: "2-digit" })}`,
      departs: Date.parse(leg.start), stopId: leg.from_stop_id,
    };
  }
  const ahead = leg.stops.filter((s) => s.along > along + 30).length;
  const stopsLeft = ahead + 1;
  return {
    ...base, phase: "ride", icon: leg.mode === "rail" ? "regional" : leg.mode === "subway" ? "subway" : leg.mode === "tram" ? "streetcar" : "bus",
    title: stopsLeft === 1 ? `Get off at the next stop: ${leg.to}` : `Get off at ${leg.to}`,
    detail: `${leg.mode === "subway" ? `Line ${route}` : route} · ${stopsLeft === 1 ? "next stop" : `${stopsLeft} stops`}${leg.exit ? ` · exit by ${leg.exit.name}` : ""}`,
    stopsLeft, distance: remaining,
  };
}

const STEP_VERBS = {
  depart: "Head out", continue: "Continue", left: "Turn left", hard_left: "Turn sharp left", right: "Turn right",
  hard_right: "Turn sharp right", slightly_left: "Bear left", slightly_right: "Bear right", uturn_left: "Make a U-turn",
  uturn_right: "Make a U-turn", circle_clockwise: "Take the roundabout", circle_counterclockwise: "Take the roundabout",
  elevator: "Take the elevator", enter_station: "Enter the station", exit_station: "Leave the station", follow_signs: "Follow the signs",
};

function stepText(step) {
  const verb = STEP_VERBS[step.action] || "Continue";
  if (!step.street) return verb;
  return `${verb} ${["continue", "follow_signs"].includes(step.action) ? "on" : "onto"} ${step.street}`;
}

function distanceText(m) {
  if (m < 15) return "Now";
  if (m < 1000) return `${Math.round(m / 10) * 10} m`;
  return `${(m / 1000).toFixed(1)} km`;
}
