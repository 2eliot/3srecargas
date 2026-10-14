/* Modo liviano AUTOMÁTICO: solo para teléfonos MUY malos o con pocos fps (o cuando el cliente lo enciende a mano).
 *
 * Pedido del dueño (2026-10-14): el modo liviano no se activa solo por cualquier cosa. Solo si:
 *   1) el cliente lo enciende (interruptor del menú, o ?lite=1), o
 *   2) el teléfono es muy malo: 1 GB de memoria o menos, o 2 núcleos o menos (lo mira el script del <head>, base.html / _head_common.html), o
 *   3) se mide que va con pocos cuadros por segundo (FPS) o que su procesador es MUY lento (este archivo).
 * Ya NO cuentan el "ahorro de datos", la red lenta ni los 2 GB de memoria.
 *
 * - Corre una sola vez, con la página ya cargada y en reposo (≈3 s después):
 *     a) FPS: durante ~1,8 s se dibuja una carga de prueba invisible (un cristal con desenfoque sobre algo que se mueve, como las ventanas y la
 *        barra de la tienda) y se cuentan los cuadros por segundo reales. Menos de 20 fps = pocos fps (si sale bajo se repite una vez y vale el
 *        mejor de los dos). Si la pestaña pasa a segundo plano a mitad de la prueba, no se decide nada.
 *     b) Procesador: si los fps estuvieron bien, 5 veces una cuenta fija y vale el tiempo del MEDIO (la mediana). SLOW_MS = 45 ms.
 * - El resultado se guarda en localStorage ("nx-lite-auto"): positivo 3 días, negativo 7. Mientras dure, no se vuelve a medir.
 *   El script de <head> lo lee y activa el modo liviano desde el primer pintado de las visitas siguientes.
 * - Manda lo que el cliente decida a mano: ?lite=1 / ?lite=0 (guardado en "nx-lite"). ?lite=auto borra todo y vuelve a medir.
 *   El mismo ajuste se hace con el interruptor "Modo liviano" de hasta abajo del menú (base.html, [data-lite-toggle]); ahí también hay
 *   "Volver a automático" ([data-lite-auto]) para quien ya eligió a mano.
 * - OFERTA (pedido del dueño 2026-10-14): a un teléfono que NO llegó a activarse solo pero tiene poca memoria (2 GB o menos) o pocos núcleos (4 o menos)
 *   se le ofrece el modo liviano con una ventana ("Activar modo liviano" / "Ahora no"). Solo en la portada, solo en teléfonos y tabletas (pantalla
 *   táctil), nunca si ya está en modo liviano o el cliente ya eligió a mano, y si dice "Ahora no" no se vuelve a preguntar en 14 días ("nx-lite-offer").
 *   Espera a que termine la prueba de fps y a que no haya otra ventana abierta (la de la comunidad, el ranking, etc.). Si acepta, queda como si lo
 *   hubiera encendido en el menú ("nx-lite" = 1).
 * - Calibración del procesador (Chrome con la CPU frenada, esta PC mide ~6 ms): 1x=6 ms, 2x=13, 3x=20, 4x=27, 6x=42, 8x=60.
 *   SLOW_MS = 45 equivale a un equipo ~7 veces más lento que una PC normal (un teléfono MUY malo). Son cálculos con el frenado de CPU de Chrome,
 *   no teléfonos reales: si en la práctica sale muy sensible o poco, se ajusta SLOW_MS / FPS_MIN.
 */
