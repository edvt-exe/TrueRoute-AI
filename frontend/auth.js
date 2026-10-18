"use strict";

// mirrors app/auth/password_policy.py and app/auth/schemas.py, the server checks everything again
const MIN_LENGTH = 12;
const MAX_LENGTH = 128;
// lowercase, uppercase, digit and symbol, all four required
const MIN_CLASSES = 4;
const MIN_PERSONAL = 3;
const USERNAME_PATTERN = /^[A-Za-z0-9_]{3,32}$/;
const OTP_PATTERN = /^[0-9]{6}$/;
// planned routes, until they exist every call answers 404 and the page says so
const ENDPOINTS = {
  signin: "/api/auth/signin",
  signup: "/api/auth/signup",
  verify: "/api/auth/otp/verify",
  resend: "/api/auth/otp/resend",
};
const CLIENT_TIMEOUT_MS = 20000;
// the server hands out the public site key, null while the human check is not configured
const CAPTCHA_CONFIG_URL = "/api/auth/captcha";
// fetched from cloudflare's exact url as their docs require, the page csp allows only this origin
const TURNSTILE_ONLOAD = "trueRouteTurnstileReady";
const TURNSTILE_SCRIPT = `https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=${TURNSTILE_ONLOAD}`;
// one action per form, the server refuses a token solved for the other form
const CAPTCHA_ACTIONS = { "signup-captcha": "signup", "signin-captcha": "signin" };

const MESSAGES = {
  invalid_credentials: "Wrong username, email or password.",
  captcha_required: "Complete the human check, then try again.",
  captcha_failed: "The human check failed. Try again.",
  captcha_loading: "The human check is still loading. Wait a moment, then try again.",
  captcha_blocked: "The human check could not load. Check your connection or content blocker, then reload the page.",
  captcha_off: "The human check is not set up on this server yet, so accounts are closed.",
  username_taken: "That username is taken. Pick another one.",
  password_breached: "This password appears in known data breaches. Choose a different one.",
  invalid_code: "That code is wrong or has expired.",
  rate_limited: "Too many attempts. Wait a few minutes and try again.",
  cross_site_request: "This request came from another site and was blocked. Open TrueRoute directly and try again.",
  invalid_request: "Some fields are not valid.",
  not_available: "Accounts are not available yet.",
  network: "Could not reach the server. Check your connection.",
  client_timeout: "The server took too long to answer. Try again.",
  internal_error: "Something went wrong. Try again.",
};

const $ = (id) => document.getElementById(id);
// the challenge id lives in memory only, never in browser storage or the url
const state = { busy: false, challengeId: null, captcha: { status: "loading", siteKey: null, widgets: {} } };

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

