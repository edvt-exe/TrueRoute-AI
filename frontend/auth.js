"use strict";

// mirrors app/auth/password_policy.py and app/auth/schemas.py, the server checks everything again
const MIN_LENGTH = 12;
const MAX_LENGTH = 128;
// lowercase, uppercase, digit and symbol, all four required
const MIN_CLASSES = 4;
const MIN_PERSONAL = 3;
const USERNAME_PATTERN = /^[A-Za-z0-9_]{3,32}$/;
const OTP_PATTERN = /^[0-9]{6}$/;
// account routes in app/auth/routes.py
const ENDPOINTS = {
  signin: "/api/auth/signin",
  signup: "/api/auth/signup",
  verify: "/api/auth/otp/verify",
  resend: "/api/auth/otp/resend",
  forgot: "/api/auth/password/forgot",
  reset: "/api/auth/password/reset",
  me: "/api/auth/me",
};
const CLIENT_TIMEOUT_MS = 20000;
// the server hands out the public site key, null while the human check is not configured
const CAPTCHA_CONFIG_URL = "/api/auth/captcha";
// fetched from cloudflare's exact url as their docs require, the page csp allows only this origin
const TURNSTILE_ONLOAD = "trueRouteTurnstileReady";
const TURNSTILE_SCRIPT = `https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=${TURNSTILE_ONLOAD}`;
// one action per form, the server refuses a token solved for another form
const CAPTCHA_ACTIONS = { "signup-captcha": "signup", "signin-captcha": "signin", "forgot-captcha": "reset" };
// every view is a form called "<name>-form"
const VIEWS = ["signin", "signup", "otp", "forgot", "reset"];
const TITLES = {
  signin: "Sign in · TrueRoute",
  signup: "Create account · TrueRoute",
  otp: "Check your email · TrueRoute",
  forgot: "Reset password · TrueRoute",
  reset: "New password · TrueRoute",
};
// these match --collapse-ms and the card height transition in auth.css
const COLLAPSE_MS = 220;
const RESIZE_MS = 300;

const MESSAGES = {
  invalid_credentials: "Wrong username, email or password.",
  captcha_required: "Complete the human check, then try again.",
  captcha_failed: "The human check failed. Try again.",
  captcha_loading: "The human check is still loading. Wait a moment, then try again.",
  captcha_running: "The human check is still running. Wait a moment, then try again.",
  captcha_blocked: "The human check could not load. Check your connection or content blocker, then reload the page.",
  captcha_off: "The human check is not set up on this server yet, so accounts are closed.",
  username_taken: "That username is taken. Pick another one.",
  password_breached: "This password appears in known data breaches. Choose a different one.",
  password_personal: "The password must not contain your username or the start of your email.",
  invalid_code: "That code is wrong or has expired.",
  rate_limited: "Too many attempts. Wait a few minutes and try again.",
  cross_site_request: "This request came from another site and was blocked. Open TrueRoute directly and try again.",
  invalid_request: "Some fields are not valid.",
  not_available: "Accounts are not available yet.",
  accounts_unavailable: "Accounts are not switched on for this server.",
  not_signed_in: "Your session ended. Sign in again.",
  network: "Could not reach the server. Check your connection.",
  client_timeout: "The server took too long to answer. Try again.",
  internal_error: "Something went wrong. Try again.",
};
// a server refusal that belongs to one field is shown under it, the rest go to the status line
const FIELD_FOR_CODE = {
  signup: { username_taken: "signup-username", password_breached: "signup-password" },
  otp: { invalid_code: "otp-code" },
  reset: { invalid_code: "reset-code", password_breached: "reset-password", password_personal: "reset-password" },
};

const $ = (id) => document.getElementById(id);
const motionReduced = window.matchMedia("(prefers-reduced-motion: reduce)");
// the challenge ids live in memory only, never in browser storage or the url
const state = {
  busy: false,
  challengeId: null,
  resetChallengeId: null,
  resizeTimer: 0,
  statusTimers: new Map(),
  // checks: per form, "running" while cloudflare works in the background, "ready" once the token is in, "problem" after an error
  captcha: { status: "loading", siteKey: null, widgets: {}, checks: {} },
};

// password rules

function normalize(value) {
  return value.normalize("NFKC");
}

function charCount(value) {
  // code points, like len() on the server, not utf-16 units
  return [...value].length;
}

