/* Top de perfiles: cambiar de pestaña o de página SIN recargar toda la página (antes cada toque cargaba todo otra vez: fondo, estilos, letras...).
   - Pide solo la lista (?partial=1), la guarda 45 s y adelanta las otras pestañas en segundo plano, así el cambio es casi instantáneo.
   - Los enlaces siguen siendo enlaces normales: sin JavaScript, o si algo falla, se abre la página completa como siempre. */
(function () {
    'use strict';
    var panel = document.getElementById('topPanel');
    if (!panel || !window.fetch || !window.history || !history.pushState || !window.URL) return;

    var CACHE_MS = 45000;
    var PERIOD_TABS = ['mes', 'historico'];
    var ALL_TABS = ['mes', 'historico', 'reputacion', 'nivel'];
    var periodNav = document.querySelector('.pf-tabs-period');
    var kindNav = document.querySelector('.pf-tabs-kind');
    var cache = {};       // 'tab:pagina' -> { html, at }
    var inflight = {};    // 'tab:pagina' -> Promise (así no se pide dos veces lo mismo)
    var seq = 0;          // la última navegación manda: una respuesta vieja que llega tarde no pisa a la nueva
    var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

    function parse(url) {
        var u = new URL(url, location.href);
        var tab = u.searchParams.get('tab');
        if (ALL_TABS.indexOf(tab) < 0) tab = 'mes';
        var page = parseInt(u.searchParams.get('page') || '1', 10);
        if (!(page >= 1)) page = 1;
        return { tab: tab, page: page, key: tab + ':' + page };
    }

    function urlFor(tab, page) {
        return location.pathname + '?tab=' + tab + (page > 1 ? '&page=' + page : '');
    }

    var currentKey = parse(location.href).key;

    function fresh(key) {
        var hit = cache[key];
        return hit && (Date.now() - hit.at) < CACHE_MS ? hit : null;
    }

    function fetchPanel(key) {
        var hit = fresh(key);
        if (hit) return Promise.resolve(hit.html);
        if (inflight[key]) return inflight[key];
        var parts = key.split(':');
        var url = location.pathname + '?tab=' + parts[0] + '&page=' + parts[1] + '&partial=1';
        inflight[key] = fetch(url, { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' } }).then(function (r) {
            if (!r.ok) throw new Error('http ' + r.status);
            return r.text();
        }).then(function (html) {
            if (html.indexOf('id="topRows"') < 0) throw new Error('respuesta inesperada');
            cache[key] = { html: html, at: Date.now() };
            delete inflight[key];
            return html;
        }, function (err) {
            delete inflight[key];
            throw err;
        });
        return inflight[key];
    }

    // Marca la pestaña activa en las dos filas. "Más compras" siempre abre en "Este mes"; "Histórico" es solo si la gente lo quiere ver.
    function setActive(tab) {
        var purchases = PERIOD_TABS.indexOf(tab) >= 0;
        var kind = purchases ? 'compras' : tab;
        if (periodNav) {
            periodNav.classList.toggle('is-idle', !purchases);
            [].forEach.call(periodNav.querySelectorAll('.pf-tab'), function (a) {
                a.classList.toggle('is-active', purchases && a.getAttribute('data-top-tab') === tab);
            });
        }
        if (kindNav) {
            [].forEach.call(kindNav.querySelectorAll('.pf-tab'), function (a) {
                var k = a.getAttribute('data-top-kind');
                a.classList.toggle('is-active', k === kind);
            });
        }
    }

    function scrollToList() {
        var anchor = kindNav || panel;
        if (anchor.getBoundingClientRect().top >= 0) return;   // las pestañas ya se ven: no mover nada
        window.scrollTo({ top: Math.max(0, panel.getBoundingClientRect().top + window.pageYOffset - 150), behavior: reduceMotion ? 'auto' : 'smooth' });
    }

    function go(target, push, scroll) {
        var mine = ++seq;
        setActive(target.tab);
        if (!fresh(target.key)) {
            panel.classList.add('is-loading');
            panel.setAttribute('aria-busy', 'true');
        }
        fetchPanel(target.key).then(function (html) {
            if (mine !== seq) return;
            panel.innerHTML = html;
            panel.classList.remove('is-loading');
            panel.removeAttribute('aria-busy');
            if (push && target.key !== currentKey) history.pushState({ top: 1 }, '', urlFor(target.tab, target.page));
            currentKey = target.key;
            if (scroll) scrollToList();
        }).catch(function () {
            if (mine !== seq) return;
            location.href = urlFor(target.tab, target.page);   // algo falló: la página completa de siempre
        });
    }

    function linkTarget(a) {
        if (a.getAttribute('data-top-kind') === 'compras') return parse(urlFor('mes', 1));
        return parse(a.href);
    }

    function topLink(el) {
        return el && el.closest ? el.closest('.pf-tabs-period a.pf-tab, .pf-tabs-kind a.pf-tab, .pf-pager a') : null;
    }

    document.addEventListener('click', function (e) {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        var a = topLink(e.target);
        if (!a) return;
        var href = a.getAttribute('href');
        if (a.classList.contains('is-disabled') || !href || href === '#') { e.preventDefault(); return; }
        e.preventDefault();
        go(linkTarget(a), true, !!a.closest('.pf-pager'));
    });

    // Al apoyar el dedo o pasar el mouse se adelanta lo que se va a pedir (gana unas decenas de milisegundos).
    function warm(e) {
        var a = topLink(e.target);
        if (!a || a.classList.contains('is-disabled') || a.getAttribute('href') === '#') return;
        fetchPanel(linkTarget(a).key).catch(function () {});
    }
    document.addEventListener('pointerdown', warm, { passive: true });
    document.addEventListener('mouseover', warm, { passive: true });

    window.addEventListener('popstate', function () {
        go(parse(location.href), false, false);
    });

    // Con la página ya cargada se adelantan las otras pestañas (una por una, sin molestar). No se hace con "ahorro de datos".
    function prefetchOthers() {
        if (navigator.connection && navigator.connection.saveData) return;
        var queue = ALL_TABS.map(function (t) { return t + ':1'; }).filter(function (k) { return k !== currentKey; });
        (function next() {
            var key = queue.shift();
            if (!key) return;
            fetchPanel(key).catch(function () {}).then(function () { setTimeout(next, 150); });
        })();
    }
    var whenIdle = window.requestIdleCallback || function (fn) { return setTimeout(fn, 700); };
    window.addEventListener('load', function () { whenIdle(prefetchOthers, { timeout: 2500 }); });

    history.replaceState({ top: 1 }, '', location.href);
    setActive(parse(location.href).tab);
})();
