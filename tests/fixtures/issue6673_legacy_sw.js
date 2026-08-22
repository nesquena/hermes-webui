// Handler-less worker fixture for the #6673 lifecycle gate.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
