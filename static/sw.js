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
//
// Strategies (by request):
//   * the app's own code -- shared.css, shared.js, data_sync.js: NETWORK-FIRST
//     (always revalidated), cached copy used only if the network fails. An
//     edit on the server reaches the very next load, and a page and its JS
//     can't drift apart because of an old cache.
//   * manifest.json + icons: STALE-WHILE-REVALIDATE -- served instantly from
//     cache but re-fetched every time, so a changed icon/manifest shows up
//     one load later and can never be stuck forever.
//   * everything else (HTML pages, /designs, /cars, /last_update, images,
//     POSTs...): straight to the network. Only a genuine network failure
//     falls back -- to a clearly-labeled offline message for a page
//     navigation, never to a stale copy of stock data.
//
// Updates: skipWaiting() + clients.claim() so a fixed worker takes over on
// the next load instead of waiting for every tab to close. Safe because the
// worker is stateless (the only state is the cache below, which every
// version fully replaces), and it never reloads or navigates an open page.
// Old caches are deleted on activate.
const CACHE_VERSION = "v3";
const STATIC_CACHE = `super-seatings-static-${CACHE_VERSION}`;

// The app's own code: network-first, cache only as an offline fallback.
const NETWORK_FIRST_ASSETS = [
    "/static/shared.css",
    "/static/shared.js",
    "/static/data_sync.js",
];

// Rarely change, no live data: cached, but revalidated on every use.
const REVALIDATED_ASSETS = [
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

// Cache entries are keyed by path only, so "?v=2" style URLs share one slot.
function cacheKey(url) {
    return new Request(url.origin + url.pathname);
}

// Best-effort precache so the offline fallback has something the first time.
// Each file is fetched fresh (bypassing the HTTP cache) and a failure of any
// one never blocks installing the worker.
async function precache() {
    const cache = await caches.open(STATIC_CACHE);
    await Promise.all(
        NETWORK_FIRST_ASSETS.concat(REVALIDATED_ASSETS).map(async (path) => {
            try {
                const response = await fetch(new Request(path, { cache: "reload" }));
                if (response.ok) {
                    await cache.put(new Request(self.location.origin + path), response);
                }
            } catch (error) {
                // Ignore: it will be cached on first real use.
            }
        })
    );
}

self.addEventListener("install", (event) => {
    event.waitUntil(precache());
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

async function networkFirst(request, url) {
    const cache = await caches.open(STATIC_CACHE);
    try {
        const response = await fetch(request, { cache: "no-cache" });
        if (response.ok) {
            await cache.put(cacheKey(url), response.clone());
        }
        return response;
    } catch (error) {
        const cached = await cache.match(cacheKey(url));
        if (cached) {
            return cached;
        }
        throw error;
    }
}

function staleWhileRevalidate(event, request, url) {
    return caches.open(STATIC_CACHE).then((cache) =>
        cache.match(cacheKey(url)).then((cached) => {
            const refresh = fetch(request, { cache: "no-cache" })
                .then((response) => {
                    if (response.ok) {
                        return cache.put(cacheKey(url), response.clone()).then(() => response);
                    }
                    return response;
                });
            if (cached) {
                event.waitUntil(refresh.catch(() => {}));
                return cached;
            }
            return refresh;
        })
    );
}

function offlineResponse(request) {
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

    if (NETWORK_FIRST_ASSETS.includes(url.pathname)) {
        event.respondWith(networkFirst(request, url).catch(() => offlineResponse(request)));
        return;
    }

    if (REVALIDATED_ASSETS.includes(url.pathname)) {
        event.respondWith(staleWhileRevalidate(event, request, url).catch(() => offlineResponse(request)));
        return;
    }

    // Everything else is network-only (see the header comment).
    event.respondWith(fetch(request).catch(() => offlineResponse(request)));
});