(function () {
    'use strict';

    var KEY = 'nx-lite-auto';
    var SLOW_MS = 45;               // ms de la cuenta fija: por encima = procesador MUY lento
    var FPS_MIN = 20;               // cuadros por segundo: por debajo = pocos fps
    var FPS_WINDOW_MS = 1800;       // cuánto dura cada prueba de fps
    var TTL_SLOW = 3 * 24 * 3600 * 1000;
    var TTL_FAST = 7 * 24 * 3600 * 1000;
    var SAMPLES = 5;
    var OFFER_KEY = 'nx-lite-offer';
    var OFFER_COOLDOWN = 14 * 24 * 3600 * 1000;
    var OFFER_MAX_MEMORY_GB = 2;    // 2 GB o menos = poca memoria
    var OFFER_MAX_CORES = 4;        // 4 núcleos o menos = pocos núcleos
    var d = document.documentElement;
    var measuring = false;          // true desde que se planea la prueba hasta que termina (la oferta espera para no estorbarla)

    function readJSON(key) {
        try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; }
    }

    function forced() {
        try {
            var f = localStorage.getItem('nx-lite');
            return f === '1' || f === '0' ? f : null;
        } catch (e) { return null; }
    }

    function isLite() { return /(?:^|\s)lite(?:\s|$)/.test(d.className); }

    // Modo liviano: si el banner de la portada se mueve (video / GIF / WebP animado) NO se pausa: sigue viéndose moviéndose, pero con su copia liviana
    // (más chica y con menos cuadros, ver utils/banner_lite.py y templates/index.html) en lugar del original. Al apagar el modo vuelve el original.
    function applyLiteMedia() {
        var els = document.querySelectorAll('[data-lite-src]');
        for (var i = 0; i < els.length; i++) {
            var el = els[i];
            var want = el.getAttribute(isLite() ? 'data-lite-src' : 'data-full-src');
            if (!want || el.getAttribute('src') === want) { continue; }
            try {
                el.setAttribute('src', want);
                if (el.tagName === 'VIDEO') {
                    el.load();
                    var p = el.play();
                    if (p && p.catch) { p.catch(function () { /* el navegador no deja reproducir solo */ }); }
                }
            } catch (e) { /* sin soporte: se deja como está */ }
        }
    }

    // Interruptor del menú: lo que el cliente elige aquí se guarda en "nx-lite" y manda sobre la detección automática.
    function initToggle() {
        var btn = document.querySelector('[data-lite-toggle]');
        if (!btn) { return; }
        var status = btn.querySelector('[data-lite-status]');
        var auto = document.querySelector('[data-lite-auto]');
        function paint() {
            var on = isLite();
            btn.setAttribute('aria-checked', on ? 'true' : 'false');
            if (status) { status.textContent = on ? 'Activado: menos efectos' : 'Úsalo si la web va lenta'; }
            if (auto) { auto.hidden = forced() === null; }
        }
        btn.addEventListener('click', function () {
            var on = !isLite();
            if (on) { d.className += ' lite'; } else { d.className = d.className.replace(/(?:^|\s)lite(?=\s|$)/g, ''); }
            try { localStorage.setItem('nx-lite', on ? '1' : '0'); } catch (e) { /* sin almacenamiento: vale solo mientras esta página esté abierta */ }
            applyLiteMedia();
            paint();
        });
        if (auto) {
            auto.addEventListener('click', function () {
                try { localStorage.removeItem('nx-lite'); localStorage.removeItem('nx-lite-auto'); } catch (e) { /* nada que borrar */ }
                location.reload();   // al recargar se vuelve a decidir solo (teléfono muy malo) y a medir los fps y la velocidad
            });
        }
        window.addEventListener('nx-lite-change', function () { applyLiteMedia(); paint(); });   // la prueba automática lo puede activar con el menú abierto
        paint();
    }

    // La misma cuenta que se calibró (scratchpad/bench_fn.txt): llena y ordena listas de números; no toca la pantalla.
    function bench() {
        var t0 = performance.now();
        var total = 0;
        for (var round = 0; round < 40; round++) {
            var list = new Array(1500);
            for (var i = 0; i < 1500; i++) { list[i] = Math.sqrt(i * (round + 1)) + ((i * 7919) % 101); }
            list.sort(function (a, b) { return a - b; });
            total += list[(round * 37) % 1500];
        }
        if (total < 0) { return -1; }   // evita que el motor "ahorre" la cuenta
        return performance.now() - t0;
    }

    // `info` = { slow: true/false, ms: tiempo del procesador (0 si no se midió), fps: cuadros por segundo medidos }
    function finish(info) {
        if (!info.slow && !(info.ms > 0)) { return; }   // el navegador esconde la hora exacta (modo privacidad): sin dato, no se decide nada
        var slow = !!info.slow;
        try {
            localStorage.setItem(KEY, JSON.stringify({
                l: slow ? 1 : 0, ms: Math.round((info.ms || 0) * 10) / 10, fps: Math.round((info.fps || 0) * 10) / 10,
                x: Date.now() + (slow ? TTL_SLOW : TTL_FAST)
            }));
        } catch (e) { /* sin almacenamiento: se vuelve a medir la próxima vez */ }
        if (slow && forced() !== '0') {
            d.className += ' lite';
            try { window.dispatchEvent(new Event('nx-lite-change')); } catch (e) { /* navegador muy viejo: el interruptor se actualiza al abrir otra página */ }
        }
        if (info.ms > 0) { window.nxLiteMs = info.ms; }
        if (info.fps > 0) { window.nxLiteFps = info.fps; }
    }

    // Cuántos cuadros por segundo da este teléfono con una carga parecida a la de la tienda. Devuelve (por `done`) los fps, o null si no se pudo decidir
    // (pestaña en segundo plano, sin requestAnimationFrame o muy pocos cuadros).
    function sampleFps(done) {
        if (!window.requestAnimationFrame) { done(null); return; }
        var box = document.createElement('div');
        var mover = document.createElement('div');
        box.setAttribute('aria-hidden', 'true');
        box.style.cssText = 'position:fixed;left:0;top:0;width:260px;height:260px;overflow:hidden;opacity:.02;pointer-events:none;z-index:2147483000;' +
            '-webkit-backdrop-filter:blur(12px) saturate(180%);backdrop-filter:blur(12px) saturate(180%)';
        mover.style.cssText = 'width:520px;height:260px;background:linear-gradient(90deg,#f33,#33f,#3f3,#f33)';
        box.appendChild(mover);
        (document.body || d).appendChild(box);
        var times = [], skipped = 0;
        function end(value) {
            try { box.parentNode.removeChild(box); } catch (e) { /* ya no estaba */ }
            done(value);
        }
        function step(ts) {
            if (document.hidden) { end(null); return; }
            if (skipped < 3) { skipped++; requestAnimationFrame(step); return; }   // los primeros cuadros siempre salen lentos (se prepara la capa)
            times.push(ts);
            mover.style.transform = 'translate3d(' + (-((ts / 5) % 260)) + 'px,0,0)';
            if (ts - times[0] >= FPS_WINDOW_MS || times.length >= 240) {
                var frames = times.length - 1;
                end(frames >= 5 ? frames / ((times[times.length - 1] - times[0]) / 1000) : null);
                return;
            }
            requestAnimationFrame(step);
        }
        requestAnimationFrame(step);
    }

    // Dos intentos si el primero sale bajo (un tropiezo del teléfono no decide): vale el mejor.
    function measureFps(done) {
        sampleFps(function (first) {
            if (first === null || first >= FPS_MIN) { done(first); return; }
            sampleFps(function (second) { done(second === null ? first : Math.max(first, second)); });
        });
    }

    function measureCpu(fps) {
        var times = [], n = 0;
        (function next() {
            var ms;
            try { ms = bench(); } catch (e) { measuring = false; return; }
            if (ms > 0) { times.push(ms); }
            if (++n < SAMPLES) { setTimeout(next, 250); return; }
            times.sort(function (a, b) { return a - b; });
            var median = times.length ? times[Math.floor(times.length / 2)] : 0;
            finish({ slow: median >= SLOW_MS, ms: median, fps: fps });
            measuring = false;
        })();
    }

    function measure() {
        measureFps(function (fps) {
            if (fps === null) { measuring = false; return; }                // no se pudo medir: no se decide nada (se reintenta en otra visita)
            if (fps < FPS_MIN) { finish({ slow: true, ms: 0, fps: fps }); measuring = false; return; }   // pocos fps: modo liviano
            measureCpu(fps);                                                // fps bien: se mira si el procesador es MUY lento
        });
    }

    // ── Oferta del modo liviano ──────────────────────────────────────────────────
    function isTouchDevice() {
        try {
            if (navigator.maxTouchPoints > 0) { return true; }
            return !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
        } catch (e) { return false; }
    }

    function lowSpecs() {
        var mem = navigator.deviceMemory, cores = navigator.hardwareConcurrency;
        return (mem > 0 && mem <= OFFER_MAX_MEMORY_GB) || (cores > 0 && cores <= OFFER_MAX_CORES);
    }

    function offerCooledDown() {
        try {
            var last = parseInt(localStorage.getItem(OFFER_KEY) || '0', 10);
            return !last || (Date.now() - last) >= OFFER_COOLDOWN;
        } catch (e) { return true; }
    }

    function offerWanted() {
        if (isLite() || forced() !== null) { return false; }                 // ya está en modo liviano, o el cliente ya eligió
        if (location.pathname !== '/' || !document.querySelector('[data-lite-toggle]')) { return false; }   // solo la portada de la tienda
        return isTouchDevice() && lowSpecs() && offerCooledDown();
    }

    // ¿Hay otra ventana abierta (comunidad, ranking, puntos, avisos...)? Entonces la oferta espera.
    function otherDialogOpen() {
        if (document.body && document.body.classList.contains('modal-open')) { return true; }
        var drawer = document.getElementById('siteDrawer');   // el menú lateral no es un diálogo, pero también es una ventana abierta
        if (drawer && drawer.getAttribute('aria-hidden') === 'false') { return true; }
        var dialogs = document.querySelectorAll('[role="dialog"]');
        for (var i = 0; i < dialogs.length; i++) {
            if (dialogs[i].classList.contains('lite-offer-card')) { continue; }
            if (dialogs[i].getClientRects().length > 0) { return true; }   // un diálogo dentro de algo oculto no tiene rectángulos
        }
        return false;
    }

    function showOffer() {
        var previous = document.activeElement;
        var overlay = document.createElement('div');
        overlay.className = 'community-popup-overlay lite-offer';
        overlay.innerHTML =
            '<div class="community-popup-card lite-offer-card" role="dialog" aria-modal="true" aria-labelledby="liteOfferTitle" aria-describedby="liteOfferText">' +
            '<div class="community-popup-icon" aria-hidden="true"><svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg></div>' +
            '<h3 id="liteOfferTitle">¿Quieres que la web vaya más rápido?</h3>' +
            '<p class="community-popup-lead" id="liteOfferText">Tu teléfono parece tener poca memoria o un procesador lento. Con el modo liviano la tienda usa menos efectos y se ve más fluida. Puedes quitarlo cuando quieras desde el menú.</p>' +
            '<button type="button" class="community-popup-join lite-offer-btn" data-lite-offer="accept"><span>Activar modo liviano</span></button>' +
            '<button type="button" class="community-popup-continue lite-offer-btn" data-lite-offer="decline">Ahora no</button>' +
            '</div>';
        var accept = overlay.querySelector('[data-lite-offer="accept"]');
        var decline = overlay.querySelector('[data-lite-offer="decline"]');

        function close() {
            document.removeEventListener('keydown', onKey, true);
            window.removeEventListener('nx-lite-change', onChange);
            if (overlay.parentNode) { overlay.parentNode.removeChild(overlay); }
            try { if (previous && previous.focus && document.contains(previous)) { previous.focus(); } } catch (e) { /* sin foco que devolver */ }
        }
        function onAccept() {
            if (!isLite()) { d.className += ' lite'; }
            try { localStorage.setItem('nx-lite', '1'); } catch (e) { /* sin almacenamiento: vale solo mientras esta página esté abierta */ }
            applyLiteMedia();
            try { window.dispatchEvent(new Event('nx-lite-change')); } catch (e) { /* el interruptor se actualiza al abrir otra página */ }
            close();
        }
        function onDecline() {
            try { localStorage.setItem(OFFER_KEY, String(Date.now())); } catch (e) { /* se volverá a preguntar */ }
            close();
        }
        function onChange() { if (isLite()) { close(); } }   // se activó por otro lado (la prueba de fps, el menú): ya no hace falta preguntar
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onDecline(); return; }
            if (e.key === 'Tab') {   // el foco se queda dentro de la ventana
                var focusOn = document.activeElement;
                if (e.shiftKey && focusOn === accept) { e.preventDefault(); decline.focus(); }
                else if (!e.shiftKey && focusOn === decline) { e.preventDefault(); accept.focus(); }
                else if (focusOn !== accept && focusOn !== decline) { e.preventDefault(); accept.focus(); }
            }
        }
        accept.addEventListener('click', onAccept);
        decline.addEventListener('click', onDecline);
        overlay.addEventListener('click', function (e) { if (e.target === overlay) { onDecline(); } });   // tocar fuera = "Ahora no"
        document.addEventListener('keydown', onKey, true);
        window.addEventListener('nx-lite-change', onChange);
        document.body.appendChild(overlay);
        try { accept.focus(); } catch (e) { /* sin foco */ }
    }

    // Revisa cada ratito si ya se puede preguntar: la prueba de fps terminó y no hay otra ventana abierta. Se rinde a los 2 minutos (queda para otra visita).
    function scheduleOffer() {
        if (!offerWanted()) { return; }
        var tries = 0;
        (function check() {
            if (!offerWanted() || ++tries > 80) { return; }
            if (measuring || document.hidden || otherDialogOpen()) { setTimeout(check, 1500); return; }
            showOffer();
        })();
    }

    function whenReady(fn) {
        function go() {
            if (document.hidden) {   // pestaña en segundo plano: los relojes van lentos y la prueba saldría mal; se espera a que la vean
                document.addEventListener('visibilitychange', function once() {
                    if (!document.hidden) { document.removeEventListener('visibilitychange', once); setTimeout(fn, 1500); }
                });
                return;
            }
            if (window.requestIdleCallback) { window.requestIdleCallback(fn, { timeout: 5000 }); } else { fn(); }
        }
        function afterLoad() { setTimeout(go, 2500); }
        if (document.readyState === 'complete') { afterLoad(); } else { window.addEventListener('load', afterLoad); }
    }

    initToggle();
    applyLiteMedia();

    // ¿Hace falta medir? No si el cliente lo fijó a mano, si ya está en modo liviano (teléfono muy malo) o si hay una medición vigente.
    var needsMeasure = forced() === null && !isLite();
    if (needsMeasure) {
        var cached = readJSON(KEY);
        if (cached && cached.x > Date.now()) { needsMeasure = false; }
    }
    if (needsMeasure && (!window.performance || !performance.now)) { needsMeasure = false; }
    if (needsMeasure) {
        measuring = true;   // la oferta espera a que termine la prueba (si sale lento, el modo liviano se activa solo y ya no se pregunta)
        whenReady(measure);
    }
    // La oferta empieza a mirar ~3 s después de cargar la página (para no tapar el primer pintado).
    if (document.readyState === 'complete') { setTimeout(scheduleOffer, 3000); } else { window.addEventListener('load', function () { setTimeout(scheduleOffer, 3000); }); }
})();