function renderRules() {
  const v = signupValues();
  const rules = passwordRules(v.password, v.password_confirm, v.username, v.email);
  for (const item of $("password-rules").querySelectorAll("li[data-rule]")) {
    item.classList.toggle("met", Boolean(rules[item.dataset.rule]));
  }
  return rules;
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

// ui helpers

function setStatus(id, text, isError) {
  const status = $(id);
  status.textContent = text;
  status.classList.toggle("is-error", Boolean(isError));
}

function setBusy(busy) {
  state.busy = busy;
  for (const id of ["signin-submit", "signup-submit", "otp-submit", "otp-resend"]) $(id).disabled = busy;
}

// human check

function setCaptchaNote(slotId, text) {
  const note = document.createElement("p");
  note.className = "hint";
  note.textContent = text;
  $(slotId).classList.remove("is-loaded");
  $(slotId).replaceChildren(note);
}

function renderCaptcha(slotId) {
  const captcha = state.captcha;
  if (captcha.status !== "ready" || slotId in captcha.widgets || $(slotId).hidden) return;
  $(slotId).replaceChildren();
  $(slotId).classList.add("is-loaded");
  captcha.widgets[slotId] = window.turnstile.render(`#${slotId}`, {
    sitekey: captcha.siteKey,
    action: CAPTCHA_ACTIONS[slotId],
    theme: "auto",
  });
}

function captchaFailed(status, message) {
  state.captcha.status = status;
  for (const slotId of Object.keys(CAPTCHA_ACTIONS)) setCaptchaNote(slotId, message);
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

function captchaProblem() {
  return {
    loading: MESSAGES.captcha_loading,
    blocked: MESSAGES.captcha_blocked,
    off: MESSAGES.captcha_off,
  }[state.captcha.status] || MESSAGES.captcha_required;
}

function resetCaptcha(slotId) {
  // cloudflare accepts each token once, every attempt needs a fresh one
  const widget = state.captcha.widgets[slotId];
  if (widget !== undefined) window.turnstile.reset(widget);
}

function selectTab(name) {
  for (const tab of ["signin", "signup"]) {
    const on = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", on ? "true" : "false");
    $(`tab-${tab}`).tabIndex = on ? 0 : -1;
    $(`${tab}-form`).hidden = !on;
  }
  $("auth-tabs").hidden = false;
  $("otp-form").hidden = true;
}

function onTabKey(event) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const next = $("tab-signin").getAttribute("aria-selected") === "true" ? "signup" : "signin";
  selectTab(next);
  $(`tab-${next}`).focus();
}

function showOtp(challengeId, message) {
  state.challengeId = challengeId;
  $("auth-tabs").hidden = true;
  $("signin-form").hidden = true;
  $("signup-form").hidden = true;
  $("otp-form").hidden = false;
  $("otp-message").textContent = message;
  $("otp-code").value = "";
  setStatus("otp-status", "");
  $("otp-code").focus();
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
  const identifier = $("signin-identifier").value.trim();
  const password = $("signin-password").value;
  if (!identifier || !password) {
    setStatus("signin-status", "Enter your username or email and your password.", true);
    return;
  }
  const body = { identifier, password };
  const token = captchaToken("signin-captcha");
  if (token) body.captcha_token = token;
  else if (!$("signin-captcha").hidden) {
    setStatus("signin-status", captchaProblem(), true);
    return;
  }

  setBusy(true);
  setStatus("signin-status", "Signing in…");
  try {
    const result = await postJson(ENDPOINTS.signin, body);
    if (result.ok && result.data && result.data.status === "signed_in") return signedIn();
    if (result.ok && result.data && result.data.status === "otp_required" && validChallenge(result.data)) {
      return showOtp(result.data.challenge_id, "Enter the 6-digit code we emailed you to finish signing in.");
    }
    resetCaptcha("signin-captcha");
    if (errorCode(result) === "captcha_required") {
      $("signin-captcha").hidden = false;
      renderCaptcha("signin-captcha");
    }
    $("signin-password").value = "";
    setStatus("signin-status", errorMessage(result), true);
  } finally {
    setBusy(false);
  }
}

async function onSignUp(event) {
  event.preventDefault();
  if (state.busy) return;
  const values = signupValues();
  const rules = renderRules();
  const problems = [];
  if (!USERNAME_PATTERN.test(values.username)) problems.push("Username must be 3 to 32 letters, digits or underscores.");
  if (!$("signup-email").checkValidity() || !values.email) problems.push("Enter a valid email address.");
  if (!rules.length || !rules.classes || !rules.personal) problems.push("The password does not meet every rule.");
  if (!rules.match) problems.push("The passwords do not match.");
  const token = captchaToken("signup-captcha");
  if (!token) problems.push(captchaProblem());
  if (problems.length) {
    setStatus("signup-status", problems.join(" "), true);
    return;
  }

  setBusy(true);
  setStatus("signup-status", "Creating your account…");
  try {
    const result = await postJson(ENDPOINTS.signup, { ...values, captcha_token: token });
    if (result.ok && validChallenge(result.data)) {
      // the same answer whether or not the email already had an account
      return showOtp(result.data.challenge_id, "If this email can be used, we sent it a 6-digit code. Enter it to confirm your address.");
    }
    resetCaptcha("signup-captcha");
    setStatus("signup-status", errorMessage(result), true);
  } finally {
    setBusy(false);
  }
}

async function onVerify(event) {
  event.preventDefault();
  if (state.busy) return;
  const code = $("otp-code").value.trim();
  if (!OTP_PATTERN.test(code)) {
    setStatus("otp-status", "Enter the 6 digits from the email.", true);
    return;
  }
  setBusy(true);
  setStatus("otp-status", "Checking the code…");
  try {
    const result = await postJson(ENDPOINTS.verify, { challenge_id: state.challengeId, code });
    if (result.ok && result.data && result.data.status === "signed_in") return signedIn();
    $("otp-code").value = "";
    setStatus("otp-status", errorMessage(result), true);
  } finally {
    setBusy(false);
  }
}

async function onResend() {
  if (state.busy || !state.challengeId) return;
  setBusy(true);
  try {
    const result = await postJson(ENDPOINTS.resend, { challenge_id: state.challengeId });
    if (result.ok) setStatus("otp-status", "A new code is on its way. Older codes no longer work.");
    else setStatus("otp-status", errorMessage(result), true);
  } finally {
    setBusy(false);
  }
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

function init() {
  $("tab-signin").addEventListener("click", () => selectTab("signin"));
  $("tab-signup").addEventListener("click", () => selectTab("signup"));
  $("auth-tabs").addEventListener("keydown", onTabKey);
  $("signin-form").addEventListener("submit", onSignIn);
  $("signup-form").addEventListener("submit", onSignUp);
  $("otp-form").addEventListener("submit", onVerify);
  $("otp-resend").addEventListener("click", onResend);
  for (const button of document.querySelectorAll("button[data-reveal]")) button.addEventListener("click", onReveal);
  for (const id of ["signup-username", "signup-email", "signup-password", "signup-confirm"]) {
    $(id).addEventListener("input", renderRules);
  }
  if (window.location.hash === "#signup") selectTab("signup");
  loadCaptcha();
}

document.addEventListener("DOMContentLoaded", init);