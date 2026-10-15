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

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
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

function statusRow(status) {
  return el(
    "li",
    { class: "status-row board-row" },
    badge(status.line, { link: false }),
    el("div", {}, el("a", { class: "name", href: `#/line/${status.line}` }, status.name), statusDetails(status)),
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
  const affected = statuses.filter((s) => s.status !== "normal" || s.detected);
  const ok = !affected.length;
  return el(
    "section",
    { class: `summary ${ok ? "ok" : "issues"}` },
    el(
      "h1",
      {},
      el("span", { class: "state-icon", "aria-hidden": "true" }, ok ? "✓" : "!"),
      ok ? "Subway: all lines running normally" : `Subway: ${affected.length} of ${statuses.length} lines affected`,
    ),
    el(
      "p",
      { class: "muted" },
      `${plural(alertData.active.length, "active alert", "active alerts")} · ${plural(alertData.accessibility.length, "elevator or escalator", "elevators or escalators")} out of service`,
    ),
  );
}

async function renderHome() {
  const [lineData, alertData] = await Promise.all([api("/api/lines"), api("/api/alerts")]);
  const accessibility = alertData.accessibility;
  return [
    networkSummary(lineData, alertData),
    staleNotice(alertData.alerts_as_of, "TTC alerts"),
    el("h2", {}, "Lines"),
    el("ul", { class: "status-list" }, lineData.lines.map((l) => statusRow(l.status))),
    stationSearch(lineData.stations),
    el("h2", {}, "Active alerts"),
    alertData.active.length ? alertData.active.map(alertCard) : el("p", { class: "muted" }, "No active subway alerts."),
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
      style: { top: `${train.position * ROW + ROW / 2}px`, translate: `${outward}px 0` },
      title: `Train ${train.train_id}: ${train.at_station ? "at" : "approaching"} ${train.station}`,
    }));
  }
  return strip;
}

function directionSummary(data, dir) {
  const trains = data.trains.filter((t) => t.direction_id === dir.direction_id);
  const longGaps = data.gaps.filter((g) => g.long && g.direction_id === dir.direction_id);
  const headway = data.scheduled_headway_seconds[dir.direction_id];
  return el(
    "section",
    { class: "card" },
    el("h2", {}, `Towards ${dir.towards}`),
    el("p", { class: "muted" }, `${trains.length} trains in the feed`, headway ? ` · scheduled about every ${minutesText(headway)}` : ""),
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
    el("h1", { class: "with-badge" }, badge(data.line.id, { link: false }), data.line.name),
    el("div", { class: "card" }, statusDetails(data.status)),
    staleNotice(data.predictions_as_of, "Train predictions"),
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
      el("p", { class: "muted small" }, "Positions are estimated from predicted arrival times. Train lists below give the same information as text."),
    ),
    [dir0, dir1].filter(Boolean).map((dir) => directionSummary(data, dir)),
  ];
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
          d.arrivals.map((a) => el("li", {}, el("span", { class: "mins" }, a.minutes === 0 ? "Due" : `${a.minutes} min`), el("span", { class: "muted" }, timeFmt.format(new Date(a.arrival))))),
        )
      : el("p", { class: "muted" }, "No predictions right now."),
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
    el("h1", { class: "with-badge" }, station.name, station.lines.map((l) => badge(l))),
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
  if (!gaps.length) return el("p", { class: "muted" }, "No measurable gaps yet today.");
  return el(
    "ol",
    { class: "gaps" },
    gaps.map((g) =>
      el(
        "li",
        {},
        el("strong", { class: "mins" }, `${(g.gap_seconds / 60).toFixed(1)} min`),
        el(
          "span",
          {},
          el("a", { href: `#/station/${g.station_key}` }, g.station_name),
          ` to ${g.towards}`,
          el("span", { class: "muted" }, ` · ${timeFmt.format(new Date(g.gap_start))}–${timeFmt.format(new Date(g.gap_end))}`, g.scheduled_headway_seconds ? ` · usually every ${minutesText(g.scheduled_headway_seconds)}` : ""),
        ),
      ),
    ),
  );
}

