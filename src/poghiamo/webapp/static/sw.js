// poghiamo service worker: offline reading only, no notifications.
//
// Everything is network-first. A cached copy is used only when the network
// fails, so a deploy is never hidden behind stale pages. The pages worth
// reading offline (feed, calendar, artists) are saved as they are visited; any
// other page falls back to /offline. The login page empties the page cache, so
// another account on the same device never sees them.

const STATIC_CACHE = "poghiamo-static-v1";
const PAGES_CACHE = "poghiamo-pages";
const PRECACHE = [
  "/offline",
  "/static/tailwind.css",
  "/static/favicon.svg",
  "/static/icons/icon-192.png",
];
const OFFLINE_PAGES = /^\/(calendario|artists(\/\d+)?)?$/;

self.addEventListener("install", (event) => {
  // One by one, so a file missing in development does not abort the install.
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) =>
      Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => undefined)))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key.startsWith("poghiamo-static-") && key !== STATIC_CACHE)
          .map((key) => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  // Forms (POST) and other origins go straight to the network, untouched.
  if (request.method !== "GET" || url.origin !== self.location.origin) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Only plain 200 pages: never a redirect (e.g. to /login after the
          // session expired) or an error page.
          if (response.status === 200 && response.type === "basic" && OFFLINE_PAGES.test(url.pathname)) {
            const copy = response.clone();
            caches.open(PAGES_CACHE).then((cache) => cache.put(url.pathname, copy));
          }
          return response;
        })
        .catch(() =>
          caches.match(url.pathname, { cacheName: PAGES_CACHE }).then(
            (page) => page || caches.match("/offline", { cacheName: STATIC_CACHE })
          )
        )
    );
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Keep the offline copy of the stylesheet in step with the deploys.
          if (response.status === 200 && PRECACHE.includes(url.pathname)) {
            const copy = response.clone();
            caches.open(STATIC_CACHE).then((cache) => cache.put(url.pathname, copy));
          }
          return response;
        })
        .catch(() => caches.match(url.pathname, { cacheName: STATIC_CACHE }))
    );
  }
});
