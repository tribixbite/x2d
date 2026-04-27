// X2D chamber-camera WebRTC viewer.
// Issues a single SDP offer, receives the answer, and binds the
// returned MediaStream to the <video> element. Stats poll every 1s
// updates the bitrate / fps / RTT pills in the footer.

(function () {
  "use strict";

  const $status     = document.getElementById("status");
  const $connect    = document.getElementById("connect");
  const $disconnect = document.getElementById("disconnect");
  const $video      = document.getElementById("video");
  const $fps        = document.getElementById("fps");
  const $rtt        = document.getElementById("rtt");
  const $bitrate    = document.getElementById("bitrate");

  let pc = null;
  let statsTimer = null;
  let lastBytes = 0;
  let lastFrames = 0;
  let lastStatsTs = 0;

  function setStatus(text, cls) {
    $status.textContent = text;
    $status.className = "status " + (cls || "");
  }

  async function connect() {
    if (pc) return;
    setStatus("connecting…");
    $connect.disabled = true;
    $disconnect.disabled = false;

    pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    pc.addTransceiver("video", { direction: "recvonly" });

    pc.addEventListener("connectionstatechange", () => {
      const s = pc.connectionState;
      if (s === "connected") setStatus("connected", "ok");
      else if (s === "failed" || s === "disconnected" || s === "closed") {
        setStatus(s, "bad");
        teardown();
      } else {
        setStatus(s);
      }
    });

    pc.addEventListener("track", (ev) => {
      if (ev.streams && ev.streams[0]) {
        $video.srcObject = ev.streams[0];
      }
    });

    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      // Wait for ICE gathering to complete so the SDP includes all
      // candidates (Trickle ICE works too but the Python side here
      // does non-trickle for simplicity).
      await new Promise((resolve) => {
        if (pc.iceGatheringState === "complete") return resolve();
        const check = () => {
          if (pc.iceGatheringState === "complete") {
            pc.removeEventListener("icegatheringstatechange", check);
            resolve();
          }
        };
        pc.addEventListener("icegatheringstatechange", check);
        setTimeout(resolve, 2500); // safety cap
      });
      const local = pc.localDescription;
      const resp = await fetch("/cam.webrtc/offer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: local.sdp, type: local.type }),
      });
      if (!resp.ok) throw new Error("server: " + resp.status);
      const answer = await resp.json();
      await pc.setRemoteDescription(answer);
      startStats();
    } catch (e) {
      console.error(e);
      setStatus("error: " + e.message, "bad");
      teardown();
    }
  }

  function startStats() {
    stopStats();
    statsTimer = setInterval(async () => {
      if (!pc) return;
      try {
        const stats = await pc.getStats();
        let bytes = 0, frames = 0, rtt = null;
        stats.forEach((r) => {
          if (r.type === "inbound-rtp" && r.kind === "video") {
            bytes = r.bytesReceived || 0;
            frames = r.framesDecoded || 0;
          } else if (r.type === "candidate-pair" && r.state === "succeeded") {
            if (typeof r.currentRoundTripTime === "number") {
              rtt = r.currentRoundTripTime;
            }
          }
        });
        const now = performance.now();
        if (lastStatsTs > 0) {
          const dt = (now - lastStatsTs) / 1000;
          if (dt > 0) {
            const kbps = (((bytes - lastBytes) * 8) / dt / 1000).toFixed(0);
            const fps = ((frames - lastFrames) / dt).toFixed(1);
            $bitrate.textContent = kbps + " kbps";
            $fps.textContent = fps + " fps";
          }
        }
        lastBytes = bytes;
        lastFrames = frames;
        lastStatsTs = now;
        if (rtt !== null) {
          $rtt.textContent = "RTT " + (rtt * 1000).toFixed(0) + " ms";
        }
      } catch (e) {
        // ignore
      }
    }, 1000);
  }

  function stopStats() {
    if (statsTimer) {
      clearInterval(statsTimer);
      statsTimer = null;
    }
  }

  function teardown() {
    stopStats();
    if (pc) {
      try { pc.close(); } catch (e) {}
      pc = null;
    }
    $video.srcObject = null;
    $connect.disabled = false;
    $disconnect.disabled = true;
  }

  $connect.addEventListener("click", connect);
  $disconnect.addEventListener("click", teardown);

  // Auto-connect when the page is opened directly.
  window.addEventListener("DOMContentLoaded", connect);
})();