function hourTable(hours) {
  if (!hours.length) return el("p", { class: "muted" }, "No data yet.");
  return el(
    "table",
    { class: "hours" },
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Hour"), el("th", { scope: "col" }, "Today"), el("th", { scope: "col" }, "Last 7 days"), el("th", { scope: "col" }, "Long gaps today"))),
    el(
      "tbody",
      {},
      hours.map((h) =>
        el("tr", {}, el("th", { scope: "row" }, hourLabel(h.hour)), el("td", {}, meter(h.today_regular_pct)), el("td", {}, meter(h.week_regular_pct)), el("td", {}, h.today_long_gaps === null ? "–" : h.today_long_gaps)),
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
          { class: "card" },
          el("h3", {}, d.title),
          el("p", { class: "muted small" }, d.description, d.day ? ` One file per day (${d.day}).` : ""),
          el(
            "ul",
            { class: "downloads" },
            d.files.slice(0, shown).map((f) =>
              el(
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
              ),
            ),
          ),
          d.files.length > shown
            ? el("p", { class: "muted small" }, `${d.files.length - shown} earlier files are listed in `, el("a", { href: "/data/index.json" }, "index.json"), ".")
            : null,
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

async function renderReliability() {
  const [data, sets] = await Promise.all([api("/api/reliability"), api("/api/datasets")]);
  if (!data.available) {
    return [
      el("h1", {}, "Reliability"),
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
    el("h1", {}, "Reliability"),
    el(
      "p",
      { class: "muted" },
      `Measured from ${coverage.headways.toLocaleString("en-CA")} gaps between observed train arrivals since ${coverage.first_day}. History starts when collection started. `,
      data.analytics_built_at ? `Analytics updated ${timeFmt.format(new Date(data.analytics_built_at))}` : "",
    ),
    el("h2", {}, "Longest gaps between trains today"),
    el("div", { class: "grid" }, lines.map((line) => lineCard(line, gapList(data.longest_gaps_today.filter((g) => g.route_id === line.id))))),
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
    el("p", { class: "muted small" }, "Gaps are timed where vehicles pass stops about every 10 scheduled minutes along each route, from TTC's live positions (to within about 20 seconds), and exclude terminals, where buses queue by design."),
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

async function renderReport() {
  const data = await api("/api/report");
  if (!data.available || !data.lines.length) {
    return [el("h1", {}, "Reliability report"), el("p", {}, "The report appears after the first scheduled transformation with measured gaps.")];
  }
  const c = data.coverage;
  const pick = (rows, key) => (k, line) => rows.find((r) => r[key] === k && r.route_id === line);
  const periods = data.period_labels.filter((p) => data.periods.some((r) => r.period === p.key));
  const missing = data.period_labels.filter((p) => !periods.includes(p));
  const rainHours = maxHours(data.weather, "rain");
  const dates = [...new Set(data.daily.map((d) => d.service_date))];
  return [
    el("h1", {}, "Reliability report"),
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
// Trains and vehicles move between refreshes (motion.js), redrawn every animation frame
// when zoomed in. Zoomed out, a frame's movement is under a pixel, so they are redrawn
// less often.
const MOTION_FRAME_MS = [[14, 0], [12, 50], [0, 250]]; // [from zoom, minimum ms between redraws]
const MOTION_STILL_MS = 5000; // With reduced motion.
const BIKES_REFRESH_MS = 120000; // Bike Share data changes every 15 minutes.
const WALK_METRES_PER_MIN = 80;
const TRAIN_OFFSET_M = 28; // Each direction sits a little to its own side of the line.
const MAP_MODES = { bikes: "Bikes", docks: "Docks" };
const DIRECTION_SIDE = { 0: 1, 1: -1 };
const mapState = { view: null, mode: "bikes", layers: { trains: true, regional: true, buses: true, bikes: true, access: true, entrances: true, roads: false, lanes: false, events: false, parking: false, bikeParking: false } };
// Buses and streetcars. TTC's subway lines own yellow, green and purple, so surface modes
// take blue and crimson; both are always labelled, never told apart by colour alone.
const SURFACE_COLOURS = { bus: "#2563eb", streetcar: "#e11d48" };
const SURFACE_LABELS = { bus: "Bus", streetcar: "Streetcar" };
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

const prefersDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// Muted streets so the transit layers carry the colour. Layer names follow the Protomaps
// basemap schema (v4) that the extract uses.
function basemapColours(dark) {
  return dark
    ? { water: "#0f1c28", land: "#14171c", park: "#16231b", building: "#1d2128", road: "#262b33", major: "#2e343e", highway: "#394150", casing: "#0f1216", label: "#8c96a4", halo: "#0b0d10", rail: "#3a404a" }
    : { water: "#cbdcea", land: "#f3f4f1", park: "#dbe9d3", building: "#e7e7e3", road: "#ffffff", major: "#ffffff", highway: "#fdfdfb", casing: "#d9dad3", label: "#6b7280", halo: "#ffffff", rail: "#c9cbc4" };
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
  const padLon = (b.getEast() - b.getWest()) * 0.2;
  const padLat = (b.getNorth() - b.getSouth()) * 0.2;
  const inView = (f) => {
    const [lon, lat] = f.geometry.coordinates;
    return lon > b.getWest() - padLon && lon < b.getEast() + padLon && lat > b.getSouth() - padLat && lat < b.getNorth() + padLat;
  };
  const place = (id, coord) => (still ? coord : glide(view.glide, id, coord, now));
  if (view.trains && map.getSource("trains")) {
    view.trainFeatures = trainFeatures(view.trains, view.coords, now).map((f) => point(place(f.properties.id, f.geometry.coordinates), f.properties));
    map.getSource("trains").setData(featureCollection(view.trainFeatures));
  }
  if (view.vehicles && map.getSource("vehicles")) {
    view.vehicleFeatures = view.vehicles.map((v) => {
      const at = vehiclePlace(v, view.patterns && view.patterns.get(v.pattern), now);
      const heading = at.bearing !== null && at.bearing !== undefined;
      const coord = place(`v:${v.id}`, at.coord);
      const bearing = heading && !still ? turn(view.glide, `v:${v.id}`, at.bearing, now) : at.bearing;
      return point(coord, { id: v.id, route: v.route, mode: v.mode, bearing: heading ? bearing : 0, heading, towards: v.towards, occupancy: v.occupancy, fade: at.fade });
    });
    map.getSource("vehicles").setData(featureCollection(view.vehicleFeatures.filter(inView)));
  }
  if (view.regionalTrains && map.getSource("regional-trains")) {
    view.regionalTrainFeatures = view.regionalTrains.trains.map((t) => point(place(`r:${t.id}`, trackPoint(t.track, now) || [t.lon, t.lat]), { id: t.id, line: t.line, colour: t.colour || "#4b5563" }));
    map.getSource("regional-trains").setData(featureCollection(view.regionalTrainFeatures));
  }
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

const DOCK_COLOURS = { ok: "#16a34a", few: "#e59a00", none: "#dc2626", unknown: "#9aa1ab" };
const DOCK_LEVEL_LABELS = { ok: "3 or more", few: "1 or 2", none: "None", unknown: "No recent report" };
const OUTAGE_COLOUR = "#f59e0b";
const ENTRANCE_COLOUR = "#57534e";
const STEP_FREE_COLOUR = "#0369a1";
const PARKING_COLOUR = "#4338ca";
const BIKE_PARKING_COLOUR = "#0f766e";

function mapCasing() {
  return prefersDark() ? "#0b0d10" : "#ffffff";
}

// Every icon the map draws, from icons.js. Subway trains take their line's colour.
function addMapIcons(map) {
  const ring = mapCasing();
  addBadge(map, "badge-bus", "bus", SURFACE_COLOURS.bus, "#ffffff", ring);
  addBadge(map, "badge-streetcar", "streetcar", SURFACE_COLOURS.streetcar, "#ffffff", ring);
  for (const l of lines) addBadge(map, `badge-train-${l.id}`, "subway", l.color || "#777", textOn(l.color || "#777777"), ring);
  addBadge(map, "badge-train", "subway", "#777777", "#ffffff", ring);
  for (const [level, colour] of Object.entries(DOCK_COLOURS)) addBadge(map, `badge-dock-${level}`, "bike", colour, "#ffffff", ring, { css: 24, glyph: 15 });
  addBadge(map, "badge-event-today", "event", EVENT_COLOUR, "#ffffff", ring, { css: 24, glyph: 14 });
  addBadge(map, "badge-event-week", "event", prefersDark() ? "#94a3b8" : EVENT_COLOUR, "#ffffff", ring, { css: 24, glyph: 14, hollow: true });
  addBadge(map, "badge-construction", "roadwork", ROAD_COLOURS.construction, "#111418", ring, { css: 24, glyph: 15 });
  addBadge(map, "badge-closure", "closure", ROAD_COLOURS.closure, "#ffffff", ring, { css: 24, glyph: 15 });
  addBadge(map, "badge-elevator", "elevator", OUTAGE_COLOUR, "#111418", ring, { css: 20, glyph: 13 });
  addBadge(map, "badge-entrance", "entrance", ENTRANCE_COLOUR, "#ffffff", ring, { css: 22, glyph: 14 });
  addBadge(map, "badge-entrance-free", "stepfree", STEP_FREE_COLOUR, "#ffffff", ring, { css: 22, glyph: 15 });
  addBadge(map, "badge-parking", "parking", PARKING_COLOUR, "#ffffff", ring, { css: 22, glyph: 14 });
  addBadge(map, "badge-parking-entrance", "entrance", PARKING_COLOUR, "#ffffff", ring, { css: 18, glyph: 12 });
  addBadge(map, "badge-rack", "rack", BIKE_PARKING_COLOUR, "#ffffff", ring, { css: 20, glyph: 13 });
  addHeadingImage(map);
}

function addSurfaceLayers(map) {
  const c = prefersDark() ? { stop: "#14171c", stopStroke: "#8c96a4", halo: "#0b0d10", casing: "#0b0d10" } : { stop: "#ffffff", stopStroke: "#64748b", halo: "#ffffff", casing: "#ffffff" };
  const modeColour = ["match", ["get", "mode"], "streetcar", SURFACE_COLOURS.streetcar, SURFACE_COLOURS.bus];
  map.addSource("route-path", { type: "geojson", data: featureCollection([]) });
  map.addSource("bus-stops", { type: "geojson", data: featureCollection([]) });
  map.addSource("vehicles", { type: "geojson", data: featureCollection([]) });
  const before = "line-casing";
  map.addLayer({ id: "route-path-casing", type: "line", source: "route-path", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 10, 5, 16, 11] } }, before);
  map.addLayer({ id: "route-path", type: "line", source: "route-path", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "colour"], "line-width": ["interpolate", ["linear"], ["zoom"], 10, 3, 16, 7], "line-opacity": 0.9 } }, before);
  map.addLayer({ id: "bus-stops", type: "circle", source: "bus-stops", minzoom: 15, paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 15, 2.5, 18, 6], "circle-color": c.stop, "circle-stroke-color": c.stopStroke, "circle-stroke-width": 1.4,
    "circle-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0, 15.4, 1], "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0, 15.4, 1],
  } }, "outages");
  map.addLayer({ id: "vehicles", type: "circle", source: "vehicles", minzoom: 11, maxzoom: 14, paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 11, 1.6, 14, 4], "circle-color": modeColour,
    "circle-stroke-color": c.casing, "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 11, 0.4, 14, 1.2],
    "circle-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0, 11.5, ["get", "fade"]], "circle-stroke-opacity": ["interpolate", ["linear"], ["zoom"], 11, 0, 11.5, ["get", "fade"]],
  } }, "outages");
  const iconSize = ["interpolate", ["linear"], ["zoom"], 14, 0.75, 17, 1.05];
  map.addLayer({ id: "vehicle-heading", type: "symbol", source: "vehicles", minzoom: 14, filter: ["get", "heading"], layout: {
    "icon-image": "heading", "icon-size": iconSize, "icon-rotate": ["get", "bearing"], "icon-rotation-alignment": "map",
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  }, paint: { "icon-color": modeColour } }, "outages");
  map.addLayer({ id: "vehicle-icon", type: "symbol", source: "vehicles", minzoom: 14, layout: {
    "icon-image": ["match", ["get", "mode"], "streetcar", "badge-streetcar", "badge-bus"], "icon-size": iconSize,
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  }, paint: { "icon-opacity": ["interpolate", ["linear"], ["zoom"], 14, 0, 14.3, ["get", "fade"]] } }, "outages");
  map.addLayer({ id: "vehicle-label", type: "symbol", source: "vehicles", minzoom: 15, layout: {
    "text-field": ["get", "route"], "text-font": ["Noto Sans Medium"], "text-size": 11, "text-anchor": "left", "text-offset": [1.3, 0],
    "text-allow-overlap": false, "text-optional": true,
  }, paint: { "text-color": modeColour, "text-halo-color": c.halo, "text-halo-width": 1.8 } });
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
            el("span", { class: "with-badge" }, el("span", { class: `route-pill ${a.mode}` }, a.short_name), el("span", {}, a.towards)),
            el("span", { class: "mins" }, a.seconds < 60 ? "Due" : `${Math.round(a.seconds / 60)} min`),
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
    el("p", { class: "sheet-kicker" }, data.stop.code ? `Stop ${data.stop.code}` : "Stop"),
    el("h2", {}, data.stop.name),
    el("p", { class: "route-pills" }, routes.map((r) => routePill(view, r))),
    el("p", { class: "sheet-label" }, "Next vehicles (estimated)"),
    stopArrivals(data),
    el("p", { class: "muted small" }, "TTC publishes no bus or streetcar predictions, so these are estimated from each vehicle's live position and the scheduled time between stops."),
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
// since Kitchener green and Line 2 green are close. Positions come from the timetable.
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
  const c = prefersDark() ? { casing: "#0b0d10", fill: "#14171c", label: "#eef1f5", halo: "#0b0d10" } : { casing: "#ffffff", fill: "#ffffff", label: "#111418", halo: "#ffffff" };
  map.addSource("regional-lines", { type: "geojson", data: featureCollection([]) });
  map.addSource("regional-stations", { type: "geojson", data: featureCollection([]) });
  map.addSource("regional-trains", { type: "geojson", data: featureCollection([]) });
  addRegionalPill(map);
  const below = "route-path-casing";
  map.addLayer({ id: "regional-line-casing", type: "line", source: "regional-lines", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": c.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 16, 8] } }, below);
  map.addLayer({ id: "regional-line", type: "line", source: "regional-lines", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": ["get", "colour"], "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.4, 16, 5], "line-opacity": 0.85 } }, below);
  map.addLayer({ id: "regional-stations", type: "circle", source: "regional-stations", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, ["case", ["get", "major"], 3.5, 2.5], 16, ["case", ["get", "major"], 8, 6.5]],
    "circle-color": c.fill, "circle-stroke-color": ["get", "colour"], "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 8, 1.4, 16, 3],
  } }, "outages");
  map.addLayer({ id: "regional-trains", type: "symbol", source: "regional-trains", layout: {
    "icon-image": "regional-pill", "icon-size": ["interpolate", ["linear"], ["zoom"], 8, 0.75, 14, 1.15], "icon-allow-overlap": true, "icon-ignore-placement": true,
    "text-field": ["get", "line"], "text-font": ["Noto Sans Medium"], "text-size": ["interpolate", ["linear"], ["zoom"], 8, 7.5, 14, 11], "text-allow-overlap": true, "text-ignore-placement": true,
  }, paint: { "icon-color": ["get", "colour"], "icon-halo-color": c.casing, "icon-halo-width": 1.5, "text-color": "#ffffff" } }, "trains-halo");
  map.addLayer({ id: "regional-station-label", type: "symbol", source: "regional-stations", minzoom: 9, filter: ["any", ["get", "major"], [">=", ["zoom"], 10.5]], layout: {
    "text-field": ["get", "name"], "text-font": ["Noto Sans Regular"], "text-size": ["interpolate", ["linear"], ["zoom"], 9, 10, 16, 13],
    "text-anchor": "left", "text-offset": [0.8, 0], "text-optional": true, "text-max-width": 9,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 2, "text-opacity": 0.85 } });
}

