"use strict";

// contract with the backend, tests/test_frontend.py keeps these in step with the api
const API_URL = "/api/itinerary";
const ACCESS_HEADER = "X-Access-Code";
const MAX_DAYS = 7;
const MAX_INTERESTS = 5;
const MAX_INTEREST_CHARS = 40;
// server caps a request at 150 s, give it a little longer before giving up here
const CLIENT_TIMEOUT_MS = 160000;
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
const OSM_TYPES = new Set(["node", "way", "relation"]);
const EMPTY_MAP_TEXT = "Choose a city and generate a plan to see each day's stops and walking route here.";
// account routes in app/auth/routes.py
const ACCOUNT_URLS = { me: "/api/auth/me", signout: "/api/auth/signout", twoFactor: "/api/auth/2fa" };

// every error code the api can return, with what the visitor should do next
const MESSAGES = {
  invalid_request: "Some details need fixing. Check the city and interests, then try again.",
  city_not_found: "That city was not found. Check the spelling or add the country, for example Porto, Portugal.",
  not_enough_places: "Not enough places were found around this city. Try a larger city or fewer days.",
  upstream_unavailable: "A map data service is busy right now. Try again in a minute.",
  planner_failed: "The planner could not build a plan this time. Try again.",
  request_timeout: "Planning took too long. Try again, or try fewer days.",
  payload_too_large: "The request was too large. Shorten the interests and try again.",
  server_misconfigured: "The server is not set up correctly. Contact the demo owner.",
  internal_error: "Something went wrong on the server. Try again.",
  access_denied: "Enter the access code for this demo.",
  rate_limited: "You have planned a lot in the last hour.",
  daily_cap_reached: "The demo has used up today's plans.",
  cross_site_request: "This request came from another site and was blocked. Open TrueRoute directly and try again.",
  invalid_credentials: "That password is not right.",
  not_signed_in: "Your session ended. Sign in again.",
  network: "Could not reach the server. Check your connection and try again.",
  client_timeout: "No answer from the server. Try again in a moment.",
};

