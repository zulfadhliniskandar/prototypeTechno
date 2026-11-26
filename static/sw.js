// SafeWatch Caregiver Service Worker
// Enables lock screen alerts, background vibration, sound, and notification actions
// even when phone is asleep or caregiver is working in other apps.

const CACHE_NAME = "safewatch-v2";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Helper to show rich notification
async function displayNotification(payload) {
  const title = payload.title || "🚨 SafeWatch Fall Alert!";
  const options = {
    body: payload.body || "Emergency: Fall detected. Tap to view camera and respond.",
    icon: payload.icon || "/static/icon-192.png",
    badge: payload.badge || "/static/icon-192.png",
    image: payload.image || undefined,
    data: payload.data || { url: "/", event_id: null },
    tag: payload.tag || "safewatch-fall-alert",
    renotify: true,
    requireInteraction: true, // Stays persistent on lock screen until caregiver taps or dismisses
    vibrate: payload.vibrate || [500, 200, 500, 200, 500, 300, 1000],
    actions: payload.actions || [
      { action: "view", title: "🚨 View Situation" },
      { action: "call", title: "📞 Call Emergency" }
    ]
  };

  return self.registration.showNotification(title, options);
}

// Receive push events from server WebPush
self.addEventListener("push", (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: "SafeWatch Alert", body: event.data.text() };
    }
  }
  event.waitUntil(displayNotification(data));
});

// Receive explicit trigger messages from app.js
self.addEventListener("message", (event) => {
  if (!event.data) return;
  if (event.data.type === "TRIGGER_NOTIFICATION") {
    event.waitUntil(displayNotification(event.data.payload));
  } else if (event.data.type === "PING") {
    event.source.postMessage({ type: "PONG" });
  }
});

// Handle user clicking the notification on lockscreen / OS tray
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const notifData = event.notification.data || {};
  const targetUrl = notifData.url || "/";

  // If caregiver clicked the "Call" action button on lockscreen
  if (event.action === "call" && notifData.call_phone) {
    if (self.clients.openWindow) {
      event.waitUntil(self.clients.openWindow(`tel:${notifData.call_phone}`));
      return;
    }
  }

  // Otherwise, focus the open SafeWatch tab or open a new window
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) {
          if (notifData.event_id && client.postMessage) {
            client.postMessage({
              type: "FOCUS_ALERT",
              event_id: notifData.event_id,
            });
          }
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
