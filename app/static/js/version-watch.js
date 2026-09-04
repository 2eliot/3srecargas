/* ── 3S Recargas — refresco de pestañas viejas ───────────────────────────
   El problema: la gente deja la tienda abierta en el teléfono y vuelve
   días después. El navegador no recarga nada solo, así que siguen viendo
   (y usando) la web de cuando la abrieron: diseño, imágenes, paquetes,
   precios y tasa. Chrome en Android ni siquiera la descarta: la pestaña
   queda viva en memoria y la restaura tal cual.

   La regla: cada vez que el cliente vuelve a la pestaña, y además cada
   5 minutos mientras la tiene delante, se le pregunta al servidor qué
   versión hay. Si es otra y no hay nada a medio hacer, se recarga. Si el
   cliente está escribiendo su ID, tiene un paquete elegido, está subiendo
   el comprobante o tiene un popup abierto, NO se toca: perder un pago a
   medias es mucho peor que ver la web vieja un rato más. Se vuelve a
   intentar en el siguiente chequeo.

   La versión incluye el sello del catálogo (precios, tasa, imágenes de
   juegos y paquetes, métodos de pago, banners), así que un cambio del
   admin también dispara la recarga, no solo un deploy.
   ──────────────────────────────────────────────────────────────────── */

(function () {
    'use strict';

    var ESPERA_ENTRE_CHEQUEOS_MS = 90 * 1000;      // no preguntar en ráfaga
    var CADA_CUANTO_MS = 5 * 60 * 1000;            // chequeo periódico
    var CALMA_PARA_RECARGAR_MS = 15 * 1000;        // en el periódico, no recargar mientras interactúa

    var versionCargada = (window.APP_VERSION || '').toString();
    if (!versionCargada) return;   // sin sello no hay con qué comparar

    var ultimaActividad = Date.now();
    var ultimoChequeo = 0;
    var comprobando = false;
    var formularioTocado = false;
    var versionNuevaVista = false;   // ya sabemos que hay otra versión; falta el momento

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

    function recargarSiSePuede() {
        if (estaOcupado()) return false;
        window.location.reload();
        return true;
    }

    function comprobarVersion(desdePeriodico) {
        // Si ya sabemos que hay versión nueva, no hace falta volver a
        // preguntar: solo esperar el momento en que no moleste.
        if (versionNuevaVista) {
            if (desdePeriodico && Date.now() - ultimaActividad < CALMA_PARA_RECARGAR_MS) return;
            recargarSiSePuede();
            return;
        }
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
                versionNuevaVista = true;
                // En el chequeo periódico se espera a que deje de tocar la
                // pantalla; al volver a la pestaña se recarga de una vez.
                if (desdePeriodico && Date.now() - ultimaActividad < CALMA_PARA_RECARGAR_MS) return;
                recargarSiSePuede();
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
