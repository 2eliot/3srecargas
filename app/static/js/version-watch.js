/* ── 3S Recargas — refresco de pestañas viejas ───────────────────────────
   El problema: la gente deja la tienda abierta en el teléfono y vuelve
   días después. El navegador no recarga nada solo, así que seguían viendo
   (y usando) la web de cuando la abrieron: diseño, imágenes, paquetes,
   precios y tasa. Chrome en Android ni siquiera la descarta: la pestaña
   queda viva en memoria y la restaura tal cual.

   Esquema (el mismo que usa King Recargas): cada 60 s con la pestaña
   visible, y cada vez que el cliente vuelve a ella, se pregunta al
   servidor por la versión. Son dos sellos: `deploy.catalogo`.

   - Cambió el CATÁLOGO (el admin tocó precios, tasa, paquetes, imágenes,
     métodos): se refrescan juegos, paquetes y tasa en silencio, sin
     recargar y sin perder nada de lo que el cliente tenga escrito. Lo hace
     main.js (window.nxSoftRefresh). Lo que main.js no repinta (banner,
     logo, métodos de pago) llega con una recarga completa en cuanto no
     moleste.
   - Cambió el DEPLOY (código nuevo): recarga completa, pero solo cuando no
     haya nada a medio hacer. Si el cliente está escribiendo su ID, tiene
     un paquete elegido, está subiendo el comprobante o tiene un popup
     abierto, NO se toca: perder un pago a medias es mucho peor que ver la
     web vieja un rato más. Se reintenta en el siguiente chequeo.
   ──────────────────────────────────────────────────────────────────── */

(function () {
    'use strict';

    var CADA_CUANTO_MS = 60 * 1000;                // sondeo con la pestaña visible
    var ESPERA_ENTRE_CHEQUEOS_MS = 20 * 1000;      // no preguntar en ráfaga al cambiar de pestaña
    var CALMA_PARA_RECARGAR_MS = 15 * 1000;        // en el periódico, no recargar mientras interactúa

    var versionCargada = (window.APP_VERSION || '').toString();
    if (!versionCargada) return;   // sin sello no hay con qué comparar

    var ultimaActividad = Date.now();
    var ultimoChequeo = 0;
    var comprobando = false;
    var formularioTocado = false;
    var recargaPendiente = false;   // hay versión nueva; falta el momento de recargar

    function parteDeploy(v) { var i = v.indexOf('.'); return i === -1 ? v : v.slice(0, i); }
    function parteCatalogo(v) { var i = v.indexOf('.'); return i === -1 ? '' : v.slice(i + 1); }

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
        // Con un paquete elegido ya está mirando los datos de pago.
        if (document.querySelector('.package-item.selected')) return true;
        if (hayAlgoAbierto()) return true;
        if (formularioTocado && hayDatosEscritos()) return true;
        return false;
    }

    function recargarSiSePuede(desdePeriodico) {
        if (desdePeriodico && Date.now() - ultimaActividad < CALMA_PARA_RECARGAR_MS) return false;
        if (estaOcupado()) return false;
        window.location.reload();
        return true;
    }

    function aplicarVersion(versionServidor, desdePeriodico) {
        if (parteDeploy(versionServidor) !== parteDeploy(versionCargada)) {
            // Código nuevo: solo sirve la recarga completa.
            recargaPendiente = true;
            recargarSiSePuede(desdePeriodico);
            return;
        }
        if (parteCatalogo(versionServidor) !== parteCatalogo(versionCargada)) {
            // El admin cambió algo del catálogo: se repinta al instante sin
            // recargar. La recarga completa queda pendiente para lo que
            // main.js no repinta, y solo cuando no moleste.
            versionCargada = versionServidor;
            if (typeof window.nxSoftRefresh === 'function') window.nxSoftRefresh();
            recargaPendiente = true;
            recargarSiSePuede(desdePeriodico);
        }
    }

    function comprobarVersion(desdePeriodico) {
        if (recargaPendiente && recargarSiSePuede(desdePeriodico)) return;
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
                aplicarVersion(versionServidor, desdePeriodico);
            })
            .catch(function () { /* sin red: la pestaña se queda como está */ })
            .finally(function () { comprobando = false; });
    }

    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') comprobarVersion(false);
    });

    // Volver con el botón "atrás" restaura la página tal cual estaba en
    // memoria (bfcache), que es el caso más viejo de todos.
    window.addEventListener('pageshow', function (ev) {
        if (ev.persisted) comprobarVersion(false);
    });

    setInterval(function () {
        if (document.visibilityState === 'visible') comprobarVersion(true);
    }, CADA_CUANTO_MS);
})();
