// 홈 화면 앱 설치용 최소 서비스 워커 — 자료는 항상 서버에서 새로 받음 (오래된 숫자를 보여 주지 않게)
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
