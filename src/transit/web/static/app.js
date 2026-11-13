"use strict";
// Plain DOM rendering. All API text goes through textContent, never innerHTML.

const REFRESH_MS = 20000;
const ROW = 44; // Must match --row in app.css.
const OVERLAP_ROWS = 0.45; // Trains closer than this on the diagram are nudged apart.
const main = document.getElementById("main");
const updated = document.getElementById("updated");
const timeFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto", hour: "numeric", minute: "2-digit",
});
const dayFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Toronto", weekday: "short", month: "short", day: "numeric",
  hour: "numeric", minute: "2-digit",
});
const STATUS_ICONS = {
  normal: "✓", delays: "!", modified_service: "!", reduced_service: "!",
  no_service: "✕", detected: "?", unknown: "?",
};
// Long TTC notices fold away so the status page stays scannable.
const DESCRIPTION_SHOWN = 160;
const NOTICES_SHOWN = 2;
let lines = [];
let stationLines = new Map();
let renderSeq = 0;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style") Object.assign(node.style, value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// The oldest stored copy (sw.js) a page was built from, when the network failed.
let savedCopyAt = null;

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const saved = response.headers.get("X-Torontrack-Saved-At");
  if (saved && (!savedCopyAt || saved < savedCopyAt)) savedCopyAt = saved;
  if (response.status === 404) throw new Error("Not found");
  if (!response.ok) throw new Error(`The data service returned ${response.status}`);
  return response.json();
}

function badge(line, { link = true, small = false } = {}) {
  const meta = lines.find((l) => l.id === line) || {};
  const attrs = {
    class: small ? "badge mini" : "badge",
    style: { background: meta.color || "#777", color: meta.text_color || "#fff" },
    "aria-label": `Line ${line}`,
  };
  return link ? el("a", { ...attrs, href: `#/line/${line}` }, line) : el("span", attrs, line);
}

function statusPill(status, label) {
  return el(
    "span",
    { class: `pill ${status}` },
    el("span", { class: "icon", "aria-hidden": "true" }, STATUS_ICONS[status] || "?"),
    label,
  );
}

function staleNotice(freshness, what) {
  if (!freshness || !freshness.stale) return null;
  // "(last update 12:00 p.m.)": the time format ends in a full stop of its own.
  const when = freshness.as_of ? ` (last update ${timeFmt.format(new Date(freshness.as_of))})` : "";
  return el("p", { class: "notice", role: "status" }, `${what} may be out of date${when}.`);
}

function minutesText(seconds) {
  if (seconds < 60) return "now";
  return `${Math.round(seconds / 60)} min`;
}

function periodText(periods) {
  return periods
    .map((p) => {
      const start = p.start ? dayFmt.format(new Date(p.start)) : "now";
      const end = p.end ? dayFmt.format(new Date(p.end)) : "until further notice";
      return `${start} – ${end}`;
    })
    .join("; ");
}

function alertCard(alert) {
  return el(
    "div",
    { class: "card alert" },
    el("p", { class: "alert-head" }, alert.lines.map((l) => badge(l, { small: true })), el("span", { class: "source" }, "Reported by TTC")),
    el("p", {}, alert.header),
    alert.description
      ? alert.description.length > DESCRIPTION_SHOWN
        ? el("details", { "data-key": `alert-${alert.id}` }, el("summary", { class: "small" }, "Details"), el("p", { class: "muted" }, alert.description))
        : el("p", { class: "muted" }, alert.description)
      : null,
    alert.advance_notice
      ? el("p", { class: "muted" }, "Advance notice: dates are in the message above.")
      : alert.timing === "upcoming" && alert.periods.length
        ? el("p", { class: "muted" }, `When: ${periodText(alert.periods)}`)
        : null,
  );
}

// Reported (TTC alerts) and detected (our inference) are always labelled separately.
function statusDetails(status) {
  const detected = status.detected;
  return [
    el("p", { class: "status-line" }, statusPill(status.status, status.label), el("span", { class: "source" }, "Reported by TTC")),
    status.summary ? el("p", { class: "muted" }, status.summary) : null,
    detected
      ? el(
          "div",
          { class: "detected" },
          el("p", { class: "status-line" }, statusPill("detected", detected.label)),
          detected.incidents.map((i) =>
            el(
              "p",
              {},
              el("a", { href: `#/station/${i.station_key}` }, i.message),
              el("span", { class: "muted" }, ` · usually every ${minutesText(i.scheduled_headway_seconds)}`),
            ),
          ),
          el("p", { class: "source" }, "Detected by this site from missing train predictions"),
        )
      : null,
  ];
}

function statusRow(status, hours) {
  return el(
    "li",
    { class: "status-row board-row" },
    badge(status.line, { link: false }),
    el("div", {}, el("a", { class: "name", href: `#/line/${status.line}` }, status.name), statusDetails(status),
      hours && hours.length ? el("div", { class: "heat-strip mini", "aria-label": "Reliability by hour today" }, hourCells(hours)) : null),
    el("span", { class: "chevron", "aria-hidden": "true" }, "›"),
  );
}

// Match "bloor yonge" to "Bloor-Yonge" and "queens park" to "Queen's Park".
function searchKey(text) {
  return text.toLowerCase().replace(/['’]/g, "").replace(/[^a-z0-9]+/g, " ").trim();
}

function stationSearch(stations) {
  const search = el("input", {
    type: "search", id: "station-search", autocomplete: "off", enterkeyhint: "go",
    placeholder: "e.g. Union, Bloor-Yonge", "aria-controls": "station-results",
    "aria-describedby": "station-count",
  });
  const count = el("p", { class: "sr-only", id: "station-count", "aria-live": "polite" });
  const results = el("ul", { class: "results", id: "station-results" });
  let matches = [];
  search.addEventListener("input", () => {
    const query = searchKey(search.value);
    matches = query ? stations.filter((s) => searchKey(s.name).includes(query)).slice(0, 8) : [];
    count.textContent = query ? `${matches.length} station${matches.length === 1 ? "" : "s"} found` : "";
    results.replaceChildren(
      ...matches.map((s) =>
        el("li", {}, el("a", { href: `#/station/${s.key}` }, el("span", {}, s.name), el("span", { class: "badges" }, s.lines.map((l) => badge(l, { link: false, small: true }))))),
      ),
    );
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && matches.length) location.hash = `#/station/${matches[0].key}`;
  });
  return [el("label", { for: "station-search" }, "Find a station"), search, count, results];
}

function networkSummary(lineData, alertData) {
  const statuses = lineData.lines.map((l) => l.status);
  // A line whose status TTC does not report is not counted as affected; it is named below.
  const unknown = statuses.filter((s) => s.status === "unknown" && !s.detected).length;
  const affected = statuses.filter((s) => !["normal", "unknown"].includes(s.status) || s.detected);
  const ok = !affected.length;
  return el(
    "section",
    { class: `summary ${ok ? "ok" : "issues"}` },
    el(
      "h1",
      {},
      el("span", { class: "state-icon", "aria-hidden": "true" }, ok ? "✓" : "!"),
      ok
        ? unknown ? "Subway: no problems reported" : "Subway: all lines running normally"
        : `Subway: ${affected.length} of ${statuses.length} lines affected`,
    ),
    el(
      "p",
      { class: "muted" },
      `${plural(alertData.active.length, "active alert", "active alerts")} · ${plural(alertData.accessibility.length, "elevator or escalator", "elevators or escalators")} out of service`
        + (unknown ? ` · ${plural(unknown, "line", "lines")} without a current status` : ""),
    ),
  );
}

// Active alerts, less those already shown word for word as a line's status above.
function activeAlerts(active, lines) {
  if (!active.length) return el("p", { class: "muted" }, "No active subway or light rail alerts.");
  const shown = new Set(lines.map((l) => l.status.summary).filter(Boolean));
  const rest = active.filter((a) => !shown.has(a.header));
  return rest.length
    ? rest.map(alertCard)
    : el("p", { class: "muted" }, active.length === 1 ? "The active alert is shown with its line above." : "The active alerts are shown with their lines above.");
}

// Saved stations, stops and lines, kept only in this browser: no account, nothing sent.
const SAVED_KEY = "torontrack-saved";
const SAVED_MAX = 12;

function savedItems() {
  try {
    const items = JSON.parse(localStorage.getItem(SAVED_KEY) || "[]");
    return Array.isArray(items) ? items.filter((i) => i && ["station", "stop", "line", "street"].includes(i.kind) && i.id) : [];
  } catch {
    return [];
  }
}

function setSavedItems(items) {
  try {
    localStorage.setItem(SAVED_KEY, JSON.stringify(items.slice(0, SAVED_MAX)));
  } catch {
    /* Storage off (private window): saving silently does nothing. */
  }
}

const isSaved = (kind, id) => savedItems().some((i) => i.kind === kind && i.id === String(id));

// "Save" / "Saved" toggle for a station, stop or line page or panel.
function saveButton(kind, id, name) {
  const button = el("button", { type: "button", class: "save-button" });
  const show = () => {
    const saved = isSaved(kind, id);
    button.setAttribute("aria-pressed", String(saved));
    button.textContent = saved ? "Saved ✓" : "Save";
    button.title = saved ? "Remove from the Status page" : "Show on the Status page";
  };
  button.addEventListener("click", () => {
    const rest = savedItems().filter((i) => !(i.kind === kind && i.id === String(id)));
    setSavedItems(isSaved(kind, id) ? rest : [...rest, { kind, id: String(id), name }]);
    show();
  });
  show();
  return button;
}

const titleRow = (heading, kind, id, name) => el("div", { class: "title-row" }, heading, saveButton(kind, id, name));

// The next two trains each way at a saved station.
async function savedStation(item) {
  const data = await api(`/api/stations/${encodeURIComponent(item.id)}`);
  return [
    el("a", { class: "name", href: `#/station/${item.id}` }, data.station.name),
    data.station.directions.map((d) =>
      el(
        "p",
        { class: "saved-times" },
        badge(d.line, { link: false, small: true }),
        el("span", { class: "muted" }, ` to ${d.towards} `),
        d.arrivals.length ? d.arrivals.slice(0, 2).map((a) => (a.minutes === 0 ? "Due" : `${a.minutes} min`)).join(", ") : el("span", { class: "muted" }, "none listed"),
      ),
    ),
  ];
}

// A saved street: its next collection, road work nearby and the ward's top 311 request.
async function savedStreet(item) {
  const s = await api(`/api/street?at=${encodeURIComponent(item.id)}`);
  const next = s.collection?.upcoming?.[0];
  const top = s.requests?.top?.[0];
  return [
    el("span", { class: "name" }, item.name),
    el("p", { class: "saved-times" }, next ? `Next collection ${wasteDay(next.collected_on)}: ${next.items.join(", ")}` : s.collection ? "Night collection area" : "No curbside collection listed"),
    s.road_work.length ? el("p", { class: "saved-times" }, `${plural(s.road_work.length, "road work", "road works")} within 500 m`) : null,
    top ? el("p", { class: "muted saved-times" }, `Ward ${s.ward.number}, ${s.ward.name}: most 311 requests are ${top.type.toLowerCase()} (${top.requests} in ${s.requests.days} days)`) : null,
  ];
}

// The next three vehicles at a saved stop.
async function savedStop(item) {
  const data = await api(`/api/stops/${encodeURIComponent(item.id)}`);
  return [
    el("a", { class: "name", href: `#/stop/${encodeURIComponent(item.id)}` }, data.stop.name),
    data.arrivals.length
      ? el("p", { class: "saved-times" }, data.arrivals.slice(0, 3).map((a, k) => [k ? " · " : "", el("span", { class: `route-pill ${a.mode || "bus"}` }, a.short_name), ` ${a.seconds < 60 ? "Due" : `${Math.round(a.seconds / 60)} min`}`]))
      : el("p", { class: "muted saved-times" }, "No vehicles in the next hour."),
  ];
}

async function savedRow(item, lines) {
  try {
    if (item.kind === "line") {
      const line = lines.find((l) => l.status.line === item.id);
      if (!line) return null;
      return el("li", { class: "saved-row" }, el("div", {}, el("a", { class: "name", href: `#/line/${item.id}` }, line.status.name), el("p", { class: "status-line" }, statusPill(line.status.status, line.status.label))));
    }
    return el("li", { class: "saved-row" }, el("div", {}, await (item.kind === "station" ? savedStation(item) : item.kind === "street" ? savedStreet(item) : savedStop(item))));
  } catch {
    return el("li", { class: "saved-row" }, el("div", {}, el("span", { class: "name" }, item.name || item.id), el("p", { class: "muted" }, "Not available right now.")));
  }
}

async function savedSection(lineData) {
  const items = savedItems();
  if (!items.length) return null;
  const rows = await Promise.all(items.map((i) => savedRow(i, lineData.lines)));
  return [el("h2", {}, "Saved"), el("ul", { class: "status-list saved-list" }, rows)];
}

// Air quality on the Status page: Downtown's AQHI now and the highest forecast for the
// rest of today and for tomorrow, in ECCC's risk categories.
const aqhiHourFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", hour: "numeric" });
const AQHI_CLASS = { Low: "ok", Moderate: "warn", High: "bad", "Very high": "bad" };

function airLine(air) {
  if (!air || !air.stations.length) return null;
  const main = air.stations[0];
  const today = torontoDay(new Date());
  const peak = (items) => items.reduce((a, b) => (!a || b.aqhi > a.aqhi ? b : a), null);
  const rest = peak(air.forecast.filter((f) => torontoDay(new Date(f.at)) === today));
  const tomorrow = peak(air.forecast.filter((f) => torontoDay(new Date(f.at)) > today));
  const level = (x) => `${Math.round(x.aqhi)} ${x.risk.toLowerCase()}`;
  return el("details", { class: "card air", "data-key": "home-air" },
    el("summary", {}, "Air quality: ", el("span", { class: `tag ${AQHI_CLASS[main.risk] || ""}` }, `${Math.round(main.aqhi)} · ${main.risk} risk`),
      el("span", { class: "muted small" }, ` ${main.name}, ${aqhiHourFmt.format(new Date(main.observed_at))}`)),
    el("p", { class: "small" }, [rest ? `Later today up to ${level(rest)}` : null, tomorrow ? `tomorrow up to ${level(tomorrow)}` : null].filter(Boolean).join("; ") || "No forecast published."),
    el("ul", { class: "plain small" }, air.stations.slice(1).map((s) => el("li", {}, `${s.name}: ${Math.round(s.aqhi)} (${s.risk.toLowerCase()})`))),
    el("p", { class: "muted small" }, "Air Quality Health Index from 1 (low risk) to 10 and above (very high), Environment and Climate Change Canada."));
}

async function renderHome() {
  const [lineData, alertData, airData, rel] = await Promise.all([api("/api/lines"), api("/api/alerts"), api("/api/air").catch(() => null), api("/api/reliability").catch(() => null)]);
  const hourly = rel && rel.available ? rel.hourly : {};
  const accessibility = alertData.accessibility;
  const saved = await savedSection(lineData);
  return [
    networkSummary(lineData, alertData),
    staleNotice(alertData.alerts_as_of, "TTC alerts"),
    airData ? airLine(airData.air) : null,
    saved,
    el("h2", {}, "Lines"),
    el("ul", { class: "status-list" }, lineData.lines.map((l) => statusRow(l.status, hourly[l.status.line]))),
    Object.keys(hourly).length ? el("p", { class: "muted small" }, "Squares: today's service hour by hour, green when trains came as scheduled. ", el("a", { href: "#/reliability" }, "Reliability")) : null,
    stationSearch(lineData.stations),
    el("h2", {}, "Active alerts"),
    activeAlerts(alertData.active, lineData.lines),
    el("h2", {}, "Planned closures and notices"),
    alertData.upcoming.length
      ? [
          alertData.upcoming.slice(0, NOTICES_SHOWN).map(alertCard),
          alertData.upcoming.length > NOTICES_SHOWN
            ? el("details", { class: "more", "data-key": "more-notices" }, el("summary", {}, `Show ${alertData.upcoming.length - NOTICES_SHOWN} more`), alertData.upcoming.slice(NOTICES_SHOWN).map(alertCard))
            : null,
        ]
      : el("p", { class: "muted" }, "None announced."),
    el("h2", {}, "Elevators and escalators"),
    accessibility.length
      ? el(
          "details",
          { class: "card", "data-key": "home-accessibility" },
          el("summary", {}, `${accessibility.length} out of service`),
          el("ul", { class: "plain" }, accessibility.map((a) => el("li", {}, a.header))),
        )
      : el("p", { class: "muted" }, "No outages reported."),
  ];
}

// Spread trains that would overlap on the diagram sideways, away from the line.
function trainOffsets(trains) {
  const offsets = new Map();
  for (const direction of [0, 1]) {
    let previous = null;
    let shift = 0;
    for (const train of trains.filter((t) => t.direction_id === direction).sort((a, b) => a.position - b.position)) {
      shift = previous !== null && train.position - previous < OVERLAP_ROWS ? shift + 1 : 0;
      offsets.set(train, shift);
      previous = train.position;
    }
  }
  return offsets;
}

function lineDiagram(data) {
  const lineId = data.line.id;
  const last = data.stations.length - 1;
  const strip = el(
    "div",
    { class: "strip", style: { height: `${data.stations.length * ROW}px` } },
    el("div", { class: "bar", "aria-hidden": "true", style: { background: data.line.color || "#777" } }),
    el(
      "ol",
      { "aria-label": `Stations on ${data.line.name}` },
      data.stations.map((s, i) => {
        const transfers = (stationLines.get(s.key) || []).filter((l) => l !== lineId);
        return el(
          "li",
          { class: i === 0 || i === last ? "terminal" : null },
          el("a", { href: `#/station/${s.key}` }, s.name, transfers.length ? el("span", { class: "badges" }, transfers.map((l) => badge(l, { link: false, small: true }))) : null),
        );
      }),
    ),
  );
  for (const gap of data.gaps.filter((g) => g.long)) {
    const top = Math.min(gap.from_position, gap.to_position) * ROW + ROW / 2;
    const size = Math.abs(gap.to_position - gap.from_position) * ROW;
    strip.append(el("div", { class: `gap d${gap.direction_id}`, "aria-hidden": "true", style: { top: `${top}px`, height: `${Math.max(size, 24)}px` } }, `${Math.round(gap.seconds / 60)} min`));
  }
  const offsets = trainOffsets(data.trains);
  for (const train of data.trains) {
    const outward = (offsets.get(train) || 0) * 12 * (train.direction_id === 0 ? -1 : 1);
    strip.append(el("div", {
      class: `train d${train.direction_id}${train.at_station ? " at" : ""}`,
      "aria-hidden": "true",
      "data-move": `${train.train_id}-${train.direction_id}`,
      style: { top: `${train.position * ROW + ROW / 2}px`, translate: `${outward}px 0` },
      title: `Train ${train.train_id}: ${train.at_station ? "at" : "approaching"} ${train.station}`,
    }));
  }
  return strip;
}

const TIMETABLE_NOTE = "TTC publishes no live data for this line, so trains are placed by timetable.";

function directionSummary(data, dir) {
  const trains = data.trains.filter((t) => t.direction_id === dir.direction_id);
  const longGaps = data.gaps.filter((g) => g.long && g.direction_id === dir.direction_id);
  const headway = data.scheduled_headway_seconds[dir.direction_id];
  return el(
    "section",
    { class: "card" },
    el("h2", {}, `Towards ${dir.towards}`),
    el("p", { class: "muted" }, `${trains.length} trains ${data.line.timetable ? "scheduled now" : "in the feed"}`, headway ? ` · scheduled about every ${minutesText(headway)}` : ""),
    longGaps.length
      ? el("p", {}, statusPill("detected", `${longGaps.length} unusually long gap${longGaps.length > 1 ? "s" : ""}`), ` Longest ${Math.round(Math.max(...longGaps.map((g) => g.seconds)) / 60)} min between trains.`)
      : el("p", { class: "muted" }, "No unusually long gaps between trains."),
    el(
      "details",
      { "data-key": `trains-${dir.direction_id}` },
      el("summary", {}, "Train list"),
      el("ul", { class: "plain" }, trains.map((t) => el("li", {}, t.at_station ? `At ${t.station}` : `Approaching ${t.station} (${minutesText(t.eta_seconds)})`))),
    ),
  );
}

async function renderLine(id) {
  const data = await api(`/api/lines/${encodeURIComponent(id)}`);
  const [dir0, dir1] = [0, 1].map((d) => data.line.directions.find((x) => x.direction_id === d));
  return [
    titleRow(el("h1", { class: "with-badge" }, badge(data.line.id, { link: false }), data.line.name), "line", data.line.id, data.line.name),
    el("div", { class: "card" }, statusDetails(data.status)),
    data.line.timetable ? el("p", { class: "notice", role: "status" }, TIMETABLE_NOTE) : staleNotice(data.predictions_as_of, "Train predictions"),
    el(
      "div",
      { class: "diagram" },
      el("div", { class: "legend", "aria-hidden": "true" }, el("span", {}, dir0 ? `↓ to ${dir0.towards}` : ""), el("span", {}, dir1 ? `↑ to ${dir1.towards}` : "")),
      lineDiagram(data),
      el(
        "ul",
        { class: "key", "aria-hidden": "true" },
        el("li", {}, el("span", { class: "train d0 sample" }), "Train between stations"),
        el("li", {}, el("span", { class: "train at sample" }), "Train at a station"),
        el("li", {}, el("span", { class: "gap sample" }), "Gap well above the schedule"),
      ),
      el("p", { class: "muted small" }, data.line.timetable ? "Positions follow the timetable. Train lists below give the same information as text." : "Positions are estimated from predicted arrival times. Train lists below give the same information as text."),
    ),
    [dir0, dir1].filter(Boolean).map((dir) => directionSummary(data, dir)),
  ];
}

function scheduledLine(id) {
  return Boolean((lines.find((l) => l.id === id) || {}).timetable);
}

function arrivalsCard(d) {
  return el(
    "section",
    { class: "card" },
    el("h3", { class: "with-badge" }, badge(d.line, { link: false, small: true }), el("span", {}, d.platform, d.platform.includes(d.towards) ? null : el("span", { class: "muted" }, ` to ${d.towards}`))),
    d.arrivals.length
      ? el(
          "ul",
          { class: "arrivals" },
          d.arrivals.map((a, i) => el("li", {}, arrivalMins(a.minutes, null, `${d.line}-${d.platform}-${i}`), el("span", { class: "muted" }, timeFmt.format(new Date(a.arrival)), a.scheduled ? " scheduled" : ""))),
        )
      : el("p", { class: "muted" }, scheduledLine(d.line) ? "No scheduled trains right now." : "No predictions right now."),
    scheduledLine(d.line) ? el("p", { class: "muted small" }, "Timetable times: TTC publishes no live data for this line.") : null,
  );
}

function bikeCard(d) {
  return el(
    "li",
    { class: "card bike" },
    el("strong", {}, d.name),
    el(
      "p",
      { class: "bike-stats" },
      el("span", {}, el("b", {}, d.bikes), d.bikes === 1 ? " bike" : " bikes"),
      el("span", {}, el("b", {}, d.docks), d.docks === 1 ? " open dock" : " open docks"),
      el("span", { class: "muted" }, `${d.distance_m} m away`),
      d.renting ? null : el("span", { class: "warn" }, "Not renting"),
    ),
  );
}

async function renderStation(key) {
  const data = await api(`/api/stations/${encodeURIComponent(key)}`);
  const station = data.station;
  const bikes = data.bike_share;
  return [
    titleRow(el("h1", { class: "with-badge" }, station.name, station.lines.map((l) => badge(l))), "station", station.key, station.name),
    data.line_status.map((s) => el("div", { class: "card status-row" }, badge(s.line, { link: false }), el("div", {}, statusDetails(s)))),
    staleNotice(data.predictions_as_of, "Train predictions"),
    el("h2", {}, "Next trains"),
    el("div", { class: "grid" }, station.directions.map(arrivalsCard)),
    el("h2", {}, "Alerts at this station"),
    data.alerts.length ? data.alerts.map(alertCard) : el("p", { class: "muted" }, "No alerts for this station, including elevators and escalators."),
    el("h2", { class: "with-link" }, "Nearest Bike Share", el("a", { href: `#/map/${station.key}`, class: "small" }, "View on map")),
    staleNotice(bikes, "Bike Share availability"),
    bikes.docks.length ? el("ul", { class: "grid bare" }, bikes.docks.map(bikeCard)) : el("p", { class: "muted" }, "No Bike Share data yet."),
  ];
}

function hourLabel(hour) {
  const h = hour % 24;
  const label = `${h % 12 || 12} ${h < 12 ? "a.m." : "p.m."}`;
  return hour >= 24 ? `${label} (late)` : label;
}

function meter(pct) {
  if (pct === null || pct === undefined) return el("span", { class: "muted" }, "–");
  return el(
    "span",
    { class: "meter", role: "img", "aria-label": `${pct}%` },
    el("span", { class: "fill", style: { width: `${pct}%` } }),
    el("span", { class: "value", "aria-hidden": "true" }, `${pct}%`),
  );
}

function durationText(minutes) {
  const h = Math.floor(minutes / 60);
  return h ? `${h} h ${minutes % 60} min` : `${minutes} min`;
}

function lineCard(line, body) {
  return el("section", { class: "card" }, el("h3", { class: "with-badge" }, badge(line.id, { small: true }), line.name), body);
}

function gapList(gaps) {
  if (!gaps.length) return el("p", { class: "muted" }, "No delays measured today.");
  return el(
    "ol",
    { class: "gaps" },
    gaps.map((g) =>
      el(
        "li",
        {},
        el("strong", { class: "mins" }, `+${Math.round(g.excess_seconds / 60)} min`),
        el(
          "span",
          {},
          `${Math.round(g.gap_seconds / 60)} min gap at `,
          el("a", { href: `#/station/${g.station_key}` }, g.station_name),
          g.both_directions ? " in both directions" : ` to ${g.towards}`,
          el("span", { class: "muted" }, ` · ${timeFmt.format(new Date(g.start))}–${timeFmt.format(new Date(g.end))} · usually every ${minutesText(g.scheduled_headway_seconds)}`, g.trains_delayed > 1 ? ` · ${g.trains_delayed} trains delayed` : ""),
        ),
      ),
    ),
  );
}

// Share of regular gaps by hour of service, as coloured cells: green when trains came as
// scheduled, through amber, to red when many gaps were long. Hours not yet run are blank.
const HOURS_SHOWN = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25];
const hourName = (h) => { const x = h % 24; return x === 0 ? "midnight" : x === 12 ? "noon" : x < 12 ? `${x} a.m.` : `${x - 12} p.m.`; };

function reliabilityColour(pct) {
  if (pct == null) return null;
  return pct >= 90 ? "var(--rel-1)" : pct >= 80 ? "var(--rel-2)" : pct >= 65 ? "var(--rel-3)" : pct >= 50 ? "var(--rel-4)" : "var(--rel-5)";
}

function hourCells(hours, { week = false } = {}) {
  const by = new Map(hours.map((h) => [h.hour, h]));
  return HOURS_SHOWN.map((hour, k) => {
    const h = by.get(hour) || by.get(hour % 24);
    const pct = h ? (week ? h.week_regular_pct : h.today_headways ? h.today_regular_pct : null) : null;
    const colour = reliabilityColour(pct);
    return el("span", { class: `heat-cell${colour ? "" : " empty"}`, style: { "--k": k, background: colour || "" },
      title: pct == null ? `${hourName(hour)}: no trains measured` : `${hourName(hour)}: ${pct}% of gaps regular${h.today_delays ? `, ${h.today_delays} long gaps` : ""}` });
  });
}

function reliabilityHeatmap(hourly) {
  return el("section", { class: "card heatmap", "aria-label": "Reliability by hour today" },
    lines.filter((line) => (hourly[line.id] || []).length).map((line) => el("div", { class: "heatmap-row" },
      badge(line.id, { link: false, small: true }),
      el("div", { class: "heat-strip" }, hourCells(hourly[line.id])))),
    el("div", { class: "heatmap-axis", "aria-hidden": "true" }, el("span"), el("div", { class: "heat-axis" },
      ["5 a.m.", "9 a.m.", "1 p.m.", "5 p.m.", "9 p.m.", "1 a.m."].map((t) => el("span", {}, t)))),
    el("div", { class: "heat-legend" }, el("span", { class: "muted small" }, "Gaps regular:"),
      [["var(--rel-1)", "90%+"], ["var(--rel-2)", "80%"], ["var(--rel-3)", "65%"], ["var(--rel-4)", "50%"], ["var(--rel-5)", "less"]].map(([c, t]) =>
        el("span", { class: "heat-key small" }, el("span", { class: "heat-cell", style: { background: c } }), t))));
}

function hourTable(hours) {
  if (!hours.length) return el("p", { class: "muted" }, "No data yet.");
  return el(
    "table",
    { class: "hours" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Hour"), el("th", { scope: "col" }, "Today"), el("th", { scope: "col" }, "Last 7 days"), el("th", { scope: "col" }, "Delays today"))),
    el(
      "tbody",
      {},
      hours.map((h) =>
        el("tr", {}, el("th", { scope: "row" }, hourLabel(h.hour)), el("td", {}, meter(h.today_regular_pct)), el("td", {}, meter(h.week_regular_pct)), el("td", {}, h.today_delays)),
      ),
    ),
  );
}

function outageItem(o) {
  return el(
    "li",
    {},
    el("a", { href: `#/station/${o.station_key}` }, o.station_name),
    el("span", { class: "muted" }, ` ${o.device_type}`),
    el("span", { class: "mins" }, o.began_before_collection ? `≥ ${durationText(o.duration_minutes)}` : durationText(o.duration_minutes)),
  );
}

const OUTAGES_SHOWN = 6;


// Open-data downloads listed from /api/datasets (the export's index.json).
const fileDayFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "UTC", weekday: "short", month: "short", day: "numeric", year: "numeric",
});

function sizeText(bytes) {
  return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1e3))} KB`;
}

const fileMonthFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", month: "long", year: "numeric" });
const fileRangeFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", month: "short", day: "numeric", year: "numeric" });

function downloadRow(f) {
  return el(
    "li",
    {},
    el("span", {}, f.day ? fileDayFmt.format(new Date(`${f.day}T00:00:00Z`)) : "All to date", f.partial ? " (partial day)" : ""),
    el("span", { class: "muted small" }, `${numberFmt.format(f.rows)} rows`),
    el(
      "span",
      { class: "links" },
      el("a", { href: `/data/${f.files.parquet.path}`, download: "" }, `Parquet, ${sizeText(f.files.parquet.bytes)}`),
      el("a", { href: `/data/${f.files.csv.path}`, download: "" }, `CSV, ${sizeText(f.files.csv.bytes)}`),
    ),
  );
}

// "22 days, 9 Oct to 30 Oct 2026 · 1,034,215 rows" for a dataset of daily files.
function coverageText(files) {
  const days = files.filter((f) => f.day).map((f) => f.day).sort();
  const rows = numberFmt.format(files.reduce((sum, f) => sum + f.rows, 0));
  if (!days.length) return `${rows} rows`;
  const at = (day) => fileRangeFmt.format(new Date(`${day}T00:00:00Z`));
  return `${plural(days.length, "day", "days")}, ${at(days[0])} to ${at(days[days.length - 1])} · ${rows} rows`;
}

// Files older than the first week, one collapsed list per month, newest month first, so
// the list stays short however long collection runs.
function earlierFiles(name, files) {
  const months = new Map();
  for (const f of files) {
    const key = f.day.slice(0, 7);
    if (!months.has(key)) months.set(key, []);
    months.get(key).push(f);
  }
  return [...months].map(([key, list]) =>
    el(
      "details",
      { class: "month", "data-key": `files-${name}-${key}` },
      el("summary", {}, `${fileMonthFmt.format(new Date(`${key}-01T00:00:00Z`))} · ${plural(list.length, "file", "files")}`),
      el("ul", { class: "downloads" }, list.map(downloadRow)),
    ),
  );
}

function downloadSection(sets) {
  if (!sets.available) return null;
  const shown = 7;
  return [
    el("h2", { id: "download" }, "Download the data"),
    el(
      "p",
      { class: "muted" },
      "Daily files from this project's own collection, free to reuse with attribution. ",
      "Parquet for analysis tools, gzipped CSV for spreadsheets.",
    ),
    sets.datasets
      .filter((d) => d.files.length)
      .map((d) =>
        el(
          "section",
          { class: "dataset" },
          el("h3", {}, d.title),
          el("p", { class: "dataset-coverage" }, coverageText(d.files)),
          el("p", { class: "muted small" }, d.description, d.day ? ` One file per day (${d.day}).` : ""),
          el("ul", { class: "downloads" }, d.files.slice(0, shown).map(downloadRow)),
          d.files.length > shown ? earlierFiles(d.name, d.files.slice(shown)) : null,
          el(
            "details",
            { "data-key": `columns-${d.name}` },
            el("summary", {}, "Columns"),
            el("dl", { class: "facts columns" }, Object.entries(d.columns).map(([name, text]) => [el("dt", {}, el("code", {}, name)), el("dd", {}, text)])),
          ),
        ),
      ),
    el(
      "p",
      { class: "muted small" },
      sets.licence,
      " ",
      el("a", { href: sets.licence_url }, "Licence terms"),
      ". Row counts and SHA-256 checksums for every file are in ",
      el("a", { href: "/data/index.json" }, "index.json"),
      ".",
    ),
  ];
}

// One Reliability tab with two views: today's measurements and the long-term report.
function reliabilityHeader(current) {
  const views = [["today", "#/reliability", "Today"], ["report", "#/reliability/report", "Long-term report"], ["card", "#/reliability/card", "Report card"]];
  return [
    el("h1", {}, "Reliability"),
    el("nav", { class: "chip-group view-switch", "aria-label": "Reliability views" },
      views.map(([key, href, label]) => el("a", { class: "chip small", href, "aria-current": key === current ? "page" : null }, label))),
  ];
}

async function renderReliability() {
  const [data, sets] = await Promise.all([api("/api/reliability"), api("/api/datasets")]);
  if (!data.available) {
    return [
      ...reliabilityHeader("today"),
      el("p", {}, "Reliability analytics have not been built yet. They appear after the first scheduled transformation."),
      downloadSection(sets),
    ];
  }
  const coverage = data.coverage;
  const outages = data.outages;
  const resolvedText =
    outages.median_resolved_minutes === null
      ? ""
      : ` (median ${durationText(outages.median_resolved_minutes)}, longest ${durationText(outages.longest_resolved_minutes)})`;
  return [
    ...reliabilityHeader("today"),
    el(
      "p",
      { class: "muted" },
      `Measured from ${coverage.headways.toLocaleString("en-CA")} gaps between observed train arrivals since ${coverage.first_day}. History starts when collection started. `,
      data.analytics_built_at ? `Analytics updated ${timeFmt.format(new Date(data.analytics_built_at))}` : "",
    ),
    el("h2", {}, "Worst delays today"),
    el("p", { class: "muted small" }, "Overlapping long gaps on a line count as one delay, ranked by minutes over the scheduled gap between trains."),
    el("div", { class: "grid" }, lines.map((line) => lineCard(line, gapList(data.longest_gaps_today.filter((g) => g.route_id === line.id))))),
    el("h2", {}, "Through the day"),
    el("p", { class: "muted small" }, "Each square is an hour of service today, coloured by the share of gaps between trains within 1.5 times the scheduled headway. Hover or tap for the figure."),
    reliabilityHeatmap(data.hourly),
    el("h2", {}, "Headway reliability by hour"),
    el(
      "p",
      { class: "muted" },
      "Share of gaps between trains within 1.5 times the scheduled headway. A long gap exceeds twice the scheduled headway and the headway plus 5 minutes. Terminals are excluded.",
    ),
    el("div", { class: "grid" }, lines.map((line) => lineCard(line, hourTable(data.hourly[line.id] || [])))),
    el("h2", {}, "Elevator and escalator outages"),
    el("p", {}, `${outages.active.length} out of service now. Resolved in the last 7 days: ${outages.resolved_last_7_days}${resolvedText}.`),
    outages.active.length
      ? el(
          "div",
          { class: "card" },
          el("ul", { class: "outages" }, outages.active.slice(0, OUTAGES_SHOWN).map(outageItem)),
          outages.active.length > OUTAGES_SHOWN
            ? el(
                "details",
                { "data-key": "more-outages" },
                el("summary", {}, `Show ${outages.active.length - OUTAGES_SHOWN} more`),
                el("ul", { class: "outages" }, outages.active.slice(OUTAGES_SHOWN).map(outageItem)),
              )
            : null,
          el("p", { class: "muted small" }, "≥ means the outage was already in progress when collection started."),
        )
      : null,
    downloadSection(sets),
  ];
}

// Reliability report: every measured gap since collection began. Excess wait time (EWT)
// is the average extra wait, beyond what the schedule promises, for a rider who turns up
// at a random moment; see transit/web/report.py.
const DAY_TYPE_LABELS = { weekday: "Weekday", saturday: "Saturday", sunday: "Sunday", holiday: "Holiday" };
const shortDayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric" });
const EARLY_DAYS = 14; // Below this many days, the page says results are preliminary.

function ewtText(seconds) {
  return seconds === null || seconds === undefined ? "–" : `${seconds} s`;
}

// One bar per value, scaled to the largest in its table. The number is always printed
// beside it, so the bar is never the only way to read the value.
function ewtBar(line, row, max) {
  const label = badge(line, { link: false, small: true });
  if (!row) return el("div", { class: "ewt" }, label, el("span", { class: "muted small" }, "not measured yet"));
  const width = max > 0 ? Math.max(0, Math.min(100, (100 * row.ewt_seconds) / max)) : 0;
  return el(
    "div",
    { class: "ewt", title: `${ewtText(row.ewt_seconds)} extra wait · ${row.long_pct}% long gaps · ${numberFmt.format(row.gaps)} gaps measured` },
    label,
    el("span", { class: "track", "aria-hidden": "true" }, el("span", { class: "fill", style: { width: `${width}%` } })),
    el("span", { class: "value" }, ewtText(row.ewt_seconds)),
    el("span", { class: "sr-only" }, ` extra wait on line ${line}, ${row.long_pct}% long gaps, ${row.gaps} gaps measured`),
  );
}

// One row per group; each row lists every line with its own bar and value, so a value is
// never read against a neighbouring bar and the table fits a phone.
function ewtTable(rowKeys, labelOf, find) {
  const values = rowKeys.flatMap((k) => lines.map((l) => find(k, l.id))).filter(Boolean);
  const max = Math.max(1, ...values.map((v) => v.ewt_seconds));
  return el(
    "div",
    { class: "card" },
    el(
      "table",
      { class: "hours report" },
      el("thead", { class: "sr-only" }, el("tr", {}, el("th", { scope: "col" }, "Group"), el("th", { scope: "col" }, "Extra wait by line"))),
      el("tbody", {}, rowKeys.map((k) => el("tr", {}, el("th", { scope: "row" }, labelOf(k)), el("td", {}, lines.map((l) => ewtBar(l.id, find(k, l.id), max)))))),
    ),
  );
}

function maxHours(rows, conditions) {
  return Math.max(0, ...rows.filter((w) => w.conditions === conditions).map((w) => w.hours));
}

// Bike Share ridership from the city's monthly open data: every trip, summarized.
function bikeTripsSection(b) {
  if (!b) return null;
  const recent = b.months.slice(-12);
  const max = Math.max(...recent.map((m) => m.trips));
  const copied = b.files.filter((f) => f.end_name_copied > f.trips * 0.1);
  return [
    el("h2", {}, "Bike Share trips"),
    el("p", {}, `${numberFmt.format(b.trips)} trips from ${longDayFmt.format(new Date(`${b.first_trip}T12:00:00Z`))} to ${longDayFmt.format(new Date(`${b.last_trip}T12:00:00Z`))}, from the city's monthly ridership data (published a few weeks after each month).`),
    el(
      "div",
      { class: "card" },
      el(
        "ol",
        { class: "bars" },
        recent.map((m) => el("li", {}, el("span", { class: "bar-label" }, tripMonthFmt.format(new Date(`${m.month}T00:00:00Z`))), el("span", { class: "bar", style: { width: `${(100 * m.trips) / max}%` } }), el("span", { class: "bar-value" }, numberFmt.format(m.trips), el("span", { class: "detail muted small" }, `${m.member_pct}% members · ${m.ebike_pct}% e-bike`)))),
      ),
    ),
    el("h3", {}, "Busiest docks, last 12 months"),
    el("div", { class: "card" }, el("ol", { class: "gaps" }, b.busiest_docks.map((d) => el("li", {}, el("strong", { class: "mins" }, numberFmt.format(d.trips)), el("span", {}, d.name))))),
    el("h3", {}, `Most popular trips in ${b.routes_year}`),
    el("div", { class: "card" }, el("ol", { class: "gaps" }, b.popular_routes.map((r) => el("li", {}, el("strong", { class: "mins" }, numberFmt.format(r.trips)), el("span", {}, `${r.start_name} → ${r.end_name}`, r.typical_minutes ? el("span", { class: "detail muted small" }, `about ${r.typical_minutes} min`) : null))))),
    copied.length
      ? el("p", { class: "muted small" }, `Data note: in the ${copied.map((f) => f.year).join(" and ")} file${copied.length > 1 ? "s" : ""}, most trips list the start dock's name as the end dock's name although the dock numbers differ, so dock names here come from start records and the live feed.`)
      : null,
  ];
}