function classCount(value) {
  return [/\p{Ll}/u, /\p{Lu}/u, /\p{Nd}/u, /[^\p{L}\p{N}]/u].filter((re) => re.test(value)).length;
}

function passwordRules(password, confirm, username, email) {
  const value = normalize(password);
  const folded = value.toLowerCase();
  const personal = [username, email.split("@")[0]].filter((p) => p.length >= MIN_PERSONAL);
  const filled = value.length > 0;
  return {
    length: charCount(value) >= MIN_LENGTH && charCount(value) <= MAX_LENGTH,
    classes: classCount(value) >= MIN_CLASSES,
    personal: filled && !personal.some((p) => folded.includes(p.toLowerCase())),
    match: filled && password === confirm,
  };
}

function signupValues() {
  return {
    username: $("signup-username").value.trim(),
    email: $("signup-email").value.trim(),
    password: $("signup-password").value,
    password_confirm: $("signup-confirm").value,
  };
}

function resetValues() {
  return {
    code: $("reset-code").value.trim(),
    password: $("reset-password").value,
    password_confirm: $("reset-confirm").value,
  };
}

function paintRules(list, rules) {
  for (const item of list.querySelectorAll("li[data-rule]")) {
    item.classList.toggle("met", Boolean(rules[item.dataset.rule]));
  }
  return rules;
}

function renderRules() {
  const v = signupValues();
  return paintRules($("password-rules"), passwordRules(v.password, v.password_confirm, v.username, v.email));
}

function renderResetRules() {
  // the username rule needs the account, the server applies it once the code is accepted
  const v = resetValues();
  return paintRules($("reset-rules"), passwordRules(v.password, v.password_confirm, "", ""));
}

// network

async function postJson(url, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CLIENT_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      // the session cookie is httponly and only ever sent to this origin
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal: controller.signal,
    });
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      // a proxy error page is not json
    }
    return { ok: response.ok, status: response.status, data, failure: null };
  } catch (err) {
    const failure = err && err.name === "AbortError" ? "client_timeout" : "network";
    return { ok: false, status: 0, data: null, failure };
  } finally {
    clearTimeout(timer);
  }
}

function errorCode(result) {
  if (result.failure) return result.failure;
  if (result.status === 404 || result.status === 405) return "not_available";
  const error = result.data && result.data.error;
  return error && typeof error.code === "string" ? error.code : "internal_error";
}

function errorMessage(result) {
  const code = errorCode(result);
  const message = MESSAGES[code] || MESSAGES.internal_error;
  const detail = result.data && result.data.error && result.data.error.message;
  if (code === "invalid_request" && typeof detail === "string") return `${message} (${detail})`;
  return message;
}

// messages: a status line per form and an error slot per field, both open and close by height

function setStatus(id, text, tone) {
  // tone is "error", "ok", or nothing for a plain progress line
  const status = $(id);
  const slot = status.closest(".collapse");
  clearTimeout(state.statusTimers.get(id));
  if (text) {
    status.textContent = text;
    status.classList.toggle("is-error", tone === "error");
    status.classList.toggle("is-ok", tone === "ok");
    slot.classList.add("is-open");
    return;
  }
  slot.classList.remove("is-open");
  // the text stays while the slot closes, then goes so a reader does not find it again
  state.statusTimers.set(
    id,
    setTimeout(() => {
      status.textContent = "";
      status.classList.remove("is-error", "is-ok");
    }, COLLAPSE_MS),
  );
}

function markInvalid(element, invalid, errorId = "") {
  if (element.classList.contains("captcha")) {
    element.classList.toggle("is-invalid", invalid);
    return;
  }
  // the description the field already had stays, the error id is added next to it
  if (element.dataset.base === undefined) element.dataset.base = element.getAttribute("aria-describedby") || "";
  if (invalid) element.setAttribute("aria-invalid", "true");
  else element.removeAttribute("aria-invalid");
  const described = [element.dataset.base, invalid ? errorId : ""].filter(Boolean).join(" ");
  if (described) element.setAttribute("aria-describedby", described);
  else element.removeAttribute("aria-describedby");
}

function fieldError(fieldId, message) {
  const slot = $(`${fieldId}-error`);
  slot.textContent = message;
  slot.closest(".collapse").classList.add("is-open");
  markInvalid($(fieldId), true, slot.id);
}

function clearField(fieldId) {
  $(`${fieldId}-error`).closest(".collapse").classList.remove("is-open");
  markInvalid($(fieldId), false);
}

