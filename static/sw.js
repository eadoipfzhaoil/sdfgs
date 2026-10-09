// Service worker ساده: برای قابل نصب بودن برنامه. هیچ داده‌ای کش نمی‌شود.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET' || e.request.mode !== 'navigate') return;
  e.respondWith(
    fetch(e.request).catch(() => new Response(
      '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<body style="font-family:Tahoma;background:#0f1220;color:#e8ebf7;text-align:center;padding:60px 20px">' +
      '<h2>📡 اتصال اینترنت قطع است</h2><p>اینترنت را بررسی کنید و دوباره تلاش کنید.</p></body></html>',
      {headers: {'Content-Type': 'text/html; charset=utf-8'}}
    ))
  );
});