// Buses and streetcars: headways measured from live positions at stops along each route.
function surfaceSection(s) {
  if (!s || !s.headways) return null;
  return [
    el("h2", {}, "Buses and streetcars"),
    el(
      "p",
      {},
      `On routes scheduled at least every 15 minutes, ${s.bunched_pct}% of gaps between vehicles were bunched (under a quarter of the scheduled gap: vehicles arriving together) and ${s.long_pct}% were long gaps. A rider waited ${ewtText(s.ewt_seconds)} more than scheduled on average. `,
      el("span", { class: "muted" }, `${numberFmt.format(s.headways)} gaps on ${s.routes} routes over ${plural(s.days, "day", "days")}.`),
    ),
    s.days < EARLY_DAYS ? el("p", { class: "notice" }, `Early results: bus and streetcar gaps have been measured for ${plural(s.days, "day", "days")}.`) : null,
    s.routes_ranked.length
      ? [
          el("h3", {}, "Routes with the most extra wait"),
          el(
            "div",
            { class: "card" },
            el(
              "ol",
              { class: "gaps" },
              s.routes_ranked.map((r) =>
                el(
                  "li",
                  {},
                  el("strong", { class: "mins" }, ewtText(r.ewt_seconds)),
                  el(
                    "span",
                    { class: "with-badge" },
                    el("span", { class: `route-pill ${r.mode}` }, r.short_name || r.route_id),
                    r.long_name || "",
                    el("span", { class: "detail muted small" }, `${r.bunched_pct}% bunched, ${r.long_pct}% long gaps, ${numberFmt.format(r.headways)} gaps measured`),
                  ),
                ),
              ),
            ),
          ),
        ]
      : el("p", { class: "muted" }, `Routes are ranked once they have ${s.route_min_headways} measured gaps.`),
    closureComparison(s.closures),
    s.trips ? tripsSection(s.trips) : deliverySection(s.delivery),
    el("p", { class: "muted small" }, "Gaps are timed where vehicles pass stops about every 10 scheduled minutes along each route, from TTC's live positions (to within about 20 seconds), and exclude terminals, where buses queue by design."),
  ];
}

// Scheduled trips one by one: those no vehicle reported running.
function tripsSection(t) {
  const since = new Date(`${t.first_day}T12:00:00`).toLocaleDateString("en-CA", { month: "long", day: "numeric" });
  return [
    el("h3", {}, "Scheduled trips no bus or streetcar ran"),
    el(
      "p",
      {},
      `${t.days < 7 ? `Since ${since}` : "Over the last 7 days"}, ${t.unseen_pct}% of scheduled bus and streetcar trips (${numberFmt.format(t.unseen)} of ${numberFmt.format(t.scheduled)}) had no vehicle report running them, and ${t.short_pct}% more were reported for less than half their scheduled length.`,
    ),
    t.routes_ranked.length
      ? el(
          "div",
          { class: "card" },
          el(
            "ol",
            { class: "gaps" },
            t.routes_ranked.map((r) =>
              el(
                "li",
                {},
                el("strong", { class: "mins" }, `${r.unseen_pct}%`),
                el(
                  "span",
                  { class: "with-badge" },
                  el("span", { class: `route-pill ${r.mode}` }, r.short_name || r.route_id),
                  r.long_name || "",
                  el("span", { class: "detail muted small" }, `${numberFmt.format(r.unseen)} of ${numberFmt.format(r.scheduled)} scheduled trips not seen`),
                ),
              ),
            ),
          ),
        )
      : null,
    el("p", { class: "muted small" }, `Each trip in TTC's timetable is checked against the trip IDs vehicles report on the live feed. A trip not seen was most likely not run, but a vehicle with its tracker off, or running under another trip ID, looks the same. Routes are ranked once they have ${t.route_min_trips} scheduled trips measured.`),
  ];
}

// Scheduled service that never appeared on the live feed, with the limits of that reading.
function deliverySection(d) {
  if (!d) return null;
  const route = (r) => [el("span", { class: `route-pill ${r.mode}` }, r.short_name || r.route_id), r.long_name || ""];
  return [
    el("h3", {}, "Scheduled service not seen"),
    el(
      "p",
      {},
      `Over the last ${plural(d.days, "day", "days")}, about ${d.missing_pct}% of scheduled bus and streetcar service did not appear on the live feed, and ${plural(d.empty_hours, "hour", "hours")} on a route and direction had vehicles scheduled and none seen.`,
    ),
    d.routes_ranked.length
      ? el(
          "div",
          { class: "card" },
          el(
            "ol",
            { class: "gaps" },
            d.routes_ranked.map((r) =>
              el(
                "li",
                {},
                el("strong", { class: "mins" }, `${r.seen_pct}%`),
                el(
                  "span",
                  { class: "with-badge" },
                  ...route(r),
                  el("span", { class: "detail muted small" }, `seen of scheduled, about ${plural(r.missing_per_day, "trip", "trips")} a day not seen, ${plural(r.empty_hours, "empty hour", "empty hours")}`),
                ),
              ),
            ),
          ),
        )
      : null,
    d.worst_days.length
      ? [
          el("h3", {}, "Days well below a route's usual level"),
          el(
            "div",
            { class: "card" },
            el(
              "ol",
              { class: "gaps" },
              d.worst_days.map((w) =>
                el(
                  "li",
                  {},
                  el("strong", { class: "mins" }, `${w.seen_pct}%`),
                  el(
                    "span",
                    { class: "with-badge" },
                    ...route(w),
                    el("span", { class: "detail muted small" }, `${shortDayFmt.format(new Date(`${w.service_date}T00:00:00Z`))}, usually ${w.usual_pct}%`),
                  ),
                ),
              ),
            ),
          ),
        ]
      : null,
    el("p", { class: "muted small" }, "This is not proof of cancellation: a vehicle with its tracker off or on a detour looks the same, and some routes read low every day because of how they are measured. A route that ran in full usually reads 95 to 100%. A route with no vehicle seen all day is not counted."),
  ];
}

// Weekdays with a high-impact road restriction along the route against weekdays without,
// on the same routes.
function closureComparison(c) {
  if (!c) return null;
  const heading = el("h3", {}, "Road closures and construction");
  if (!c.with || !c.without) {
    return [heading, el("p", { class: "muted" }, `Not enough data yet. A route is compared once it has ${c.min_days} weekdays with a high-impact restriction along it and ${c.min_days} without; restrictions that last the whole collection period never allow it.`)];
  }
  const row = (label, side) => el("li", {}, el("strong", { class: "mins" }, ewtText(side.ewt_seconds)), el("span", {}, label, el("span", { class: "detail muted small" }, `${side.bunched_pct}% bunched, ${side.long_pct}% long gaps · ${plural(side.route_days, "route-day", "route-days")}`)));
  return [
    heading,
    el("p", { class: "muted" }, `Extra wait on ${plural(c.with.route_directions, "route direction", "route directions")} that had weekdays both with and without a high-impact City of Toronto road restriction along them.`),
    el("div", { class: "card" }, el("ul", { class: "gaps" }, row("With a restriction", c.with), row("Without", c.without))),
    el("p", { class: "muted small" }, "A comparison, not proof of cause: restrictions are more common on busy streets, and other things change from day to day."),
  ];
}

const cardMonthFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", month: "long", year: "numeric" });
const monthName = (month) => cardMonthFmt.format(new Date(`${month}-01T00:00:00Z`));
const secondsText = (s) => (s === null || s === undefined ? "not measured" : s <= 0 ? "no extra wait" : ewtText(s));

function cardRow(letter, title, detail) {
  return el("li", {}, el("strong", { class: "grade", "aria-label": letter ? `Grade ${letter}` : "Not graded" }, letter || "–"), el("span", {}, title, el("span", { class: "detail muted small" }, detail)));
}

function cardRoute(r) {
  return cardRow(r.grade, el("span", { class: "with-badge" }, el("span", { class: `route-pill ${r.mode}` }, r.short_name || r.route_id), r.long_name || ""), `${secondsText(r.ewt_seconds)} · ${r.bunched_pct}% bunched, ${r.long_pct}% long gaps`);
}

// A month's measurements, graded on this project's own scale.
async function renderReportCard(month) {
  const { months } = await api("/api/report-card");
  if (!months.length) {
    return [...reliabilityHeader("card"), el("p", {}, "The first report card appears when the first measured month has ended.")];
  }
  const chosen = months.includes(month) ? month : months[0];
  const data = await api(`/api/report-card/${chosen}`);
  const c = data.coverage;
  const s = data.surface;
  const o = data.official;
  const scale = data.grades.map((g) => `${g.grade} up to ${g.up_to_seconds} s`).join(", ");
  return [
    ...reliabilityHeader("card"),
    el("h2", {}, monthName(chosen)),
    months.length > 1
      ? el("nav", { class: "chip-group", "aria-label": "Months" }, months.map((m) => el("a", { class: "chip small", href: `#/reliability/card/${m}`, "aria-current": m === chosen ? "page" : null }, monthName(m))))
      : null,
    el("p", {}, `Graded by extra wait: how much longer than the schedule promises a rider who turns up at a random moment waits on average. ${scale}, F beyond that. The same scale applies to every mode, and less frequent routes find it harder to score well.`),
    c.days < c.days_in_month || c.not_collected_hours > 0
      ? el("p", { class: "notice" }, `Measured on ${c.days} of ${c.days_in_month} days${c.not_collected_hours > 0 ? `, with ${c.not_collected_hours} hours not collected` : ""}.`)
      : null,
    el("h3", {}, "Subway"),
    el("div", { class: "card" }, el("ol", { class: "gaps" }, data.lines.map((l) => cardRow(
      l.grade,
      el("span", { class: "with-badge" }, badge(l.route_id, { small: true }), `Line ${l.route_id}`),
      `${secondsText(l.ewt_seconds)} · ${l.regular_pct}% of gaps as scheduled, ${numberFmt.format(l.long_gaps)} long gaps${l.previous_grade ? ` · last month ${l.previous_grade}, ${secondsText(l.previous_ewt_seconds)}` : ""}${l.grade ? "" : ` · needs ${data.min_days} measured days to be graded`}`,
    )))),
    s
      ? [
          el("h3", {}, "Buses and streetcars"),
          el("div", { class: "card" }, el("ol", { class: "gaps" }, cardRow(s.grade, `All ${s.routes} routes measured`, `${secondsText(s.ewt_seconds)} · ${s.bunched_pct}% bunched, ${s.long_pct}% long gaps`))),
          s.worst.length ? [el("h3", {}, "Most extra wait"), el("div", { class: "card" }, el("ol", { class: "gaps" }, s.worst.map(cardRoute)))] : null,
          s.best.length ? [el("h3", {}, "Least extra wait"), el("div", { class: "card" }, el("ol", { class: "gaps" }, s.best.map(cardRoute)))] : null,
        ]
      : null,
    data.service_seen
      ? el("p", {}, `${data.service_seen.seen_pct}% of scheduled bus and streetcar service was seen on the live feed, with ${plural(data.service_seen.empty_hours, "hour", "hours")} on a route and direction with vehicles scheduled and none seen. A vehicle with its tracker off looks the same as one that did not run.`)
      : null,
    data.elevators.outages
      ? el("p", {}, `TTC posted ${plural(data.elevators.outages, "elevator or escalator alert", "elevator and escalator alerts")} this month, and half cleared within ${durationText(data.elevators.median_minutes * 60)}. One device can have several alerts.`)
      : null,
    el("h3", {}, "TTC's own delay log"),
    o.published
      ? o.lines.length
        ? el("div", { class: "card" }, el("ol", { class: "gaps" }, o.lines.map((l) => el("li", {}, el("strong", { class: "mins" }, numberFmt.format(l.incidents)), el("span", {}, `Line ${l.route_id}: incidents TTC logged with a gap between trains`, el("span", { class: "detail muted small" }, `${l.seen_pct === null ? "None could be checked" : `This project saw the gap behind ${l.seen_pct}% of those it could check`}`))))))
        : el("p", { class: "muted" }, "TTC's log has no incidents this project could compare for this month.")
      : el("p", { class: "muted" }, `TTC publishes its log some time after a month ends${o.through ? `; it currently runs to ${shortDayFmt.format(new Date(`${o.through}T00:00:00Z`))}` : ""}. The comparison appears here once it covers this month.`),
    el("p", { class: "muted small" }, "These grades and the scale are this project's own, from predicted arrivals and live positions. They are not TTC statistics."),
  ];
}

async function renderReport() {
  const data = await api("/api/report");
  if (!data.available || !data.lines.length) {
    return [...reliabilityHeader("report"), el("p", {}, "The report appears after the first scheduled transformation with measured gaps.")];
  }
  const c = data.coverage;
  const pick = (rows, key) => (k, line) => rows.find((r) => r[key] === k && r.route_id === line);
  const periods = data.period_labels.filter((p) => data.periods.some((r) => r.period === p.key));
  const missing = data.period_labels.filter((p) => !periods.includes(p));
  const rainHours = maxHours(data.weather, "rain");
  const dates = [...new Set(data.daily.map((d) => d.service_date))];
  return [
    ...reliabilityHeader("report"),
    el(
      "p",
      {},
      `${c.days} service days measured from ${c.first_day} to ${c.last_day}, ${c.weekdays} of them weekdays. `,
      c.outage_hours ? `${c.outage_hours} hours when the collector was down are left out, not estimated.` : "",
    ),
    c.days < EARLY_DAYS
      ? el("p", { class: "notice" }, `Early results: with ${c.days} days of data, one bad day moves these numbers. They firm up over the coming weeks.`)
      : null,
    el("h2", {}, "Extra wait by line"),
    el(
      "p",
      { class: "muted" },
      "Extra wait is how much longer than the schedule promises a rider who arrives at a random moment waits on average. Uneven gaps raise it even when trains run as often as scheduled. Below zero means waits were shorter than scheduled.",
    ),
    ewtTable(["weekday", "weekend"], (k) => (k === "weekday" ? "Weekdays" : "Weekends and holidays"), pick(data.lines, "days")),
    el("h2", {}, "By time of day on weekdays"),
    ewtTable(
      periods.map((p) => p.key),
      (k) => {
        const p = data.period_labels.find((x) => x.key === k);
        return [p.label, el("span", { class: "detail muted small" }, p.hours)];
      },
      pick(data.periods, "period"),
    ),
    missing.length
      ? el("p", { class: "muted small" }, `Not measured on a weekday yet: ${missing.map((p) => p.label.toLowerCase()).join(", ")}.`)
      : null,
    el("h2", {}, "Platforms with the most extra wait"),
    el("p", { class: "muted" }, `All days, platforms with at least ${data.station_min_gaps} measured gaps. Terminals are excluded because trains wait there by design.`),
    el(
      "div",
      { class: "card" },
      el(
        "ol",
        { class: "gaps" },
        data.stations.map((s) =>
          el(
            "li",
            {},
            el("strong", { class: "mins" }, ewtText(s.ewt_seconds)),
            el(
              "span",
              {},
              badge(s.route_id, { small: true }),
              " ",
              el("a", { href: `#/station/${s.station_key}` }, s.station_name),
              ` ${s.platform.toLowerCase()} towards ${s.towards}`,
              el("span", { class: "detail muted small" }, `${s.long_pct}% long gaps, ${numberFmt.format(s.gaps)} gaps measured`),
            ),
          ),
        ),
      ),
    ),
    surfaceSection(data.surface),
    bikeTripsSection(data.bike_trips),
    el("h2", {}, "Rain and dry hours"),
    el(
      "p",
      { class: "muted" },
      "Each hour is matched to Environment Canada's observation at Toronto City, downtown. ",
      rainHours < 24 ? `Only ${rainHours} rainy hours so far, too few to say whether rain changes anything.` : `${rainHours} rainy hours so far.`,
    ),
    ewtTable(
      ["dry", "rain"],
      (k) => [k === "dry" ? "Dry" : "Rain", el("span", { class: "detail muted small" }, `${maxHours(data.weather, k)} hours`)],
      pick(data.weather, "conditions"),
    ),
    el("h2", {}, "Day by day"),
    ewtTable(
      dates,
      (d) => {
        const day = data.daily.find((r) => r.service_date === d);
        return [shortDayFmt.format(new Date(`${d}T00:00:00Z`)), el("span", { class: "detail muted small" }, DAY_TYPE_LABELS[day.day_type] || day.day_type)];
      },
      pick(data.daily, "service_date"),
    ),
    el(
      "p",
      { class: "muted small" },
      "Method: gaps are measured between inferred train arrivals at each platform and compared with that platform's scheduled headway for the hour. A long gap exceeds twice the scheduled headway and the headway plus 5 minutes. Gaps spanning a collector outage, at terminals, or caused by work trains outside service hours are excluded. ",
      el("a", { href: "https://github.com/MavenTheKeen/torontrack/blob/main/docs/reliability.md" }, "Full definitions"),
    ),
  ];
}

// Map: a self-hosted street basemap (transit map-tiles; docs/map.md) with subway lines,
// estimated train positions, stations, elevator outages and every Bike Share dock.
// MapLibre and the tile reader load only when the map is opened. The visitor's location
// is used only in this page to find what is nearest; it is never sent anywhere.
const MAP_HOME = { center: [-79.385, 43.6585], zoom: 12.3 };
const MAP_FOCUS_ZOOM = 15;
// Wide enough for the regional rail network: Stratford and Kitchener to Oshawa, Niagara
// Falls to Barrie. Streets outside Toronto are in the map at regional detail only.
const MAP_BOUNDS = [[-81.2, 42.95], [-78.45, 44.55]];
const TRAIN_REFRESH_MS = 10000; // The collector polls trains and vehicles every 10 s.
// Trains and vehicles move between refreshes (motion.js). Each redraw sends them all to
// the map's worker, which re-indexes them, so they are redrawn only as often as the
// fastest of them (about 90 km/h) can move one pixel: every 70 ms at zoom 15, 280 ms at
// zoom 13. Redrawing on every frame repeated the same picture up to 60 times a second.
const MOTION_SPEED_MPS = 25;
const MOTION_MIN_MS = 16;
const MOTION_MAX_MS = 1000;
// While the map is being dragged, vehicles already sit on it; redraws only add those
// coming into view, so this often is enough.
const MOTION_PANNING_MS = 200;
const MOTION_STILL_MS = 5000; // With reduced motion.
// Sources of fixed points (stops, stations, docks) are cut into tiles up to this zoom and
// scaled up beyond it, which places a point exactly; finer tiles only used memory. Point
// sources also have no tile buffer: a point lies in one tile, so copies of those near an
// edge in the neighbouring tiles were never drawn.
const POINTS_MAXZOOM = 14;
const BIKES_REFRESH_MS = 120000; // Bike Share data changes every 15 minutes.
const WALK_METRES_PER_MIN = 80;
const TRAIN_OFFSET_M = 28; // Each direction sits a little to its own side of the line.
const MAP_MODES = { bikes: "Bikes", docks: "Docks" };
const DIRECTION_SIDE = { 0: 1, 1: -1 };
// The map is a transit map or a food map, switched from the toolbar and remembered.
const MAP_KIND_KEY = "torontrack-map-kind";
function savedMapKind() {
  try {
    const kind = localStorage.getItem(MAP_KIND_KEY);
    return ["food", "city", "weather"].includes(kind) ? kind : "transit";
  } catch {
    return "transit";
  }
}
const mapState = { view: null, kind: savedMapKind(), food: { kinds: [true, true, true, true, true], clean: false, long: false, notable: false, other: false }, city: { skate: true, swim: true, beach: true, patio: true, events: true, now: false }, mode: "bikes", layers: { trains: true, regional: true, buses: true, bikes: true, access: true, entrances: true, roads: false, lanes: false, events: false, food: false, parking: false, bikeParking: false } };
// Buses and streetcars. TTC's subway lines own yellow, green and purple and the site's
// brand is raspberry, so TTC buses take blue and streetcars crimson, and neighbouring
// agencies' buses (mode = agency key, transit.agencies) teal and indigo. All are always
// labelled, never told apart by colour alone.
const SURFACE_COLOURS = { bus: "#2563eb", streetcar: "#e11d48", br: "#0f766e", mi: "#4f46e5", yr: "#a21caf" };
const SURFACE_LABELS = { bus: "Bus", streetcar: "Streetcar", br: "Brampton Transit bus", mi: "MiWay bus", yr: "York Region Transit bus" };
const surfaceGlyph = (mode) => (mode === "streetcar" ? "streetcar" : "bus");
// A route's number as shown: agencies' route IDs carry their prefix ("br:511").
const routeName = (view, id) => (view.surfaceRoutes && view.surfaceRoutes.get(id)?.short_name) || String(id).replace(/^[a-z]{2}:/, "");
const OCCUPANCY = {
  EMPTY: "Empty", MANY_SEATS_AVAILABLE: "Many seats free", FEW_SEATS_AVAILABLE: "Few seats free",
  STANDING_ROOM_ONLY: "Standing room only", CRUSHED_STANDING_ROOM_ONLY: "Very crowded", FULL: "Full",
  NOT_ACCEPTING_PASSENGERS: "Not taking passengers",
};
let maplibre = null;
let activeMap = null; // { map, timers, watch, data, trains, ... } while the map page is open.

function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const script = el("script", { src });
    script.addEventListener("load", resolve);
    script.addEventListener("error", () => reject(new Error(`Could not load ${src}`)));
    document.head.append(script);
  });
}

async function loadMapLibrary() {
  if (maplibre) return maplibre;
  document.head.append(el("link", { rel: "stylesheet", href: "/static/vendor/maplibre-gl/maplibre-gl.css" }));
  const [lib] = await Promise.all([import("/static/vendor/maplibre-gl/maplibre-gl.mjs"), loadScript("/static/vendor/pmtiles/pmtiles.js")]);
  const protocol = new window.pmtiles.Protocol();
  lib.addProtocol("pmtiles", protocol.tile);
  maplibre = lib;
  return lib;
}

// The theme chosen on the site, or the device's (theme.js keeps <html data-theme> current).
const prefersDark = () => document.documentElement.dataset.theme === "dark";
const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Muted streets so the transit layers carry the colour. Layer names follow the Protomaps
// basemap schema (v4) that the extract uses.
function basemapColours(dark) {
  return dark
    ? { water: "#0f151c", land: "#15171c", park: "#15201c", building: "#1c1f25", road: "#272b33", major: "#2f343d", highway: "#3a3f49", casing: "#0e1014", label: "#a6aab3", halo: "#14161b", rail: "#3e4450" }
    : { water: "#c6d4e8", land: "#eef1f6", park: "#d6e8e1", building: "#e1e6ee", road: "#ffffff", major: "#ffffff", highway: "#fbfcfd", casing: "#cfd5e1", label: "#4c5775", halo: "#f3f5f8", rail: "#a5aec2" };
}

function mapStyle(hasBasemap) {
  const c = basemapColours(prefersDark());
  const name = ["coalesce", ["get", "name:en"], ["get", "name"]];
  const width = (stops) => ["interpolate", ["exponential", 1.6], ["zoom"], ...stops];
  const roadKinds = { highway: "highway", major: "major_road", minor: "minor_road" };
  const basemap = hasBasemap
    ? [
        { id: "earth", type: "fill", source: "basemap", "source-layer": "earth", paint: { "fill-color": c.land } },
        { id: "park", type: "fill", source: "basemap", "source-layer": "landuse", filter: ["in", ["get", "kind"], ["literal", ["park", "wood", "forest", "grass", "golf_course", "nature_reserve", "cemetery", "garden", "playground", "recreation_ground", "village_green"]]], paint: { "fill-color": c.park } },
        { id: "water", type: "fill", source: "basemap", "source-layer": "water", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": c.water } },
        { id: "waterway", type: "line", source: "basemap", "source-layer": "water", filter: ["==", ["geometry-type"], "LineString"], paint: { "line-color": c.water, "line-width": width([10, 0.5, 16, 3]) } },
        { id: "buildings", type: "fill", source: "basemap", "source-layer": "buildings", minzoom: 14, paint: { "fill-color": c.building, "fill-opacity": ["interpolate", ["linear"], ["zoom"], 14, 0, 15.5, 1] } },
        { id: "rail", type: "line", source: "basemap", "source-layer": "roads", filter: ["==", ["get", "kind"], "rail"], minzoom: 11, paint: { "line-color": c.rail, "line-width": 1, "line-dasharray": [3, 3] } },
        { id: "road-casing", type: "line", source: "basemap", "source-layer": "roads", filter: ["in", ["get", "kind"], ["literal", [roadKinds.highway, roadKinds.major]]], minzoom: 11, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.casing, "line-width": width([11, 1.5, 18, 22]) } },
        { id: "road-minor", type: "line", source: "basemap", "source-layer": "roads", filter: ["==", ["get", "kind"], roadKinds.minor], minzoom: 12.5, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.road, "line-width": width([12.5, 0.5, 18, 12]) } },
        { id: "road-path", type: "line", source: "basemap", "source-layer": "roads", filter: ["==", ["get", "kind"], "path"], minzoom: 15, paint: { "line-color": c.road, "line-width": width([15, 0.5, 18, 2]), "line-dasharray": [2, 1.5] } },
        { id: "road-major", type: "line", source: "basemap", "source-layer": "roads", filter: ["==", ["get", "kind"], roadKinds.major], minzoom: 10, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.major, "line-width": width([10, 0.6, 18, 18]) } },
        { id: "road-highway", type: "line", source: "basemap", "source-layer": "roads", filter: ["==", ["get", "kind"], roadKinds.highway], layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.highway, "line-width": width([8, 0.8, 18, 20]) } },
        { id: "road-label", type: "symbol", source: "basemap", "source-layer": "roads", filter: ["in", ["get", "kind"], ["literal", [roadKinds.major, roadKinds.minor]]], minzoom: 14, layout: { "symbol-placement": "line", "text-field": name, "text-font": ["Noto Sans Regular"], "text-size": 11, "text-letter-spacing": 0.02 }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 1.5 } },
        { id: "place-label", type: "symbol", source: "basemap", "source-layer": "places", filter: ["in", ["get", "kind"], ["literal", ["neighbourhood", "macrohood", "locality"]]], minzoom: 11, maxzoom: 15.5, layout: { "text-field": name, "text-font": ["Noto Sans Medium"], "text-size": ["interpolate", ["linear"], ["zoom"], 11, 10, 15, 13], "text-transform": "uppercase", "text-letter-spacing": 0.08, "text-max-width": 8 }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 1.5, "text-opacity": 0.75 } },
      ]
    : [];
  return {
    version: 8,
    glyphs: `${location.origin}/static/map/fonts/{fontstack}/{range}.pbf`,
    sources: hasBasemap
      ? { basemap: { type: "vector", url: `pmtiles://${location.origin}/map-data/toronto.pmtiles`, attribution: "© OpenStreetMap contributors · Protomaps" } }
      : {},
    layers: [{ id: "background", type: "background", paint: { "background-color": hasBasemap ? c.water : c.land } }, ...basemap],
  };
}

function metres(a, b) {
  const rad = Math.PI / 180;
  const dLat = (b[1] - a[1]) * rad;
  const dLon = (b[0] - a[0]) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
}

