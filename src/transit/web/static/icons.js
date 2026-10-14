"use strict";
// Line icons on a 24-unit grid, drawn with round 2-unit strokes. The same path data renders
// as inline SVG in the page and is rasterized onto canvases for the map, so the legend and
// the map always match. There are no image files or sprite sheets. Vehicles differ by
// silhouette as well as colour: the subway from the front, the streetcar with its trolley
// pole, the bus with wheels, and the two-level regional coach. Station entrances are a door
// with an arrow, car parks a P, and bike parking a pair of racks.

const ICONS = {
  subway: "M8 3h8a4 4 0 0 1 4 4v7a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a4 4 0 0 1 4-4z M7 7h10v4H7z M8 14h.01 M16 14h.01 M8 21l2-4 M16 21l-2-4",
  streetcar: "M3 9h18v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z M3 13h18 M9 9v4 M15 9v4 M12 9l-4-5 M5 4h8 M7 21h10",
  bus: "M3 5h14l4 5v7H3z M3 10h18 M9 5v5 M14 5v5 M7 17.5m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0 M17 17.5m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0",
  regional: "M4 4h13a4 4 0 0 1 4 4v9H3V5a1 1 0 0 1 1-1z M3 9h18 M3 13h18 M7 20h.01 M12 20h.01 M17 20h.01",
  bike: "M6 16.5m-3.5 0a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0 M18 16.5m-3.5 0a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0 M6 16.5l3.5-7.5h5.5 M18 16.5l-3-7.5 M9.5 9l3.5 7.5H6 M15 9l-.8-2.5H12",
  stop: "M12 21v-9 M7 3h10a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z M9 7.5h6",
  station: "M12 12m-8 0a8 8 0 1 0 16 0a8 8 0 1 0-16 0 M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  event: "M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z",
  roadwork: "M10 4h4l4.5 15h-13z M8.3 10h7.4 M7 14.5h10 M4 20h16",
  closure: "M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M7 12h10",
  elevator: "M6 3h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z M8.5 10L12 6.5l3.5 3.5 M8.5 14l3.5 3.5 3.5-3.5",
  lane: "M5 21L9 3 M19 21L15 3 M12 5v3 M12 11v3 M12 17v3",
  me: "M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0 M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0",
  layers: "M12 3l9 4.5-9 4.5-9-4.5z M3 12l9 4.5 9-4.5 M3 16.5l9 4.5 9-4.5",
  directions: "M4 20v-7a4 4 0 0 1 4-4h11 M15 5l4 4-4 4",
  locate: "M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0 M12 12m-8 0a8 8 0 1 0 16 0a8 8 0 1 0-16 0 M12 2v2 M12 20v2 M2 12h2 M20 12h2",
  navigate: "M12 2l7 19-7-4-7 4z",
  walk: "M13.5 4.5m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0 M7 21l3-6 3 2.5V21 M10 15l1.2-5.5 3.3 3 3 .5 M11.2 9.5L8 11.5l-1 3",
  clock: "M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M12 7v5l3 2",
  close: "M6 6l12 12 M18 6L6 18",
  left: "M16 20v-6a4 4 0 0 0-4-4H5 M9 6l-4 4 4 4",
  right: "M8 20v-6a4 4 0 0 1 4-4h7 M15 6l4 4-4 4",
  straight: "M12 20V4 M6 10l6-6 6 6",
  slightly_left: "M15 20v-6L8 7 M8 13V7h6",
  slightly_right: "M9 20v-6l7-7 M16 13V7h-6",
  uturn: "M8 20V10a4 4 0 0 1 8 0v4 M12 11l4 4 4-4",
  arrive: "M6 21V4 M6 4h11l-2.5 4.5L17 13H6",
  stairs: "M3 20h5v-5h5v-5h5V5h3",
  entrance: "M13 3h6a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1h-6 M3 12h11 M10 8l4 4-4 4",
  stepfree: "M10 4.5m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0 M10 8v6h5l2.5 5 M10 11h5 M7 11.5a5 5 0 1 0 7.5 6",
  parking: "M8 20V4h5a4.5 4.5 0 0 1 0 9H8",
  rack: "M4 20V9a3 3 0 0 1 6 0v11 M14 20V9a3 3 0 0 1 6 0v11 M2 20h20",
  pin: "M12 21s-7-6.2-7-11.5a7 7 0 0 1 14 0C19 14.8 12 21 12 21z M12 9.5m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0",
  shield: "M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z M12 8v5 M12 16h.01",
};

