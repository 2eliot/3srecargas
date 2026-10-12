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

    var thread = document.getElementById('scThread');
    var codeEl = document.getElementById('scCode');
    var presenceEl = document.getElementById('scPresence');
    var closedEl = document.getElementById('scClosed');

    var composer = document.getElementById('scComposer');
    var input = document.getElementById('scInput');
    var attachBtn = document.getElementById('scAttach');
    var fileInput = document.getElementById('scFile');

    if (!thread || !composer) return;

    var state = {
        token: '',
        // El chat existe en el servidor desde el primer mensaje; antes de eso
        // la pantalla es solo la caja para escribir.
        hasChat: !!window.SUPPORT_HAS_CHAT,
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
        el.className = 'sc-msg ' + (message.sender || 'admin') + (message.is_deleted ? ' deleted' : '');
        el.dataset.id = message.id;

        if (message.is_deleted) {
            var deletedText = document.createElement('span');
            deletedText.className = 'sc-msg-body sc-msg-deleted-text';
            deletedText.textContent = '🚫 Mensaje eliminado';
            el.appendChild(deletedText);
            if (message.sender !== 'system') {
                var t2 = document.createElement('span');
                t2.className = 'sc-time';
                t2.textContent = timeLabel(message);
                el.appendChild(t2);
            }
            thread.appendChild(el);
            state.lastId = Math.max(state.lastId, message.id || 0);
            scrollDown();
            return;
        }

        if (message.is_ai) {
            el.classList.add('ai');
            var who = document.createElement('span');
            who.className = 'sc-ai-label';
            who.textContent = '🤖 Asistente virtual';
            el.appendChild(who);
        }

        if (message.body) {
            var body = document.createElement('span');
            body.className = 'sc-msg-body';
            body.textContent = message.body;
            el.appendChild(body);
        }

        // Botón del asistente. Solo dos clases, nunca un link a otro sitio: el link de
        // una orden de la tienda, o "crear mi orden con este comprobante" (#comprar:<id
        // del mensaje con la foto>), que abre la compra dentro del chat (support-buy.js).
        if (message.action_url && /^\/order\/\d+$/.test(message.action_url)) {
            var action = document.createElement('a');
            action.className = 'sc-action-btn';
            action.href = message.action_url;
            action.textContent = message.action_label || 'Ver mi orden';
            el.appendChild(action);
        } else if (message.action_url && /^#comprar:\d+$/.test(message.action_url)) {
            var buy = document.createElement('button');
            buy.type = 'button';
            buy.className = 'sc-action-btn';
            buy.dataset.buyReceipt = message.action_url.split(':')[1];
            buy.textContent = message.action_label || 'Crear mi orden con este comprobante';
            el.appendChild(buy);
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

    // "Escribiendo..." del asistente: mientras dura, se pregunta más seguido
    // para que la respuesta aparezca apenas esté lista.
    function applyTyping(isTyping) {
        var el = document.getElementById('scTyping');
        if (isTyping) {
            if (!el) {
                el = document.createElement('div');
                el.id = 'scTyping';
                el.className = 'sc-msg admin ai sc-typing';
                el.innerHTML = '<span class="sc-ai-label">🤖 Asistente virtual</span><span class="sc-dots"><i></i><i></i><i></i></span>';
            }
            thread.appendChild(el);  // siempre al final
            scrollDown();
            clearTimeout(state.typingTimer);
            state.typingTimer = setTimeout(poll, 1500);
        } else if (el) {
            el.remove();
        }
    }

    function applyChat(chat) {
        if (!chat) return;
        applyTyping(!!chat.ai_typing);
        if (codeEl) codeEl.textContent = chat.code || '';
        var isClosed = chat.status === 'closed';
        if (closedEl) closedEl.hidden = !isClosed;
        if (presenceEl && isClosed) presenceEl.textContent = 'Chat cerrado';
    }

    // Deja el hilo al final y el cursor listo para escribir.
    function readyToType() {
        scrollDown();
        if (input) input.focus();
    }

    // Actualiza una burbuja ya pintada si soporte borró ese mensaje después
    // de que este navegador ya la había recibido en un poll anterior.
    function applyDeleted(id) {
        var el = thread.querySelector('.sc-msg[data-id="' + id + '"]');
        if (!el || el.classList.contains('deleted')) return;
        el.classList.add('deleted');
        el.innerHTML = '';
        var deletedText = document.createElement('span');
        deletedText.className = 'sc-msg-body sc-msg-deleted-text';
        deletedText.textContent = '🚫 Mensaje eliminado';
        el.appendChild(deletedText);
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
                (data.messages || []).forEach(render);
                applyChat(data.chat);
                (data.deleted_ids || []).forEach(applyDeleted);
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
    //
    // No hay formulario: la pantalla es un chat normal y el hilo se crea en
    // el servidor con el primer mensaje (o el primer adjunto). Asi abrir
    // "Soporte" sin escribir nada no deja chats vacios en la bandeja.

    // Error con texto listo para mostrarle al cliente (el que mando el
    // servidor), para distinguirlo de un corte de red.
    function failure(message) {
        var err = new Error(message);
        err.fromServer = true;
        return err;
    }

    function startChat(message, hasAttachment) {
        var payload = {
            message: message,
            context: collectContext(),
            // Si trae adjunto, el asistente espera a que suba para leerlo
            // en vez de responder dos veces.
            has_attachment: !!hasAttachment
        };
        // Solo el flujo "Olvide mi contrasena" trae el correo (y de el, el nombre).
        var autoEmail = window.SUPPORT_AUTO_EMAIL || '';
        if (autoEmail) {
            payload.email = autoEmail;
            payload.name = autoEmail.split('@')[0] || 'Mini';
        }

        return fetch('/soporte/iniciar', {
            method: 'POST',
            credentials: 'same-origin',
            headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify(payload)
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) {
                    throw failure((data && data.error) || 'No pudimos abrir el chat. Intenta de nuevo.');
                }
                writeToken(data.token);
                state.hasChat = true;
                state.lastId = 0;
                thread.innerHTML = '';  // quita el saludo y lo que se haya consultado en el menú
                (data.messages || []).forEach(render);
                applyChat(data.chat);
                startPolling();
            });
    }

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

    function sendMessage(body) {
        return fetch('/soporte/mensaje', {
            method: 'POST',
            credentials: 'same-origin',
            headers: headers({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ body: body })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) throw failure((data && data.error) || 'No se pudo enviar el mensaje.');
                render(data.message);
                applyChat(data.chat);
            });
    }

    composer.addEventListener('submit', function (evt) {
        evt.preventDefault();
        if (state.sending) return;

        var body = input ? String(input.value || '').trim() : '';
        if (!body) return;

        state.sending = true;
        if (input) { input.value = ''; input.style.height = ''; }

        // Sin chat todavia, el primer mensaje lo crea.
        var request = state.hasChat ? sendMessage(body) : startChat(body, false);
        request
            .then(function () {
                state.sending = false;
                // Escribir reabre el hilo cerrado por inactividad.
                if (closedEl) closedEl.hidden = true;
            })
            .catch(function (err) {
                state.sending = false;
                // Devolver el texto: perder lo que alguien acaba de
                // escribir por un fallo de red es la peor forma de
                // recibir a quien ya viene molesto.
                if (input) input.value = body;
                if (err && err.fromServer) alert(err.message);
            });
    });

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

            attachBtn.disabled = true;

            // Sin chat todavia, el adjunto tambien lo crea.
            var ready = state.hasChat ? Promise.resolve() : startChat('', true);
            ready
                .then(function () {
                    var payload = new FormData();
                    payload.append('file', file);
                    payload.append('body', input ? input.value : '');
                    return fetch('/soporte/adjunto', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: headers(),
                        body: payload
                    });
                })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (!data || !data.ok) throw failure((data && data.error) || 'No se pudo enviar la imagen.');
                    if (input) input.value = '';
                    render(data.message);
                    applyChat(data.chat);
                })
                .catch(function (err) {
                    alert(err && err.fromServer ? err.message : 'No se pudo enviar la imagen.');
                })
                .then(function () {
                    attachBtn.disabled = false;
                    fileInput.value = '';
                });
        });
    }

    // ── Arranque ─────────────────────────────────────────────────────────

    // Manda solo el mensaje que ya trae la pagina (flujo "Olvide mi
    // contrasena"): el mini ya dio su correo antes de llegar, asi que no
    // tiene que escribir nada ni tocar enviar.
    function sendAutoMessage() {
        if (!window.SUPPORT_AUTO_MESSAGE || !input) return;
        input.value = window.SUPPORT_AUTO_MESSAGE;
        if (composer.requestSubmit) composer.requestSubmit();
        else composer.dispatchEvent(new Event('submit', { cancelable: true }));
    }

    state.token = readToken();

    // Con ?orden= el mensaje del pedido llega escrito: se ajusta el alto de
    // la caja para que se lea y el cliente solo lo envia.
    if (input && input.value) input.dispatchEvent(new Event('input'));

    if (window.SUPPORT_AUTO_START && !window.SUPPORT_HAS_CHAT) {
        sendAutoMessage();
    } else if (window.SUPPORT_HAS_CHAT && window.SUPPORT_AUTO_SEND) {
        // Mismo caso, pero el navegador ya tenia un chat abierto (uno por
        // navegador): el mensaje se agrega ahi en vez de perderse. Se
        // manda una sola vez por pestana para que recargar la pagina no
        // lo repita.
        readyToType();
        startPolling();
        var yaEnviado = false;
        try { yaEnviado = sessionStorage.getItem('store:support-forgot-sent') === '1'; } catch (_) {}
        if (!yaEnviado) {
            try { sessionStorage.setItem('store:support-forgot-sent', '1'); } catch (_) {}
            sendAutoMessage();
        }
    } else if (window.SUPPORT_HAS_CHAT) {
        // El servidor ya pinto el hilo (reconocio la cookie). Solo falta
        // ponerse a sondear.
        readyToType();
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
                state.hasChat = true;
                (data.messages || []).forEach(render);
                applyChat(data.chat);
                readyToType();
                startPolling();
            })
            .catch(function () {});
    } else if (input) {
        input.focus();
    }
})();