function walkText(m) {
  const minutes = Math.max(1, Math.round(m / WALK_METRES_PER_MIN));
  return `${m < 1000 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(1)} km`} · ${minutes} min walk`;
}

function dockCount(dock, mode) {
  return mode === "bikes" ? dock.bikes : dock.docks;
}

function dockLevel(dock, mode) {
  if (!dock.current) return "unknown";
  const n = dockCount(dock, mode);
  return n === 0 ? "none" : n <= 2 ? "few" : "ok";
}

// A train's position is a fractional index into its line's stations; place it on the
// straight segment between them, nudged to its direction's side.
function trainPoint(line, train, coords) {
  const keys = line.stations;
  const i = Math.max(0, Math.min(keys.length - 1, Math.floor(train.position)));
  const j = Math.min(keys.length - 1, i + 1);
  const a = coords.get(keys[i]);
  const b = coords.get(keys[j]);
  if (!a || !b) return null;
  const f = Math.max(0, Math.min(1, train.position - i));
  const lon = a[0] + (b[0] - a[0]) * f;
  const lat = a[1] + (b[1] - a[1]) * f;
  const dx = (b[0] - a[0]) * Math.cos((lat * Math.PI) / 180);
  const dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  const side = DIRECTION_SIDE[train.direction_id] * TRAIN_OFFSET_M / 111320;
  return [lon + (-dy / len) * side / Math.cos((lat * Math.PI) / 180), lat + (dx / len) * side];
}

function featureCollection(features) {
  return { type: "FeatureCollection", features };
}

function point(coord, properties) {
  return { type: "Feature", geometry: { type: "Point", coordinates: coord }, properties };
}

function circlePolygon(center, radiusM) {
  const ring = [];
  for (let k = 0; k <= 48; k++) {
    const angle = (k / 48) * 2 * Math.PI;
    ring.push([
      center[0] + (radiusM * Math.cos(angle)) / (111320 * Math.cos((center[1] * Math.PI) / 180)),
      center[1] + (radiusM * Math.sin(angle)) / 111320,
    ]);
  }
  return { type: "Feature", geometry: { type: "Polygon", coordinates: [ring] }, properties: {} };
}

function networkFeatures(data) {
  const lines = featureCollection(
    data.lines.map((l) => ({ type: "Feature", geometry: { type: "LineString", coordinates: l.path.map(([lat, lon]) => [lon, lat]) }, properties: { id: l.id, color: l.color || "#777" } })),
  );
  const terminals = new Set(data.lines.flatMap((l) => [l.path[0], l.path[l.path.length - 1]].map(([lat, lon]) => `${lat},${lon}`)));
  const stations = featureCollection(
    data.stations.map((s) =>
      point([s.lon, s.lat], { key: s.key, name: s.name, major: s.lines.length > 1 || terminals.has(`${s.lat},${s.lon}`), outages: s.outages.length }),
    ),
  );
  const entrances = featureCollection(
    data.stations.flatMap((s) => (s.entrances || []).map((e) => point(e.point, { id: `${s.key}|${e.id}`, name: e.name, step_free: e.step_free }))),
  );
  return { lines, stations, entrances };
}

function dockFeatures(data, mode) {
  return featureCollection(
    data.bike_share.docks.map((d) => {
      const count = dockCount(d, mode);
      return point([d.lon, d.lat], { id: d.id, level: dockLevel(d, mode), label: count === null || count === undefined ? "" : String(count) });
    }),
  );
}

function trainFeatures(trains, coords, nowMs = Date.now()) {
  const features = [];
  for (const line of trains.lines) {
    for (const t of line.trains) {
      const at = trainPoint(line, { ...t, position: trainPosition(t, nowMs) }, coords);
      if (at) features.push(point(at, { id: `${line.id}:${t.train_id}`, line: line.id, at_station: t.at_station }));
    }
  }
  return features;
}

function vehicleRecords(data) {
  const i = Object.fromEntries(data.fields.map((f, k) => [f, k]));
  return data.vehicles.map((v) => ({
    id: v[i.id], route: v[i.route], mode: v[i.mode], lon: v[i.lon], lat: v[i.lat], bearing: v[i.bearing], towards: v[i.towards] || "",
    occupancy: v[i.occupancy] || "", reported: v[i.reported], stopped: v[i.stopped], pattern: v[i.pattern], position: v[i.position], pace: v[i.pace],
  }));
}

// Draws every train and vehicle where motion.js estimates it is now. Only those in or near
// the view are sent to the map, which keeps a redraw cheap enough for every frame.
function moveVehicles(view) {
  const map = view.map;
  if (!map || activeMap !== view || !view.glide) return;
  const now = Date.now();
  const still = reduceMotion();
  const b = map.getBounds();
  // Vehicles a little beyond the edges are drawn too, so a short pan finds them there.
  const padLon = (b.getEast() - b.getWest()) * 0.5;
  const padLat = (b.getNorth() - b.getSouth()) * 0.5;
  const inView = (f) => {
    const [lon, lat] = f.geometry.coordinates;
    return lon > b.getWest() - padLon && lon < b.getEast() + padLon && lat > b.getSouth() - padLat && lat < b.getNorth() + padLat;
  };
  const place = (id, coord) => (still ? coord : glide(view.glide, id, coord, now));
  // Layers hidden at this zoom are not moved; a train shown again starts afresh (glide).
  const zoom = map.getZoom();
  const source = map.getSource("moving");
  if (!source) return;
  const moving = [];
  if (view.trains && shownAt("trains", zoom)) {
    view.trainFeatures = trainFeatures(view.trains, view.coords, now).map((f) => point(place(f.properties.id, f.geometry.coordinates), { ...f.properties, moving: "train" }));
    moving.push(...view.trainFeatures);
  }
  if (view.vehicles && shownAt("vehicles", zoom)) {
    // Only vehicles near the view are estimated each frame; one coming into view starts
    // afresh (motion.js glide), so it does not catch up on its time off screen.
    const near = (v) => v.lon > b.getWest() - 1.5 * padLon && v.lon < b.getEast() + 1.5 * padLon && v.lat > b.getSouth() - 1.5 * padLat && v.lat < b.getNorth() + 1.5 * padLat;
    view.vehicleFeatures = view.vehicles.filter(near).map((v) => {
      const at = vehiclePlace(v, view.patterns && view.patterns.get(v.pattern), now);
      const heading = at.bearing !== null && at.bearing !== undefined;
      const coord = place(`v:${v.id}`, at.coord);
      const bearing = heading && !still ? turn(view.glide, `v:${v.id}`, at.bearing, now) : at.bearing;
      // Only what the map's layers draw; the vehicle panel reads the rest from view.vehicles.
      return point(coord, { id: v.id, route: v.route, label: routeName(view, v.route), mode: v.mode, bearing: heading ? bearing : 0, heading, fade: at.fade, moving: "vehicle" });
    });
    moving.push(...view.vehicleFeatures.filter(inView));
  }
  if (view.regionalTrains && shownAt("regional", zoom)) {
    view.regionalTrainFeatures = view.regionalTrains.trains.map((t) => point(place(`r:${t.id}`, trackPoint(t.track, now) || [t.lon, t.lat]), { id: t.id, line: t.line, colour: t.colour || "#4b5563", moving: "regional" }));
    moving.push(...view.regionalTrainFeatures);
  }
  // One update for all three: separate sources each made the map render once more.
  source.setData(featureCollection(moving));
  if (!still) pruneGlide(view.glide, now);
}

// New data: vehicles glide from where they are drawn to their new estimates.
function newPositions(view) {
  if (!view.glide) return;
  view.glide.seq += 1;
  moveVehicles(view);
}

function stopFeatures(surface) {
  return featureCollection(surface.stops.map(([id, code, name, lat, lon, routes]) => point([lon, lat], { id, name, routes: routes.join(" ") })));
}

const DOCK_COLOURS = { ok: "#2bb39a", few: "#f08a3c", none: "#e8336d", unknown: "#9aa5bf" };
const DOCK_LEVEL_LABELS = { ok: "3 or more", few: "1 or 2", none: "None", unknown: "No recent report" };
const OUTAGE_COLOUR = "#ffc83d";
const ENTRANCE_COLOUR = "#57534e";
const STEP_FREE_COLOUR = "#0369a1";
const PARKING_COLOUR = "#4338ca";
const BIKE_PARKING_COLOUR = "#0f766e";
// Food premises by latest inspection result, in the order of /api/food's result codes.
const FOOD_LEVELS = ["pass", "conditional", "closed", "other"];
const FOOD_COLOURS = { pass: "#1f9a83", conditional: "#ffc83d", closed: "#e8336d", other: "#9aa5bf" };
const FOOD_LABELS = { pass: "Pass", conditional: "Conditional pass", closed: "Closed", other: "Other result" };
// The food map's places to eat, in the order of /api/food/places' kind codes. Each kind has
// its own icon and colour; amber and red are kept for failed inspections.
const EAT_KINDS = [
  { glyph: "food", label: "Restaurants", short: "Restaurants", one: "Restaurant", colour: "#2f6fb8" },
  { glyph: "cafe", label: "Cafés", short: "Cafés", one: "Café", colour: "#8a5a3c" },
  { glyph: "fastfood", label: "Fast food", short: "Fast food", one: "Fast food", colour: "#1f9a83" },
  { glyph: "drink", label: "Bars and pubs", short: "Bars", one: "Bar or pub", colour: "#1d2a52" },
  { glyph: "dessert", label: "Bakeries and desserts", short: "Sweets", one: "Bakery or dessert shop", colour: "#e8336d" },
];
const EAT_COLOUR = "#4c5775";
// Notable places (Wikidata): a yellow ring, drawn on top and never clustered.
const NOTABLE_COLOUR = "#ffc83d";

function mapCasing() {
  return prefersDark() ? "#14161b" : "#ffffff";
}

// Every icon the map draws, from icons.js. Subway trains take their line's colour.
function addMapIcons(map) {
  const ring = mapCasing();
  for (const [mode, colour] of Object.entries(SURFACE_COLOURS)) addBadge(map, `badge-${mode}`, surfaceGlyph(mode), colour, "#ffffff", ring);
  for (const l of lines) addBadge(map, `badge-train-${l.id}`, "subway", l.color || "#777", textOn(l.color || "#777777"), ring);
  addBadge(map, "badge-train", "subway", "#777777", "#ffffff", ring);
  for (const [level, colour] of Object.entries(DOCK_COLOURS)) addBadge(map, `badge-dock-${level}`, "bike", colour, "#ffffff", ring, { css: 24, glyph: 15 });
  addBadge(map, "badge-event-today", "event", EVENT_COLOUR, "#ffffff", ring, { css: 24, glyph: 14 });
  addBadge(map, "badge-event-week", "event", prefersDark() ? "#94a3b8" : EVENT_COLOUR, "#ffffff", ring, { css: 24, glyph: 14, hollow: true });
  addBadge(map, "badge-construction", "roadwork", ROAD_COLOURS.construction, "#141d3b", ring, { css: 24, glyph: 15 });
  addBadge(map, "badge-closure", "closure", ROAD_COLOURS.closure, "#ffffff", ring, { css: 24, glyph: 15 });
  addBadge(map, "badge-elevator", "elevator", OUTAGE_COLOUR, "#141d3b", ring, { css: 20, glyph: 13 });
  addBadge(map, "badge-entrance", "entrance", ENTRANCE_COLOUR, "#ffffff", ring, { css: 22, glyph: 14 });
  addBadge(map, "badge-entrance-free", "stepfree", STEP_FREE_COLOUR, "#ffffff", ring, { css: 22, glyph: 15 });
  addBadge(map, "badge-parking", "parking", PARKING_COLOUR, "#ffffff", ring, { css: 22, glyph: 14 });
  addBadge(map, "badge-parking-entrance", "entrance", PARKING_COLOUR, "#ffffff", ring, { css: 18, glyph: 12 });
  addBadge(map, "badge-rack", "rack", BIKE_PARKING_COLOUR, "#ffffff", ring, { css: 20, glyph: 13 });
  EAT_KINDS.forEach((k, i) => {
    addBadge(map, `badge-eat-${i}`, k.glyph, k.colour, "#ffffff", ring, { css: 22, glyph: 14 });
    addBadge(map, `badge-eat-${i}-conditional`, k.glyph, FOOD_COLOURS.conditional, "#141d3b", ring, { css: 24, glyph: 15 });
    addBadge(map, `badge-eat-${i}-closed`, k.glyph, FOOD_COLOURS.closed, "#ffffff", ring, { css: 24, glyph: 15 });
    addBadge(map, `badge-eat-${i}-notable`, k.glyph, k.colour, "#ffffff", ring, { css: 32, glyph: 16, halo: NOTABLE_COLOUR });
  });
  addBadge(map, "badge-agenda", "event", EVENT_COLOUR, "#ffffff", ring, { css: 22, glyph: 13 });
  addBadge(map, "badge-agenda-major", "event", EVENT_COLOUR, "#ffffff", ring, { css: 32, glyph: 17, halo: MAJOR_COLOUR });
  addBadge(map, "badge-patio", "patio", PATIO_COLOUR, "#ffffff", ring, { css: 22, glyph: 14 });
  for (const [state, colour] of Object.entries(BEACH_COLOURS)) addBadge(map, `badge-beach-${state}`, "beach", colour, "#ffffff", ring, { css: 26, glyph: 16, hollow: state === "off" });
  for (const [kind, colour] of Object.entries(REC_COLOURS)) {
    addBadge(map, `badge-rec-${kind}`, kind, colour, "#ffffff", ring, { css: 26, glyph: 16 });
    addBadge(map, `badge-rec-${kind}-now`, kind, colour, "#ffffff", ring, { css: 30, glyph: 17, halo: "#16a34a" });
    addBadge(map, `badge-rec-${kind}-done`, kind, colour, "#ffffff", ring, { css: 22, glyph: 13, hollow: true });
  }
  for (const level of FOOD_LEVELS) addBadge(map, `badge-food-${level}`, "food", FOOD_COLOURS[level], level === "conditional" ? "#141d3b" : "#ffffff", ring, { css: 20, glyph: 13 });
  addHeadingImage(map);
}

function addSurfaceLayers(map) {
  const c = prefersDark() ? { stop: "#1c1f25", stopStroke: "#8c8c8c", halo: "#14161b", casing: "#14161b" } : { stop: "#ffffff", stopStroke: "#4c5775", halo: "#ffffff", casing: "#ffffff" };
  const modeColour = ["match", ["get", "mode"], ...Object.entries(SURFACE_COLOURS).filter(([m]) => m !== "bus").flat(), SURFACE_COLOURS.bus];
  map.addSource("route-path", { type: "geojson", data: featureCollection([]) });
  map.addSource("bus-stops", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  const before = "line-casing";
  map.addLayer({ id: "route-path-casing", type: "line", source: "route-path", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 5, 16, 11] } }, before);
  map.addLayer({ id: "route-path", type: "line", source: "route-path", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "colour"], "line-width": ["interpolate", ["linear"], ["zoom"], 10, 3, 16, 7], "line-opacity": 0.9 } }, before);
  map.addLayer({ id: "bus-stops", type: "circle", source: "bus-stops", minzoom: 15, paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 2.5, 18, 6], "circle-color": c.stop, "circle-stroke-color": c.stopStroke, "circle-stroke-width": 1.4,
    "circle-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0, 15.4, 1], "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0, 15.4, 1],
  } }, "outages");
  map.addLayer({ id: "vehicles", type: "circle", source: "moving", filter: ["==", ["get", "moving"], "vehicle"], minzoom: 11, maxzoom: 14, paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 1.6, 14, 4], "circle-color": modeColour,
    "circle-stroke-color": c.casing, "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 11, 0.4, 14, 1.2],
    "circle-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0, 11.5, ["get", "fade"]], "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0, 11.5, ["get", "fade"]],
  } }, "outages");
  const iconSize = ["interpolate", ["linear"], ["zoom"], 14, 0.75, 17, 1.05];
  map.addLayer({ id: "vehicle-heading", type: "symbol", source: "moving", minzoom: 14, filter: ["all", ["==", ["get", "moving"], "vehicle"], ["get", "heading"]], layout: {
    "icon-image": "heading", "icon-size": iconSize, "icon-rotate": ["get", "bearing"], "icon-rotation-alignment": "map",
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  }, paint: { "icon-color": modeColour } }, "outages");
  map.addLayer({ id: "vehicle-icon", type: "symbol", source: "moving", filter: ["==", ["get", "moving"], "vehicle"], minzoom: 14, layout: {
    "icon-image": ["concat", "badge-", ["get", "mode"]], "icon-size": iconSize,
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  }, paint: { "icon-opacity": ["interpolate", ["linear"], ["zoom"], 14, 0, 14.3, ["get", "fade"]] } }, "outages");
  map.addLayer({ id: "vehicle-label", type: "symbol", source: "moving", filter: ["==", ["get", "moving"], "vehicle"], minzoom: 15, layout: {
    "text-field": ["get", "label"], "text-font": ["Noto Sans Medium"], "text-size": 11, "text-anchor": "left", "text-offset": [1.3, 0],
    "text-allow-overlap": false, "text-optional": true,
  }, paint: { "text-color": modeColour, "text-halo-color": c.halo, "text-halo-width": 1.8 } });
}

// "Stop 1234", or "Brampton Transit stop 1001" for another agency's.
function stopKicker(stop) {
  const kind = stop.agency ? `${stop.agency} stop` : "Stop";
  return stop.code ? `${kind} ${stop.code}` : kind;
}

// How a stop's arrival times are worked out, for the TTC's stops or another agency's.
function estimateNote(stopId) {
  return String(stopId).includes(":")
    ? "Times are the agency's own predictions where it publishes them; others, marked est., are estimated from the bus's live position and the scheduled time between stops."
    : "Times are TTC's predictions where it publishes them; others, marked est., are estimated from the vehicle's live position and the scheduled time between stops.";
}

function routePill(view, routeId, { button = true } = {}) {
  const route = view.surfaceRoutes.get(routeId);
  const mode = route ? route.mode : "bus";
  const attrs = { class: `route-pill ${mode}`, title: route ? `${SURFACE_LABELS[mode]} ${route.short_name} ${route.long_name}` : routeId };
  return button ? el("button", { type: "button", ...attrs, "data-route": routeId }, route ? route.short_name : routeId) : el("span", attrs, route ? route.short_name : routeId);
}

async function showRoute(view, routeId) {
  try {
    const data = await api(`/api/routes/${encodeURIComponent(routeId)}`);
    const colour = SURFACE_COLOURS[data.route.mode] || SURFACE_COLOURS.bus;
    view.map.getSource("route-path").setData(featureCollection(data.patterns.map((p) => ({ type: "Feature", geometry: { type: "LineString", coordinates: p.path }, properties: { colour } }))));
    view.route = routeId;
    focusVehicles(view);
    const coords = data.patterns.flatMap((p) => p.path);
    if (coords.length) {
      const lons = coords.map((c) => c[0]);
      const lats = coords.map((c) => c[1]);
      view.map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], { padding: { top: 170, bottom: 260, left: 40, right: 40 }, duration: reduceMotion() ? 0 : 900, maxZoom: 15 });
    }
  } catch {
    /* The route stays unhighlighted; the panel still shows its details. */
  }
}

// While a route is shown, its own vehicles stay solid and the rest fade back.
function focusVehicles(view) {
  const map = view.map;
  // Vehicles whose report is old are faded too (fade, from motion.js).
  const strength = view.route ? ["*", ["case", ["==", ["get", "route"], view.route], 1, 0.18], ["get", "fade"]] : ["get", "fade"];
  if (map.getLayer("vehicles")) {
    map.setPaintProperty("vehicles", "circle-opacity", strength);
    map.setPaintProperty("vehicles", "circle-stroke-opacity", strength);
  }
  if (map.getLayer("vehicle-icon")) map.setPaintProperty("vehicle-icon", "icon-opacity", view.route ? strength : ["interpolate", ["linear"], ["zoom"], 13.5, 0, 14, ["get", "fade"]]);
  if (map.getLayer("vehicle-label")) map.setPaintProperty("vehicle-label", "text-opacity", strength);
}

function clearRoute(view) {
  view.route = null;
  focusVehicles(view);
  const source = view.map.getSource("route-path");
  if (source) source.setData(featureCollection([]));
}

function vehicleSheet(view, id) {
  const f = (view.vehicleFeatures || []).find((x) => x.properties.id === id);
  if (!f) return null;
  const v = f.properties;
  const route = view.surfaceRoutes.get(v.route);
  return [
    el("p", { class: "sheet-kicker" }, SURFACE_LABELS[v.mode] || "Bus"),
    el("h2", { class: "with-badge" }, routePill(view, v.route, { button: false }), route ? route.long_name : `Route ${v.route}`),
    el("p", {}, v.towards ? `Towards ${v.towards}` : el("span", { class: "muted" }, "Not matched to its route right now (detour or not yet in service)")),
    v.occupancy ? el("p", { class: "muted" }, OCCUPANCY[v.occupancy] || v.occupancy) : null,
    el("p", { class: "muted small" }, `Vehicle ${id}. Position from TTC's live feed, which updates every 10 to 30 seconds; in between, the map estimates its movement along the route.`),
    el("p", { class: "sheet-actions" }, el("button", { type: "button", class: "button", "data-route": v.route }, `Show route ${route ? route.short_name : v.route}`)),
  ];
}

function stopArrivals(data) {
  return data.arrivals.length
    ? el(
        "ul",
        { class: "sheet-list" },
        data.arrivals.map((a) =>
          el(
            "li",
            { class: "arrival-row" },
            el("span", { class: "with-badge" }, el("span", { class: `route-pill ${a.mode || "bus"}` }, a.short_name), el("span", {}, a.towards)),
            arrivalMins(a.seconds < 60 ? 0 : Math.round(a.seconds / 60), a.source === "estimate" ? el("span", { class: "muted small" }, " est.") : null),
          ),
        ),
      )
    : el("p", { class: "muted" }, "No vehicles found approaching in the next hour.");
}

function stopSheet(view, id, data) {
  const stop = view.surfaceStops.get(id);
  if (!data) {
    return [el("p", { class: "sheet-kicker" }, "Stop"), el("h2", {}, stop ? stop[2] : `Stop ${id}`), el("p", { class: "muted" }, "Finding the next vehicles…")];
  }
  const routes = [...new Set(data.routes.map((r) => r.route_id))];
  return [
    el("p", { class: "sheet-kicker" }, stopKicker(data.stop)),
    el("h2", {}, data.stop.name),
    saveButton("stop", id, data.stop.name),
    el("p", { class: "route-pills" }, routes.map((r) => routePill(view, r))),
    el("p", { class: "sheet-label" }, "Next vehicles (estimated)"),
    stopArrivals(data),
    el("p", { class: "muted small" }, estimateNote(id)),
    stop ? planHere(view, [stop[4], stop[3]], data.stop.name) : null,
    el("p", { class: "sheet-actions" }, el("a", { class: "button", href: `#/stop/${encodeURIComponent(id)}` }, "Stop page")),
  ];
}

async function loadStopSheet(view, id) {
  try {
    const data = await api(`/api/stops/${encodeURIComponent(id)}`);
    if (view.selected && view.selected.kind === "stop" && view.selected.id === id) fill(view.sheetBody, stopSheet(view, id, data));
  } catch {
    if (view.selected && view.selected.id === id) fill(view.sheetBody, el("p", { class: "muted" }, "This stop could not be loaded right now."));
  }
}

// Regional trains. Their own line colours, always with the line code in the marker,
// since Kitchener green and Line 2 green are close. Positions are live where Metrolinx
// reports the train (docs/regional.md#live-trains) and follow the timetable otherwise.
function regionalPill(code, colour, { small = false } = {}) {
  return el("span", { class: `code-pill${small ? " small" : ""}`, style: { background: colour || "#4b5563" } }, code);
}

function regionalFeatures(network) {
  const colours = new Map(network.lines.map((l) => [l.code, l.colour || "#4b5563"]));
  const lines = featureCollection(network.lines.flatMap((l) => l.paths.map((path) => ({ type: "Feature", geometry: { type: "LineString", coordinates: path }, properties: { code: l.code, colour: l.colour || "#4b5563" } }))));
  const stations = featureCollection(network.stations.map((s) => point([s.lon, s.lat], { id: s.id, name: s.name, colour: colours.get(s.lines[0]) || "#4b5563", major: s.lines.length > 1 })));
  return { lines, stations };
}

// A rounded pill drawn once and tinted per line (SDF), with the line code on top.
function addRegionalPill(map) {
  const [w, h, r] = [56, 32, 12];
  const canvas = el("canvas", { width: w, height: h });
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.beginPath();
  ctx.roundRect(4, 4, w - 8, h - 8, r);
  ctx.fill();
  map.addImage("regional-pill", ctx.getImageData(0, 0, w, h), { sdf: true, pixelRatio: 2 });
}

function addRegionalLayers(map) {
  const c = prefersDark() ? { casing: "#14161b", fill: "#1c1f25", label: "#eef1f8", halo: "#14161b" } : { casing: "#ffffff", fill: "#ffffff", label: "#141d3b", halo: "#ffffff" };
  map.addSource("regional-lines", { type: "geojson", data: featureCollection([]) });
  map.addSource("regional-stations", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  addRegionalPill(map);
  const below = "route-path-casing";
  map.addLayer({ id: "regional-line-casing", type: "line", source: "regional-lines", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 16, 8] } }, below);
  map.addLayer({ id: "regional-line", type: "line", source: "regional-lines", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "colour"], "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.4, 16, 5], "line-opacity": 0.85 } }, below);
  map.addLayer({ id: "regional-stations", type: "circle", source: "regional-stations", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, ["case", ["get", "major"], 3.5, 2.5], 16, ["case", ["get", "major"], 8, 6.5]],
    "circle-color": c.fill, "circle-stroke-color": ["get", "colour"], "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 8, 1.4, 16, 3],
  } }, "outages");
  map.addLayer({ id: "regional-trains", type: "symbol", source: "moving", filter: ["==", ["get", "moving"], "regional"], minzoom: SHOWN_FROM_ZOOM.regional, layout: {
    "icon-image": "regional-pill", "icon-size": ["interpolate", ["linear"], ["zoom"], 8, 0.75, 14, 1.15], "icon-allow-overlap": true, "icon-ignore-placement": true,
    "text-field": ["get", "line"], "text-font": ["Noto Sans Medium"], "text-size": ["interpolate", ["linear"], ["zoom"], 8, 7.5, 14, 11], "text-allow-overlap": true, "text-ignore-placement": true,
  }, paint: { "icon-color": ["get", "colour"], "icon-halo-color": c.casing, "icon-halo-width": 1.5, "text-color": "#ffffff" } }, "trains-halo");
  map.addLayer({ id: "regional-station-label", type: "symbol", source: "regional-stations", minzoom: 9, filter: ["any", ["get", "major"], [">=", ["zoom"], 10.5]], layout: {
    "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": ["interpolate", ["linear"], ["zoom"], 9, 10, 16, 13],
    "text-anchor": "left", "text-offset": [0.8, 0], "text-optional": true, "text-max-width": 9,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 2, "text-opacity": 0.85 } });
}

// "3 min late", "On time" or "1 min early" for a live train or departure.
function regionalDelay(minutes) {
  if (minutes === null || minutes === undefined) return null;
  if (minutes >= 1) return el("span", { class: "warn" }, `${minutes} min late`);
  return minutes <= -1 ? `${-minutes} min early` : "On time";
}

function regionalAlerts(alerts) {
  return alerts && alerts.length
    ? el("ul", { class: "sheet-list" }, alerts.map((a) => el("li", {}, el("strong", {}, a.header), a.description ? el("p", { class: "muted small" }, a.description) : null)))
    : null;
}

function regionalTrainSheet(view, id) {
  const data = view.regionalTrains || {};
  const t = (data.trains || []).find((x) => x.id === id);
  if (!t) return null;
  const alerts = (data.alerts || []).filter((a) => a.lines.includes(t.line));
  return [
    el("p", { class: "sheet-kicker" }, "Regional train"),
    el("h2", { class: "with-badge" }, regionalPill(t.line, t.colour), `${t.line_name} towards ${t.destination}`),
    el("p", {}, t.at_station ? `At ${t.at_station}` : t.next_station ? `Next: ${t.next_station}` : "Arriving", t.next_arrival && !t.at_station ? el("span", { class: "muted" }, ` · ${t.live ? "expected" : "scheduled"} ${timeFmt.format(new Date(t.next_arrival))}`) : ""),
    t.live ? el("p", { class: "with-dot" }, el("span", { class: "live-dot", "aria-hidden": "true" }), el("span", {}, "Live", t.delay_minutes !== null ? [" · ", regionalDelay(t.delay_minutes)] : "")) : null,
    alerts.length ? el("p", { class: "sheet-label" }, "Service alerts") : null,
    regionalAlerts(alerts),
    el("p", { class: "muted small" }, t.live ? "Position from Metrolinx's live report, moved along the track by the timetable between reports." : "No live report for this train right now, so it is placed by the published timetable."),
  ];
}

function regionalStationSheet(view, id, data) {
  const s = ((view.regional || {}).stations || []).find((x) => x.id === id);
  if (!data) {
    return [el("p", { class: "sheet-kicker" }, "Regional rail station"), el("h2", {}, s ? s.name : id), el("p", { class: "muted" }, "Loading departures…")];
  }
  return [
    el("p", { class: "sheet-kicker" }, "Regional rail station"),
    el("h2", {}, data.station.name),
    el("p", { class: "route-pills" }, data.station.lines.map((l) => el("span", { class: "with-badge small" }, regionalPill(l.code, l.colour, { small: true }), l.name))),
    data.alerts && data.alerts.length ? el("p", { class: "sheet-label" }, "Service alerts") : null,
    regionalAlerts(data.alerts),
    el("p", { class: "sheet-label" }, "Departures"),
    data.departures.length
      ? el("ul", { class: "sheet-list" }, data.departures.map((d) => el("li", { class: "arrival-row" },
        el("span", { class: "with-badge" }, regionalPill(d.line, d.colour, { small: true }), el("span", {}, d.destination, d.cancelled ? el("span", { class: "warn" }, " · Cancelled") : d.delay_minutes ? [" · ", regionalDelay(d.delay_minutes)] : "")),
        el("span", { class: "mins with-dot" }, d.live && !d.cancelled ? el("span", { class: "live-dot", "aria-hidden": "true" }) : null, d.minutes < 1 ? "Now" : d.minutes < 60 ? `${d.minutes} min` : timeFmt.format(new Date(d.departs))))))
      : el("p", { class: "muted" }, "No departures in the next three hours."),
    el("p", { class: "muted small" }, data.source === "live" ? "Times marked live are Metrolinx's predictions; the rest follow the published timetable." : "From the published timetable: no live reports for these trains right now."),
    s ? planHere(view, [s.lon, s.lat], s.name) : null,
  ];
}

async function loadRegionalStationSheet(view, id) {
  try {
    const data = await api(`/api/regional/stations/${encodeURIComponent(id)}`);
    if (view.selected && view.selected.kind === "regional-station" && view.selected.id === id) fill(view.sheetBody, regionalStationSheet(view, id, data));
  } catch {
    if (view.selected && view.selected.id === id) fill(view.sheetBody, el("p", { class: "muted" }, "This station could not be loaded right now."));
  }
}

// Road restrictions from the City of Toronto. Orange, dashed for full closures; a
// restriction outside its working hours right now is drawn faded.
const ROAD_COLOURS = { closure: "#ea580c", construction: "#f59e0b" };
const ROAD_LABELS = { closure: "Road closed", construction: "Construction" };
const DAY_KEYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
const longDayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", month: "short", day: "numeric", year: "numeric" });

function roadFeatures(data) {
  return featureCollection(data.restrictions.map((r) => ({
    type: "Feature",
    geometry: r.path.length > 1 ? { type: "LineString", coordinates: r.path } : { type: "Point", coordinates: r.path[0] },
    properties: { id: r.restriction_id, kind: r.kind, now: r.current_impact && r.current_impact !== "None" },
  })));
}

function addRoadLayers(map) {
  map.addSource("roads", { type: "geojson", data: featureCollection([]) });
  const colour = ["match", ["get", "kind"], "closure", ROAD_COLOURS.closure, ROAD_COLOURS.construction];
  const opacity = ["case", ["get", "now"], 0.95, 0.45];
  const width = ["interpolate", ["linear"], ["zoom"], 12.5, 2, 17, 6];
  const line = (kind) => ["all", ["==", ["geometry-type"], "LineString"], ["==", ["get", "kind"], kind]];
  // Dash patterns cannot vary per feature, so closures and construction are two layers.
  map.addLayer({ id: "road-work-line", type: "line", source: "roads", minzoom: 12.5, filter: line("construction"), layout: { "line-cap": "round" }, paint: { "line-color": colour, "line-opacity": opacity, "line-width": width } }, "regional-line-casing");
  map.addLayer({ id: "road-closure-line", type: "line", source: "roads", minzoom: 12.5, filter: line("closure"), paint: { "line-color": colour, "line-opacity": opacity, "line-width": width, "line-dasharray": [1.4, 0.8] } }, "regional-line-casing");
  map.addLayer({ id: "road-closure-point", type: "circle", source: "roads", minzoom: 12.5, maxzoom: 14.5, filter: ["==", ["geometry-type"], "Point"], paint: {
    "circle-color": colour, "circle-opacity": opacity, "circle-radius": ["interpolate", ["linear"], ["zoom"], 12.5, 2.5, 14.5, 4],
  } }, "regional-line-casing");
  const icon = {
    "icon-image": ["match", ["get", "kind"], "closure", "badge-closure", "badge-construction"],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 14.5, 0.8, 17, 1], "icon-allow-overlap": false, "icon-padding": 4,
  };
  const iconPaint = { "icon-opacity": ["case", ["get", "now"], 1, 0.6] };
  // Placement cannot vary per feature: one icon at the middle of each line, one per point.
  map.addLayer({ id: "road-icons", type: "symbol", source: "roads", minzoom: 14.5, filter: ["==", ["geometry-type"], "LineString"], layout: { "symbol-placement": "line-center", ...icon }, paint: iconPaint });
  map.addLayer({ id: "road-icons-point", type: "symbol", source: "roads", minzoom: 14.5, filter: ["==", ["geometry-type"], "Point"], layout: icon, paint: iconPaint });
}

function scheduleToday(schedule) {
  const today = DAY_KEYS[new Date(new Date().toLocaleString("en-US", { timeZone: "America/Toronto" })).getDay()];
  return schedule.everyday || schedule[today] || null;
}

function roadSheet(view, id) {
  const r = ((view.roads || {}).restrictions || []).find((x) => x.restriction_id === id);
  if (!r) return null;
  const hours = scheduleToday(r.schedule);
  const description = (r.description || "").replace(/^Toronto-TMC:\s*/, "");
  return [
    el("p", { class: "sheet-kicker" }, ROAD_LABELS[r.kind]),
    el("h2", {}, r.name),
    el("p", {}, r.current_impact && r.current_impact !== "None" ? `In effect now · ${r.current_impact.toLowerCase()} impact` : "Not in effect right now", hours ? el("span", { class: "muted" }, ` · today ${hours}`) : ""),
    description ? el("p", { class: "muted" }, description.length > 260 ? `${description.slice(0, 257)}…` : description) : null,
    r.routes.length
      ? el("div", { class: "sheet-block" }, el("p", { class: "sheet-label" }, "Along these bus and streetcar routes"), el("p", { class: "route-pills" }, r.routes.sort((a, b) => a.localeCompare(b, "en", { numeric: true })).map((id) => routePill(view, id))))
      : null,
    el("p", { class: "muted small" }, `${r.directions === "ONE_DIRECTION" ? "One direction" : r.directions === "BOTH_DIRECTIONS" ? "Both directions" : ""}${r.directions ? " · " : ""}Until ${longDayFmt.format(new Date(r.ends_at))}. City of Toronto road restrictions, refreshed every 10 minutes.`),
  ];
}

// The city's cycling network, greens by how separated from traffic each segment is, with
// trails dashed. Built by `transit map-tiles` into /map-data; absent until then.
const LANE_COLOURS = { protected: "#15803d", lane: "#22c55e", trail: "#0d9488", shared: "#86efac" };
const LANE_LABELS = { protected: "Protected cycle track", lane: "Painted bike lane", trail: "Multi-use trail", shared: "Shared route (sharrows or signs)" };

