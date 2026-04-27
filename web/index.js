// X2D bridge thin-client (#46 + #48) — vanilla JS, no build step.
//
// Subscribes to /state.events (SSE) for live state, hits POST
// /control/<verb> for actions, and toggles between three camera
// transports: snapshot (cam.jpg poll), HLS (<video> with hls.js
// disabled by default — relies on native HLS in Safari), and WebRTC
// (delegates to /cam.webrtc.html in an inline <video> via SDP exchange).
//
// Auth (#48): on boot, hit /auth/info. If `auth_required` is true and
// no token is in localStorage, redirect to /login.html?next=<here>.
// Otherwise wrap fetch() to add `Authorization: Bearer <token>` so
// every API call carries auth. EventSource picks up the same token
// via the `x2d_token` cookie set by the login page (EventSource has
// no API for custom headers — cookies are the only path).
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);

  // Token is whatever the login page persisted; null when no auth
  // is configured at the daemon side (single-user loopback).
  let _token = null;
  try { _token = localStorage.getItem("x2d_token"); } catch (e) {}

  // Wrap window.fetch so every code path under here automatically
  // attaches the Bearer header. We keep the original around so the
  // login page (loaded separately) is unaffected.
  const _origFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    init = init || {};
    if (_token) {
      const h = new Headers(init.headers || {});
      if (!h.has("Authorization")) h.set("Authorization", "Bearer " + _token);
      init.headers = h;
    }
    return _origFetch(input, init);
  };

  function redirectToLogin() {
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = "/login.html?next=" + next;
  }

  // Probe the daemon's auth state. If the daemon requires auth and we
  // don't have a token, kick the user to /login.html before booting
  // the rest of the UI. Use the unwrapped fetch so /auth/info isn't
  // gated by our own missing token.
  _origFetch("/auth/info").then((r) => r.ok ? r.json() : null)
    .then(async (info) => {
      if (info && info.auth_required) {
        if (!_token) { redirectToLogin(); return; }
        // We have a token; verify it before launching SSE so a stale
        // token doesn't quietly fail. /auth/check returns 200 on
        // valid bearer/cookie auth, 401 otherwise.
        const r = await fetch("/auth/check");
        if (r.status === 401) {
          try { localStorage.removeItem("x2d_token"); } catch (e) {}
          document.cookie = "x2d_token=; path=/; Max-Age=0; SameSite=Strict";
          redirectToLogin();
          return;
        }
      }
      boot();
    })
    .catch(() => {
      // Daemon unreachable — show what we can; the SSE stream will
      // surface the connect failure.
      boot();
    });

  function boot() {
    // Queue card init (#55)
    initQueue();
    // ?capture=1 disables SSE + camera polling so headless screenshot
    // tools (chromium-browser --headless --screenshot) don't block on
    // a never-ending page-load. Inject a one-shot fake state so the
    // UI shows real values in the shot.
    const params = new URLSearchParams(location.search);
    if (params.get("capture") === "1") {
      renderState({
        print: {
          nozzle_temper: 213.5, bed_temper: 60.0, chamber_temper: 35.0,
          subtask_name: "rumi_frame.gcode.3mf", mc_percent: 42,
          mc_current_layer: 17, total_layer_num: 120,
          mc_remaining_time: 75,
          ams: { ams: [{ id: 0, tray: [
            { tray_color: "FF7676FF", tray_type: "PLA" },
            { tray_color: "66E08CFF", tray_type: "PETG" },
            { tray_color: "FFC857FF", tray_type: "PLA" },
            {} ] }], tray_now: "0" },
        },
      });
      connStatus.textContent = "capture mode";
      connStatus.className = "pill ok";
      lastUpdate.textContent = new Date().toLocaleTimeString();
      return;
    }
    setCameraMode("snapshot");
    loadPrinters();
  }

  // --- header ----------------------------------------------------------
  const printerSelect = $("printer-select");
  const connStatus    = $("conn-status");
  const lastUpdate    = $("last-update");
  const printerName   = $("printer-name");

  const log = (() => {
    const el = $("log");
    const lines = [];
    return (msg) => {
      const ts = new Date().toLocaleTimeString();
      lines.push(`${ts} ${msg}`);
      while (lines.length > 60) lines.shift();
      el.textContent = lines.join("\n");
      el.scrollTop = el.scrollHeight;
    };
  })();

  // --- printer discovery ----------------------------------------------
  let activePrinter = "";
  let evtSrc = null;

  async function loadPrinters() {
    try {
      const r = await fetch("/printers");
      if (!r.ok) throw new Error("status " + r.status);
      const { printers } = await r.json();
      if (!printers || printers.length === 0) return;
      printerSelect.innerHTML = "";
      printers.forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name || "(default)";
        printerSelect.appendChild(opt);
      });
      printerSelect.hidden = printers.length < 2;
      activePrinter = printers[0];
      printerName.textContent = activePrinter || "x2d";
      printerSelect.value = activePrinter;
      printerSelect.addEventListener("change", () => {
        activePrinter = printerSelect.value;
        printerName.textContent = activePrinter || "x2d";
        connectSSE();
      });
      connectSSE();
    } catch (e) {
      log("printer list failed: " + e.message);
      // Fall back to default printer.
      activePrinter = "";
      connectSSE();
    }
  }

  // --- SSE state stream ------------------------------------------------
  function connectSSE() {
    if (evtSrc) { evtSrc.close(); evtSrc = null; }
    const url = `/state.events?printer=${encodeURIComponent(activePrinter)}`;
    evtSrc = new EventSource(url);
    evtSrc.addEventListener("open", () => {
      connStatus.textContent = "connected";
      connStatus.className = "pill ok";
      log("SSE open");
    });
    evtSrc.addEventListener("error", () => {
      connStatus.textContent = "reconnecting";
      connStatus.className = "pill bad";
    });
    evtSrc.addEventListener("message", (e) => {
      try {
        const { state, ts } = JSON.parse(e.data);
        renderState(state || {});
        lastUpdate.textContent = new Date(ts * 1000).toLocaleTimeString();
      } catch (err) {
        log("bad SSE frame: " + err.message);
      }
    });
  }

  // --- state → UI -----------------------------------------------------
  function renderState(state) {
    const print = state.print || {};
    const setT = (id, v, unit) =>
      $(id).textContent = (v == null ? "—" : v.toFixed(0) + unit);
    setT("temp-nozzle",  print.nozzle_temper,  "°");
    setT("temp-bed",     print.bed_temper,     "°");
    setT("temp-chamber", print.chamber_temper, "°");

    const filename = print.subtask_name || print.gcode_file || "";
    const stage    = print.gcode_state || print.mc_print_sub_stage || "";
    const pct      = Number(print.mc_percent || 0);
    $("job-name").textContent = filename || (stage ? stage : "no print");
    $("job-progress").textContent = filename ? `${pct}%` : "—";
    $("job-bar").style.width = (filename ? pct : 0) + "%";
    const cur = print.mc_current_layer ?? "—";
    const tot = print.total_layer_num ?? print.layer_num ?? "—";
    $("job-layer").textContent = `${cur}/${tot}`;
    const eta = print.mc_remaining_time;
    $("job-eta").textContent = eta != null
      ? formatEta(eta)
      : "—";

    renderAms(print.ams || {});
  }

  function formatEta(minutes) {
    minutes = Math.max(0, Math.round(minutes));
    if (minutes < 60) return `${minutes} m`;
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    return `${h} h ${m} m`;
  }

  function renderAms(amsData) {
    const grid = $("ams-slots");
    const amsList = amsData.ams || [];
    const activeTrayId = (amsData.tray_now ?? "");
    grid.innerHTML = "";
    if (!amsList.length) {
      grid.innerHTML = '<div class="muted small">no AMS detected</div>';
      return;
    }
    amsList.forEach((ams) => {
      const trays = ams.tray || [];
      trays.forEach((tray, idx) => {
        const slotIdx = (Number(ams.id) * 4) + idx + 1;  // 1-indexed
        const div = document.createElement("div");
        const empty = !tray || !tray.tray_color;
        div.className = "swatch" + (empty ? " empty" : "");
        if (!empty) {
          div.style.background = "#" + tray.tray_color.slice(0, 6);
          div.title = `${tray.tray_type} ${tray.tray_sub_brands || ""}`.trim();
        }
        if (tray && tray.id != null && String(tray.id) === activeTrayId) {
          div.classList.add("active");
        }
        div.textContent = "" + slotIdx;
        div.addEventListener("click", () => loadSlot(slotIdx));
        grid.appendChild(div);
      });
    });
  }

  // --- control actions -------------------------------------------------
  async function control(verb, body) {
    const url = `/control/${verb}?printer=${encodeURIComponent(activePrinter)}`;
    try {
      const r = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : "{}",
      });
      const data = await r.json().catch(() => ({}));
      log(`${verb} → ${r.status} ${data.ok ? "ok" : (data.error || "")}`);
    } catch (e) {
      log(`${verb} failed: ${e.message}`);
    }
  }

  $("btn-pause").addEventListener("click",  () => control("pause"));
  $("btn-resume").addEventListener("click", () => control("resume"));
  $("btn-stop").addEventListener("click",   () => {
    if (confirm("Abort the current print?")) control("stop");
  });
  document.querySelectorAll("[data-light]").forEach((b) =>
    b.addEventListener("click", () =>
      control("light", { state: b.dataset.light })));
  document.querySelectorAll("[data-heat]").forEach((b) =>
    b.addEventListener("click", () => {
      const preset = b.dataset.heat;
      if (preset === "pla")  { control("temp", { target: "nozzle", value: 215 });
                                control("temp", { target: "bed",    value: 60 }); }
      else if (preset === "petg") { control("temp", { target: "nozzle", value: 240 });
                                     control("temp", { target: "bed",    value: 80 }); }
      else if (preset === "cool") { control("temp", { target: "nozzle", value: 0 });
                                     control("temp", { target: "bed",    value: 0 }); }
    }));
  function loadSlot(slot) {
    if (!confirm(`Load filament from AMS slot ${slot}?`)) return;
    control("ams_load", { slot });
  }

  // --- camera transports -----------------------------------------------
  const camImg   = $("cam-img");
  const camVideo = $("cam-video");
  let snapTimer  = null;
  let webrtcPC   = null;

  function setCameraMode(mode) {
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t.dataset.mode === mode));
    if (snapTimer) { clearInterval(snapTimer); snapTimer = null; }
    if (webrtcPC)  { try { webrtcPC.close(); } catch (e) {} webrtcPC = null; }
    camImg.hidden = false;
    camVideo.hidden = true;
    camVideo.removeAttribute("src");
    camVideo.srcObject = null;
    if (mode === "snapshot") {
      const tick = () => {
        camImg.src = "/cam.jpg?t=" + Date.now();
      };
      tick();
      snapTimer = setInterval(tick, 1000);
    } else if (mode === "hls") {
      camImg.hidden = true;
      camVideo.hidden = false;
      // Native HLS playback (Safari, recent Chrome on iOS).
      camVideo.src = "/cam.m3u8";
    } else if (mode === "webrtc") {
      camImg.hidden = true;
      camVideo.hidden = false;
      startWebrtc().catch((e) => log("webrtc: " + e.message));
    }
  }
  document.querySelectorAll(".tab").forEach((t) =>
    t.addEventListener("click", () => setCameraMode(t.dataset.mode)));

  async function startWebrtc() {
    webrtcPC = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });
    webrtcPC.addTransceiver("video", { direction: "recvonly" });
    webrtcPC.addEventListener("track", (ev) => {
      if (ev.streams && ev.streams[0]) camVideo.srcObject = ev.streams[0];
    });
    const offer = await webrtcPC.createOffer();
    await webrtcPC.setLocalDescription(offer);
    while (webrtcPC.iceGatheringState !== "complete") {
      await new Promise((r) => setTimeout(r, 100));
    }
    const local = webrtcPC.localDescription;
    const r = await fetch("/cam.webrtc/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: local.sdp, type: local.type }),
    });
    if (!r.ok) throw new Error("offer rejected: " + r.status);
    const answer = await r.json();
    await webrtcPC.setRemoteDescription(answer);
  }

  // ===== queue card (#55) =====================================
  const $qList    = document.getElementById("queue-list");
  const $qGcode   = document.getElementById("queue-gcode");
  const $qPrinter = document.getElementById("queue-printer");
  const $qSlot    = document.getElementById("queue-slot");
  const $qAddBtn  = document.getElementById("queue-add-btn");
  let _qDragId    = null;

  function initQueue() {
    if ($qAddBtn) {
      $qAddBtn.addEventListener("click", queueAdd);
      pollQueue();
      setInterval(pollQueue, 3000);
    }
  }

  async function pollQueue() {
    try {
      const r = await fetch("/queue");
      if (!r.ok) return;
      const data = await r.json();
      renderQueue(data.jobs || []);
    } catch (e) { /* ignore */ }
  }

  function renderQueue(jobs) {
    const printerOpts = Array.from($qPrinter.options).map(o => o.value);
    const known = Array.from(new Set(
      jobs.map(j => j.printer).concat([activePrinter])
        .filter(p => p !== undefined)));
    if (known.length && JSON.stringify(known) !== JSON.stringify(printerOpts)) {
      $qPrinter.innerHTML = "";
      known.forEach(p => {
        const o = document.createElement("option");
        o.value = p; o.textContent = p || "(default)";
        $qPrinter.appendChild(o);
      });
    }
    $qList.innerHTML = "";
    jobs.forEach(j => $qList.appendChild(renderQueueRow(j)));
  }

  function renderQueueRow(job) {
    const li = document.createElement("li");
    li.className = job.status;
    li.draggable = (job.status === "pending");
    li.dataset.id = job.id;
    li.innerHTML =
      `<span class="pill">${job.printer || "default"}</span>` +
      `<span class="label" title="${escapeAttr(job.gcode)}">${escapeText(job.label || job.gcode)}</span>` +
      `<span class="pill">slot ${job.slot}</span>` +
      `<span class="pill">${job.status}</span>` +
      `<button class="cancel" type="button" title="cancel">×</button>`;
    li.querySelector("button.cancel").addEventListener("click", () => queueCancel(job.id));
    li.addEventListener("dragstart", (e) => {
      _qDragId = job.id; e.dataTransfer.effectAllowed = "move"; });
    li.addEventListener("dragover",  (e) => { e.preventDefault(); li.classList.add("over"); });
    li.addEventListener("dragleave", () => { li.classList.remove("over"); });
    li.addEventListener("drop", async (e) => {
      e.preventDefault();
      li.classList.remove("over");
      if (!_qDragId || _qDragId === job.id) return;
      const lis = Array.from($qList.children);
      const target = lis.find(el => el.dataset.id === job.id);
      const pending = lis.filter(el => el.classList.contains("pending"));
      const newPos = pending.indexOf(target);
      await fetch("/queue/move", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: _qDragId, dest_printer: job.printer,
                                position: Math.max(0, newPos) }),
      });
      _qDragId = null;
      pollQueue();
    });
    return li;
  }

  async function queueAdd() {
    const gcode = $qGcode.value.trim();
    if (!gcode) return;
    const printer = $qPrinter.value || "";
    const slot    = parseInt($qSlot.value, 10) || 1;
    const label   = gcode.split("/").pop();
    const r = await fetch("/queue/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gcode, printer, slot, label }),
    });
    if (r.ok) { $qGcode.value = ""; pollQueue(); }
    else { log("queue add failed: HTTP " + r.status); }
  }

  async function queueCancel(id) {
    await fetch("/queue/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    pollQueue();
  }

  function escapeText(s) {
    const d = document.createElement("div");
    d.textContent = s; return d.innerHTML;
  }
  function escapeAttr(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[c]));
  }
})();
