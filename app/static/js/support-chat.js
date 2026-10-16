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

    // Un mensaje puede llegar dos veces (la respuesta del envío y un sondeo que ya iba en camino, o dos sondeos al mismo tiempo mientras
    // el asistente "escribe"): cada uno se pinta una sola vez, el id lo decide.
    function alreadyShown(id) {
        return !!id && !!thread.querySelector('.sc-msg[data-id="' + id + '"]');
    }

    // textContent y nunca innerHTML: el cuerpo lo escribe una persona y
    // llega por JSON, sin pasar por el autoescape de Jinja.
    function render(message) {
        if (alreadyShown(message.id)) {
            state.lastId = Math.max(state.lastId, message.id || 0);
            return;
        }
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
        if (presenceEl && chat.status === 'closed') { presenceEl.textContent = 'Chat cerrado'; presenceEl.hidden = false; }
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

    // Pasadas 24 h desde el último mensaje el servidor ya no reconoce el chat (410): este
    // navegador lo olvida (token y cookie) y vuelve a la pantalla de bienvenida.
    function chatExpired() {
        clearInterval(state.timer);
        state.timer = null;
        writeToken('');
        state.hasChat = false;
        location.reload();
    }

    function poll() {
        if (!state.token) return;
        // Una sola pregunta a la vez: con el asistente escribiendo se pregunta cada 1,5 s y si el servidor tarda se amontonaban
        // varias iguales. Si una se quedó colgada más de 15 s, se deja pasar otra.
        if (state.polling && Date.now() - state.polling < 15000) return;
        state.polling = Date.now();
        fetch('/soporte/hilo?after_id=' + state.lastId, {
            credentials: 'same-origin',
            headers: headers()
        })
            .then(function (r) {
                if (r.status === 410) { chatExpired(); return null; }
                return r.json();
            })
            .then(function (data) {
                if (!data || !data.ok) return;
                (data.messages || []).forEach(render);
                applyChat(data.chat);
                (data.deleted_ids || []).forEach(applyDeleted);
            })
            .catch(function () {})
            .then(function () { state.polling = 0; });
    }

    // 5 s con la pestana a la vista. En segundo plano no se pregunta nada:
    // ahi es donde se van las peticiones que nadie va a leer.
    function startPolling() {
        if (state.timer) return;
        setTimeout(offerPush, 2500);   // ya hay chat: se ofrece (una vez) avisar al teléfono cuando soporte responda
        state.timer = setInterval(function () {
            if (document.visibilityState === 'visible') poll();
        }, 5000);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') poll();
        });
    }

    // ── Avisos al teléfono cuando soporte responde ───────────────────────
    //
    // Si el cliente acepta, el navegador le muestra "Soporte te respondió" (con el botón "Ir al chat") aunque tenga la página cerrada. La
    // suscripción se liga a ESTE chat con su token (el servidor la guarda en push_subscriptions.chat_id y el aviso sale cuando el equipo escribe).
    var PUSH_ASKED_KEY = 'store:support-push-asked';

    function pushSupported() {
        return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
    }

    function keyToBytes(base64String) {
        var padding = '='.repeat((4 - base64String.length % 4) % 4);
        var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        var raw = window.atob(base64);
        var out = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
        return out;
    }

    function linkPush(registration) {
        return registration.pushManager.getSubscription().then(function (existing) {
            if (existing) return existing;
            return fetch('/push/vapid-public-key').then(function (r) { return r.json(); }).then(function (data) {
                return registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyToBytes(data.publicKey) });
            });
        }).then(function (subscription) {
            var raw = subscription.toJSON();
            return fetch('/push/subscribe', {
                method: 'POST',
                credentials: 'same-origin',
                headers: headers({ 'Content-Type': 'application/json' }),
                body: JSON.stringify({ endpoint: raw.endpoint, keys: raw.keys, support_token: state.token })
            });
        });
    }

    function enablePush() {
        navigator.serviceWorker.register('/sw.js').then(function (registration) {
            return Notification.requestPermission().then(function (permission) {
                return permission === 'granted' ? linkPush(registration) : null;
            });
        }).catch(function () {});
    }

    function offerPush() {
        if (!pushSupported() || !state.hasChat || !state.token) return;
        if (Notification.permission === 'granted') {
            // Ya dio el permiso en otra página de la tienda: solo se liga este chat a su dispositivo, sin preguntar nada.
            navigator.serviceWorker.register('/sw.js').then(linkPush).catch(function () {});
            return;
        }
        if (Notification.permission !== 'default' || document.getElementById('scPushOffer')) return;
        var asked = '';
        try { asked = localStorage.getItem(PUSH_ASKED_KEY) || ''; } catch (_) {}
        if (asked) return;

        var box = document.createElement('div');
        box.id = 'scPushOffer';
        box.className = 'sc-msg system sc-push-offer';
        var text = document.createElement('span');
        text.className = 'sc-msg-body';
        text.textContent = '🔔 ¿Quieres que te avisemos en tu teléfono cuando soporte te responda?';
        box.appendChild(text);
        var row = document.createElement('div');
        row.className = 'sc-actions';
        function remember() { try { localStorage.setItem(PUSH_ASKED_KEY, '1'); } catch (_) {} }
        var yes = document.createElement('button');
        yes.type = 'button';
        yes.className = 'sc-act primary';
        yes.textContent = 'Activar avisos';
        yes.addEventListener('click', function () { remember(); box.remove(); enablePush(); });
        var no = document.createElement('button');
        no.type = 'button';
        no.className = 'sc-act';
        no.textContent = 'Ahora no';
        no.addEventListener('click', function () { remember(); box.remove(); });
        row.appendChild(yes);
        row.appendChild(no);
        box.appendChild(row);
        thread.appendChild(box);
        scrollDown();
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

    // Aviso pasajero dentro del chat (7 s), sin ventanas del sistema: antes un fallo de red hacía "desaparecer" el mensaje sin explicar nada.
    function sendNotice(text) {
        var n = document.createElement('div');
        n.className = 'sc-msg system';
        var b = document.createElement('span');
        b.className = 'sc-msg-body';
        b.textContent = text;
        n.appendChild(b);
        thread.appendChild(n);
        scrollDown();
        setTimeout(function () { if (n.parentNode) n.parentNode.removeChild(n); }, 7000);
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
            .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
            .then(function (res) {
                var data = res.data;
                if (res.status === 410) {
                    // La pestaña seguía abierta pasadas 24 h: ese chat ya no existe, se empieza uno nuevo con este mensaje.
                    writeToken('');
                    state.hasChat = false;
                    return startChat(body, false);
                }
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
            })
            .catch(function (err) {
                state.sending = false;
                // Devolver el texto: perder lo que alguien acaba de
                // escribir por un fallo de red es la peor forma de
                // recibir a quien ya viene molesto.
                if (input) input.value = body;
                sendNotice(err && err.fromServer ? err.message : 'No se pudo enviar. Revisa tu internet y toca enviar otra vez.');
            });
    });

    if (input) {
        // Enter envia, Shift+Enter salta de linea.
        input.addEventListener('keydown', function (evt) {
            if (evt.key === 'Enter' && !evt.shiftKey) {
                evt.preventDefault();
                if (composer.requestSubmit) composer.requestSubmit();
                else composer.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));   // navegadores viejos sin requestSubmit
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

            // Sin chat todavia, el adjunto tambien lo crea. Si el chat ya expiro (24 h), se abre
            // uno nuevo y se reintenta una sola vez.
            function upload(retried) {
                var ready = state.hasChat ? Promise.resolve() : startChat('', true);
                return ready
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
                    .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
                    .then(function (res) {
                        if (res.status === 410 && !retried) {
                            writeToken('');
                            state.hasChat = false;
                            return upload(true);
                        }
                        return res.data;
                    });
            }
            upload(false)
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
    // El servidor dijo que el chat de este navegador ya expiró (24 h): se olvida el respaldo del token.
    if (window.SUPPORT_RESET_TOKEN) writeToken('');

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