async function addLaneLayers(view) {
  const map = view.map;
  map.addSource("lanes", { type: "geojson", data: featureCollection([]) });
  const colour = ["match", ["get", "kind"], ...Object.entries(LANE_COLOURS).flat(), "#22c55e"];
  const width = ["interpolate", ["linear"], ["zoom"], 11, ["match", ["get", "kind"], "shared", 0.8, 1.6], 17, ["match", ["get", "kind"], "shared", 2.5, 5]];
  map.addLayer({ id: "lanes", type: "line", source: "lanes", minzoom: 10.5, filter: ["!=", ["get", "kind"], "trail"], layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": colour, "line-width": width, "line-opacity": 0.85 } }, "road-work-line");
  map.addLayer({ id: "lanes-trail", type: "line", source: "lanes", minzoom: 10.5, filter: ["==", ["get", "kind"], "trail"], paint: { "line-color": colour, "line-width": width, "line-opacity": 0.85, "line-dasharray": [2, 1.2] } }, "road-work-line");
  map.addSource("flows", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "flows", type: "line", source: "flows", layout: { "line-cap": "round" }, paint: {
    "line-color": "#7c3aed", "line-opacity": 0.8,
    "line-width": ["interpolate", ["linear"], ["get", "share"], 0, 1.5, 1, 7],
  } }, "docks");
  applyLayerToggles(map);
  try {
    const response = await fetch("/map-data/bike-lanes.geojson");
    if (response.ok && activeMap === view) map.getSource("lanes").setData(await response.json());
  } catch {
    /* No bike lane layer built yet; everything else works. */
  }
}

function laneSheet(feature) {
  const p = feature.properties;
  return [
    el("p", { class: "sheet-kicker" }, "Cycling network"),
    el("h2", {}, p.street || LANE_LABELS[p.kind]),
    el("p", {}, LANE_LABELS[p.kind], p.infra && p.infra !== "null" ? el("span", { class: "muted" }, ` · ${p.infra}`) : ""),
    p.installed && p.installed !== "null" ? el("p", { class: "muted small" }, `Installed ${p.installed}. City of Toronto cycling network.`) : el("p", { class: "muted small" }, "City of Toronto cycling network."),
  ];
}

// Car parks, their entrances and bike parking from OpenStreetMap, built by `transit
// osm-layers` into /map-data. Each file loads the first time its layer is switched on.
const PARKING_KINDS = { surface: "Surface lot", underground: "Underground garage", "multi-storey": "Parking garage", rooftop: "Rooftop parking" };
const RACK_KINDS = { stands: "Bike stands", wall_loops: "Wheel racks", rack: "Bike rack", shed: "Bike shelter", lockers: "Bike lockers", building: "Indoor bike parking", "two-tier": "Two-tier racks", bollard: "Bike bollards", anchors: "Ground anchors" };
const OSM_SOURCES = { parking: ["parking", "parking.geojson", "Parking"], bikeParking: ["bike-parking", "bike-parking.geojson", "Bike parking"] };

function addOsmLayers(view) {
  const map = view.map;
  const c = prefersDark() ? { label: "#eef1f8", halo: "#14161b" } : { label: "#141d3b", halo: "#ffffff" };
  map.addSource("parking", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addSource("bike-parking", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addLayer({ id: "bike-parking", type: "symbol", source: "bike-parking", minzoom: 14.5, layout: {
    "icon-image": "badge-rack", "icon-size": ["interpolate", ["linear"], ["zoom"], 14.5, 0.6, 18, 1], "icon-allow-overlap": ["step", ["zoom"], false, 16.5, true],
  } }, "outages");
  map.addLayer({ id: "parking", type: "symbol", source: "parking", minzoom: 14.5, filter: ["==", ["get", "role"], "lot"], layout: {
    "icon-image": "badge-parking", "icon-size": ["interpolate", ["linear"], ["zoom"], 14.5, 0.7, 17, 1], "icon-allow-overlap": ["step", ["zoom"], false, 15.5, true],
    "text-field": ["step", ["zoom"], "", 16, ["coalesce", ["get", "name"], ["get", "operator"], ""]], "text-font": ["Noto Sans Medium"], "text-size": 11,
    "text-anchor": "top", "text-offset": [0, 1.1], "text-optional": true, "text-max-width": 8,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 1.6 } }, "outages");
  map.addLayer({ id: "parking-entrances", type: "symbol", source: "parking", minzoom: 15.5, filter: ["==", ["get", "role"], "entrance"], layout: {
    "icon-image": "badge-parking-entrance", "icon-size": ["interpolate", ["linear"], ["zoom"], 15.5, 0.75, 18, 1], "icon-allow-overlap": true,
  } }, "outages");
  view.osm = {};
  applyLayerToggles(map);
  loadOsmLayers(view);
}

async function loadOsmLayers(view) {
  for (const [key, [source, file, label]] of Object.entries(OSM_SOURCES)) {
    if (!mapState.layers[key] || view.osm[key]) continue;
    view.osm[key] = "loading";
    try {
      const response = await fetch(`/map-data/${file}`);
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      if (activeMap !== view) return;
      view.osm[key] = data;
      view.map.getSource(source).setData(data);
    } catch {
      view.osm[key] = null; // Not built yet; the next switch-on tries again.
      if (activeMap === view) fill(view.status, el("span", { class: "warn" }, `${label} layer not built yet (docs/map.md)`));
    }
  }
}

function osmFeatures(view, key) {
  const data = view.osm && view.osm[key];
  return data && data.features ? data.features : [];
}

function sentence(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function tag(text, kind = "") {
  return el("span", { class: kind ? `tag ${kind}` : "tag" }, text);
}

function entranceTags(e) {
  return [
    e.step_free ? tag("Step-free", "ok") : e.no_step_free ? tag("Stairs or escalator") : null,
    e.automatic ? tag("Automatic, no staff") : null,
    e.exit_only ? tag("Exit only", "warn") : null,
  ];
}

function doorText(e) {
  return e.where ? `${e.name} (${e.where})` : e.name;
}

function entranceSheet(view, id) {
  const [key, entranceId] = id.split("|");
  const s = view.data.stations.find((x) => x.key === key);
  const e = s && (s.entrances || []).find((x) => x.id === entranceId);
  if (!e) return null;
  return [
    el("p", { class: "sheet-kicker" }, `${s.name} station entrance`),
    el("h2", {}, e.name),
    e.where ? el("p", {}, sentence(e.where)) : null,
    el("p", { class: "entrance-tags" }, entranceTags(e)),
    s.outages.length ? el("p", { class: "warn" }, `${plural(s.outages.length, "elevator or escalator", "elevators or escalators")} out of service at this station`) : null,
    el("p", { class: "sheet-actions" }, el("button", { type: "button", class: "button secondary", "data-station": s.key }, `${s.name} station`)),
    el("p", { class: "muted small" }, "Entrances from OpenStreetMap. Step-free means an elevator or ramp is mapped from the street."),
  ];
}

function parkingSheet(view, id) {
  const features = osmFeatures(view, "parking");
  const lot = features.find((f) => f.properties.role === "lot" && f.properties.id === id);
  if (!lot) return null;
  const p = lot.properties;
  const doors = features.filter((f) => f.properties.role === "entrance" && f.properties.lot === id);
  const title = p.name || p.operator || PARKING_KINDS[p.kind] || "Parking";
  const facts = [
    p.fee === true ? "Paid" : p.fee === false ? "Free" : null,
    p.capacity ? plural(p.capacity, "space", "spaces") : null,
    p.maxheight ? `Height limit ${p.maxheight}${/^[\d.]+$/.test(p.maxheight) ? " m" : ""}` : null,
    p.park_ride ? "Park and ride" : null,
  ].filter(Boolean);
  const doorWhere = (w) => (w ? `Entrance ${w.startsWith("on ") ? w : `on the ${w}`}` : "Entrance off a lane or driveway");
  return [
    el("p", { class: "sheet-kicker" }, PARKING_KINDS[p.kind] || "Parking"),
    el("h2", {}, title),
    p.name && p.operator ? el("p", { class: "muted" }, p.operator) : null,
    facts.length ? el("p", {}, facts.join(" · ")) : null,
    p.hours ? el("p", { class: "muted small" }, `Hours: ${p.hours}`) : null,
    doors.length
      ? el("div", { class: "sheet-block" }, el("p", { class: "sheet-label" }, plural(doors.length, "entrance", "entrances")),
          el("ul", { class: "plain" }, doors.map((d) => el("li", {}, doorWhere(d.properties.where)))))
      : el("p", { class: "muted small" }, `No entrance is mapped${p.where ? `; the car park is ${p.where}` : ""}.`),
    planHere(view, lot.geometry.coordinates, title),
    el("p", { class: "muted small" }, "From OpenStreetMap. Rates and opening times change; check the signs at the entrance."),
  ];
}

function rackSheet(view, id) {
  const f = osmFeatures(view, "bikeParking").find((x) => x.properties.id === id);
  if (!f) return null;
  const p = f.properties;
  return [
    el("p", { class: "sheet-kicker" }, "Bike parking"),
    el("h2", {}, p.name || RACK_KINDS[p.kind] || "Bike parking"),
    el("p", {}, [p.capacity ? plural(p.capacity, "space", "spaces") : "Number of spaces not mapped", p.covered ? "Covered" : null, p.fee === true ? "Paid" : null].filter(Boolean).join(" · ")),
    p.operator ? el("p", { class: "muted small" }, p.operator) : null,
    el("p", { class: "muted small" }, "From OpenStreetMap."),
  ];
}

function placeSheet(view, index) {
  const item = (view.placeResults || [])[index];
  if (!item) return null;
  return [
    el("p", { class: "sheet-kicker" }, item.detail || "Place"),
    el("h2", {}, item.name),
    saveButton("street", `${item.coord[1].toFixed(3)},${item.coord[0].toFixed(3)}`, item.name),
    streetBlock(view, item.coord),
    planHere(view, item.coord, item.name),
    el("p", { class: "muted small" }, "From OpenStreetMap."),
  ];
}

const wasteDayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", weekday: "long", month: "short", day: "numeric" });
const wasteDay = (d) => (d === torontoDay(new Date()) ? "today" : wasteDayFmt.format(new Date(`${d}T00:00:00Z`)));
const roadEndFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", month: "short", day: "numeric", year: "numeric" });

// My street at a place: curbside collection, road work within 500 m and the ward's most
// common 311 requests, filled in when /api/street answers.
function streetBlock(view, [lon, lat]) {
  const block = el("div", { class: "street" });
  api(`/api/street?at=${lat.toFixed(5)},${lon.toFixed(5)}`)
    .then((s) => {
      const c = s.collection;
      const r = s.requests;
      fill(block,
        c ? el("div", { class: "sheet-block" },
          el("p", { class: "sheet-label" }, "Curbside collection"),
          c.calendar
            ? el("ul", { class: "sheet-list" }, c.upcoming.map((u) => el("li", { class: "arrival-row" }, el("span", {}, wasteDay(u.collected_on)), el("span", { class: "mins" }, u.items.join(", ")))))
            : el("p", {}, "Night collection area: the city's daytime calendar does not apply here."),
          c.calendar ? el("p", { class: "muted small" }, `Calendar ${c.calendar.replace(/(\d)$/, " $1")} for houses on curbside collection; apartments and businesses may differ.`) : null) : null,
        s.road_work.length ? el("div", { class: "sheet-block" },
          el("p", { class: "sheet-label" }, "Road work within 500 m"),
          el("ul", { class: "sheet-list" }, s.road_work.slice(0, 4).map((w) => el("li", { class: "arrival-row" }, el("span", {}, w.name), el("span", { class: "mins" }, `${w.metres} m · until ${roadEndFmt.format(new Date(w.ends_at))}`))))) : null,
        s.ward && r && r.top.length ? el("div", { class: "sheet-block" },
          el("p", { class: "sheet-label" }, `311 requests in Ward ${s.ward.number}, ${s.ward.name}`),
          el("p", { class: "muted small" }, `${numberFmt.format(r.total)} in the ${r.days} days to ${inspectedOn(r.through)}; the most common:`),
          el("ul", { class: "sheet-list" }, r.top.map((t) => el("li", { class: "arrival-row" }, el("span", {}, t.type), el("span", { class: "mins" }, numberFmt.format(t.requests)))))) : null,
        s.ward || c ? el("p", { class: "muted small" }, "City of Toronto Open Data. Saving keeps this place, rounded to about 100 m, on this device only.") : null);
    })
    .catch(() => {});
  return block;
}

function bikeParkingText(b) {
  return `Bike parking within 250 m: ${plural(b.places, "place", "places")}${b.spaces ? `, about ${b.spaces} spaces` : ""}${b.covered ? `, ${b.covered} covered` : ""}.`;
}

const tripMonthFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", month: "long", year: "numeric" });

function hourBars(hours) {
  const max = Math.max(1, ...hours.map((h) => h.departures + h.arrivals));
  const busiest = hours.reduce((a, b) => (b.departures + b.arrivals > a.departures + a.arrivals ? b : a), hours[0]);
  return el(
    "div",
    { class: "hour-bars", role: "img", "aria-label": `Weekday trips by hour; busiest ${busiest ? hourLabel(busiest.hour) : ""}` },
    hours.map((h) => el("span", { style: { height: `${Math.max(4, (100 * (h.departures + h.arrivals)) / max)}%` }, title: `${hourLabel(h.hour)}: ${numberFmt.format(h.departures)} out, ${numberFmt.format(h.arrivals)} in` })),
  );
}

function dockTripsBlock(view, id, data) {
  if (!data) return null;
  const t = data.totals;
  const busiest = data.weekday_hours.length ? data.weekday_hours.reduce((a, b) => (b.departures + b.arrivals > a.departures + a.arrivals ? b : a)) : null;
  return el(
    "div",
    { class: "sheet-block" },
    el("p", { class: "sheet-label" }, `Trips in the 12 months to ${tripMonthFmt.format(new Date(`${data.through}T00:00:00Z`))}`),
    el("p", {}, `${numberFmt.format(t.departures)} started here, ${numberFmt.format(t.arrivals)} ended here`, el("span", { class: "muted small" }, ` · ${Math.round((100 * t.member_departures) / Math.max(1, t.departures))}% members, ${Math.round((100 * t.ebike_departures) / Math.max(1, t.departures))}% e-bikes`)),
    data.weekday_hours.length ? [hourBars(data.weekday_hours), el("p", { class: "muted small" }, `Weekdays by hour; busiest at ${hourLabel(busiest.hour)}`)] : null,
    data.destinations.length
      ? [
          el("p", { class: "sheet-label" }, `Top destinations in ${data.destinations_year}`),
          el("ul", { class: "sheet-list" }, data.destinations.map((d) => el("li", { class: "arrival-row" }, d.lat ? el("button", { type: "button", class: "linkish", "data-dock": d.station_id }, d.name) : el("span", {}, d.name), el("span", { class: "mins" }, `${numberFmt.format(d.trips)}${d.typical_minutes ? ` · ${d.typical_minutes} min` : ""}`)))),
        ]
      : null,
  );
}

function showFlows(view, id, data) {
  const source = view.map.getSource("flows");
  if (!source) return;
  const from = view.data.bike_share.docks.find((d) => d.id === id);
  const destinations = (data && data.destinations.filter((d) => d.lat)) || [];
  if (!from || !destinations.length) return source.setData(featureCollection([]));
  const max = Math.max(...destinations.map((d) => d.trips));
  source.setData(featureCollection(destinations.map((d) => ({ type: "Feature", geometry: { type: "LineString", coordinates: [[from.lon, from.lat], [d.lon, d.lat]] }, properties: { share: d.trips / max } }))));
}

async function loadDockTrips(view, id) {
  try {
    const data = await api(`/api/docks/${encodeURIComponent(id)}/trips`);
    if (!view.selected || view.selected.kind !== "dock" || view.selected.id !== id) return;
    const block = dockTripsBlock(view, id, data);
    if (block) view.sheetBody.append(block);
    showFlows(view, id, data);
  } catch {
    /* No published trips for this dock (new, or not in the city's files). */
  }
}

// City of Toronto festivals and events: today's in a solid marker, the rest of the week
// hollow. The city's calendar has no professional sports and few arena concerts.
const EVENT_COLOUR = "#141d3b";
const eventDayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", weekday: "short", month: "short", day: "numeric" });
const NEARBY_EVENT_M = 700;

function torontoDay(date) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto" }).format(date);
}

function eventFeatures(data) {
  const today = torontoDay(new Date());
  return featureCollection(data.events.map((e) => point([e.lon, e.lat], { id: e.event_id, today: e.dates.some((d) => torontoDay(new Date(d)) === today), featured: e.featured })));
}

function addEventLayers(view) {
  const map = view.map;
  map.addSource("events", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addLayer({ id: "events", type: "symbol", source: "events", minzoom: 12, layout: {
    "icon-image": ["case", ["get", "today"], "badge-event-today", "badge-event-week"],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 12, ["case", ["get", "featured"], 0.75, 0.6], 16, ["case", ["get", "featured"], 1.1, 0.95]],
    "icon-allow-overlap": true, "symbol-sort-key": ["case", ["get", "today"], 0, 1],
  } }, "trains-halo");
  refreshEvents(view);
}

async function refreshEvents(view) {
  try {
    view.events = await api("/api/events");
    view.map.getSource("events").setData(eventFeatures(view.events));
  } catch {
    /* Events are extra context; the map works without them. */
  }
}

// Toronto Public Health inspections of food premises (DineSafe), coloured by the latest
// result. Loaded the first time the layer is switched on (about 20,000 premises).
function addFoodLayers(view) {
  const map = view.map;
  map.addSource("food", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addLayer({ id: "food", type: "symbol", source: "food", minzoom: 15, layout: {
    "icon-image": ["concat", "badge-food-", ["get", "level"]],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 15, 0.75, 18, 1],
    "icon-allow-overlap": true, "symbol-sort-key": ["-", 3, ["get", "rank"]],
  } }, "trains-halo");
  // The food map: places to eat in numbered clusters up to zoom 14, then each with its own
  // icon, in colour only when its latest inspection was not a pass; names from zoom 16.
  const c = basemapColours(prefersDark());
  map.addSource("eats", { type: "geojson", data: featureCollection([]), cluster: true, clusterMaxZoom: 14, clusterRadius: 44 });
  map.addSource("food-other", { type: "geojson", data: featureCollection([]), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addLayer({ id: "eats-cluster", type: "circle", source: "eats", filter: ["has", "point_count"], paint: {
    "circle-color": EAT_COLOUR, "circle-opacity": 0.9,
    "circle-radius": ["step", ["get", "point_count"], 13, 50, 17, 250, 22, 1000, 27],
    "circle-stroke-color": mapCasing(), "circle-stroke-width": 2,
  } }, "trains-halo");
  map.addLayer({ id: "eats-count", type: "symbol", source: "eats", filter: ["has", "point_count"], layout: {
    "text-field": ["get", "point_count_abbreviated"], "text-font": ["Noto Sans Medium"], "text-size": 12, "text-allow-overlap": true,
  }, paint: { "text-color": "#ffffff" } }, "trains-halo");
  map.addLayer({ id: "food-other", type: "symbol", source: "food-other", minzoom: 15, layout: {
    "icon-image": ["concat", "badge-food-", ["get", "level"]], "icon-size": 0.7, "icon-allow-overlap": true,
  } }, "trains-halo");
  map.addLayer({ id: "eats", type: "symbol", source: "eats", filter: ["!", ["has", "point_count"]], layout: {
    "icon-image": ["concat", "badge-eat-", ["get", "k"], ["get", "flag"]],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 14, 0.8, 18, 1.05],
    "icon-allow-overlap": true, "symbol-sort-key": ["case", ["==", ["get", "flag"], ""], 0, 1],
  } }, "trains-halo");
  map.addSource("eats-notable", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "eats-notable", type: "symbol", source: "eats-notable", layout: {
    "icon-image": ["concat", "badge-eat-", ["get", "k"], ["case", ["==", ["get", "flag"], ""], "-notable", ["get", "flag"]]],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.75, 16, 1],
    "icon-allow-overlap": true,
    "text-field": ["step", ["zoom"], "", 13, ["get", "label"]], "text-font": ["Noto Sans Medium"], "text-size": 12, "text-anchor": "top", "text-offset": [0, 1.4],
    "text-max-width": 9, "text-optional": true,
  }, paint: { "text-color": prefersDark() ? "#fde68a" : "#713f12", "text-halo-color": c.halo, "text-halo-width": 1.8 } }, "trains-halo");
  map.addLayer({ id: "eats-label", type: "symbol", source: "eats", minzoom: 16, filter: ["!", ["has", "point_count"]], layout: {
    "text-field": ["get", "label"], "text-font": ["Noto Sans Medium"], "text-size": 11.5, "text-anchor": "top", "text-offset": [0, 1.2],
    "text-max-width": 9, "text-optional": true,
  }, paint: { "text-color": prefersDark() ? "#eef1f8" : "#141d3b", "text-halo-color": c.halo, "text-halo-width": 1.6 } }, "trains-halo");
  applyLayerToggles(map);
  loadFood(view);
}

async function loadEats(view) {
  if (view.eats) return showEats(view);
  if (view.eatsLoading) return;
  view.eatsLoading = true;
  try {
    view.eats = await api("/api/food/places");
    view.notableByPid = new Map();
    for (const p of view.eats.places) if (p[4] && p[8] != null) view.notableByPid.set(p[4], view.eats.notable[p[8]]);
    for (const p of view.eats.places) if (p[4]) rememberFood(view, p[4], [p[0], p[1]]);
    for (const p of view.eats.other) rememberFood(view, p[0], [p[2], p[1]]);
    showEats(view);
  } catch {
    /* Tried again the next time the food map opens. */
  } finally {
    view.eatsLoading = false;
  }
}

// The food map's places after its filters: kinds, clean records only, other premises.
function showEats(view) {
  if (!view.eats || !view.map) return;
  const f = mapState.food;
  const flag = (code) => (code === 1 ? "-conditional" : code === 2 ? "-closed" : "");
  const features = [];
  const notable = [];
  const long = new Date().getFullYear() - LONG_RUNNING;
  view.eats.places.forEach(([lon, lat, name, k, pid, code, clean, since, star], i) => {
    const old = since && since <= long;
    if (!f.kinds[k] || (f.clean && !clean) || (f.long && !old) || (f.notable && star == null)) return;
    const label = old ? `${name}\nsince ${since}` : name;
    (star == null ? features : notable).push(point([lon, lat], { id: i, k, name, label, pid: pid || "", flag: pid ? flag(code) : "" }));
  });
  view.map.getSource("eats")?.setData(featureCollection(features));
  view.map.getSource("eats-notable")?.setData(featureCollection(notable));
  const other = f.other ? view.eats.other.filter((p) => !f.clean || p[4]).map(([id, lat, lon, code]) => point([lon, lat], { id, level: FOOD_LEVELS[code] || "other" })) : [];
  view.map.getSource("food-other")?.setData(featureCollection(other));
}

function eatSheet(view, i) {
  const p = view.eats && view.eats.places[i];
  if (!p) return null;
  const [lon, lat, name, k, , , , , star] = p;
  return [
    el("p", { class: "sheet-kicker" }, EAT_KINDS[k].one),
    el("h2", {}, name),
    star != null ? notableBlock(view.eats.notable[star]) : null,
    el("p", {}, "No Toronto Public Health inspection matched this place by name. It may be inspected under another name."),
    planHere(view, [lon, lat], name),
    el("p", { class: "muted small" }, "From OpenStreetMap."),
  ];
}

const CLEAN_RECORD = "Latest result a pass, and no significant or crucial infraction in two years";
// Years at one address, under one name, for the food map's long-running filter and labels.
const LONG_RUNNING = 20;

function foodPanel(rerender, view) {
  const f = mapState.food;
  const row = (icon, label, note, on, change) => el("li", { class: on ? "layer on" : "layer" }, icon,
    el("span", { class: "layer-text" }, el("span", { class: "layer-label" }, label), note ? el("span", { class: "muted small" }, note) : null),
    layerSwitch(on, label, () => { change(); showEats(view); applyLayerToggles(view.map); rerender(); }));
  const result = (level, size) => badgeEl("food", FOOD_COLOURS[level], level === "conditional" ? "#141d3b" : "#ffffff", size ? { size } : undefined);
  return [
    el("div", { class: "panel-head" },
      el("p", { class: "panel-heading" }, "Food map"),
      el("button", { type: "button", class: "panel-close", "aria-label": "Close food map filters" }, iconSvg("close", { size: 18 }))),
    el("p", { class: "muted small" }, "Places to eat from OpenStreetMap, with Toronto Public Health's latest inspection and the city's business licences where the names match. Numbers show how many places an area has; zoom in for each place, and closer for names."),
    el("p", { class: "panel-subheading" }, "Places"),
    el("ul", { class: "layer-list" }, EAT_KINDS.map((k, i) => row(badgeEl(k.glyph, k.colour, "#ffffff"), k.label, null, f.kinds[i], () => { f.kinds[i] = !f.kinds[i]; }))),
    el("p", { class: "panel-subheading" }, "Inspections"),
    el("ul", { class: "layer-list" },
      row(result("pass"), "Clean record only", CLEAN_RECORD, f.clean, () => { f.clean = !f.clean; }),
      row(badgeEl("notable", NOTABLE_COLOUR, "#141d3b"), "Notable only", "Places with a Wikipedia article or a Michelin Guide award, from Wikidata; always shown, with a gold ring", f.notable, () => { f.notable = !f.notable; }),
      row(badgeEl("clock", EAT_KINDS[0].colour, "#ffffff"), "Long-running only", `Licensed at the same address under the same name for ${LONG_RUNNING} years or more; their names show the year`, f.long, () => { f.long = !f.long; }),
      row(result("other"), "Other inspected premises", "Grocers, institutional kitchens and places not on the map, close up", f.other, () => { f.other = !f.other; })),
    el("ul", { class: "legend-list compact" },
      legendRow(badgeEl("food", EAT_KINDS[0].colour, "#ffffff", { size: 20 }), "Colour of its kind: passed its latest inspection, or none matched"),
      legendRow(result("conditional", 20), "Latest inspection: conditional pass"),
      legendRow(result("closed", 20), "Latest inspection: closed")),
  ];
}

// The city map: drop-in skating and swimming today at city rinks, pools and community
// centres (transit.recreation). A place is ringed in green while a session is on, and drawn
// hollow once its last session of the day has ended.
const REC_COLOURS = { skate: "#2f6fb8", swim: "#1f9a83" };
const REC_LABELS = { skate: "Skating", swim: "Swimming" };
// Beaches: green when the latest result met Toronto's E. coli standard, red when not, and
// hollow grey out of season (more than three days since any beach was sampled).
const BEACH_COLOURS = { safe: "#2bb39a", unsafe: "#e8336d", off: "#9aa5bf" };
const PATIO_COLOUR = "#f08a3c";
// Today's events on the city map: city festivals and events and ticketed events from the
// Events page's agenda. Events at a major venue (arenas and stadiums) are ringed in yellow
// and named at every zoom.
const MAJOR_COLOUR = "#ffc83d";

function addCityLayers(view) {
  const map = view.map;
  const c = basemapColours(prefersDark());
  map.addSource("rec", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "rec", type: "symbol", source: "rec", layout: {
    "icon-image": ["concat", "badge-rec-", ["get", "kind"], ["get", "state"]],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.75, 16, 1],
    "icon-allow-overlap": true, "symbol-sort-key": ["get", "rank"],
    "text-field": ["step", ["zoom"], "", 13, ["get", "label"]], "text-font": ["Noto Sans Medium"], "text-size": 12, "text-anchor": "top", "text-offset": [0, 1.3],
    "text-max-width": 10, "text-optional": true,
  }, paint: { "text-color": prefersDark() ? "#eef1f8" : "#141d3b", "text-halo-color": c.halo, "text-halo-width": 1.6 } }, "trains-halo");
  map.addSource("agenda", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "agenda", type: "symbol", source: "agenda", layout: {
    "icon-image": ["case", ["get", "major"], "badge-agenda-major", "badge-agenda"],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.8, 16, 1], "icon-allow-overlap": true,
    "symbol-sort-key": ["case", ["get", "major"], 0, 1],
    "text-field": ["step", ["zoom"], ["case", ["get", "major"], ["get", "name"], ""], 15, ["get", "name"]],
    "text-font": ["Noto Sans Medium"], "text-size": ["case", ["get", "major"], 12.5, 11.5], "text-anchor": "top", "text-offset": [0, 1.4],
    "text-max-width": 10, "text-optional": true,
  }, paint: { "text-color": prefersDark() ? "#eef1f8" : "#141d3b", "text-halo-color": c.halo, "text-halo-width": 1.6 } }, "trains-halo");
  map.addSource("patios", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "patios", type: "symbol", source: "patios", minzoom: 13, layout: {
    "icon-image": "badge-patio", "icon-size": ["interpolate", ["linear"], ["zoom"], 13, 0.7, 17, 1], "icon-allow-overlap": true,
    "text-field": ["step", ["zoom"], "", 16, ["get", "name"]], "text-font": ["Noto Sans Medium"], "text-size": 11.5, "text-anchor": "top", "text-offset": [0, 1.2],
    "text-max-width": 9, "text-optional": true,
  }, paint: { "text-color": prefersDark() ? "#eef1f8" : "#141d3b", "text-halo-color": c.halo, "text-halo-width": 1.6 } }, "trains-halo");
  map.addSource("beaches", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "beaches", type: "symbol", source: "beaches", layout: {
    "icon-image": ["concat", "badge-beach-", ["get", "state"]],
    "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.8, 16, 1], "icon-allow-overlap": true,
    "text-field": ["step", ["zoom"], "", 12, ["get", "name"]], "text-font": ["Noto Sans Medium"], "text-size": 12, "text-anchor": "top", "text-offset": [0, 1.3],
    "text-max-width": 10, "text-optional": true,
  }, paint: { "text-color": prefersDark() ? "#eef1f8" : "#141d3b", "text-halo-color": c.halo, "text-halo-width": 1.6 } }, "trains-halo");
  applyLayerToggles(map);
  loadCity(view);
}

async function loadCity(view) {
  if (mapState.kind !== "city" || !view.map) return;
  const today = torontoDay(new Date());
  if (!view.rec || view.rec.day !== today) {
    try {
      [view.rec, view.beaches, view.patios, view.agenda] = await Promise.all([api("/api/recreation"), api("/api/beaches"), api("/api/patios"), api("/api/agenda").catch(() => null)]);
    } catch {
      return; /* Tried again the next time the city map opens. */
    }
  }
  showCity(view);
  if (view.renderChips) view.renderChips(); // The Today chip needs the agenda.
}

const minutesOf = (hhmm) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5));

function nowMinutes() {
  const [h, m] = new Intl.DateTimeFormat("en-GB", { timeZone: "America/Toronto", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date()).split(":");
  return Number(h) * 60 + Number(m);
}

// A place's sessions after the city map's filters.
function recSessions(place) {
  const f = mapState.city;
  return place.sessions.filter((s) => f[s.kind]);
}

function showCity(view) {
  if (!view.rec || !view.map) return;
  const now = nowMinutes();
  const features = [];
  for (const p of view.rec.places) {
    const sessions = recSessions(p);
    if (!sessions.length) continue;
    const on = sessions.find((s) => minutesOf(s.starts) <= now && now < minutesOf(s.ends));
    const next = sessions.find((s) => minutesOf(s.starts) > now);
    if (mapState.city.now && !on) continue;
    const kind = (on || next || sessions[0]).kind;
    const state = on ? "-now" : next ? "" : "-done";
    const label = on ? `${p.name}\n${REC_LABELS[on.kind]} until ${clockText(on.ends)}` : next ? `${p.name}\n${REC_LABELS[next.kind]} at ${clockText(next.starts)}` : p.name;
    features.push(point([p.lon, p.lat], { id: p.id, kind, state, label, rank: on ? 0 : next ? 1 : 2 }));
  }
  view.map.getSource("rec")?.setData(featureCollection(features));
  view.map.getSource("agenda")?.setData(featureCollection(todaysEvents(view).filter((e) => e.lat != null).map((e) =>
    point([e.lon, e.lat], { id: e.id, name: e.name, major: Boolean(e.major) }))));
  view.map.getSource("patios")?.setData(featureCollection(view.patios ? view.patios.patios.map(([lon, lat, name], i) => point([lon, lat], { id: i, name })) : []));
  const b = view.beaches;
  view.map.getSource("beaches")?.setData(featureCollection(!b ? [] : b.beaches.filter((x) => x.recent.length).map((x) =>
    point([x.lon, x.lat], { id: x.id, name: x.name, state: !b.in_season ? "off" : x.recent[0].safe ? "safe" : "unsafe" }))));
  applyLayerToggles(view.map);
}

// Today's agenda events, major ones first, then by start time.
function todaysEvents(view) {
  const today = torontoDay(new Date());
  const all = (view.agenda && view.agenda.events) || [];
  return all.filter((e) => e.date === today && e.status !== "cancelled")
    .sort((a, b) => (b.major - a.major) || (b.featured - a.featured) || String(a.starts_at).localeCompare(String(b.starts_at)));
}

function agendaWhen(e) {
  if (e.all_day || e.ongoing) return e.ongoing && !e.all_day ? `Ongoing, today from ${timeFmt.format(new Date(e.starts_at))}` : "All day";
  if (e.time_tbd) return "Time to be announced";
  return timeFmt.format(new Date(e.starts_at));
}

function agendaRow(view, e) {
  return el("li", { class: "arrival-row" },
    el("button", { type: "button", class: "link-button", "data-agenda": e.id }, e.major ? el("strong", {}, e.name) : e.name),
    el("span", { class: "mins" }, agendaWhen(e)));
}

function agendaSheet(view, id) {
  const e = todaysEvents(view).find((x) => x.id === id);
  if (!e) return null;
  const category = view.agenda.categories[e.category] || "Event";
  return [
    el("p", { class: "sheet-kicker" }, e.major ? `${category} · major venue` : category),
    el("h2", {}, e.name),
    el("p", {}, `Today · ${agendaWhen(e)}`),
    e.venue ? el("p", { class: "muted small" }, [e.venue, e.address].filter(Boolean).join(", ")) : null,
    e.station ? el("p", { class: "small" }, `Nearest station: ${e.station.name}, about ${e.station.walk_min} min walk${e.major ? "; expect crowds before and after" : ""}.`) : null,
    e.lat != null ? planHere(view, [e.lon, e.lat], e.name) : null,
    e.url ? el("p", {}, el("a", { href: e.url, target: "_blank", rel: "noopener" }, e.source === "ticketmaster" ? "Tickets and details" : "Event page")) : null,
    el("p", {}, el("a", { href: "#/events" }, "All events this week")),
  ];
}

// The list behind the "Today" chip: the day's major and featured events, then a count.
function todaySheet(view) {
  const events = todaysEvents(view);
  const big = events.filter((e) => e.major || e.featured);
  return [
    el("p", { class: "sheet-kicker" }, "Happening today"),
    el("h2", {}, big.length ? `${plural(big.length, "big event", "big events")} today` : "Today in Toronto"),
    big.length ? el("ul", { class: "sheet-list" }, big.slice(0, 12).map((e) => agendaRow(view, e))) : el("p", { class: "muted" }, "No events at major venues today."),
    events.length > big.length ? el("p", { class: "small" }, `${plural(events.length - big.length, "other event", "other events")} on the map. `, el("a", { href: "#/events" }, "Browse all events")) : null,
    el("p", { class: "muted small" }, "City of Toronto festivals and events, and ticketed events from Ticketmaster; a major venue is an arena or stadium."),
  ];
}

function patioSheet(view, id) {
  const p = view.patios && view.patios.patios[id];
  if (!p) return null;
  const [lon, lat, name, address, kind] = p;
  return [
    el("p", { class: "sheet-kicker" }, `CaféTO patio${kind ? ` · ${kind.toLowerCase()}` : ""}`),
    el("h2", {}, name),
    address ? el("p", { class: "muted small" }, address) : null,
    planHere(view, [lon, lat], name),
    el("p", { class: "muted small" }, "Licensed under the city's CaféTO program; curb-lane and sidewalk patios are seasonal. City of Toronto Open Data."),
  ];
}

const beachPlace = (view, id) => (view.beaches ? view.beaches.beaches.find((x) => x.id === Number(id)) : null);

function beachSheet(view, id) {
  const b = view.beaches;
  const x = beachPlace(view, id);
  if (!x) return null;
  const [latest] = x.recent;
  const verdict = (d) => el("span", { class: `tag ${d.safe ? "ok" : "bad"}` }, d.safe ? "Safe for swimming" : "Unsafe for swimming");
  return [
    el("p", { class: "sheet-kicker" }, "Beach water quality"),
    el("h2", {}, x.name),
    b.in_season
      ? el("p", {}, verdict(latest), ` ${inspectedOn(latest.day)}`)
      : el("p", {}, `The sampling season is over; the last sample was taken on ${inspectedOn(latest.day)}.`),
    el("ul", { class: "sheet-list" }, x.recent.map((d) => el("li", { class: "arrival-row" }, el("span", {}, inspectedOn(d.day)), el("span", { class: "mins" }, `${numberFmt.format(d.ecoli)} E. coli · `, d.safe ? "met the standard" : "over the standard")))),
    planHere(view, [x.lon, x.lat], x.name),
    el("p", { class: "muted small" }, `E. coli per 100 mL, the geometric mean of the beach's sampling sites; Toronto posts a beach as unsafe above ${b.threshold} (Ontario and Canada use 200). Samples are taken in the morning and results arrive the next day. Toronto Public Health, City of Toronto Open Data.`),
  ];
}

function clockText(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return timeFmt.format(new Date(2000, 0, 1, h, m));
}

const recPlace = (view, id) => (view.rec ? view.rec.places.find((p) => p.id === Number(id)) : null);

function recSheet(view, id) {
  const p = recPlace(view, id);
  if (!p) return null;
  const now = nowMinutes();
  const ages = ([low, high]) => (low && high && high < 99 ? `ages ${low} to ${high}` : low ? `ages ${low}+` : null);
  return [
    el("p", { class: "sheet-kicker" }, "Drop-in today"),
    el("h2", {}, p.name),
    p.address ? el("p", { class: "muted small" }, p.address) : null,
    el("ul", { class: "sheet-list" }, p.sessions.map((s) => {
      const on = minutesOf(s.starts) <= now && now < minutesOf(s.ends);
      const over = minutesOf(s.ends) <= now;
      return el("li", { class: `arrival-row${over ? " muted" : ""}` },
        el("span", {}, badgeEl(s.kind, REC_COLOURS[s.kind], "#ffffff", { size: 18 }), ` ${s.title}`, ages(s.ages) ? el("span", { class: "muted small" }, ` · ${ages(s.ages)}`) : null),
        el("span", { class: "mins" }, on ? `now, until ${clockText(s.ends)}` : `${clockText(s.starts)} to ${clockText(s.ends)}`));
    })),
    planHere(view, [p.lon, p.lat], p.name),
    p.url ? el("p", {}, el("a", { href: p.url, target: "_blank", rel: "noopener" }, "Facility page on toronto.ca")) : null,
    el("p", { class: "muted small" }, "Drop-in programs as the city publishes them, refreshed weekly; check the facility page for closures. City of Toronto Open Data."),
  ];
}

function cityChips(rerender, view) {
  const f = mapState.city;
  const toggle = (key) => () => {
    f[key] = !f[key];
    if (!f.skate && !f.swim) { f.skate = true; f.swim = true; }
    showCity(view);
    rerender();
  };
  return [
    ...Object.entries(REC_LABELS).map(([kind, label]) => chip([badgeEl(kind, REC_COLOURS[kind], "#ffffff", { size: 20 }), label], f[kind], toggle(kind), { class: "chip kind-chip" })),
    chip([badgeEl("beach", BEACH_COLOURS.safe, "#ffffff", { size: 20 }), "Beaches"], f.beach, () => { f.beach = !f.beach; showCity(view); rerender(); }, { class: "chip kind-chip" }),
    chip([badgeEl("event", EVENT_COLOUR, "#ffffff", { size: 20 }), "Events"], f.events, () => { f.events = !f.events; showCity(view); rerender(); }, { class: "chip kind-chip", title: "Today's events" }),
    (() => {
      const today = todaysEvents(view);
      const major = today.filter((e) => e.major || e.featured).length; // As in the Today list.
      return today.length ? chip([badgeEl("event", EVENT_COLOUR, "#ffffff", { size: 20 }), major ? `Today: ${plural(major, "big event", "big events")}` : `Today: ${today.length}`], false, () => openSheet(view, "today"), { class: "chip kind-chip", title: "What is on today" }) : null;
    })(),
    chip([badgeEl("patio", PATIO_COLOUR, "#ffffff", { size: 20 }), "Patios"], f.patio, () => { f.patio = !f.patio; showCity(view); rerender(); }, { class: "chip kind-chip", title: "CaféTO patios, from zoom 13" }),
    chip([badgeEl("clock", "#16a34a", "#ffffff", { size: 20 }), "On now"], f.now, toggle("now"), { class: "chip kind-chip", title: "Only places with a session on now" }),
  ];
}

function cityPanel(rerender, view) {
  return [
    el("div", { class: "panel-head" },
      el("p", { class: "panel-heading" }, "City map"),
      el("button", { type: "button", class: "panel-close", "aria-label": "Close city map filters" }, iconSvg("close", { size: 18 }))),
    el("p", { class: "muted small" }, "Drop-in skating and swimming today at city rinks, pools and community centres. Use the chips under the search to choose what is shown."),
    el("ul", { class: "legend-list compact" },
      legendRow(badgeEl("skate", REC_COLOURS.skate, "#ffffff", { size: 20 }), "Skating later today"),
      legendRow(badgeEl("swim", REC_COLOURS.swim, "#ffffff", { size: 20 }), "Swimming later today"),
      legendRow(badgeEl("clock", "#16a34a", "#ffffff", { size: 20 }), "Green ring: a session is on now"),
      legendRow(badgeEl("swim", REC_COLOURS.swim, "#ffffff", { size: 20, hollow: true }), "Hollow: today's sessions are over"),
      legendRow(badgeEl("beach", BEACH_COLOURS.safe, "#ffffff", { size: 20 }), "Beach: latest sample met the E. coli standard"),
      legendRow(badgeEl("beach", BEACH_COLOURS.unsafe, "#ffffff", { size: 20 }), "Beach: latest sample over the standard"),
      legendRow(badgeEl("beach", BEACH_COLOURS.off, "#ffffff", { size: 20, hollow: true }), "Beach: out of the sampling season"),
      legendRow(badgeEl("patio", PATIO_COLOUR, "#ffffff", { size: 20 }), "CaféTO patio, shown when zoomed in"),
      legendRow(badgeEl("event", EVENT_COLOUR, "#ffffff", { size: 20 }), "Event today; a yellow ring marks a major venue")),
  ];
}

// The weather map: ECCC's rain and snow radar for the last hour, played as a loop, the
// Air Quality Health Index at each Toronto station, and a card with the conditions,
// forecast, warnings and what the findings say this weather tends to do.
const RADAR_STEP_MS = 650;
const AQHI_COLOURS = { Low: "#1f9a83", Moderate: "#d9a21b", High: "#f08a3c", "Very high": "#e8336d" };
const radarTimeFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", hour: "numeric", minute: "2-digit" });
const radarUrl = (kind, at) => `/api/radar/${kind}/${Math.round(Date.parse(at) / 1000)}.png`;
const RADAR_CORNERS = [[-80.1, 44.15], [-78.7, 44.15], [-78.7, 43.25], [-80.1, 43.25]];
// The forecast: ECCC's HRDPS model, hour by hour for the next twelve hours. Precipitation is
// drawn as the model's grid cells; wind as particles that drift with it.
const FORECAST_STEP_MS = 1100;
const PRECIP_STEPS = [0.1, "#a9d0f6", 0.5, "#58a6f0", 2, "#2f6fb8", 5, "#1d2a52", 10, "#e8336d"];
const forecastMode = () => mapState.weatherMode !== "radar";

function addWeatherLayers(view) {
  const map = view.map;
  // Radar layers are added with their first frame (showRadarFrame): an image source needs one.
  map.addSource("precip-grid", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "precip-grid", type: "fill", source: "precip-grid", paint: {
    "fill-color": ["step", ["get", "mm"], ...PRECIP_STEPS.slice(1)],
    "fill-opacity": 0.5, "fill-antialias": false,
  } }, "trains-halo");
  map.addSource("aqhi", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "aqhi-dot", type: "circle", source: "aqhi", paint: {
    "circle-radius": 15, "circle-color": ["get", "colour"], "circle-stroke-color": mapCasing(), "circle-stroke-width": 2,
  } }, "trains-halo");
  map.addLayer({ id: "aqhi-value", type: "symbol", source: "aqhi", layout: {
    "text-field": ["get", "value"], "text-font": ["Noto Sans Medium"], "text-size": 13, "text-allow-overlap": true,
  }, paint: { "text-color": "#ffffff" } }, "trains-halo");
  applyLayerToggles(map);
  if (mapState.kind === "weather") loadWeather(view, { open: true });
}

async function loadWeather(view, { open = false } = {}) {
  if (mapState.kind !== "weather" || !view.map) {
    stopRadar(view);
    stopWind(view);
    return;
  }
  try {
    const [w, air, found] = await Promise.all([api("/api/weather"), api("/api/air").catch(() => null), api("/api/findings").catch(() => null)]);
    view.weather = w.weather;
    view.air = air && air.air;
    view.findings = found ? found.findings : [];
  } catch {
    return; /* Tried again the next time the weather map opens. */
  }
  const stations = (view.air && view.air.stations) || [];
  view.map.getSource("aqhi")?.setData(featureCollection(stations.filter((s) => s.point).map((s) =>
    point(s.point, { value: String(Math.round(s.aqhi)), colour: AQHI_COLOURS[s.risk] || "#6b7280", name: s.name }))));
  const frames = (view.weather && view.weather.radar.frames) || { rain: [], snow: [] };
  view.radarFrames = frames.rain.length >= frames.snow.length ? frames.rain : frames.snow;
  for (const kind of ["rain", "snow"]) for (const at of frames[kind]) new Image().src = radarUrl(kind, at); // Preload the loop.
  view.radarIndex = Math.max(0, view.radarFrames.length - 1);
  const grid = view.weather && view.weather.grid;
  view.gridHours = grid ? grid.hours.filter((h) => Date.parse(h.at) > Date.now() - 3600e3) : [];
  view.gridIndex = 0;
  view.precipCells = new Map();
  showRadarFrame(view);
  showForecastHour(view);
  startWind(view);
  if (!reduceMotion() && mapState.weatherPlaying !== false) startRadar(view);
  if (open && view.weather) openSheet(view, "weather");
  if (view.renderChips) view.renderChips();
}

// Shows frame `view.radarIndex` of each kind: the frame at that time, or the nearest earlier.
function showRadarFrame(view) {
  const frames = (view.weather && view.weather.radar.frames) || { rain: [], snow: [] };
  const at = view.radarFrames && view.radarFrames[view.radarIndex];
  for (const kind of ["rain", "snow"]) {
    const list = frames[kind].filter((t) => !at || Date.parse(t) <= Date.parse(at) + 1000);
    const pick = list[list.length - 1];
    const id = `radar-${kind}`;
    const source = view.map.getSource(id);
    if (pick && !source) {
      view.map.addSource(id, { type: "image", url: radarUrl(kind, pick), coordinates: RADAR_CORNERS });
      view.map.addLayer({ id, type: "raster", source: id, paint: { "raster-opacity": 0.78, "raster-fade-duration": 0 } }, "trains-halo");
      applyLayerToggles(view.map);
    } else if (pick) {
      source.updateImage({ url: radarUrl(kind, pick) });
    }
    if (view.map.getLayer(id)) view.map.setPaintProperty(id, "raster-opacity", pick ? 0.78 : 0);
  }
  const label = view.page.querySelector(".radar-time");
  if (label && !forecastMode()) label.textContent = at ? `Radar ${radarTimeFmt.format(new Date(at))}` : "No radar";
}

// The forecast hour on the map: its precipitation cells, and the wind the particles follow.
function forecastHour(view) {
  const hours = view.gridHours || [];
  return forecastMode() ? hours[view.gridIndex] : hours[0];
}

function showForecastHour(view) {
  const hour = forecastHour(view);
  const grid = view.weather && view.weather.grid;
  if (!grid || !hour) {
    view.map.getSource("precip-grid")?.setData(featureCollection([]));
    return;
  }
  if (!view.precipCells.has(hour.at)) {
    const cells = [];
    hour.precip.forEach((mm, i) => {
      if (mm < 0.1) return;
      const x = grid.west + (i % grid.width) * grid.dx;
      const y = grid.north - Math.floor(i / grid.width) * grid.dy;
      cells.push({ type: "Feature", properties: { mm }, geometry: { type: "Polygon",
        coordinates: [[[x, y], [x + grid.dx, y], [x + grid.dx, y - grid.dy], [x, y - grid.dy], [x, y]]] } });
    });
    view.precipCells.set(hour.at, featureCollection(cells));
  }
  const cells = view.precipCells.get(hour.at);
  view.map.getSource("precip-grid")?.setData(cells);
  const label = view.page.querySelector(".radar-time");
  if (label && forecastMode()) {
    const dry = view.gridHours.every((h) => !h.precip.some((mm) => mm >= 0.1));
    label.textContent = `Forecast ${radarTimeFmt.format(new Date(hour.at))}${dry ? " · dry" : ""}`;
  }
}

// Wind particles on a canvas over the map. Each particle drifts with the forecast wind at its
// position (bilinear in the model grid) at a screen speed proportional to the wind speed, and
// leaves a fading trail; faster wind is drawn in the brand colour. With reduced motion the
// streaks are drawn once, without moving.
const WIND_FRAME_MS = 33;

function windAt(grid, hour, lon, lat) {
  const gx = (lon - grid.west) / grid.dx - 0.5;
  const gy = (grid.north - lat) / grid.dy - 0.5;
  if (gx < 0 || gy < 0 || gx > grid.width - 1 || gy > grid.height - 1) return null;
  const x0 = Math.floor(gx), y0 = Math.floor(gy);
  const x1 = Math.min(x0 + 1, grid.width - 1), y1 = Math.min(y0 + 1, grid.height - 1);
  const fx = gx - x0, fy = gy - y0;
  const mix = (a) => {
    const i = (x, y) => a[y * grid.width + x];
    return (i(x0, y0) * (1 - fx) + i(x1, y0) * fx) * (1 - fy) + (i(x0, y1) * (1 - fx) + i(x1, y1) * fx) * fy;
  };
  return [mix(hour.u), mix(hour.v)];
}

