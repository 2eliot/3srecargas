/* ── 3S Recargas — buscador de amigos por nombre de usuario (barra de arriba de los perfiles) ─────────────────────────────────────────────
 * Escribes 2 o más letras del usuario y salen las cuentas con perfil público que lo contienen (nombre, @usuario y foto). Al tocar una se abre
 * su perfil (/u/<usuario>). Va a GET /perfiles/buscar (el servidor limita las consultas por minuto y solo devuelve perfiles que cualquiera puede
 * abrir). Todo lo que llega del servidor se revisa antes de dibujarlo (el usuario y la foto deben tener la forma esperada) y se escribe como
 * texto, nunca como HTML.
 */
(function () {
    'use strict';

    var root = document.getElementById('pfSearch');
    if (!root) return;
    var input = document.getElementById('pfSearchInput');
    var clearBtn = document.getElementById('pfSearchClear');
    var panel = document.getElementById('pfSearchPanel');
    var list = document.getElementById('pfSearchList');
    var note = document.getElementById('pfSearchNote');
    var url = root.getAttribute('data-url') || '/perfiles/buscar';
    if (!input || !panel || !list || !note) return;

    var MIN = 2;
    var HANDLE_RE = /^[A-Za-z0-9._-]{3,20}$/;
    var AVATAR_RE = /^uploads\/profiles\/\d+\/[a-z_]+_[0-9a-f]{12}\.(webp|gif)$/;
    var timer = null;
    var controller = null;
    var lastQuery = '';
    var items = [];

    function setOpen(open) {
        panel.hidden = !open;
        input.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    function say(text) {
        note.textContent = text || '';
        note.hidden = !text;
    }

    function clearList() {
        while (list.firstChild) list.removeChild(list.firstChild);
        items = [];
    }

    function buildItem(entry) {
        var handle = entry && typeof entry.handle === 'string' ? entry.handle : '';
        if (!HANDLE_RE.test(handle)) return null;
        var name = entry && typeof entry.name === 'string' && entry.name ? entry.name : handle;
        var li = document.createElement('li');
        li.setAttribute('role', 'option');
        var a = document.createElement('a');
        a.className = 'pf-search-item';
        a.href = '/u/' + handle;
        var av = document.createElement('span');
        av.className = 'pf-search-av';
        av.setAttribute('aria-hidden', 'true');
        if (entry.avatar && typeof entry.avatar === 'string' && AVATAR_RE.test(entry.avatar)) {
            var img = document.createElement('img');
            img.src = '/static/' + entry.avatar;
            img.alt = '';
            img.loading = 'lazy';
            img.decoding = 'async';
            av.appendChild(img);
        } else {
            av.textContent = (name.charAt(0) || '?').toUpperCase();
        }
        var text = document.createElement('span');
        text.className = 'pf-search-txt';
        var strong = document.createElement('strong');
        strong.textContent = name;
        if (entry.official === true) {
            // Cuenta oficial del administrador: verificado + etiqueta ADMIN (todo con textContent / DOM, nada de HTML del servidor).
            var ns = 'http://www.w3.org/2000/svg';
            var badge = document.createElement('span');
            badge.className = 'pf-verified is-mini';
            badge.setAttribute('title', 'Cuenta oficial verificada de 3S Recargas');
            badge.setAttribute('role', 'img');
            badge.setAttribute('aria-label', 'Cuenta oficial verificada');
            var svg = document.createElementNS(ns, 'svg');
            svg.setAttribute('viewBox', '0 0 24 24');
            svg.setAttribute('aria-hidden', 'true');
            var circle = document.createElementNS(ns, 'path');   // el sello con puntas (igual que el de las demás pantallas)
            circle.setAttribute('class', 'pf-seal');
            circle.setAttribute('d', 'M12.00 1.50 L15.18 4.33 L19.42 4.58 L19.67 8.82 L22.50 12.00 L19.67 15.18 L19.42 19.42 L15.18 19.67 L12.00 22.50 L8.82 19.67 L4.58 19.42 L4.33 15.18 L1.50 12.00 L4.33 8.82 L4.58 4.58 L8.82 4.33 Z');
            var check = document.createElementNS(ns, 'path');
            check.setAttribute('class', 'pf-verified-check');
            check.setAttribute('d', 'm7.4 12.6 3.1 3.1 6.1-6.6');
            svg.appendChild(circle); svg.appendChild(check); badge.appendChild(svg);
            var tag = document.createElement('span');
            tag.className = 'pf-admintag is-mini';
            tag.textContent = 'ADMIN';
            strong.appendChild(badge);
            strong.appendChild(tag);
        }
        var small = document.createElement('small');
        small.textContent = '@' + handle;
        text.appendChild(strong);
        text.appendChild(small);
        a.appendChild(av);
        a.appendChild(text);
        li.appendChild(a);
        return li;
    }

    function render(results) {
        clearList();
        (results || []).forEach(function (entry) {
            var li = buildItem(entry);
            if (li) { list.appendChild(li); items.push(li.firstChild); }
        });
        if (items.length) {
            say('');
        } else {
            say('No encontramos a nadie con ese usuario.');
        }
        setOpen(true);
    }

    function search(query) {
        if (controller && controller.abort) controller.abort();
        controller = window.AbortController ? new AbortController() : null;
        lastQuery = query;
        say('Buscando…');
        setOpen(true);
        fetch(url + '?q=' + encodeURIComponent(query), {
            credentials: 'same-origin', headers: { 'Accept': 'application/json' }, signal: controller ? controller.signal : undefined,
        })
            .then(function (res) {
                return res.json().catch(function () { return {}; }).then(function (data) { return { ok: res.ok, status: res.status, data: data }; });
            })
            .then(function (r) {
                if (query !== lastQuery) return;   // ya se escribió otra cosa: esta respuesta quedó vieja
                if (!r.ok) {
                    clearList();
                    say((r.data && r.data.error) || (r.status === 429 ? 'Vas muy rápido. Espera un momento.' : 'No se pudo buscar ahora. Intenta de nuevo.'));
                    return;
                }
                render(r.data && Array.isArray(r.data.results) ? r.data.results : []);
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return;
                if (query !== lastQuery) return;
                clearList();
                say('Sin conexión. Revisa tu internet e intenta de nuevo.');
            });
    }

    function clean() {
        return input.value.trim().replace(/^@+/, '');
    }

    function onInput() {
        var value = clean();
        clearBtn.hidden = !input.value;
        if (timer) clearTimeout(timer);
        if (value.length < MIN) {
            if (controller && controller.abort) controller.abort();
            lastQuery = '';
            clearList();
            say(value.length ? 'Escribe al menos ' + MIN + ' letras.' : '');
            setOpen(value.length > 0);
            return;
        }
        timer = setTimeout(function () { search(value); }, 250);
    }

    input.addEventListener('input', onInput);
    input.addEventListener('focus', function () { if (clean().length >= MIN && (items.length || note.textContent)) setOpen(true); });
    input.addEventListener('keydown', function (evt) {
        if (evt.key === 'ArrowDown' && items.length) {
            evt.preventDefault();
            items[0].focus();
        } else if (evt.key === 'Enter') {
            evt.preventDefault();
            if (items.length) { window.location.href = items[0].getAttribute('href'); return; }
            var value = clean();
            if (HANDLE_RE.test(value)) window.location.href = '/u/' + value;   // si escribió el usuario completo, se intenta abrir directo
        } else if (evt.key === 'Escape') {
            setOpen(false);
        }
    });
    list.addEventListener('keydown', function (evt) {
        var idx = items.indexOf(document.activeElement);
        if (evt.key === 'ArrowDown') {
            evt.preventDefault();
            if (idx < items.length - 1) items[idx + 1].focus();
        } else if (evt.key === 'ArrowUp') {
            evt.preventDefault();
            if (idx > 0) items[idx - 1].focus(); else input.focus();
        } else if (evt.key === 'Escape') {
            setOpen(false);
            input.focus();
        }
    });
    clearBtn.addEventListener('click', function () {
        input.value = '';
        onInput();
        input.focus();
    });
    document.addEventListener('click', function (evt) {
        if (!root.contains(evt.target)) setOpen(false);
    });
})();