const state = {
  map: null,
  layer: null,
  markers: [],
  routingAttribution: null,
  data: null,
  day: 0,
  stop: 0,
  busy: false,
  // the signed-in account from /api/auth/me, null when signed out or accounts are off
  account: null,
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  // text only, place names come from openstreetmap and are untrusted
  if (text !== undefined) node.textContent = text;
  return node;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// formatting

function formatDistance(km) {
  if (km < 1) return `${Math.max(10, Math.round((km * 1000) / 10) * 10)} m`;
  return `${km.toFixed(1)} km`;
}

function formatMinutes(minutes) {
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function plural(n, word) {
  return n === 1 ? `1 ${word}` : `${n} ${word}s`;
}

function capitalize(text) {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

function isWalking(day) {
  return day.distance_source === "walking" && day.route_latlon.length > 1;
}

function legText(stop, walking) {
  if (stop.leg_distance_km === null || stop.leg_distance_km === undefined) return null;
  if (walking && stop.leg_walk_minutes !== null) {
    return `${formatMinutes(stop.leg_walk_minutes)} walk, ${formatDistance(stop.leg_distance_km)}`;
  }
  return `${formatDistance(stop.leg_distance_km)} straight-line`;
}

function osmLink(place) {
  if (!OSM_TYPES.has(place.osm_type) || !Number.isInteger(place.osm_id) || place.osm_id <= 0) return null;
  return `https://www.openstreetmap.org/${place.osm_type}/${place.osm_id}`;
}

// form

function clampDays(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return 1;
  return Math.min(MAX_DAYS, Math.max(1, n));
}

function syncStepper() {
  const input = $("days");
  const days = clampDays(input.value);
  input.value = String(days);
  document.querySelectorAll(".stepper-btn").forEach((b) => {
    const step = Number(b.dataset.step);
    b.disabled = (step < 0 && days <= 1) || (step > 0 && days >= MAX_DAYS);
  });
}

function readInterests() {
  const picked = [...document.querySelectorAll('input[name="interest"]:checked')].map((i) => i.value);
  const typed = $("interests").value.split(",").map((s) => s.trim()).filter(Boolean);
  const seen = new Set();
  // same interest twice counts once, whatever the case
  return [...picked, ...typed].filter((s) => {
    const key = s.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function readForm() {
  const form = $("plan-form");
  const city = $("city").value.trim();
  const interests = readInterests();
  const errors = [];
  if (!city) errors.push(["city", "Enter a city."]);
  if (interests.length > MAX_INTERESTS) errors.push(["interests", `Choose at most ${MAX_INTERESTS} interests in total.`]);
  if (interests.some((s) => s.length > MAX_INTEREST_CHARS)) {
    errors.push(["interests", `Keep each interest under ${MAX_INTEREST_CHARS} characters.`]);
  }
  $("city").setAttribute("aria-invalid", errors.some(([f]) => f === "city") ? "true" : "false");
  $("interests").setAttribute("aria-invalid", errors.some(([f]) => f === "interests") ? "true" : "false");
  const body = {
    city,
    days: clampDays($("days").value),
    pace: form.elements.pace.value,
    budget: form.elements.budget.value,
    interests,
  };
  return { body, errors };
}

function setStatus(text, isError) {
  const status = $("status");
  status.textContent = text;
  status.classList.toggle("is-error", Boolean(isError));
}

function setBusy(busy) {
  state.busy = busy;
  const button = $("submit");
  button.disabled = busy;
  button.textContent = busy ? "Generating…" : "Generate itinerary";
  $("progress").hidden = !busy;
}

function retryText(response) {
  const seconds = Number(response.headers.get("Retry-After"));
  if (!Number.isFinite(seconds) || seconds <= 0) return "Try again later.";
  if (seconds < 90) return `Try again in ${Math.ceil(seconds)} seconds.`;
  if (seconds < 5400) return `Try again in ${Math.ceil(seconds / 60)} minutes.`;
  return `Try again in about ${Math.round(seconds / 3600)} hours.`;
}

async function readError(response) {
  let code = "internal_error";
  let detail = "";
  try {
    const data = await response.json();
    if (data && data.error && typeof data.error.code === "string") {
      code = data.error.code;
      detail = typeof data.error.message === "string" ? data.error.message : "";
    }
  } catch (_) {
    // a proxy error page is not json, keep the generic message
  }
  let message = MESSAGES[code] || MESSAGES.internal_error;
  if (code === "invalid_request" && detail) message = `${message} (${detail})`;
  if (code === "rate_limited" || code === "daily_cap_reached") message = `${message} ${retryText(response)}`;
  return { code, message };
}

function setEmptyMap(text) {
  $("map-empty-text").textContent = text;
}

async function submit(event) {
  event.preventDefault();
  if (state.busy) return;
  const { body, errors } = readForm();
  if (errors.length) {
    setStatus(errors.map(([, m]) => m).join(" "), true);
    $(errors[0][0]).focus();
    return;
  }
  const headers = { "Content-Type": "application/json", Accept: "application/json" };
  const code = $("access-code").value.trim();
  if (code) headers[ACCESS_HEADER] = code;

  setBusy(true);
  const started = Date.now();
  const tick = () => {
    const s = Math.round((Date.now() - started) / 1000);
    setStatus(`Finding places and planning your days, ${s} s. A city seen for the first time takes about half a minute.`);
  };
  tick();
  const timer = setInterval(tick, 1000);
  setEmptyMap(`Planning your trip to ${body.city}…`);
  const controller = new AbortController();
  const abort = setTimeout(() => controller.abort(), CLIENT_TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetch(API_URL, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
        credentials: "same-origin",
      });
    } catch (err) {
      const key = err && err.name === "AbortError" ? "client_timeout" : "network";
      setStatus(MESSAGES[key], true);
      setEmptyMap(EMPTY_MAP_TEXT);
      return;
    }
    if (!response.ok) {
      setEmptyMap(EMPTY_MAP_TEXT);
      const { code: errorCode, message } = await readError(response);
      setStatus(message, true);
      if (errorCode === "access_denied") {
        $("access-field").hidden = false;
        $("access-code").focus();
      }
      return;
    }
    const data = await response.json();
    setStatus("");
    render(data, body);
  } finally {
    clearInterval(timer);
    clearTimeout(abort);
    setBusy(false);
  }
}

// views

function showPlan() {
  $("result").hidden = true;
  $("plan-view").hidden = false;
  $("sidebar").scrollTop = 0;
  $("city").focus();
}

function render(data, request) {
  state.data = data;
  const it = data.itinerary;
  $("result-title").textContent = it.city;
  $("result-meta").textContent = `${plural(it.days, "day")}, ${it.pace} pace, ${it.budget} budget${
    request.interests.length ? `, ${request.interests.join(", ")}` : ""
  }`;
  $("disclaimer").textContent = it.disclaimer;
  const attribution = $("data-attribution");
  attribution.textContent = data.attribution;
  if (typeof data.attribution_url === "string" && data.attribution_url.startsWith("https://www.openstreetmap.org/")) {
    attribution.href = data.attribution_url;
  }
  setRoutingAttribution(data.routing_attribution);
  renderTabs();
  $("plan-view").hidden = true;
  $("result").hidden = false;
  $("map-empty").hidden = true;
  $("sidebar").scrollTop = 0;
  selectDay(0);
  // move keyboard and screen reader users to the plan
  $("result-title").focus();
}

function renderTabs() {
  const tabs = $("day-tabs");
  tabs.replaceChildren();
  state.data.itinerary.day_plans.forEach((day, i) => {
    const tab = el("button", "tab", `Day ${day.day}`);
    tab.type = "button";
    tab.id = `day-tab-${i}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", "day-panel");
    tab.addEventListener("click", () => selectDay(i));
    tabs.append(tab);
  });
}

function onTabKey(event) {
  const days = state.data ? state.data.itinerary.day_plans.length : 0;
  if (!days) return;
  const moves = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
  let next = null;
  if (event.key in moves) next = (state.day + moves[event.key] + days) % days;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = days - 1;
  if (next === null) return;
  event.preventDefault();
  selectDay(next);
  $(`day-tab-${next}`).focus();
}

function selectDay(index) {
  state.day = index;
  state.stop = 0;
  document.querySelectorAll(".tab").forEach((tab, i) => {
    const on = i === index;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  });
  $("day-panel").setAttribute("aria-labelledby", `day-tab-${index}`);
  const day = state.data.itinerary.day_plans[index];
  renderDay(day);
  drawDay(day);
  markStop(0, false);
}

function stat(label, value) {
  const box = el("div", "stat");
  box.append(el("p", "stat-label", label), el("p", "stat-value", value));
  return box;
}

function renderDay(day) {
  const walking = isWalking(day);
  const stats = el("div", "stats");
  stats.append(
    stat("Stops", String(day.stops.length)),
    stat(walking ? "Walking distance" : "Straight-line", day.stops.length > 1 ? formatDistance(day.total_distance_km) : "None"),
    stat("Walking time", walking ? formatMinutes(day.total_walk_minutes) : "Not routed"),
  );
  const parts = [el("h2", "day-theme", day.theme), stats];
  if (!walking && day.stops.length > 1) {
    parts.push(el("p", "route-note", "No walking route for this day. Legs show straight-line distance between stops."));
  }
  const list = el("ol", "stops");
  day.stops.forEach((stop, i) => {
    const item = el("li");
    const leg = legText(stop, walking);
    if (leg) item.append(el("div", walking ? "leg is-walking" : "leg", leg));
    const card = el("button", "stop");
    card.type = "button";
    card.id = `stop-${i}`;
    const body = el("span", "stop-body");
    body.append(el("span", "stop-name", stop.place.name), el("span", "stop-meta", capitalize(stop.place.category)));
    card.append(el("span", "stop-index", String(stop.order)), body);
    card.addEventListener("click", () => markStop(i, true));
    item.append(card);
    list.append(item);
  });
  parts.push(list);
  $("day-panel").replaceChildren(...parts);
}

function markStop(index, fromList) {
  state.stop = index;
  document.querySelectorAll(".stop").forEach((card, i) => {
    if (i === index) card.setAttribute("aria-current", "true");
    else card.removeAttribute("aria-current");
  });
  state.markers.forEach((marker, i) => {
    const node = marker.getElement();
    if (node) node.classList.toggle("is-selected", i === index);
    marker.setZIndexOffset(i === index ? 1000 : 0);
  });
  const marker = state.markers[index];
  if (fromList && marker && state.map) {
    state.map.panTo(marker.getLatLng(), { animate: !reducedMotion() });
    marker.openPopup();
  }
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// map

function initMap() {
  if (!window.L) {
    $("map-empty-text").textContent = "The map could not load. Your plan still appears in the list.";
    return;
  }
  const map = L.map("map", { zoomControl: true, worldCopyJump: true }).setView([48, 15], 4);
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  L.tileLayer(TILE_URL, { maxZoom: 19, attribution: TILE_ATTRIBUTION }).addTo(map);
  state.map = map;
  state.layer = L.layerGroup().addTo(map);
}

function setRoutingAttribution(text) {
  if (!state.map) return;
  if (state.routingAttribution) state.map.attributionControl.removeAttribution(state.routingAttribution);
  state.routingAttribution = text ? escapeHtml(text) : null;
  if (state.routingAttribution) state.map.attributionControl.addAttribution(state.routingAttribution);
}

function markerIcon(number) {
  const box = document.createElement("div");
  box.append(el("span", null, String(number)));
  return L.divIcon({ html: box, className: "stop-marker", iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -14] });
}

function popupContent(day, stop) {
  const box = el("div");
  box.append(
    el("p", "popup-name", stop.place.name),
    el("p", "popup-meta", `Day ${day.day}, stop ${stop.order}, ${stop.place.category}`),
  );
  const href = osmLink(stop.place);
  if (href) {
    const link = el("a", "popup-link", "Open in OpenStreetMap");
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener";
    box.append(link);
  }
  return box;
}

function drawDay(day) {
  if (!state.map) return;
  state.layer.clearLayers();
  const color = cssVar("--accent-line") || "#2563eb";
  const points = day.stops.map((s) => [s.place.coords.lat, s.place.coords.lon]);
  const walking = isWalking(day);
  let line = null;
  if (walking) {
    line = L.polyline(day.route_latlon, { color, weight: 4, opacity: 0.9, lineJoin: "round", interactive: false });
  } else if (points.length > 1) {
    // straight legs are an estimate, dashed so they never look like a path
    line = L.polyline(points, { color, weight: 2.5, opacity: 0.8, dashArray: "6 6", interactive: false });
  }
  if (line) state.layer.addLayer(line);
  state.markers = day.stops.map((stop, i) => {
    const marker = L.marker(points[i], { icon: markerIcon(stop.order), keyboard: true, title: stop.place.name, alt: stop.place.name });
    marker.bindPopup(popupContent(day, stop), { closeButton: true, autoPanPadding: [24, 24] });
    marker.on("click", () => {
      markStop(i, false);
      const card = $(`stop-${i}`);
      if (card) card.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });
    });
    state.layer.addLayer(marker);
    return marker;
  });
  if (points.length === 1) state.map.setView(points[0], 16);
  else state.map.fitBounds(L.latLngBounds(walking ? day.route_latlon : points), { padding: [56, 56], maxZoom: 17 });
}

// account

function showAccount(account) {
  state.account = account;
  $("account-signin").hidden = true;
  $("account-menu").hidden = false;
  $("account-name").textContent = account.username;
  $("account-email").textContent = account.email;
  const on = account.two_factor_enabled;
  $("twofa-state").textContent = on ? "Two-step sign-in is on" : "Two-step sign-in is off";
  $("twofa-submit").textContent = on ? "Turn off two-step sign-in" : "Turn on two-step sign-in";
}

async function loadAccount() {
  let response;
  try {
    response = await fetch(ACCOUNT_URLS.me, { credentials: "same-origin", cache: "no-store" });
  } catch (_) {
    return;
  }
  if (response.ok) showAccount(await response.json());
  // signed out: offer sign-in; any other answer means accounts are off here, so show nothing
  else if (response.status === 401) $("account-signin").hidden = false;
}

async function accountPost(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
  });
}

async function toggleTwoFactor(event) {
  event.preventDefault();
  const password = $("twofa-password").value;
  if (!password || !state.account) {
    $("twofa-status").textContent = "Enter your password to confirm.";
    $("twofa-status").classList.add("is-error");
    return;
  }
  $("twofa-submit").disabled = true;
  try {
    const response = await accountPost(ACCOUNT_URLS.twoFactor, { enabled: !state.account.two_factor_enabled, password });
    $("twofa-password").value = "";
    if (response.ok) {
      showAccount(await response.json());
      $("twofa-status").classList.remove("is-error");
      $("twofa-status").textContent = state.account.two_factor_enabled
        ? "Done. Signing in now also needs a code from your email."
        : "Done. Signing in needs only your password.";
    } else {
      const { message } = await readError(response);
      $("twofa-status").classList.add("is-error");
      $("twofa-status").textContent = message;
    }
  } catch (_) {
    $("twofa-status").classList.add("is-error");
    $("twofa-status").textContent = MESSAGES.network;
  } finally {
    $("twofa-submit").disabled = false;
  }
}

async function signOut() {
  try {
    await accountPost(ACCOUNT_URLS.signout, {});
  } finally {
    // a fresh load shows the signed-out state from the server, nothing is cached here
    window.location.reload();
  }
}

function start() {
  initMap();
  loadAccount();
  $("twofa-form").addEventListener("submit", toggleTwoFactor);
  $("signout").addEventListener("click", signOut);
  syncStepper();
  $("plan-form").addEventListener("submit", submit);
  $("days").addEventListener("change", syncStepper);
  document.querySelectorAll(".stepper-btn").forEach((b) =>
    b.addEventListener("click", () => {
      $("days").value = String(clampDays(Number($("days").value) + Number(b.dataset.step)));
      syncStepper();
    }),
  );
  $("day-tabs").addEventListener("keydown", onTabKey);
  $("back").addEventListener("click", showPlan);
}

document.addEventListener("DOMContentLoaded", start);