function startWind(view) {
  const grid = view.weather && view.weather.grid;
  if (!grid || !view.map || mapState.weatherWind === false) return stopWind(view);
  const container = view.map.getContainer();
  let canvas = container.querySelector(".wind-canvas");
  if (!canvas) {
    canvas = el("canvas", { class: "wind-canvas", "aria-hidden": "true" });
    container.appendChild(canvas);
  }
  canvas.hidden = false;
  if (view.wind) return; // Already running.
  const ctx = canvas.getContext("2d");
  const dark = prefersDark();
  const colours = dark ? ["rgba(238,241,248,0.5)", "rgba(238,241,248,0.85)", "rgba(255,111,154,0.95)"]
    : ["rgba(76,87,117,0.45)", "rgba(20,29,59,0.75)", "rgba(232,51,109,0.9)"];
  const state = { particles: [], last: 0, frame: 0 };
  view.wind = state;
  const resize = () => {
    const ratio = window.devicePixelRatio || 1;
    canvas.width = container.clientWidth * ratio;
    canvas.height = container.clientHeight * ratio;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const count = Math.min(1600, Math.round(container.clientWidth * container.clientHeight / 2400));
    state.particles = Array.from({ length: count }, () => spawn({}));
  };
  const spawn = (p) => {
    const b = view.map.getBounds();
    p.lon = Math.max(b.getWest(), grid.west) + Math.random() * (Math.min(b.getEast(), grid.west + grid.width * grid.dx) - Math.max(b.getWest(), grid.west));
    p.lat = Math.max(b.getSouth(), grid.north - grid.height * grid.dy) + Math.random() * (Math.min(b.getNorth(), grid.north) - Math.max(b.getSouth(), grid.north - grid.height * grid.dy));
    p.age = Math.floor(Math.random() * 140);
    return p;
  };
  // One step of every particle; returns segments by colour.
  const step = () => {
    const hour = forecastHour(view);
    const segments = [[], [], []];
    if (!hour) return segments;
    const zoom = view.map.getZoom();
    for (const p of state.particles) {
      const wind = windAt(grid, hour, p.lon, p.lat);
      if (!wind || ++p.age > 140) {
        spawn(p);
        p.age = 0;
        continue;
      }
      const [u, v] = wind;
      const speed = Math.hypot(u, v);
      const metresPerPixel = 40075016 * Math.cos(p.lat * Math.PI / 180) / (512 * 2 ** zoom);
      const scale = 0.22 * metresPerPixel / 111320;
      const from = view.map.project([p.lon, p.lat]);
      p.lon += u * scale / Math.cos(p.lat * Math.PI / 180);
      p.lat += v * scale;
      const to = view.map.project([p.lon, p.lat]);
      segments[speed * 3.6 < 12 ? 0 : speed * 3.6 < 28 ? 1 : 2].push(from.x, from.y, to.x, to.y);
    }
    return segments;
  };
  const draw = (segments) => {
    ctx.lineWidth = 1.2;
    ctx.lineCap = "round";
    segments.forEach((list, i) => {
      if (!list.length) return;
      ctx.strokeStyle = colours[i];
      ctx.beginPath();
      for (let k = 0; k < list.length; k += 4) {
        ctx.moveTo(list[k], list[k + 1]);
        ctx.lineTo(list[k + 2], list[k + 3]);
      }
      ctx.stroke();
    });
  };
  const still = () => { // Reduced motion: short streaks, drawn once.
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const p of state.particles) p.age = 0;
    for (let i = 0; i < 12; i += 1) draw(step());
  };
  const tick = (time) => {
    if (view.wind !== state) return;
    if (mapState.kind !== "weather") return stopWind(view);
    state.raf = requestAnimationFrame(tick);
    if (document.hidden || time - state.last < WIND_FRAME_MS) return;
    state.last = time;
    ctx.save();
    ctx.globalCompositeOperation = "destination-in";
    ctx.fillStyle = "rgba(0,0,0,0.955)"; // Fade the trails.
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.restore();
    draw(step());
  };
  state.onMove = () => ctx.clearRect(0, 0, canvas.width, canvas.height);
  state.onMoveEnd = () => {
    for (const p of state.particles) spawn(p);
    if (reduceMotion()) still();
  };
  state.onResize = () => {
    resize();
    if (reduceMotion()) still();
  };
  view.map.on("move", state.onMove);
  view.map.on("moveend", state.onMoveEnd);
  view.map.on("resize", state.onResize);
  resize();
  if (reduceMotion()) still();
  else state.raf = requestAnimationFrame(tick);
}

function stopWind(view) {
  const state = view.wind;
  if (state) {
    cancelAnimationFrame(state.raf);
    view.map.off("move", state.onMove);
    view.map.off("moveend", state.onMoveEnd);
    view.map.off("resize", state.onResize);
    view.wind = null;
  }
  const canvas = view.map && view.map.getContainer().querySelector(".wind-canvas");
  if (canvas) {
    canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    canvas.hidden = true;
  }
}

function startRadar(view) {
  stopRadar(view);
  if (forecastMode()) {
    const count = (view.gridHours || []).length;
    if (count < 2) return;
    view.radarTimer = setInterval(() => {
      if (mapState.kind !== "weather") return stopRadar(view);
      view.gridIndex = (view.gridIndex + 1) % count;
      showForecastHour(view);
      if (reduceMotion() && view.wind) view.wind.onMoveEnd();
    }, FORECAST_STEP_MS);
    return;
  }
  if (!view.radarFrames || view.radarFrames.length < 2) return;
  view.radarTimer = setInterval(() => {
    if (mapState.kind !== "weather") return stopRadar(view);
    // Hold the newest frame a little longer, so the loop reads as "up to now".
    view.radarIndex = view.radarIndex >= view.radarFrames.length - 1 ? 0 : view.radarIndex + 1;
    showRadarFrame(view);
  }, RADAR_STEP_MS);
}

function stopRadar(view) {
  if (view.radarTimer) clearInterval(view.radarTimer);
  view.radarTimer = null;
}

function weatherChips(rerender, view) {
  const playing = Boolean(view.radarTimer);
  return [
    chip([iconSvg(playing ? "close" : "right", { size: 16 }), playing ? "Pause" : "Play"], playing, () => {
      if (view.radarTimer) {
        stopRadar(view);
        mapState.weatherPlaying = false;
      } else {
        mapState.weatherPlaying = true;
        startRadar(view);
      }
      rerender();
    }, { class: "chip kind-chip", title: "Play or pause the loop" }),
    el("span", { class: "chip kind-chip radar-time", "aria-live": "off" }, forecastMode() ? "Forecast" : "Radar"),
    ...[["radar", "Past hour", "Rain and snow on the radar over the last hour"], ["forecast", "Next 12 hours", "Forecast precipitation and wind, hour by hour"]].map(([mode, label, title]) =>
      chip(label, (mode === "radar") !== forecastMode(), () => {
        mapState.weatherMode = mode;
        view.gridIndex = 0;
        applyLayerToggles(view.map);
        showRadarFrame(view);
        showForecastHour(view);
        if (view.radarTimer || (!reduceMotion() && mapState.weatherPlaying !== false)) startRadar(view);
        if (view.wind && reduceMotion()) view.wind.onMoveEnd();
        rerender();
      }, { class: "chip kind-chip", title })),
    chip("Wind", mapState.weatherWind !== false, () => {
      mapState.weatherWind = mapState.weatherWind === false;
      if (mapState.weatherWind) startWind(view);
      else stopWind(view);
      rerender();
    }, { class: "chip kind-chip", title: "Show the wind as moving particles" }),
    chip([iconSvg("cloud", { size: 16 }), "Details"], false, () => openSheet(view, "weather"), { class: "chip kind-chip" }),
  ];
}

function weatherPanel() {
  return [
    el("div", { class: "panel-head" },
      el("p", { class: "panel-heading" }, "Weather map"),
      el("button", { type: "button", class: "panel-close", "aria-label": "Close weather map legend" }, iconSvg("close", { size: 18 }))),
    el("p", { class: "muted small" }, "Next 12 hours: precipitation and wind from Environment Canada's high-resolution forecast model, hour by hour. Past hour: rain and snow from the radar, every six minutes, with stronger colours for heavier precipitation. Moving lines follow the forecast wind; the faster the wind, the faster and darker they move, and above about 28 km/h they turn raspberry. Circles are the Air Quality Health Index at each Toronto station."),
    el("ul", { class: "legend-list compact" }, [[0.1, "Light, under 0.5 mm an hour"], [0.5, "Moderate, 0.5 to 2 mm"], [2, "Heavy, 2 to 5 mm"], [5, "Very heavy, 5 mm or more"]].map(([mm, text]) =>
      legendRow(el("span", { class: "legend-dot", style: { background: PRECIP_STEPS[PRECIP_STEPS.indexOf(mm) + 1], borderRadius: "3px" } }), `Forecast precipitation: ${text.charAt(0).toLowerCase()}${text.slice(1)}`))),
    el("ul", { class: "legend-list compact" }, Object.entries(AQHI_COLOURS).map(([risk, colour]) => legendRow(el("span", { class: "legend-dot", style: { background: colour } }), `Air quality: ${risk.toLowerCase()} risk`))),
    el("p", { class: "muted small" }, "Environment and Climate Change Canada (MSC GeoMet), Open Government Licence – Canada. Images are served from this site."),
  ];
}

// Twelve hours at a glance: temperature as a line, chance of precipitation as bars.
function hourChart(hours) {
  const temps = hours.map((h) => h.temperature).filter((t) => t != null);
  if (hours.length < 3 || !temps.length) return null;
  const W = 320, H = 86, top = 18, base = 70;
  const lo = Math.min(...temps) - 1, hi = Math.max(...temps) + 1;
  const x = (i) => 10 + (i * (W - 20)) / (hours.length - 1);
  const y = (t) => base - ((t - lo) / (hi - lo)) * (base - top);
  const bars = hours.map((h, i) => {
    const chance = h.precip_chance || 0;
    const bar = svgNode("rect", { class: "rain-bar", x: x(i) - 7, y: base - (chance / 100) * (base - top), width: 14, height: (chance / 100) * (base - top), rx: 2 });
    bar.style.setProperty("--k", i);
    return bar;
  });
  const points = hours.map((h, i) => (h.temperature == null ? null : `${x(i).toFixed(1)},${y(h.temperature).toFixed(1)}`)).filter(Boolean);
  const warmest = hours.reduce((a, h, i) => (h.temperature != null && (a < 0 || h.temperature > hours[a].temperature) ? i : a), -1);
  return el("figure", { class: "hour-chart chart-draw" },
    svgNode("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Temperature and chance of precipitation over the next hours" },
      bars,
      svgNode("polyline", { class: "temp-line", points: points.join(" "), pathLength: 1 }),
      warmest >= 0 ? svgNode("text", { class: "temp-label", x: x(warmest), y: y(hours[warmest].temperature) - 6, "text-anchor": "middle" }, `${Math.round(hours[warmest].temperature)}°`) : null,
      svgNode("line", { class: "chart-base", x1: 0, x2: W, y1: base, y2: base })));
}

// Inspections over time: one mark per inspection, placed by date, coloured by result and
// as tall as its number of infractions.
function inspectionTimeline(inspections) {
  if (inspections.length < 2) return null;
  const days = inspections.map((i) => Date.parse(`${String(i.inspected_on).slice(0, 10)}T12:00:00Z`));
  const first = Math.min(...days), last = Math.max(...days, first + 864e5);
  const most = Math.max(1, ...inspections.map((i) => i.infractions.length));
  const W = 320, H = 54;
  const marks = inspections.map((i, k) => {
    const level = foodLevel(i.status);
    const height = 8 + (i.infractions.length / most) * (H - 22);
    const mark = svgNode("rect", { class: `inspect-mark ${level}`, x: 8 + ((days[k] - first) / (last - first)) * (W - 22), y: H - 12 - height, width: 6, height, rx: 2 });
    mark.style.setProperty("--k", k);
    return svgNode("g", {}, svgNode("title", {}, `${inspectedOn(i.inspected_on)}: ${i.status}, ${plural(i.infractions.length, "infraction", "infractions")}`), mark);
  });
  const year = (t) => new Date(t).getUTCFullYear();
  return el("figure", { class: "inspect-timeline chart-draw" },
    svgNode("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Inspection results over time" },
      svgNode("line", { class: "chart-base", x1: 0, x2: W, y1: H - 12, y2: H - 12 }), marks,
      svgNode("text", { class: "tick", x: 4, y: H - 1 }, String(year(first))),
      svgNode("text", { class: "tick", x: W - 4, y: H - 1, "text-anchor": "end" }, String(year(last)))));
}

const hourShortFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", hour: "numeric" });

// What the findings say this weather tends to do, for the coming day.
function weatherEffects(page, findings) {
  const by = Object.fromEntries((findings || []).map((f) => [f.id, f]));
  const soon = page.hourly.slice(0, 18);
  const daily = page.daily.slice(0, 2);
  const wet = soon.some((h) => (h.precip_chance || 0) >= 60) || daily.some((d) => d.precip.length);
  const snow = daily.some((d) => d.precip.some((p) => /snow|flurr/i.test(p))) || soon.some((h) => /snow|flurr/i.test(h.condition || ""));
  const temps = soon.map((h) => h.temperature).filter((t) => t != null);
  const freezeThaw = temps.length && Math.min(...temps) < 0 && Math.max(...temps) > 0;
  const month = new Date().getMonth() + 1;
  const effects = [];
  const add = (id, text) => by[id] && effects.push(el("li", {}, text, " ", el("a", { href: `#/findings` }, "Finding")));
  if (wet && !snow) add("bikeshare-weather", `Rain is in the forecast. On wet days Bike Share has about ${by["bikeshare-weather"]?.numbers.wet_fewer_pct}% fewer trips than on dry days at the same temperature.`);
  if (snow) add("snow-delays", `Snow is possible. On snowy winter days, subway delays have added up to about ${by["snow-delays"]?.numbers.snow_more_minutes_pct}% more delay time.`);
  if (wet && month >= 6 && month <= 9) add("beach-rain", `After heavy rain, beaches fail the E. coli standard far more often (${by["beach-rain"]?.numbers.wet_unsafe_pct}% of beach days after 10 mm, against ${by["beach-rain"]?.numbers.dry_unsafe_pct}% after a dry day).`);
  if (freezeThaw) add("potholes", "Temperatures cross zero: freeze-thaw days are followed by more pothole reports.");
  return effects;
}

function weatherSheet(view) {
  const w = view.weather;
  if (!w || !w.page) return [el("p", { class: "sheet-kicker" }, "Weather"), el("p", {}, "The forecast is not available right now.")];
  const p = w.page;
  const round = (t) => (t == null ? "–" : `${Math.round(t)}°`);
  const effects = weatherEffects(p, view.findings);
  return [
    el("p", { class: "sheet-kicker" }, "Weather in Toronto"),
    el("div", { class: "weather-now" },
      el("span", { class: "weather-temp" }, round(p.temperature)),
      el("span", {}, el("strong", {}, p.condition || ""), el("br"), el("span", { class: "muted small" },
        [p.feels_like != null ? `Feels like ${round(p.feels_like)}` : null, p.wind_kmh != null ? `wind ${p.wind_dir ? `${p.wind_dir} ` : ""}${p.wind_kmh} km/h${p.gust_kmh ? `, gusts ${p.gust_kmh}` : ""}` : null, p.humidity != null ? `humidity ${p.humidity}%` : null].filter(Boolean).join(" · ")))),
    p.warnings.length ? p.warnings.map((x) => el("p", { class: "notice" }, el("strong", {}, x.description || x.type || "Weather warning"), x.url ? [" ", el("a", { href: x.url, target: "_blank", rel: "noopener" }, "Details")] : null)) : null,
    hourChart(p.hourly.slice(0, 12)),
    el("ol", { class: "hour-strip", "aria-label": "Next hours" }, p.hourly.slice(0, 12).map((h) => el("li", {},
      el("span", { class: "muted small" }, hourShortFmt.format(new Date(h.at))),
      el("strong", {}, round(h.temperature)),
      el("span", { class: "small" }, h.precip_chance ? `${h.precip_chance}%` : "")))),
    effects.length ? [el("p", { class: "sheet-label" }, "What this weather tends to do"), el("ul", { class: "plain small effects" }, effects)] : null,
    el("ul", { class: "plain small daily" }, p.daily.slice(0, 4).map((d) => el("li", {}, el("strong", {}, `${d.name}: `), d.summary))),
    el("p", { class: "muted small" }, "Environment and Climate Change Canada city page forecast for Toronto; precipitation percentages are its chance of precipitation."),
  ];
}

// The food map's kind chips under the search. With every kind shown, a tap shows only that
// kind; after that, taps add or remove kinds, and removing the last shows them all again.
function foodKindChips(rerender, view) {
  const kinds = mapState.food.kinds;
  return EAT_KINDS.map((k, i) => chip([badgeEl(k.glyph, k.colour, "#ffffff", { size: 20 }), k.short], kinds[i], () => {
    if (kinds.every(Boolean)) kinds.forEach((_, j) => { kinds[j] = j === i; });
    else kinds[i] = !kinds[i];
    if (!kinds.some(Boolean)) kinds.fill(true);
    showEats(view);
    rerender();
  }, { class: "chip kind-chip", title: k.label })).concat(
    chip([badgeEl("notable", NOTABLE_COLOUR, "#141d3b", { size: 20 }), "Notable"], mapState.food.notable, () => {
      mapState.food.notable = !mapState.food.notable;
      showEats(view);
      rerender();
    }, { class: "chip kind-chip", title: "Notable places only" }));
}

function rememberFood(view, id, coord) {
  view.foodCoords = view.foodCoords || new Map();
  view.foodCoords.set(id, coord);
}

async function loadFood(view) {
  if (mapState.kind === "food") return loadEats(view);
  if (!mapState.layers.food || view.foodLoading) return;
  view.foodLoading = true;
  try {
    const body = await api("/api/food");
    const features = body.premises.map(([id, lat, lon, code, clean]) => {
      rememberFood(view, id, [lon, lat]);
      return point([lon, lat], { id, level: FOOD_LEVELS[code] || "other", rank: code > 2 ? 0 : code, clean: clean ? 1 : 0 });
    });
    view.map.getSource("food")?.setData(featureCollection(features));
  } catch {
    view.foodLoading = false; // Try again the next time the layer is switched on.
  }
}

function foodLevel(status) {
  return status === "Pass" ? "pass" : status === "Conditional Pass" ? "conditional" : status === "Closed" ? "closed" : "other";
}

function inspectedOn(day) {
  return fileRangeFmt.format(new Date(`${day}T00:00:00Z`));
}

async function findFood(view, text) {
  if (text.trim().length < 3) return [];
  const centre = view.me || [view.map.getCenter().lng, view.map.getCenter().lat];
  try {
    const body = await api(`/api/food/search?${new URLSearchParams({ q: text, near: `${centre[1].toFixed(5)},${centre[0].toFixed(5)}` })}`);
    return body.premises.map((p) => {
      rememberFood(view, p.premises_id, [p.lon, p.lat]);
      return { id: p.premises_id, name: p.name, detail: `${p.status} on ${inspectedOn(p.inspected_on)} · ${p.address}`, coord: [p.lon, p.lat] };
    });
  } catch {
    return [];
  }
}

function foodStatus(status) {
  const level = foodLevel(status);
  return el("span", { class: `tag${level === "pass" ? " free" : level === "conditional" ? " warn" : level === "closed" ? " bad" : ""}` }, status);
}

const SEVERITY_LABELS = { crucial: "Crucial", significant: "Significant", minor: "Minor", "not applicable": "" };

function infractionRow(f) {
  const court = f.outcome ? [f.outcome, f.fine ? `$${numberFmt.format(Number(f.fine))}` : null].filter(Boolean).join(", ") : null;
  return el("li", { class: "arrival-row" }, el("span", {}, f.infraction, court ? el("span", { class: "muted small" }, ` · ${court}`) : null), el("span", { class: "mins" }, SEVERITY_LABELS[f.severity] || ""));
}

function foodSheet() {
  return [el("p", { class: "sheet-kicker" }, "Food inspection"), el("p", { class: "muted small" }, "Loading inspections…")];
}

function foodSheetBody(view, p) {
  const [latest, ...earlier] = p.inspections;
  return [
    el("p", { class: "sheet-kicker" }, "Food inspection"),
    el("h2", {}, p.name),
    el("p", { class: "muted small" }, p.address),
    p.licensed_since ? el("p", { class: "sheet-label" }, licensedSince(p.licensed_since)) : null,
    view.notableByPid?.has(p.premises_id) ? notableBlock(view.notableByPid.get(p.premises_id)) : null,
    latest
      ? el("div", { class: "sheet-block" },
          el("p", { class: "sheet-label" }, `Latest inspection: ${inspectedOn(latest.inspected_on)}`),
          el("p", {}, foodStatus(latest.status), latest.infractions.length ? ` ${plural(latest.infractions.length, "infraction", "infractions")}` : " No infractions"),
          latest.infractions.length ? el("ul", { class: "sheet-list" }, latest.infractions.map(infractionRow)) : null)
      : null,
    inspectionTimeline(p.inspections),
    earlier.length
      ? [
          el("p", { class: "sheet-label" }, "Earlier inspections"),
          el("ul", { class: "sheet-list" }, earlier.map((i) => el("li", { class: "arrival-row" }, el("span", {}, `${inspectedOn(i.inspected_on)} `, foodStatus(i.status)), el("span", { class: "mins" }, i.infractions.length ? plural(i.infractions.length, "infraction", "infractions") : "None")))),
        ]
      : null,
    p.inspections_total > p.inspections.length ? el("p", { class: "muted small" }, `${p.inspections_total} inspections on record; the latest ${p.inspections.length} are shown.`) : null,
    p.listed ? null : el("p", { class: "muted small" }, "No longer in the city's current list of food premises."),
    planHere(view, [p.lon, p.lat], p.name),
    el("p", { class: "muted small" }, "Each result is from its inspection date; premises are inspected again, and a closed premises reopens once it passes. Toronto Public Health DineSafe, City of Toronto Open Data."),
    p.licensed_since ? el("p", { class: "muted small" }, "Licensed since: the earliest city business licence at this address under a matching name; a new owner keeping the name keeps the year. Municipal Licensing and Standards, City of Toronto Open Data.") : null,
  ];
}

// A notable place's line on its card: Michelin awards, opening year, Wikipedia article.
function notableBlock(n) {
  const facts = [n.awards, n.opened ? `Opened ${n.opened}` : null].filter(Boolean).join(" · ");
  return el("div", { class: "sheet-block notable" },
    el("p", { class: "sheet-label" }, badgeEl("notable", NOTABLE_COLOUR, "#141d3b", { size: 20 }), " Notable place", facts ? ` · ${facts}` : ""),
    n.wikipedia ? el("p", {}, el("a", { href: n.wikipedia, target: "_blank", rel: "noopener" }, "Read about it on Wikipedia")) : null);
}

function licensedSince(day) {
  const year = Number(day.slice(0, 4));
  const years = new Date().getFullYear() - year;
  return years >= 1 ? `Licensed here since ${year} (${plural(years, "year", "years")})` : `Licensed here since ${year}`;
}

async function loadFoodSheet(view, id) {
  const current = () => view.selected && view.selected.kind === "food" && view.selected.id === id;
  try {
    const p = await api(`/api/food/${encodeURIComponent(id)}`);
    if (!current()) return;
    rememberFood(view, id, [p.lon, p.lat]);
    fill(view.sheetBody, foodSheetBody(view, p));
    view.map.getSource("selected")?.setData(featureCollection([point([p.lon, p.lat], {})]));
  } catch {
    if (current()) fill(view.sheetBody, el("p", { class: "sheet-kicker" }, "Food inspection"), el("p", {}, "Inspection results are unavailable right now."));
  }
}

function eventWhen(e) {
  const today = torontoDay(new Date());
  const first = e.dates.find((d) => torontoDay(new Date(d)) === today);
  // An event given without a start time is dated at local midnight: show no time for it.
  const midnight = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: "America/Toronto", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(d)) === "00:00";
  if (first) return midnight(first) ? "Today" : `Today, ${timeFmt.format(new Date(first))}`;
  return e.dates.slice(0, 3).map((d) => eventDayFmt.format(new Date(d))).join(", ") + (e.dates.length > 3 ? "…" : "");
}

function eventSheet(view, id) {
  const e = ((view.events || {}).events || []).find((x) => x.event_id === id);
  if (!e) return null;
  return [
    el("p", { class: "sheet-kicker" }, e.categories.slice(0, 2).join(" · ") || "Event"),
    el("h2", {}, e.name),
    el("p", {}, eventWhen(e), e.free ? el("span", { class: "muted" }, " · free") : ""),
    e.venue || e.address ? el("p", { class: "muted" }, [e.venue, e.address].filter(Boolean).join(", ")) : null,
    e.website ? el("p", { class: "sheet-actions" }, el("a", { class: "button", href: e.website, target: "_blank", rel: "noopener noreferrer" }, "Event details")) : null,
    planHere(view, [e.lon, e.lat], e.name),
    el("p", { class: "muted small" }, "City of Toronto festivals and events calendar."),
  ];
}

function nearbyEvents(view, from) {
  const today = torontoDay(new Date());
  return ((view.events || {}).events || [])
    .filter((e) => e.dates.some((d) => torontoDay(new Date(d)) === today))
    .map((e) => [metres(from, [e.lon, e.lat]), e])
    .filter(([m]) => m <= NEARBY_EVENT_M)
    .sort((a, b) => a[0] - b[0])
    .slice(0, 3);
}

function nearbyEventsBlock(view, from) {
  const near = nearbyEvents(view, from);
  if (!near.length) return null;
  return el(
    "div",
    { class: "sheet-block" },
    el("p", { class: "sheet-label" }, "Events nearby today"),
    el("ul", { class: "sheet-list" }, near.map(([m, e]) => el("li", {}, el("button", { type: "button", class: "linkish", "data-event": e.event_id }, e.name), el("span", { class: "muted small" }, `${eventWhen(e)} · ${walkText(m)}`)))),
  );
}

// Trip planner (docs/planner.md): OpenTripPlanner on the published TTC and regional
// timetables. It is optional; the map shows the Directions button only when it is running.
const PLAN_TRANSIT = "#475569";
const PLAN_MODES = { subway: "Subway", tram: "Streetcar", bus: "Bus", rail: "Train", ferry: "Ferry" };
const PLAN_NAME_M = 150;

// Ways to travel a visitor can switch off; walking is always allowed. Bike Share starts off.
const PLAN_MODE_CHOICES = [["subway", "Subway"], ["tram", "Streetcar"], ["bus", "TTC bus"], ["rail", "Regional train"], ["regional_bus", "Regional bus"], ["bike_share", "Bike Share"]];
const PLAN_DEFAULT_MODES = ["subway", "tram", "bus", "rail", "regional_bus"];
const PLAN_PREFS_KEY = "torontrack.plan";

// The travel modes and step-free choice are remembered on this device only.
function planPrefs() {
  try {
    const saved = JSON.parse(localStorage.getItem(PLAN_PREFS_KEY) || "{}");
    const known = new Set(PLAN_MODE_CHOICES.map(([m]) => m));
    return {
      modes: Array.isArray(saved.modes) ? saved.modes.filter((m) => known.has(m)) : PLAN_DEFAULT_MODES,
      stepFree: saved.stepFree === true,
    };
  } catch {
    return { modes: PLAN_DEFAULT_MODES, stepFree: false };
  }
}

function savePlanPrefs(p) {
  try {
    localStorage.setItem(PLAN_PREFS_KEY, JSON.stringify({ modes: p.modes, stepFree: p.stepFree }));
  } catch {
    /* Private browsing or storage turned off: the choice lasts for this visit. */
  }
}

function newPlan() {
  const prefs = planPrefs();
  return { from: null, to: null, pick: "from", when: "now", at: "", stepFree: prefs.stepFree, modes: [...prefs.modes], query: { from: null, to: null }, result: null, chosen: 0, loading: false, error: null, seq: 0 };
}

// Suggestions for a typed place: stations and stops from the map's own data first, then
// addresses, places and intersections from /api/places, nearest the map's centre.
const SUGGEST_MS = 200;
const PLACE_KINDS = { address: "Address", intersection: "Intersection", aerodrome: "Airport", building: "Building" };

function localPlaces(view, text) {
  const query = searchKey(text);
  if (query.length < 2) return [];
  const stations = view.data.stations.filter((s) => searchKey(s.name).includes(query)).slice(0, 3)
    .map((s) => ({ name: `${s.name} station`, detail: "Subway", coord: [s.lon, s.lat] }));
  const regional = ((view.regional || {}).stations || []).filter((s) => searchKey(s.name).includes(query)).slice(0, 2)
    .map((s) => ({ name: `${s.name} GO`, detail: "Regional train", coord: [s.lon, s.lat] }));
  const stops = query.length < 3 ? [] : [...view.surfaceStops.values()].filter((s) => s[1] === query || searchKey(s[2]).includes(query)).slice(0, 3)
    .map((s) => ({ name: s[2], detail: s[1] ? `Stop ${s[1]}` : "Stop", coord: [s[4], s[3]] }));
  return [...stations, ...regional, ...stops];
}

async function findPlaces(view, text) {
  if (text.trim().length < 2) return [];
  const centre = view.me || [view.map.getCenter().lng, view.map.getCenter().lat];
  try {
    const body = await api(`/api/places?${new URLSearchParams({ q: text, near: `${centre[1].toFixed(5)},${centre[0].toFixed(5)}` })}`);
    return body.places.map((p) => ({ name: p.name, detail: [PLACE_KINDS[p.kind] || sentence(p.kind), p.detail].filter(Boolean).join(" · "), coord: [p.lon, p.lat] }));
  } catch {
    return []; // Search not built or unavailable; stations and stops still match.
  }
}

function suggestionList(items, attr) {
  return items.map((item, i) => el("li", { role: "option" }, el("button", { type: "button", [attr]: String(i) }, el("span", {}, item.name), item.detail ? el("span", { class: "muted small" }, item.detail) : null)));
}

function suggestPlanPlaces(view, key, text) {
  clearTimeout(view.suggestTimer);
  const list = view.sheetBody.querySelector(`#plan-suggest-${key}`);
  if (!list) return;
  const seq = (view.suggestSeq = (view.suggestSeq || 0) + 1);
  const show = (items) => {
    view.suggestions = { key, items };
    fill(list, suggestionList(items, "data-plan-choose"));
    list.previousElementSibling?.querySelector("input")?.setAttribute("aria-expanded", String(items.length > 0));
  };
  const local = localPlaces(view, text);
  show(local);
  view.suggestTimer = setTimeout(async () => {
    const found = await findPlaces(view, text);
    if (seq === view.suggestSeq && activeMap === view) show([...local, ...found].slice(0, 9));
  }, SUGGEST_MS);
}

function textOn(hex) {
  const n = parseInt(hex.slice(1), 16);
  return 0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255) > 160 ? "#141d3b" : "#ffffff";
}

function durationText(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function insidePlanner(view, [lon, lat]) {
  const [west, south, east, north] = view.planner.extent;
  return lon >= west && lon <= east && lat >= south && lat <= north;
}

// A tapped point is named after the nearest station or stop, if one is close.
function planPlace(view, coord) {
  let best = [PLAN_NAME_M, null];
  const consider = (lon, lat, name) => {
    const m = metres(coord, [lon, lat]);
    if (m < best[0]) best = [m, name];
  };
  for (const s of view.data.stations) consider(s.lon, s.lat, s.name);
  for (const s of (view.regional || {}).stations || []) consider(s.lon, s.lat, s.name);
  for (const s of view.surfaceStops.values()) consider(s[4], s[3], s[2]);
  return { coord, name: best[1] ? `Near ${best[1]}` : "Point on the map" };
}

function addPlanLayers(view) {
  const map = view.map;
  const c = prefersDark() ? { fill: "#1c1f25", ink: "#eef1f8" } : { fill: "#ffffff", ink: "#141d3b" };
  const transit = ["all", ["==", ["geometry-type"], "LineString"], ["!", ["get", "walk"]]];
  map.addSource("plan", { type: "geojson", data: featureCollection([]) });
  // An ink outline sets the trip apart from the network lines it runs along.
  map.addLayer({ id: "plan-casing", type: "line", source: "plan", filter: transit, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.ink, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 8, 16, 14] } }, "trains-halo");
  map.addLayer({ id: "plan-transit", type: "line", source: "plan", filter: transit, layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4.5, 16, 8] } }, "trains-halo");
  map.addLayer({ id: "plan-walk", type: "line", source: "plan", filter: ["all", ["==", ["geometry-type"], "LineString"], ["get", "walk"]], layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.ink, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 3, 16, 5], "line-dasharray": [0.2, 1.8] } }, "trains-halo");
  map.addLayer({ id: "plan-ends", type: "circle", source: "plan", filter: ["==", ["geometry-type"], "Point"], paint: {
    "circle-radius": 7, "circle-color": ["case", ["==", ["get", "end"], "to"], c.ink, c.fill], "circle-stroke-color": ["case", ["==", ["get", "end"], "to"], c.fill, c.ink], "circle-stroke-width": 3,
  } });
}

function drawPlan(view) {
  const p = view.plan;
  const source = view.map.getSource("plan");
  if (!source) return;
  const features = [];
  const trip = p && p.result && p.result.itineraries[p.chosen];
  for (const leg of trip ? trip.legs : []) {
    const bike = leg.mode === "bicycle";
    if (leg.path.length > 1) features.push({ type: "Feature", geometry: { type: "LineString", coordinates: leg.path }, properties: { walk: !leg.network && !bike, color: bike ? DOCK_COLOURS.ok : leg.color || PLAN_TRANSIT } });
  }
  if (p && p.from) features.push(point(p.from.coord, { end: "from" }));
  if (p && p.to) features.push(point(p.to.coord, { end: "to" }));
  source.setData(featureCollection(features));
}

function fitPlan(view) {
  const trip = view.plan.result && view.plan.result.itineraries[view.plan.chosen];
  const coords = trip ? trip.legs.flatMap((l) => l.path) : [];
  if (coords.length < 2) return;
  const lons = coords.map((c) => c[0]);
  const lats = coords.map((c) => c[1]);
  const wide = window.matchMedia("(min-width: 720px)").matches;
  view.map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]], {
    padding: wide ? { top: 140, bottom: 40, left: 420, right: 60 } : { top: 140, bottom: Math.round(window.innerHeight * 0.55), left: 30, right: 30 },
    maxZoom: 16, duration: reduceMotion() ? 0 : 700,
  });
}

function setPlanEnd(view, key, place) {
  const p = view.plan;
  if (!insidePlanner(view, place.coord)) {
    p.error = "The planner covers Toronto, Brampton, Mississauga and York Region, north to Keswick.";
  } else {
    p[key] = place;
    p.query[key] = null; // Show the chosen place, not what was typed.
    p.error = null;
    p.pick = key === "from" && !p.to ? "to" : null;
  }
  drawPlan(view);
  if (p.from && p.to && !p.error) runPlan(view);
  else openSheet(view, "plan");
}

async function runPlan(view) {
  const p = view.plan;
  const seq = ++p.seq;
  Object.assign(p, { loading: true, error: null, result: null, chosen: 0 });
  openSheet(view, "plan");
  drawPlan(view);
  const query = new URLSearchParams({ from: `${p.from.coord[1]},${p.from.coord[0]}`, to: `${p.to.coord[1]},${p.to.coord[0]}` });
  if (p.when !== "now" && p.at) {
    query.set("at", p.at);
    if (p.when === "arrive") query.set("arrive", "true");
  }
  if (p.stepFree) query.set("step_free", "true");
  query.set("modes", p.modes.join(","));
  try {
    const result = await api(`/api/plan?${query}`);
    if (seq !== p.seq || activeMap !== view) return;
    p.result = result;
    if (!result.itineraries.length) p.error = "No trips found for that time.";
  } catch {
    if (seq !== p.seq) return;
    p.error = "The trip planner is unavailable right now. Try again in a minute.";
  }
  p.loading = false;
  if (view.selected && view.selected.kind === "plan") openSheet(view, "plan");
  drawPlan(view);
  fitPlan(view);
}

function planPill(leg) {
  const colour = leg.color || PLAN_TRANSIT;
  return el("span", { class: "plan-pill", style: { background: colour, color: textOn(colour) }, title: PLAN_MODES[leg.mode] || leg.mode }, leg.route || PLAN_MODES[leg.mode] || leg.mode);
}

function planSteps(trip, stepFree = false) {
  const steps = [];
  trip.legs.forEach((leg, i) => {
    if (leg.mode === "bicycle") {
      const turns = (leg.steps || []).filter((s) => s.street && s.action !== "depart");
      steps.push(el("li", { class: "plan-step bike" }, iconSvg("bike", { size: 20 }), el("span", {},
        el("strong", {}, `${timeFmt.format(new Date(leg.start))} `),
        `Ride a Bike Share bike ${durationText(leg.seconds)} (${numberFmt.format(Math.round(leg.metres / 10) * 10)} m)${leg.from ? ` from ${leg.from}` : ""} to ${leg.to || "your destination"}`,
        turns.length ? el("span", { class: "plan-turns" }, turns.slice(0, 4).map((s) => el("span", { class: "plan-turn" }, iconSvg(STEP_ICONS[s.action] || "straight", { size: 14 }), stepText(s)))) : null)));
      return;
    }
    if (!leg.network) {
      if (leg.seconds >= 30) {
        const turns = (leg.steps || []).filter((s) => s.street && s.action !== "depart");
        steps.push(el("li", { class: "plan-step walk" }, iconSvg("walk", { size: 20 }), el("span", {},
          `Walk ${durationText(leg.seconds)} (${numberFmt.format(Math.round(leg.metres / 10) * 10)} m) to ${leg.to || (i === trip.legs.length - 1 ? "your destination" : "the stop")}`,
          turns.length ? el("span", { class: "plan-turns" }, turns.slice(0, 4).map((s) => el("span", { class: "plan-turn" }, iconSvg(STEP_ICONS[s.action] || "straight", { size: 14 }), stepText(s)))) : null)));
      }
      return;
    }
    const door = (icon, text) => el("span", { class: "plan-door" }, iconSvg(icon, { size: 14 }), text);
    steps.push(el(
      "li",
      { class: "plan-step" },
      planPill(leg),
      el("span", {},
        leg.entrance ? door(leg.entrance.step_free ? "stepfree" : "entrance", `Enter by ${doorText(leg.entrance)}`) : null,
        leg.entrance === null && stepFree ? door("stepfree", "No step-free entrance is mapped here; check before you go") : null,
        el("strong", {}, `${timeFmt.format(new Date(leg.start))} `), `${PLAN_MODES[leg.mode] || "Transit"} from ${leg.from}`,
        leg.headsign ? el("span", { class: "muted" }, ` towards ${leg.headsign}`) : null,
        leg.delay_minutes !== null && leg.delay_minutes !== undefined ? [" · ", regionalDelay(leg.delay_minutes)] : null,
        el("br"),
        el("strong", {}, `${timeFmt.format(new Date(leg.end))} `), `Get off at ${leg.to}`,
        leg.exit ? door(leg.exit.step_free ? "stepfree" : "entrance", `Exit by ${doorText(leg.exit)}`) : null,
        leg.exit === null && stepFree ? door("stepfree", "No step-free exit is mapped here; check before you go") : null,
        // TTC's alert text starts with the station's name, so it is shown as it is.
        (leg.outages || []).flatMap((o) => o.alerts).map((text) => el("span", { class: "plan-warn" }, iconSvg("elevator", { size: 14 }), text)),
      ),
    ));
  });
  return el("ol", { class: "plan-steps" }, steps);
}

