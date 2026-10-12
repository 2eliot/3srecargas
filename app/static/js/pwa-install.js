/* ── 3S Recargas — instalar la web como app ───────────────────────────────
 *
 * Los botones con `data-pwa-install` (junto a la campanita y en el menú) dejan
 * al cliente instalar la tienda en la pantalla de inicio de su celular.
 *
 *  - Android / Chrome / Edge / Samsung: el navegador avisa con
 *    `beforeinstallprompt` cuando se puede instalar; el botón aparece y, al
 *    tocarlo, abre el cuadro de instalación del propio navegador.
 *  - iPhone / iPad: Apple no permite instalar con un botón, así que el botón
 *    muestra los pasos (Compartir > Añadir a pantalla de inicio).
 *  - Si la tienda ya está instalada (se abrió como app) o el navegador no
 *    puede instalarla, los botones no se muestran.
 *
 * Todo texto entra con textContent, nunca innerHTML.
 */
(function () {
    'use strict';

    var buttons = document.querySelectorAll('[data-pwa-install]');
    if (!buttons.length) return;
    var headerRight = document.querySelector('.header-right');
    var deferredPrompt = null;
    var helpOpener = null;

    function isStandalone() {
        try {
            if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
        } catch (_) { /* nada */ }
        return window.navigator.standalone === true;
    }

    function isIOS() {
        var ua = navigator.userAgent || '';
        // iPadOS se presenta como un Mac, pero con pantalla táctil.
        return /iphone|ipad|ipod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    function setVisible(show) {
        Array.prototype.forEach.call(buttons, function (btn) { btn.hidden = !show; });
        // Con tres botones en el header se achican un poco para que quepan en pantallas angostas.
        if (headerRight) headerRight.classList.toggle('has-install', !!show);
    }

    // Ya instalada (abierta como app): no hay nada que ofrecer.
    if (isStandalone()) {
        document.documentElement.classList.add('is-pwa');
        return;
    }

    // Instalar exige un service worker registrado; el de las notificaciones push ya sirve
    // y registrarlo otra vez es inocuo.
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js').catch(function () { /* sin service worker no se instala */ });
    }

    // ── Pasos para iPhone / iPad ─────────────────────────────────────────
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function closeHelp() {
        var overlay = document.getElementById('pwaHelp');
        if (overlay) overlay.remove();
        document.removeEventListener('keydown', onHelpKey);
        if (helpOpener && helpOpener.focus) helpOpener.focus();
        helpOpener = null;
    }

    function onHelpKey(evt) {
        if (evt.key === 'Escape') closeHelp();
    }

    function openHelp(opener) {
        if (document.getElementById('pwaHelp')) return;
        helpOpener = opener || null;

        var overlay = el('div', 'pwa-help');
        overlay.id = 'pwaHelp';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-labelledby', 'pwaHelpTitle');

        var card = el('div', 'pwa-help-card');
        var title = el('h3', 'pwa-help-title', 'Instala la app de 3S Recargas');
        title.id = 'pwaHelpTitle';
        card.appendChild(title);
        card.appendChild(el('p', 'pwa-help-lead', 'Queda en la pantalla de inicio de tu iPhone, como cualquier app. Son 3 pasos:'));

        var steps = el('ol', 'pwa-help-steps');
        [
            'Toca el botón Compartir de tu navegador: el cuadrito con una flecha hacia arriba (abajo en el centro, o arriba a la derecha).',
            'Baja y elige «Añadir a pantalla de inicio».',
            'Toca «Añadir». ¡Listo! Verás el ícono de 3S Recargas en tu pantalla.'
        ].forEach(function (text) { steps.appendChild(el('li', null, text)); });
        card.appendChild(steps);

        var ok = el('button', 'pwa-help-ok', 'Entendido');
        ok.type = 'button';
        ok.addEventListener('click', closeHelp);
        card.appendChild(ok);

        overlay.appendChild(card);
        // Tocar fuera de la tarjeta también cierra.
        overlay.addEventListener('click', function (evt) { if (evt.target === overlay) closeHelp(); });
        document.body.appendChild(overlay);
        document.addEventListener('keydown', onHelpKey);
        ok.focus();
    }

    // ── Instalar ─────────────────────────────────────────────────────────
    function install(evt) {
        var opener = evt && evt.currentTarget ? evt.currentTarget : null;
        if (deferredPrompt) {
            var promptEvent = deferredPrompt;
            deferredPrompt = null;   // el navegador solo deja usar cada aviso una vez
            promptEvent.prompt();
            var choice = promptEvent.userChoice;
            if (choice && choice.then) {
                choice.then(function () { setVisible(false); }, function () { setVisible(false); });
            } else {
                setVisible(false);
            }
            return;
        }
        if (isIOS()) openHelp(opener);
    }

    Array.prototype.forEach.call(buttons, function (btn) { btn.addEventListener('click', install); });

    window.addEventListener('beforeinstallprompt', function (evt) {
        evt.preventDefault();   // el botón lo ofrecemos nosotros, no el mini aviso del navegador
        deferredPrompt = evt;
        setVisible(true);
    });

    window.addEventListener('appinstalled', function () {
        deferredPrompt = null;
        setVisible(false);
        document.documentElement.classList.add('is-pwa');
    });

    if (isIOS()) setVisible(true);
})();
