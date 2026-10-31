// Service worker de la Gazette : l'appli s'ouvre même sans réseau, avec la dernière collecte reçue.
// Réseau d'abord pour la page et les données (toujours la version la plus récente),
// copie locale en secours. Les polices et icônes, qui changent rarement, viennent du cache.
const CACHE = "gazette-v1";
const BASE = [
  "./",
  "manifest.webmanifest",
  "icones/icone-gazette-janze-32.png",
  "icones/icone-gazette-janze-180.png",
  "icones/icone-gazette-janze-192.png",
  "icones/icone-gazette-janze-512.png",
  "icones/icone-gazette-janze-maskable-512.png"
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(cles => Promise.all(cles.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Réseau d'abord ; la réponse est gardée sous une clé fixe (sans le ?v= de auto.js).
async function reseauDabord(req, cle) {
  const cache = await caches.open(CACHE);
  try {
    const rep = await fetch(req);
    if (rep.ok) cache.put(cle, rep.clone());
    return rep;
  } catch (err) {
    const copie = await cache.match(cle);
    if (copie) return copie;
    throw err;
  }
}

// Cache d'abord, rafraîchi en arrière-plan.
async function cacheDabord(req) {
  const cache = await caches.open(CACHE);
  const copie = await cache.match(req);
  const frais = fetch(req).then(rep => { if (rep.ok || rep.type === "opaque") cache.put(req, rep.clone()); return rep; });
  if (copie) { frais.catch(() => {}); return copie; }
  return frais;
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (req.mode === "navigate") { e.respondWith(reseauDabord(req, "./")); return; }
  if (url.origin === location.origin) {
    if (url.pathname.endsWith("/donnees/auto.js")) { e.respondWith(reseauDabord(req, "donnees/auto.js")); return; }
    e.respondWith(cacheDabord(req));
    return;
  }
  if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") e.respondWith(cacheDabord(req));
});