function planTrips(view) {
  const p = view.plan;
  return el("ol", { class: "plan-trips" }, p.result.itineraries.map((trip, i) => {
    const transit = trip.legs.filter((l) => l.network);
    const bike = trip.legs.some((l) => l.mode === "bicycle");
    return el("li", {}, el(
      "button",
      { type: "button", class: "plan-trip", "aria-pressed": String(i === p.chosen), "data-plan-trip": String(i) },
      el("span", { class: "plan-trip-top" }, el("span", {}, `${timeFmt.format(new Date(trip.start))} – ${timeFmt.format(new Date(trip.end))}`), el("strong", {}, durationText(trip.seconds))),
      el("span", { class: "plan-pills" }, bike ? el("span", { class: "plan-pill bike", title: "Bike Share" }, iconSvg("bike", { size: 14 })) : null,
        transit.length ? transit.map(planPill) : bike ? null : el("span", { class: "muted" }, "Walk the whole way")),
      el("span", { class: "muted small" }, `${transit.length > 1 ? plural(transit.length - 1, "transfer", "transfers") : transit.length ? "No transfers" : ""}${transit.length ? " · " : ""}${numberFmt.format(Math.round(trip.walk_metres / 10) * 10)} m walking`),
      trip.on_time ? el("span", { class: "small" }, p.when === "arrive"
        ? `Leave by ${timeFmt.format(new Date(trip.on_time.leave_by))} to arrive on time about 9 times in 10`
        : `About 9 times in 10 you arrive by ${timeFmt.format(new Date(trip.on_time.arrive_by))}`) : null,
      trip.outages && trip.outages.length ? el("span", { class: "warn small" }, `Elevator or escalator out at ${trip.outages.map((o) => o.station).join(", ")}`) : null,
    ));
  }));
}

function planSheet(view) {
  const p = view.plan;
  const slot = (key, label) => [
    el(
      "div",
      { class: "plan-slot" },
      el("span", { class: `plan-end ${key}`, "aria-hidden": "true" }),
      el("label", { class: "plan-slot-text" }, el("span", { class: "muted small" }, label),
        el("input", {
          type: "text", class: "plan-input", "data-plan-query": key, autocomplete: "off", enterkeyhint: "search", spellcheck: "false",
          value: p.query[key] ?? (p[key] ? p[key].name : ""), role: "combobox", "aria-expanded": "false", "aria-controls": `plan-suggest-${key}`,
          placeholder: p.pick === key ? "Type an address, or tap the map" : "Address, place, station or stop",
        })),
      el("button", { type: "button", class: "chip small icon-chip", "aria-pressed": String(p.pick === key), "data-plan-pick": key, "aria-label": `Choose ${label.toLowerCase()} on the map`, title: "Choose on the map" }, iconSvg("pin", { size: 18 })),
    ),
    el("ul", { class: "plan-suggest", id: `plan-suggest-${key}`, role: "listbox", "aria-label": `${label} suggestions` }),
  ];
  const whenSelect = el("select", { "data-plan-when": "1", "aria-label": "When" },
    [["now", "Leave now"], ["depart", "Leave at"], ["arrive", "Arrive by"]].map(([value, label]) => el("option", { value, selected: p.when === value ? "selected" : null }, label)));
  const trip = p.result && p.result.itineraries[p.chosen];
  return [
    el("p", { class: "sheet-kicker" }, "Trip planner"),
    el("h2", {}, "Directions"),
    slot("from", "From"),
    slot("to", "To"),
    el("p", { class: "plan-actions" },
      el("button", { type: "button", class: "linkish small", "data-plan-me": "1" }, "Start from my location"),
      p.from || p.to ? el("button", { type: "button", class: "linkish small", "data-plan-swap": "1" }, "Swap") : null),
    el("div", { class: "plan-when" }, whenSelect, p.when !== "now" ? el("input", { type: "datetime-local", "data-plan-at": "1", value: p.at, "aria-label": "Date and time" }) : null),
    el("div", { class: "plan-modes", role: "group", "aria-label": "Travel by" },
      el("span", { class: "muted small" }, "Travel by"),
      PLAN_MODE_CHOICES.map(([mode, label]) => el("button", { type: "button", class: "chip small", "aria-pressed": String(p.modes.includes(mode)), "data-plan-mode": mode }, label))),
    el("label", { class: "plan-option" }, el("input", { type: "checkbox", "data-plan-stepfree": "1", checked: p.stepFree ? "checked" : null }), "Step-free trips: elevators and ramps, no stairs"),
    p.stepFree ? el("p", { class: "muted small" }, "Uses only stations and stops TTC lists as accessible, and lists trips without a reported elevator outage first. Elevators can fail without notice.") : null,
    p.modes.includes("bike_share") ? el("p", { class: "muted small" }, "Bike Share uses live bikes and docks at each station. A ride over 30 minutes costs extra.") : null,
    p.loading ? el("p", { class: "muted" }, "Finding trips…") : null,
    p.error ? el("p", { class: "warn" }, p.error) : null,
    p.result && p.result.itineraries.length ? [planTrips(view), trip ? [
      el("p", { class: "plan-start" },
        el("button", { type: "button", class: "button", "data-guide": "gps" }, iconSvg("navigate", { size: 18 }), "Start"),
        el("button", { type: "button", class: "button secondary", "data-guide": "simulated", title: "Run through this trip on its timetable, ten times faster" }, "Preview")),
      el("p", { class: "sheet-label" }, "Steps"), planSteps(trip, p.stepFree)] : null] : null,
    el("p", { class: "muted small" }, "TTC and neighbouring buses use live times where the feed covers the ride; regional trains use the timetable. On-time estimates come from how these TTC rides ran over the last four weeks; they do not cover subway ride times or unusual events. Walking routes, addresses and places use OpenStreetMap. Your travel choices are kept on this device."),
  ];
}

function planHere(view, coord, name) {
  if (!view.planner || !insidePlanner(view, coord)) return null;
  const data = { "data-plan-coord": coord.join(","), "data-plan-name": name };
  return el("p", { class: "plan-actions" },
    el("button", { type: "button", class: "linkish small", "data-plan-set": "from", ...data }, "Directions from here"),
    el("button", { type: "button", class: "linkish small", "data-plan-set": "to", ...data }, "Directions to here"));
}

function planFromMe(view) {
  const use = (coord) => setPlanEnd(view, "from", { coord, name: "Your location" });
  if (view.me) return use(view.me);
  if (!("geolocation" in navigator) || !window.isSecureContext) {
    view.plan.error = LOCATION_UNSUPPORTED;
    return openSheet(view, "plan");
  }
  const once = watchLocation(
    (position) => { once.stop(); use([position.coords.longitude, position.coords.latitude]); },
    (error) => { once.stop(); view.plan.error = locationErrorText(error); openSheet(view, "plan"); },
    { maximumAge: 30000 },
  );
}

// ---- Guidance mode (docs/navigation.md) -----------------------------------------------
// guidance.js decides what to show from each position; this part draws it. The position
// comes from a location source, so real GPS and the simulated preview share everything.
const GUIDE_ZOOM = 16.5;
// Vehicle positions arrive every 10 s and the server caches stop estimates for 15 s.
const LIVE_WAIT_MS = 15000;

function addGuideLayers(view) {
  const map = view.map;
  const ratio = 2;
  const size = 34 * ratio;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 2 * ratio, 0, 2 * Math.PI);
  ctx.fillStyle = "#ffffff";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 4.5 * ratio, 0, 2 * Math.PI);
  ctx.fillStyle = "#58a6f0";
  ctx.fill();
  ctx.beginPath();
  ctx.moveTo(size / 2, 7 * ratio);
  ctx.lineTo(size / 2 + 7 * ratio, 25 * ratio);
  ctx.lineTo(size / 2, 20.5 * ratio);
  ctx.lineTo(size / 2 - 7 * ratio, 25 * ratio);
  ctx.closePath();
  ctx.fillStyle = "#ffffff";
  ctx.fill();
  map.addImage("guide-me", ctx.getImageData(0, 0, size, size), { pixelRatio: ratio });
  map.addSource("guide-me", { type: "geojson", data: featureCollection([]) });
  map.addLayer({ id: "guide-me", type: "symbol", source: "guide-me", layout: {
    "icon-image": "guide-me", "icon-rotate": ["get", "heading"], "icon-rotation-alignment": "map",
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  } });
}

async function requestWakeLock(guide) {
  try {
    guide.wakeLock = await navigator.wakeLock?.request("screen");
  } catch {
    /* Not supported or not allowed: the screen may dim; guidance still works. */
  }
}

function startGuidance(view, kind) {
  const p = view.plan;
  const trip = p && p.result && p.result.itineraries[p.chosen];
  if (!trip) return;
  stopGuidance(view, { keepPlan: true });
  const source = kind === "simulated" ? simulatedLocationSource(trip) : browserLocationSource();
  const guide = { trip, source, state: newGuidance(trip), follow: true, info: null, wakeLock: null, live: null, liveTimer: null, alerted: new Set(), error: null };
  view.guide = guide;
  view.sheet.classList.remove("open");
  view.page.classList.add("guiding");
  renderGuidance(view);
  requestWakeLock(guide);
  source.start((fix) => onGuideFix(view, guide, fix), (error) => {
    guide.error = error.message;
    renderGuidance(view);
  });
}

function stopGuidance(view, { keepPlan = false } = {}) {
  const guide = view.guide;
  if (!guide) return;
  guide.source.stop();
  clearInterval(guide.liveTimer);
  guide.wakeLock?.release?.().catch(() => {});
  view.guide = null;
  view.page.classList.remove("guiding");
  const source = view.map.getSource("guide-me");
  if (source) source.setData(featureCollection([]));
  view.map.easeTo({ bearing: 0, duration: reduceMotion() ? 0 : 500 });
  if (!keepPlan && view.plan) openSheet(view, "plan");
}

function onGuideFix(view, guide, fix) {
  if (view.guide !== guide) return;
  const info = updateGuidance(guide.state, fix);
  guide.info = info;
  guide.fix = fix;
  const heading = fix.heading ?? guide.lastHeading ?? 0;
  if (fix.heading !== null && fix.heading !== undefined && (fix.speed ?? 1) > 0.5) guide.lastHeading = fix.heading;
  view.map.getSource("guide-me").setData(featureCollection([point([fix.lon, fix.lat], { heading })]));
  if (guide.follow) {
    view.map.easeTo({
      center: [fix.lon, fix.lat], zoom: Math.max(view.map.getZoom(), GUIDE_ZOOM), bearing: guide.lastHeading ?? 0,
      padding: { top: 150, bottom: 150 }, duration: reduceMotion() ? 0 : 900,
    });
  }
  // A buzz when it is time to act: the stop before yours, and arrival.
  const cue = info.phase === "ride" && info.stopsLeft === 1 ? `last-stop-${info.legIndex}` : info.phase === "arrived" ? "arrived" : null;
  if (cue && !guide.alerted.has(cue)) {
    guide.alerted.add(cue);
    navigator.vibrate?.([200, 100, 200]);
  }
  watchLiveArrivals(view, guide, info);
  renderGuidance(view);
  if (info.phase === "arrived") guide.source.stop();
}

// While waiting for a TTC bus or streetcar, show the live estimate for that stop and route.
function watchLiveArrivals(view, guide, info) {
  const leg = info.leg;
  const wanted = info.phase === "wait" && guide.source.kind === "gps" && leg && leg.network === "ttc" && ["bus", "tram"].includes(leg.mode) && info.stopId;
  const key = wanted ? `${info.stopId}:${leg.route}` : null;
  if (key === guide.liveKey) return;
  guide.liveKey = key;
  guide.live = null;
  clearInterval(guide.liveTimer);
  if (!key) return;
  const load = async () => {
    try {
      const data = await api(`/api/stops/${encodeURIComponent(info.stopId)}`);
      if (view.guide !== guide || guide.liveKey !== key) return;
      const next = data.arrivals.filter((a) => a.short_name === leg.route).sort((a, b) => a.seconds - b.seconds)[0];
      guide.live = next ? { seconds: next.seconds, at: Date.now() } : { none: true };
      renderGuidance(view);
    } catch {
      /* The scheduled time stays on screen. */
    }
  };
  load();
  guide.liveTimer = setInterval(load, LIVE_WAIT_MS);
}

function guideLegPill(leg, current, done) {
  const cls = `guide-leg${current ? " current" : ""}${done ? " done" : ""}`;
  if (leg.mode === "bicycle") return el("span", { class: cls, title: "Bike Share" }, iconSvg("bike", { size: 16 }));
  if (!leg.network) return el("span", { class: cls, title: "Walk" }, iconSvg("walk", { size: 16 }));
  const colour = leg.color || PLAN_TRANSIT;
  return el("span", { class: cls }, el("span", { class: "plan-pill", style: { background: colour, color: textOn(colour) } }, leg.route || PLAN_MODES[leg.mode]));
}

function renderGuidance(view) {
  const guide = view.guide;
  if (!guide) return;
  const info = guide.info;
  const top = view.guideTop;
  const bar = view.guideBar;
  if (guide.error) {
    fill(top, el("div", { class: "guide-main" }, iconSvg("locate", { size: 30 }), el("div", { class: "guide-text" }, el("p", { class: "guide-title" }, "Location unavailable"), el("p", { class: "guide-detail" }, guide.error))));
  } else if (!info) {
    fill(top, el("div", { class: "guide-main" }, iconSvg("locate", { size: 30 }), el("div", { class: "guide-text" }, el("p", { class: "guide-title" }, "Finding your location…"))));
  } else {
    let detail = info.detail;
    if (info.phase === "arrived") detail = view.plan?.to?.name || detail;
    if (info.phase === "wait" && guide.live) {
      detail = guide.live.none
        ? `No ${info.leg.route} on its way to this stop right now`
        : `Next ${info.leg.route} ${guide.live.seconds < 60 ? "is arriving" : `in ${Math.round(guide.live.seconds / 60)} min`} · live estimate`;
    }
    fill(top,
      el("div", { class: `guide-main phase-${info.phase}` },
        el("span", { class: "guide-icon" }, iconSvg(info.icon, { size: 30 })),
        el("div", { class: "guide-text" },
          info.distance !== undefined && info.phase === "walk" ? el("p", { class: "guide-distance" }, distanceText(info.distance)) : null,
          el("p", { class: "guide-title" }, info.title),
          el("p", { class: "guide-detail" }, detail))),
      info.offRoute ? el("p", { class: "guide-offroute" }, "You seem to be off the route.", el("button", { type: "button", class: "linkish", "data-guide-replan": "1" }, "Re-plan from here")) : null,
    );
  }
  const legs = guide.trip.legs;
  const current = info ? info.legIndex : 0;
  fill(bar,
    el("div", { class: "guide-legs" }, legs.map((leg, i) => guideLegPill(leg, i === current && info?.phase !== "arrived", i < current || info?.phase === "arrived"))),
    el("div", { class: "guide-row" },
      el("span", { class: "guide-eta" }, el("strong", {}, timeFmt.format(new Date(guide.trip.end))), " arrival", guide.source.kind === "simulated" ? el("span", { class: "guide-preview" }, "Preview") : null),
      guide.follow ? null : el("button", { type: "button", class: "button secondary small", "data-guide-recenter": "1" }, iconSvg("navigate", { size: 16 }), "Recenter"),
      el("button", { type: "button", class: "button secondary small", "data-safety": "1", "aria-label": "Safety" }, iconSvg("shield", { size: 16 })),
      el("button", { type: "button", class: "button danger small", "data-guide-end": "1" }, info?.phase === "arrived" ? "Done" : "End")),
  );
}

function lineColour(id) {
  return (lines.find((l) => l.id === id) || {}).color || "#777";
}

