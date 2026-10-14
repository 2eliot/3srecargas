/* ── 3S Recargas — selector de color moderno ───────────────────────────────────────────────────────────────────────
 *
 * Reemplaza el cuadro de color del navegador (viejo y feo) en los campos <input type="color" data-modern-color>: un botón con el color y
 * su código que abre un panel de cristal con el cuadro de color (saturación y brillo), la barra de tono, el código #RRGGBB, el gotero
 * (donde el navegador lo permite) y una paleta de colores rápidos.
 *
 * El <input> original SIGUE en la página (escondido): ahí se guarda el valor y por ahí se envía el formulario. Este archivo solo escribe su
 * .value y avisa con los eventos "input" (mientras se arrastra) y "change" (al soltar), así que la vista previa en vivo, el "hay cambios sin
 * guardar" y el envío del formulario siguen funcionando igual. Sin JavaScript queda el cuadro normal del navegador.
 */
(function () {
    'use strict';

    var PALETTE = ['#ff1a2e', '#e11d48', '#f97316', '#fbbf24', '#34d399', '#06b6d4', '#3b82f6', '#8b5cf6',
                   '#ec4899', '#ffffff', '#9ca3af', '#374151', '#000000', '#7a0010', '#ffe08a', '#a5f3fc'];
    var HEX_RE = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;
    var EYE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m2 22 1-1h3l9-9"/><path d="M3 21v-3l9-9"/><path d="m15 6 3.4-3.4a2.1 2.1 0 1 1 3 3L18 9l.4.4a2.1 2.1 0 1 1-3 3l-3.8-3.8a2.1 2.1 0 1 1 3-3l.4.4Z"/></svg>';
    var CARET_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

    var state = { h: 0, s: 0, v: 0 };   // el color que se está editando (tono 0-360, saturación y brillo 0-1)
    var pop = null;                     // el panel (uno solo para toda la página)
    var els = null;
    var active = null;                  // { input, trigger } del campo que tiene el panel abierto
    var entries = [];
    var dragging = false;

    /* ── Conversión de colores ── */
    function clamp(n, lo, hi) { return Math.min(hi, Math.max(lo, n)); }
    function normHex(value) {
        var m = HEX_RE.exec(String(value || '').trim());
        if (!m) return '';
        var h = m[1].toLowerCase();
        if (h.length === 3) h = h.charAt(0) + h.charAt(0) + h.charAt(1) + h.charAt(1) + h.charAt(2) + h.charAt(2);
        return '#' + h;
    }
    function hexToRgb(hex) { var n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
    function rgbToHex(r, g, b) {
        return '#' + [r, g, b].map(function (x) { var s = Math.round(clamp(x, 0, 255)).toString(16); return s.length < 2 ? '0' + s : s; }).join('');
    }
    function rgbToHsv(r, g, b) {
        r /= 255; g /= 255; b /= 255;
        var max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min, h = 0;
        if (d) {
            if (max === r) h = ((g - b) / d) % 6; else if (max === g) h = (b - r) / d + 2; else h = (r - g) / d + 4;
            h *= 60;
            if (h < 0) h += 360;
        }
        return { h: h, s: max ? d / max : 0, v: max };
    }
    function hsvToRgb(h, s, v) {
        var c = v * s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = v - c, r = 0, g = 0, b = 0;
        if (h < 60) { r = c; g = x; } else if (h < 120) { r = x; g = c; } else if (h < 180) { g = c; b = x; }
        else if (h < 240) { g = x; b = c; } else if (h < 300) { r = x; b = c; } else { r = c; b = x; }
        return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
    }
    function hsvFromHex(hex) {
        var rgb = hexToRgb(hex), hsv = rgbToHsv(rgb[0], rgb[1], rgb[2]);
        if (hsv.s === 0 || hsv.v === 0) hsv.h = state.h;   // en grises y negro el tono no se puede deducir: se conserva el que había
        return hsv;
    }
    function currentHex() { var rgb = hsvToRgb(state.h, state.s, state.v); return rgbToHex(rgb[0], rgb[1], rgb[2]); }

    /* ── El botón de cada campo ── */
    function paintEntry(entry) {
        var hex = normHex(entry.input.value) || '#000000';
        entry.trigger.style.setProperty('--mcp-c', hex);
        entry.hexEl.textContent = hex.toUpperCase();
    }

    function enhance(input) {
        if (!input || input.__mcp) return;
        input.__mcp = true;
        var trigger = document.createElement('button');
        trigger.type = 'button';
        trigger.className = 'mcp-trigger';
        trigger.setAttribute('aria-haspopup', 'dialog');
        trigger.setAttribute('aria-expanded', 'false');
        var label = input.id ? document.querySelector('label[for="' + input.id + '"]') : null;
        trigger.setAttribute('aria-label', 'Elegir color' + (label ? ': ' + label.textContent.trim() : ''));
        trigger.innerHTML = '<span class="mcp-chip"></span><span class="mcp-hex"></span><span class="mcp-caret">' + CARET_SVG + '</span>';
        var entry = { input: input, trigger: trigger, hexEl: trigger.querySelector('.mcp-hex') };
        input.classList.add('mcp-native');
        input.tabIndex = -1;
        input.setAttribute('aria-hidden', 'true');
        input.parentNode.insertBefore(trigger, input.nextSibling);
        input.parentNode.classList.add('has-mcp');
        paintEntry(entry);
        entries.push(entry);
        trigger.addEventListener('click', function () { if (active === entry) closePanel(true); else openPanel(entry); });
        input.addEventListener('input', function () { paintEntry(entry); });
        input.addEventListener('change', function () { paintEntry(entry); });
        // tocar el nombre del campo abre ESTE panel, no el cuadro del navegador
        if (label) label.addEventListener('click', function (evt) { evt.preventDefault(); trigger.click(); });
    }

    /* ── El panel ── */
    function buildPanel() {
        pop = document.createElement('div');
        pop.className = 'mcp-pop';
        pop.setAttribute('role', 'dialog');
        pop.setAttribute('aria-label', 'Elegir color');
        pop.tabIndex = -1;
        pop.hidden = true;
        pop.innerHTML =
            '<div class="mcp-sv" role="slider" tabindex="0" aria-label="Saturación y brillo (flechas para mover)"><i class="mcp-sv-thumb"></i></div>' +
            '<input class="mcp-hue" type="range" min="0" max="360" step="1" value="0" aria-label="Tono del color">' +
            '<div class="mcp-row">' +
                '<span class="mcp-preview"></span>' +
                '<input class="mcp-hexinput" type="text" maxlength="7" spellcheck="false" autocomplete="off" aria-label="Código del color (#RRGGBB)">' +
                '<button type="button" class="mcp-eye" aria-label="Tomar un color de la pantalla" hidden>' + EYE_SVG + '</button>' +
            '</div>' +
            '<div class="mcp-swatches"></div>' +
            '<button type="button" class="mcp-done">Listo</button>';
        document.body.appendChild(pop);
        els = {
            sv: pop.querySelector('.mcp-sv'), thumb: pop.querySelector('.mcp-sv-thumb'), hue: pop.querySelector('.mcp-hue'),
            preview: pop.querySelector('.mcp-preview'), hex: pop.querySelector('.mcp-hexinput'), eye: pop.querySelector('.mcp-eye'),
            swatches: pop.querySelector('.mcp-swatches'), done: pop.querySelector('.mcp-done')
        };
        PALETTE.forEach(function (color) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'mcp-sw';
            b.style.background = color;
            b.setAttribute('data-color', color);
            b.setAttribute('aria-label', 'Color ' + color);
            els.swatches.appendChild(b);
        });

        // cuadro de saturación y brillo (se arrastra con el dedo o el mouse)
        function fromPointer(evt) {
            var r = els.sv.getBoundingClientRect();
            state.s = clamp((evt.clientX - r.left) / r.width, 0, 1);
            state.v = 1 - clamp((evt.clientY - r.top) / r.height, 0, 1);
            apply(false);
        }
        els.sv.addEventListener('pointerdown', function (evt) {
            evt.preventDefault();
            dragging = true;
            try { els.sv.setPointerCapture(evt.pointerId); } catch (err) { /* nada */ }
            els.sv.focus({ preventScroll: true });
            fromPointer(evt);
        });
        els.sv.addEventListener('pointermove', function (evt) { if (dragging) fromPointer(evt); });
        function endDrag() { if (dragging) { dragging = false; apply(true); } }
        els.sv.addEventListener('pointerup', endDrag);
        els.sv.addEventListener('pointercancel', endDrag);
        els.sv.addEventListener('keydown', function (evt) {
            var step = evt.shiftKey ? 0.1 : 0.01, used = true;
            if (evt.key === 'ArrowLeft') state.s = clamp(state.s - step, 0, 1);
            else if (evt.key === 'ArrowRight') state.s = clamp(state.s + step, 0, 1);
            else if (evt.key === 'ArrowUp') state.v = clamp(state.v + step, 0, 1);
            else if (evt.key === 'ArrowDown') state.v = clamp(state.v - step, 0, 1);
            else used = false;
            if (used) { evt.preventDefault(); apply(true); }
        });
        // barra de tono
        els.hue.addEventListener('input', function () { state.h = Number(els.hue.value) || 0; apply(false); });
        els.hue.addEventListener('change', function () { apply(true); });
        // código escrito a mano
        els.hex.addEventListener('input', function () {
            var n = normHex(els.hex.value);
            els.hex.classList.toggle('is-bad', !n && els.hex.value.trim() !== '');
            if (n) { state = hsvFromHex(n); apply(false); }
        });
        els.hex.addEventListener('change', function () { apply(true); });
        els.hex.addEventListener('blur', function () { els.hex.classList.remove('is-bad'); paintPanel(); });
        els.hex.addEventListener('keydown', function (evt) { if (evt.key === 'Enter') { evt.preventDefault(); els.hex.blur(); } });
        // colores rápidos
        els.swatches.addEventListener('click', function (evt) {
            var b = evt.target.closest('.mcp-sw');
            if (!b) return;
            state = hsvFromHex(b.getAttribute('data-color'));
            apply(true);
        });
        // gotero (solo donde el navegador lo ofrece)
        if (window.EyeDropper) {
            els.eye.hidden = false;
            els.eye.addEventListener('click', function () {
                new window.EyeDropper().open().then(function (result) {
                    var n = normHex(result && result.sRGBHex);
                    if (n) { state = hsvFromHex(n); apply(true); }
                }).catch(function () { /* cancelado */ });
            });
        }
        els.done.addEventListener('click', function () { closePanel(true); });
    }

    // Dibuja el panel con el color actual (sin tocar el campo).
    function paintPanel() {
        if (!pop) return;
        var hex = currentHex();
        pop.style.setProperty('--mcp-h', String(Math.round(state.h)));
        els.thumb.style.left = (state.s * 100) + '%';
        els.thumb.style.top = ((1 - state.v) * 100) + '%';
        els.thumb.style.background = hex;
        els.hue.value = String(Math.round(state.h));
        els.preview.style.background = hex;
        if (document.activeElement !== els.hex) els.hex.value = hex.toUpperCase();
        els.sv.setAttribute('aria-valuetext', hex.toUpperCase());
        Array.prototype.forEach.call(els.swatches.children, function (b) { b.classList.toggle('is-on', b.getAttribute('data-color') === hex); });
    }

    // Escribe el color en el campo y avisa: "input" siempre, "change" solo al soltar/confirmar.
    function apply(commit) {
        paintPanel();
        if (!active) return;
        var hex = currentHex();
        if (active.input.value.toLowerCase() !== hex) {
            active.input.value = hex;
            paintEntry(active);
            active.input.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (commit) active.input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function place() {
        if (!pop || pop.hidden || !active) return;
        var r = active.trigger.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight, vw = window.innerWidth, vh = window.innerHeight, m = 8;
        var left = clamp(r.left, m, Math.max(m, vw - w - m));
        var top = r.bottom + 8;
        if (top + h > vh - m) {
            var above = r.top - h - 8;
            top = above >= m ? above : Math.max(m, vh - h - m);
        }
        pop.style.left = left + 'px';
        pop.style.top = top + 'px';
    }

    function onOutside(evt) {
        if (!pop || pop.contains(evt.target) || (active && active.trigger.contains(evt.target))) return;
        closePanel(false);
    }
    function onKey(evt) {
        if (evt.key !== 'Escape') return;
        evt.stopPropagation();   // cierra solo el selector, no la ventana de opciones de atrás
        evt.preventDefault();
        closePanel(true);
    }

    function openPanel(entry) {
        if (!pop) buildPanel();
        if (active && active !== entry) closePanel(false);
        active = entry;
        entry.trigger.setAttribute('aria-expanded', 'true');
        state = hsvFromHex(normHex(entry.input.value) || '#000000');
        pop.hidden = false;
        paintPanel();
        place();
        document.addEventListener('pointerdown', onOutside, true);
        document.addEventListener('keydown', onKey, true);
        window.addEventListener('resize', place);
        window.addEventListener('scroll', place, true);
        try { pop.focus({ preventScroll: true }); } catch (err) { /* nada */ }
    }

    function closePanel(refocus) {
        if (!pop || !active) return;
        var entry = active;
        active = null;
        dragging = false;
        pop.hidden = true;
        entry.trigger.setAttribute('aria-expanded', 'false');
        document.removeEventListener('pointerdown', onOutside, true);
        document.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', place);
        window.removeEventListener('scroll', place, true);
        if (refocus) { try { entry.trigger.focus({ preventScroll: true }); } catch (err) { /* nada */ } }
    }

    function enhanceAll(root) {
        Array.prototype.forEach.call((root || document).querySelectorAll('input[type="color"][data-modern-color]'), enhance);
    }

    // Para el resto de la página: si algo cambia el color por código (estilos listos, círculos de color...), se repinta el botón.
    window.ModernColor = {
        enhance: enhance,
        enhanceAll: enhanceAll,
        sync: function () {
            entries.forEach(paintEntry);
            if (active && pop && !pop.hidden && !dragging) {
                var n = normHex(active.input.value);
                if (n && n !== currentHex()) { state = hsvFromHex(n); paintPanel(); }
            }
        }
    };

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { enhanceAll(); });
    else enhanceAll();
})();
