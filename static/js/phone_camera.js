(() => {
  "use strict";

  const video = document.getElementById("camVideo");
  const canvas = document.getElementById("captureCanvas");
  const ctx = canvas.getContext("2d");
  const ipBadge = document.getElementById("phoneIpBadge");
  const connPill = document.getElementById("connPill");
  const aiPill = document.getElementById("aiPill");
  const viewfinder = document.getElementById("viewfinder");
  const btnStart = document.getElementById("btnStart");
  const btnStop = document.getElementById("btnStop");

  let ws = null;
  let stream = null;
  let sendTimer = null;
  let wakeLock = null;
  let streaming = false;
  let batteryLevel = null;

  async function checkHttpsRequirement() {
    const isSecure = window.isSecureContext;
    if (!isSecure) {
      try {
        const info = await fetch("/api/system/info").then((r) => r.json());
        if (info.https_enabled) {
          const banner = document.getElementById("httpsBanner");
          const msg = document.getElementById("httpsMsg");
          banner.style.display = "block";
          msg.textContent = `Open ${info.phone_camera_url} so your phone browser permits camera access.`;
          document.getElementById("btnSwitchHttps").onclick = () => {
            location.href = info.phone_camera_url;
          };
        }
      } catch {}
    }
  }

  async function initBattery() {
    if (navigator.getBattery) {
      try {
        const b = await navigator.getBattery();
        batteryLevel = Math.round(b.level * 100);
        b.addEventListener("levelchange", () => {
          batteryLevel = Math.round(b.level * 100);
          if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "battery", level: batteryLevel }));
          }
        });
      } catch {}
    }
  }

  function connectSocket() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/phone-node`);

    ws.onopen = () => {
      connPill.textContent = "Hub Connected";
      ws.send(JSON.stringify({
        type: "hello",
        label: document.getElementById("nodeName").value.trim(),
        battery: batteryLevel,
      }));
    };

    ws.onclose = () => {
      connPill.textContent = "Disconnected";
      connPill.classList.remove("online");
      setTimeout(connectSocket, 2500);
    };

    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.type === "registered") {
        ipBadge.textContent = `IP: ${msg.ip_address}`;
      } else if (msg.type === "start_stream") {
        // Triggered remotely by caregiver pairing this phone by IP!
        if (msg.room) document.getElementById("nodeRoom").value = msg.room;
        if (msg.name) document.getElementById("nodeName").value = msg.name;
        if (!streaming) await startCamera();
      } else if (msg.type === "stop_stream") {
        stopCamera();
      } else if (msg.type === "ai_feedback") {
        const p = String(msg.posture || "MONITORING").toUpperCase();
        if (p === "FALL DETECTED") {
          aiPill.textContent = `🚨 FALL DETECTED (${Math.round(msg.confidence)}%)`;
          aiPill.className = "status-pill fall";
          viewfinder.classList.add("alert-border");
        } else if (p === "LYING") {
          aiPill.textContent = "🟠 LYING — Confirming...";
          aiPill.className = "status-pill";
          viewfinder.classList.add("alert-border");
        } else if (p === "FALLING") {
          aiPill.textContent = "🟡 FALLING — Tracking...";
          aiPill.className = "status-pill";
          viewfinder.classList.remove("alert-border");
        } else if (p === "STANDING") {
          aiPill.textContent = "🟢 STANDING";
          aiPill.className = "status-pill online";
          viewfinder.classList.remove("alert-border");
        } else {
          aiPill.textContent = "AI MONITORING";
          aiPill.className = "status-pill";
          viewfinder.classList.remove("alert-border");
        }
      }
    };
  }

  async function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      alert("Camera API unavailable in this browser context. Please open the HTTPS URL.");
      return;
    }
    const facingMode = document.getElementById("facingSelect").value;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode, width: { ideal: 640 }, height: { ideal: 480 } },
        audio: false,
      });
      video.srcObject = stream;
      await video.play();
    } catch (err) {
      alert(`Could not access phone camera: ${err.message}`);
      return;
    }

    if ("wakeLock" in navigator) {
      try { wakeLock = await navigator.wakeLock.request("screen"); } catch {}
    }

    streaming = true;
    btnStart.disabled = true;
    btnStop.disabled = false;
    connPill.textContent = "Streaming Live";
    connPill.classList.add("online");

    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: "start_camera",
        room: document.getElementById("nodeRoom").value.trim() || "Living Room",
        name: document.getElementById("nodeName").value.trim() || "Phone Camera",
        battery: batteryLevel,
      }));
    }

    const fps = Number(document.getElementById("fpsSelect").value || 6);
    const intervalMs = Math.round(1000 / fps);
    clearInterval(sendTimer);
    sendTimer = setInterval(() => {
      if (!streaming || !ws || ws.readyState !== WebSocket.OPEN || video.videoWidth === 0) return;
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      canvas.toBlob(
        (blob) => {
          if (blob && ws && ws.readyState === WebSocket.OPEN) {
            ws.send(blob);
          }
        },
        "image/jpeg",
        0.72
      );
    }, intervalMs);
  }

  function stopCamera() {
    streaming = false;
    clearInterval(sendTimer);
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    video.srcObject = null;
    if (wakeLock) {
      try { wakeLock.release(); } catch {}
      wakeLock = null;
    }
    btnStart.disabled = false;
    btnStop.disabled = true;
    connPill.textContent = "Standby";
    connPill.classList.remove("online");
    aiPill.textContent = "AI Idle";
    viewfinder.classList.remove("alert-border");
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "stop_camera" }));
    }
  }

  btnStart.onclick = startCamera;
  btnStop.onclick = stopCamera;
  document.getElementById("facingSelect").onchange = async () => {
    if (streaming) {
      stopCamera();
      await startCamera();
    }
  };

  document.addEventListener("DOMContentLoaded", async () => {
    await checkHttpsRequirement();
    await initBattery();
    connectSocket();
  });
})();
