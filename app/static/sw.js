/* Service worker de 3S Recargas — solo maneja notificaciones push.
   No cachea nada (evita servir versiones viejas de la tienda). */

self.addEventListener('install', function (event) {
    self.skipWaiting();
});

self.addEventListener('activate', function (event) {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
    var payload = { title: '3S Recargas', body: 'Tienes una notificación nueva.' };
    if (event.data) {
        try {
            payload = event.data.json();
        } catch (_) {
            payload.body = event.data.text();
        }
    }

    var options = {
        body: payload.body || '',
        icon: '/static/img/logo.png',
        badge: '/static/img/logo.png',
        tag: payload.tag || 'general',
        data: { url: payload.url || '/' },
    };

    event.waitUntil(self.registration.showNotification(payload.title || '3S Recargas', options));
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    var targetUrl = (event.notification.data && event.notification.data.url) || '/';

    event.waitUntil(
        self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (clientList) {
            for (var i = 0; i < clientList.length; i++) {
                var client = clientList[i];
                if (client.url.indexOf(targetUrl) !== -1 && 'focus' in client) {
                    return client.focus();
                }
            }
            if (self.clients.openWindow) {
                return self.clients.openWindow(targetUrl);
            }
        })
    );
});
