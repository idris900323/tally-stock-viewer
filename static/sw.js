// Service worker for the customer-facing PWA (installability only -- no
// push notifications, no offline browsing of the design catalog). Only
// ever registered from a customer session (see index.html's registration
// script, gated on {% if not is_admin %}), but a service worker's scope is
// per-origin, not per Flask session/role -- it has no reliable way to see
// which role is browsing on a later request (the Cookie header isn't
// exposed to fetch events), so it's deliberately written to be safe
// regardless of who ends up served by it: HTML and data always go to the
// network first, real business data (stock/mappings) is never served stale
// from a cache.
const CACHE_VERSION = "v1";
const STATIC_CACHE = `super-seatings-static-${CACHE_VERSION}`;

// Genuinely static, rarely-changing assets only -- no HTML, no API/data
// routes. Cache-first is safe for these because none of them carry live
// business data.
const STATIC_ASSETS = [
    "/static/shared.css",
    "/static/shared.js",
    "/static/manifest.json",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
    "/static/icons/apple-touch-icon.png",
];

const OFFLINE_MESSAGE_HTML = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Offline - Super Seatings</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: Arial, sans-serif; background:#f3f6fb; color:#132238; padding:24px; box-sizing:border-box; }
  .card { max-width:360px; text-align:center; background:#fff; border:1px solid #d9e2ec; border-radius:16px; padding:28px 22px; }
  h1 { font-size:18px; margin:0 0 10px; }
  p { margin:0; color:#5d6b82; font-size:14px; line-height:1.5; }
  button { margin-top:18px; padding:10px 18px; border-radius:10px; border:0; background:#1f6feb; color:#fff; font-size:14px; font-weight:700; cursor:pointer; }
</style>
</head>
<body>
  <div class="card">
    <h1>You're offline</h1>
    <p>Please reconnect to see current stock and designs. This page never shows old stock data as if it were current.</p>
    <button onclick="location.reload()">Try again</button>
  </div>
</body>
</html>`;

self.addEventListener("install", (event) => {
    event.waitUntil(
        caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS))
    );
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) =>
            Promise.all(
                keys
                    .filter((key) => key !== STATIC_CACHE)
                    .map((key) => caches.delete(key))
            )
        )
    );
    self.clients.claim();
});

function isStaticAsset(url) {
    return STATIC_ASSETS.some((path) => url.pathname === path);
}

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") {
        // Never intercept POST/PUT/DELETE (logins, stock updates, uploads,
        // etc.) -- those always go straight to the network, untouched.
        return;
    }

    const url = new URL(request.url);
    if (url.origin !== self.location.origin) {
        return;
    }

    if (isStaticAsset(url)) {
        // Cache-first: these never carry live business data.
        event.respondWith(
            caches.match(request).then((cached) => {
                if (cached) {
                    return cached;
                }
                return fetch(request).then((response) => {
                    if (response.ok) {
                        const clone = response.clone();
                        caches.open(STATIC_CACHE).then((cache) => cache.put(request, clone));
                    }
                    return response;
                });
            })
        );
        return;
    }

    // Everything else (HTML pages, /designs, /cars, /get_stock_image,
    // /update_stock, etc.) is network-first, always. Only a genuine network
    // failure falls back -- and only to a clearly-labeled offline message
    // for a real page navigation, never to a silently-stale cached
    // response standing in for current stock/mapping data.
    event.respondWith(
        fetch(request).catch(() => {
            if (request.mode === "navigate") {
                return new Response(OFFLINE_MESSAGE_HTML, {
                    status: 503,
                    headers: { "Content-Type": "text/html; charset=utf-8" },
                });
            }
            return new Response(JSON.stringify({ ok: false, offline: true, error: "You're offline." }), {
                status: 503,
                headers: { "Content-Type": "application/json" },
            });
        })
    );
});