// OpenTripPlanner's walking directions, mapped to the closest icon.
const STEP_ICONS = {
  depart: "walk", continue: "straight", left: "left", hard_left: "left", right: "right", hard_right: "right",
  slightly_left: "slightly_left", slightly_right: "slightly_right", uturn_left: "uturn", uturn_right: "uturn",
  circle_clockwise: "uturn", circle_counterclockwise: "uturn", elevator: "elevator", enter_station: "station",
  exit_station: "station", follow_signs: "straight",
};

const SVG_NS = "http://www.w3.org/2000/svg";

function iconSvg(name, { size = 20, label = null, className = "icon" } = {}) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("class", className);
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name] || ICONS.station);
  svg.append(path);
  return svg;
}

function drawIcon(ctx, name, cx, cy, size, colour) {
  ctx.save();
  ctx.translate(cx - size / 2, cy - size / 2);
  ctx.scale(size / 24, size / 24);
  ctx.strokeStyle = colour;
  ctx.lineWidth = 2.2;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.stroke(new Path2D(ICONS[name] || ICONS.station));
  ctx.restore();
}

// A round map badge: a filled disc with a ring in the map's casing colour and the glyph on
// top. Sizes are CSS pixels; the canvas is drawn at twice that for sharp screens.
function badgeImage(name, fill, ink, ring, { css = 26, glyph = 15, hollow = false } = {}) {
  const ratio = 2;
  const size = css * ratio;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const r = size / 2 - ratio;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, r, 0, 2 * Math.PI);
  ctx.fillStyle = hollow ? ring : fill;
  ctx.fill();
  ctx.lineWidth = hollow ? 2.5 * ratio : 1.5 * ratio;
  ctx.strokeStyle = hollow ? fill : ring;
  ctx.stroke();
  drawIcon(ctx, name, size / 2, size / 2, glyph * ratio, hollow ? fill : ink);
  return ctx.getImageData(0, 0, size, size);
}

function addBadge(map, id, name, fill, ink, ring, options) {
  if (map.hasImage(id)) map.removeImage(id);
  map.addImage(id, badgeImage(name, fill, ink, ring, options), { pixelRatio: 2 });
}

// A small pointer on a ring around a badge, tinted per feature (SDF) and rotated to the
// vehicle's heading; drawn larger than the badge so the pointer sits just outside it.
function addHeadingImage(map) {
  const ratio = 2;
  const size = 40 * ratio;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.beginPath();
  ctx.moveTo(size / 2, 1 * ratio);
  ctx.lineTo(size / 2 + 6 * ratio, 9 * ratio);
  ctx.lineTo(size / 2 - 6 * ratio, 9 * ratio);
  ctx.closePath();
  ctx.fill();
  if (map.hasImage("heading")) map.removeImage("heading");
  map.addImage("heading", ctx.getImageData(0, 0, size, size), { sdf: true, pixelRatio: ratio });
}

// The same badge as a page element, for the legend and panels.
function badgeEl(name, fill, ink, { hollow = false, size = 26 } = {}) {
  const span = document.createElement("span");
  span.className = hollow ? "badge-icon hollow" : "badge-icon";
  span.style.width = span.style.height = `${size}px`;
  if (hollow) {
    span.style.borderColor = fill;
    span.style.color = fill;
  } else {
    span.style.background = fill;
    span.style.color = ink;
  }
  span.append(iconSvg(name, { size: Math.round(size * 0.6) }));
  return span;
}
