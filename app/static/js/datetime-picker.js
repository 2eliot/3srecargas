/* ── 3S Recargas — selector de fecha y hora propio (reemplaza el calendario del navegador en el panel) ─────────────────────────────────────
 *
 * Mejora solos los campos <input type="date"> y <input type="datetime-local"> (se salta los que lleven data-native-date). El campo original se queda en la
 * página, escondido: ahí vive el valor y por ahí se envía el formulario, con el MISMO formato de siempre ('AAAA-MM-DD' o 'AAAA-MM-DDTHH:MM'), así que no cambia
 * nada del servidor. Respeta min, max y required, avisa con los eventos input/change y se entera cuando otro script le cambia el valor (input.value = ...).
 * Todo en español: meses, días, "Hoy", "Mañana", "Borrar", hora de 12 horas con AM/PM.
 */
(function () {
    'use strict';

    var MONTHS = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'];
    var MONTHS_SHORT = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
    var WEEK = ['lu', 'ma', 'mi', 'ju', 'vi', 'sá', 'do'];                       // la semana empieza el lunes
    var WEEKDAYS = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];             // getDay(): 0 = domingo
    var ICONS = {
        cal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="18" rx="3"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>',
        prev: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 5-7 7 7 7"/></svg>',
        next: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>',
        x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
        up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 15 6-6 6 6"/></svg>',
        down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>',
        clock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'
    };
    var openPicker = null;

    function pad(n) { return (n < 10 ? '0' : '') + n; }
    function sameDay(a, b) { return a && b && a.y === b.y && a.m === b.m && a.d === b.d; }
    function cmpDay(a, b) { return (a.y - b.y) || (a.m - b.m) || (a.d - b.d); }
    function daysIn(y, m) { return new Date(y, m + 1, 0).getDate(); }
    function today() { var n = new Date(); return { y: n.getFullYear(), m: n.getMonth(), d: n.getDate() }; }
    function addDays(day, n) { var dt = new Date(day.y, day.m, day.d + n); return { y: dt.getFullYear(), m: dt.getMonth(), d: dt.getDate() }; }
    function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; }

    // 'AAAA-MM-DD' o 'AAAA-MM-DDTHH:MM' -> { y, m (0-11), d, hh, mm } (sin hora si es solo fecha) | null
    function parse(value) {
        var m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(String(value || ''));
        if (!m) return null;
        var out = { y: +m[1], m: +m[2] - 1, d: +m[3], hh: m[4] === undefined ? null : +m[4], mm: m[5] === undefined ? null : +m[5] };
        if (out.m < 0 || out.m > 11 || out.d < 1 || out.d > daysIn(out.y, out.m)) return null;
        return out;
    }
    function format(s, withTime) {
        var out = s.y + '-' + pad(s.m + 1) + '-' + pad(s.d);
        return withTime ? out + 'T' + pad(s.hh == null ? 0 : s.hh) + ':' + pad(s.mm == null ? 0 : s.mm) : out;
    }
    function time12(hh, mm) { var h = hh % 12 === 0 ? 12 : hh % 12; return h + ':' + pad(mm) + ' ' + (hh >= 12 ? 'PM' : 'AM'); }
    function pretty(s, withTime) {
        var wd = WEEKDAYS[new Date(s.y, s.m, s.d).getDay()];
        var text = wd.charAt(0).toUpperCase() + wd.slice(1) + ' ' + s.d + ' ' + MONTHS_SHORT[s.m] + ' ' + s.y;
        return withTime && s.hh != null ? text + ' · ' + time12(s.hh, s.mm) : text;
    }
    var VALUE_PROTO = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');

    function Picker(input) {
        this.input = input;
        this.withTime = input.type === 'datetime-local';
        this.min = parse(input.min);
        this.max = parse(input.max);
        this.sel = parse(input.value);
        this.view = this.sel ? { y: this.sel.y, m: this.sel.m } : { y: today().y, m: today().m };
        this.build();
    }

    Picker.prototype.build = function () {
        var self = this, input = this.input;
        var wrap = el('span', 'dtp');
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        input.classList.add('dtp-native');
        input.tabIndex = -1;
        input.setAttribute('aria-hidden', 'true');
        this.wrap = wrap;
        this.trigger = el('button', 'dtp-trigger', '<span class="dtp-ico">' + ICONS.cal + '</span><span class="dtp-text"></span>');
        this.trigger.type = 'button';
        this.trigger.setAttribute('aria-haspopup', 'dialog');
        this.trigger.setAttribute('aria-expanded', 'false');
        var label = input.id ? document.querySelector('label[for="' + input.id + '"]') : null;
        if (label) {
            if (!label.id) label.id = input.id + '_lbl';
            this.trigger.setAttribute('aria-labelledby', label.id);
            label.addEventListener('click', function (evt) { evt.preventDefault(); self.trigger.focus(); self.toggle(); });
        }
        wrap.appendChild(this.trigger);
        this.clearBtn = el('button', 'dtp-clear', ICONS.x);
        this.clearBtn.type = 'button';
        this.clearBtn.setAttribute('aria-label', 'Quitar la fecha');
        this.clearBtn.title = 'Quitar la fecha';
        wrap.appendChild(this.clearBtn);
        this.text = this.trigger.querySelector('.dtp-text');

        this.trigger.addEventListener('click', function () { self.toggle(); });
        this.clearBtn.addEventListener('click', function () { self.setSel(null, true); self.close(); self.trigger.focus(); });

        // Otros scripts cambian el valor con input.value = '...' (p. ej. al abrir una ventana de edición): el botón tiene que enterarse.
        Object.defineProperty(input, 'value', {
            configurable: true,
            get: function () { return VALUE_PROTO.get.call(input); },
            set: function (v) { VALUE_PROTO.set.call(input, v); self.sel = parse(v); self.syncText(); if (self.pop) self.renderAll(); }
        });
        if (input.form) input.form.addEventListener('reset', function () { setTimeout(function () { self.sel = parse(input.value); self.syncText(); }, 0); });
        this.syncText();
    };

    Picker.prototype.syncText = function () {
        var has = !!this.sel;
        this.text.textContent = has ? pretty(this.sel, this.withTime) : (this.withTime ? 'Elegir fecha y hora' : 'Elegir fecha');
        this.wrap.classList.toggle('has-value', has);
        this.text.classList.toggle('is-placeholder', !has);
    };

    Picker.prototype.setSel = function (sel, emit) {
        this.sel = sel;
        VALUE_PROTO.set.call(this.input, sel ? format(sel, this.withTime) : '');
        this.syncText();
        if (emit) {
            this.input.dispatchEvent(new Event('input', { bubbles: true }));
            this.input.dispatchEvent(new Event('change', { bubbles: true }));
        }
    };

    Picker.prototype.inRange = function (day) {
        if (this.min && cmpDay(day, this.min) < 0) return false;
        if (this.max && cmpDay(day, this.max) > 0) return false;
        return true;
    };

    Picker.prototype.toggle = function () { if (this.pop) this.close(); else this.open(); };

    Picker.prototype.open = function () {
        if (openPicker && openPicker !== this) openPicker.close();
        var self = this;
        openPicker = this;
        if (this.sel) this.view = { y: this.sel.y, m: this.sel.m };
        var pop = el('div', 'dtp-pop');
        pop.setAttribute('role', 'dialog');
        pop.setAttribute('aria-modal', 'false');
        pop.setAttribute('aria-label', this.withTime ? 'Elegir fecha y hora' : 'Elegir fecha');
        pop.tabIndex = -1;
        this.pop = pop;
        pop.innerHTML =
            '<div class="dtp-head"><button type="button" class="dtp-nav" data-nav="-1" aria-label="Mes anterior">' + ICONS.prev + '</button>' +
            '<div class="dtp-title" aria-live="polite"></div>' +
            '<button type="button" class="dtp-nav" data-nav="1" aria-label="Mes siguiente">' + ICONS.next + '</button></div>' +
            '<div class="dtp-week">' + WEEK.map(function (w) { return '<span>' + w + '</span>'; }).join('') + '</div>' +
            '<div class="dtp-grid" role="grid"></div>' +
            '<div class="dtp-quick"><button type="button" data-quick="0">Hoy</button><button type="button" data-quick="1">Mañana</button></div>' +
            (this.withTime ?
                '<div class="dtp-time"><span class="dtp-time-label">' + ICONS.clock + 'Hora <small>(Venezuela)</small></span>' +
                '<div class="dtp-time-row">' +
                '<div class="dtp-step"><button type="button" data-step="h" data-dir="1" aria-label="Subir hora">' + ICONS.up + '</button><input class="dtp-num" data-field="h" inputmode="numeric" maxlength="2" aria-label="Hora" autocomplete="off"><button type="button" data-step="h" data-dir="-1" aria-label="Bajar hora">' + ICONS.down + '</button></div>' +
                '<span class="dtp-colon">:</span>' +
                '<div class="dtp-step"><button type="button" data-step="m" data-dir="1" aria-label="Subir minutos">' + ICONS.up + '</button><input class="dtp-num" data-field="m" inputmode="numeric" maxlength="2" aria-label="Minutos" autocomplete="off"><button type="button" data-step="m" data-dir="-1" aria-label="Bajar minutos">' + ICONS.down + '</button></div>' +
                '<div class="dtp-ampm" role="group" aria-label="AM o PM"><button type="button" data-ampm="AM">AM</button><button type="button" data-ampm="PM">PM</button></div>' +
                '</div></div>' : '') +
            '<div class="dtp-foot"><button type="button" class="dtp-link" data-act="clear">Borrar</button><button type="button" class="dtp-done" data-act="done">Listo</button></div>';
        document.body.appendChild(pop);
        this.grid = pop.querySelector('.dtp-grid');
        this.title = pop.querySelector('.dtp-title');
        this.trigger.setAttribute('aria-expanded', 'true');
        this.renderAll();
        this.place();

        pop.addEventListener('click', function (evt) { self.onClick(evt); });
        pop.addEventListener('keydown', function (evt) { self.onKey(evt); });
        pop.addEventListener('input', function (evt) { self.onType(evt); });
        pop.addEventListener('change', function (evt) { if (evt.target.classList.contains('dtp-num')) self.commitTime(); });
        this.outside = function (evt) { if (!pop.contains(evt.target) && !self.wrap.contains(evt.target)) self.close(); };
        this.reflow = function () { self.place(); };
        document.addEventListener('pointerdown', this.outside, true);
        window.addEventListener('resize', this.reflow);
        window.addEventListener('scroll', this.reflow, true);
        var focusTarget = pop.querySelector('.dtp-day.is-sel') || pop.querySelector('.dtp-day.is-today:not([disabled])') || pop.querySelector('.dtp-day:not([disabled])');
        if (focusTarget) focusTarget.focus({ preventScroll: true });
    };

    Picker.prototype.close = function () {
        if (!this.pop) return;
        document.removeEventListener('pointerdown', this.outside, true);
        window.removeEventListener('resize', this.reflow);
        window.removeEventListener('scroll', this.reflow, true);
        this.pop.remove();
        this.pop = null;
        this.trigger.setAttribute('aria-expanded', 'false');
        if (openPicker === this) openPicker = null;
    };

    // Debajo del campo (o arriba si no cabe); en el celular sube como una hoja pegada abajo.
    Picker.prototype.place = function () {
        if (!this.pop) return;
        var pop = this.pop, r = this.trigger.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight;
        pop.classList.toggle('is-sheet', vw <= 520);
        if (vw <= 520) { pop.style.left = ''; pop.style.top = ''; return; }
        var w = pop.offsetWidth, h = pop.offsetHeight;
        var left = Math.min(Math.max(8, r.left), vw - w - 8);
        var top = r.bottom + 8;
        if (top + h > vh - 8 && r.top - h - 8 > 8) top = r.top - h - 8;
        top = Math.min(Math.max(8, top), Math.max(8, vh - h - 8));   // siempre dentro de la ventana, aunque el campo esté medio fuera de pantalla
        pop.style.left = left + 'px';
        pop.style.top = top + 'px';
    };

    Picker.prototype.renderAll = function () { this.renderGrid(); this.renderTime(); };

    Picker.prototype.renderGrid = function () {
        if (!this.grid) return;
        var v = this.view, first = new Date(v.y, v.m, 1), offset = (first.getDay() + 6) % 7, t = today();
        this.title.textContent = MONTHS[v.m].charAt(0).toUpperCase() + MONTHS[v.m].slice(1) + ' ' + v.y;
        this.grid.textContent = '';
        for (var i = 0; i < 42; i++) {
            var day = addDays({ y: v.y, m: v.m, d: 1 }, i - offset);
            var b = el('button', 'dtp-day', String(day.d));
            b.type = 'button';
            b.dataset.y = day.y; b.dataset.m = day.m; b.dataset.d = day.d;
            b.setAttribute('aria-label', day.d + ' de ' + MONTHS[day.m] + ' de ' + day.y);
            if (day.m !== v.m) b.classList.add('is-out');
            if (sameDay(day, t)) b.classList.add('is-today');
            if (this.sel && sameDay(day, this.sel)) { b.classList.add('is-sel'); b.setAttribute('aria-pressed', 'true'); }
            if (!this.inRange(day)) b.disabled = true;
            else if (cmpDay(day, t) < 0) b.classList.add('is-past');
            this.grid.appendChild(b);
        }
    };

    Picker.prototype.renderTime = function () {
        if (!this.withTime || !this.pop) return;
        var hh = this.sel && this.sel.hh != null ? this.sel.hh : null, mm = this.sel && this.sel.mm != null ? this.sel.mm : null;
        var h12 = hh == null ? '' : String(hh % 12 === 0 ? 12 : hh % 12), mmText = mm == null ? '' : pad(mm);
        var inputs = this.pop.querySelectorAll('.dtp-num');
        if (document.activeElement !== inputs[0]) inputs[0].value = h12;
        if (document.activeElement !== inputs[1]) inputs[1].value = mmText;
        var pm = hh != null && hh >= 12;
        this.pop.querySelectorAll('[data-ampm]').forEach(function (b) { b.classList.toggle('is-on', hh != null && (b.dataset.ampm === 'PM') === pm); });
        this.pop.classList.toggle('no-day', !this.sel);
    };

    // Una hora cómoda por defecto cuando se elige el día sin haber puesto hora: la próxima hora en punto.
    Picker.prototype.defaultTime = function () { var n = new Date(); return { hh: (n.getHours() + 1) % 24, mm: 0 }; };

    Picker.prototype.pickDay = function (day) {
        var next = { y: day.y, m: day.m, d: day.d, hh: null, mm: null };
        if (this.withTime) {
            var keep = this.sel && this.sel.hh != null ? { hh: this.sel.hh, mm: this.sel.mm } : this.defaultTime();
            next.hh = keep.hh; next.mm = keep.mm;
        }
        this.view = { y: day.y, m: day.m };
        this.setSel(next, true);
        this.renderAll();
    };

    Picker.prototype.commitTime = function () {
        if (!this.withTime || !this.pop) return;
        var inputs = this.pop.querySelectorAll('.dtp-num');
        var base = this.sel || today();
        var h = parseInt(inputs[0].value, 10), m = parseInt(inputs[1].value, 10);
        var pm = this.sel && this.sel.hh != null ? this.sel.hh >= 12 : false;
        if (isNaN(h)) h = this.sel && this.sel.hh != null ? (this.sel.hh % 12 || 12) : 12;
        if (isNaN(m)) m = this.sel && this.sel.mm != null ? this.sel.mm : 0;
        h = Math.min(12, Math.max(1, h)); m = Math.min(59, Math.max(0, m));
        this.setSel({ y: base.y, m: base.m, d: base.d, hh: (h % 12) + (pm ? 12 : 0), mm: m }, true);
        this.renderAll();
    };

    Picker.prototype.stepTime = function (field, dir) {
        var base = this.sel || today(), dt = this.defaultTime();
        var hh = this.sel && this.sel.hh != null ? this.sel.hh : dt.hh, mm = this.sel && this.sel.mm != null ? this.sel.mm : dt.mm;
        if (field === 'h') hh = (hh + dir + 24) % 24;
        else { mm += dir * 5; if (mm >= 60) { mm = 0; hh = (hh + 1) % 24; } else if (mm < 0) { mm = 55; hh = (hh + 23) % 24; } }
        this.setSel({ y: base.y, m: base.m, d: base.d, hh: hh, mm: mm }, true);
        this.renderAll();
    };

    Picker.prototype.setAmPm = function (value) {
        if (!(this.sel && this.sel.hh != null)) this.stepTime('m', 0);
        var cur = this.sel;
        if (!cur) return;
        this.setSel({ y: cur.y, m: cur.m, d: cur.d, hh: (cur.hh % 12) + (value === 'PM' ? 12 : 0), mm: cur.mm }, true);
        this.renderAll();
    };

    Picker.prototype.onClick = function (evt) {
        var t = evt.target.closest('button');
        if (!t || !this.pop || !this.pop.contains(t)) return;
        if (t.classList.contains('dtp-day')) { this.pickDay({ y: +t.dataset.y, m: +t.dataset.m, d: +t.dataset.d }); if (!this.withTime) { this.close(); this.trigger.focus(); } return; }
        if (t.dataset.nav) {
            var m = this.view.m + (+t.dataset.nav), y = this.view.y;
            if (m < 0) { m = 11; y--; } else if (m > 11) { m = 0; y++; }
            this.view = { y: y, m: m };
            this.renderGrid();
            return;
        }
        if (t.dataset.quick !== undefined) { var day = addDays(today(), +t.dataset.quick); if (this.inRange(day)) this.pickDay(day); return; }
        if (t.dataset.step) { this.stepTime(t.dataset.step, +t.dataset.dir); return; }
        if (t.dataset.ampm) { this.setAmPm(t.dataset.ampm); return; }
        if (t.dataset.act === 'clear') { this.setSel(null, true); this.renderAll(); this.close(); this.trigger.focus(); return; }
        if (t.dataset.act === 'done') { this.close(); this.trigger.focus(); }
    };

    Picker.prototype.onType = function (evt) {
        if (evt.target.classList.contains('dtp-num')) evt.target.value = evt.target.value.replace(/\D/g, '').slice(0, 2);
    };

    Picker.prototype.onKey = function (evt) {
        if (evt.key === 'Escape') { evt.stopPropagation(); this.close(); this.trigger.focus(); return; }
        var t = evt.target;
        if (t.classList.contains('dtp-num')) {
            if (evt.key === 'Enter') { evt.preventDefault(); this.commitTime(); }
            else if (evt.key === 'ArrowUp' || evt.key === 'ArrowDown') { evt.preventDefault(); this.commitTime(); this.stepTime(t.dataset.field, evt.key === 'ArrowUp' ? 1 : -1); }
            return;
        }
        if (!t.classList.contains('dtp-day')) return;
        var day = { y: +t.dataset.y, m: +t.dataset.m, d: +t.dataset.d }, delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[evt.key];
        if (delta === undefined) return;
        evt.preventDefault();
        var target = addDays(day, delta);
        if (target.m !== this.view.m || target.y !== this.view.y) { this.view = { y: target.y, m: target.m }; this.renderGrid(); }
        var btn = this.grid.querySelector('.dtp-day[data-y="' + target.y + '"][data-m="' + target.m + '"][data-d="' + target.d + '"]');
        if (btn && !btn.disabled) btn.focus();
    };

    function upgrade(input) {
        if (input.__dtp || input.hasAttribute('data-native-date')) return;
        if (input.type !== 'date' && input.type !== 'datetime-local') return;
        input.__dtp = new Picker(input);
    }

    function upgradeAll(root) {
        Array.prototype.forEach.call((root || document).querySelectorAll('input[type="date"], input[type="datetime-local"]'), upgrade);
    }

    window.ModernDateTime = { upgrade: upgrade, upgradeAll: upgradeAll };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { upgradeAll(); });
    else upgradeAll();
})();
