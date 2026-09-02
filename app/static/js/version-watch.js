/* ── 3S Recargas — refresco de pestañas viejas ───────────────────────────
   El problema: la gente deja la tienda abierta en el teléfono, vuelve al
   día siguiente y sigue viendo la versión de ayer (precios, banners,
   paquetes). El navegador no recarga nada solo.

   La regla es simple y, sobre todo, prudente: al volver a la pestaña,
   si estuvo una hora sin que la tocaran Y hay una versión nueva
   desplegada Y no hay nada a medio llenar, se recarga sola. Si el cliente
   está escribiendo su ID, subiendo el comprobante o con un popup abierto,
   NO se toca: perder un pago a medias es mucho peor que ver la web vieja.
   ──────────────────────────────────────────────────────────────────── */

(function () {
    'use strict';

    var LIMITE_INACTIVIDAD_MS = 60 * 60 * 1000;   // 1 hora
    var ESPERA_ENTRE_CHEQUEOS_MS = 5 * 60 * 1000; // no preguntar en ráfaga

    var versionCargada = (window.APP_VERSION || '').toString();
    if (!versionCargada) return;   // sin sello no hay con qué comparar

    var ultimaActividad = Date.now();
    var ultimoChequeo = 0;
    var comprobando = false;
    var formularioTocado = false;

    ['click', 'keydown', 'mousemove', 'touchstart', 'scroll'].forEach(function (evento) {
        window.addEventListener(evento, function () {
            ultimaActividad = Date.now();
        }, { passive: true });
    });

    // Escribir marca el formulario como "en uso". Se mira aparte de la
    // actividad porque los campos pueden venir rellenados solos (los datos
    // recordados), y eso no es alguien tecleando.
    ['input', 'change'].forEach(function (evento) {
        document.addEventListener(evento, function (ev) {
            var el = ev.target;
            if (!el || !el.tagName) return;
            var tag = el.tagName.toUpperCase();
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
                formularioTocado = true;
                ultimaActividad = Date.now();
            }
        }, true);
    });

    function esVisible(el) {
        if (!el || el.hidden) return false;
        if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
        return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    }

    function hayDatosEscritos() {
        var campos = document.querySelectorAll('input, textarea, select');
        for (var i = 0; i < campos.length; i++) {
            var el = campos[i];
            var tipo = (el.type || '').toLowerCase();
            if (el.disabled || tipo === 'hidden') continue;
            if (tipo === 'file') {
                if (el.files && el.files.length) return true;   // comprobante ya elegido
                continue;
            }
            if (tipo === 'checkbox' || tipo === 'radio') continue;
            if (String(el.value || '').trim()) return true;
        }
        return false;
    }

    function hayAlgoAbierto() {
        if (document.body && document.body.classList.contains('drawer-open')) return true;
        var capas = document.querySelectorAll(
            '.store-popup-overlay, .payment-warning-modal, .nx-video-modal, ' +
            '.rdm-modal, .nx-modal, .modal, [role="dialog"]'
        );
        for (var i = 0; i < capas.length; i++) {
            if (esVisible(capas[i])) return true;
        }
        return false;
    }

    function estaOcupado() {
        // Un checkout que ya pasó del primer paso jamás se recarga.
        var etapa = document.getElementById('nxStage');
        if (etapa && etapa.value && etapa.value !== 'init') return true;
        if (hayAlgoAbierto()) return true;
        if (formularioTocado && hayDatosEscritos()) return true;
        return false;
    }

    function comprobarVersion() {
        if (comprobando) return;
        var ahora = Date.now();
        if (ahora - ultimoChequeo < ESPERA_ENTRE_CHEQUEOS_MS) return;
        comprobando = true;
        ultimoChequeo = ahora;

        fetch('/api/version', { cache: 'no-store', credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var versionServidor = (data && data.version ? data.version : '').toString();
                if (!versionServidor || versionServidor === versionCargada) return;
                // Se vuelve a mirar: entre la pregunta y la respuesta el
                // cliente pudo haber empezado a escribir.
                if (estaOcupado()) return;
                window.location.reload();
            })
            .catch(function () { /* sin red: la pestaña se queda como está */ })
            .finally(function () { comprobando = false; });
    }

    function quizasRefrescar() {
        if (Date.now() - ultimaActividad < LIMITE_INACTIVIDAD_MS) return;
        if (estaOcupado()) return;
        comprobarVersion();
    }

    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') quizasRefrescar();
    });

    // Volver con el botón "atrás" restaura la página tal cual estaba en
    // memoria (bfcache), que es el caso más viejo de todos.
    window.addEventListener('pageshow', function (ev) {
        if (ev.persisted) quizasRefrescar();
    });
})();
