/* Campana de notificaciones del header.
 *
 * Junta las novedades del admin y el estado de las órdenes hechas en ESTE
 * navegador (la página de la orden registra su número aquí solo si la compra se
 * hizo en este navegador). Todo texto se pinta con textContent: lo que escribe
 * el admin o el proveedor nunca se interpreta como HTML.
 */
(function () {
    'use strict';

    var bell = document.getElementById('notifToggle');
    var panel = document.getElementById('notifPanel');
    var listEl = document.getElementById('notifList');
    var badge = document.getElementById('notifBadge');
    var closeBtn = document.getElementById('notifClose');
    if (!bell || !panel || !listEl || !badge) return;

    var ORDERS_KEY = '3s_orders';
    var SEEN_KEY = '3s_notif_seen';
    var MAX_ORDERS = 15;
    var POLL_MS = 60000;
    var FIRST_VISIT_WINDOW_MS = 7 * 24 * 3600 * 1000;

    var items = [];
    var unreadIds = {};      // avisos que estaban sin leer al abrir el panel
    var pollTimer = null;

    // ── almacenamiento (puede no estar disponible: modo privado, etc.) ───────
    function read(key) {
        try { return JSON.parse(window.localStorage.getItem(key)); } catch (e) { return null; }
    }
    function write(key, value) {
        try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* sin almacenamiento */ }
    }
    function lastSeen() {
        var stored = Number(read(SEEN_KEY));
        return stored > 0 ? stored : Date.now() - FIRST_VISIT_WINDOW_MS;
    }

    function registerOrder(number) {
        number = String(number || '').trim();
        if (!/^[0-9A-Za-z]{4,20}$/.test(number)) return;
        var orders = (read(ORDERS_KEY) || []).filter(function (n) { return n !== number; });
        orders.push(number);
        write(ORDERS_KEY, orders.slice(-MAX_ORDERS));
    }
    if (window.OWN_ORDER_NUMBER) registerOrder(window.OWN_ORDER_NUMBER);

    // ── pintado ──────────────────────────────────────────────────────────────
    function relativeTime(iso) {
        var then = Date.parse(iso);
        if (!then) return '';
        var diff = Math.max(0, Date.now() - then) / 1000;
        if (diff < 60) return 'ahora';
        if (diff < 3600) return 'hace ' + Math.floor(diff / 60) + ' min';
        if (diff < 86400) return 'hace ' + Math.floor(diff / 3600) + ' h';
        if (diff < 7 * 86400) return 'hace ' + Math.floor(diff / 86400) + ' d';
        return new Date(then).toLocaleDateString('es-VE', { day: '2-digit', month: 'short' });
    }

    // Íconos propios (en vez de emojis): cada aviso lleva un cuadrito de cristal rojo, blanco o negro con su dibujo.
    var SVG_NS = 'http://www.w3.org/2000/svg';
    var ICONS = {
        check: ['M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0z', 'm8.5 12.2 2.4 2.4 4.7-4.9'],
        alert: ['M12 4.2 2.8 19.8h18.4z', 'M12 10v4.2', 'M12 17.3v.1'],
        cross: ['M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0z', 'm9.2 9.2 5.6 5.6', 'm14.8 9.2-5.6 5.6'],
        mail: ['M5 5.5h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2z', 'm3.5 8 8.5 5.8L20.5 8'],
        megaphone: ['M4 10v4a1 1 0 0 0 1 1h2.2l5.3 4V5L7.2 9H5a1 1 0 0 0-1 1z', 'M16 9.2a4 4 0 0 1 0 5.6', 'M19 6.5a8 8 0 0 1 0 11'],
        clock: ['M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0z', 'M12 7.5V12l3 1.8']
    };
    // Color según el proceso de la orden: en proceso = amarillo, aprobada = verde, rechazada = rojo. Los avisos que no
    // son de una orden (novedades y mensajes del equipo) van en tono neutro. El mismo color lo llevan el ícono, la
    // etiqueta del título y el botón "Ver mi orden" (clases st-wait / st-ok / st-bad / st-neutral).
    function statusOf(item) {
        if (item.kind !== 'order') return 'neutral';
        if (item.level === 'success') return 'ok';
        if (item.level === 'danger') return 'bad';
        return 'wait';
    }
    function iconSpec(item) {
        if (item.kind === 'support') return { name: 'mail', tone: 'white' };
        if (item.level === 'success') return { name: 'check', tone: '' };
        if (item.level === 'warning') return { name: 'alert', tone: '' };
        if (item.level === 'danger') return { name: 'cross', tone: '' };
        if (item.kind === 'message') return { name: 'mail', tone: 'white' };
        if (item.kind === 'broadcast') return { name: 'megaphone', tone: 'black' };
        return { name: 'clock', tone: '' };
    }
    function buildIcon(item) {
        var spec = iconSpec(item);
        var icon = document.createElement('span');
        icon.className = 'notif-icon' + (spec.tone ? ' ic-' + spec.tone : '');
        icon.setAttribute('aria-hidden', 'true');
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        ICONS[spec.name].forEach(function (d) {
            var path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        icon.appendChild(svg);
        return icon;
    }

    // Solo rutas de la tienda o http(s): nunca "javascript:" ni otros esquemas.
    function safeHref(url) {
        url = String(url || '').trim();
        if (/^\/(?!\/)/.test(url) || /^https?:\/\//i.test(url)) return url;
        return '';
    }

    function render() {
        listEl.textContent = '';
        if (!items.length) {
            var empty = document.createElement('li');
            empty.className = 'notif-empty';
            empty.textContent = 'Todavía no tienes notificaciones.';
            listEl.appendChild(empty);
            return;
        }
        items.forEach(function (item) {
            var href = safeHref(item.url);
            var row = document.createElement('li');
            var box = document.createElement(href ? 'a' : 'div');
            box.className = 'notif-item lvl-' + item.level + ' st-' + statusOf(item) + (unreadIds[item.id] ? ' is-unread' : '');
            if (href) {
                box.href = href;
                if (/^https?:/i.test(href)) { box.target = '_blank'; box.rel = 'noopener noreferrer'; }
            }

            var icon = buildIcon(item);

            var text = document.createElement('span');
            text.className = 'notif-text';
            var title = document.createElement('p');
            title.className = 'notif-title';
            title.textContent = item.title;
            var body = document.createElement('p');
            body.className = 'notif-body';
            body.textContent = item.body;
            var time = document.createElement('p');
            time.className = 'notif-time';
            time.textContent = relativeTime(item.at);
            text.append(title, body, time);
            if (href) {
                // Se ve que se puede tocar: lleva a la orden (o a la página del aviso).
                var go = document.createElement('p');
                go.className = 'notif-go';
                go.textContent = item.action_label || (item.order_number ? 'Ver mi orden ›' : 'Ver más ›');
                text.appendChild(go);
            }

            box.append(icon, text);
            row.appendChild(box);
            listEl.appendChild(row);
        });
    }

    function unreadCount() {
        var seen = lastSeen();
        return items.filter(function (item) { return Date.parse(item.at) > seen; }).length;
    }

    function updateBadge() {
        var count = panel.hidden ? unreadCount() : 0;
        badge.hidden = count === 0;
        badge.textContent = count > 9 ? '9+' : String(count);
        bell.classList.toggle('has-unread', count > 0);
        bell.setAttribute('aria-label', count > 0 ? 'Notificaciones (' + count + ' nuevas)' : 'Notificaciones');
    }

    // ── abrir / cerrar ───────────────────────────────────────────────────────
    function openPanel() {
        var seen = lastSeen();
        unreadIds = {};
        items.forEach(function (item) { if (Date.parse(item.at) > seen) unreadIds[item.id] = true; });
        render();
        panel.hidden = false;
        // Las pantallas que tapan el header (el chat de soporte en el teléfono) lo usan
        // para bajar de nivel y dejar ver el panel encima.
        document.body.classList.add('notif-open');
        bell.setAttribute('aria-expanded', 'true');
        // Se marcan como leídas, pero esta vez siguen resaltadas para que se vea qué es nuevo.
        // Se guarda la hora del aviso más nuevo (del servidor), no la del reloj
        // del teléfono: un reloj adelantado ocultaría avisos nuevos.
        var newest = items.reduce(function (max, item) { return Math.max(max, Date.parse(item.at) || 0); }, 0);
        write(SEEN_KEY, newest || Date.now());
        updateBadge();
        load();
    }

    function closePanel(returnFocus) {
        if (panel.hidden) return;
        panel.hidden = true;
        document.body.classList.remove('notif-open');
        bell.setAttribute('aria-expanded', 'false');
        unreadIds = {};
        updateBadge();
        if (returnFocus) bell.focus();
    }

    bell.addEventListener('click', function (event) {
        event.stopPropagation();
        if (panel.hidden) openPanel(); else closePanel(true);
    });
    if (closeBtn) closeBtn.addEventListener('click', function () { closePanel(true); });
    // Si el aviso lleva a la página en la que ya estás (por ejemplo tu orden),
    // recargarla parece que no pasó nada: se cierra el panel y se sube arriba.
    listEl.addEventListener('click', function (event) {
        var link = event.target.closest ? event.target.closest('a.notif-item') : null;
        if (!link) return;
        var target = new URL(link.href, window.location.href);
        if (target.origin === window.location.origin && target.pathname === window.location.pathname && !target.hash) {
            event.preventDefault();
            closePanel(false);
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
    });
    document.addEventListener('click', function (event) {
        if (!panel.hidden && !panel.contains(event.target) && !bell.contains(event.target)) closePanel(false);
    });
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') closePanel(true);
    });

    // ── carga ────────────────────────────────────────────────────────────────
    function load() {
        var orders = read(ORDERS_KEY);
        orders = Array.isArray(orders) ? orders.slice(-MAX_ORDERS) : [];
        // El chat de soporte de este navegador (su token está guardado desde que se abrió): sirve para avisar "Soporte te respondió".
        var supportToken = '';
        try { supportToken = window.localStorage.getItem('store:support-token') || ''; } catch (e) { /* sin almacenamiento */ }
        return window.fetch('/api/notificaciones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ orders: orders, support_token: supportToken }),
            cache: 'no-store'
        }).then(function (response) {
            return response.ok ? response.json() : Promise.reject(new Error('http ' + response.status));
        }).then(function (data) {
            items = Array.isArray(data.items) ? data.items : [];
            if (!panel.hidden) render();
            updateBadge();
        }).catch(function () { /* sin red: se conserva lo último que se vio */ });
    }

    function startPolling() {
        if (pollTimer) return;
        pollTimer = window.setInterval(function () {
            if (document.visibilityState === 'visible') load();
        }, POLL_MS);
    }
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') load();
    });

    window.SiteNotifications = { registerOrder: registerOrder, refresh: load };
    load();
    startPolling();
})();
