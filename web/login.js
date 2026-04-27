// X2D bridge — login page (#48).
// POSTs the entered token to /auth/check (which uses the same
// _check_bearer path as every other route). On 200, persists the
// token both as a `x2d_token` cookie (for SSE / EventSource which
// can't send custom headers) and in localStorage (so the JS layer
// can attach Authorization: Bearer headers to fetch() too). Then
// redirects to /index.html. On 401, shows the error inline.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const form  = $("login-form");
  const tokIn = $("token");
  const msg   = $("msg");
  const sub   = $("submit");
  const clear = $("clear");

  // If auth is not required (loopback + no --auth-token), skip the
  // whole flow.
  fetch("/auth/info").then((r) => r.json()).then((info) => {
    if (info && info.auth_required === false) {
      msg.textContent = "Daemon has no auth — redirecting…";
      msg.className = "login-msg ok";
      setTimeout(() => { location.href = "/index.html"; }, 400);
    }
  }).catch(() => { /* non-fatal */ });

  // Pre-populate from stored token if present, so refreshing the
  // login page after a redirect-back-from-failure doesn't lose it.
  try {
    const stored = localStorage.getItem("x2d_token");
    if (stored) tokIn.value = stored;
  } catch (e) { /* ignore */ }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const token = tokIn.value.trim();
    if (!token) return;
    sub.disabled = true;
    msg.textContent = "Signing in…";
    msg.className = "login-msg";
    try {
      const r = await fetch("/auth/check", {
        method: "GET",
        headers: { "Authorization": "Bearer " + token },
      });
      if (r.status !== 200) {
        msg.textContent = "Token rejected (HTTP " + r.status + ")";
        msg.className = "login-msg bad";
        return;
      }
      // Persist for SSE (cookie) + fetch (localStorage).
      try { localStorage.setItem("x2d_token", token); } catch (e) {}
      // SameSite=Strict + path=/ so the cookie is sent on every same-
      // origin request including the SSE EventSource. Max-Age=30d.
      const secure = location.protocol === "https:" ? "; Secure" : "";
      document.cookie =
        "x2d_token=" + encodeURIComponent(token) +
        "; path=/; SameSite=Strict; Max-Age=2592000" + secure;
      msg.textContent = "Signed in. Redirecting…";
      msg.className = "login-msg ok";
      // Honour ?next=... if present so deep-linked URLs round-trip
      // through the gate cleanly.
      const next = new URLSearchParams(location.search).get("next");
      const target = (next && next.startsWith("/")) ? next : "/index.html";
      setTimeout(() => { location.href = target; }, 250);
    } catch (e) {
      msg.textContent = "Network error: " + e.message;
      msg.className = "login-msg bad";
    } finally {
      sub.disabled = false;
    }
  });

  clear.addEventListener("click", () => {
    try { localStorage.removeItem("x2d_token"); } catch (e) {}
    document.cookie = "x2d_token=; path=/; Max-Age=0; SameSite=Strict";
    tokIn.value = "";
    msg.textContent = "Cleared.";
    msg.className = "login-msg";
  });
})();
