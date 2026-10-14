/* ── 3S Recargas — selector de productos en "burbuja" de cristal ─────────────────────────────────────────────
 *
 * Cambia un <select> de verdad por un botón que, al tocarlo, abre una burbuja de cristal con las opciones. El <select>
 * original se queda escondido y es el que viaja en el formulario (así sigue funcionando igual en el servidor y en el
 * resto del script de la página, que escucha su evento "change"). Sin JavaScript se ve el <select> normal.
 *
 *   <select data-bubble-picker="idDelBoton" data-bubble="idDeLaBurbuja"> ... </select>
 *   <button id="idDelBoton"> <span class="nx-picker-value"></span> ... </button>
 *   <div id="idDeLaBurbuja" class="nx-bubble" hidden> ... <button class="nx-bubble-item" data-value="1"> ... </div>
 *
 * Todo texto se pinta con textContent.
 */
(function () {
    'use strict';

    var GAP = 12;               // separación entre el botón y la burbuja (aquí vive la flechita)
    var EDGE = 8;               // margen mínimo con el borde de la pantalla

    function init(select) {
        var btn = document.getElementById(select.getAttribute('data-bubble-picker'));
        var bubble = document.getElementById(select.getAttribute('data-bubble'));
        if (!btn || !bubble) return;

        var valueEl = btn.querySelector('.nx-picker-value');
        var arrow = bubble.querySelector('.nx-bubble-arrow');
        var items = Array.prototype.slice.call(bubble.querySelectorAll('.nx-bubble-item'));
        var closeBtn = bubble.querySelector('.nx-bubble-close');
        var placeholder = (select.options[0] && select.options[0].value === '') ? select.options[0].textContent.trim() : '';
        var isOpen = false;

        document.documentElement.classList.add('js-bubble-picker');
        document.body.appendChild(bubble);      // fuera de la tarjeta: así su cristal ve la página y no queda recortada

        function sync() {
            var chosen = items.filter(function (item) { return item.getAttribute('data-value') === select.value; })[0];
            if (valueEl) {
                valueEl.textContent = chosen ? chosen.getAttribute('data-name') : placeholder;
                valueEl.classList.toggle('is-placeholder', !chosen);
            }
            items.forEach(function (item) {
                var on = item === chosen;
                item.classList.toggle('is-selected', on);
                item.setAttribute('aria-selected', on ? 'true' : 'false');
            });
        }

        // La burbuja se coloca debajo del botón (o encima si abajo no cabe) y su flechita apunta al centro del botón.
        function place() {
            var r = btn.getBoundingClientRect();
            var width = Math.min(r.width, window.innerWidth - EDGE * 2);
            var left = Math.max(EDGE, Math.min(r.left, window.innerWidth - EDGE - width));
            bubble.style.width = width + 'px';
            bubble.style.left = left + 'px';

            var room = bubble.scrollHeight || 260;
            var below = window.innerHeight - r.bottom - GAP - EDGE;
            var above = r.top - GAP - EDGE;
            var useAbove = below < Math.min(room, 220) && above > below;
            var space = Math.max(120, useAbove ? above : below);
            bubble.style.maxHeight = space + 'px';
            var height = Math.min(room, space);
            bubble.style.top = (useAbove ? r.top - GAP - height : r.bottom + GAP) + 'px';
            bubble.classList.toggle('is-above', useAbove);

            var center = r.left + r.width / 2 - left;
            if (arrow) arrow.style.left = Math.max(20, Math.min(width - 20, center)) + 'px';
            bubble.style.transformOrigin = Math.max(20, Math.min(width - 20, center)) + 'px ' + (useAbove ? '100%' : '0');
        }

        function open() {
            if (isOpen) return;
            isOpen = true;
            bubble.hidden = false;
            place();
            void bubble.offsetWidth;                   // para que se anime la entrada
            bubble.classList.add('is-open');
            btn.setAttribute('aria-expanded', 'true');
            var current = bubble.querySelector('.nx-bubble-item.is-selected') || items[0];
            if (current) current.focus({ preventScroll: true });
            if (current && current.scrollIntoView) current.scrollIntoView({ block: 'nearest' });
        }

        function close(returnFocus) {
            if (!isOpen) return;
            isOpen = false;
            bubble.classList.remove('is-open');
            btn.setAttribute('aria-expanded', 'false');
            window.setTimeout(function () { if (!isOpen) bubble.hidden = true; }, 200);
            if (returnFocus) btn.focus({ preventScroll: true });
        }

        function choose(item) {
            select.value = item.getAttribute('data-value');
            sync();
            select.dispatchEvent(new Event('change', { bubbles: true }));
            close(true);
        }

        btn.addEventListener('click', function () { if (isOpen) close(false); else open(); });
        items.forEach(function (item) { item.addEventListener('click', function () { choose(item); }); });
        if (closeBtn) closeBtn.addEventListener('click', function () { close(true); });

        // Tocar fuera la cierra; Escape también. Con las flechas se recorre la lista.
        document.addEventListener('pointerdown', function (event) {
            if (isOpen && !bubble.contains(event.target) && !btn.contains(event.target)) close(false);
        });
        bubble.addEventListener('keydown', function (event) {
            var index = items.indexOf(document.activeElement);
            if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
            if (event.key === 'ArrowDown') { event.preventDefault(); (items[Math.min(items.length - 1, index + 1)] || items[0]).focus(); }
            if (event.key === 'ArrowUp') { event.preventDefault(); (items[Math.max(0, index - 1)] || items[0]).focus(); }
            if (event.key === 'Home') { event.preventDefault(); items[0].focus(); }
            if (event.key === 'End') { event.preventDefault(); items[items.length - 1].focus(); }
            if (event.key === 'Tab') close(false);
        });
        btn.addEventListener('keydown', function (event) {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
        });
        window.addEventListener('resize', function () { if (isOpen) place(); });
        window.addEventListener('scroll', function () { if (isOpen) place(); }, true);

        select.nxPicker = { open: open, close: close, isOpen: function () { return isOpen; } };
        sync();
    }

    Array.prototype.forEach.call(document.querySelectorAll('select[data-bubble-picker]'), init);
})();
