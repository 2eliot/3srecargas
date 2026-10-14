/* ── 3S Recargas — comprar una recarga dentro del chat de soporte ─────────
 *
 * Despues de elegir juego y paquete, esto lleva toda la compra por el chat:
 * ID (y su apodo, donde se puede verificar) -> metodo de pago -> codigo de
 * descuento -> datos para pagar -> foto del comprobante -> orden -> estado.
 *
 * Otra entrada: el cliente ya pagó y mandó su comprobante por el chat, la IA vio que
 * ese pago no tiene orden y que es a los datos de la tienda, y le ofreció el botón
 * "Crear mi orden con este comprobante". Es la misma compra, pero sin mostrar los
 * datos para pagar (ya pagó) y usando esa foto en lugar de pedirla otra vez.
 *
 * No hay una logica de compra propia: usa EXACTAMENTE las mismas rutas de la
 * tienda (POST /checkout/<id> para iniciar y confirmar, GET ?format=json para
 * cotizar, /order/<n>/auto-verify para seguir el pago). Por eso la orden es
 * una orden normal: sale en el panel del admin, da puntos a ese ID, avisa por
 * la campanita, etc. El chat solo pone la cara.
 *
 * Todo texto que viene del servidor entra con textContent, nunca innerHTML.
 */

(function () {
    'use strict';

    var B = window.SupportBot;
    if (!B) return;

    var el = B.el, botSays = B.botSays, userSays = B.userSays, typing = B.typing, addActions = B.addActions;
    var scrollDown = B.scrollDown, lock = B.lock, note = B.note, talkToSupport = B.talkToSupport;

    // ── Compra a medias: se guarda 2 horas ───────────────────────────────
    //
    // La compra del chat vive en la página. Si el teléfono la cierra en segundo plano (al ir a pagar en la app del banco) o el cliente la
    // recarga, antes todo se perdía y había que empezar de cero. Ahora cada paso guarda los datos de la compra en ESTE navegador:
    // al volver se ofrece "continuar" (se vuelve a pedir el pago con los mismos datos) y si pasan 2 horas sin tocarla se descarta sola.
    // Mientras haya una compra en marcha la página tampoco se recarga sola (version-watch.js pregunta a window.nxHoldReload).
    var DRAFT_KEY = 'store:support-buy-draft';
    var DRAFT_TTL_MS = 2 * 60 * 60 * 1000;
    var activeUntil = 0;

    window.nxHoldReload = function () { return activeUntil > Date.now(); };

    function readDraft() {
        try {
            var raw = localStorage.getItem(DRAFT_KEY);
            var d = raw ? JSON.parse(raw) : null;
            if (!d || !d.at || !d.gameId || !d.pkgId || Date.now() - d.at > DRAFT_TTL_MS) {
                if (raw) localStorage.removeItem(DRAFT_KEY);
                return null;
            }
            return d;
        } catch (_) { return null; }
    }

    function saveDraft(P) {
        if (!P || P.receipt) return;   // la compra con un comprobante ya mandado no se puede guardar (la foto no se guarda)
        activeUntil = Date.now() + DRAFT_TTL_MS;
        try {
            localStorage.setItem(DRAFT_KEY, JSON.stringify({
                at: Date.now(), gameId: P.game.id, pkgId: P.pkg.id, playerId: P.playerId || '', nick: P.nick || '', zone: P.zone || '',
                email: P.email || '', method: P.method ? P.method.code : '', code: P.code || ''
            }));
        } catch (_) { /* sin almacenamiento: la compra sigue, solo que no se podrá retomar */ }
    }

    function clearDraft() {
        activeUntil = 0;
        try { localStorage.removeItem(DRAFT_KEY); } catch (_) { /* nada */ }
    }

    function agoText(ms) {
        var minutes = Math.max(1, Math.round(ms / 60000));
        if (minutes < 60) return 'hace ' + minutes + ' min';
        var hours = Math.floor(minutes / 60);
        var rest = minutes % 60;
        return 'hace ' + hours + ' h' + (rest ? ' ' + rest + ' min' : '');
    }

    var MAX_PHOTO_BYTES = 15 * 1024 * 1024;
    var FIRST_CHECK_MS = 6000;       // igual que la pagina de la orden
    var DEFAULT_CHECK_MS = 30000;
    var MAX_CHECKS = 60;

    // ── Utilidades ───────────────────────────────────────────────────────

    function request(url, options) {
        options = options || {};
        options.credentials = 'same-origin';
        return fetch(url, options).then(function (r) {
            return r.json().catch(function () { return null; }).then(function (data) {
                return { status: r.status, data: data };
            });
        });
    }

    function bs(n) { return 'Bs ' + Math.round(Number(n)).toLocaleString('es-VE'); }
    function usd(n) { return '$' + Number(n).toFixed(2); }
    function moneyOf(currency, n) { return currency === 'usd' ? usd(n) : bs(n); }
    function copyValueOf(currency, n) { return currency === 'usd' ? Number(n).toFixed(2) : String(Math.round(Number(n))); }

    function copy(text, button) {
        function done() {
            var old = button.textContent;
            button.textContent = '¡Copiado!';
            setTimeout(function () { button.textContent = old; }, 1400);
        }
        function fallback() {
            var area = document.createElement('textarea');
            area.value = text;
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            try { document.execCommand('copy'); } catch (_) { /* nada */ }
            document.body.removeChild(area);
            done();
        }
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
        else fallback();
    }

    // Enlace de respaldo: la misma compra, terminada en la tienda.
    function shopUrl(P) {
        var url = '/?juego=' + encodeURIComponent(P.game.slug) + '&paquete=' + encodeURIComponent(P.pkg.id);
        if (P.game.id_kind === 'player' && P.playerId) url += '&jugador=' + encodeURIComponent(P.playerId);
        if (P.zone) url += '&zona=' + encodeURIComponent(P.zone);
        return url;
    }

    function problemActions(P, extra) {
        var actions = (extra || []).concat([
            { label: 'Terminar en la tienda', href: shopUrl(P) },
            { label: 'Hablar con soporte', action: talkToSupport }
        ]);
        return actions;
    }

    function summaryCard(rows) {
        var card = el('div', 'sc-card');
        rows.forEach(function (row) {
            var line = el('div', 'sc-card-row');
            line.appendChild(el('span', 'sc-card-k', row[0]));
            line.appendChild(el('strong', 'sc-card-v', row[1]));
            card.appendChild(line);
        });
        return card;
    }

    // Boton que abre el selector de fotos. `onFile(file)` recibe la foto ya revisada.
    function photoButton(bubble, label, onFile) {
        var picker = el('input');
        picker.type = 'file';
        picker.accept = 'image/*';
        picker.hidden = true;
        var btn = el('button', 'sc-act primary sc-attach-receipt', label);
        btn.type = 'button';
        btn.insertBefore(B.attachIcon(), btn.firstChild);
        var problem = el('span', 'sc-inline-error');
        problem.hidden = true;
        btn.addEventListener('click', function () { picker.click(); });
        picker.addEventListener('change', function () {
            var file = picker.files && picker.files[0];
            picker.value = '';
            if (!file) return;
            var message = !/^image\//.test(file.type) ? 'Adjunta una foto (JPG, PNG o WEBP).'
                : file.size > MAX_PHOTO_BYTES ? 'La foto pesa demasiado (máximo 15 MB).' : '';
            problem.hidden = !message;
            problem.textContent = message;
            if (!message) onFile(file);
        });
        var row = el('div', 'sc-actions');
        row.appendChild(btn);
        row.appendChild(picker);
        bubble.appendChild(row);
        bubble.appendChild(problem);
        scrollDown();
        return btn;
    }

    function showPhoto(file) {
        var mine = el('div', 'sc-msg client local');
        var preview = el('img', 'sc-media');
        var url = URL.createObjectURL(file);
        preview.src = url;
        preview.alt = 'Tu comprobante';
        preview.addEventListener('load', function () { URL.revokeObjectURL(url); });
        mine.appendChild(preview);
        B.thread.appendChild(mine);
        scrollDown();
    }

    // ── Inicio: ID, verificacion de apodo y zona ────────────────────────

    function start(game, pkg, cur, receipt) {
        var P = {
            game: game, pkg: pkg, cur: cur || { usd: true, bs: true },
            playerId: '', nick: '', zone: '', email: '', method: null, methods: null,
            code: '', quote: null, ocrRef: '',
            receipt: receipt && receipt.file ? receipt : null   // comprobante que ya está en el chat
        };
        saveDraft(P);
        askId(P);
    }

    // Al abrir la página: si quedó una compra sin terminar (menos de 2 horas), se ofrece continuarla.
    function resumeDraft() {
        var d = readDraft();
        if (!d || window.SUPPORT_AUTO_MESSAGE) return;
        activeUntil = d.at + DRAFT_TTL_MS;   // mientras decide, la página no se recarga sola
        Promise.all([request('/soporte/bot/paquetes/' + encodeURIComponent(d.gameId)), request('/soporte/bot/pago/metodos')]).then(function (res) {
            var pk = res[0].data;
            if (!pk || !pk.ok || !pk.game) { clearDraft(); return; }
            var cur = pk.currencies || { usd: true, bs: true };
            pk.game.currencies = cur;
            var pkg = (pk.packages || []).filter(function (p) { return p.id === d.pkgId; })[0];
            if (!pkg || !pkg.available) { clearDraft(); return; }
            var methods = res[1].data && res[1].data.ok ? res[1].data.methods || [] : [];
            var P = {
                game: pk.game, pkg: pkg, cur: cur, playerId: d.playerId, nick: d.nick, zone: d.zone, email: d.email,
                method: methods.filter(function (m) { return m.code === d.method; })[0] || null, methods: methods.length ? methods : null,
                code: d.code, quote: null, ocrRef: '', receipt: null
            };
            var bubble = botSays('Tienes una compra sin terminar (' + agoText(Date.now() - d.at) + '): ' + pk.game.name + ' · ' + pkg.name +
                '. Si ya pagaste, continúa y adjunta tu comprobante. ¿La seguimos?', true);
            addActions(bubble, [
                { label: 'Continuar mi compra', primary: true, action: function () { lock(bubble, null); userSays('Continuar mi compra'); continueDraft(P); } },
                { label: 'Empezar de nuevo', action: function () { lock(bubble, null); userSays('Empezar de nuevo'); clearDraft(); B.chooseGame(); } }
            ]);
        }).catch(function () { /* sin red: el borrador se conserva para la próxima vez */ });
    }

    // Retoma la compra en el primer paso que falte; con todos los datos, vuelve a pedir el pago y muestra los datos otra vez.
    function continueDraft(P) {
        var kind = P.game.id_kind;
        var hasId = kind === 'player' ? !!P.playerId : !!P.email;
        if (!hasId) { askId(P); return; }
        if (kind === 'player' && P.game.requires_zone && !P.zone) { askZone(P); return; }
        if (!P.method) { chooseMethod(P); return; }
        prepare(P);
    }

    // La foto del comprobante que el cliente mandó al chat, para usarla en la orden.
    function loadChatPhoto(src) {
        if (!src || src.indexOf('/static/uploads/') !== 0) return Promise.resolve(null);
        return fetch(src, { credentials: 'same-origin' }).then(function (r) {
            return r.ok ? r.blob() : null;
        }).then(function (blob) {
            if (!blob || !/^image\//.test(blob.type) || blob.size > MAX_PHOTO_BYTES) return null;
            return new File([blob], src.split('/').pop() || 'comprobante.jpg', { type: blob.type });
        }).catch(function () { return null; });
    }

    function startWithReceipt(messageId) {
        var photo = B.thread.querySelector('.sc-msg[data-id="' + String(messageId).replace(/\D/g, '') + '"] img.sc-media');
        var token = B.nextFlow();
        userSays('Crear mi orden con este comprobante');
        var stop = typing('Preparando tu comprobante…');
        loadChatPhoto(photo ? photo.getAttribute('src') : '').then(function (file) {
            stop();
            if (token !== B.flowNow()) return;
            if (file) botSays('Perfecto, creemos tu orden con ese comprobante. Como ya pagaste, solo necesito unos datos.');
            else botSays('No pude cargar la foto del comprobante, pero igual podemos crear tu orden: al final te la pido otra vez.');
            B.chooseGame(file ? { file: file } : null);
        });
    }

    B.thread.addEventListener('click', function (evt) {
        var btn = evt.target.closest ? evt.target.closest('[data-buy-receipt]') : null;
        if (btn) startWithReceipt(btn.getAttribute('data-buy-receipt'));
    });

    function idProblem(P) {
        var kind = P.game.id_kind;
        var label = P.game.player_id_label;
        return function (value) {
            if (!value) return kind === 'player' ? 'Escribe tu ' + label + '.' : 'Escribe tu correo.';
            if (kind !== 'player') return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value) ? '' : 'Revisa tu correo: parece que no está bien escrito.';
            if (P.game.player_id_numeric ? !/^\d{3,25}$/.test(value) : !/^[A-Za-z0-9_.@+-]{3,60}$/.test(value)) {
                return P.game.player_id_numeric ? 'El ' + label + ' solo lleva números.' : 'Revisa tu ' + label + ': tiene caracteres que no son válidos.';
            }
            return '';
        };
    }

    function askId(P) {
        var token = B.nextFlow();
        var kind = P.game.id_kind;
        var question = kind === 'player' ? 'Escribe tu ' + P.game.player_id_label + ' para ' + P.game.name + ':'
            : kind === 'email' ? 'Escribe el correo de tu cuenta de ' + P.game.name + ':'
            : 'Escribe el correo donde quieres recibir tu código:';
        var bubble = botSays(question);
        B.askValue(bubble, {
            placeholder: kind === 'player' ? P.game.player_id_label : 'correo@ejemplo.com',
            numeric: kind === 'player' && P.game.player_id_numeric,
            maxLength: kind === 'player' ? 60 : 120,
            validate: idProblem(P),
            onDone: function (value) {
                if (token !== B.flowNow()) return;
                userSays(value);
                if (kind === 'player') {
                    P.playerId = value;
                    P.email = '';
                    verifyNick(P);
                } else {
                    // Wallet: el "ID" es el correo. Tarjetas: solo hay correo de entrega.
                    P.playerId = kind === 'email' ? value.toLowerCase() : '';
                    P.email = value.toLowerCase();
                    afterId(P);
                }
            }
        });
    }

    // Donde la tienda sabe el apodo del jugador, se muestra para confirmar que es su cuenta.
    function verifyNick(P) {
        if (!P.game.verifiable) { afterId(P); return; }
        var token = B.nextFlow();
        var stop = typing('Verificando tu ID…');
        request('/store/player/verify/auto?uid=' + encodeURIComponent(P.playerId) + '&gid=' + encodeURIComponent(P.game.id))
            .then(function (res) {
                stop();
                if (token !== B.flowNow()) return;
                var data = res.data || {};
                if (res.status === 200 && data.ok) {
                    P.nick = data.nick || '';
                    var bubble = botSays('Encontré esta cuenta: ' + (P.nick || P.playerId) + '. ¿Es la tuya?');
                    addActions(bubble, [
                        { label: 'Sí, es mi cuenta', primary: true, action: function () { lock(bubble, null); userSays('Sí, es mi cuenta'); afterId(P); } },
                        { label: 'No, cambiar ID', action: function () { lock(bubble, null); userSays('No, cambiar ID'); askId(P); } }
                    ]);
                } else if (res.status === 403) {
                    afterId(P);       // este juego no tiene verificador: se sigue sin apodo
                } else if (res.status === 404 || res.status === 400) {
                    var nf = botSays('No encontré ese ID. Revísalo y escríbelo otra vez.');
                    addActions(nf, [{ label: 'Escribir el ID otra vez', primary: true, action: function () { lock(nf, null); askId(P); } }, { label: 'Hablar con soporte', action: talkToSupport }]);
                } else {
                    var er = botSays('No pude verificar tu ID ahora. Intenta de nuevo en unos segundos.');
                    addActions(er, [{ label: 'Reintentar', primary: true, action: function () { lock(er, null); verifyNick(P); } }, { label: 'Hablar con soporte', action: talkToSupport }]);
                }
            })
            .catch(function () {
                stop();
                if (token !== B.flowNow()) return;
                var er = botSays('Sin conexión. Revisa tu internet e intenta de nuevo.');
                addActions(er, [{ label: 'Reintentar', primary: true, action: function () { lock(er, null); verifyNick(P); } }]);
            });
    }

    function afterId(P) {
        saveDraft(P);
        if (P.game.requires_zone && P.game.id_kind === 'player') askZone(P);
        else chooseMethod(P);
    }

    function askZone(P) {
        var token = B.nextFlow();
        var label = P.game.zone_id_label;
        var bubble = botSays('Ahora escribe tu ' + label + ':');
        B.askValue(bubble, {
            placeholder: label,
            validate: function (value) {
                if (!value) return 'Escribe tu ' + label + '.';
                return /^[A-Za-z0-9_.@+-]{1,30}$/.test(value) ? '' : 'Revisa tu ' + label + '.';
            },
            onDone: function (value) {
                if (token !== B.flowNow()) return;
                userSays(value);
                P.zone = value;
                chooseMethod(P);
            }
        });
    }

    // ── Metodo de pago ───────────────────────────────────────────────────

    function chooseMethod(P) {
        var token = B.nextFlow();
        var stop = typing();
        request('/soporte/bot/pago/metodos').then(function (res) {
            stop();
            if (token !== B.flowNow()) return;
            var methods = res.data && res.data.ok ? res.data.methods || [] : null;
            if (!methods || !methods.length) {
                var bad = botSays('Ahora mismo no hay métodos de pago disponibles. Puedes intentarlo en la tienda o escribirnos.');
                addActions(bad, problemActions(P));
                return;
            }
            P.methods = methods;
            if (methods.length === 1) {
                P.method = methods[0];
                extras(P);
                return;
            }
            var bubble = botSays(P.receipt ? '¿Con qué método hiciste el pago?' : '¿Con qué método vas a pagar?');
            var group = el('div', 'sc-choices');
            methods.forEach(function (m) {
                var btn = el('button', 'sc-choice');
                btn.type = 'button';
                btn.appendChild(el('span', null, m.name));
                btn.appendChild(el('small', 'sc-choice-tag', m.currency === 'usd' ? '$' : 'Bs'));
                btn.addEventListener('click', function () {
                    lock(group, btn);
                    userSays(m.name);
                    P.method = m;
                    extras(P);
                });
                group.appendChild(btn);
            });
            bubble.appendChild(group);
            scrollDown();
        }).catch(function () {
            stop();
            if (token !== B.flowNow()) return;
            var er = botSays('Sin conexión. Revisa tu internet e intenta de nuevo.');
            addActions(er, [{ label: 'Reintentar', primary: true, action: function () { lock(er, null); chooseMethod(P); } }]);
        });
    }

    // ── Codigo de descuento (opcional) ───────────────────────────────────

    function extras(P) {
        saveDraft(P);
        var token = B.nextFlow();
        var bubble = botSays((P.receipt ? 'Antes de crear tu orden' : 'Antes de pagar') + ': ¿tienes un código de descuento? Es opcional.');
        var state = el('div', 'sc-extras');
        bubble.appendChild(state);
        var holder = el('div', 'sc-extras-form');
        bubble.appendChild(holder);

        function paintState() {
            state.textContent = '';
            if (P.code) state.appendChild(el('span', 'sc-chip-on', 'Código: ' + P.code));
        }

        function inlineField(opts) {
            holder.textContent = '';
            B.askValue(holder, {
                placeholder: opts.placeholder,
                maxLength: opts.maxLength,
                button: 'Guardar',
                validate: opts.validate,
                onDone: function (value) { opts.save(value); holder.textContent = ''; paintState(); }
            });
        }

        var row = el('div', 'sc-actions');
        function actionButton(label, onClick, primary) {
            var b = el('button', 'sc-act' + (primary ? ' primary' : ''), label);
            b.type = 'button';
            b.addEventListener('click', onClick);
            row.appendChild(b);
            return b;
        }
        actionButton('Tengo un código', function () {
            inlineField({
                placeholder: 'CÓDIGO', maxLength: 30,
                validate: function (v) { return /^[A-Za-z0-9_-]{2,30}$/.test(v) ? '' : 'El código solo lleva letras, números y guiones.'; },
                save: function (v) { P.code = v.toUpperCase(); }
            });
        });
        actionButton('Continuar →', function () {
            if (token !== B.flowNow()) return;
            Array.prototype.forEach.call(bubble.querySelectorAll('button, input'), function (n) { n.disabled = true; });
            userSays('Continuar' + (P.code ? ' con el código ' + P.code : ''));
            prepare(P);
        }, true);
        bubble.appendChild(row);
        scrollDown();
    }

    // ── Preparar el pago: iniciar la compra y cotizar ───────────────────

    function checkoutForm(P) {
        var form = new FormData();
        form.set('ajax', '1');
        form.set('player_id', P.playerId);
        form.set('player_nickname', P.nick);
        form.set('zone_id', P.zone);
        form.set('email', P.email);
        form.set('phone', '');
        form.set('payment_method', P.method.code);
        form.set('affiliate_code', P.code);
        form.set('client_version', 'support-chat');
        return form;
    }

    // Inicia la compra en el servidor y pide la cotizacion. Devuelve una promesa con
    // la cotizacion o lanza un Error con el mensaje para el cliente.
    function initAndQuote(P) {
        var url = '/checkout/' + encodeURIComponent(P.pkg.id);
        return request(url, { method: 'POST', body: checkoutForm(P) }).then(function (res) {
            if (!res.data || !res.data.ok) throw new Error((res.data && res.data.message) || 'No pude preparar tu pago. Intenta de nuevo.');
            return request(url + '?format=json');
        }).then(function (res) {
            if (!res.data || !res.data.ok) throw new Error((res.data && res.data.message) || 'No pude calcular tu pago. Intenta de nuevo.');
            P.quote = res.data;
            return res.data;
        });
    }

    function prepare(P) {
        saveDraft(P);
        var token = B.nextFlow();
        var stop = typing('Preparando tu pago…');
        initAndQuote(P).then(function (q) {
            stop();
            if (token !== B.flowNow()) return;
            if (P.code && !q.has_discount) {
                var bubble = botSays('El código ' + P.code + ' no es válido o no aplica para este paquete. ¿Qué hacemos?');
                addActions(bubble, [
                    { label: 'Seguir sin código', primary: true, action: function () { lock(bubble, null); userSays('Seguir sin código'); P.code = ''; prepare(P); } },
                    { label: 'Probar otro código', action: function () { lock(bubble, null); userSays('Probar otro código'); retryCode(P); } }
                ]);
                return;
            }
            showPayment(P);
        }).catch(function (err) {
            stop();
            if (token !== B.flowNow()) return;
            var bubble = botSays(err && err.message && err.message !== 'Failed to fetch' ? err.message : 'Sin conexión. Revisa tu internet e intenta de nuevo.');
            addActions(bubble, problemActions(P, [
                { label: 'Corregir mis datos', primary: true, action: function () { lock(bubble, null); askId(P); } }
            ]));
        });
    }

    function retryCode(P) {
        var token = B.nextFlow();
        var bubble = botSays('Escribe el código de descuento:');
        B.askValue(bubble, {
            placeholder: 'CÓDIGO', maxLength: 30,
            validate: function (v) { return /^[A-Za-z0-9_-]{2,30}$/.test(v) ? '' : 'El código solo lleva letras, números y guiones.'; },
            onDone: function (v) { if (token !== B.flowNow()) return; userSays(v.toUpperCase()); P.code = v.toUpperCase(); prepare(P); }
        });
    }

    // ── Datos para pagar ─────────────────────────────────────────────────

    function showPayment(P) {
        var q = P.quote;
        var currency = q.display_currency;
        var rows = [['Juego', P.game.name], ['Paquete', P.pkg.name]];
        if (P.game.id_kind === 'player') rows.push([P.game.player_id_label, P.nick ? P.playerId + ' · ' + P.nick : P.playerId]);
        else rows.push(['Correo', P.email]);
        if (P.zone) rows.push([P.game.zone_id_label, P.zone]);
        rows.push(['Método', q.selected_method_name || P.method.name]);
        if (q.has_discount) {
            rows.push(['Precio normal', moneyOf(currency, q.original_amount)]);
            rows.push(['Descuento', '− ' + moneyOf(currency, q.discount_amount)]);
        }
        rows.push(['Total a pagar', moneyOf(currency, q.display_amount)]);

        var first = botSays(P.receipt ? 'Listo, esto es tu compra:' : 'Listo, esto es lo que vas a pagar:');
        first.appendChild(summaryCard(rows));

        if (P.receipt) {
            usePaidReceipt(P, q);
            return;
        }

        var bubble = botSays(q.binance_auto ? 'Paga por Binance Pay con estos datos:' : 'Haz el pago con estos datos:');
        var card = el('div', 'sc-card sc-pay');
        var copyLines = [];   // lo mismo que muestra la tarjeta, para "Copiar todos los datos"
        function payRow(label, value) {
            copyLines.push(label + ': ' + value);
            var line = el('div', 'sc-pay-row');
            var text = el('div', 'sc-pay-text');
            text.appendChild(el('small', null, label));
            text.appendChild(el('strong', null, value));
            var btn = el('button', 'sc-copy', 'Copiar');
            btn.type = 'button';
            btn.addEventListener('click', function () { copy(value, btn); });
            line.appendChild(text);
            line.appendChild(btn);
            card.appendChild(line);
        }
        if (q.binance_auto) {
            if (q.binance_wallet) payRow('Dirección Binance Pay', q.binance_wallet);
            payRow('Código para la "Nota del beneficiario"', q.binance_code || '');
        } else {
            (P.method.rows || []).forEach(function (r) { payRow(r.label, r.value); });
            if (!(P.method.rows || []).length) card.appendChild(el('p', 'sc-card-text', 'Realiza el pago con ' + P.method.name + ' por el monto indicado.'));
        }
        var amountRow = el('div', 'sc-pay-row sc-pay-amount');
        var amountText = el('div', 'sc-pay-text');
        amountText.appendChild(el('small', null, 'Monto exacto'));
        amountText.appendChild(el('strong', null, moneyOf(currency, q.display_amount)));
        var amountBtn = el('button', 'sc-copy', 'Copiar');
        amountBtn.type = 'button';
        amountBtn.addEventListener('click', function () { copy(copyValueOf(currency, q.display_amount), amountBtn); });
        amountRow.appendChild(amountText);
        amountRow.appendChild(amountBtn);
        card.appendChild(amountRow);
        bubble.appendChild(card);

        // Igual que la tienda: todos los datos y el monto en un solo copiado.
        var copyAll = el('button', 'sc-copy-all', 'Copiar todos los datos');
        copyAll.type = 'button';
        copyAll.addEventListener('click', function () {
            copy('--- DATOS DE PAGO ---\n' + copyLines.join('\n') + '\nMonto: ' + moneyOf(currency, q.display_amount), copyAll);
        });
        bubble.appendChild(copyAll);

        var rules = el('ul', 'sc-rules');
        if (q.binance_auto) {
            rules.appendChild(el('li', null, 'Escribe el código en la "Nota del beneficiario": sin él tu pago NO se verifica solo.'));
            rules.appendChild(el('li', null, 'Paga el monto exacto.'));
        } else {
            rules.appendChild(el('li', null, 'Paga SOLO a estos datos.'));
            rules.appendChild(el('li', null, 'Paga el monto exacto que ves aquí.'));
            rules.appendChild(el('li', null, 'Guarda tu comprobante: tiene la referencia que necesito.'));
        }
        bubble.appendChild(rules);

        var actions = [];
        if (q.payment_video && q.payment_video.enabled && q.payment_video.file) {
            actions.push({ label: 'Ver tutorial', href: '/static/uploads/' + encodeURI(q.payment_video.file) });
        }
        actions.push({ label: 'Cancelar compra', action: function () {
            lock(bubble, null);
            userSays('Cancelar compra');
            botSays('Listo, cancelé la compra. Cuando quieras retomarla, toca "Comprar recarga".');
            clearDraft();
            B.nextFlow();
        } });
        addActions(bubble, actions);

        var ask = botSays(q.binance_auto
            ? 'Cuando hayas pagado, toca "Ya pagué". Si quieres, adjunta también tu comprobante.'
            : 'Cuando hayas pagado, adjunta aquí la foto de tu comprobante 👇');
        if (q.binance_auto) {
            var paid = el('button', 'sc-act primary', 'Ya pagué ✓');
            paid.type = 'button';
            paid.addEventListener('click', function () {
                Array.prototype.forEach.call(ask.querySelectorAll('button'), function (b) { b.disabled = true; });
                userSays('Ya pagué');
                confirmOrder(P, null, { reference: q.binance_code || '' });
            });
            var row = el('div', 'sc-actions');
            row.appendChild(paid);
            ask.appendChild(row);
            photoButton(ask, 'Adjuntar comprobante (opcional)', function (file) {
                Array.prototype.forEach.call(ask.querySelectorAll('button'), function (b) { b.disabled = true; });
                showPhoto(file);
                confirmOrder(P, file, { reference: q.binance_code || '' });
            });
        } else {
            photoButton(ask, 'Adjuntar comprobante', function (file) {
                Array.prototype.forEach.call(ask.querySelectorAll('button'), function (b) { b.disabled = true; });
                showPhoto(file);
                readReceipt(P, file);
            });
        }
    }

    // Ya pagó: no se muestran los datos para pagar, se usa el comprobante que mandó.
    function usePaidReceipt(P, q) {
        var token = B.nextFlow();
        if (q.binance_auto) {
            // Binance Pay se verifica con un código que va en la nota del pago: un pago hecho antes sin él no sirve.
            var bubble = botSays('Con ' + (q.selected_method_name || P.method.name) + ' el pago se verifica con un código que va en la nota del pago, y tu comprobante es de un pago que ya hiciste. Elige el método con el que pagaste, o escríbenos.');
            addActions(bubble, [
                { label: 'Elegir otro método', primary: true, action: function () { lock(bubble, null); userSays('Elegir otro método'); chooseMethod(P); } },
                { label: 'Hablar con soporte', action: talkToSupport }
            ]);
            return;
        }
        botSays('Usaré el comprobante que me enviaste, no tienes que pagar de nuevo. Reviso que el monto cubra el total.');
        readReceipt(P, P.receipt.file);
    }

    // ── Comprobante: leer la referencia y confirmar ─────────────────────

    function readReceipt(P, file) {
        var token = B.nextFlow();
        var stop = typing('Leyendo tu comprobante…');
        var form = new FormData();
        form.append('payment_capture', file);
        request('/api/extract-payment-reference', { method: 'POST', body: form }).then(function (res) {
            stop();
            if (token !== B.flowNow()) return;
            P.ocrRef = res.data && res.data.ok ? String(res.data.reference || '') : '';
            referenceForm(P, file);
        }).catch(function () {
            stop();
            if (token !== B.flowNow()) return;
            P.ocrRef = '';
            referenceForm(P, file);
        });
    }

    // Referencia (revisada por el cliente) y, en los metodos que lo piden, telefono y cedula de quien pagó.
    function referenceForm(P, file) {
        var token = B.nextFlow();
        // La cotizacion de la tienda dice si este metodo pide telefono y cedula de quien pago.
        var payer = !!(P.quote && P.quote.payer_verification);
        var bubble = botSays(P.ocrRef
            ? 'Leí esta referencia en tu comprobante. Revisa que sea la correcta' + (payer ? ' y completa tus datos:' : ':')
            : 'No pude leer la referencia en la foto. Escríbela tú (la ves en tu comprobante)' + (payer ? ' y completa tus datos:' : ':'));
        var form = el('form', 'sc-form');

        function field(label, attrs) {
            var wrap = el('label', 'sc-field');
            wrap.appendChild(el('span', null, label));
            var input = el('input');
            Object.keys(attrs).forEach(function (k) { input.setAttribute(k, attrs[k]); });
            wrap.appendChild(input);
            form.appendChild(wrap);
            return input;
        }
        var ref = field('Referencia del pago', { type: 'text', maxlength: '30', autocomplete: 'off', placeholder: 'Ej: 123456' });
        ref.value = P.ocrRef;
        var phone, dniType, dni;
        if (payer) {
            phone = field('Teléfono desde el que pagaste', { type: 'tel', inputmode: 'numeric', maxlength: '20', autocomplete: 'off', placeholder: '0414 1234567' });
            var dniWrap = el('div', 'sc-field sc-field-dni');
            dniWrap.appendChild(el('span', null, 'Cédula de quien pagó'));
            var inner = el('div', 'sc-dni');
            dniType = el('select');
            ['V', 'E', 'J', 'G', 'P'].forEach(function (t) { var o = el('option', null, t); o.value = t; dniType.appendChild(o); });
            dni = el('input');
            dni.type = 'text'; dni.maxLength = 20; dni.autocomplete = 'off'; dni.placeholder = '12345678';
            inner.appendChild(dniType);
            inner.appendChild(dni);
            dniWrap.appendChild(inner);
            form.appendChild(dniWrap);
        }
        var send = el('button', 'sc-inline-send sc-form-send', 'Confirmar pago');
        send.type = 'submit';
        form.appendChild(send);
        var error = el('span', 'sc-inline-error');
        error.hidden = true;
        bubble.appendChild(form);
        bubble.appendChild(error);

        form.addEventListener('submit', function (evt) {
            evt.preventDefault();
            var values = { reference: ref.value.trim() };
            var problem = '';
            if (!values.reference) problem = 'Escribe la referencia del pago.';
            else if (payer) {
                var digits = phone.value.replace(/\D/g, '');
                var dniDigits = dni.value.replace(/\D/g, '');
                if (digits.length < 10) problem = 'Escribe el teléfono completo (10 dígitos o más).';
                else if (!dniDigits) problem = 'Escribe la cédula de quien hizo el pago.';
                else { values.phone = phone.value.trim(); values.dniType = dniType.value; values.dni = dni.value.trim(); }
            }
            error.hidden = !problem;
            error.textContent = problem;
            if (problem || token !== B.flowNow()) return;
            Array.prototype.forEach.call(bubble.querySelectorAll('input, select, button'), function (n) { n.disabled = true; });
            userSays('Referencia ' + values.reference);
            confirmOrder(P, file, values);
        });
        scrollDown();
        if (window.matchMedia && window.matchMedia('(min-width: 641px)').matches) ref.focus();
    }

    function confirmForm(P, file, values) {
        var form = new FormData();
        form.set('stage', 'confirm');
        form.set('ajax', '1');
        form.set('confirm_token', P.quote.confirm_token);
        form.set('payment_reference', values.reference || '');
        form.set('ai_extracted_reference', P.ocrRef || '');
        if (file) form.set('payment_capture', file);
        if (values.phone) form.set('payer_phone', values.phone);
        if (values.dniType) form.set('payer_dni_type', values.dniType);
        if (values.dni) form.set('payer_dni_number', values.dni);
        return form;
    }

    function confirmOrder(P, file, values, retried) {
        var token = B.nextFlow();
        var stop = typing('Registrando tu orden…');
        request('/checkout/' + encodeURIComponent(P.pkg.id), { method: 'POST', body: confirmForm(P, file, values) })
            .then(function (res) {
                stop();
                if (token !== B.flowNow()) return;
                var data = res.data || {};
                if (data.ok) { orderCreated(P, data); return; }
                var code = data.code || '';
                if ((code === 'token_expired' || code === 'session_expired') && !retried) {
                    // La compra "se enfrió" (sesión o llave vencidas): se prepara de nuevo y se reintenta una vez.
                    var before = P.quote ? Number(P.quote.display_amount) : null;
                    var again = typing('Un momento…');
                    initAndQuote(P).then(function (q) {
                        again();
                        if (before !== null && Math.abs(Number(q.display_amount) - before) > 0.009) {
                            var changed = botSays('El monto cambió: ahora son ' + moneyOf(q.display_currency, q.display_amount) + '. Si ya pagaste el monto anterior, escríbenos para ayudarte.');
                            addActions(changed, [
                                { label: 'Confirmar con el monto nuevo', primary: true, action: function () { lock(changed, null); confirmOrder(P, file, values, true); } },
                                { label: 'Hablar con soporte', action: talkToSupport }
                            ]);
                            return;
                        }
                        confirmOrder(P, file, values, true);
                    }).catch(function (err) {
                        again();
                        var bubble = botSays((err && err.message) || 'No pude registrar tu orden. Intenta de nuevo.');
                        addActions(bubble, problemActions(P));
                    });
                    return;
                }
                var message = data.message || 'No pude registrar tu orden. Intenta de nuevo.';
                var bubble = botSays(message);
                var actions = [];
                if (code === 'reference_in_use') {
                    bubble.appendChild(el('span', 'sc-bot-note', 'Si acabas de enviarla y no viste la confirmación, revisa tus notificaciones 🔔 o toca "Estado de mi recarga".'));
                    actions.push({ label: 'Corregir la referencia', primary: true, action: function () { lock(bubble, null); referenceForm(P, file); } });
                } else if (file) {
                    actions.push({ label: 'Reintentar', primary: true, action: function () { lock(bubble, null); referenceForm(P, file); } });
                } else {
                    actions.push({ label: 'Reintentar', primary: true, action: function () { lock(bubble, null); confirmOrder(P, null, values); } });
                }
                addActions(bubble, problemActions(P, actions));
            })
            .catch(function () {
                stop();
                if (token !== B.flowNow()) return;
                var bubble = botSays('Sin conexión. Revisa tu internet e intenta de nuevo.');
                addActions(bubble, [{ label: 'Reintentar', primary: true, action: function () { lock(bubble, null); confirmOrder(P, file, values); } }, { label: 'Hablar con soporte', action: talkToSupport }]);
            });
    }

    // ── Orden creada: seguirla hasta el final ────────────────────────────

    function orderCreated(P, data) {
        var number = data.order_number;
        clearDraft();   // la orden ya existe: no queda compra a medias
        activeUntil = Date.now() + 20 * 60 * 1000;   // y 20 min más sin recargar la página solos, mientras se verifica el pago aquí
        // La campanita del sitio sigue las ordenes hechas en este navegador.
        try {
            if (window.SiteNotifications && window.SiteNotifications.registerOrder) {
                window.SiteNotifications.registerOrder(number);
                if (window.SiteNotifications.refresh) window.SiteNotifications.refresh();   // que se vea ya en la campanita
            }
        } catch (_) { /* nada */ }
        // Si ya tenia una orden pendiente igual, la tienda devuelve ESA (y trae un mensaje).
        var bubble = botSays(data.message
            ? 'ℹ️ Ya tenías una orden pendiente para esta compra: la #' + number + '. Te sigo mostrando su estado.'
            : '✅ Recibí tu comprobante y registré tu orden #' + number + '. Ahora verifico tu pago.');
        (data.warnings || []).forEach(function (w) { bubble.appendChild(el('span', 'sc-bot-note', w)); });
        addActions(bubble, [{ label: 'Ver mi orden ›', href: data.order_url || '/order/' + number }]);
        watchOrder(P, number);
    }

    function watchOrder(P, number) {
        var live = botSays('Verificando tu pago…');
        var liveText = live.querySelector('.sc-msg-body');
        var dots = el('span', 'sc-dots');
        dots.appendChild(el('i')); dots.appendChild(el('i')); dots.appendChild(el('i'));
        live.appendChild(dots);
        live.classList.add('sc-live');
        var checks = 0;

        function setText(text) { if (text) liveText.textContent = text; }
        function finish() { if (dots.parentNode) dots.parentNode.removeChild(dots); live.classList.remove('sc-live'); }

        function poll() {
            checks += 1;
            request('/order/' + encodeURIComponent(number) + '/auto-verify', { method: 'POST', headers: { 'Content-Type': 'application/json' } })
                .then(function (res) {
                    var d = res.data || {};
                    var status = d.status || 'pending';
                    if (status === 'completed') { finish(); setText('✅ Pago verificado.'); done(P, number, d); return; }
                    if (status !== 'pending') { finish(); setText('Tu orden está en estado: ' + (d.status_label || status) + '.'); problem(P, number, 'Si necesitas ayuda con esta orden, escríbenos.'); return; }
                    if (d.underpaid) { finish(); setText('Tu pago llegó incompleto.'); underpaid(P, number, d.missing_amount); return; }
                    if (d.manual_review_required || (d.payment_verified && d.stop_polling)) { finish(); setText('✅ Pago verificado.'); manual(P, number, d.message); return; }
                    if (d.stop_polling) { finish(); setText(d.message || 'Tu orden se procesa manualmente.'); manual(P, number, ''); return; }
                    setText(d.message || 'Verificando tu pago… puede tardar unos minutos.');
                    if (checks >= MAX_CHECKS) { finish(); setText('Sigo sin ver tu pago.'); problem(P, number, 'Tu orden queda registrada y seguimos revisando. Si ya pasó un buen rato, escríbenos con tu número de orden.'); return; }
                    var wait = Number(d.next_retry_in_seconds) > 0 ? Number(d.next_retry_in_seconds) * 1000 : DEFAULT_CHECK_MS;
                    setTimeout(poll, Math.max(wait, 5000));
                })
                .catch(function () { setTimeout(poll, 45000); });
        }
        setTimeout(poll, FIRST_CHECK_MS);
    }

    function done(P, number, d) {
        var isCode = P.game.id_kind === 'none';
        var bubble = botSays(isCode
            ? '🎉 ¡Listo! Tu código de ' + P.pkg.name + ' ya está disponible. Ábrelo en tu orden.'
            : '🎉 ¡Listo! Tu recarga de ' + P.pkg.name + ' para ' + (P.nick || P.playerId) + ' fue enviada. Si no la ves, reinicia el juego.');
        addActions(bubble, [{ label: isCode ? 'Ver mi código ›' : 'Ver mi orden ›', href: '/order/' + number, primary: true }]);
    }

    function manual(P, number, message) {
        var bubble = botSays('Tu pago ya está verificado. ' + (message && message.indexOf('Pabilo') < 0 ? message + ' ' : '') +
            'Esta recarga la procesa nuestro equipo en orden de llegada. Te avisamos por la campanita 🔔.');
        addActions(bubble, [{ label: 'Ver mi orden ›', href: '/order/' + number, primary: true }, { label: 'Hablar con soporte', action: talkToSupport }]);
    }

    function problem(P, number, text) {
        var bubble = botSays(text);
        addActions(bubble, [{ label: 'Ver mi orden ›', href: '/order/' + number, primary: true }, { label: 'Hablar con soporte', action: talkToSupport }]);
    }

    // Pago a medias: se pide el comprobante del pago que completa lo que falta.
    function underpaid(P, number, missing) {
        var bubble = botSays('Tu pago llegó incompleto' + (missing ? ': faltan Bs ' + missing : '') +
            '. Haz el pago por lo que falta y adjunta aquí el comprobante de ESE pago nuevo (no el anterior).');
        photoButton(bubble, 'Adjuntar comprobante del resto', function (file) {
            Array.prototype.forEach.call(bubble.querySelectorAll('button'), function (b) { b.disabled = true; });
            showPhoto(file);
            payRemainder(P, number, file);
        });
        addActions(bubble, [{ label: 'Ver mi orden ›', href: '/order/' + number }, { label: 'Hablar con soporte', action: talkToSupport }]);
    }

    function payRemainder(P, number, file) {
        var stop = typing('Verificando el pago restante…');
        var form = new FormData();
        form.set('payment_capture', file);
        form.set('payment_reference', '');
        request('/order/' + encodeURIComponent(number) + '/complete-payment', { method: 'POST', body: form })
            .then(function (res) {
                stop();
                var d = res.data || {};
                if (d.ok && d.verified && d.covered) {
                    if (d.status === 'completed') done(P, number, d);
                    else manual(P, number, d.message);
                } else if (d.ok && d.verified && d.missing_amount) {
                    underpaid(P, number, d.missing_amount);
                } else {
                    var bubble = botSays(d.message || 'No pude verificar ese pago todavía.');
                    var retry = function () { lock(bubble, null); underpaid(P, number, null); };
                    addActions(bubble, [
                        { label: 'Probar con otro comprobante', primary: true, action: retry },
                        { label: 'Hablar con soporte', action: talkToSupport }
                    ]);
                }
            })
            .catch(function () {
                stop();
                var bubble = botSays('Sin conexión. Revisa tu internet e intenta de nuevo.');
                addActions(bubble, [{ label: 'Reintentar', primary: true, action: function () { lock(bubble, null); underpaid(P, number, null); } }]);
            });
    }

    resumeDraft();

    window.SupportBuy = { start: start, startWithReceipt: startWithReceipt };
})();