function resetMessages(formId) {
  const form = $(formId);
  for (const slot of form.querySelectorAll(".field-error")) clearField(slot.id.slice(0, -"-error".length));
  for (const status of form.querySelectorAll(".status")) setStatus(status.id, "");
}

function hasInvalid(form) {
  return form.querySelector("[aria-invalid='true'], .captcha.is-invalid") !== null;
}

function focusFirstInvalid(form) {
  const field = form.querySelector("input[aria-invalid='true']");
  if (field) field.focus();
}

function showFailure(formName, result) {
  const code = errorCode(result);
  const target = FIELD_FOR_CODE[formName] && FIELD_FOR_CODE[formName][code];
  if (target) {
    fieldError(target, errorMessage(result));
    $(target).focus();
  } else {
    setStatus(`${formName}-status`, errorMessage(result), "error");
  }
}

// buttons: one is busy and shows a spinner, every other action waits

function setLoading(button, loading, text) {
  const label = button.querySelector(".btn-label") || button;
  if (loading) {
    if (button.dataset.idle === undefined) button.dataset.idle = label.textContent;
    label.textContent = text;
  } else if (button.dataset.idle !== undefined) {
    label.textContent = button.dataset.idle;
    delete button.dataset.idle;
  }
  button.classList.toggle("is-loading", loading);
  button.setAttribute("aria-busy", String(loading));
}

// a control waits while an action is busy, and a submit button also while its human check is still running
function syncControls() {
  for (const control of document.querySelectorAll(".auth-submit, [data-locks]")) {
    const slotId = control.id.replace(/-submit$/, "-captcha");
    control.disabled = state.busy || state.captcha.checks[slotId] === "running";
  }
}

function setBusy(busy, button = null, text = "") {
  state.busy = busy;
  syncControls();
  if (button) setLoading(button, busy, text);
}

// human check

function setCaptchaNote(slotId, text) {
  const note = document.createElement("p");
  note.className = "captcha-note";
  note.textContent = text;
  $(slotId).classList.remove("is-loaded");
  $(slotId).replaceChildren(note);
}

function setCheck(slotId, value) {
  state.captcha.checks[slotId] = value;
  syncControls();
}

// the label and frame show only while the visitor has something to click, the message row below them always works
function showCaptchaField(slotId, shown) {
  const field = $(slotId).closest(".captcha-field");
  field.classList.toggle("is-quiet", !shown);
  for (const part of [field.querySelector(".auth-label"), $(slotId)]) part.inert = !shown;
}

// a notice asking for the check is stale once the check has passed
function dropCheckNotice(slotId) {
  const notices = [MESSAGES.captcha_required, MESSAGES.captcha_running, MESSAGES.captcha_failed];
  const statusId = slotId.replace("-captcha", "-status");
  if (notices.includes($(statusId).textContent)) setStatus(statusId, "");
  if (notices.includes($(`${slotId}-error`).textContent)) clearField(slotId);
}

function renderCaptcha(slotId) {
  const captcha = state.captcha;
  // a widget drawn inside a hidden view would be sized wrong, it renders when the view shows
  if (captcha.status !== "ready" || slotId in captcha.widgets || $(slotId).hidden || $(slotId).closest("form").hidden) return;
  $(slotId).replaceChildren();
  $(slotId).classList.add("is-loaded");
  setCheck(slotId, "running");
  captcha.widgets[slotId] = window.turnstile.render(`#${slotId}`, {
    sitekey: captcha.siteKey,
    action: CAPTCHA_ACTIONS[slotId],
    theme: "dark",
    // fills the card width like the inputs around it
    size: "flexible",
    // hidden unless cloudflare needs a click, and never in the browser's language
    appearance: "interaction-only",
    language: "en",
    callback: () => {
      setCheck(slotId, "ready");
      dropCheckNotice(slotId);
    },
    "before-interactive-callback": () => showCaptchaField(slotId, true),
    "after-interactive-callback": () => showCaptchaField(slotId, false),
    // an expired or timed-out token is replaced by a fresh background check
    "expired-callback": () => setCheck(slotId, "running"),
    "timeout-callback": () => setCheck(slotId, "running"),
    "error-callback": () => {
      showCaptchaField(slotId, false);
      setCheck(slotId, "problem");
    },
  });
}

function captchaFailed(status, message) {
  state.captcha.status = status;
  for (const slotId of Object.keys(CAPTCHA_ACTIONS)) {
    setCaptchaNote(slotId, message);
    showCaptchaField(slotId, true);
  }
}

