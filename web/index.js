// X2D bridge thin-client (#46) — vanilla JS, no build step.
//
// Subscribes to /state.events (SSE) for live state, hits POST
// /control/<verb> for actions, and toggles between three camera
// transports: snapshot (cam.jpg poll), HLS (<video> with hls.js
// disabled by default — relies on native HLS in Safari), and WebRTC
// (delegates to /cam.webrtc.html in an inline <video> via SDP exchange).
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);

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

  // Boot. ?capture=1 disables SSE + camera polling so headless screenshot
  // tools (chromium-browser --headless --screenshot) don't block on a
  // never-ending page-load.
  const params = new URLSearchParams(location.search);
  if (params.get("capture") === "1") {
    // Inject a one-shot fake state so the UI shows real values in the shot.
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
  } else {
    setCameraMode("snapshot");
    loadPrinters();
  }
})();
