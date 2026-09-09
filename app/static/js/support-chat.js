/* ── 3S Recargas — pantalla de soporte ───────────────────────────────────
 *
 * Vive aparte de main.js porque solo la usa esta pagina, y porque el chat
 * dejo de ser un modal de la tienda para ser una pantalla propia.
 *
 * El hilo se sostiene con sondeo HTTP, no con WebSocket: el servidor corre
 * `gunicorn -w 3` sin estado compartido, asi que el worker que recibe la
 * respuesta del admin no seria el que sostiene el socket del cliente.
 */

(function () {
    'use strict';

    var TOKEN_KEY = 'store:support-token';

    var startBox = document.getElementById('scStart');
    var startForm = document.getElementById('scStartForm');
    var nameInput = document.getElementById('scName');
    var firstMessage = document.getElementById('scFirstMessage');
    var emailInput = document.getElementById('scEmail');
    var trap = document.getElementById('scWebsite');
    var errorEl = document.getElementById('scError');

    var chatBox = document.getElementById('scChat');
    var thread = document.getElementById('scThread');
    var codeEl = document.getElementById('scCode');
    var presenceEl = document.getElementById('scPresence');
    var closedEl = document.getElementById('scClosed');

    var composer = document.getElementById('scComposer');
    var input = document.getElementById('scInput');
    var attachBtn = document.getElementById('scAttach');
    var fileInput = document.getElementById('scFile');

    if (!startForm || !thread) return;

    var state = {
        token: '',
        lastId: parseInt(window.SUPPORT_LAST_ID || 0, 10) || 0,
        sending: false,
        timer: null
    };

    function readToken() {
        try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (_) { return ''; }
    }

    function writeToken(token) {
        state.token = token || '';
        try {
            if (token) localStorage.setItem(TOKEN_KEY, token);
            else localStorage.removeItem(TOKEN_KEY);
        } catch (_) {}
    }

    // La cookie httponly es el camino normal. La cabecera es el respaldo
    // para navegadores que la borran por su cuenta (Safari purga cookies de
    // sitios que no se visitan seguido) — perderla seria perder el hilo de
    // alguien que esta esperando respuesta.
    function headers(extra) {
        var h = extra || {};
        if (state.token) h['X-Support-Token'] = state.token;
        return h;
    }

    function showError(message) {
        if (!errorEl) return;
        errorEl.textContent = message || '';
        errorEl.hidden = !message;
    }

    function scrollDown() { thread.scrollTop = thread.scrollHeight; }

    // La hora la formatea el servidor en horario de Venezuela; el reloj
    // del telefono puede estar en otro huso y entonces cada mensaje
    // parecia de otra hora.
    function timeLabel(message) {
        if (message.time_label) return message.time_label;
        var d = new Date(message.created_at);
        return isNaN(d) ? '' : d.toLocaleTimeString('es-VE',
            { hour: '2-digit', minute: '2-digit' });
    }

    // textContent y nunca innerHTML: el cuerpo lo escribe una persona y
    // llega por JSON, sin pasar por el autoescape de Jinja.
    function render(message) {
        var el = document.createElement('div');
        el.className = 'sc-msg ' + (message.sender || 'admin');
        el.dataset.id = message.id;

        if (message.body) {
            var body = document.createElement('span');
            body.className = 'sc-msg-body';
            body.textContent = message.body;
            el.appendChild(body);
        }

        if (message.attachment) {
            var url = '/static/uploads/' + message.attachment;

            if (message.is_video) {
                var video = document.createElement('video');
                video.className = 'sc-media';
                video.src = url;
                video.controls = true;
                video.preload = 'metadata';
                el.appendChild(video);
            } else {
                // Miniatura acotada por CSS que abre el original en otra
                // pestana, igual que los comprobantes de las ordenes. A
                // tamano completo una foto de movil ocupaba toda la
                // pantalla y empujaba la conversacion fuera de vista.
                var link = document.createElement('a');
                link.className = 'sc-media-link';
                link.href = url;
                link.target = '_blank';
                link.rel = 'noopener';

                var img = document.createElement('img');
                img.className = 'sc-media';
                img.src = url;
                img.alt = 'Imagen del chat';
                img.loading = 'lazy';
                link.appendChild(img);

                var hint = document.createElement('span');
                hint.className = 'sc-media-open';
                hint.textContent = 'Abrir \u2922';
                link.appendChild(hint);

                el.appendChild(link);
            }
        }

        if (message.sender !== 'system') {
            var time = document.createElement('span');
            time.className = 'sc-time';
            time.textContent = timeLabel(message);
            el.appendChild(time);
        }

        thread.appendChild(el);
        state.lastId = Math.max(state.lastId, message.id || 0);
        scrollDown();
    }

    function applyChat(chat) {
        if (!chat) return;
        if (codeEl) codeEl.textContent = chat.code || '';
        var isClosed = chat.status === 'closed';
        if (closedEl) closedEl.hidden = !isClosed;
        if (presenceEl && isClosed) presenceEl.textContent = 'Chat cerrado';
    }

    function enterChat() {
        if (startBox) startBox.hidden = true;
        if (chatBox) chatBox.hidden = false;
        scrollDown();
        if (input) input.focus();
    }

    function poll() {
        if (!state.token) return;
        fetch('/soporte/hilo?after_id=' + state.lastId, {
            credentials: 'same-origin',
            headers: headers()
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) return;
                applyChat(data.chat);
                (data.messages || []).forEach(render);
            })
            .catch(function () {});
    }

    // 5 s con la pestana a la vista. En segundo plano no se pregunta nada:
    // ahi es donde se van las peticiones que nadie va a leer.
    function startPolling() {
        if (state.timer) return;
        state.timer = setInterval(function () {
            if (document.visibilityState === 'visible') poll();
        }, 5000);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') poll();
        });
    }

    // ── Abrir el chat ────────────────────────────────────────────────────

    startForm.addEventListener('submit', function (evt) {
        evt.preventDefault();
        showError('');

        var name = nameInput ? String(nameInput.value || '').trim() : '';
        if (name.length < 2) {
            showError('Escribe tu nombre para iniciar el chat.');
            return;
        }

        var btn = startForm.querySelector('.sc-start-btn');
        if (btn) btn.disabled = true;

        fetch('/soporte/iniciar', {
            method: 'POST',
            credentials: 'same-origin',
            headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({
                name: name,
                message: firstMessage ? firstMessage.value : '',
                email: emailInput ? emailInput.value : '',
                website: trap ? trap.value : '',
                context: collectContext()
            })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (btn) btn.disabled = false;
                if (!data || !data.ok) {
                    showError((data && data.error) || 'No pudimos abrir el chat. Intenta de nuevo.');
                    return;
                }
                writeToken(data.token);
                state.lastId = 0;
                thread.innerHTML = '';
                (data.messages || []).forEach(render);
                applyChat(data.chat);
                enterChat();
                startPolling();
            })
            .catch(function () {
                if (btn) btn.disabled = false;
                showError('Sin conexión. Revisa tu internet e intenta de nuevo.');
            });
    });

    function collectContext() {
        var context = { page: location.pathname.slice(0, 255) };

        var orden = new URLSearchParams(location.search).get('orden');
        if (orden) context.order_number = orden;

        // Contacto que el cliente guardo en un checkout anterior.
        try {
            var raw = localStorage.getItem('store:remembered-contact');
            var saved = raw ? JSON.parse(raw) : null;
            if (saved) {
                if (saved.email) context.email = saved.email;
                if (saved.phone) context.phone = saved.phone;
            }
        } catch (_) {}

        return context;
    }

    // ── Escribir ─────────────────────────────────────────────────────────

    if (composer) {
        composer.addEventListener('submit', function (evt) {
            evt.preventDefault();
            if (state.sending) return;

            var body = input ? String(input.value || '').trim() : '';
            if (!body) return;

            state.sending = true;
            if (input) { input.value = ''; input.style.height = ''; }

            fetch('/soporte/mensaje', {
                method: 'POST',
                credentials: 'same-origin',
                headers: headers({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ body: body })
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    state.sending = false;
                    if (data && data.ok) {
                        render(data.message);
                        applyChat(data.chat);
                        // Escribir reabre el hilo cerrado por inactividad.
                        if (closedEl) closedEl.hidden = true;
                    } else if (input) {
                        // Devolver el texto: perder lo que alguien acaba de
                        // escribir por un fallo de red es la peor forma de
                        // recibir a quien ya viene molesto.
                        input.value = body;
                        alert((data && data.error) || 'No se pudo enviar el mensaje.');
                    }
                })
                .catch(function () {
                    state.sending = false;
                    if (input) input.value = body;
                });
        });
    }

    if (input) {
        // Enter envia, Shift+Enter salta de linea.
        input.addEventListener('keydown', function (evt) {
            if (evt.key === 'Enter' && !evt.shiftKey) {
                evt.preventDefault();
                composer.requestSubmit();
            }
        });
        // La caja crece con el texto, como en cualquier mensajeria.
        input.addEventListener('input', function () {
            input.style.height = 'auto';
            input.style.height = Math.min(input.scrollHeight, 130) + 'px';
        });
    }

    // ── Adjuntar ─────────────────────────────────────────────────────────

    if (attachBtn && fileInput) {
        attachBtn.addEventListener('click', function () { fileInput.click(); });

        fileInput.addEventListener('change', function () {
            var file = fileInput.files && fileInput.files[0];
            if (!file) return;

            // El servidor admite hasta 16 MB, pero cortar aqui evita subir
            // una foto de 12 MB por datos moviles para que la rechacen al
            // final.
            if (file.size > 15 * 1024 * 1024) {
                alert('El archivo es muy pesado. Envía uno de menos de 15 MB.');
                fileInput.value = '';
                return;
            }

            var payload = new FormData();
            payload.append('file', file);
            payload.append('body', input ? input.value : '');

            attachBtn.disabled = true;
            fetch('/soporte/adjunto', {
                method: 'POST',
                credentials: 'same-origin',
                headers: headers(),
                body: payload
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    attachBtn.disabled = false;
                    fileInput.value = '';
                    if (data && data.ok) {
                        if (input) input.value = '';
                        render(data.message);
                    } else {
                        alert((data && data.error) || 'No se pudo enviar la imagen.');
                    }
                })
                .catch(function () {
                    attachBtn.disabled = false;
                    fileInput.value = '';
                    alert('No se pudo enviar la imagen.');
                });
        });
    }

    // ── Arranque ─────────────────────────────────────────────────────────

    state.token = readToken();

    if (window.SUPPORT_HAS_CHAT) {
        // El servidor ya pinto el hilo (reconocio la cookie). Solo falta
        // ponerse a sondear.
        enterChat();
        startPolling();
    } else if (state.token) {
        // Hay token guardado pero el servidor no lo vio: la cookie se
        // perdio y toca reclamar el hilo por cabecera.
        fetch('/soporte/hilo?after_id=0', {
            credentials: 'same-origin',
            headers: headers()
        })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) {
                if (!data || !data.ok) { writeToken(''); return; }
                thread.innerHTML = '';
                state.lastId = 0;
                (data.messages || []).forEach(render);
                applyChat(data.chat);
                enterChat();
                startPolling();
            })
            .catch(function () {});
    } else if (nameInput) {
        nameInput.focus();
    }
})();
