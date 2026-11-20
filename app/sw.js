// Picture House offline support.
// The app page is network-first with a short timeout (weak signal falls back
// to the saved copy); posters and fonts are cache-first. The library JSON and
// the wish list are saved by the page itself.
const SHELL = "ph-shell-v4";
const POSTERS = "ph-posters";
const FONTS = "ph-fonts";
const STATIC = ["/manifest.webmanifest", "/icon.svg", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil((async () => {
    const c = await caches.open(SHELL);
    await c.addAll(STATIC).catch(() => {});
    try {
      const r = await fetch("/", {credentials: "same-origin"});
      if (r.ok && !r.headers.get("X-PH-Login")) await c.put("/", r);
    } catch (err) {}
    self.skipWaiting();
  })());
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const keep = [SHELL, POSTERS, FONTS, "ph-data"];
    for (const k of await caches.keys()) if (!keep.includes(k)) await caches.delete(k);
    await self.clients.claim();
  })());
});

function timeout(ms) { return new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms)); }

async function page(req) {
  const c = await caches.open(SHELL);
  const net = fetch(req).then(async r => {
    if (r.ok && !r.headers.get("X-PH-Login")) await c.put("/", r.clone());
    return r;
  });
  try {
    return await Promise.race([net, timeout(4000)]);
  } catch (err) {
    const hit = await c.match("/");
    if (hit) { net.catch(() => {}); return hit; }
    try { return await net; } catch (e2) {
      return new Response("<!doctype html><meta name=viewport content='width=device-width'><title>Offline</title><body style='font-family:sans-serif;padding:24px'><h1>You're offline</h1><p>Open Picture House once with signal and it will work offline after that.</p>", {headers: {"Content-Type": "text/html"}});
    }
  }
}

async function cacheFirst(req, name) {
  const c = await caches.open(name);
  const hit = await c.match(req);
  if (hit) return hit;
  const r = await fetch(req);
  if (r.ok || r.type === "opaque") c.put(req, r.clone());
  return r;
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin === location.origin) {
    if (req.mode === "navigate" && (url.pathname === "/" || url.pathname === "/index.html")) return e.respondWith(page(req));
    if (url.pathname.startsWith("/api/poster/")) return e.respondWith(cacheFirst(req, POSTERS));
    if (STATIC.includes(url.pathname)) return e.respondWith(cacheFirst(req, SHELL));
    return;
  }
  if (url.host === "fonts.googleapis.com" || url.host === "fonts.gstatic.com") e.respondWith(cacheFirst(req, FONTS));
});