function addOverlayLayers(map, data) {
  const { lines: lineFc, stations, entrances } = networkFeatures(data);
  const c = prefersDark() ? { casing: "#14161b", stationFill: "#1c1f25", stationStroke: "#eef1f8", label: "#eef1f8", halo: "#14161b", trainStroke: "#ffffff" } : { casing: "#ffffff", stationFill: "#ffffff", stationStroke: "#141d3b", label: "#141d3b", halo: "#ffffff", trainStroke: "#141d3b" };
  map.addSource("lines", { type: "geojson", data: lineFc });
  map.addSource("stations", { type: "geojson", data: stations, maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addSource("docks", { type: "geojson", data: dockFeatures(data, mapState.mode), maxzoom: POINTS_MAXZOOM, buffer: 0 });
  // Subway trains, buses and streetcars, and regional trains (moveVehicles), told apart by
  // each feature's `moving`.
  map.addSource("moving", { type: "geojson", data: featureCollection([]), buffer: 0 });
  map.addSource("me", { type: "geojson", data: featureCollection([]) });
  map.addSource("selected", { type: "geojson", data: featureCollection([]) });
  addMapIcons(map);
  map.addLayer({ id: "line-casing", type: "line", source: "lines", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 4, 16, 12] } });
  map.addLayer({ id: "line", type: "line", source: "lines", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "color"], "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2.5, 16, 8] } });
  map.addLayer({ id: "docks", type: "circle", source: "docks", minzoom: 13, maxzoom: 15.5, paint: {
    "circle-color": ["match", ["get", "level"], ...Object.entries(DOCK_COLOURS).flat(), DOCK_COLOURS.unknown],
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 13, 2.5, 14, 4.5, 15.5, 6],
    "circle-stroke-color": c.casing, "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 12, 0.5, 16, 1.5],
    "circle-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0, 13.6, 0.9],
    "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 13, 0, 13.6, 1],
  } });
  map.addLayer({ id: "dock-icon", type: "symbol", source: "docks", minzoom: 15.5, layout: {
    "icon-image": ["concat", "badge-dock-", ["get", "level"]], "icon-size": ["interpolate", ["linear"], ["zoom"], 15.5, 0.85, 18, 1.05],
    "icon-allow-overlap": true, "text-field": ["get", "label"], "text-font": ["Noto Sans Medium"], "text-size": 11,
    "text-anchor": "top", "text-offset": [0, 1.05], "text-optional": true,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 1.6 } });
  // Stations with an elevator or escalator out of service carry a small badge at the corner.
  map.addLayer({ id: "outages", type: "symbol", source: "stations", minzoom: 11.5, filter: [">", ["get", "outages"], 0], layout: {
    "icon-image": "badge-elevator", "icon-size": ["interpolate", ["linear"], ["zoom"], 11.5, 0.65, 16, 1], "icon-offset": [14, -14],
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  } });
  map.addLayer({ id: "stations", type: "circle", source: "stations", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, ["case", ["get", "major"], 4, 2.5], 16, ["case", ["get", "major"], 8, 6]],
    "circle-color": c.stationFill, "circle-stroke-color": c.stationStroke, "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 16, 2.5],
  } });
  // Station entrances appear close up, step-free ones in their own colour.
  map.addSource("entrances", { type: "geojson", data: entrances, maxzoom: POINTS_MAXZOOM, buffer: 0 });
  map.addLayer({ id: "entrances", type: "symbol", source: "entrances", minzoom: 15, layout: {
    "icon-image": ["case", ["get", "step_free"], "badge-entrance-free", "badge-entrance"], "icon-size": ["interpolate", ["linear"], ["zoom"], 15, 0.7, 18, 1],
    "icon-allow-overlap": true, "text-field": ["step", ["zoom"], "", 16.5, ["get", "name"]], "text-font": ["Noto Sans Medium"], "text-size": 11,
    "text-anchor": "top", "text-offset": [0, 1.1], "text-optional": true, "text-max-width": 8,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 1.6, "icon-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0, 15.3, 1] } });
  // A soft shadow under each train badge; later layers are placed relative to it.
  // Trains: small dots zoomed out, full icons from TRAIN_ICON_ZOOM, nothing further out
  // (motion.js SHOWN_FROM_ZOOM), as with buses.
  const trainColour = ["match", ["get", "line"], ...lines.flatMap((l) => [l.id, l.color || "#777"]), "#777"];
  map.addLayer({ id: "trains-dot", type: "circle", source: "moving", filter: ["==", ["get", "moving"], "train"], minzoom: SHOWN_FROM_ZOOM.trains, maxzoom: TRAIN_ICON_ZOOM, paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 3.2, 12.5, 4.5], "circle-color": trainColour,
    "circle-stroke-color": c.casing, "circle-stroke-width": 1,
  } });
  map.addLayer({ id: "trains-halo", type: "circle", source: "moving", filter: ["==", ["get", "moving"], "train"], minzoom: TRAIN_ICON_ZOOM, paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 7, 16, 15], "circle-color": "#000000", "circle-opacity": prefersDark() ? 0.35 : 0.12, "circle-blur": 0.6 } });
  map.addLayer({ id: "trains", type: "symbol", source: "moving", filter: ["==", ["get", "moving"], "train"], minzoom: TRAIN_ICON_ZOOM, layout: {
    "icon-image": ["concat", "badge-train-", ["get", "line"]], "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.55, 13, 0.8, 16, 1.05],
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  } });
  map.addLayer({ id: "station-label", type: "symbol", source: "stations", minzoom: 10.5, filter: ["any", ["get", "major"], [">=", ["zoom"], 12.5]], layout: {
    "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 16, 14],
    "text-anchor": "left", "text-offset": [0.9, 0], "text-optional": true, "text-max-width": 9,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 2 } });
  map.addLayer({ id: "me-accuracy", type: "fill", source: "me", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#58a6f0", "fill-opacity": 0.12 } });
  map.addLayer({ id: "me-pulse", type: "circle", source: "me", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 9, "circle-color": "#58a6f0", "circle-opacity": 0.25 } });
  map.addLayer({ id: "me", type: "circle", source: "me", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 7, "circle-color": "#58a6f0", "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 } });
  map.addLayer({ id: "selected", type: "circle", source: "selected", paint: { "circle-radius": 16, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#58a6f0", "circle-stroke-width": 3 } });
  applyLayerToggles(map);
}

const FOOD_MAP_LAYERS = { trains: false, regional: false, buses: false, bikes: false, access: false, entrances: false, roads: false, lanes: false, events: false, parking: false, bikeParking: false, food: false };

function applyLayerToggles(map) {
  const show = (ids, on) => ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", on ? "visible" : "none"));
  const layers = mapState.kind !== "transit" ? FOOD_MAP_LAYERS : mapState.layers;
  show(["rec", "rec-label"], mapState.kind === "city");
  show(["radar-rain", "radar-snow"], mapState.kind === "weather" && !forecastMode());
  show(["precip-grid"], mapState.kind === "weather" && forecastMode());
  show(["aqhi-dot", "aqhi-value"], mapState.kind === "weather");
  show(["beaches"], mapState.kind === "city" && mapState.city.beach);
  show(["patios"], mapState.kind === "city" && mapState.city.patio);
  show(["agenda"], mapState.kind === "city" && mapState.city.events);
  show(["eats-cluster", "eats-count", "eats", "eats-label", "eats-notable"], mapState.kind === "food");
  show(["food-other"], mapState.kind === "food" && mapState.food.other);
  // The food map leaves out the subway lines and stations as well as every transit layer.
  show(["line-casing", "line", "stations", "station-label"], mapState.kind === "transit");
  show(["trains", "trains-halo", "trains-dot"], layers.trains);
  show(["regional-line", "regional-line-casing", "regional-stations", "regional-station-label", "regional-trains"], layers.regional);
  show(["vehicles", "vehicle-heading", "vehicle-icon", "vehicle-label", "bus-stops", "route-path", "route-path-casing"], layers.buses);
  show(["docks", "dock-icon"], layers.bikes);
  show(["outages"], layers.access);
  show(["entrances"], layers.entrances);
  show(["parking", "parking-entrances"], layers.parking);
  show(["bike-parking"], layers.bikeParking);
  show(["road-work-line", "road-closure-line", "road-closure-point", "road-icons", "road-icons-point"], layers.roads);
  show(["lanes", "lanes-trail"], layers.lanes);
  show(["events"], layers.events);
  show(["food"], layers.food);
}



function stationSheet(view, key) {
  const s = view.data.stations.find((x) => x.key === key);
  if (!s) return null;
  const statuses = view.data.lines.filter((l) => s.lines.includes(l.id)).map((l) => l.status);
  const here = [s.lon, s.lat];
  const docks = nearestDocks(view, here, 3);
  return [
    el("p", { class: "sheet-kicker" }, "Subway station"),
    el("h2", { class: "with-badge" }, s.name, el("span", { class: "badges" }, s.lines.map((l) => badge(l, { small: true })))),
    saveButton("station", s.key, s.name),
    statuses.map((st) => {
      // A disruption on another part of the line (say Eglinton to Lawrence, seen from
      // Union) is shown as elsewhere, not as this station's status.
      const elsewhere = !["normal", "unknown"].includes(st.status) && s.disrupted && !s.disrupted.includes(st.line);
      return [
        el("p", { class: "status-line" }, badge(st.line, { link: false, small: true }), elsewhere ? statusPill("normal", "Running here") : statusPill(st.status, st.label)),
        elsewhere && st.summary ? el("p", { class: "muted small" }, `Elsewhere on the line: ${st.summary}`) : null,
      ];
    }),
    s.outages.length
      ? el("div", { class: "sheet-block" }, el("p", { class: "sheet-label" }, `${plural(s.outages.length, "elevator or escalator", "elevators or escalators")} out of service`), el("ul", { class: "plain" }, s.outages.map((o) => el("li", {}, o))))
      : null,
    s.entrances && s.entrances.length
      ? el("div", { class: "sheet-block" }, el("p", { class: "sheet-label" }, plural(s.entrances.length, "entrance", "entrances")),
          el("ul", { class: "sheet-list" }, s.entrances.map((e) => el("li", {},
            el("button", { type: "button", class: "linkish", "data-entrance": `${s.key}|${e.id}` }, e.name),
            e.where ? el("span", { class: "muted small" }, sentence(e.where)) : null,
            el("span", { class: "entrance-tags" }, entranceTags(e))))))
      : null,
    docks.length ? el("div", { class: "sheet-block" }, el("p", { class: "sheet-label" }, "Nearest Bike Share"), dockList(docks, here)) : null,
    s.bike_parking ? el("p", { class: "muted small" }, bikeParkingText(s.bike_parking)) : null,
    nearbyEventsBlock(view, here),
    planHere(view, here, s.name),
    el("p", { class: "sheet-actions" }, el("a", { class: "button", href: `#/station/${s.key}` }, "Next trains and details")),
  ];
}

function dockList(docks, from) {
  return el(
    "ul",
    { class: "sheet-list" },
    docks.map((d) =>
      el(
        "li",
        {},
        el("button", { type: "button", class: "linkish", "data-dock": d.id }, d.name),
        el("span", { class: "muted small" }, d.current ? `${plural(d.bikes, "bike", "bikes")} · ${plural(d.docks, "open dock", "open docks")}` : "No current report"),
        from ? el("span", { class: "muted small" }, walkText(metres(from, [d.lon, d.lat]))) : null,
      ),
    ),
  );
}

function dockSheet(view, id) {
  const d = view.data.bike_share.docks.find((x) => x.id === id);
  if (!d) return null;
  return [
    el("p", { class: "sheet-kicker" }, "Bike Share dock"),
    el("h2", {}, d.name),
    d.current
      ? el("div", { class: "stat-row" }, el("div", { class: "stat" }, el("b", {}, d.bikes), el("span", {}, d.bikes === 1 ? "bike" : "bikes")), el("div", { class: "stat" }, el("b", {}, d.docks), el("span", {}, d.docks === 1 ? "open dock" : "open docks")), el("div", { class: "stat" }, el("b", {}, d.capacity), el("span", {}, "capacity")))
      : el("p", { class: "muted" }, "This dock has no current report, so its availability is unknown."),
    d.reported_at ? el("p", { class: "muted small" }, `Reported ${timeFmt.format(new Date(d.reported_at))}`) : null,
    view.me ? el("p", { class: "muted small" }, walkText(metres(view.me, [d.lon, d.lat]))) : null,
  ];
}

function trainSheet(view, id) {
  const [lineId, trainId] = id.split(":");
  const line = view.trains.lines.find((l) => l.id === lineId);
  const t = line && line.trains.find((x) => x.train_id === trainId);
  if (!t) return null;
  const towards = (line.directions.find((d) => d.direction_id === t.direction_id) || {}).towards;
  return [
    el("p", { class: "sheet-kicker" }, t.scheduled ? "Light rail train" : "Subway train"),
    el("h2", { class: "with-badge" }, badge(lineId, { small: true }), towards ? `Towards ${towards}` : `Line ${lineId}`),
    el("p", {}, t.at_station ? `At ${t.station}` : `Approaching ${t.station}`, t.at_station ? "" : el("span", { class: "muted" }, ` · ${minutesText(t.eta_seconds)}`)),
    el("p", { class: "muted small" }, t.scheduled ? "Placed by timetable: TTC publishes no live data for this line." : "Position estimated from TTC's predicted arrival times; the subway feed has no GPS positions."),
  ];
}

function nearestDocks(view, from, count) {
  return view.data.bike_share.docks
    .filter((d) => !d.current || dockCount(d, mapState.mode) > 0)
    .map((d) => [metres(from, [d.lon, d.lat]), d])
    .sort((a, b) => a[0] - b[0])
    .slice(0, count)
    .map(([, d]) => d);
}

function nearSheet(view) {
  if (!view.me) return [el("p", { class: "sheet-kicker" }, "Near you"), el("p", { class: "muted" }, "Finding your location…")];
  const stations = view.data.stations.map((s) => [metres(view.me, [s.lon, s.lat]), s]).sort((a, b) => a[0] - b[0]).slice(0, 2);
  const docks = nearestDocks(view, view.me, 3);
  return [
    el("p", { class: "sheet-kicker" }, "Near you"),
    el("h2", {}, "Closest stations, stops and docks"),
    el(
      "ul",
      { class: "sheet-list" },
      stations.map(([m, s]) =>
        el("li", {}, el("button", { type: "button", class: "linkish", "data-station": s.key }, s.name, el("span", { class: "badges" }, s.lines.map((l) => badge(l, { link: false, small: true })))), el("span", { class: "muted small" }, walkText(m))),
      ),
    ),
    view.surfaceStops.size
      ? [
          el("p", { class: "sheet-label" }, "Bus and streetcar stops"),
          el(
            "ul",
            { class: "sheet-list" },
            [...view.surfaceStops.values()]
              .map((s) => [metres(view.me, [s[4], s[3]]), s])
              .sort((a, b) => a[0] - b[0])
              .slice(0, 3)
              .map(([m, s]) => el("li", {}, el("button", { type: "button", class: "linkish", "data-stop": s[0] }, s[2]), el("span", { class: "route-pills small" }, s[5].slice(0, 6).map((r) => routePill(view, r, { button: false }))), el("span", { class: "muted small" }, walkText(m)))),
          ),
        ]
      : null,
    el("p", { class: "sheet-label" }, mapState.mode === "bikes" ? "Docks with bikes" : "Docks with open spaces"),
    dockList(docks, view.me),
    el("p", { class: "muted small" }, `Location accurate to about ${Math.round(view.accuracy)} m. It stays on this device.`),
  ];
}

function openSheet(view, kind, id) {
  const fresh = !view.selected || view.selected.kind !== kind || view.selected.id !== id;
  view.selected = { kind, id };
  view.sheetBody.classList.toggle("sheet-fresh", fresh); // Charts draw in only for a new selection.
  const content =
    kind === "station" ? stationSheet(view, id)
    : kind === "dock" ? dockSheet(view, id)
    : kind === "train" ? trainSheet(view, id)
    : kind === "vehicle" ? vehicleSheet(view, id)
    : kind === "stop" ? stopSheet(view, id, null)
    : kind === "regional-train" ? regionalTrainSheet(view, id)
    : kind === "regional-station" ? regionalStationSheet(view, id, null)
    : kind === "road" ? roadSheet(view, id)
    : kind === "lane" ? laneSheet(id)
    : kind === "entrance" ? entranceSheet(view, id)
    : kind === "parking" ? parkingSheet(view, id)
    : kind === "rack" ? rackSheet(view, id)
    : kind === "place" ? placeSheet(view, id)
    : kind === "event" ? eventSheet(view, id)
    : kind === "food" ? foodSheet()
    : kind === "eat" ? eatSheet(view, id)
    : kind === "rec" ? recSheet(view, id)
    : kind === "weather" ? weatherSheet(view)
    : kind === "beach" ? beachSheet(view, id)
    : kind === "patio" ? patioSheet(view, id)
    : kind === "agenda" ? agendaSheet(view, id)
    : kind === "today" ? todaySheet(view)
    : kind === "plan" ? planSheet(view)
    : nearSheet(view);
  if (!content) return closeSheet(view);
  fill(view.sheetBody, content);
  view.sheet.classList.add("open");
  view.sheet.setAttribute("aria-hidden", "false");
  const selected = view.map.getSource("selected");
  const where =
    kind === "station" ? (() => { const s = view.data.stations.find((x) => x.key === id); return [s.lon, s.lat]; })()
    : kind === "dock" ? (() => { const d = view.data.bike_share.docks.find((x) => x.id === id); return [d.lon, d.lat]; })()
    : kind === "stop" && view.surfaceStops.get(id) ? [view.surfaceStops.get(id)[4], view.surfaceStops.get(id)[3]]
    : kind === "regional-station" ? (() => { const s = ((view.regional || {}).stations || []).find((x) => x.id === id); return s ? [s.lon, s.lat] : null; })()
    : kind === "entrance" ? (() => { const [key, eid] = id.split("|"); const s = view.data.stations.find((x) => x.key === key); const e = s && (s.entrances || []).find((x) => x.id === eid); return e ? e.point : null; })()
    : kind === "parking" ? (osmFeatures(view, "parking").find((f) => f.properties.role === "lot" && f.properties.id === id) || { geometry: {} }).geometry.coordinates || null
    : kind === "rack" ? (osmFeatures(view, "bikeParking").find((f) => f.properties.id === id) || { geometry: {} }).geometry.coordinates || null
    : kind === "place" ? (view.placeResults || [])[id]?.coord || null
    : kind === "food" ? (view.foodCoords && view.foodCoords.get(id)) || null
    : kind === "eat" ? (view.eats && view.eats.places[id] ? view.eats.places[id].slice(0, 2) : null)
    : kind === "rec" ? (() => { const p = recPlace(view, id); return p ? [p.lon, p.lat] : null; })()
    : kind === "beach" ? (() => { const p = beachPlace(view, id); return p ? [p.lon, p.lat] : null; })()
    : kind === "patio" ? (view.patios && view.patios.patios[id] ? view.patios.patios[id].slice(0, 2) : null)
    : kind === "agenda" ? (() => { const e = todaysEvents(view).find((x) => x.id === id); return e && e.lat != null ? [e.lon, e.lat] : null; })()
    : null;
  if (selected) selected.setData(featureCollection(where ? [point(where, {})] : []));
  if (where && fresh) requestAnimationFrame(() => revealPoint(view, where));
  if (kind === "stop") loadStopSheet(view, id);
  if (kind === "regional-station") loadRegionalStationSheet(view, id);
  const flows = view.map.getSource("flows");
  if (flows) flows.setData(featureCollection([]));
  if (kind === "dock") loadDockTrips(view, id);
  if (kind === "food") loadFoodSheet(view, id);
}

function sheetDrag(sheet) {
  const grip = sheet.querySelector(".sheet-grip");
  let drag = null;
  grip.addEventListener("pointerdown", (e) => {
    if (window.innerWidth >= 720) return;
    drag = { y: e.clientY, last: e.clientY, at: performance.now(), v: 0, height: sheet.offsetHeight };
    grip.setPointerCapture(e.pointerId);
    sheet.classList.add("dragging");
  });
  grip.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const now = performance.now();
    drag.v = (e.clientY - drag.last) / Math.max(1, now - drag.at);
    drag.last = e.clientY;
    drag.at = now;
    const dy = e.clientY - drag.y;
    sheet.style.transform = dy > 0 ? `translateY(${dy}px)` : "";
    sheet.style.maxHeight = dy < 0 ? `${drag.height - dy}px` : "";
  });
  const end = (e) => {
    if (!drag) return;
    const dy = e.clientY - drag.y;
    const v = drag.v;
    drag = null;
    sheet.classList.remove("dragging");
    sheet.style.transform = "";
    sheet.style.maxHeight = "";
    if (dy > 90 || v > 0.6) {
      if (sheet.classList.contains("full")) sheet.classList.remove("full");
      else sheet.querySelector(".sheet-close").click();
    } else if (dy < -50 || v < -0.6) sheet.classList.add("full");
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

// Brings a selected place into the part of the map the sheet does not cover, only when it
// is hidden, so the map never moves needlessly.
function revealPoint(view, coord) {
  const map = view.map;
  const box = map.getContainer().getBoundingClientRect();
  const phone = window.innerWidth < 720;
  const pad = phone
    ? { top: 160, bottom: Math.min(view.sheet.offsetHeight, box.height * 0.62) + 24, left: 24, right: 24 }
    : { top: 170, bottom: 40, left: 412, right: 70 };
  const p = map.project(coord);
  if (p.x > pad.left && p.x < box.width - pad.right && p.y > pad.top && p.y < box.height - pad.bottom) return;
  const target = [(pad.left + box.width - pad.right) / 2, (pad.top + box.height - pad.bottom) / 2];
  const center = map.unproject([box.width / 2 + p.x - target[0], box.height / 2 + p.y - target[1]]);
  map.easeTo({ center, duration: reduceMotion() ? 0 : 650, essential: false });
}

function closeSheet(view) {
  view.sheet.classList.remove("full");
  if (view.selected && view.selected.kind === "plan") {
    view.plan = null;
    drawPlan(view);
  }
  view.selected = null;
  view.sheet.classList.remove("open");
  view.sheet.setAttribute("aria-hidden", "true");
  const selected = view.map.getSource("selected");
  if (selected) selected.setData(featureCollection([]));
}

function flyTo(view, coord, zoom = MAP_FOCUS_ZOOM) {
  view.map.flyTo({ center: coord, zoom: Math.max(view.map.getZoom(), zoom), duration: reduceMotion() ? 0 : 900, essential: false });
}

function locate(view, button) {
  if (!("geolocation" in navigator) || !window.isSecureContext) {
    fill(view.sheetBody, el("p", { class: "sheet-kicker" }, "Near you"), el("p", {}, LOCATION_UNSUPPORTED));
    view.sheet.classList.add("open");
    return;
  }
  if (view.watch !== null && view.me) {
    flyTo(view, view.me);
    openSheet(view, "near");
    return;
  }
  button.classList.add("busy");
  openSheet(view, "near");
  let first = true;
  view.watch = watchLocation(
    (position) => {
      button.classList.remove("busy");
      button.classList.add("on");
      view.me = [position.coords.longitude, position.coords.latitude];
      view.accuracy = position.coords.accuracy;
      view.map.getSource("me").setData(featureCollection([circlePolygon(view.me, view.accuracy), point(view.me, {})]));
      if (first) {
        first = false;
        flyTo(view, view.me);
        openSheet(view, "near");
      } else if (view.selected && view.selected.kind === "near") {
        openSheet(view, "near");
      }
    },
    (error) => {
      button.classList.remove("busy");
      view.watch.stop();
      view.watch = null;
      fill(view.sheetBody, el("p", { class: "sheet-kicker" }, "Near you"), el("p", {}, locationErrorText(error)));
      view.sheet.classList.add("open");
    },
    { maximumAge: 15000 },
  );
}

function pulseMe(view) {
  if (reduceMotion()) return;
  const step = (now) => {
    if (!activeMap || activeMap !== view) return;
    if (view.me && view.map.getLayer("me-pulse")) {
      const t = (now % 2000) / 2000;
      view.map.setPaintProperty("me-pulse", "circle-radius", 9 + t * 18);
      view.map.setPaintProperty("me-pulse", "circle-opacity", 0.3 * (1 - t));
    }
    view.pulse = requestAnimationFrame(step);
  };
  view.pulse = requestAnimationFrame(step);
}

// The time the fastest vehicle takes to move a pixel at this zoom and latitude.
function motionGap(map) {
  const metresPerPixel = (40075016.7 * Math.cos((map.getCenter().lat * Math.PI) / 180)) / (512 * 2 ** map.getZoom());
  return Math.min(MOTION_MAX_MS, Math.max(MOTION_MIN_MS, (metresPerPixel / MOTION_SPEED_MPS) * 1000));
}

// Redraws moving vehicles once they can have moved a pixel (motionGap), and at most every
// MOTION_PANNING_MS while the map moves; with reduced motion, every MOTION_STILL_MS.
// Nothing is redrawn while the tab is hidden.
function animateVehicles(view) {
  if (reduceMotion()) {
    view.timers.push(setInterval(() => { if (document.visibilityState === "visible") moveVehicles(view); }, MOTION_STILL_MS));
    return;
  }
  let last = 0;
  const step = (now) => {
    if (activeMap !== view) return;
    if (document.visibilityState !== "visible") {
      view.animations.vehicles = requestAnimationFrame(step);
      return;
    }
    const gap = view.map.isMoving() ? Math.max(MOTION_PANNING_MS, motionGap(view.map)) : motionGap(view.map);
    if (now - last >= gap) {
      last = now;
      moveVehicles(view);
    }
    view.animations.vehicles = requestAnimationFrame(step);
  };
  view.animations = view.animations || {};
  view.animations.vehicles = requestAnimationFrame(step);
}

function mapCleanup() {
  if (!activeMap) return;
  const view = activeMap;
  if (view.guide) {
    view.guide.source.stop();
    clearInterval(view.guide.liveTimer);
    view.guide.wakeLock?.release?.().catch(() => {});
  }
  activeMap = null;
  view.timers.forEach(clearInterval);
  Object.values(view.animations || {}).forEach(cancelAnimationFrame);
  cancelAnimationFrame(view.pulse);
  view.watch?.stop();
  mapState.view = { center: view.map.getCenter().toArray(), zoom: view.map.getZoom() };
  view.map.remove();
  document.body.classList.remove("map-mode");
}

function coordsOf(data) {
  return new Map(data.stations.map((s) => [s.key, [s.lon, s.lat]]));
}

async function refreshTrains(view) {
  if (document.visibilityState !== "visible") return;
  try {
    const [trains, vehicles] = await Promise.all([api("/api/trains"), api("/api/vehicles")]);
    view.trains = trains;
    view.vehicles = vehicleRecords(vehicles);
    newPositions(view);
    refreshRegional(view);
    if (view.selected && view.selected.kind === "stop") loadStopSheet(view, view.selected.id);
    else if (view.selected && view.selected.kind === "vehicle") openSheet(view, "vehicle", view.selected.id);
    fill(view.status, view.trains.predictions_as_of.stale ? el("span", { class: "warn" }, "Train predictions may be out of date") : [el("span", { class: "live-dot", "aria-hidden": "true" }), `Live · ${timeFmt.format(new Date())}`]);
  } catch {
    fill(view.status, el("span", { class: "warn" }, "Could not refresh trains; retrying"));
  }
}

async function refreshRegional(view) {
  try {
    view.regionalTrains = await api("/api/regional/trains");
    newPositions(view);
    if (view.selected && view.selected.kind === "regional-train") openSheet(view, "regional-train", view.selected.id);
  } catch {
    /* Keep the last positions; the next refresh tries again. */
  }
}

async function refreshRoads(view) {
  try {
    view.roads = await api("/api/roads");
    view.map.getSource("roads").setData(roadFeatures(view.roads));
  } catch {
    /* Roadwork is extra context; the map works without it. */
  }
}

async function refreshNetwork(view) {
  if (document.visibilityState !== "visible") return;
  try {
    view.data = await api("/api/map");
    view.coords = coordsOf(view.data);
    const network = networkFeatures(view.data);
    view.map.getSource("stations").setData(network.stations);
    view.map.getSource("entrances").setData(network.entrances);
    view.map.getSource("docks").setData(dockFeatures(view.data, mapState.mode));
    refreshRoads(view);
    if (view.selected) openSheet(view, view.selected.kind, view.selected.id);
  } catch {
    /* Keep showing the last data; the next refresh tries again. */
  }
}

// Each switchable layer, in the order the panel lists them.
const MAP_LAYERS = [
  { key: "trains", label: "Subway", note: "Estimated train positions", icon: () => badgeEl("subway", lineColour("1"), textOn(lineColour("1"))) },
  { key: "regional", label: "Regional trains", note: "Live where reported", icon: () => badgeEl("regional", "#4b5563", "#ffffff") },
  { key: "buses", label: "Buses and streetcars", note: "Live positions and stops", icon: () => badgeEl("bus", SURFACE_COLOURS.bus, "#ffffff") },
  { key: "bikes", label: "Bike Share", note: "Live bikes or open docks", icon: () => badgeEl("bike", DOCK_COLOURS.ok, "#ffffff") },
  { key: "access", label: "Elevator outages", note: "Stations with an elevator or escalator out", icon: () => badgeEl("elevator", OUTAGE_COLOUR, "#141d3b") },
  { key: "entrances", label: "Station entrances", note: "Close up; step-free ones in blue", icon: () => badgeEl("entrance", ENTRANCE_COLOUR, "#ffffff") },
  { key: "roads", label: "Roadwork", note: "Construction and road closures", icon: () => badgeEl("roadwork", ROAD_COLOURS.construction, "#141d3b") },
  { key: "events", label: "Events", note: "Festivals and events this week", icon: () => badgeEl("event", EVENT_COLOUR, "#ffffff") },
  { key: "food", label: "Food inspections", note: "Latest result at each restaurant and food shop", icon: () => badgeEl("food", FOOD_COLOURS.pass, "#ffffff") },
  { key: "lanes", label: "Bike lanes", note: "City cycling network", icon: () => badgeEl("lane", LANE_COLOURS.protected, "#ffffff") },
  { key: "bikeParking", label: "Bike parking", note: "Racks, shelters and lockers", icon: () => badgeEl("rack", BIKE_PARKING_COLOUR, "#ffffff") },
  { key: "parking", label: "Parking", note: "Public car parks and their entrances", icon: () => badgeEl("parking", PARKING_COLOUR, "#ffffff") },
];

function lineSwatch(colour, { dashed = false, dotted = false } = {}) {
  return el("span", { class: `swatch-line${dashed ? " dashed" : ""}${dotted ? " dotted" : ""}`, style: { borderColor: colour }, "aria-hidden": "true" });
}

function legendRow(symbol, text) {
  return el("li", {}, el("span", { class: "legend-symbol" }, symbol), el("span", {}, text));
}

function mapLegend() {
  const surface = Object.entries(SURFACE_LABELS);
  return el(
    "div",
    { class: "map-legend" },
    el("p", { class: "panel-heading" }, "Legend"),
    el("ul", { class: "legend-list" },
      lines.map((l) => legendRow(badgeEl("subway", l.color || "#777", textOn(l.color || "#777777"), { size: 22 }), `Line ${l.id} train (${l.timetable ? "placed by timetable" : "estimated position"})`)),
      surface.map(([mode, label]) => legendRow(badgeEl(surfaceGlyph(mode), SURFACE_COLOURS[mode], "#ffffff", { size: 22 }), `${label}, with the pointer showing its heading`)),
      legendRow(el("span", { class: "swatch-pill" }, "LW"), "Regional train, live where Metrolinx reports it, else by timetable"),
      legendRow(el("span", { class: "swatch-station", "aria-hidden": "true" }), "Station"),
      legendRow(badgeEl("elevator", OUTAGE_COLOUR, "#141d3b", { size: 20 }), "Elevator or escalator out of service"),
    ),
    el("p", { class: "muted small" }, "Between updates, trains, buses and streetcars move along their routes by estimate: subway trains to TTC's predicted arrival, buses at their route's usual pace from their last report, regional trains along their timetable from their last live report. A faded bus or streetcar has not reported for over two and a half minutes."),
    el("p", { class: "panel-subheading" }, "Entrances and parking"),
    el("ul", { class: "legend-list compact" },
      legendRow(badgeEl("entrance", ENTRANCE_COLOUR, "#ffffff", { size: 20 }), "Station entrance"),
      legendRow(badgeEl("stepfree", STEP_FREE_COLOUR, "#ffffff", { size: 20 }), "Step-free entrance"),
      legendRow(badgeEl("parking", PARKING_COLOUR, "#ffffff", { size: 20 }), "Car park"),
      legendRow(badgeEl("entrance", PARKING_COLOUR, "#ffffff", { size: 18 }), "Car park entrance"),
      legendRow(badgeEl("rack", BIKE_PARKING_COLOUR, "#ffffff", { size: 20 }), "Bike parking")),
    el("p", { class: "panel-subheading" }, mapState.mode === "bikes" ? "Bike Share: bikes available" : "Bike Share: open docks"),
    el("ul", { class: "legend-list compact" },
      Object.entries(DOCK_COLOURS).map(([level, colour]) => legendRow(badgeEl("bike", colour, "#ffffff", { size: 20 }), DOCK_LEVEL_LABELS[level]))),
    el("p", { class: "panel-subheading" }, "Roads and bike lanes"),
    el("ul", { class: "legend-list compact" },
      legendRow(lineSwatch(ROAD_COLOURS.construction), "Construction"),
      legendRow(lineSwatch(ROAD_COLOURS.closure, { dashed: true }), "Road closed"),
      Object.entries(LANE_COLOURS).map(([kind, colour]) => legendRow(lineSwatch(colour, { dashed: kind === "trail" }), LANE_LABELS[kind]))),
    el("p", { class: "panel-subheading" }, "Other"),
    el("ul", { class: "legend-list compact" },
      legendRow(badgeEl("event", EVENT_COLOUR, "#ffffff", { size: 20 }), "Event today"),
      legendRow(badgeEl("event", prefersDark() ? "#94a3b8" : EVENT_COLOUR, "#ffffff", { size: 20, hollow: true }), "Event later this week"),
      FOOD_LEVELS.slice(0, 3).map((level) => legendRow(badgeEl("food", FOOD_COLOURS[level], level === "conditional" ? "#141d3b" : "#ffffff", { size: 20 }), `Food inspection: ${FOOD_LABELS[level].toLowerCase()}`)),
      legendRow(el("span", { class: "swatch-me", "aria-hidden": "true" }), "Your location"),
      legendRow(lineSwatch(prefersDark() ? "#eef1f8" : "#141d3b"), "Planned trip")),
  );
}

// Safety information for riders. It is reference only: numbers are plain text, and the
// site never calls or messages anyone. Checked against ttc.ca and the services' own pages,
// October 2026.
const SAFETY_TTC = [
  ["Yellow emergency alarm", "On subway trains, beside the doors. It alerts the operator, and Transit Control calls 911."],
  ["Designated Waiting Area", "On every subway platform: brighter lighting, cameras, an intercom to TTC staff and a phone that calls 911 for free."],
  ["Request Stop", "On buses from 9 pm to 5 am, if you are travelling alone: ask the operator at least one stop ahead to let you off between stops. You leave by the front doors. Not on streetcars."],
  ["Report a safety concern", "Text TTC Transit Control at 647-496-1940, or use the SafeTTC app."],
];
const SAFETY_LINES = [
  ["Toronto Police, non-emergency", "416-808-2222, or *877 from most mobile phones"],
  ["Assaulted Women's Helpline", "416-863-0511 or 1-866-863-0511; 24 hours, free and anonymous"],
  ["Victim Services Toronto", "416-808-7066"],
  ["Toronto Community Crisis Service", "211, for a mental health crisis that does not need police"],
  ["Suicide Crisis Helpline", "Call or text 988; 24 hours"],
];

function safetyItems(items) {
  return el("ul", { class: "safety-list" }, items.map(([title, text]) => el("li", {}, el("strong", {}, title), el("span", {}, text))));
}

function safetyPanel(view) {
  const from = view.guide?.fix ? [view.guide.fix.lon, view.guide.fix.lat] : view.me;
  const nearest = from ? view.data.stations.map((s) => [metres(from, [s.lon, s.lat]), s]).sort((a, b) => a[0] - b[0])[0] : null;
  return [
    el("div", { class: "panel-head" },
      el("p", { class: "panel-heading" }, "Safety"),
      el("button", { type: "button", class: "panel-close", "aria-label": "Close safety" }, iconSvg("close", { size: 18 }))),
    el("div", { class: "safety-urgent" },
      el("p", { class: "safety-big" }, "In danger now: call 911"),
      el("p", {}, "Free from any phone. Tell them where you are; the nearest cross streets help.")),
    el("p", { class: "panel-subheading" }, "On the TTC"),
    safetyItems(SAFETY_TTC),
    nearest ? el("p", { class: "muted small" }, `Nearest subway station: ${nearest[1].name}, ${walkText(nearest[0])}.`) : null,
    el("p", { class: "panel-subheading" }, "Support"),
    safetyItems(SAFETY_LINES),
    el("p", { class: "muted small" }, "For reference only: this site does not call or message anyone."),
  ];
}

function layerSwitch(on, label, onClick) {
  const button = el("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(on), "aria-label": label }, el("span", { class: "switch-knob", "aria-hidden": "true" }));
  button.addEventListener("click", onClick);
  return button;
}

function chip(label, pressed, onClick, extra = {}) {
  const button = el("button", { type: "button", class: "chip", "aria-pressed": String(pressed), ...extra }, label);
  button.addEventListener("click", onClick);
  return button;
}

// A text page for each bus or streetcar stop: the same estimates as the map's panel.
async function renderStop(id) {
  const data = await api(`/api/stops/${encodeURIComponent(id)}`);
  const routes = [];
  for (const r of data.routes) {
    let entry = routes.find((x) => x.route_id === r.route_id);
    if (!entry) routes.push((entry = { ...r, towards: [] }));
    entry.towards.push(r.towards);
  }
  return [
    el("p", { class: "eyebrow" }, stopKicker(data.stop)),
    titleRow(el("h1", {}, data.stop.name), "stop", id, data.stop.name),
    el("h2", {}, "Next vehicles (estimated)"),
    el("div", { class: "card" }, stopArrivals(data)),
    el("p", { class: "muted small" }, `${estimateNote(id)} They assume the vehicle keeps the schedule's pace.`),
    el("h2", {}, "Routes at this stop"),
    el(
      "ul",
      { class: "card plain-list" },
      routes.map((r) => el("li", {}, el("span", { class: "with-badge" }, el("span", { class: `route-pill ${r.mode}` }, r.short_name), el("span", {}, `${r.long_name} towards ${r.towards.join(" or ")}`)))),
    ),
    el("p", {}, el("a", { href: `#/map` }, "Open the map")),
  ];
}

async function renderMap(focusKey) {
  const [data, trains, hasBasemap] = await Promise.all([
    api("/api/map"),
    api("/api/trains"),
    fetch("/map-data/toronto.json", { method: "HEAD" }).then((r) => r.ok).catch(() => false),
  ]);
  const lib = await loadMapLibrary();
  const container = el("div", { class: "map-canvas", role: "region", "aria-label": "Map of subway and regional train lines and trains, buses, streetcars and their stops, and Bike Share docks" });
  const sheetBody = el("div", { class: "sheet-body", "aria-live": "polite" });
  const closeButton = el("button", { type: "button", class: "sheet-close", "aria-label": "Close" }, "×");
  const sheet = el("aside", { class: "map-sheet", "aria-hidden": "true" }, el("div", { class: "sheet-grip", "aria-hidden": "true" }), closeButton, sheetBody);
  sheetDrag(sheet);
  const status = el("p", { class: "map-status", "aria-live": "polite" });
  const locateButton = el("button", { type: "button", class: "map-fab locate", "aria-label": "Show where I am and what is nearest", title: "Near me" }, iconSvg("locate", { size: 22 }));
  const planButton = el("button", { type: "button", class: "map-fab plan", "aria-label": "Directions", title: "Directions", hidden: "hidden" }, iconSvg("directions", { size: 22 }));
  const layersButton = el("button", { type: "button", class: "map-tool", "aria-label": "Layers and legend", "aria-expanded": "false", "aria-controls": "map-layers", title: "Layers and legend" }, iconSvg("layers", { size: 22 }));
  const layersPanel = el("aside", { class: "map-panel", id: "map-layers", "aria-label": "Layers and legend", hidden: "hidden" });
  const safetyButton = el("button", { type: "button", class: "map-tool", "aria-label": "Safety", "aria-expanded": "false", "aria-controls": "map-safety", title: "Safety" }, iconSvg("shield", { size: 22 }));
  const safetyPanelEl = el("aside", { class: "map-panel safety", id: "map-safety", "aria-label": "Safety", hidden: "hidden" });
  // The transit map and the food map, switched like the light and dark themes.
  const kindButtons = { transit: ["subway", "Transit"], food: ["food", "Food"], city: ["park", "City"], weather: ["cloud", "Weather"] };
  for (const [kind, [glyph, label]] of Object.entries(kindButtons)) {
    kindButtons[kind] = el("button", { type: "button", class: "map-kind-option", "aria-pressed": "false", "data-kind": kind }, iconSvg(glyph, { size: 18 }), label);
  }
  const kindThumb = el("span", { class: "map-kind-thumb", "aria-hidden": "true" });
  const kindSwitch = el("div", { class: "map-kind has-thumb", role: "group", "aria-label": "Map" }, kindThumb, kindButtons.transit, kindButtons.food, kindButtons.city, kindButtons.weather);
  // The highlight follows the pressed option; it moves only after the first placement.
  const placeThumb = () => {
    const on = Object.values(kindButtons).find((b) => b.getAttribute("aria-pressed") === "true");
    if (!on || !on.offsetWidth) return;
    kindThumb.style.width = `${on.offsetWidth}px`;
    kindThumb.style.transform = `translateX(${on.offsetLeft - 3}px)`;
    requestAnimationFrame(() => kindThumb.classList.add("ready"));
  };
  new MutationObserver(placeThumb).observe(kindSwitch, { subtree: true, attributes: true, attributeFilter: ["aria-pressed"] });
  if ("ResizeObserver" in window) new ResizeObserver(placeThumb).observe(kindSwitch);
  const search = el("input", { type: "search", id: "map-search", name: "q", class: "map-search", placeholder: "Stations, routes, places", "aria-label": "Search stations, stops, routes, addresses, places or restaurants", autocomplete: "off", enterkeyhint: "go" });
  const results = el("ul", { class: "map-results" });
  const modeGroup = el("span", { class: "chip-group", role: "group", "aria-label": "Show Bike Share" });
  const kindChips = el("div", { class: "chips food-kinds", role: "group", "aria-label": "Places to eat shown", hidden: "hidden" });
  const toolbar = el("div", { class: "map-toolbar" }, el("div", { class: "map-search-row" }, el("div", { class: "map-search-wrap" }, search, results), layersButton, safetyButton), kindSwitch, kindChips, status);
  const guideTop = el("div", { class: "guide-card", "aria-live": "polite" });
  const guideBar = el("div", { class: "guide-bar" });
  const page = el("div", { class: "map-page" }, container, toolbar, layersPanel, safetyPanelEl, guideTop, guideBar, planButton, locateButton, sheet, hasBasemap ? null : el("p", { class: "map-note" }, "Street map not built yet: run the map-tiles tool (docs/map.md)."));

  const view = {
    map: null, timers: [], watch: null, data, trains, coords: coordsOf(data), sheet, sheetBody, status, me: null, accuracy: 0, selected: null,
    pulse: 0, trainFeatures: null, vehicleFeatures: null, surfaceStops: new Map(), surfaceRoutes: new Map(), route: null,
    regional: null, regionalTrains: null, regionalTrainFeatures: null, roads: null, events: null,
    planner: null, plan: null, guide: null, page, guideTop, guideBar,
  };

  const renderKind = () => {
    const food = mapState.kind === "food";
    const transit = mapState.kind === "transit";
    for (const [kind, button] of Object.entries(kindButtons)) button.setAttribute("aria-pressed", String(mapState.kind === kind));
    safetyButton.hidden = !transit;
    search.placeholder = food ? "Restaurants, places" : transit ? "Stations, routes, places" : "Places, addresses";
    layersButton.setAttribute("aria-label", food ? "Food map filters" : transit ? "Layers and legend" : mapState.kind === "weather" ? "Weather map legend" : "City map filters");
    layersButton.title = transit ? "Layers and legend" : "Filters";
  };
  kindSwitch.addEventListener("click", (event) => {
    const kind = event.target.closest("[data-kind]")?.dataset.kind;
    if (!kind || kind === mapState.kind) return;
    if (kind !== "transit") setSafetyOpen(false);
    mapState.kind = kind;
    try {
      localStorage.setItem(MAP_KIND_KEY, mapState.kind);
    } catch {
      /* Private browsing: the choice lasts for this visit. */
    }
    renderKind();
    renderChips();
    search.value = "";
    fill(results);
    if (view.map) {
      applyLayerToggles(view.map);
      loadFood(view);
      loadCity(view);
      if (view.selected) closeSheet(view);
      loadWeather(view, { open: true });
    }
  });
  renderKind();

  const renderChips = () => {
    view.renderChips = renderChips;
    kindChips.hidden = mapState.kind === "transit";
    if (mapState.kind === "weather") {
      fill(kindChips, weatherChips(renderChips, view));
      fill(layersPanel, weatherPanel());
      layersPanel.querySelector(".panel-close").addEventListener("click", () => setLayersOpen(false));
      return;
    }
    if (mapState.kind === "city") {
      fill(kindChips, cityChips(renderChips, view));
      fill(layersPanel, cityPanel(renderChips, view));
      layersPanel.querySelector(".panel-close").addEventListener("click", () => setLayersOpen(false));
      return;
    }
    if (mapState.kind === "food") {
      fill(kindChips, foodKindChips(renderChips, view));
      fill(layersPanel, foodPanel(renderChips, view));
      layersPanel.querySelector(".panel-close").addEventListener("click", () => setLayersOpen(false));
      return;
    }
    fill(modeGroup, Object.entries(MAP_MODES).map(([mode, label]) =>
      chip(label, mapState.mode === mode, () => {
        mapState.mode = mode;
        view.map.getSource("docks").setData(dockFeatures(view.data, mode));
        renderChips();
        if (view.selected) openSheet(view, view.selected.kind, view.selected.id);
      }, { class: "chip small" }),
    ));
    fill(layersPanel,
      el("div", { class: "panel-head" },
        el("p", { class: "panel-heading" }, "Layers"),
        el("button", { type: "button", class: "panel-close", "aria-label": "Close layers" }, iconSvg("close", { size: 18 }))),
      el("ul", { class: "layer-list" }, MAP_LAYERS.map((layer) => el(
        "li",
        { class: mapState.layers[layer.key] ? "layer on" : "layer" },
        layer.icon(),
        el("span", { class: "layer-text" }, el("span", { class: "layer-label" }, layer.label), el("span", { class: "muted small" }, layer.note),
          layer.key === "bikes" && mapState.layers.bikes ? modeGroup : null),
        layerSwitch(mapState.layers[layer.key], layer.label, () => {
          mapState.layers[layer.key] = !mapState.layers[layer.key];
          applyLayerToggles(view.map);
          loadOsmLayers(view);
          loadFood(view);
          renderChips();
        }),
      ))),
      mapLegend(),
    );
    layersPanel.querySelector(".panel-close").addEventListener("click", () => setLayersOpen(false));
  };
  const setLayersOpen = (open) => {
    if (open) setSafetyOpen(false);
    layersPanel.hidden = !open;
    layersButton.setAttribute("aria-expanded", String(open));
    layersButton.classList.toggle("on", open);
    if (open) layersPanel.querySelector(".switch")?.focus();
  };
  const setSafetyOpen = (open) => {
    if (open) {
      setLayersOpen(false);
      fill(safetyPanelEl, safetyPanel(view));
      safetyPanelEl.querySelector(".panel-close").addEventListener("click", () => setSafetyOpen(false));
    }
    safetyPanelEl.hidden = !open;
    safetyButton.setAttribute("aria-expanded", String(open));
    safetyButton.classList.toggle("on", open);
    if (open) safetyPanelEl.querySelector(".panel-close").focus();
  };
  view.setSafetyOpen = setSafetyOpen;
  layersButton.addEventListener("click", () => setLayersOpen(layersPanel.hidden));
  safetyButton.addEventListener("click", () => setSafetyOpen(safetyPanelEl.hidden));
  page.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !layersPanel.hidden) {
      setLayersOpen(false);
      layersButton.focus();
    } else if (event.key === "Escape" && !safetyPanelEl.hidden) {
      setSafetyOpen(false);
      safetyButton.focus();
    }
  });

  search.addEventListener("input", () => {
    const query = searchKey(search.value);
    clearTimeout(view.placeTimer);
    if (!query) return fill(results);
    const typed = search.value;
    view.placeTimer = setTimeout(async () => {
      const [found, food] = await Promise.all([findPlaces(view, typed), findFood(view, typed)]);
      if (search.value !== typed || !(found.length || food.length)) return;
      view.placeResults = found.slice(0, 5);
      view.foodResults = food;
      results.querySelectorAll(".place-result").forEach((n) => n.remove());
      const placeItems = suggestionList(view.placeResults, "data-place");
      const foodItems = suggestionList(food, "data-food");
      const items = mapState.kind === "food" ? [...foodItems, ...placeItems] : [...placeItems, ...foodItems];
      results.append(...items.map((li) => { li.classList.add("place-result"); return li; }));
    }, SUGGEST_MS);
    const transit = mapState.kind === "transit";
    const stations = !transit ? [] : view.data.stations.filter((s) => searchKey(s.name).includes(query)).slice(0, 4);
    const regionalStations = !transit ? [] : ((view.regional || {}).stations || []).filter((s) => searchKey(s.name).includes(query)).slice(0, 3);
    const routes = !transit ? [] : [...view.surfaceRoutes.values()].filter((r) => r.short_name === query || searchKey(`${r.short_name} ${r.long_name}`).startsWith(query)).slice(0, 3);
    const stops = !transit || query.length < 3 ? [] : [...view.surfaceStops.values()].filter((s) => s[1] === query || searchKey(s[2]).includes(query)).slice(0, 5);
    fill(
      results,
      stations.map((s) => el("li", {}, el("button", { type: "button", "data-station": s.key }, s.name, el("span", { class: "badges" }, s.lines.map((l) => badge(l, { link: false, small: true })))))),
      regionalStations.map((s) => el("li", {}, el("button", { type: "button", "data-regional-station": s.id }, s.name, el("span", { class: "badges" }, s.lines.map((code) => regionalPill(code, (view.regional.lines.find((l) => l.code === code) || {}).colour, { small: true })))))),
      routes.map((r) => el("li", {}, el("button", { type: "button", "data-route": r.route_id }, `${r.short_name} ${r.long_name}`, el("span", { class: `route-pill ${r.mode}` }, SURFACE_LABELS[r.mode])))),
      stops.map((s) => el("li", {}, el("button", { type: "button", "data-stop": s[0] }, s[2], el("span", { class: "muted small" }, s[1] ? `Stop ${s[1]}` : "")))),
    );
  });
  search.addEventListener("keydown", (event) => {
    const first = results.querySelector("button");
    if (event.key === "Enter" && first) first.click();
    if (event.key === "Escape") { search.value = ""; fill(results); }
  });
  page.addEventListener("input", (event) => {
    const input = event.target.closest("[data-plan-query]");
    if (!input || !view.plan) return;
    view.plan.query[input.dataset.planQuery] = input.value;
    suggestPlanPlaces(view, input.dataset.planQuery, input.value);
  });
  page.addEventListener("keydown", (event) => {
    const input = event.target.closest("[data-plan-query]");
    if (!input || event.key !== "Enter") return;
    event.preventDefault();
    const first = view.sheetBody.querySelector(`#plan-suggest-${input.dataset.planQuery} button`);
    if (first) first.click();
  });
  page.addEventListener("change", (event) => {
    if (!view.plan) return;
    if (event.target.matches("[data-plan-when]")) {
      view.plan.when = event.target.value;
      if (view.plan.when !== "now" && !view.plan.at) {
        const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
        view.plan.at = local.toISOString().slice(0, 16);
      }
    } else if (event.target.matches("[data-plan-at]")) {
      view.plan.at = event.target.value;
    } else if (event.target.matches("[data-plan-stepfree]")) {
      view.plan.stepFree = event.target.checked;
      savePlanPrefs(view.plan);
    } else return;
    if (view.plan.from && view.plan.to) runPlan(view);
    else openSheet(view, "plan");
  });
  page.addEventListener("click", (event) => {
    if (event.target.closest("[data-safety]")) return view.setSafetyOpen(safetyPanelEl.hidden);
    const guideAction = event.target.closest("[data-guide], [data-guide-end], [data-guide-recenter], [data-guide-replan]");
    if (guideAction) {
      const d = guideAction.dataset;
      if (d.guide) startGuidance(view, d.guide);
      else if (d.guideEnd) stopGuidance(view);
      else if (d.guideRecenter && view.guide) {
        view.guide.follow = true;
        if (view.guide.fix) onGuideFix(view, view.guide, view.guide.fix);
      } else if (d.guideReplan && view.guide?.fix) {
        const fix = view.guide.fix;
        stopGuidance(view, { keepPlan: true });
        view.plan.from = { coord: [fix.lon, fix.lat], name: "Your location" };
        runPlan(view);
      }
      return;
    }
    const choose = event.target.closest("[data-plan-choose]");
    if (choose && view.plan && view.suggestions) {
      const { key, items } = view.suggestions;
      const item = items[Number(choose.dataset.planChoose)];
      view.plan.query[key] = null;
      view.suggestions = null;
      return setPlanEnd(view, key, { coord: item.coord, name: item.name });
    }
    const modeChip = event.target.closest("[data-plan-mode]");
    if (modeChip && view.plan) {
      const mode = modeChip.dataset.planMode;
      view.plan.modes = view.plan.modes.includes(mode) ? view.plan.modes.filter((m) => m !== mode) : [...view.plan.modes, mode];
      savePlanPrefs(view.plan);
      return view.plan.from && view.plan.to ? runPlan(view) : openSheet(view, "plan");
    }
    const agendaHit = event.target.closest("[data-agenda]");
    if (agendaHit) {
      const e = todaysEvents(view).find((x) => x.id === agendaHit.dataset.agenda);
      if (e && e.lat != null) flyTo(view, [e.lon, e.lat], 15);
      return openSheet(view, "agenda", agendaHit.dataset.agenda);
    }
    const foodHit = event.target.closest("[data-food]");
    if (foodHit && view.foodResults) {
      const item = view.foodResults[Number(foodHit.dataset.food)];
      search.value = "";
      fill(results);
      flyTo(view, item.coord, 17);
      return openSheet(view, "food", item.id);
    }
    const placeHit = event.target.closest("[data-place]");
    if (placeHit && view.placeResults) {
      const item = view.placeResults[Number(placeHit.dataset.place)];
      search.value = "";
      fill(results);
      flyTo(view, item.coord, 16.5);
      return openSheet(view, "place", Number(placeHit.dataset.place));
    }
    const planAction = event.target.closest("[data-plan-pick], [data-plan-me], [data-plan-swap], [data-plan-trip], [data-plan-set]");
    if (planAction) {
      const d = planAction.dataset;
      if (d.planSet) {
        view.plan = view.plan || newPlan();
        return setPlanEnd(view, d.planSet, { coord: d.planCoord.split(",").map(Number), name: d.planName });
      }
      if (d.planPick) view.plan.pick = view.plan.pick === d.planPick ? null : d.planPick;
      else if (d.planMe) return planFromMe(view);
      else if (d.planSwap) {
        [view.plan.from, view.plan.to] = [view.plan.to, view.plan.from];
        view.plan.query = { from: null, to: null };
        if (view.plan.from && view.plan.to) return runPlan(view);
        drawPlan(view);
      } else if (d.planTrip) {
        view.plan.chosen = Number(d.planTrip);
        drawPlan(view);
        fitPlan(view);
      }
      return openSheet(view, "plan");
    }
    const entrance = event.target.closest("[data-entrance]");
    if (entrance) {
      const [key, eid] = entrance.dataset.entrance.split("|");
      const e = (view.data.stations.find((x) => x.key === key)?.entrances || []).find((x) => x.id === eid);
      if (e) flyTo(view, e.point, 17);
      return openSheet(view, "entrance", entrance.dataset.entrance);
    }
    const station = event.target.closest("[data-station]");
    const dock = event.target.closest("[data-dock]");
    const stop = event.target.closest("[data-stop]");
    const route = event.target.closest("[data-route]");
    const regionalStation = event.target.closest("[data-regional-station]");
    const cityEvent = event.target.closest("[data-event]");
    if (cityEvent) {
      const e = view.events.events.find((x) => x.event_id === cityEvent.dataset.event);
      flyTo(view, [e.lon, e.lat], 15.5);
      return openSheet(view, "event", e.event_id);
    }
    if (stop || route || regionalStation) {
      search.value = "";
      fill(results);
    }
    if (regionalStation) {
      const s = view.regional.stations.find((x) => x.id === regionalStation.dataset.regionalStation);
      if (!mapState.layers.regional) { mapState.layers.regional = true; applyLayerToggles(view.map); renderChips(); }
      flyTo(view, [s.lon, s.lat], 13);
      openSheet(view, "regional-station", s.id);
    } else if (stop) {
      const s = view.surfaceStops.get(stop.dataset.stop);
      if (s) flyTo(view, [s[4], s[3]], 16.5);
      openSheet(view, "stop", stop.dataset.stop);
    } else if (route) {
      if (!mapState.layers.buses) { mapState.layers.buses = true; applyLayerToggles(view.map); renderChips(); }
      showRoute(view, route.dataset.route);
    } else if (station) {
      const s = view.data.stations.find((x) => x.key === station.dataset.station);
      search.value = "";
      fill(results);
      flyTo(view, [s.lon, s.lat]);
      openSheet(view, "station", s.key);
    } else if (dock) {
      const d = view.data.bike_share.docks.find((x) => x.id === dock.dataset.dock);
      flyTo(view, [d.lon, d.lat], 16);
      openSheet(view, "dock", d.id);
    }
  });
  closeButton.addEventListener("click", () => {
    clearRoute(view);
    closeSheet(view);
  });
  locateButton.addEventListener("click", () => locate(view, locateButton));
  planButton.addEventListener("click", () => {
    view.plan = view.plan || newPlan();
    openSheet(view, "plan");
  });

  page.mount = () => {
    mapCleanup();
    document.body.classList.add("map-mode");
    const start = mapState.view || MAP_HOME;
    const map = new lib.Map({
      container, style: mapStyle(hasBasemap), center: start.center, zoom: start.zoom, minZoom: 8, maxZoom: 18.5,
      maxBounds: MAP_BOUNDS, attributionControl: false, dragRotate: false, pitchWithRotate: false,
      // Labels appear without a fade: the fade kept the map repainting for 200 ms after
      // every vehicle redraw, which with moving vehicles meant without pause.
      fadeDuration: 0,
      // Tiles are kept for 3 zoom levels around the current one rather than 5: every
      // source keeps its own, and the map's memory went past 700 MB on a large screen.
      maxTileCacheZoomLevels: 3,
      // The canvas and its buffers grow with the square of the pixel ratio; past 2 (most
      // phones are 3) the extra sharpness is hard to see but uses 2.25 times the memory.
      pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new lib.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new lib.AttributionControl({ compact: true, customAttribution: "Data: TTC, Bike Share Toronto, City of Toronto and Toronto Parking Authority (OGL – Toronto); regional rail data provided with the permission of Metrolinx; Brampton Transit data: City of Brampton (CC BY 4.0); MiWay data: City of Mississauga Terms of Use; contains public transit Information made available under YRT's Open Data Licence" }), "bottom-left");
    view.map = map;
    activeMap = view;
    renderChips();
    map.on("load", () => {
      addOverlayLayers(map, view.data);
      addSurfaceLayers(map);
      addRegionalLayers(map);
      addRoadLayers(map);
      addLaneLayers(view);
      addOsmLayers(view);
      addEventLayers(view);
      addFoodLayers(view);
      addCityLayers(view);
      addWeatherLayers(view);
      addPlanLayers(view);
      addGuideLayers(view);
      // Panning by hand stops the camera following the traveller until Recenter.
      map.on("dragstart", () => {
        if (view.guide && view.guide.follow) {
          view.guide.follow = false;
          renderGuidance(view);
        }
      });
      api("/api/plan/status").then((status) => {
        if (activeMap !== view || !status.available) return;
        view.planner = status;
        planButton.hidden = false;
        // "Get there" from the Events page: plan a trip to the venue.
        const trip = mapState.pendingTrip;
        if (trip) {
          mapState.pendingTrip = null;
          map.jumpTo({ center: trip.coord, zoom: Math.max(map.getZoom(), 14) });
          view.plan = newPlan();
          setPlanEnd(view, "to", trip);
        }
      }).catch(() => { /* No planner: the map works without directions. */ });
      applyLayerToggles(map);
      refreshRoads(view);
      Promise.all([api("/api/regional"), api("/api/regional/trains")]).then(([network, trains]) => {
        if (activeMap !== view) return;
        view.regional = network;
        view.regionalTrains = trains;
        const { lines: regionalLines, stations: regionalStations } = regionalFeatures(network);
        map.getSource("regional-lines").setData(regionalLines);
        map.getSource("regional-stations").setData(regionalStations);
        newPositions(view);
      }).catch(() => { /* The rest of the map works without regional trains. */ });
      view.glide = newGlide();
      moveVehicles(view);
      animateVehicles(view);
      // The stop list is ~190 KB compressed, so it loads after the map is on screen.
      Promise.all([api("/api/surface"), api("/api/vehicles")]).then(([surface, vehicles]) => {
        if (activeMap !== view) return;
        view.surfaceRoutes = new Map(surface.routes.map((r) => [r.route_id, r]));
        view.surfaceStops = new Map(surface.stops.map((s) => [s[0], s]));
        map.getSource("bus-stops").setData(stopFeatures(surface));
        view.vehicles = vehicleRecords(vehicles);
        newPositions(view);
        // Route patterns let vehicles move along their routes; until they load, vehicles
        // stay at their last reported positions.
        api("/api/patterns").then((data) => {
          if (activeMap !== view) return;
          view.patterns = new Map(Object.entries(data.patterns).map(([id, points]) => [Number(id), preparePattern(points)]));
          moveVehicles(view);
        }).catch(() => { /* Vehicles stay at their reported positions. */ });
      }).catch(() => fill(status, el("span", { class: "warn" }, "Buses and streetcars are unavailable right now")));
      fill(status, [el("span", { class: "live-dot", "aria-hidden": "true" }), `Live · ${timeFmt.format(new Date())}`]);
      for (const layer of ["stations", "docks", "dock-icon", "trains", "vehicles", "vehicle-icon", "bus-stops", "regional-trains", "regional-stations", "road-work-line", "road-closure-line", "road-closure-point", "road-icons", "road-icons-point", "lanes", "lanes-trail", "events", "food", "eats", "eats-notable", "eats-cluster", "food-other", "rec", "beaches", "patios", "agenda", "aqhi-dot", "outages", "entrances", "parking", "parking-entrances", "bike-parking"]) {
        map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
      }
      map.on("click", (event) => {
        if (view.guide) return;
        if (view.plan && view.plan.pick && view.selected && view.selected.kind === "plan") {
          return setPlanEnd(view, view.plan.pick, planPlace(view, [event.lngLat.lng, event.lngLat.lat]));
        }
        // A finger-sized box, so small stops and vehicles are easy to hit.
        const box = [[event.point.x - 8, event.point.y - 8], [event.point.x + 8, event.point.y + 8]];
        const layers = ["trains", "trains-dot", "regional-trains", "events", "agenda", "rec", "beaches", "patios", "aqhi-dot", "eats-notable", "eats", "eats-cluster", "food", "food-other", "entrances", "stations", "outages", "parking-entrances", "parking", "bike-parking", "regional-stations", "vehicle-icon", "vehicles", "bus-stops", "dock-icon", "docks", "road-icons", "road-icons-point", "road-work-line", "road-closure-line", "road-closure-point", "lanes", "lanes-trail"].filter((l) => map.getLayer(l) && map.getLayoutProperty(l, "visibility") !== "none");
        const hit = map.queryRenderedFeatures(box, { layers });
        if (!hit.length) {
          clearRoute(view);
          return closeSheet(view);
        }
        const f = hit.sort((a, b) => layers.indexOf(a.layer.id) - layers.indexOf(b.layer.id))[0];
        if (f.layer.id === "stations") openSheet(view, "station", f.properties.key);
        else if (f.layer.id === "docks" || f.layer.id === "dock-icon") openSheet(view, "dock", f.properties.id);
        else if (f.layer.id === "outages") openSheet(view, "station", f.properties.key);
        else if (f.layer.id === "entrances") openSheet(view, "entrance", f.properties.id);
        else if (f.layer.id === "parking") openSheet(view, "parking", f.properties.id);
        else if (f.layer.id === "parking-entrances") openSheet(view, "parking", f.properties.lot);
        else if (f.layer.id === "bike-parking") openSheet(view, "rack", f.properties.id);
        else if (f.layer.id === "bus-stops") openSheet(view, "stop", f.properties.id);
        else if (f.layer.id === "regional-trains") openSheet(view, "regional-train", f.properties.id);
        else if (f.layer.id === "regional-stations") openSheet(view, "regional-station", f.properties.id);
        else if (f.layer.id.startsWith("road-")) openSheet(view, "road", f.properties.id);
        else if (f.layer.id.startsWith("lanes")) openSheet(view, "lane", f);
        else if (f.layer.id === "events") openSheet(view, "event", f.properties.id);
        else if (f.layer.id === "food" || f.layer.id === "food-other") openSheet(view, "food", f.properties.id);
        else if (f.layer.id === "rec") openSheet(view, "rec", f.properties.id);
        else if (f.layer.id === "aqhi-dot") openSheet(view, "weather");
        else if (f.layer.id === "beaches") openSheet(view, "beach", f.properties.id);
        else if (f.layer.id === "patios") openSheet(view, "patio", f.properties.id);
        else if (f.layer.id === "agenda") openSheet(view, "agenda", f.properties.id);
        else if (f.layer.id === "eats" || f.layer.id === "eats-notable") f.properties.pid ? openSheet(view, "food", f.properties.pid) : openSheet(view, "eat", f.properties.id);
        else if (f.layer.id === "eats-cluster") {
          map.getSource("eats").getClusterExpansionZoom(f.properties.cluster_id)
            .then((zoom) => map.easeTo({ center: f.geometry.coordinates, zoom: zoom + 0.3, duration: reduceMotion() ? 0 : 500 }))
            .catch(() => {});
        }
        else if (f.layer.id === "vehicles" || f.layer.id === "vehicle-icon") openSheet(view, "vehicle", f.properties.id);
        else openSheet(view, "train", f.properties.id);
      });
      if (focusKey) {
        const s = view.data.stations.find((x) => x.key === focusKey);
        if (s) {
          map.jumpTo({ center: [s.lon, s.lat], zoom: MAP_FOCUS_ZOOM });
          openSheet(view, "station", s.key);
        }
      }
      pulseMe(view);
      // Compact attribution opens expanded on narrow screens; start it collapsed.
      container.querySelector(".maplibregl-ctrl-attrib")?.classList.remove("maplibregl-compact-show");
    });
    view.timers.push(setInterval(() => refreshTrains(view), TRAIN_REFRESH_MS));
    view.timers.push(setInterval(() => refreshNetwork(view), BIKES_REFRESH_MS));
  };
  return page;
}

const HEALTH = {
  ok: ["normal", "Healthy"],
  delayed: ["delays", "Delayed"],
  failing: ["no_service", "Failing"],
};
const CHECK_LABELS = {
  feed_freshness: "Feeds published within the last 30 minutes",
  required_fields_and_unique_stations: "Required fields present, no duplicate stations",
  station_report_freshness: "Every station reported within the last 30 minutes",
  station_status_coverage: "Every listed station has a status report",
};
const numberFmt = new Intl.NumberFormat("en-CA");

function healthPill(status) {
  const [css, label] = HEALTH[status] || HEALTH.failing;
  return statusPill(css, label);
}

// Validation flags on the pipeline page (docs/ttc.md, Validation). Flagged records are kept.
const FLAG_LABELS = {
  identical_consecutive_arrival: "same predicted minute at neighbouring stations",
  out_of_order_stop_sequence: "stops listed out of order",
  feed_order_differs_from_line_order: "stops not in line order",
  arrival_time_decreasing: "later station predicted earlier",
  alert_effect_text_mismatch: "alert type disagrees with its text",
  stale_feed_timestamp: "feed clock off",
  missing_vehicle_label: "no train number",
};

function ageText(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 90) return `${seconds} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

function healthCard(title, status, ...body) {
  return el("section", { class: "card" }, el("h3", { class: "with-link" }, title, healthPill(status)), body);
}

function checkDetail(check) {
  const d = check.details || {};
  if (check.check_name === "station_report_freshness") {
    return `${numberFmt.format(d.stale_or_future_reports)} of ${numberFmt.format(d.observed_stations)} stale`;
  }
  if (check.check_name === "station_status_coverage") {
    return `${numberFmt.format(d.observed_stations)} of ${numberFmt.format(d.metadata_stations)} reported`;
  }
  return null;
}

async function renderPipeline() {
  const data = await api("/api/pipeline");
  const { ttc, bikeshare: bikes, dbt, data: volume } = data;
  const drops = ttc.dropouts_today;
  const latest = dbt.latest;
  const dateOf = (iso) => (iso ? dayFmt.format(new Date(iso)) : "–");
  return [
    el("h1", { class: "with-link" }, "Pipeline status", healthPill(data.overall)),
    el(
      "p",
      { class: "muted" },
      "Health of the data pipelines behind this site, read from the records each step writes as it runs. ",
      "Healthy means the last success is recent; delayed or failing means data on this site may be out of date.",
    ),
    el("h2", {}, "TTC realtime feeds"),
    el("p", { class: "muted small" }, "Subway predictions and vehicle positions are polled every 10 seconds, alerts every 30."),
    ttc.feeds.map((f) =>
      healthCard(
        f.label,
        f.status,
        el("p", {}, `Last update ${ageText(f.age_seconds)}`),
        el(
          "p",
          { class: "muted small" },
          `Last hour: ${numberFmt.format(f.last_hour.polls)} polls, ${f.last_hour.failed} failed · `,
          `${numberFmt.format(f.last_hour.rejected)} ${f.last_hour.rejected === 1 ? "record" : "records"} rejected, ${numberFmt.format(f.last_hour.flagged)} flagged by validation`,
        ),
        f.last_hour.flag_reasons.length
          ? el("p", { class: "muted small" }, `Flags: ${f.last_hour.flag_reasons.map((r) => `${FLAG_LABELS[r.issue] || r.issue} (${numberFmt.format(r.records)})`).join(", ")}. Flagged records are kept; see docs/ttc.md.`)
          : null,
      ),
    ),
    drops.snapshots
      ? el(
          "p",
          {},
          `Feed dropouts today: ${numberFmt.format(drops.dropouts)} of ${numberFmt.format(drops.snapshots)} train snapshots (${drops.pct}%) listed far fewer trains than the minutes before and were skipped, so they never blank this site.`,
        )
      : null,
    el("h2", {}, "Bike Share"),
    healthCard(
      "Dock availability",
      bikes.status,
      el("p", {}, `Last collection ${ageText(bikes.age_seconds)} · runs every 15 minutes`),
      el(
        "p",
        { class: "muted small" },
        `${numberFmt.format(bikes.succeeded_24h)} collections in the last 24 hours of ${bikes.expected_24h} expected (under 90% marks this check delayed), ${bikes.failed_attempts_24h} failed attempts`,
      ),
      el(
        "ul",
        { class: "checks" },
        bikes.quality_checks.map((c) =>
          el(
            "li",
            {},
            el("span", { class: c.passed ? "pass" : "fail", "aria-hidden": "true" }, c.passed ? "✓" : "✕"),
            el(
              "span",
              {},
              CHECK_LABELS[c.check_name] || c.check_name,
              el("span", { class: "sr-only" }, c.passed ? " (passed)" : " (failed)"),
              checkDetail(c) ? el("span", { class: "detail muted small" }, checkDetail(c)) : null,
            ),
          ),
        ),
      ),
    ),
    el("h2", {}, "Transformations"),
    healthCard(
      "dbt models and tests",
      dbt.status,
      el("p", {}, `Last successful build ${ageText(dbt.age_seconds)}`),
      latest
        ? el("p", { class: "muted small" }, `Latest build: ${latest.passed} passed, ${plural(latest.warned, "warning", "warnings")}, ${latest.failed} failed`)
        : null,
    ),
    el("h2", {}, "Weather"),
    healthCard(
      "Environment Canada hourly observations",
      data.weather.status,
      el("p", {}, `Last load ${ageText(data.weather.age_seconds)} · runs hourly`),
      el(
        "p",
        { class: "muted small" },
        data.weather.latest_observation
          ? `${numberFmt.format(data.weather.hours)} hours stored. Latest observation ${dayFmt.format(new Date(data.weather.latest_observation))}, ${ageText(data.weather.data_age_seconds)}. Environment Canada publishes each day once, the next morning, so up to ${Math.round(data.weather.data_ok_seconds / 3600)} hours is normal; older marks this check delayed.`
          : "No observations loaded yet",
      ),
    ),
    el("h2", {}, "TTC schedule"),
    healthCard(
      "Published timetable",
      data.schedule.status,
      el("p", {}, data.schedule.service_end ? `${data.schedule.expired ? "Ran out" : "Runs until"} ${shortDayFmt.format(new Date(`${data.schedule.service_end}T00:00:00Z`))}` : "No schedule loaded"),
      el("p", { class: "muted small" }, `Last checked successfully ${ageText(data.schedule.age_seconds)} · checked daily`),
      data.schedule.last_error ? el("p", { class: "muted small" }, `Latest refresh failed: ${data.schedule.last_error}`) : null,
    ),
    data.roads
      ? [
          el("h2", {}, "Road restrictions"),
          healthCard(
            "City of Toronto construction and closures",
            data.roads.status,
            el("p", {}, `Last load ${ageText(data.roads.age_seconds)} · runs every 10 minutes`),
            el("p", { class: "muted small" }, `${numberFmt.format(data.roads.in_effect)} restrictions in effect today`),
          ),
        ]
      : null,
    data.food_inspections
      ? [
          el("h2", {}, "Food inspections"),
          healthCard(
            "City of Toronto DineSafe",
            data.food_inspections.status,
            el("p", {}, `Last load ${ageText(data.food_inspections.age_seconds)} · runs daily`),
            el("p", { class: "muted small" }, `${numberFmt.format(data.food_inspections.premises)} premises. Latest inspection ${data.food_inspections.latest_inspection || "none loaded"}.`),
          ),
        ]
      : null,
    data.regional
      ? [
          el("h2", {}, "Regional trains"),
          healthCard(
            "Metrolinx live positions, delays and alerts",
            data.regional.status,
            el("p", {}, `Last successful poll ${ageText((data.regional.feeds.vehicles || {}).age_seconds)} · runs every 30 seconds`),
            el("p", { class: "muted small" }, `${numberFmt.format(data.regional.live_trains)} trains reporting now. Alerts last loaded ${ageText((data.regional.feeds.alerts || {}).age_seconds)}, every 2 minutes.`),
          ),
        ]
      : null,
    data.official_delays
      ? [
          el("h2", {}, "TTC's official delay log"),
          data.official_delays.log_reaches
            ? el("p", {}, "Latest incident in TTC's published log: ", el("strong", {}, dayFmt.format(new Date(data.official_delays.log_reaches))))
            : null,
          el(
            "p",
            { class: "muted small" },
            `Checked ${ageText(Math.round((Date.now() - new Date(data.official_delays.finished_at)) / 1000))} (${data.official_delays.status}). `,
            "The city publishes each month a few weeks after it ends; this site's observed gaps are compared with it once the months overlap.",
          ),
        ]
      : null,
    el("h2", {}, "Data collected"),
    el(
      "dl",
      { class: "card facts" },
      el("dt", {}, "Subway history since"), el("dd", {}, dateOf(volume.ttc_since)),
      el("dt", {}, "Train visits recorded"), el("dd", {}, numberFmt.format(volume.rows.ttc_train_visits ?? 0)),
      el("dt", {}, "Gaps between trains measured"), el("dd", {}, numberFmt.format(volume.rows.ttc_headways ?? 0)),
      el("dt", {}, "Bike Share history since"), el("dd", {}, dateOf(volume.bikeshare_since)),
      el("dt", {}, "Dock readings recorded"), el("dd", {}, numberFmt.format(volume.rows.bikeshare_observations ?? 0)),
      el("dt", {}, "Database size"), el("dd", {}, `${(volume.database_bytes / 1e6).toFixed(0)} MB`),
      el("dt", {}, "Schema version"), el("dd", {}, (volume.schema_version || "–").replaceAll("_", " ")),
    ),
    el("p", { class: "muted small" }, "Row counts are PostgreSQL's planner estimates, refreshed as tables are analyzed."),
  ];
}

// A periodic refresh must not throw away what the visitor is doing: typed search text,
// focus, open panels and scroll position are carried over to the new content.
function captureState() {
  const active = document.activeElement;
  const focused = active && main.contains(active) && active.id ? active : null;
  return {
    open: new Set([...main.querySelectorAll("details[data-key]")].filter((d) => d.open).map((d) => d.dataset.key)),
    search: document.getElementById("station-search")?.value || "",
    focusId: focused ? focused.id : null,
    selection: focused && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : null,
    scroll: window.scrollY,
    counts: new Map([...main.querySelectorAll("[data-count]")].map((n) => [n.dataset.count, [n.textContent, n.querySelector(".ring-fill")?.style.strokeDashoffset]])),
    moves: new Map([...main.querySelectorAll("[data-move]")].map((n) => [n.dataset.move, n.style.top])),
  };
}

function restoreState(state) {
  for (const d of main.querySelectorAll("details[data-key]")) d.open = state.open.has(d.dataset.key);
  const search = document.getElementById("station-search");
  if (search && state.search) {
    search.value = state.search;
    search.dispatchEvent(new Event("input"));
  }
  const focused = state.focusId && document.getElementById(state.focusId);
  if (focused) {
    focused.focus({ preventScroll: true });
    if (state.selection) focused.setSelectionRange(...state.selection);
  }
  window.scrollTo(0, state.scroll);
  if (reduceMotion()) return;
  for (const n of main.querySelectorAll("[data-count]")) {
    const old = state.counts.get(n.dataset.count);
    if (!old || old[0] === n.textContent) continue;
    n.classList.add("tick");
    const fill = n.querySelector(".ring-fill");
    if (fill && old[1]) {
      const to = fill.style.strokeDashoffset;
      fill.style.strokeDashoffset = old[1];
      requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.strokeDashoffset = to; }));
    }
  }
  for (const n of main.querySelectorAll("[data-move]")) {
    const old = state.moves.get(n.dataset.move);
    if (!old || old === n.style.top) continue;
    const to = n.style.top;
    n.style.top = old;
    n.classList.add("moved");
    requestAnimationFrame(() => requestAnimationFrame(() => { n.style.top = to; }));
  }
}

// ---- Events page ---------------------------------------------------------------------
// What's on over the next 14 days (/api/agenda): a strip of days, then the chosen day's
// events in time order. Arena and stadium events are highlighted with the station they
// fill; exhibits and markets running most days are folded into "ongoing".
const EVENT_FILTERS = [["all", "All"], ["concerts", "Concerts"], ["sports", "Sports"], ["arts", "Arts & theatre"], ["community", "Festivals & community"], ["free", "Free"]];
const eventsState = { filter: "all" };
const dayLongFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", weekday: "long", month: "long", day: "numeric" });
const dayShortFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", weekday: "short" });
const dayDateFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", month: "long", day: "numeric" });

function dayName(iso, days) {
  const d = new Date(`${iso}T00:00:00Z`);
  if (iso === days[0]) return { short: "Today", long: `Today, ${dayDateFmt.format(d)}` };
  if (iso === days[1]) return { short: "Tmrw", long: `Tomorrow, ${dayDateFmt.format(d)}` };
  return { short: dayShortFmt.format(d), long: dayLongFmt.format(d) };
}

function stationText(s) {
  if (!s) return null;
  return `${s.name}${s.kind === "regional" ? " (regional)" : ""}, ${s.walk_min} min walk`;
}

function eventTime(e) {
  if (e.all_day) return "All day";
  return e.time_tbd || !e.starts_at ? "Time TBA" : timeFmt.format(new Date(e.starts_at));
}

function agendaItem(e, categories) {
  const crowd = e.major && e.station
    ? `Expect crowds at ${e.station.name} ${e.time_tbd ? "around the event" : `before ${eventTime(e)}`} and when it ends.`
    : null;
  return el(
    "li",
    { class: `agenda-item${e.major ? " major" : ""}` },
    el("p", { class: "agenda-time" }, eventTime(e), e.sessions > 1 ? el("span", { class: "muted small agenda-sessions" }, `${e.sessions} times`) : null),
    el(
      "div",
      { class: "agenda-body" },
      el("p", { class: "agenda-title" }, e.name),
      el(
        "p",
        { class: "agenda-tags" },
        el("span", { class: `tag cat-${e.category}` }, categories[e.category] || "Event"),
        e.major ? el("span", { class: "tag major" }, "Major venue") : null,
        e.free ? el("span", { class: "tag free" }, "Free") : null,
        e.status ? el("span", { class: "tag warn" }, e.status === "postponed" ? "Postponed" : "Rescheduled") : null,
      ),
      el("p", { class: "muted small" }, [e.venue, stationText(e.station)].filter(Boolean).join(" · ")),
      crowd ? el("p", { class: "agenda-crowd small" }, crowd) : null,
      el(
        "p",
        { class: "agenda-actions" },
        el("button", { type: "button", class: "button small", "data-event-go": e.id }, iconSvg("directions", { size: 16 }), "Get there"),
        e.url ? el("a", { class: "linkish small", href: e.url, target: "_blank", rel: "noopener" }, e.source === "ticketmaster" ? "Tickets" : "Website") : null,
      ),
    ),
  );
}

// Findings: what the collected data shows when sources are compared (transit.findings).
const findingDayFmt = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", month: "short", year: "numeric" });

function arrivalMins(minutes, extra = null, key = null) {
  const share = Math.max(0, Math.min(1, minutes / 10));
  const fill = svgNode("circle", { class: "ring-fill", cx: 10, cy: 10, r: 7.5, pathLength: 1 });
  fill.style.strokeDashoffset = (1 - share).toFixed(3); // Through the CSSOM: the CSP allows no style attributes.
  return el("span", { class: `mins countdown${minutes === 0 ? " due" : ""}`, "data-count": key },
    svgNode("svg", { class: "ring", viewBox: "0 0 20 20", "aria-hidden": "true" },
      svgNode("circle", { class: "ring-track", cx: 10, cy: 10, r: 7.5 }), fill),
    el("span", { class: "count" }, minutes === 0 ? "Due" : `${minutes} min`), extra);
}

function svgNode(name, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  for (const child of children.flat()) if (child != null) node.append(child);
  return node;
}

function niceMax(value) {
  const step = 10 ** Math.floor(Math.log10(value || 1));
  return Math.ceil(value / step) * step;
}

// A small chart for one finding: lines or vertical bars in SVG, or horizontal bars in HTML.
function findingChart(c) {
  const all = c.series.flatMap((s) => s.values).filter((v) => v != null);
  const max = niceMax(Math.max(...all));
  const legend = c.series.length > 1
    ? el("ul", { class: "chart-legend" }, c.series.map((s, i) => el("li", { class: `series-${i}` }, s.name)))
    : null;
  if (c.kind === "hbars") {
    return el("div", { class: "finding-chart" },
      el("ul", { class: "hbars", "aria-label": c.y_label }, c.x.map((label, i) => {
        const v = c.series[0].values[i];
        return el("li", { style: { "--k": i } }, el("span", { class: "hbar-label" }, label),
          el("span", { class: "hbar-track" }, hbarFill((100 * v) / max)),
          el("span", { class: "hbar-value" }, numberFmt.format(v)));
      })));
  }
  const W = 600, H = 280, L = 70, R = 10, T = 14, B = 58;
  const x = (i) => L + ((W - L - R) * (i + 0.5)) / c.x.length;
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => svgNode("g", {},
    svgNode("line", { x1: L, x2: W - R, y1: y(max * f), y2: y(max * f), class: "grid" }),
    svgNode("text", { x: L - 6, y: y(max * f) + 4, "text-anchor": "end", class: "tick" }, numberFmt.format(Math.round(max * f)))));
  const every = Math.ceil(c.x.length / 6);
  const labels = c.x.map((label, i) => (i % every ? null : svgNode("text", { x: x(i), y: H - B + 24, "text-anchor": "middle", class: "tick" }, label)));
  let marks;
  if (c.kind === "bars") {
    const width = ((W - L - R) / c.x.length) * 0.6;
    marks = c.series[0].values.map((v, i) => (v == null ? null : svgNode("rect", { x: x(i) - width / 2, y: y(v), width, height: y(0) - y(v), class: "bar series-0" },
      svgNode("title", {}, `${c.x[i]}: ${numberFmt.format(v)}`))));
  } else {
    marks = c.series.map((s, k) => {
      const runs = [];
      let run = [];
      s.values.forEach((v, i) => {
        if (v == null) { if (run.length) runs.push(run); run = []; } else run.push(`${x(i)},${y(v)}`);
      });
      if (run.length) runs.push(run);
      return svgNode("g", { class: `series-${k}` },
        runs.map((r) => svgNode("polyline", { points: r.join(" "), class: "line", pathLength: 1 })),
        s.values.map((v, i) => (v == null ? null : svgNode("circle", { cx: x(i), cy: y(v), r: 3.5, class: "dot" }, svgNode("title", {}, `${s.name}, ${c.x[i]}: ${numberFmt.format(v)}`)))));
    });
  }
  const svg = svgNode("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": `${c.y_label} by ${c.x_label || "group"}` },
    grid, labels, marks,
    c.x_label ? svgNode("text", { x: (L + W - R) / 2, y: H - 6, "text-anchor": "middle", class: "axis-label" }, c.x_label) : null);
  return el("div", { class: "finding-chart" }, el("p", { class: "muted small chart-y" }, c.y_label), svg, legend);
}

function hbarFill(percent) {
  const bar = el("span", { class: "hbar", style: { width: `${percent}%` } });
  bar.style.setProperty("--w", `${percent}%`);
  return bar;
}

const findingsSeen = new Set(); // Drawn once per visit; a refresh does not replay them.

const findingObserver = "IntersectionObserver" in window
  ? new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) {
        e.target.classList.add("in-view");
        findingObserver.unobserve(e.target);
      }
    }), { threshold: 0.6, rootMargin: "0px 0px -8% 0px" })
  : null;

function findingCard(f) {
  const [first, last] = f.period.map((d) => findingDayFmt.format(new Date(`${String(d).slice(0, 10)}T00:00:00Z`)));
  const card = el("article", { class: "card finding", id: f.id },
    el("p", { class: "sheet-kicker" }, f.topic),
    el("h2", {}, f.title),
    el("p", {}, f.summary),
    findingChart(f.chart),
    el("details", { class: "finding-method" },
      el("summary", {}, "How this was worked out"),
      el("p", {}, f.method),
      el("p", { class: "muted small" }, `${first} to ${last}. Sources: ${f.sources.join("; ")}.`)));
  const chart = card.querySelector(".finding-chart") || card;
  if (findingObserver && !findingsSeen.has(f.id)) {
    findingsSeen.add(f.id);
    findingObserver.observe(chart);
  } else chart.classList.add("in-view");
  return card;
}

async function renderFindings() {
  const data = await api("/api/findings");
  return [
    el("h1", {}, "Findings"),
    el("p", {}, "What the data collected here shows when the sources are put side by side: transit, Bike Share, weather and the city's open data. Each finding is recomputed from the stored data, with its method and period below it."),
    data.findings.length ? data.findings.map(findingCard) : el("p", { class: "muted" }, "Findings appear once enough data has been collected."),
  ];
}

async function renderEvents(dayParam) {
  const data = await api("/api/agenda");
  const { days, categories } = data;
  const day = days.includes(dayParam) ? dayParam : days[0];
  const f = eventsState.filter;
  const shown = data.events.filter((e) => f === "all" || (f === "free" ? e.free === true : e.category === f));
  const counts = new Map(days.map((d) => [d, { all: 0, major: 0 }]));
  for (const e of shown) {
    const c = counts.get(e.date);
    if (!c || e.ongoing) continue;
    c.all += 1;
    if (e.major) c.major += 1;
  }
  const onDay = shown.filter((e) => e.date === day);
  const timed = onDay.filter((e) => !e.ongoing);
  const ongoing = onDay.filter((e) => e.ongoing);
  const majorCount = timed.filter((e) => e.major).length;
  const page = el(
    "div",
    { class: "events-page" },
    el("h1", {}, "Events"),
    el("p", { class: "muted" }, "What's on in Toronto over the next two weeks, and the station to get there."),
    el(
      "div",
      { class: "event-filters", role: "group", "aria-label": "Show" },
      EVENT_FILTERS.map(([key, label]) => el("button", { type: "button", class: "chip small", "aria-pressed": String(f === key), "data-event-filter": key }, label)),
    ),
    el(
      "nav",
      { class: "day-strip", "aria-label": "Days" },
      days.map((d) => {
        const c = counts.get(d);
        const name = dayName(d, days);
        return el(
          "a",
          { class: `day${c.major ? " has-major" : ""}`, href: `#/events/${d}`, "aria-current": d === day ? "date" : null, "aria-label": `${name.long}: ${plural(c.all, "event", "events")}` },
          el("span", { class: "day-name" }, name.short),
          el("span", { class: "day-num" }, String(Number(d.slice(8)))),
          el("span", { class: "day-dots", "aria-hidden": "true" }, Array.from({ length: Math.min(3, Math.ceil(c.all / 4)) }, () => el("i"))),
        );
      }),
    ),
    el("h2", { class: "agenda-day" }, dayName(day, days).long),
    majorCount ? el("p", { class: "muted small" }, `${plural(majorCount, "event", "events")} at a major venue: stations nearby will be busy.`) : null,
    timed.length
      ? el("ol", { class: "agenda" }, timed.map((e) => agendaItem(e, categories)))
      : el("p", { class: "muted" }, f === "all" ? "Nothing listed for this day yet." : "Nothing of this kind listed for this day."),
    ongoing.length
      ? el(
          "details",
          { class: "card ongoing", "data-key": "ongoing" },
          el("summary", {}, plural(ongoing.length, "ongoing exhibit, market or program", "ongoing exhibits, markets and programs")),
          el("ol", { class: "agenda" }, ongoing.map((e) => agendaItem(e, categories))),
        )
      : null,
    el(
      "p",
      { class: "muted small" },
      "Festivals and community events: City of Toronto festivals and events calendar, under the Open Government Licence – Toronto.",
      data.sources.ticketmaster ? " Concerts, games and shows: Ticketmaster; tickets are sold on Ticketmaster." : "",
      " Walking times are straight-line estimates.",
    ),
  );
  page.addEventListener("click", (event) => {
    const filter = event.target.closest("[data-event-filter]");
    if (filter) {
      eventsState.filter = filter.dataset.eventFilter;
      route({ refresh: true });
      return;
    }
    const go = event.target.closest("[data-event-go]");
    if (go) {
      const e = data.events.find((x) => x.id === go.dataset.eventGo);
      if (!e) return;
      // The map picks this up once its trip planner is ready (renderMap).
      mapState.pendingTrip = { coord: [e.lon, e.lat], name: e.venue || e.name };
      location.hash = "#/map";
    }
  });
  return page;
}

