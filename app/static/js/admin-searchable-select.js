/* ── 3S Recargas (admin) — desplegables con buscador ─────────────────────────────────────────────────────
 *
 * Cuando una lista tiene muchísimas opciones (los paquetes de un proveedor pueden ser cientos), un <select> normal obliga a bajar y bajar.
 * `nxSearchableSelect(select)` lo deja con una cajita para escribir: al tocar el desplegable se abre una lista con un buscador arriba y
 * van quedando solo las opciones que contienen lo escrito (sin importar mayúsculas ni acentos; varias palabras = todas deben estar).
 *
 * El <select> original NO se quita: se esconde y sigue siendo la fuente de verdad (su valor, sus opciones y su evento "change"), así que
 * el resto de la página funciona igual. Si la página cambia sus opciones, su valor o lo apaga (disabled), el buscador se pone al día solo.
 * Teclado: flechas para moverse, Enter para elegir, Escape para cerrar. Todo texto entra con textContent (nunca como HTML).
 */
(function () {
    'use strict';

    var STYLE_ID = 'nx-ss-style';
    var MAX_SHOWN = 300;   // con listas enormes se dibujan las primeras; al escribir se filtra sobre TODAS
    var openInstance = null;

    function injectStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var css = [
            '.nx-ss { position: relative; min-width: 0; }',
            '.nx-ss-btn { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 8px; text-align: left; cursor: pointer;',
            '  padding: 8px 10px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #e2e8f0; font: inherit; font-size: 13px; }',
            '.nx-ss-btn.is-big { padding: 10px 12px; border-radius: 8px; border-color: #334155; background: #1e293b; font-size: 14px; }',
            '.nx-ss-btn:hover:not(:disabled) { border-color: #64748b; }',
            '.nx-ss-btn:focus-visible { outline: 2px solid #6366f1; outline-offset: 1px; }',
            '.nx-ss-btn:disabled { opacity: .5; cursor: not-allowed; }',
            '.nx-ss-btn.is-placeholder .nx-ss-label { color: #94a3b8; }',
            '.nx-ss-label { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
            '.nx-ss-caret { flex: 0 0 auto; width: 10px; height: 10px; border-right: 2px solid #94a3b8; border-bottom: 2px solid #94a3b8; transform: rotate(45deg) translateY(-2px); }',
            '.nx-ss.is-open .nx-ss-caret { transform: rotate(-135deg) translateY(-2px); }',
            '.nx-ss-panel { position: absolute; z-index: 1000; left: 0; top: calc(100% + 4px); min-width: 100%; width: max-content; max-width: min(560px, 92vw);',
            '  background: #0f172a; border: 1px solid #6366f1; border-radius: 10px; box-shadow: 0 12px 32px rgba(0,0,0,.55); overflow: hidden; }',
            '.nx-ss-panel.is-up { top: auto; bottom: calc(100% + 4px); }',
            '.nx-ss-search { display: block; width: 100%; box-sizing: border-box; padding: 10px 12px; border: 0; border-bottom: 1px solid #334155; outline: none;',
            '  background: #111c33; color: #e2e8f0; font: inherit; font-size: 13px; }',
            '.nx-ss-search::placeholder { color: #64748b; }',
            '.nx-ss-list { max-height: 280px; overflow-y: auto; overscroll-behavior: contain; }',
            '.nx-ss-opt { display: block; width: 100%; text-align: left; padding: 8px 12px; border: 0; background: transparent; color: #e2e8f0; font: inherit; font-size: 13px; cursor: pointer; line-height: 1.35; }',
            '.nx-ss-opt:hover, .nx-ss-opt.is-active { background: #334155; }',
            '.nx-ss-opt.is-selected { color: #a5b4fc; font-weight: 700; }',
            '.nx-ss-opt.is-empty-choice { color: #94a3b8; }',
            '.nx-ss-note { padding: 9px 12px; font-size: 12px; color: #94a3b8; }',
            '.nx-ss-count { padding: 6px 12px; font-size: 11px; color: #64748b; border-top: 1px solid #1e293b; }'
        ].join('\n');
        var style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = css;
        document.head.appendChild(style);
    }

    // Sin mayúsculas ni acentos: "pase booyah" encuentra "Pase Booyah Premium".
    function fold(text) {
        var s = String(text || '').toLowerCase();
        try { s = s.normalize('NFD').replace(/[̀-ͯ]/g, ''); } catch (e) { /* navegador viejo: se busca tal cual */ }
        return s;
    }

    function closeOpen() {
        if (openInstance) openInstance.close();
    }

    document.addEventListener('mousedown', function (evt) {
        if (openInstance && !openInstance.root.contains(evt.target)) closeOpen();
    }, true);
    document.addEventListener('focusin', function (evt) {
        if (openInstance && !openInstance.root.contains(evt.target)) closeOpen();
    });

    function enhance(select, options) {
        if (!select || select.tagName !== 'SELECT') return null;
        if (select._nxSs) return select._nxSs;
        injectStyle();
        options = options || {};

        // El contenedor hereda las clases de posición del select (grp, pkg...), pero NO la caja (map-select): la caja la dibuja el botón.
        var classes = (select.className || '').split(/\s+/).filter(Boolean);
        var isBig = classes.indexOf('map-select') !== -1;
        var root = document.createElement('div');
        root.className = 'nx-ss ' + classes.filter(function (c) { return c !== 'map-select'; }).join(' ');
        if (isBig) { root.style.width = '100%'; root.style.maxWidth = '400px'; }

        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'nx-ss-btn' + (options.big || isBig ? ' is-big' : '');
        btn.setAttribute('aria-haspopup', 'listbox');
        btn.setAttribute('aria-expanded', 'false');
        var label = document.createElement('span');
        label.className = 'nx-ss-label';
        var caret = document.createElement('span');
        caret.className = 'nx-ss-caret';
        caret.setAttribute('aria-hidden', 'true');
        btn.appendChild(label);
        btn.appendChild(caret);
        root.appendChild(btn);

        var panel = null, search = null, list = null, active = -1, shown = [];
        var api = null;

        function currentOption() {
            return select.options[select.selectedIndex] || null;
        }

        function sync() {
            var opt = currentOption();
            var text = opt ? opt.textContent : '';
            var isPlaceholder = !opt || opt.value === '';
            label.textContent = text || (options.placeholder || 'Selecciona…');
            btn.classList.toggle('is-placeholder', isPlaceholder);
            btn.disabled = !!select.disabled;
            if (select.disabled && panel) close();
        }

        function allOptions() {
            return Array.prototype.map.call(select.options, function (o, index) {
                return { index: index, value: o.value, text: o.textContent, folded: fold(o.textContent) };
            });
        }

        function setActive(pos, scroll) {
            var nodes = list ? list.querySelectorAll('.nx-ss-opt') : [];
            if (active >= 0 && nodes[active]) nodes[active].classList.remove('is-active');
            active = pos;
            if (nodes[active]) {
                nodes[active].classList.add('is-active');
                if (scroll) {
                    var n = nodes[active], top = n.offsetTop, bottom = top + n.offsetHeight;
                    if (top < list.scrollTop) list.scrollTop = top;
                    else if (bottom > list.scrollTop + list.clientHeight) list.scrollTop = bottom - list.clientHeight;
                }
            }
        }

        function renderList() {
            if (!panel) return;
            var terms = fold(search.value).split(/\s+/).filter(Boolean);
            var matches = allOptions().filter(function (o) {
                if (!terms.length) return true;
                return terms.every(function (t) { return o.folded.indexOf(t) !== -1; });
            });
            shown = matches.slice(0, MAX_SHOWN);
            list.innerHTML = '';
            active = -1;
            if (!matches.length) {
                var none = document.createElement('div');
                none.className = 'nx-ss-note';
                none.textContent = 'No hay resultados para "' + search.value.trim() + '".';
                list.appendChild(none);
                return;
            }
            shown.forEach(function (o, i) {
                var b = document.createElement('button');
                b.type = 'button';
                b.className = 'nx-ss-opt' + (o.index === select.selectedIndex ? ' is-selected' : '') + (o.value === '' ? ' is-empty-choice' : '');
                b.setAttribute('role', 'option');
                b.dataset.pos = String(i);
                b.textContent = o.text;
                b.addEventListener('mousemove', function () { if (active !== i) setActive(i, false); });
                b.addEventListener('click', function () { choose(o); });
                list.appendChild(b);
            });
            if (matches.length > shown.length) {
                var more = document.createElement('div');
                more.className = 'nx-ss-count';
                more.textContent = 'Mostrando ' + shown.length + ' de ' + matches.length + ': escribe para encontrar el que buscas.';
                list.appendChild(more);
            }
            var selectedPos = -1;
            shown.forEach(function (o, i) { if (o.index === select.selectedIndex) selectedPos = i; });
            setActive(terms.length ? 0 : (selectedPos >= 0 ? selectedPos : 0), true);
        }

        function choose(o) {
            var changed = select.selectedIndex !== o.index;
            select.selectedIndex = o.index;
            close();
            sync();
            if (changed) select.dispatchEvent(new Event('change', { bubbles: true }));
            btn.focus();
        }

        function open() {
            if (openInstance && openInstance !== api) closeOpen();
            if (select.disabled || panel) return;
            panel = document.createElement('div');
            panel.className = 'nx-ss-panel';
            search = document.createElement('input');
            search.type = 'text';
            search.className = 'nx-ss-search';
            search.placeholder = options.searchPlaceholder || 'Escribe para buscar…';
            search.setAttribute('autocomplete', 'off');
            search.setAttribute('aria-label', 'Buscar en la lista');
            list = document.createElement('div');
            list.className = 'nx-ss-list';
            list.setAttribute('role', 'listbox');
            panel.appendChild(search);
            panel.appendChild(list);
            root.appendChild(panel);
            root.classList.add('is-open');
            btn.setAttribute('aria-expanded', 'true');
            // Si no cabe hacia abajo, se abre hacia arriba.
            var rect = root.getBoundingClientRect();
            if (window.innerHeight - rect.bottom < 360 && rect.top > 360) panel.classList.add('is-up');
            search.addEventListener('input', renderList);
            search.addEventListener('keydown', onKey);
            renderList();
            openInstance = api;
            search.focus();
        }

        function close() {
            if (!panel) return;
            root.removeChild(panel);
            panel = search = list = null;
            active = -1;
            root.classList.remove('is-open');
            btn.setAttribute('aria-expanded', 'false');
            if (openInstance === api) openInstance = null;
        }

        function onKey(evt) {
            if (evt.key === 'Escape') { evt.preventDefault(); close(); btn.focus(); return; }
            if (evt.key === 'Tab') { close(); return; }
            var nodes = list.querySelectorAll('.nx-ss-opt');
            if (!nodes.length) return;
            if (evt.key === 'ArrowDown') { evt.preventDefault(); setActive(Math.min(active + 1, nodes.length - 1), true); }
            else if (evt.key === 'ArrowUp') { evt.preventDefault(); setActive(Math.max(active - 1, 0), true); }
            else if (evt.key === 'Enter') { evt.preventDefault(); if (shown[active]) choose(shown[active]); }
        }

        btn.addEventListener('click', function () { if (panel) close(); else open(); });
        btn.addEventListener('keydown', function (evt) {
            if (evt.key === 'ArrowDown' || evt.key === 'ArrowUp') { evt.preventDefault(); open(); }
        });

        // El valor se puede poner desde el código de la página (select.value = ...): el buscador lo refleja al instante.
        var nativeDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
        Object.defineProperty(select, 'value', {
            configurable: true,
            get: function () { return nativeDesc.get.call(this); },
            set: function (v) { nativeDesc.set.call(this, v); sync(); }
        });
        select.addEventListener('change', sync);
        if (window.MutationObserver) {
            new MutationObserver(function () { sync(); if (panel) renderList(); })
                .observe(select, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled'] });
        }

        select.parentNode.insertBefore(root, select);
        select.style.display = 'none';
        select.setAttribute('aria-hidden', 'true');
        select.tabIndex = -1;
        root.appendChild(select);   // sigue dentro del mismo contenedor (formularios, etiquetas)

        api = { root: root, select: select, open: open, close: close, sync: sync };
        select._nxSs = api;
        sync();
        return api;
    }

    window.nxSearchableSelect = enhance;
})();
