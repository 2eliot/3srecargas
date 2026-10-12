/* ── 3S Recargas — menú de botones del chat de soporte ───────────────────
 *
 * Comprar recarga y Estado de mi recarga.
 * Es un menú fijo (no usa IA): pide los datos a /soporte/bot/* y los pinta
 * como burbujas del asistente dentro del hilo. Nada de esto crea un chat ni
 * se guarda: son consultas sueltas, y el hilo real solo nace cuando el
 * cliente escribe (support-chat.js).
 *
 * Todo texto que viene del servidor entra con textContent, nunca innerHTML.
 */

(function () {
    'use strict';

    var cfg = window.SUPPORT_BOT;
    var thread = document.getElementById('scThread');
    var quick = document.getElementById('scQuick');
    var input = document.getElementById('scInput');

    if (!cfg || !cfg.enabled || !thread || !quick) return;

    // Cada acción del cliente sube este número: una respuesta que llega
    // tarde de una consulta anterior se descarta en vez de pintarse revuelta.
    var flow = 0;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function scrollDown() { thread.scrollTop = thread.scrollHeight; }

    // Precio en las monedas en que se puede pagar ahora (las de los métodos de pago
    // activos): $ si hay algún método en dólares, Bs si hay alguno en bolívares, y los
    // dos —en ese orden— si hay de ambos. Si el paquete no tiene precio en esas
    // monedas se muestra el que tenga, para que nunca quede vacío.
    function priceParts(pkg, cur) {
        var usd = cur.usd && typeof pkg.usd === 'number' ? '$' + pkg.usd.toFixed(2) : '';
        var bs = cur.bs && typeof pkg.bs === 'number' ? 'Bs ' + Math.round(pkg.bs).toLocaleString('es-VE') : '';
        if (!usd && !bs) {
            if (typeof pkg.usd === 'number') usd = '$' + pkg.usd.toFixed(2);
            else if (typeof pkg.bs === 'number') bs = 'Bs ' + Math.round(pkg.bs).toLocaleString('es-VE');
        }
        return [usd, bs].filter(Boolean);
    }

    // ── Burbujas ─────────────────────────────────────────────────────────

    function userSays(text) {
        var bubble = el('div', 'sc-msg client local');
        bubble.appendChild(el('span', 'sc-msg-body', text));
        thread.appendChild(bubble);
        scrollDown();
    }

    function botSays(text) {
        var bubble = el('div', 'sc-msg admin bot');
        bubble.appendChild(el('span', 'sc-bot-label', cfg.name));
        if (text) bubble.appendChild(el('span', 'sc-msg-body', text));
        thread.appendChild(bubble);
        scrollDown();
        return bubble;
    }

    function typing(label) {
        var bubble = el('div', 'sc-msg admin bot sc-typing');
        if (label) bubble.appendChild(el('span', 'sc-bot-label', label));
        var dots = el('span', 'sc-dots');
        dots.appendChild(el('i')); dots.appendChild(el('i')); dots.appendChild(el('i'));
        bubble.appendChild(dots);
        thread.appendChild(bubble);
        scrollDown();
        return function () { if (bubble.parentNode) bubble.parentNode.removeChild(bubble); };
    }

    function note(bubble, text) {
        bubble.appendChild(el('span', 'sc-bot-note', text));
    }

    // ── Pedir datos al servidor ──────────────────────────────────────────

    function api(url, body) {
        var options = { credentials: 'same-origin', headers: {} };
        if (body) {
            options.method = 'POST';
            if (typeof FormData !== 'undefined' && body instanceof FormData) {
                options.body = body;   // el navegador pone solo el tipo y el borde
            } else {
                options.headers['Content-Type'] = 'application/json';
                options.body = JSON.stringify(body);
            }
        }
        return fetch(url, options)
            .then(function (r) { return r.json().catch(function () { return null; }); })
            .then(function (data) {
                if (!data || !data.ok) {
                    throw new Error((data && data.error) || 'No pudimos consultar eso ahora. Intenta de nuevo.');
                }
                return data;
            });
    }

    function failed(token, stopTyping, err) {
        stopTyping();
        if (token !== flow) return;
        var bubble = botSays(err && err.message ? err.message : 'Sin conexión. Revisa tu internet e intenta de nuevo.');
        addActions(bubble, [{ label: 'Hablar con soporte', action: talkToSupport }]);
    }

    // ── Piezas reutilizables ─────────────────────────────────────────────

    function addActions(bubble, actions) {
        var row = el('div', 'sc-actions');
        actions.forEach(function (a) {
            var node;
            if (a.href) {
                node = el('a', 'sc-act' + (a.primary ? ' primary' : ''), a.label);
                node.href = a.href;
                node.target = '_blank';
                node.rel = 'noopener';
            } else {
                node = el('button', 'sc-act' + (a.primary ? ' primary' : ''), a.label);
                node.type = 'button';
                node.addEventListener('click', a.action);
            }
            row.appendChild(node);
        });
        bubble.appendChild(row);
        scrollDown();
    }

    // Una vez elegida una opción, el resto del grupo se apaga: queda a la vista
    // lo que se eligió y no se pueden mandar dos respuestas a la misma pregunta.
    function lock(group, chosen) {
        Array.prototype.forEach.call(group.querySelectorAll('button'), function (b) {
            b.disabled = true;
            if (b === chosen) b.classList.add('chosen');
        });
    }

    function talkToSupport() {
        flow++;
        botSays('Escríbeme tu consulta aquí abajo 👇 y te atiende nuestro equipo.');
        if (input) input.focus();
    }

    // Campo para escribir un dato dentro de la burbuja. `validate` devuelve el
    // mensaje de error o '' si está bien; `onDone` recibe el valor limpio.
    function askValue(bubble, opts) {
        var form = el('form', 'sc-inline');
        var field = el('input', 'sc-inline-input');
        field.type = 'text';
        field.maxLength = opts.maxLength || 40;
        field.placeholder = opts.placeholder || '';
        field.autocomplete = 'off';
        if (opts.numeric) field.setAttribute('inputmode', 'numeric');
        var send = el('button', 'sc-inline-send', opts.button || 'Continuar');
        send.type = 'submit';
        var error = el('span', 'sc-inline-error');
        error.hidden = true;
        form.appendChild(field);
        form.appendChild(send);
        bubble.appendChild(form);
        bubble.appendChild(error);

        form.addEventListener('submit', function (evt) {
            evt.preventDefault();
            var value = field.value.trim();
            var problem = opts.validate(value);
            error.hidden = !problem;
            error.textContent = problem || '';
            if (problem) return;
            field.disabled = true;
            send.disabled = true;
            opts.onDone(value);
        });
        scrollDown();
        // En el teléfono enfocar abre el teclado encima del chat: solo en pantallas grandes.
        if (window.matchMedia && window.matchMedia('(min-width: 641px)').matches) field.focus();
    }

    // ── Comprar recarga: juego -> paquete (con su precio) -> compra ──────

    // `receipt` ({file}) viene cuando el cliente ya pagó y mandó su comprobante por el chat.
    function chooseGame(receipt) {
        var token = ++flow;
        var stop = typing();
        api('/soporte/bot/juegos').then(function (data) {
            stop();
            if (token !== flow) return;
            var games = data.games || [];
            if (!games.length) {
                botSays('Ahora mismo no hay juegos disponibles. Escríbenos y te ayudamos.');
                return;
            }
            var bubble = botSays('¿Qué juego quieres recargar?');
            var group = el('div', 'sc-choices');
            games.forEach(function (game) {
                var btn = el('button', 'sc-choice');
                btn.type = 'button';
                if (game.image) {
                    var img = el('img');
                    img.src = '/static/uploads/' + game.image;
                    img.alt = '';
                    img.loading = 'lazy';
                    img.addEventListener('error', function () { img.remove(); });
                    btn.appendChild(img);
                }
                btn.appendChild(el('span', null, game.name));
                btn.addEventListener('click', function () {
                    lock(group, btn);
                    userSays(game.name);
                    showPackages(game.id, receipt);
                });
                group.appendChild(btn);
            });
            bubble.appendChild(group);
            scrollDown();
        }).catch(function (err) { failed(token, stop, err); });
    }

    function showPackages(gameId, receipt) {
        var token = ++flow;
        var stop = typing();
        api('/soporte/bot/paquetes/' + gameId).then(function (data) {
            stop();
            if (token !== flow) return;
            var game = data.game;
            var packages = data.packages || [];
            var cur = data.currencies || { usd: true, bs: true };
            game.currencies = cur;   // para el resumen de la compra
            if (!packages.length) {
                botSays('Por ahora ' + game.name + ' no tiene paquetes disponibles.');
                return;
            }
            var bubble = botSays(game.name + ': elige tu paquete.');
            bubble.classList.add('narrow');   // burbuja del ancho de la cuadricula
            var list = el('div', 'sc-pkgs');
            packages.forEach(function (pkg) {
                var row = el('button', 'sc-pkg');
                row.type = 'button';
                row.appendChild(el('span', 'sc-pkg-name', pkg.name));
                var price = el('span', 'sc-pkg-price');
                if (!pkg.available) {
                    price.textContent = pkg.note;
                } else {
                    // La primera moneda en grande y, si hay dos, la otra debajo.
                    var parts = priceParts(pkg, cur);
                    if (parts.length) price.appendChild(el('b', null, parts[0]));
                    if (parts.length > 1) price.appendChild(el('small', null, parts[1]));
                }
                row.appendChild(price);
                if (!pkg.available) {
                    row.disabled = true;
                    row.classList.add('off');
                } else {
                    row.addEventListener('click', function () {
                        lock(list, row);
                        userSays(pkg.name);
                        startBuy(game, pkg, cur, receipt);
                    });
                }
                list.appendChild(row);
            });
            bubble.appendChild(list);
            scrollDown();
        }).catch(function (err) { failed(token, stop, err); });
    }

    // Elegido el paquete, la compra completa (ID, metodo de pago, comprobante...)
    // la lleva support-buy.js con las mismas rutas de la tienda.
    function startBuy(game, pkg, cur, receipt) {
        window.SupportBuy.start(game, pkg, cur, receipt);
    }

    // ── Estado de mi recarga ─────────────────────────────────────────────

    var MAX_RECEIPT_BYTES = 8 * 1024 * 1024;

    // Abre el panel de la campanita (el de notificaciones de la tienda). El clic
    // no debe seguir subiendo: el panel se cierra con cualquier clic de afuera.
    function openNotifications(evt) {
        if (evt && evt.stopPropagation) evt.stopPropagation();
        var bell = document.getElementById('notifToggle');
        var panel = document.getElementById('notifPanel');
        if (bell && panel && panel.hidden) bell.click();
    }

    function askOrder() {
        var token = ++flow;
        // Lo primero: en las notificaciones ya sale el estado del ultimo pedido
        // hecho desde este navegador, sin tener que buscar nada.
        var tip = botSays('🔔 Primero revisa tus notificaciones: ahí tienes la actualización de tu último pedido y su estado.');
        addActions(tip, [{ label: 'Ver mis notificaciones', action: openNotifications, primary: true }]);
        var bubble = botSays('Si no lo ves en tus notificaciones, búscalo con una de estas 3 opciones (la que te sea más fácil):\n• Número de orden\n• Referencia del pago\n• Foto del comprobante');
        askValue(bubble, {
            placeholder: 'Número de orden o referencia',
            numeric: true,
            maxLength: 30,
            button: 'Consultar',
            validate: function (value) {
                var digits = value.replace(/\D/g, '');
                return digits.length >= 6 ? '' : 'Escribe el número de orden (12 dígitos) o la referencia de tu pago.';
            },
            onDone: function (value) {
                if (token !== flow) return;
                lockForm(bubble);
                userSays(value);
                lookupOrder(value);
            }
        });

        // Foto del comprobante: la IA de la tienda saca la referencia.
        var picker = el('input');
        picker.type = 'file';
        picker.accept = 'image/*';
        picker.hidden = true;
        var attach = el('button', 'sc-act sc-attach-receipt', 'Adjuntar comprobante');
        attach.type = 'button';
        attach.insertBefore(attachIcon(), attach.firstChild);
        attach.addEventListener('click', function () { picker.click(); });
        picker.addEventListener('change', function () {
            var file = picker.files && picker.files[0];
            if (!file) return;
            var error = bubble.querySelector('.sc-inline-error');
            if (!/^image\//.test(file.type)) { error.hidden = false; error.textContent = 'Adjunta una foto (JPG, PNG o WEBP).'; picker.value = ''; return; }
            if (file.size > MAX_RECEIPT_BYTES) { error.hidden = false; error.textContent = 'La foto pesa demasiado (máximo 8 MB).'; picker.value = ''; return; }
            if (token !== flow) return;
            lockForm(bubble);
            readReceipt(file);
        });
        var row = el('div', 'sc-actions');
        row.appendChild(attach);
        row.appendChild(picker);
        bubble.appendChild(row);
        scrollDown();
    }

    function attachIcon() {
        var ns = 'http://www.w3.org/2000/svg';
        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('fill', 'none');
        svg.setAttribute('stroke', 'currentColor');
        svg.setAttribute('stroke-width', '2');
        svg.setAttribute('stroke-linecap', 'round');
        svg.setAttribute('stroke-linejoin', 'round');
        var path = document.createElementNS(ns, 'path');
        path.setAttribute('d', 'M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48');
        svg.appendChild(path);
        return svg;
    }

    // Tras responder, el campo y el botón de foto de esa pregunta se apagan.
    function lockForm(bubble) {
        Array.prototype.forEach.call(bubble.querySelectorAll('input, button'), function (n) { n.disabled = true; });
    }

    function noOrderFound(text) {
        var bubble = botSays(text);
        addActions(bubble, [
            { label: 'Probar de nuevo', action: askOrder },
            { label: 'Hablar con soporte', action: talkToSupport }
        ]);
    }

    function showOrders(orders) {
        orders.forEach(function (order) {
            var bubble = botSays('');
            var card = el('div', 'sc-card sc-order-card');
            var head = el('div', 'sc-card-head');
            head.appendChild(el('strong', null, [order.game, order.package].filter(Boolean).join(' · ') || 'Pedido'));
            var tone = ['ok', 'info', 'warn', 'bad'].indexOf(order.tone) >= 0 ? order.tone : 'info';
            head.appendChild(el('span', 'sc-status tone-' + tone, order.status));
            card.appendChild(head);
            card.appendChild(el('p', 'sc-card-text', order.text));
            card.appendChild(el('span', 'sc-card-meta', '#' + order.number + (order.date ? ' · ' + order.date : '')));
            bubble.appendChild(card);
            var actions = [{ label: 'Ver mi orden ›', href: order.url, primary: !order.needs_support }];
            if (order.needs_support) actions.push({ label: 'Hablar con soporte', action: talkToSupport, primary: true });
            addActions(bubble, actions);
        });
    }

    function lookupOrder(value) {
        var token = ++flow;
        var stop = typing();
        api('/soporte/bot/estado', { query: value }).then(function (data) {
            stop();
            if (token !== flow) return;
            var orders = data.orders || [];
            if (!orders.length) {
                noOrderFound('No encontré ningún pedido con ese dato. Revisa que sea el número de orden de 12 dígitos o la referencia completa de tu pago.');
                return;
            }
            showOrders(orders);
        }).catch(function (err) { failed(token, stop, err); });
    }

    function readReceipt(file) {
        var token = ++flow;
        // Se muestra la foto como la mandó el cliente, igual que en un chat.
        var mine = el('div', 'sc-msg client local');
        var preview = el('img', 'sc-media');
        var url = URL.createObjectURL(file);
        preview.src = url;
        preview.alt = 'Tu comprobante';
        preview.addEventListener('load', function () { URL.revokeObjectURL(url); });
        mine.appendChild(preview);
        thread.appendChild(mine);
        scrollDown();

        var stop = typing('Leyendo tu comprobante…');
        var form = new FormData();
        form.append('file', file);
        api('/soporte/bot/comprobante', form).then(function (data) {
            stop();
            if (token !== flow) return;
            var orders = data.orders || [];
            if (!data.reference) {
                noOrderFound('No pude leer una referencia en la foto. Prueba con una foto más clara donde se vea completa la referencia, o escríbela.');
            } else if (!orders.length) {
                noOrderFound('Leí la referencia ' + data.reference + ' pero no encuentro un pedido con ella. Revisa que sea la de tu pago.');
            } else {
                botSays('Leí la referencia ' + data.reference + '. Esto encontré:');
                showOrders(orders);
            }
        }).catch(function (err) { failed(token, stop, err); });
    }

    // ── Botones del menú ─────────────────────────────────────────────────

    function start(key) {
        var button = (cfg.buttons || []).filter(function (b) { return b.key === key; })[0];
        if (!button) return;
        userSays(button.label);
        if (key === 'comprar') chooseGame();
        else if (key === 'estado') askOrder();
    }

    quick.addEventListener('click', function (evt) {
        var btn = evt.target.closest ? evt.target.closest('.sc-quick-btn') : null;
        if (btn) start(btn.getAttribute('data-bot'));
    });

    // Lo que usa support-buy.js (se carga despues de este archivo).
    window.SupportBot = {
        thread: thread,
        el: el,
        scrollDown: scrollDown,
        userSays: userSays,
        botSays: botSays,
        typing: typing,
        note: note,
        addActions: addActions,
        lock: lock,
        askValue: askValue,
        attachIcon: attachIcon,
        talkToSupport: talkToSupport,
        chooseGame: chooseGame,
        nextFlow: function () { return ++flow; },
        flowNow: function () { return flow; }
    };
})();