function markCurrentNav(kind, id) {
  for (const link of document.querySelectorAll("#line-nav a, nav.primary a, .tabs a")) {
    const href = link.getAttribute("href");
    const current = kind ? (kind === "line" && href === `#/line/${id}`) || href === `#/${kind}` : href === "#/";
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function skeleton() {
  return el("div", { class: "skeleton", "aria-hidden": "true" },
    el("div", { class: "sk-title" }), el("div", { class: "sk-line" }), el("div"), el("div"), el("div"));
}

// Swap in new content. Navigations cross-fade (View Transitions, where supported) and
// stagger their sections in; periodic refreshes replace content without any motion.
let heroFrom = null; // The badge a visitor clicked, which glides into the next page's heading.

document.addEventListener("click", (event) => {
  const link = event.target.closest && event.target.closest('main a[href^="#/line/"]');
  if (heroFrom) heroFrom.style.viewTransitionName = "";
  heroFrom = null;
  const row = link && link.closest("li, .card");
  const from = row && row.querySelector(".badge");
  if (from && document.startViewTransition && !reduceMotion()) {
    from.style.viewTransitionName = "line-hero";
    heroFrom = from;
  }
}, true);

function show(content, { refresh }) {
  const nodes = [content].flat(Infinity).filter(Boolean);
  let heroTo = null;
  const swap = () => {
    main.classList.toggle("entering", !refresh);
    nodes.forEach((node, i) => node.style && node.style.setProperty("--i", Math.min(i, 10)));
    main.replaceChildren(...nodes);
    for (const node of nodes) if (typeof node.mount === "function") node.mount();
    if (heroFrom) {
      heroTo = main.querySelector("h1 .badge");
      if (heroTo) heroTo.style.viewTransitionName = "line-hero";
    }
  };
  if (!refresh && document.startViewTransition && !reduceMotion() && !document.body.classList.contains("map-mode")) {
    // A transition is skipped when another navigation starts first; its `ready` promise
    // then rejects with an AbortError that is expected and needs no handling.
    const transition = document.startViewTransition(swap);
    transition.ready.catch(() => {});
    transition.finished.finally(() => {
      if (heroTo) heroTo.style.viewTransitionName = "";
      if (heroFrom) heroFrom.style.viewTransitionName = "";
      heroFrom = null;
    });
  } else {
    swap();
    if (heroTo) heroTo.style.viewTransitionName = "";
    if (heroFrom) heroFrom.style.viewTransitionName = "";
    heroFrom = null;
  }
}

function setHeaderHeight() {
  const bar = document.querySelector(".site-bar");
  if (bar) document.documentElement.style.setProperty("--header-h", `${bar.offsetHeight}px`);
}

// Installable app (static/sw.js). Service workers need a secure context, which localhost is.
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

async function route({ refresh = false } = {}) {
  let [, kind, id, part] = (location.hash || "#/").split("/");
  if (kind === "report") [kind, id] = ["reliability", "report"]; // Old links.
  const seq = ++renderSeq;
  if (!refresh && kind !== "map") {
    mapCleanup();
    document.body.classList.remove("map-mode"); // Also when leaving before the map loaded.
  }
  const waiting = refresh ? null : setTimeout(() => seq === renderSeq && kind !== "map" && main.replaceChildren(skeleton()), 160);
  if (!refresh && kind === "map" && !activeMap) {
    // The map library and data take a few seconds on a phone; an empty map frame replaces
    // the previous page at once, so that page is not left showing over the map.
    document.body.classList.add("map-mode");
    main.replaceChildren(el("div", { class: "map-canvas", "aria-busy": "true" }, el("p", { class: "muted map-loading" }, "Loading map…")));
  }
  savedCopyAt = null;
  try {
    if (!lines.length) {
      const lineData = await api("/api/lines");
      lines = lineData.lines;
      stationLines = new Map(lineData.stations.map((s) => [s.key, s.lines]));
      document.getElementById("line-nav").replaceChildren(...lines.map((l) => badge(l.id)));
    }
    let content;
    if (kind === "line" && id) content = await renderLine(id);
    else if (kind === "station" && id) content = await renderStation(id);
    else if (kind === "reliability") content = id === "report" ? await renderReport() : id === "card" ? await renderReportCard(part) : await renderReliability();
    else if (kind === "map") content = await renderMap(id);
    else if (kind === "stop" && id) content = await renderStop(decodeURIComponent(id));
    else if (kind === "pipeline") content = await renderPipeline();
    else if (kind === "events") content = await renderEvents(id);
    else if (kind === "findings") content = await renderFindings();
    else content = await renderHome();
    clearTimeout(waiting);
    if (seq !== renderSeq) return; // A newer navigation or refresh finished first.
    const at = savedCopyAt && timeFmt.format(new Date(savedCopyAt));
    // Built from copies saved on an earlier visit (sw.js): said at the top, and retried.
    if (at && kind !== "map") content = [el("p", { class: "notice offline-notice", role: "status" }, `No connection. Showing data saved at ${at}; times and arrivals are not live.`), content];
    const state = refresh ? captureState() : null;
    show(content, { refresh });
    if (state) restoreState(state);
    markCurrentNav(kind, id);
    if (at) {
      updated.textContent = `Offline · data from ${at}`;
      updated.classList.add("warn");
    } else {
      updated.textContent = `Updated ${timeFmt.format(new Date())}`;
      updated.classList.remove("warn");
    }
  } catch (error) {
    clearTimeout(waiting);
    if (seq !== renderSeq) return;
    if (!activeMap) document.body.classList.remove("map-mode");
    if (refresh && main.querySelector("h1") && error.message !== "Not found") {
      // Keep the last good data on screen rather than replacing it with an error.
      updated.textContent = `Could not refresh at ${timeFmt.format(new Date())}. Showing earlier data; retrying shortly.`;
      updated.classList.add("warn");
      return;
    }
    const why = error.message === "Not found"
      ? "That line or station does not exist."
      : navigator.onLine === false
        ? "No connection, and this page was not saved on an earlier visit. Pages you have opened before still work offline."
        : "Live data is unavailable right now. Retrying shortly.";
    main.replaceChildren(el("h1", {}, "Unavailable"), el("p", {}, why));
  }
}

window.addEventListener("hashchange", () => {
  route().then(() => { window.scrollTo(0, 0); main.focus({ preventScroll: true }); });
});
setInterval(() => {
  // The map refreshes its own layers in place; re-rendering would rebuild it.
  if (document.visibilityState !== "visible" || location.hash.startsWith("#/map")) return;
  route({ refresh: true });
}, REFRESH_MS);
window.addEventListener("resize", setHeaderHeight);
document.getElementById("theme-switch").addEventListener("click", (event) => {
  if (!document.startViewTransition || reduceMotion()) return window.toggleTheme();
  const box = event.currentTarget.getBoundingClientRect();
  const x = box.left + box.width / 2;
  const y = box.top + box.height / 2;
  const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  document.documentElement.classList.add("theme-flip");
  const flip = document.startViewTransition(() => window.toggleTheme());
  flip.ready.then(() => document.documentElement.animate(
    { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
    { duration: 520, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)", pseudoElement: "::view-transition-new(root)" },
  )).catch(() => {});
  flip.finished.finally(() => document.documentElement.classList.remove("theme-flip"));
});
// The page restyles itself; the map draws its colours when built, so it is rebuilt, unless
// guidance is running, which a rebuild would end.
window.addEventListener("themechange", () => {
  if (location.hash.startsWith("#/map") && !activeMap?.guide) route();
});
setHeaderHeight();
route();