async function loadCaptcha() {
  let siteKey;
  try {
    const response = await fetch(CAPTCHA_CONFIG_URL, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("config");
    siteKey = (await response.json()).site_key;
  } catch (_) {
    return captchaFailed("blocked", MESSAGES.captcha_blocked);
  }
  if (typeof siteKey !== "string" || !siteKey) return captchaFailed("off", MESSAGES.captcha_off);
  state.captcha.siteKey = siteKey;
  // turnstile calls this by name once its script has run
  window[TURNSTILE_ONLOAD] = () => {
    state.captcha.status = "ready";
    for (const slotId of Object.keys(CAPTCHA_ACTIONS)) renderCaptcha(slotId);
  };
  const script = document.createElement("script");
  script.src = TURNSTILE_SCRIPT;
  script.async = true;
  script.addEventListener("error", () => captchaFailed("blocked", MESSAGES.captcha_blocked));
  document.head.append(script);
}

function captchaToken(slotId) {
  const widget = state.captcha.widgets[slotId];
  return widget === undefined ? null : window.turnstile.getResponse(widget) || null;
}

function captchaProblem(slotId) {
  const early = { loading: MESSAGES.captcha_loading, blocked: MESSAGES.captcha_blocked, off: MESSAGES.captcha_off }[state.captcha.status];
  if (early) return early;
  const check = state.captcha.checks[slotId];
  if (check === "running") return MESSAGES.captcha_running;
  if (check === "problem") {
    // the next attempt starts a fresh check
    resetCaptcha(slotId);
    return MESSAGES.captcha_failed;
  }
  return MESSAGES.captcha_required;
}

function resetCaptcha(slotId) {
  // cloudflare accepts each token once, every attempt needs a fresh one
  const widget = state.captcha.widgets[slotId];
  if (widget === undefined) return;
  showCaptchaField(slotId, false);
  setCheck(slotId, "running");
  window.turnstile.reset(widget);
}

// views

function swapHeight(change) {
  // the card eases from the old height to the new one instead of jumping
  const card = $("auth-card");
  const from = card.offsetHeight;
  clearTimeout(state.resizeTimer);
  card.style.height = "";
  card.classList.remove("is-resizing");
  change();
  const to = card.offsetHeight;
  if (motionReduced.matches || from === to) return;
  card.style.height = `${from}px`;
  card.classList.add("is-resizing");
  // reading the height commits the start value before the end value is set
  void card.offsetHeight;
  card.style.height = `${to}px`;
  state.resizeTimer = setTimeout(() => {
    card.style.height = "";
    card.classList.remove("is-resizing");
  }, RESIZE_MS + 40);
}

function showView(name, focusId = `${name}-title`) {
  const leaving = VIEWS.filter((view) => view !== name && !$(`${view}-form`).hidden);
  swapHeight(() => {
    for (const view of VIEWS) $(`${view}-form`).hidden = view !== name;
  });
  // what a view said stays behind with it, it is not waiting when the person comes back
  for (const view of leaving) resetMessages(`${view}-form`);
  document.title = TITLES[name];
  // the new view's heading or first field takes focus, so screen readers announce the switch
  $(focusId).focus();
  if (`${name}-captcha` in CAPTCHA_ACTIONS) renderCaptcha(`${name}-captcha`);
}

function showOtp(challengeId, message) {
  state.challengeId = challengeId;
  $("otp-message").textContent = message;
  $("otp-code").value = "";
  showView("otp", "otp-code");
}

function signedIn() {
  // a full page load, so the planner reads the new session from the server
  window.location.assign("/");
}

function validChallenge(data) {
  return data && typeof data.challenge_id === "string" && data.challenge_id.length > 0;
}

// flows

async function onSignIn(event) {
  event.preventDefault();
  if (state.busy) return;
  const form = $("signin-form");
  resetMessages("signin-form");
  const identifier = $("signin-identifier").value.trim();
  const password = $("signin-password").value;
  if (!identifier) fieldError("signin-identifier", "Enter your username or email.");
  if (!password) fieldError("signin-password", "Enter your password.");
  const body = { identifier, password };
  const token = captchaToken("signin-captcha");
  if (token) body.captcha_token = token;
  else if (!$("signin-captcha").hidden) fieldError("signin-captcha", captchaProblem("signin-captcha"));
  if (hasInvalid(form)) return focusFirstInvalid(form);

  setBusy(true, $("signin-submit"), "Signing in…");
  let leaving = false;
  try {
    const result = await postJson(ENDPOINTS.signin, body);
    if (result.ok && result.data && result.data.status === "signed_in") {
      leaving = true;
      return signedIn();
    }
    if (result.ok && result.data && result.data.status === "otp_required" && validChallenge(result.data)) {
      return showOtp(result.data.challenge_id, "Enter the 6-digit code we emailed you to finish signing in.");
    }
    resetCaptcha("signin-captcha");
    const code = errorCode(result);
    if (code === "captcha_required") {
      $("signin-captcha").hidden = false;
      renderCaptcha("signin-captcha");
    }
    if (code === "invalid_credentials") {
      // the answer never says which of the two was wrong, so both fields are marked
      markInvalid($("signin-identifier"), true);
      markInvalid($("signin-password"), true);
    }
    $("signin-password").value = "";
    setStatus("signin-status", errorMessage(result), "error");
    if (code === "invalid_credentials") $("signin-password").focus();
  } finally {
    if (!leaving) setBusy(false, $("signin-submit"));
  }
}

async function onSignUp(event) {
  event.preventDefault();
  if (state.busy) return;
  const form = $("signup-form");
  resetMessages("signup-form");
  const values = signupValues();
  const rules = renderRules();
  if (!USERNAME_PATTERN.test(values.username)) fieldError("signup-username", "Use 3 to 32 letters, digits or underscores.");
  if (!$("signup-email").checkValidity() || !values.email) fieldError("signup-email", "Enter a valid email address.");
  if (!rules.length || !rules.classes || !rules.personal) fieldError("signup-password", "The password does not meet every rule.");
  if (!rules.match) fieldError("signup-confirm", "The passwords do not match.");
  const token = captchaToken("signup-captcha");
  if (!token) fieldError("signup-captcha", captchaProblem("signup-captcha"));
  if (hasInvalid(form)) return focusFirstInvalid(form);

  setBusy(true, $("signup-submit"), "Creating account…");
  try {
    const result = await postJson(ENDPOINTS.signup, { ...values, captcha_token: token });
    if (result.ok && validChallenge(result.data)) {
      // the same answer whether or not the email already had an account
      return showOtp(result.data.challenge_id, "If this email can be used, we sent it a 6-digit code. Enter it to confirm your address.");
    }
    resetCaptcha("signup-captcha");
    showFailure("signup", result);
  } finally {
    setBusy(false, $("signup-submit"));
  }
}

async function onVerify(event) {
  event.preventDefault();
  if (state.busy) return;
  resetMessages("otp-form");
  const code = $("otp-code").value.trim();
  if (!OTP_PATTERN.test(code)) {
    fieldError("otp-code", "Enter the 6 digits from the email.");
    $("otp-code").focus();
    return;
  }
  setBusy(true, $("otp-submit"), "Checking…");
  let leaving = false;
  try {
    const result = await postJson(ENDPOINTS.verify, { challenge_id: state.challengeId, code });
    if (result.ok && result.data && result.data.status === "signed_in") {
      leaving = true;
      return signedIn();
    }
    $("otp-code").value = "";
    showFailure("otp", result);
  } finally {
    if (!leaving) setBusy(false, $("otp-submit"));
  }
}

async function onResend() {
  if (state.busy || !state.challengeId) return;
  resetMessages("otp-form");
  setBusy(true, $("otp-resend"), "Sending…");
  try {
    const result = await postJson(ENDPOINTS.resend, { challenge_id: state.challengeId });
    if (result.ok && validChallenge(result.data)) {
      // the new code belongs to a new challenge, the old one is retired
      state.challengeId = result.data.challenge_id;
      $("otp-code").value = "";
      setStatus("otp-status", "A new code is on its way. Older codes no longer work.", "ok");
    } else {
      setStatus("otp-status", errorMessage(result), "error");
    }
  } finally {
    setBusy(false, $("otp-resend"));
  }
}

async function onForgot(event) {
  event.preventDefault();
  if (state.busy) return;
  const form = $("forgot-form");
  resetMessages("forgot-form");
  const email = $("forgot-email").value.trim();
  if (!email || !$("forgot-email").checkValidity()) fieldError("forgot-email", "Enter the email address of your account.");
  const token = captchaToken("forgot-captcha");
  if (!token) fieldError("forgot-captcha", captchaProblem("forgot-captcha"));
  if (hasInvalid(form)) return focusFirstInvalid(form);

  setBusy(true, $("forgot-submit"), "Sending code…");
  try {
    const result = await postJson(ENDPOINTS.forgot, { email, captcha_token: token });
    // the token is spent either way
    resetCaptcha("forgot-captcha");
    if (result.ok && validChallenge(result.data)) {
      // the same answer whether or not the address has an account
      state.resetChallengeId = result.data.challenge_id;
      for (const id of ["reset-code", "reset-password", "reset-confirm"]) $(id).value = "";
      renderResetRules();
      return showView("reset", "reset-code");
    }
    setStatus("forgot-status", errorMessage(result), "error");
  } finally {
    setBusy(false, $("forgot-submit"));
  }
}

async function onReset(event) {
  event.preventDefault();
  if (state.busy) return;
  const form = $("reset-form");
  resetMessages("reset-form");
  const values = resetValues();
  const rules = renderResetRules();
  if (!OTP_PATTERN.test(values.code)) fieldError("reset-code", "Enter the 6 digits from the email.");
  if (!rules.length || !rules.classes) fieldError("reset-password", "The password does not meet every rule.");
  if (!rules.match) fieldError("reset-confirm", "The passwords do not match.");
  if (hasInvalid(form)) return focusFirstInvalid(form);

  setBusy(true, $("reset-submit"), "Changing password…");
  try {
    const result = await postJson(ENDPOINTS.reset, { challenge_id: state.resetChallengeId, ...values });
    if (result.ok && result.data && result.data.status === "password_changed") {
      state.resetChallengeId = null;
      for (const id of ["reset-code", "reset-password", "reset-confirm"]) $(id).value = "";
      renderResetRules();
      // nobody is signed in by a reset, the person signs in with the new password
      $("signin-password").value = "";
      showView("signin", "signin-identifier");
      setStatus("signin-status", "Your password was changed and every device was signed out. Sign in with the new one.", "ok");
      return;
    }
    if (errorCode(result) === "invalid_code") $("reset-code").value = "";
    showFailure("reset", result);
  } finally {
    setBusy(false, $("reset-submit"));
  }
}

function onRestart() {
  if (state.busy) return;
  state.resetChallengeId = null;
  showView("forgot", "forgot-email");
}

function onReveal(event) {
  const button = event.currentTarget;
  const input = $(button.dataset.reveal);
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  button.setAttribute("aria-pressed", show ? "true" : "false");
  button.setAttribute("aria-label", show ? "Hide password" : "Show password");
  button.textContent = show ? "Hide" : "Show";
}

async function leaveIfSignedIn() {
  // already signed in, nothing to do on this page
  try {
    const response = await fetch(ENDPOINTS.me, { credentials: "same-origin", cache: "no-store" });
    if (response.ok) window.location.replace("/");
  } catch (_) {
    // offline: stay and let the forms explain
  }
}

function routeFromHash() {
  const view = { "#signup": "signup", "#forgot": "forgot" }[window.location.hash];
  if (view) showView(view);
}

function init() {
  for (const button of document.querySelectorAll("button[data-view]")) {
    button.addEventListener("click", () => showView(button.dataset.view));
  }
  $("signin-form").addEventListener("submit", onSignIn);
  $("signup-form").addEventListener("submit", onSignUp);
  $("otp-form").addEventListener("submit", onVerify);
  $("forgot-form").addEventListener("submit", onForgot);
  $("reset-form").addEventListener("submit", onReset);
  $("otp-resend").addEventListener("click", onResend);
  $("reset-restart").addEventListener("click", onRestart);
  for (const button of document.querySelectorAll("button[data-reveal]")) button.addEventListener("click", onReveal);
  for (const id of ["signup-username", "signup-email", "signup-password", "signup-confirm"]) {
    $(id).addEventListener("input", renderRules);
  }
  for (const id of ["reset-password", "reset-confirm"]) $(id).addEventListener("input", renderResetRules);
  // typing in a field that was refused takes its error away
  $("auth-card").addEventListener("input", (event) => {
    const field = event.target;
    if (field instanceof HTMLInputElement && field.getAttribute("aria-invalid") === "true") clearField(field.id);
  });
  routeFromHash();
  // a link to #signup or #forgot also works while the page is already open
  window.addEventListener("hashchange", routeFromHash);
  loadCaptcha();
  leaveIfSignedIn();
}

document.addEventListener("DOMContentLoaded", init);