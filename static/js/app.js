(() => {
  "use strict";

  const state = {
    system: {},
    caregiver: {},
    resident: {},
    contacts: [],
    cameras: [],
    standbyNodes: [],
    events: [],
    settings: {},
    activeTab: "home",
    cameraViewSize: "large",
    historyType: "all",
    activeAlertEvent: null,
    detailEvent: null,
    expandingCameraId: null,
    editingCamera: null,
    editingRoi: { x: 0, y: 0, w: 1, h: 1 },
    ws: null,
    swRegistration: null,
    notificationPermission: "default",
    dismissedNotificationBanner: false,
  };

  const $ = (id) => document.getElementById(id);

  function showToast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 3200);
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (!res.ok) {
      let msg = `Request failed (${res.status})`;
      try {
        const j = await res.json();
        if (j.error) msg = j.error;
      } catch {}
      throw new Error(msg);
    }
    return res.json();
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatTime(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch {
      return iso;
    }
  }

  let alarmInterval = null;

  function playAlertTone() {
    if (state.settings.alert_sound === false) return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [880, 660, 880].forEach((freq, idx) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.18, ctx.currentTime + idx * 0.18);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + idx * 0.18 + 0.16);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(ctx.currentTime + idx * 0.18);
        osc.stop(ctx.currentTime + idx * 0.18 + 0.16);
      });
    } catch {}
  }

  function startEmergencyAlarm() {
    if ($("setEmergencySiren") && !$("setEmergencySiren").checked) return;
    if (state.settings.alert_sound === false) return;
    playAlertTone();
    stopEmergencyAlarm();
    alarmInterval = setInterval(() => {
      if (!$("fallAlertModal") || !$("fallAlertModal").classList.contains("open")) {
        stopEmergencyAlarm();
      } else {
        playAlertTone();
      }
    }, 2400);
  }

  function stopEmergencyAlarm() {
    if (alarmInterval) {
      clearInterval(alarmInterval);
      alarmInterval = null;
    }
  }

  // ---------- System & Lockscreen Notifications ----------

  async function triggerSystemNotification(evt) {
    if ($("setLockscreenNotify") && !$("setLockscreenNotify").checked) return;
    if (!("Notification" in window)) return;
    if (Notification.permission !== "granted") return;

    const resName = (state.resident && state.resident.name) || "Resident";
    const room = evt.room || (evt.camera_name ? evt.camera_name.split(" ")[0] : "Room");
    const conf = Math.round((evt.confidence || 0.95) * 100);
    const title = evt.notification_title || `🚨 SafeWatch Fall Alert: ${resName}!`;
    const body = evt.notification_body || `CRITICAL: Sudden fall detected in ${room} (${conf}% confidence). Tap to view live camera & alert emergency contacts immediately.`;
    const snapshotUrl = evt.photo_url || "/static/icon-512.png";
    const absoluteSnapshot = snapshotUrl.startsWith("http") ? snapshotUrl : `${location.origin}${snapshotUrl}`;

    // Intense vibration pattern for smartphones in sleep mode or pocket
    const vibratePattern = ($("setVibrationPattern") && !$("setVibrationPattern").checked)
      ? [200, 100, 200]
      : [500, 200, 500, 200, 500, 300, 1000];

    if ("vibrate" in navigator) {
      try { navigator.vibrate(vibratePattern); } catch {}
    }

    const notifOptions = {
      body,
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      image: absoluteSnapshot,
      tag: `safewatch-fall-${evt.id}`,
      renotify: true,
      requireInteraction: true, // Remains on lockscreen until caregiver swipes or taps
      vibrate: vibratePattern,
      data: {
        url: `/#event-${evt.id}`,
        event_id: evt.id,
        call_phone: (state.resident && state.resident.phone) || "",
      },
      actions: [
        { action: "view", title: "🚨 View Situation" },
        { action: "call", title: "📞 Call Emergency" },
      ],
    };

    // 1. Service Worker showNotification (supports lock screen notifications on mobile & OS level)
    if (state.swRegistration && typeof state.swRegistration.showNotification === "function") {
      try {
        await state.swRegistration.showNotification(title, notifOptions);
        return;
      } catch (err) {
        console.warn("ServiceWorker showNotification error, falling back:", err);
      }
    }

    // 2. Direct Web Notification fallback (desktop browsers)
    try {
      const n = new Notification(title, notifOptions);
      n.onclick = () => {
        window.focus();
        showFallAlertModal(evt);
        n.close();
      };
    } catch (err) {
      console.warn("Notification constructor error:", err);
    }
  }

  async function initServiceWorker() {
    if (!("serviceWorker" in navigator)) return;
    try {
      const reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
      state.swRegistration = reg;

      navigator.serviceWorker.addEventListener("message", (ev) => {
        if (ev.data && ev.data.type === "FOCUS_ALERT" && ev.data.event_id) {
          const found = state.events.find((e) => e.id === ev.data.event_id);
          if (found) showFallAlertModal(found);
        }
      });

      if ("Notification" in window && Notification.permission === "granted") {
        await registerBackendSubscription(reg);
      }
    } catch (err) {
      console.warn("ServiceWorker registration note:", err);
    }
  }

  async function registerBackendSubscription(reg) {
    try {
      let sub = null;
      if (reg && reg.pushManager) {
        sub = await reg.pushManager.getSubscription();
      }
      const endpoint = sub ? sub.endpoint : `local-${navigator.userAgent.slice(0, 32)}`;
      let p256dh = "";
      let auth = "";
      if (sub && sub.toJSON) {
        const j = sub.toJSON();
        p256dh = (j.keys && j.keys.p256dh) || "";
        auth = (j.keys && j.keys.auth) || "";
      }
      await api("/api/notifications/subscribe", {
        method: "POST",
        body: JSON.stringify({
          endpoint,
          keys: { p256dh, auth },
          device_name: navigator.userAgent.includes("Mobile") ? "Caregiver Smartphone" : "Caregiver PC / Desktop",
        }),
      });
    } catch (e) {
      console.warn("Backend notification registration note:", e);
    }
  }

  function updateNotificationUi() {
    if (!("Notification" in window)) {
      if ($("notifStatusPill")) {
        $("notifStatusPill").textContent = "Not Supported";
        $("notifStatusPill").className = "pill pill-denied";
      }
      if ($("btnHeaderNotify")) $("btnHeaderNotify").style.display = "none";
      if ($("notificationBanner")) $("notificationBanner").style.display = "none";
      return;
    }

    const perm = Notification.permission;
    state.notificationPermission = perm;

    const pill = $("notifStatusPill");
    const banner = $("notificationBanner");
    const headerBtn = $("btnHeaderNotify");
    const enableBtn = $("btnEnableNotifications");

    if (perm === "granted") {
      if (pill) {
        pill.textContent = "🔔 Granted (Active)";
        pill.className = "pill pill-granted";
      }
      if (banner) banner.style.display = "none";
      if (headerBtn) {
        headerBtn.textContent = "🔔 Alerts Active";
        headerBtn.classList.remove("btn-outline");
        headerBtn.classList.add("btn-secondary");
      }
      if (enableBtn) {
        enableBtn.textContent = "✅ Lock Screen Alerts Enabled";
        enableBtn.disabled = true;
      }
    } else if (perm === "denied") {
      if (pill) {
        pill.textContent = "❌ Blocked in Browser";
        pill.className = "pill pill-denied";
      }
      if (banner) banner.style.display = "none";
      if (headerBtn) {
        headerBtn.textContent = "🔕 Blocked";
        headerBtn.classList.add("btn-outline");
      }
      if (enableBtn) {
        enableBtn.textContent = "⚠️ Permission Blocked";
        enableBtn.disabled = true;
      }
    } else {
      if (pill) {
        pill.textContent = "⚠️ Permission Required";
        pill.className = "pill pill-default";
      }
      if (banner && !state.dismissedNotificationBanner) {
        banner.style.display = "flex";
      }
      if (headerBtn) {
        headerBtn.textContent = "🔔 Enable Alerts";
        headerBtn.classList.add("btn-outline");
      }
      if (enableBtn) {
        enableBtn.textContent = "🔔 Enable Lock Screen Alerts";
        enableBtn.disabled = false;
      }
    }
  }

  async function requestNotificationPermission() {
    if (!("Notification" in window)) {
      showToast("Web Notifications are not supported in this browser.");
      return;
    }
    try {
      const res = await Notification.requestPermission();
      updateNotificationUi();
      if (res === "granted") {
        showToast("Notifications enabled! You will receive lock screen alerts even during sleep mode.");
        if (state.swRegistration) {
          await registerBackendSubscription(state.swRegistration);
        }
      } else if (res === "denied") {
        showToast("Notification permission was denied. Please allow notifications in site settings.");
      }
    } catch (e) {
      showToast(`Permission error: ${e.message}`);
    }
  }

  // ---------- Tab Navigation & Modals ----------

  function switchTab(tab) {
    state.activeTab = tab;
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    const panel = $(`tab-${tab}`);
    if (panel) panel.classList.add("active");
    document.querySelectorAll(".nav-tab, .bottom-nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === tab);
    });
  }

  function openModal(id) {
    $(id).classList.add("open");
  }

  function closeModal(id) {
    $(id).classList.remove("open");
    if (id === "fallAlertModal") {
      stopEmergencyAlarm();
    }
  }

  // ---------- Rendering ----------

  function renderAll() {
    renderHeaderAndHero();
    renderCameraGrids();
    renderStandbyNodes();
    renderEvents();
    renderSettingsForms();
    renderContacts();
  }

  function renderHeaderAndHero() {
    const pendingFalls = state.events.filter(
      (e) => e.status === "pending" && (e.event_type === "fall" || e.event_type === "inactivity")
    );
    const badge = $("bellBadge");
    if (pendingFalls.length > 0) {
      badge.style.display = "grid";
      badge.textContent = String(pendingFalls.length);
    } else {
      badge.style.display = "none";
    }

    const resName = state.resident.name || "Resident";
    const resPhone = state.resident.phone || "";
    $("qaCallLabel").textContent = `Call ${resName}`;
    $("qaCallResident").href = resPhone ? `tel:${resPhone}` : "#";
    $("brandSubtitle").textContent = `Monitoring ${resName} · ${state.cameras.length} phone(s)`;

    const onlineCams = state.cameras.filter((c) => c.status === "online");
    const hero = $("heroCard");
    const heroActions = $("heroActions");
    hero.className = "hero-card";

    if (pendingFalls.length > 0) {
      const latest = pendingFalls[0];
      hero.classList.add("alert");
      $("heroEmoji").textContent = "🚨";
      $("heroTitle").textContent = `${latest.event_type === "fall" ? "Fall Detected" : "Inactivity Alert"} — ${latest.room}`;
      $("heroSub").textContent = `${latest.camera_name} (${latest.phone_ip}) · ${formatTime(latest.created_at)} · ${Math.round(latest.confidence)}% confidence`;
      heroActions.innerHTML = `
        <button class="btn btn-white" id="heroViewAlertBtn">View Photo / Video</button>
        <a class="btn btn-outline" style="background:rgba(255,255,255,0.16);color:#fff;border-color:rgba(255,255,255,0.5)" href="${resPhone ? `tel:${escapeHtml(resPhone)}` : "#"}">📞 Call ${escapeHtml(resName)}</a>
      `;
      $("heroViewAlertBtn").onclick = () => showFallAlertModal(latest);
    } else if (state.resident.master_paused) {
      hero.classList.add("warn");
      $("heroEmoji").textContent = "⏸️";
      $("heroTitle").textContent = "Privacy Mode Active — All Cameras Paused";
      $("heroSub").textContent = "Monitoring is paused by caregiver/resident agreement.";
      heroActions.innerHTML = `<button class="btn btn-white" id="heroResumeAllBtn">Resume Monitoring</button>`;
      $("heroResumeAllBtn").onclick = () => toggleMasterPause(false);
    } else if (state.cameras.length === 0) {
      hero.classList.add("warn");
      $("heroEmoji").textContent = "📱";
      $("heroTitle").textContent = "No Phone Cameras Connected Yet";
      $("heroSub").textContent = "Connect one or more phones via IP address or scan the QR code to start AI fall monitoring.";
      heroActions.innerHTML = `<button class="btn btn-white" id="heroAddPhoneBtn">+ Connect Phone IP</button>`;
      $("heroAddPhoneBtn").onclick = () => openConnectModal();
    } else {
      hero.classList.add("clear");
      $("heroEmoji").textContent = "✅";
      $("heroTitle").textContent = `${resName} is Safe — All Clear`;
      const rooms = state.cameras.map((c) => c.room).join(", ");
      $("heroSub").textContent = `${onlineCams.length}/${state.cameras.length} phone camera(s) online · ${rooms}`;
      heroActions.innerHTML = `<button class="btn btn-white" id="heroAddMoreBtn">+ Add Another Phone</button>`;
      $("heroAddMoreBtn").onclick = () => openConnectModal();
    }
  }

  function cameraCardHtml(cam) {
    const statusPillClass =
      cam.status === "online"
        ? "pill-online"
        : cam.status === "paused"
        ? "pill-paused"
        : cam.status === "connecting"
        ? "pill-connecting"
        : "pill-offline";

    const posture = String(cam.live_posture || "MONITORING").toUpperCase();
    const postureBadge =
      posture === "FALL DETECTED"
        ? `<span class="pill pill-soft-red">🚨 FALL DETECTED</span>`
        : posture === "LYING"
        ? `<span class="pill pill-soft-orange">🟠 LYING</span>`
        : posture === "FALLING"
        ? `<span class="pill pill-soft-amber">⚠️ FALLING</span>`
        : posture === "STANDING"
        ? `<span class="pill pill-soft-green">🟢 STANDING</span>`
        : `<span class="pill pill-soft-teal">🤖 ${escapeHtml(posture)}</span>`;

    const batteryTxt = cam.battery_level != null ? ` · 🔋 ${cam.battery_level}%` : "";
    const fpsTxt = cam.fps ? ` · ${cam.fps} fps` : "";
    const angleTxt = cam.live_torso_angle ? ` · Torso ${cam.live_torso_angle}°` : "";

    return `
      <div class="cam-card" data-cam-id="${escapeHtml(cam.id)}">
        <div class="cam-feed-wrap" data-expand-cam="${escapeHtml(cam.id)}">
          <img src="/api/cameras/${escapeHtml(cam.id)}/stream" alt="${escapeHtml(cam.name)}" />
          <div class="cam-overlay-top">
            <span class="pill ${statusPillClass}">● ${escapeHtml(cam.status.toUpperCase())}</span>
            <span class="pill pill-ip">${escapeHtml(cam.ip_address)}${cam.port ? `:${cam.port}` : ""}</span>
          </div>
          <button class="cam-expand-fab" type="button" data-cam-action="expand" data-id="${escapeHtml(cam.id)}">⛶ Larger View</button>
        </div>
        <div class="cam-body">
          <div class="cam-title-row">
            <div>
              <div class="cam-name">${escapeHtml(cam.name)}</div>
              <div class="cam-meta">📍 ${escapeHtml(cam.room)} · ${escapeHtml(cam.source_type)}${fpsTxt}${angleTxt}${batteryTxt}</div>
            </div>
            ${postureBadge}
          </div>
          <div class="cam-actions">
            <button class="btn btn-outline btn-sm" data-cam-action="expand" data-id="${escapeHtml(cam.id)}">⛶ Expand</button>
            <button class="btn btn-outline btn-sm" data-cam-action="test" data-id="${escapeHtml(cam.id)}">🧪 Test Fall</button>
            <button class="btn btn-outline btn-sm" data-cam-action="pause" data-id="${escapeHtml(cam.id)}">${cam.paused ? "▶️ Resume" : "⏸️ Pause"}</button>
            <button class="btn btn-secondary btn-sm" data-cam-action="config" data-id="${escapeHtml(cam.id)}">⚙️ Configure</button>
          </div>
        </div>
      </div>
    `;
  }

  function applyCameraGridSize() {
    ["homeCameraGrid", "camerasFullGrid"].forEach((gid) => {
      const el = $(gid);
      if (!el) return;
      el.classList.remove("size-cinema", "size-compact");
      if (state.cameraViewSize === "cinema") el.classList.add("size-cinema");
      else if (state.cameraViewSize === "compact") el.classList.add("size-compact");
    });
    document.querySelectorAll("[data-cam-size]").forEach((b) => {
      b.classList.toggle("active", b.dataset.camSize === state.cameraViewSize);
    });
  }

  function renderCameraGrids() {
    $("camCountLabel").textContent = `${state.cameras.length} phone camera(s) configured`;
    const emptyHtml = `
      <div class="card empty-state" style="grid-column:1/-1;">
        <div style="font-size:32px; margin-bottom:8px;">📱</div>
        <strong>No phone cameras connected</strong>
        <p class="muted" style="margin:6px 0 14px;">Connect phones by entering their IP address (IP Webcam / DroidCam) or opening the browser camera link on each phone.</p>
        <button class="btn btn-primary btn-sm" onclick="document.getElementById('btnQuickConnect').click()">+ Connect Phone by IP</button>
      </div>
    `;

    const html = state.cameras.length ? state.cameras.map(cameraCardHtml).join("") : emptyHtml;
    $("homeCameraGrid").innerHTML = html;
    $("camerasFullGrid").innerHTML = html;
    applyCameraGridSize();

    // History camera filter options
    const sel = $("historyCameraFilter");
    const currVal = sel.value;
    sel.innerHTML = `<option value="all">All Phone Cameras</option>` +
      state.cameras.map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.name)} (${escapeHtml(c.ip_address)})</option>`).join("");
    sel.value = currVal || "all";

    document.querySelectorAll("[data-expand-cam]").forEach((wrap) => {
      wrap.onclick = (e) => {
        if (e.target.closest("button")) return;
        const cam = state.cameras.find((c) => c.id === wrap.dataset.expandCam);
        if (cam) openExpandCamera(cam);
      };
    });

    document.querySelectorAll("[data-cam-action]").forEach((btn) => {
      btn.onclick = async () => {
        const id = btn.dataset.id;
        const action = btn.dataset.camAction;
        const cam = state.cameras.find((c) => c.id === id);
        if (!cam) return;
        if (action === "expand") {
          openExpandCamera(cam);
        } else if (action === "test") {
          btn.disabled = true;
          try {
            await api(`/api/cameras/${id}/test-fall`, { method: "POST" });
            showToast(`Test fall triggered for ${cam.name}`);
          } catch (e) {
            showToast(e.message);
          } finally {
            btn.disabled = false;
          }
        } else if (action === "pause") {
          await api(`/api/cameras/${id}`, {
            method: "PUT",
            body: JSON.stringify({ paused: !cam.paused }),
          });
          await refreshState();
        } else if (action === "config") {
          openCameraConfig(cam);
        }
      };
    });
  }

  function renderStandbyNodes() {
    const sec = $("standbyNodesSection");
    const list = $("standbyNodesList");
    const unpaired = state.standbyNodes.filter((n) => !n.camera_id);
    if (!unpaired.length) {
      sec.style.display = "none";
      list.innerHTML = "";
      return;
    }
    sec.style.display = "block";
    list.innerHTML = unpaired.map((n) => `
      <div class="event-item" style="cursor:default;">
        <div class="quick-icon">📱</div>
        <div class="event-info">
          <div class="event-title">${escapeHtml(n.device_label)} <span class="pill pill-ip">${escapeHtml(n.ip_address)}</span></div>
          <div class="event-sub">Connected at ${formatTime(n.connected_at)} ${n.battery_level != null ? `· Battery ${n.battery_level}%` : ""}</div>
        </div>
        <button class="btn btn-primary btn-sm" data-pair-ip="${escapeHtml(n.ip_address)}">Pair Phone</button>
      </div>
    `).join("");
    list.querySelectorAll("[data-pair-ip]").forEach((btn) => {
      btn.onclick = () => {
        openConnectModal("ip");
        $("ipInput").value = btn.dataset.pairIp;
        $("presetInput").value = "phone_node";
      };
    });
  }

  function eventRowHtml(evt) {
    const icon = evt.event_type === "fall" ? "🔴" : evt.event_type === "inactivity" ? "🟡" : "🟢";
    const statusPill =
      evt.status === "pending"
        ? `<span class="pill pill-soft-red">Pending</span>`
        : evt.status === "false_alert"
        ? `<span class="pill pill-soft-amber">False Alert</span>`
        : `<span class="pill pill-soft-green">Resolved</span>`;

    const thumb = evt.photo_url
      ? `<img class="event-thumb" src="${escapeHtml(evt.photo_url)}" alt="Incident thumbnail" />`
      : `<div class="event-thumb" style="display:grid;place-items:center;color:#fff;">📷</div>`;

    return `
      <div class="event-item" data-event-id="${escapeHtml(evt.id)}">
        ${thumb}
        <div class="event-info">
          <div class="event-title">
            <span>${icon} ${escapeHtml(evt.event_type.replace("_", " ").toUpperCase())}</span>
            ${statusPill}
            ${evt.video_url ? `<span class="pill pill-soft-teal">🎬 Video</span>` : ""}
          </div>
          <div class="event-sub">
            ${escapeHtml(evt.room)} (${escapeHtml(evt.phone_ip || "IP")}) · ${formatTime(evt.created_at)} · ${Math.round(evt.confidence)}% AI
          </div>
        </div>
        <span class="muted">›</span>
      </div>
    `;
  }

  function renderEvents() {
    const homeList = $("homeEventList");
    const recent = state.events.slice(0, 6);
    homeList.innerHTML = recent.length
      ? recent.map(eventRowHtml).join("")
      : `<div class="empty-state">No events recorded yet. Connected phone cameras are actively monitoring.</div>`;

    const statusFilter = $("historyStatusFilter").value;
    const camFilter = $("historyCameraFilter").value;
    const filtered = state.events.filter((e) => {
      if (state.historyType !== "all" && e.event_type !== state.historyType) return false;
      if (statusFilter !== "all" && e.status !== statusFilter) return false;
      if (camFilter !== "all" && e.camera_id !== camFilter) return false;
      return true;
    });

    const histList = $("historyEventList");
    histList.innerHTML = filtered.length
      ? filtered.map(eventRowHtml).join("")
      : `<div class="empty-state">No matching events found.</div>`;

    document.querySelectorAll("[data-event-id]").forEach((el) => {
      el.onclick = () => {
        const evt = state.events.find((x) => x.id === el.dataset.eventId);
        if (evt) openEventDetail(evt);
      };
    });
  }

  function renderSettingsForms() {
    $("cgName").value = state.caregiver.full_name || "";
    $("cgEmail").value = state.caregiver.email || "";
    $("cgPhone").value = state.caregiver.phone || "";

    $("resName").value = state.resident.name || "";
    $("resRelation").value = state.resident.relationship || "Parent";
    $("resPhone").value = state.resident.phone || "";
    $("resAddress").value = state.resident.address || "";
    $("resMedical").value = state.resident.medical_notes || "";

    $("masterPauseToggle").checked = Boolean(state.resident.master_paused);
    const badge = $("consentBadge");
    if (state.resident.consent_agreed) {
      badge.className = "pill pill-soft-green";
      badge.textContent = "Consent Active";
      $("consentMeta").textContent = `Confirmed on ${formatTime(state.resident.consent_timestamp)} · Rooms: ${(state.resident.consent_rooms || []).join(", ")}`;
    } else {
      badge.className = "pill pill-soft-amber";
      badge.textContent = "Consent Pending";
      $("consentMeta").textContent = "Resident has not confirmed informed consent yet.";
    }

    $("setAlertSound").checked = state.settings.alert_sound !== false;
    $("setSkeletonOverlay").checked = state.settings.show_skeleton_overlay !== false;
    $("setAutoDelete").checked = state.settings.auto_delete !== false;
    $("setRetentionDays").value = String(state.settings.retention_days || 30);
    $("setCooldown").value = String(state.settings.cooldown_seconds || 15);
  }

  function renderContacts() {
    const el = $("contactsList");
    if (!state.contacts.length) {
      el.innerHTML = `<div class="empty-state">No emergency contacts added yet.</div>`;
      return;
    }
    el.innerHTML = state.contacts.map((c) => `
      <div class="event-item" style="cursor:default;">
        <div class="quick-icon">👤</div>
        <div class="event-info">
          <div class="event-title">${escapeHtml(c.name)} <span class="pill pill-soft-teal">${escapeHtml(c.relationship)}</span></div>
          <div class="event-sub">📞 ${escapeHtml(c.phone)} · Priority #${c.priority}</div>
        </div>
        <a class="btn btn-outline btn-sm" href="tel:${escapeHtml(c.phone)}">Call</a>
        <button class="btn btn-ghost btn-sm" data-del-contact="${escapeHtml(c.id)}">🗑️</button>
      </div>
    `).join("");
    el.querySelectorAll("[data-del-contact]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/api/contacts/${btn.dataset.delContact}`, { method: "DELETE" });
        await refreshState();
        showToast("Contact removed");
      };
    });
  }

  // ---------- Modals: Fall Alert & Event Detail ----------

  function showFallAlertModal(evt) {
    state.activeAlertEvent = evt;
    $("alertModalSub").textContent = `${evt.camera_name} (${evt.phone_ip}) · ${evt.room} · ${formatTime(evt.created_at)}`;

    const mediaBox = $("alertMediaContainer");
    if (evt.video_url) {
      mediaBox.innerHTML = `<video class="media-preview" src="${escapeHtml(evt.video_url)}" controls autoplay muted loop playsinline poster="${escapeHtml(evt.photo_url || "")}"></video>`;
    } else if (evt.photo_url) {
      mediaBox.innerHTML = `<img class="media-preview" src="${escapeHtml(evt.photo_url)}" alt="Fall incident photo" />`;
    } else {
      mediaBox.innerHTML = `<div class="empty-state">Capturing visual evidence...</div>`;
    }

    $("alertMetaPills").innerHTML = `
      <span class="pill pill-soft-red">AI Confidence: ${Math.round(evt.confidence)}%</span>
      <span class="pill pill-soft-teal">📍 ${escapeHtml(evt.room)}</span>
      <span class="pill pill-ip">IP: ${escapeHtml(evt.phone_ip)}</span>
      ${evt.duration_sec ? `<span class="pill pill-soft-amber">⏱️ ${evt.duration_sec}s Clip</span>` : ""}
    `;

    const resPhone = state.resident.phone || "";
    const callBtn = $("alertBtnCallResident");
    callBtn.href = resPhone ? `tel:${resPhone}` : "#";
    callBtn.onclick = async () => {
      await api(`/api/events/${evt.id}/logs`, {
        method: "POST",
        body: JSON.stringify({ action: `Caregiver called ${state.resident.name || "resident"}` }),
      });
      await refreshState();
    };
    $("alertBtnCall999").onclick = async () => {
      await api(`/api/events/${evt.id}/logs`, {
        method: "POST",
        body: JSON.stringify({ action: "Caregiver initiated Emergency (999) call" }),
      });
      await refreshState();
    };
    $("alertBtnSafe").onclick = () => resolveEvent(evt.id, "resolved", "Marked as safe by caregiver");
    $("alertBtnFalse").onclick = () => resolveEvent(evt.id, "false_alert", "Marked as false alert by caregiver");

    openModal("fallAlertModal");
    startEmergencyAlarm();
  }

  async function resolveEvent(eventId, status, logAction) {
    await api(`/api/events/${eventId}`, {
      method: "PUT",
      body: JSON.stringify({ status, log_action: logAction }),
    });
    closeModal("fallAlertModal");
    await refreshState();
    showToast(status === "resolved" ? "Alert marked as safe" : "Marked as false alert");
  }

  function openEventDetail(evt) {
    state.detailEvent = evt;
    $("detailTitle").textContent = `${evt.event_type.replace("_", " ").toUpperCase()} — ${evt.room}`;
    $("detailSub").textContent = `${evt.camera_name} (${evt.phone_ip}) · ${formatTime(evt.created_at)}`;

    const showMedia = (mode) => {
      $("btnShowPhoto").classList.toggle("active", mode === "photo");
      $("btnShowVideo").classList.toggle("active", mode === "video");
      if (mode === "video" && evt.video_url) {
        $("detailMediaBox").innerHTML = `<video class="media-preview" src="${escapeHtml(evt.video_url)}" controls autoplay playsinline></video>`;
      } else if (evt.photo_url) {
        $("detailMediaBox").innerHTML = `<img class="media-preview" src="${escapeHtml(evt.photo_url)}" alt="Incident photo" />`;
      } else {
        $("detailMediaBox").innerHTML = `<div class="empty-state">No media saved for this event.</div>`;
      }
    };

    $("btnShowVideo").style.display = evt.video_url ? "inline-flex" : "none";
    $("btnShowPhoto").onclick = () => showMedia("photo");
    $("btnShowVideo").onclick = () => showMedia("video");
    showMedia(evt.video_url ? "video" : "photo");

    $("detailDownloadPhoto").style.display = evt.photo_url ? "inline-flex" : "none";
    if (evt.photo_url) $("detailDownloadPhoto").href = `${evt.photo_url}?download=1`;
    $("detailDownloadVideo").style.display = evt.video_url ? "inline-flex" : "none";
    if (evt.video_url) $("detailDownloadVideo").href = `${evt.video_url}?download=1`;

    $("detailMetaGrid").innerHTML = `
      <span class="pill pill-soft-teal">AI Confidence: ${Math.round(evt.confidence)}%</span>
      <span class="pill pill-ip">Phone IP: ${escapeHtml(evt.phone_ip)}</span>
      <span class="pill pill-soft-amber">Status: ${escapeHtml(evt.status)}</span>
      ${evt.duration_sec ? `<span class="pill pill-soft-green">Clip: ${evt.duration_sec}s</span>` : ""}
    `;

    const logs = evt.response_logs || [];
    $("detailTimeline").innerHTML = logs.map((lg) => `
      <div class="timeline-item">
        <strong>${escapeHtml(lg.actor)}</strong> — ${escapeHtml(lg.action)}
        <div class="muted">${formatTime(lg.created_at)}</div>
      </div>
    `).join("");

    $("btnAddEventLog").onclick = async () => {
      const text = $("detailNoteInput").value.trim();
      if (!text) return;
      const updated = await api(`/api/events/${evt.id}/logs`, {
        method: "POST",
        body: JSON.stringify({ action: text }),
      });
      $("detailNoteInput").value = "";
      await refreshState();
      openEventDetail(updated);
    };
    $("detailResolveBtn").onclick = async () => {
      await resolveEvent(evt.id, "resolved", "Marked resolved in detail view");
      closeModal("eventDetailModal");
    };
    $("detailFalseBtn").onclick = async () => {
      await resolveEvent(evt.id, "false_alert", "Marked false alert in detail view");
      closeModal("eventDetailModal");
    };
    $("detailDeleteBtn").onclick = async () => {
      await api(`/api/events/${evt.id}`, { method: "DELETE" });
      closeModal("eventDetailModal");
      await refreshState();
      showToast("Event deleted");
    };

    openModal("eventDetailModal");
  }

  // ---------- Modals: Connect Phone, Expand View & Camera Config ----------

  function updateExpandCameraTelemetry(cam) {
    if (!cam) return;
    $("expandCamTitle").textContent = `${cam.name} — Large Live View`;
    $("expandCamSub").textContent = `Room: ${cam.room} · Phone IP: ${cam.ip_address}${cam.port ? `:${cam.port}` : ""} · Status: ${cam.status.toUpperCase()}`;

    const activeState = String(cam.live_posture || "MONITORING").toUpperCase();
    document.querySelectorAll("#expandStateStepper .state-step").forEach((el) => {
      const s = el.dataset.step;
      el.className = "state-step";
      if (s === activeState) {
        if (s === "STANDING") el.classList.add("active-standing");
        else if (s === "FALLING") el.classList.add("active-falling");
        else if (s === "LYING") el.classList.add("active-lying");
        else if (s === "FALL DETECTED") el.classList.add("active-fall");
      }
    });

    const kpLabel =
      cam.live_keypoint_source === "full_pose"
        ? "Pose: Shoulders/Hips/Knees/Ankles"
        : cam.live_keypoint_source === "partial_pose"
        ? "Pose: Partial Keypoints"
        : cam.live_keypoint_source === "bbox_fallback"
        ? "Fallback: Bounding Box"
        : "Pose: Waiting";

    $("expandTelemetryPills").innerHTML = `
      <span class="pill pill-soft-teal">Torso Angle: ${cam.live_torso_angle ?? 0}°</span>
      <span class="pill pill-soft-teal">Body Angle: ${cam.live_body_angle ?? 0}°</span>
      <span class="pill pill-ip">${escapeHtml(kpLabel)}</span>
      <span class="pill pill-soft-green">Cancelled False Falls: ${cam.cancelled_count ?? 0}</span>
    `;
    $("expandBtnPause").textContent = cam.paused ? "▶️ Resume" : "⏸️ Pause";
  }

  function openExpandCamera(cam) {
    state.expandingCameraId = cam.id;
    $("expandFeedImg").src = `/api/cameras/${cam.id}/stream`;
    updateExpandCameraTelemetry(cam);
    $("expandBtnTest").onclick = async () => {
      await api(`/api/cameras/${cam.id}/test-fall`, { method: "POST" });
      showToast(`Test fall triggered for ${cam.name}`);
    };
    $("expandBtnPause").onclick = async () => {
      const curr = state.cameras.find((c) => c.id === cam.id) || cam;
      await api(`/api/cameras/${cam.id}`, {
        method: "PUT",
        body: JSON.stringify({ paused: !curr.paused }),
      });
      await refreshState();
    };
    $("expandBtnConfig").onclick = () => {
      closeModal("cameraExpandModal");
      openCameraConfig(state.cameras.find((c) => c.id === cam.id) || cam);
    };
    openModal("cameraExpandModal");
  }

  async function openConnectModal(method = "ip") {
    document.querySelectorAll(".method-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.method === method);
    });
    ["ip", "qr", "webcam"].forEach((m) => {
      $(`method-${m}`).style.display = m === method ? "block" : "none";
    });
    if (state.system.phone_qr_data_uri) {
      $("qrImage").src = state.system.phone_qr_data_uri;
    }
    $("phoneUrlText").textContent = state.system.phone_camera_url || "";
    $("probePreviewBox").style.display = "none";
    if (method === "webcam") {
      try {
        const ds = await api("/api/dataset/gmdcsa24");
        const sel = $("wcDatasetSelect");
        if (ds.clips && ds.clips.length) {
          sel.innerHTML = ds.clips
            .map((c) => `<option value="${escapeHtml(c.video_path)}">${escapeHtml(c.label)} (${escapeHtml(c.category)})</option>`)
            .join("");
        } else {
          sel.innerHTML = `<option value="">No local GMDCSA-24 clips downloaded (run tools/evaluate_gmdcsa24.py)</option>`;
        }
      } catch {}
    }
    openModal("connectModal");
  }

  function openCameraConfig(cam) {
    state.editingCamera = cam;
    state.editingRoi = { ...(cam.roi || { x: 0, y: 0, w: 1, h: 1 }) };
    $("cfgCamTitle").textContent = `Configure ${cam.name}`;
    $("cfgCamIp").textContent = `IP: ${cam.ip_address}${cam.port ? `:${cam.port}` : ""} · ${cam.source_type}`;
    $("cfgPreviewImg").src = `/api/cameras/${cam.id}/snapshot?t=${Date.now()}`;
    $("cfgName").value = cam.name;
    $("cfgRoom").value = cam.room;
    $("cfgIp").value = cam.ip_address;
    $("cfgSensitivity").value = cam.sensitivity || "medium";
    $("cfgFall").checked = Boolean(cam.fall_detection_enabled);
    $("cfgInactivity").checked = Boolean(cam.inactivity_enabled);
    $("cfgMotion").checked = Boolean(cam.motion_enabled);
    $("cfgMotionAssist").checked = Boolean(cam.motion_assist_enabled);
    $("cfgPaused").checked = Boolean(cam.paused);
    syncRoiBox();
    openModal("cameraConfigModal");
  }

  function syncRoiBox() {
    const r = state.editingRoi;
    const rect = $("roiRect");
    rect.style.left = `${r.x * 100}%`;
    rect.style.top = `${r.y * 100}%`;
    rect.style.width = `${r.w * 100}%`;
    rect.style.height = `${r.h * 100}%`;
  }

  function initRoiDrawing() {
    const stage = $("roiStage");
    let start = null;
    stage.addEventListener("mousedown", (e) => {
      const box = stage.getBoundingClientRect();
      start = {
        x: Math.max(0, Math.min(1, (e.clientX - box.left) / box.width)),
        y: Math.max(0, Math.min(1, (e.clientY - box.top) / box.height)),
      };
    });
    window.addEventListener("mousemove", (e) => {
      if (!start) return;
      const box = stage.getBoundingClientRect();
      const cx = Math.max(0, Math.min(1, (e.clientX - box.left) / box.width));
      const cy = Math.max(0, Math.min(1, (e.clientY - box.top) / box.height));
      state.editingRoi = {
        x: Math.min(start.x, cx),
        y: Math.min(start.y, cy),
        w: Math.max(0.05, Math.abs(cx - start.x)),
        h: Math.max(0.05, Math.abs(cy - start.y)),
      };
      syncRoiBox();
    });
    window.addEventListener("mouseup", () => {
      start = null;
    });
  }

  // ---------- Actions & WebSocket ----------

  async function refreshState() {
    const data = await api("/api/state");
    state.system = data.system;
    state.caregiver = data.caregiver;
    state.resident = data.resident;
    state.contacts = data.contacts;
    state.cameras = data.cameras;
    state.standbyNodes = data.standby_nodes || [];
    state.events = data.events;
    state.settings = data.settings;
    renderAll();
  }

  async function toggleMasterPause(paused) {
    state.resident = await api("/api/resident", {
      method: "PUT",
      body: JSON.stringify({ master_paused: paused }),
    });
    await refreshState();
    showToast(paused ? "All cameras paused" : "Monitoring resumed");
  }

  function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/caregiver`);
    state.ws = ws;

    ws.onopen = () => {
      $("wsPill").classList.remove("offline");
      $("wsPill").querySelector(".ws-text").textContent = "Live";
    };

    ws.onclose = () => {
      $("wsPill").classList.add("offline");
      $("wsPill").querySelector(".ws-text").textContent = "Reconnecting";
      setTimeout(connectWebSocket, 2500);
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.type === "new_alert") {
        state.events.unshift(msg.event);
        renderHeaderAndHero();
        renderEvents();
        if (msg.event.event_type === "fall" || msg.event.event_type === "inactivity") {
          playAlertTone();
          triggerSystemNotification(msg.event);
          showFallAlertModal(msg.event);
        }
      } else if (msg.type === "event_updated") {
        const idx = state.events.findIndex((e) => e.id === msg.event.id);
        if (idx >= 0) state.events[idx] = msg.event;
        else state.events.unshift(msg.event);
        renderHeaderAndHero();
        renderEvents();
        if (state.activeAlertEvent && state.activeAlertEvent.id === msg.event.id && $("fallAlertModal").classList.contains("open")) {
          showFallAlertModal(msg.event);
        }
        if (state.detailEvent && state.detailEvent.id === msg.event.id && $("eventDetailModal").classList.contains("open")) {
          openEventDetail(msg.event);
        }
      } else if (msg.type === "event_deleted") {
        state.events = state.events.filter((e) => e.id !== msg.event_id);
        renderHeaderAndHero();
        renderEvents();
      } else if (msg.type === "camera_added") {
        if (!state.cameras.some((c) => c.id === msg.camera.id)) {
          state.cameras.push(msg.camera);
        }
        renderHeaderAndHero();
        renderCameraGrids();
      } else if (msg.type === "camera_updated" || msg.type === "camera_status") {
        if (msg.camera) {
          const idx = state.cameras.findIndex((c) => c.id === msg.camera.id);
          if (idx >= 0) state.cameras[idx] = msg.camera;
          renderHeaderAndHero();
          renderCameraGrids();
          if (state.expandingCameraId === msg.camera.id && $("cameraExpandModal").classList.contains("open")) {
            updateExpandCameraTelemetry(msg.camera);
          }
        }
      } else if (msg.type === "camera_removed") {
        state.cameras = state.cameras.filter((c) => c.id !== msg.camera_id);
        renderHeaderAndHero();
        renderCameraGrids();
      } else if (msg.type === "standby_nodes") {
        state.standbyNodes = msg.nodes || [];
        renderStandbyNodes();
      } else if (msg.type === "resident_updated") {
        state.resident = msg.resident;
        renderHeaderAndHero();
        renderSettingsForms();
      }
    };
  }

  function bindUiEvents() {
    document.querySelectorAll("[data-tab]").forEach((b) => {
      b.onclick = () => switchTab(b.dataset.tab);
    });
    document.querySelectorAll("[data-goto]").forEach((b) => {
      b.onclick = () => switchTab(b.dataset.goto);
    });
    document.querySelectorAll("[data-close]").forEach((b) => {
      b.onclick = () => {
        const id = b.dataset.close;
        if (id === "cameraExpandModal") {
          state.expandingCameraId = null;
          $("expandFeedImg").src = "";
        }
        closeModal(id);
      };
    });
    document.querySelectorAll("[data-cam-size]").forEach((b) => {
      b.onclick = () => {
        state.cameraViewSize = b.dataset.camSize;
        applyCameraGridSize();
      };
    });
    $("wcSourceMode").onchange = (e) => {
      const isDataset = e.target.value === "video_file";
      $("wcIndexField").style.display = isDataset ? "none" : "flex";
      $("wcDatasetField").style.display = isDataset ? "flex" : "none";
    };
    document.querySelectorAll(".method-tab").forEach((t) => {
      t.onclick = () => openConnectModal(t.dataset.method);
    });

    $("btnQuickConnect").onclick = () => openConnectModal("ip");
    $("qaConnectPhone").onclick = () => openConnectModal("ip");
    $("btnOpenConnectModal").onclick = () => openConnectModal("ip");
    $("btnGuide").onclick = () => openModal("guideModal");
    $("btnAlertsBell").onclick = () => {
      const pending = state.events.find((e) => e.status === "pending");
      if (pending) showFallAlertModal(pending);
      else switchTab("history");
    };

    // Probe phone IP
    $("btnTestProbe").onclick = async () => {
      const ip = $("ipInput").value.trim();
      if (!ip) return showToast("Enter a phone IP address first");
      const box = $("probePreviewBox");
      const txt = $("probeStatusText");
      const img = $("probePreviewImg");
      box.style.display = "block";
      img.style.display = "none";
      txt.textContent = "Probing phone camera stream...";
      try {
        const res = await api("/api/cameras/probe", {
          method: "POST",
          body: JSON.stringify({
            ip_address: ip,
            port: Number($("portInput").value || 8080),
            preset: $("presetInput").value,
            username: $("authUserInput").value.trim(),
            password: $("authPassInput").value,
          }),
        });
        if (res.reachable) {
          txt.textContent = `✅ Connected! Stream: ${res.stream_url} (${res.width}x${res.height})`;
          if (res.preview_data_url) {
            img.src = res.preview_data_url;
            img.style.display = "block";
          }
        } else {
          txt.textContent = `⚠️ Could not reach ${res.stream_url}: ${res.error || "Check Wi-Fi & IP app"}`;
        }
      } catch (e) {
        txt.textContent = `❌ ${e.message}`;
      }
    };

    // Connect phone IP
    $("btnSubmitPhoneIp").onclick = async () => {
      const ip = $("ipInput").value.trim();
      if (!ip) return showToast("Phone IP address is required");
      const preset = $("presetInput").value;
      const room = $("roomInput").value.trim() || "Living Room";
      const name = $("camNameInput").value.trim() || `${room} Phone (${ip})`;
      const btn = $("btnSubmitPhoneIp");
      btn.disabled = true;
      try {
        await api("/api/cameras", {
          method: "POST",
          body: JSON.stringify({
            ip_address: ip,
            port: Number($("portInput").value || 8080),
            room,
            name,
            preset,
            source_type: preset === "phone_node" ? "phone_node" : "ip_camera",
            sensitivity: $("sensInput").value,
            username: $("authUserInput").value.trim(),
            password: $("authPassInput").value,
            motion_assist_enabled: $("motionAssistInput").checked,
          }),
        });
        closeModal("connectModal");
        $("ipInput").value = "";
        $("camNameInput").value = "";
        await refreshState();
        showToast(`Connected ${name}`);
      } catch (e) {
        showToast(e.message);
      } finally {
        btn.disabled = false;
      }
    };

    $("btnSubmitWebcam").onclick = async () => {
      const mode = $("wcSourceMode").value;
      try {
        if (mode === "video_file") {
          const vp = $("wcDatasetSelect").value;
          if (!vp) return showToast("No GMDCSA-24 clip selected");
          await api("/api/cameras", {
            method: "POST",
            body: JSON.stringify({
              name: $("wcNameInput").value.trim() || "GMDCSA-24 Dataset Feed",
              room: $("wcRoomInput").value.trim() || "Living Room",
              source_type: "video_file",
              stream_url: vp,
            }),
          });
        } else {
          await api("/api/cameras", {
            method: "POST",
            body: JSON.stringify({
              name: $("wcNameInput").value.trim() || "Local Test Camera",
              room: $("wcRoomInput").value.trim() || "Living Room",
              source_type: "webcam",
              stream_url: $("wcIndexInput").value.trim() || "0",
            }),
          });
        }
        closeModal("connectModal");
        await refreshState();
        showToast("Test camera connected");
      } catch (e) {
        showToast(e.message);
      }
    };

    $("btnCopyPhoneUrl").onclick = async () => {
      try {
        await navigator.clipboard.writeText($("phoneUrlText").textContent);
        showToast("Phone Camera URL copied");
      } catch {
        showToast("Copy failed");
      }
    };

    // Wi-Fi Scan
    $("btnScanWifi").onclick = async () => {
      const btn = $("btnScanWifi");
      const box = $("scanResultsBox");
      btn.disabled = true;
      btn.textContent = "Scanning subnet...";
      box.style.display = "block";
      box.innerHTML = `<div class="muted">Scanning local Wi-Fi for phones running IP Webcam / DroidCam...</div>`;
      try {
        const res = await api("/api/cameras/scan", { method: "POST", body: "{}" });
        if (!res.discovered.length) {
          box.innerHTML = `<div class="muted">No IP camera phones detected automatically. Make sure IP Webcam is started on each phone, or enter the IP directly.</div>`;
        } else {
          box.innerHTML = res.discovered.map((d) => `
            <div class="event-item" style="cursor:default; margin-bottom:8px;">
              <img class="event-thumb" src="${d.preview_data_url || ""}" alt="Found phone" />
              <div class="event-info">
                <div class="event-title">Found Phone Camera <span class="pill pill-ip">${escapeHtml(d.ip_address)}:${d.port}</span></div>
                <div class="event-sub">${escapeHtml(d.stream_url)} (${d.width}x${d.height})</div>
              </div>
              <button class="btn btn-primary btn-sm" data-quick-add="${escapeHtml(d.ip_address)}" data-quick-port="${d.port}">+ Connect</button>
            </div>
          `).join("");
          box.querySelectorAll("[data-quick-add]").forEach((b) => {
            b.onclick = () => {
              openConnectModal("ip");
              $("ipInput").value = b.dataset.quickAdd;
              $("portInput").value = b.dataset.quickPort;
            };
          });
        }
      } catch (e) {
        box.innerHTML = `<div class="muted">Scan error: ${escapeHtml(e.message)}</div>`;
      } finally {
        btn.disabled = false;
        btn.textContent = "🔍 Scan Wi-Fi for Phones";
      }
    };

    // History Filters
    document.querySelectorAll("[data-type]").forEach((chip) => {
      chip.onclick = () => {
        state.historyType = chip.dataset.type;
        document.querySelectorAll("[data-type]").forEach((c) => c.classList.toggle("active", c === chip));
        renderEvents();
      };
    });
    $("historyStatusFilter").onchange = renderEvents;
    $("historyCameraFilter").onchange = renderEvents;

    // Camera Config Modal Actions
    $("btnResetRoi").onclick = () => {
      state.editingRoi = { x: 0, y: 0, w: 1, h: 1 };
      syncRoiBox();
    };
    $("btnSaveCameraCfg").onclick = async () => {
      const cam = state.editingCamera;
      if (!cam) return;
      await api(`/api/cameras/${cam.id}`, {
        method: "PUT",
        body: JSON.stringify({
          name: $("cfgName").value.trim(),
          room: $("cfgRoom").value.trim(),
          ip_address: $("cfgIp").value.trim(),
          sensitivity: $("cfgSensitivity").value,
          fall_detection_enabled: $("cfgFall").checked,
          inactivity_enabled: $("cfgInactivity").checked,
          motion_enabled: $("cfgMotion").checked,
          motion_assist_enabled: $("cfgMotionAssist").checked,
          paused: $("cfgPaused").checked,
          roi: state.editingRoi,
        }),
      });
      closeModal("cameraConfigModal");
      await refreshState();
      showToast("Camera settings saved");
    };
    $("btnRunCamTest").onclick = async () => {
      if (!state.editingCamera) return;
      await api(`/api/cameras/${state.editingCamera.id}/test-fall`, { method: "POST" });
      closeModal("cameraConfigModal");
    };
    $("btnDeleteCamera").onclick = async () => {
      if (!state.editingCamera) return;
      await api(`/api/cameras/${state.editingCamera.id}`, { method: "DELETE" });
      closeModal("cameraConfigModal");
      await refreshState();
      showToast("Phone camera removed");
    };

    // Settings & Profiles
    $("caregiverForm").onsubmit = async (e) => {
      e.preventDefault();
      state.caregiver = await api("/api/caregiver", {
        method: "PUT",
        body: JSON.stringify({
          full_name: $("cgName").value,
          email: $("cgEmail").value,
          phone: $("cgPhone").value,
        }),
      });
      renderAll();
      showToast("Caregiver profile saved");
    };

    $("residentForm").onsubmit = async (e) => {
      e.preventDefault();
      state.resident = await api("/api/resident", {
        method: "PUT",
        body: JSON.stringify({
          name: $("resName").value,
          relationship: $("resRelation").value,
          phone: $("resPhone").value,
          address: $("resAddress").value,
          medical_notes: $("resMedical").value,
        }),
      });
      renderAll();
      showToast("Resident profile saved");
    };

    $("masterPauseToggle").onchange = (e) => toggleMasterPause(e.target.checked);
    $("btnOpenConsent").onclick = () => {
      $("consentRoomsInput").value = (state.resident.consent_rooms || []).join(", ");
      $("consentAgreeCheck").checked = Boolean(state.resident.consent_agreed);
      openModal("consentModal");
    };
    $("btnSaveConsent").onclick = async () => {
      const rooms = $("consentRoomsInput").value.split(",").map((s) => s.trim()).filter(Boolean);
      state.resident = await api("/api/resident", {
        method: "PUT",
        body: JSON.stringify({
          consent_agreed: $("consentAgreeCheck").checked,
          consent_rooms: rooms,
          consent_pin: $("consentPinInput").value.trim() || "1234",
        }),
      });
      closeModal("consentModal");
      renderAll();
      showToast("Consent updated");
    };

    $("btnSaveSystemSettings").onclick = async () => {
      state.settings = await api("/api/settings", {
        method: "PUT",
        body: JSON.stringify({
          alert_sound: $("setAlertSound").checked,
          show_skeleton_overlay: $("setSkeletonOverlay").checked,
          auto_delete: $("setAutoDelete").checked,
          retention_days: Number($("setRetentionDays").value),
          cooldown_seconds: Number($("setCooldown").value || 15),
        }),
      });
      showToast("System settings saved");
    };

    $("btnPurgeNow").onclick = async () => {
      const res = await api("/api/events/purge", { method: "POST", body: "{}" });
      await refreshState();
      showToast(`Purged ${res.purged} expired event(s)`);
    };

    $("btnAddContact").onclick = () => openModal("contactModal");
    $("btnSaveContact").onclick = async () => {
      const name = $("newContactName").value.trim();
      const phone = $("newContactPhone").value.trim();
      if (!name || !phone) return showToast("Name and phone required");
      await api("/api/contacts", {
        method: "POST",
        body: JSON.stringify({
          name,
          relationship: $("newContactRel").value.trim() || "Family",
          phone,
        }),
      });
      $("newContactName").value = "";
      $("newContactPhone").value = "";
      closeModal("contactModal");
      await refreshState();
      showToast("Emergency contact added");
    };

    // Notification Controls
    if ($("btnHeaderNotify")) {
      $("btnHeaderNotify").onclick = () => {
        if (Notification.permission === "granted") {
          switchTab("settings");
        } else {
          requestNotificationPermission();
        }
      };
    }
    if ($("btnBannerEnableNotify")) {
      $("btnBannerEnableNotify").onclick = () => requestNotificationPermission();
    }
    if ($("btnBannerDismissNotify")) {
      $("btnBannerDismissNotify").onclick = () => {
        state.dismissedNotificationBanner = true;
        if ($("notificationBanner")) $("notificationBanner").style.display = "none";
      };
    }
    if ($("btnEnableNotifications")) {
      $("btnEnableNotifications").onclick = () => requestNotificationPermission();
    }
    if ($("btnTestNotification")) {
      $("btnTestNotification").onclick = async () => {
        const btn = $("btnTestNotification");
        btn.disabled = true;
        try {
          if ("Notification" in window && Notification.permission !== "granted") {
            await Notification.requestPermission();
            updateNotificationUi();
          }
          await api("/api/notifications/test", { method: "POST" });
          showToast("Test lockscreen alert sent! Lock phone or switch apps to verify.");
        } catch (e) {
          showToast(`Test error: ${e.message}`);
        } finally {
          btn.disabled = false;
        }
      };
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    bindUiEvents();
    initRoiDrawing();
    initServiceWorker();
    updateNotificationUi();
    try {
      await refreshState();
      connectWebSocket();
    } catch (err) {
      showToast(`Failed to connect: ${err.message}`);
    }
  });
})();