function regionalTrainSheet(view, id) {
  const t = ((view.regionalTrains || {}).trains || []).find((x) => x.id === id);
  if (!t) return null;
  return [
    el("p", { class: "sheet-kicker" }, "Regional train"),
    el("h2", { class: "with-badge" }, regionalPill(t.line, t.colour), `${t.line_name} towards ${t.destination}`),
    el("p", {}, t.at_station ? `At ${t.at_station}` : t.next_station ? `Next: ${t.next_station}` : "Arriving", t.next_arrival && !t.at_station ? el("span", { class: "muted" }, ` · scheduled ${timeFmt.format(new Date(t.next_arrival))}`) : ""),
    el("p", { class: "muted small" }, "Placed by the published timetable, not a live position: live regional train data needs a Metrolinx API key this site does not have yet. Delays will not show."),
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
    el("p", { class: "sheet-label" }, "Scheduled departures"),
    data.departures.length
      ? el("ul", { class: "sheet-list" }, data.departures.map((d) => el("li", { class: "arrival-row" }, el("span", { class: "with-badge" }, regionalPill(d.line, d.colour, { small: true }), el("span", {}, d.destination)), el("span", { class: "mins" }, d.minutes < 1 ? "Now" : d.minutes < 60 ? `${d.minutes} min` : timeFmt.format(new Date(d.departs))))))
      : el("p", { class: "muted" }, "No departures in the next three hours."),
    el("p", { class: "muted small" }, "From the published timetable; delays and cancellations are not shown."),
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
  const c = prefersDark() ? { label: "#eef1f5", halo: "#0b0d10" } : { label: "#111418", halo: "#ffffff" };
  map.addSource("parking", { type: "geojson", data: featureCollection([]) });
  map.addSource("bike-parking", { type: "geojson", data: featureCollection([]) });
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
    planHere(view, item.coord, item.name),
    el("p", { class: "muted small" }, "From OpenStreetMap."),
  ];
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
const EVENT_COLOUR = "#334155";
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
  map.addSource("events", { type: "geojson", data: featureCollection([]) });
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
  return 0.299 * (n >> 16) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255) > 160 ? "#111418" : "#ffffff";
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
  const c = prefersDark() ? { fill: "#14171c", ink: "#eef1f5" } : { fill: "#ffffff", ink: "#111418" };
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
    p.error = "The planner covers Oakville to Ajax and north to about Major Mackenzie Drive.";
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
    el("p", { class: "muted small" }, "Planned from the published TTC and regional timetables, not live predictions; check the station or stop for live times. Walking routes, addresses and places use OpenStreetMap. Your travel choices are kept on this device."),
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
  ctx.fillStyle = "#3b6cf6";
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
  const c = prefersDark() ? { casing: "#0b0d10", stationFill: "#14171c", stationStroke: "#eef1f5", label: "#eef1f5", halo: "#0b0d10", trainStroke: "#ffffff" } : { casing: "#ffffff", stationFill: "#ffffff", stationStroke: "#111418", label: "#111418", halo: "#ffffff", trainStroke: "#111418" };
  map.addSource("lines", { type: "geojson", data: lineFc });
  map.addSource("stations", { type: "geojson", data: stations });
  map.addSource("docks", { type: "geojson", data: dockFeatures(data, mapState.mode) });
  map.addSource("trains", { type: "geojson", data: featureCollection([]) });
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
  map.addSource("entrances", { type: "geojson", data: entrances });
  map.addLayer({ id: "entrances", type: "symbol", source: "entrances", minzoom: 15, layout: {
    "icon-image": ["case", ["get", "step_free"], "badge-entrance-free", "badge-entrance"], "icon-size": ["interpolate", ["linear"], ["zoom"], 15, 0.7, 18, 1],
    "icon-allow-overlap": true, "text-field": ["step", ["zoom"], "", 16.5, ["get", "name"]], "text-font": ["Noto Sans Medium"], "text-size": 11,
    "text-anchor": "top", "text-offset": [0, 1.1], "text-optional": true, "text-max-width": 8,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 1.6, "icon-opacity": ["interpolate", ["linear"], ["zoom"], 15, 0, 15.3, 1] } });
  // A soft shadow under each train badge; later layers are placed relative to it.
  map.addLayer({ id: "trains-halo", type: "circle", source: "trains", paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 7, 16, 15], "circle-color": "#000000", "circle-opacity": prefersDark() ? 0.35 : 0.12, "circle-blur": 0.6 } });
  map.addLayer({ id: "trains", type: "symbol", source: "trains", layout: {
    "icon-image": ["concat", "badge-train-", ["get", "line"]], "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.55, 13, 0.8, 16, 1.05],
    "icon-allow-overlap": true, "icon-ignore-placement": true,
  } });
  map.addLayer({ id: "station-label", type: "symbol", source: "stations", minzoom: 10.5, filter: ["any", ["get", "major"], [">=", ["zoom"], 12.5]], layout: {
    "text-field": ["get", "name"], "text-font": ["Noto Sans Medium"], "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 16, 14],
    "text-anchor": "left", "text-offset": [0.9, 0], "text-optional": true, "text-max-width": 9,
  }, paint: { "text-color": c.label, "text-halo-color": c.halo, "text-halo-width": 2 } });
  map.addLayer({ id: "me-accuracy", type: "fill", source: "me", filter: ["==", ["geometry-type"], "Polygon"], paint: { "fill-color": "#3b6cf6", "fill-opacity": 0.12 } });
  map.addLayer({ id: "me-pulse", type: "circle", source: "me", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 9, "circle-color": "#3b6cf6", "circle-opacity": 0.25 } });
  map.addLayer({ id: "me", type: "circle", source: "me", filter: ["==", ["geometry-type"], "Point"], paint: { "circle-radius": 7, "circle-color": "#3b6cf6", "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 } });
  map.addLayer({ id: "selected", type: "circle", source: "selected", paint: { "circle-radius": 16, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#3b6cf6", "circle-stroke-width": 3 } });
  applyLayerToggles(map);
}

function applyLayerToggles(map) {
  const show = (ids, on) => ids.forEach((id) => map.getLayer(id) && map.setLayoutProperty(id, "visibility", on ? "visible" : "none"));
  show(["trains", "trains-halo"], mapState.layers.trains);
  show(["regional-line", "regional-line-casing", "regional-stations", "regional-station-label", "regional-trains"], mapState.layers.regional);
  show(["vehicles", "vehicle-heading", "vehicle-icon", "vehicle-label", "bus-stops", "route-path", "route-path-casing"], mapState.layers.buses);
  show(["docks", "dock-icon"], mapState.layers.bikes);
  show(["outages"], mapState.layers.access);
  show(["entrances"], mapState.layers.entrances);
  show(["parking", "parking-entrances"], mapState.layers.parking);
  show(["bike-parking"], mapState.layers.bikeParking);
  show(["road-work-line", "road-closure-line", "road-closure-point", "road-icons", "road-icons-point"], mapState.layers.roads);
  show(["lanes", "lanes-trail"], mapState.layers.lanes);
  show(["events"], mapState.layers.events);
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
    el("p", { class: "sheet-kicker" }, "Subway train"),
    el("h2", { class: "with-badge" }, badge(lineId, { small: true }), towards ? `Towards ${towards}` : `Line ${lineId}`),
    el("p", {}, t.at_station ? `At ${t.station}` : `Approaching ${t.station}`, t.at_station ? "" : el("span", { class: "muted" }, ` · ${minutesText(t.eta_seconds)}`)),
    el("p", { class: "muted small" }, "Position estimated from TTC's predicted arrival times; the subway feed has no GPS positions."),
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
  view.selected = { kind, id };
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
    : null;
  if (selected) selected.setData(featureCollection(where ? [point(where, {})] : []));
  if (kind === "stop") loadStopSheet(view, id);
  if (kind === "regional-station") loadRegionalStationSheet(view, id);
  const flows = view.map.getSource("flows");
  if (flows) flows.setData(featureCollection([]));
  if (kind === "dock") loadDockTrips(view, id);
}

function closeSheet(view) {
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

// Redraws moving vehicles on every animation frame (fewer zoomed out, see MOTION_FRAME_MS);
// with reduced motion, every MOTION_STILL_MS. Hidden pages get no frames.
function animateVehicles(view) {
  if (reduceMotion()) {
    view.timers.push(setInterval(() => { if (document.visibilityState === "visible") moveVehicles(view); }, MOTION_STILL_MS));
    return;
  }
  let last = 0;
  const step = (now) => {
    if (activeMap !== view) return;
    const zoom = view.map.getZoom();
    const gap = MOTION_FRAME_MS.find(([from]) => zoom >= from)[1];
    // A pan or zoom also redraws, so vehicles entering the view are placed at once.
    if (now - last >= gap || view.map.isMoving()) {
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
  { key: "regional", label: "Regional trains", note: "Placed by timetable", icon: () => badgeEl("regional", "#4b5563", "#ffffff") },
  { key: "buses", label: "Buses and streetcars", note: "Live positions and stops", icon: () => badgeEl("bus", SURFACE_COLOURS.bus, "#ffffff") },
  { key: "bikes", label: "Bike Share", note: "Live bikes or open docks", icon: () => badgeEl("bike", DOCK_COLOURS.ok, "#ffffff") },
  { key: "access", label: "Elevator outages", note: "Stations with an elevator or escalator out", icon: () => badgeEl("elevator", OUTAGE_COLOUR, "#111418") },
  { key: "entrances", label: "Station entrances", note: "Close up; step-free ones in blue", icon: () => badgeEl("entrance", ENTRANCE_COLOUR, "#ffffff") },
  { key: "roads", label: "Roadwork", note: "Construction and road closures", icon: () => badgeEl("roadwork", ROAD_COLOURS.construction, "#111418") },
  { key: "events", label: "Events", note: "Festivals and events this week", icon: () => badgeEl("event", EVENT_COLOUR, "#ffffff") },
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
  const surface = [["bus", "Bus"], ["streetcar", "Streetcar"]];
  return el(
    "div",
    { class: "map-legend" },
    el("p", { class: "panel-heading" }, "Legend"),
    el("ul", { class: "legend-list" },
      lines.map((l) => legendRow(badgeEl("subway", l.color || "#777", textOn(l.color || "#777777"), { size: 22 }), `Line ${l.id} train (estimated position)`)),
      surface.map(([mode, label]) => legendRow(badgeEl(mode, SURFACE_COLOURS[mode], "#ffffff", { size: 22 }), `${label}, with the pointer showing its heading`)),
      legendRow(el("span", { class: "swatch-pill" }, "LW"), "Regional train, placed by timetable"),
      legendRow(el("span", { class: "swatch-station", "aria-hidden": "true" }), "Station"),
      legendRow(badgeEl("elevator", OUTAGE_COLOUR, "#111418", { size: 20 }), "Elevator or escalator out of service"),
    ),
    el("p", { class: "muted small" }, "Between updates, trains, buses and streetcars move along their routes by estimate: subway trains to TTC's predicted arrival, buses at their route's usual pace from their last report, regional trains by timetable. A faded bus or streetcar has not reported for over two and a half minutes."),
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
      legendRow(el("span", { class: "swatch-me", "aria-hidden": "true" }), "Your location"),
      legendRow(lineSwatch(prefersDark() ? "#eef1f5" : "#111418"), "Planned trip")),
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
    el("p", { class: "eyebrow" }, data.stop.code ? `Stop ${data.stop.code}` : "Stop"),
    el("h1", {}, data.stop.name),
    el("h2", {}, "Next vehicles (estimated)"),
    el("div", { class: "card" }, stopArrivals(data)),
    el("p", { class: "muted small" }, "TTC publishes no bus or streetcar predictions, so these are estimated from each vehicle's live position and the scheduled time between stops. They assume the vehicle keeps the schedule's pace."),
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
  const status = el("p", { class: "map-status", "aria-live": "polite" });
  const locateButton = el("button", { type: "button", class: "map-fab locate", "aria-label": "Show where I am and what is nearest", title: "Near me" }, iconSvg("locate", { size: 22 }));
  const planButton = el("button", { type: "button", class: "map-fab plan", "aria-label": "Directions", title: "Directions", hidden: "hidden" }, iconSvg("directions", { size: 22 }));
  const layersButton = el("button", { type: "button", class: "map-tool", "aria-label": "Layers and legend", "aria-expanded": "false", "aria-controls": "map-layers", title: "Layers and legend" }, iconSvg("layers", { size: 22 }));
  const layersPanel = el("aside", { class: "map-panel", id: "map-layers", "aria-label": "Layers and legend", hidden: "hidden" });
  const safetyButton = el("button", { type: "button", class: "map-tool", "aria-label": "Safety", "aria-expanded": "false", "aria-controls": "map-safety", title: "Safety" }, iconSvg("shield", { size: 22 }));
  const safetyPanelEl = el("aside", { class: "map-panel safety", id: "map-safety", "aria-label": "Safety", hidden: "hidden" });
  const search = el("input", { type: "search", class: "map-search", placeholder: "Search stations, stops or routes", "aria-label": "Search stations, stops or routes", autocomplete: "off", enterkeyhint: "go" });
  const results = el("ul", { class: "map-results" });
  const modeGroup = el("span", { class: "chip-group", role: "group", "aria-label": "Show Bike Share" });
  const toolbar = el("div", { class: "map-toolbar" }, el("div", { class: "map-search-row" }, el("div", { class: "map-search-wrap" }, search, results), layersButton, safetyButton), status);
  const guideTop = el("div", { class: "guide-card", "aria-live": "polite" });
  const guideBar = el("div", { class: "guide-bar" });
  const page = el("div", { class: "map-page" }, container, toolbar, layersPanel, safetyPanelEl, guideTop, guideBar, planButton, locateButton, sheet, hasBasemap ? null : el("p", { class: "map-note" }, "Street map not built yet: run the map-tiles tool (docs/map.md)."));

  const view = {
    map: null, timers: [], watch: null, data, trains, coords: coordsOf(data), sheet, sheetBody, status, me: null, accuracy: 0, selected: null,
    pulse: 0, trainFeatures: null, vehicleFeatures: null, surfaceStops: new Map(), surfaceRoutes: new Map(), route: null,
    regional: null, regionalTrains: null, regionalTrainFeatures: null, roads: null, events: null,
    planner: null, plan: null, guide: null, page, guideTop, guideBar,
  };

  const renderChips = () => {
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
      const found = await findPlaces(view, typed);
      if (search.value !== typed || !found.length) return;
      view.placeResults = found.slice(0, 5);
      results.querySelectorAll(".place-result").forEach((n) => n.remove());
      results.append(...suggestionList(view.placeResults, "data-place").map((li) => { li.classList.add("place-result"); return li; }));
    }, SUGGEST_MS);
    const stations = view.data.stations.filter((s) => searchKey(s.name).includes(query)).slice(0, 4);
    const regionalStations = ((view.regional || {}).stations || []).filter((s) => searchKey(s.name).includes(query)).slice(0, 3);
    const routes = [...view.surfaceRoutes.values()].filter((r) => r.short_name === query || searchKey(`${r.short_name} ${r.long_name}`).startsWith(query)).slice(0, 3);
    const stops = query.length < 3 ? [] : [...view.surfaceStops.values()].filter((s) => s[1] === query || searchKey(s[2]).includes(query)).slice(0, 5);
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
      maxBounds: MAP_BOUNDS, attributionControl: false, dragRotate: false, pitchWithRotate: false, fadeDuration: reduceMotion() ? 0 : 200,
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new lib.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new lib.AttributionControl({ compact: true, customAttribution: "Data: TTC, Bike Share Toronto, City of Toronto and Toronto Parking Authority (OGL – Toronto); regional rail data provided with the permission of Metrolinx" }), "bottom-left");
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
      for (const layer of ["stations", "docks", "dock-icon", "trains", "vehicles", "vehicle-icon", "bus-stops", "regional-trains", "regional-stations", "road-work-line", "road-closure-line", "road-closure-point", "road-icons", "road-icons-point", "lanes", "lanes-trail", "events", "outages", "entrances", "parking", "parking-entrances", "bike-parking"]) {
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
        const layers = ["trains", "regional-trains", "events", "entrances", "stations", "outages", "parking-entrances", "parking", "bike-parking", "regional-stations", "vehicle-icon", "vehicles", "bus-stops", "dock-icon", "docks", "road-icons", "road-icons-point", "road-work-line", "road-closure-line", "road-closure-point", "lanes", "lanes-trail"].filter((l) => map.getLayer(l) && map.getLayoutProperty(l, "visibility") !== "none");
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
        `${numberFmt.format(bikes.succeeded_24h)} collections in the last 24 hours (at most ${bikes.expected_24h}), ${bikes.failed_attempts_24h} failed attempts`,
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
          ? `${numberFmt.format(data.weather.hours)} hours stored, latest observation ${dayFmt.format(new Date(data.weather.latest_observation))} (Environment Canada publishes each hour several hours late)`
          : "No observations loaded yet",
      ),
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
  return el("div", { class: "skeleton", "aria-hidden": "true" }, el("div"), el("div"), el("div"));
}

// Swap in new content. Navigations cross-fade (View Transitions, where supported) and
// stagger their sections in; periodic refreshes replace content without any motion.
function show(content, { refresh }) {
  const nodes = [content].flat(Infinity).filter(Boolean);
  const swap = () => {
    main.classList.toggle("entering", !refresh);
    nodes.forEach((node, i) => node.style && node.style.setProperty("--i", Math.min(i, 10)));
    main.replaceChildren(...nodes);
    for (const node of nodes) if (typeof node.mount === "function") node.mount();
  };
  if (!refresh && document.startViewTransition && !reduceMotion() && !document.body.classList.contains("map-mode")) {
    document.startViewTransition(swap);
  } else {
    swap();
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
  const [, kind, id] = (location.hash || "#/").split("/");
  const seq = ++renderSeq;
  if (!refresh && kind !== "map") mapCleanup();
  const waiting = refresh ? null : setTimeout(() => seq === renderSeq && kind !== "map" && main.replaceChildren(skeleton()), 160);
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
    else if (kind === "reliability") content = await renderReliability();
    else if (kind === "report") content = await renderReport();
    else if (kind === "map") content = await renderMap(id);
    else if (kind === "stop" && id) content = await renderStop(decodeURIComponent(id));
    else if (kind === "pipeline") content = await renderPipeline();
    else if (kind === "events") content = await renderEvents(id);
    else content = await renderHome();
    clearTimeout(waiting);
    if (seq !== renderSeq) return; // A newer navigation or refresh finished first.
    const state = refresh ? captureState() : null;
    show(content, { refresh });
    if (state) restoreState(state);
    markCurrentNav(kind, id);
    updated.textContent = `Updated ${timeFmt.format(new Date())}`;
    updated.classList.remove("warn");
  } catch (error) {
    clearTimeout(waiting);
    if (seq !== renderSeq) return;
    if (refresh && main.querySelector("h1") && error.message !== "Not found") {
      // Keep the last good data on screen rather than replacing it with an error.
      updated.textContent = `Could not refresh at ${timeFmt.format(new Date())}. Showing earlier data; retrying shortly.`;
      updated.classList.add("warn");
      return;
    }
    main.replaceChildren(el("h1", {}, "Unavailable"), el("p", {}, error.message === "Not found" ? "That line or station does not exist." : "Live data is unavailable right now. Retrying shortly."));
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
setHeaderHeight();
route();
