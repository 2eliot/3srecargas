/* ── 3S Recargas — Tienda de puntos de las cuentas (/tiendapuntos) ──────────
 * Página propia (la portada no carga nada de esto). El servidor deja el catálogo y el saldo en <script id="tpBoot"> para que se pinte sin esperar otra
 * petición; canjear, verificar el ID y el historial van por /api/tiendapuntos/*. Todo se dibuja con textContent (nunca con innerHTML): los nombres y las
 * descripciones los escribe el admin y no deben poder meter HTML.
 */
(function () {
    'use strict';

    var bootEl = document.getElementById('tpBoot');
    if (!bootEl) return;
    var boot = {};
    try { boot = JSON.parse(bootEl.textContent || '{}'); } catch (_) { boot = {}; }

    var state = {
        loggedIn: !!boot.logged_in, balance: parseInt(boot.balance, 10) || 0,
        items: boot.items || [], tabs: boot.tabs || [], roulettes: boot.roulettes || [], activeTab: '', subs: {}, busy: false, current: null
    };

    var $ = function (id) { return document.getElementById(id); };
    var grid = $('tpGrid');
    var tabsEl = $('tpTabs');
    var subsEl = $('tpSubs');
    var hintEl = $('tpHint');
    var emptyEl = $('tpEmpty');
    var modal = $('tpModal');
    var modalBody = $('tpModalBody');

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function fmt(n) { return String(parseInt(n, 10) || 0).replace(/\B(?=(\d{3})+(?!\d))/g, '.'); }

    // Puntos como en los paquetes de la web: dorados y con una estrella (la estrella la pone el CSS). `unit` = el texto chico de al lado ("pts" por defecto).
    function ptsEl(n, unit) {
        var box = el('span', 'tp-pts');
        box.appendChild(document.createTextNode(fmt(n)));
        box.appendChild(el('small', '', unit === undefined ? 'pts' : unit));
        return box;
    }

    function dateLabel(iso) {
        var d = new Date(iso);
        if (isNaN(d)) return '';
        return d.toLocaleString('es-VE', { timeZone: 'America/Caracas', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    }

    function postJson(url, body) {
        return fetch(url, {
            method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        }).then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); });
    }

    // ── Cabecera: saldo (con sesión) o invitación a crear la cuenta (sin sesión) ─────────────────────────────────
    function renderHead() {
        $('tpBalance').hidden = !state.loggedIn;
        $('tpLogin').hidden = state.loggedIn;
        $('tpBalanceValue').textContent = fmt(state.balance);
    }

    // ── Pestañas y tarjetas ──────────────────────────────────────────────────────────────────────────────────────
    // Subpestañas: bajo "Recargas" van los JUEGOS (también los de tarjetas); bajo "Marcos" y "Nivel y reputación", los tipos.
    // Solo se muestran si hay más de un grupo. Se eligen igual que en la portada: tocas el juego y salen sus paquetes.
    var SUB_LABELS = {
        frame_avatar: 'Marco de perfil', frame_banner: 'Marco de banner', avatar_photo: 'Foto de perfil', banner_photo: 'Foto de banner', background: 'Fondos',
        xp: 'Nivel', reputation: 'Reputación'
    };
    var SUB_ICONS = { frame_avatar: '⭕', frame_banner: '🖼️', avatar_photo: '🧑', banner_photo: '🏞️', background: '🌄', xp: '⭐', reputation: '🏆' };
    var KIND_ORDER = ['frame_avatar', 'frame_banner', 'avatar_photo', 'banner_photo', 'background', 'xp', 'reputation'];
    var PREVIEW_KINDS = ['frame_avatar', 'frame_banner', 'avatar_photo', 'banner_photo', 'background'];

    function currentTab() { return state.tabs.filter(function (t) { return t.key === state.activeTab; })[0] || null; }
    function byGame(tab) { return !!tab && tab.key === 'recargas'; }
    function groupKey(tab, item) { return byGame(tab) ? 'g' + (item.game_id || 0) : item.kind; }

    function tabItems(tab) {
        if (!tab) return state.items;
        return state.items.filter(function (i) { return tab.kinds.indexOf(i.kind) >= 0; });
    }

    function subGroups(tab) {
        var groups = [], seen = {};
        tabItems(tab).forEach(function (item) {
            var key = groupKey(tab, item);
            if (!seen[key]) {
                seen[key] = {
                    key: key, count: 0, image: byGame(tab) ? (item.game_image || '') : '', icon: byGame(tab) ? '' : (SUB_ICONS[item.kind] || ''),
                    label: byGame(tab) ? (item.game_name || 'Otros') : (SUB_LABELS[item.kind] || item.kind_label || item.kind),
                    order: byGame(tab) ? (item.game_position || 0) : KIND_ORDER.indexOf(item.kind)
                };
                groups.push(seen[key]);
            }
            seen[key].count += 1;
        });
        groups.sort(function (a, b) { return (a.order - b.order) || a.label.localeCompare(b.label); });   // los juegos en el orden de la tienda; los tipos, siempre igual
        return groups.length > 1 ? groups : [];
    }

    function activeSub(tab) {
        var groups = tab ? subGroups(tab) : [];
        if (!groups.length) return '';
        var wanted = state.subs[tab.key];
        return groups.some(function (g) { return g.key === wanted; }) ? wanted : groups[0].key;
    }

    function visibleItems() {
        var tab = currentTab();
        var items = tabItems(tab);
        var sub = tab ? activeSub(tab) : '';
        if (!sub) return items;
        return items.filter(function (i) { return groupKey(tab, i) === sub; });
    }

    // La dirección refleja la pestaña y la subpestaña (así al recargar la página se queda donde estabas).
    function syncUrl() {
        try {
            var tab = currentTab();
            var params = new URLSearchParams();
            if (tab) params.set('tab', tab.key);
            var sub = tab ? activeSub(tab) : '';
            if (sub) params.set('sub', sub);
            window.history.replaceState(null, '', window.location.pathname + (params.toString() ? '?' + params.toString() : ''));
        } catch (_) { /* nada */ }
    }

    function renderTabs() {
        tabsEl.textContent = '';
        tabsEl.hidden = state.tabs.length < 2;
        state.tabs.forEach(function (tab) {
            var b = el('button', 'tp-tab' + (tab.key === state.activeTab ? ' is-active' : ''), tab.label);
            b.type = 'button';
            b.addEventListener('click', function () { state.activeTab = tab.key; renderTabs(); renderSubs(); renderGrid(); syncUrl(); });
            tabsEl.appendChild(b);
        });
    }

    function renderSubs() {
        subsEl.textContent = '';
        var tab = currentTab();
        var groups = tab ? subGroups(tab) : [];
        subsEl.hidden = !groups.length;
        var active = tab ? activeSub(tab) : '';
        if (hintEl) hintEl.hidden = !(tab && (tab.key === 'marcos' || tab.key === 'fondos' || tab.key === 'fotos'));
        groups.forEach(function (group) {
            var b = el('button', 'tp-subtab' + (group.key === active ? ' is-active' : ''));
            b.type = 'button';
            var logo = el('span', 'tp-subtab-logo');
            if (group.image) {
                var img = document.createElement('img');
                img.src = '/static/uploads/' + group.image;
                img.alt = '';
                img.loading = 'lazy';
                logo.appendChild(img);
            } else {
                logo.textContent = group.icon || (group.label || '?').charAt(0).toUpperCase();
            }
            b.appendChild(logo);
            b.appendChild(el('span', 'tp-subtab-name', group.label));
            b.appendChild(el('span', 'tp-subtab-count', String(group.count)));
            b.addEventListener('click', function () { state.subs[tab.key] = group.key; renderSubs(); renderGrid(); syncUrl(); });
            subsEl.appendChild(b);
        });
    }

    function buttonFor(item) {
        var b = el('button', 'tp-primary-btn tp-card-btn');
        b.type = 'button';
        if (!state.loggedIn) {
            b.textContent = 'Inicia sesión';
            b.addEventListener('click', function () { window.location.href = '/login?tab=cuenta&next=%2Ftiendapuntos'; });
            return b;
        }
        if (item.state !== 'available') {
            b.disabled = true;
            b.textContent = item.state === 'owned' ? 'Ya lo tienes' : item.state === 'sold_out' ? 'Agotado' : item.state === 'limit_reached' ? 'Ya canjeado' : 'No disponible';
            return b;
        }
        if (!item.is_free && item.points_cost > state.balance) {
            b.disabled = true;
            b.textContent = 'Te faltan ' + fmt(item.points_cost - state.balance);
            return b;
        }
        b.textContent = item.is_free ? 'Canjear gratis' : 'Canjear';
        b.addEventListener('click', function () { openRedeem(item); });
        return b;
    }

    function cardImage(item) {
        var box = el('div', 'tp-card-img');
        if (item.asset && item.asset_video) {
            // un fondo en VIDEO se ve moviéndose en la propia tarjeta (en modo liviano se queda quieto, sin reproducir solo)
            var clip = document.createElement('video');
            clip.src = '/static/uploads/' + item.asset;
            clip.muted = true; clip.loop = true; clip.playsInline = true; clip.preload = 'metadata';
            clip.setAttribute('muted', ''); clip.setAttribute('playsinline', ''); clip.setAttribute('aria-hidden', 'true');
            if (!document.documentElement.classList.contains('lite')) clip.autoplay = true;
            box.appendChild(clip);
            return box;
        }
        if (item.image) {
            var img = document.createElement('img');
            img.src = '/static/uploads/' + item.image;
            img.alt = '';
            img.loading = 'lazy';
            img.decoding = 'async';
            box.appendChild(img);
        } else {
            box.textContent = { xp: '⭐', reputation: '🏆', card: '🎴', frame_avatar: '🖼️', frame_banner: '🖼️', background: '🌄', banner_photo: '🏞️', avatar_photo: '🧑' }[item.kind] || '🎁';
        }
        return box;
    }

    function buildCard(item) {
        var card = el('article', 'tp-card' + (item.state === 'available' ? '' : ' is-disabled'));
        var imageBox = cardImage(item);
        card.appendChild(imageBox);
        if (PREVIEW_KINDS.indexOf(item.kind) >= 0 && item.asset) {
            // marcos, fotos y fondos: al tocar la imagen se ve cómo le queda a SU perfil
            card.classList.add('is-previewable');
            imageBox.appendChild(el('span', 'tp-card-eye', '👁'));
            imageBox.tabIndex = 0;
            imageBox.setAttribute('role', 'button');
            imageBox.setAttribute('aria-label', 'Ver cómo te queda: ' + item.name);
            imageBox.addEventListener('click', function () { openPreview(item); });
            imageBox.addEventListener('keydown', function (evt) { if (evt.key === 'Enter' || evt.key === ' ') { evt.preventDefault(); openPreview(item); } });
        }
        card.appendChild(el('div', 'tp-card-name', item.name));
        if (item.game_name) card.appendChild(el('div', 'tp-card-game', item.game_name + (item.kind === 'card' ? ' · llega a tu correo' : '')));

        var meta = el('div', 'tp-card-meta');
        if (item.is_free) meta.appendChild(el('span', 'tp-chip is-free', 'Gratis'));
        if (item.amount && (item.kind === 'xp' || item.kind === 'reputation')) meta.appendChild(el('span', 'tp-chip is-free', '+' + fmt(item.amount) + (item.kind === 'xp' ? ' de nivel' : ' de reputación')));
        if (item.owned) meta.appendChild(el('span', 'tp-chip is-free', 'En tu colección'));
        if (item.stock_left !== null && item.stock_left !== undefined && item.stock_left > 0) meta.appendChild(el('span', 'tp-chip is-warn', 'Quedan ' + item.stock_left));
        if (item.per_user_limit) meta.appendChild(el('span', 'tp-chip', 'Máx. ' + item.per_user_limit + ' ' + item.limit_label));
        if (item.ends_at) meta.appendChild(el('span', 'tp-chip is-warn', 'Hasta ' + dateLabel(item.ends_at)));
        if (meta.childNodes.length) card.appendChild(meta);

        var price = el('div', 'tp-card-price');
        if (item.is_free) {
            price.appendChild(el('span', '', 'Gratis'));
        } else {
            price.appendChild(ptsEl(item.points_cost));
        }
        card.appendChild(price);
        card.appendChild(buttonFor(item));
        return card;
    }

    // Una tarjeta de RULETA: el juego, el premio y lo que cuesta cada giro. Abre la misma ruleta de "Canjear puntos", pero pagando con los puntos de la cuenta.
    function buildRouletteCard(r) {
        var card = el('article', 'tp-card tp-roulette');
        var box = el('div', 'tp-card-img');
        if (r.game_image) {
            var img = document.createElement('img');
            img.src = '/static/uploads/' + r.game_image;
            img.alt = '';
            img.loading = 'lazy';
            box.appendChild(img);
        } else {
            box.textContent = '🎰';
        }
        card.appendChild(box);
        card.appendChild(el('div', 'tp-card-name', 'Ruleta de ' + r.game_name));
        card.appendChild(el('div', 'tp-card-game', 'Premio: ' + r.prize_label));
        var price = el('div', 'tp-card-price');
        price.appendChild(ptsEl(r.spin_cost));
        price.appendChild(el('small', '', 'por giro'));
        card.appendChild(price);
        var b = el('button', 'tp-primary-btn tp-card-btn');
        b.type = 'button';
        if (!state.loggedIn) {
            b.textContent = 'Inicia sesión';
            b.addEventListener('click', function () { window.location.href = '/login?tab=cuenta&next=%2Ftiendapuntos%3Ftab%3Druletas'; });
        } else if (r.spin_cost > state.balance) {
            b.disabled = true;
            b.textContent = 'Te faltan ' + fmt(r.spin_cost - state.balance);
        } else {
            b.textContent = 'Girar';
            b.addEventListener('click', function () {
                if (typeof window.openAccountRoulette === 'function') window.openAccountRoulette(r.game_id);
            });
        }
        card.appendChild(b);
        return card;
    }

    function renderGrid() {
        grid.textContent = '';
        var tab = currentTab();
        emptyEl.hidden = state.items.length > 0 || state.roulettes.length > 0;
        if (tab && tab.key === 'ruletas') {
            state.roulettes.forEach(function (r) { grid.appendChild(buildRouletteCard(r)); });
            return;
        }
        visibleItems().forEach(function (item) { grid.appendChild(buildCard(item)); });
    }

    function renderAll() { renderHead(); renderTabs(); renderSubs(); renderGrid(); }

    function reload() {
        return fetch('/api/tiendapuntos/items', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) return;
                state.loggedIn = !!data.logged_in;
                state.balance = parseInt(data.balance, 10) || 0;
                state.items = data.items || [];
                state.roulettes = data.roulettes || [];
                state.tabs = data.tabs || [];
                if (!state.tabs.some(function (t) { return t.key === state.activeTab; })) state.activeTab = state.tabs.length ? state.tabs[0].key : '';
                renderAll();
            })
            .catch(function () { /* sin conexión: se queda lo que ya se ve */ });
    }

    // ── Ventana ──────────────────────────────────────────────────────────────────────────────────────────────────
    function openModal() {
        modal.hidden = false;
        document.documentElement.style.overflow = 'hidden';
        var t = modalBody.querySelector('h3');
        if (t) t.id = 'tpModalTitle';
    }

    function closeModal() {
        modal.hidden = true;
        modalBody.textContent = '';
        document.documentElement.style.overflow = '';
        state.current = null;
    }

    $('tpModalClose').addEventListener('click', closeModal);
    modal.addEventListener('click', function (evt) { if (evt.target === modal) closeModal(); });
    document.addEventListener('keydown', function (evt) { if (evt.key === 'Escape' && !modal.hidden) closeModal(); });

    function summaryBox(item) {
        var box = el('div', 'tp-summary');
        box.appendChild(cardImage(item));
        var text = el('div', 'tp-summary-text');
        text.appendChild(el('strong', '', item.name));
        var line = el('span', '');
        if (item.is_free) {
            line.textContent = 'Gratis';
        } else {
            line.appendChild(ptsEl(item.points_cost));
            line.appendChild(document.createTextNode(' · te quedan ' + fmt(state.balance - item.points_cost) + ' pts'));
        }
        text.appendChild(line);
        box.appendChild(text);
        return box;
    }

    function field(labelText, id, inputMode) {
        var wrap = el('div', 'tp-field');
        var label = el('label', '', labelText);
        label.setAttribute('for', id);
        var input = document.createElement('input');
        input.type = 'text'; input.id = id; input.autocomplete = 'off'; input.maxLength = 100;
        if (inputMode) input.setAttribute('inputmode', inputMode);
        wrap.appendChild(label);
        wrap.appendChild(input);
        return { wrap: wrap, input: input };
    }

    function openRedeem(item) {
        state.current = item;
        modalBody.textContent = '';
        modalBody.appendChild(el('h3', '', item.is_free ? 'Canjear gratis' : 'Canjear con puntos'));
        modalBody.appendChild(el('p', 'tp-modal-sub', item.needs_id
            ? 'Escribe el ' + (item.id_label || 'ID') + ' de la cuenta del juego donde quieres recibirlo.'
            : item.kind === 'card' ? 'Te enviaremos el código de la tarjeta al correo de tu cuenta.' : 'Confirma para canjearlo.'));
        modalBody.appendChild(summaryBox(item));
        if (item.description) modalBody.appendChild(el('p', 'tp-modal-desc', item.description));

        var idField = null, zoneField = null;
        if (item.needs_id) {
            idField = field(item.id_label || 'ID del jugador', 'tpPlayerId', item.id_kind === 'numeric' ? 'numeric' : '');
            modalBody.appendChild(idField.wrap);
            if (item.requires_zone) {
                zoneField = field(item.zone_label || 'Zone ID', 'tpZoneId', 'numeric');
                modalBody.appendChild(zoneField.wrap);
            }
        }
        var errorEl = el('p', 'tp-modal-error'); errorEl.hidden = true;
        var nickEl = el('div', 'tp-nick'); nickEl.hidden = true;
        modalBody.appendChild(nickEl);
        modalBody.appendChild(errorEl);

        var actions = el('div', 'tp-modal-actions');
        var cancel = el('button', 'tp-ghost-btn', 'Cancelar'); cancel.type = 'button'; cancel.addEventListener('click', closeModal);
        var redeemLabel = item.is_free ? 'Canjear gratis' : 'Canjear ' + fmt(item.points_cost) + ' puntos';
        var confirm = el('button', 'tp-primary-btn', item.verifiable ? 'Verificar mi ID' : redeemLabel); confirm.type = 'button';
        actions.appendChild(cancel); actions.appendChild(confirm);
        modalBody.appendChild(actions);

        var verified = !item.verifiable;       // los juegos con verificador piden confirmar el nombre del jugador antes de gastar
        function fail(text) { errorEl.textContent = text; errorEl.hidden = false; }
        if (idField) {
            idField.input.addEventListener('input', function () {
                verified = !item.verifiable;
                nickEl.hidden = true;
                confirm.textContent = item.verifiable ? 'Verificar mi ID' : redeemLabel;
            });
        }

        confirm.addEventListener('click', function () {
            if (state.busy) return;
            errorEl.hidden = true;
            var playerId = idField ? idField.input.value.trim() : '';
            var zoneId = zoneField ? zoneField.input.value.trim() : '';
            if (item.needs_id && !playerId) return fail('Escribe tu ' + (item.id_label || 'ID') + '.');
            if (zoneField && !zoneId) return fail('Escribe tu ' + (item.zone_label || 'Zone ID') + '.');

            state.busy = true;
            confirm.disabled = true;
            if (!verified) {
                confirm.textContent = 'Verificando...';
                postJson('/api/tiendapuntos/verificar', { item_id: item.id, player_id: playerId }).then(function (res) {
                    state.busy = false; confirm.disabled = false;
                    if (!res.data.ok) { confirm.textContent = 'Verificar mi ID'; return fail(res.data.message || 'No se pudo verificar tu ID.'); }
                    verified = true;
                    if (res.data.nick) { nickEl.textContent = 'Jugador: ' + res.data.nick + '. Si es tu cuenta, confirma el canje.'; nickEl.hidden = false; }
                    confirm.textContent = item.is_free ? 'Confirmar canje gratis' : 'Confirmar ' + fmt(item.points_cost) + ' puntos';
                }).catch(function () { state.busy = false; confirm.disabled = false; confirm.textContent = 'Verificar mi ID'; fail('Error de conexión. Intenta de nuevo.'); });
                return;
            }
            confirm.textContent = 'Canjeando...';
            postJson('/api/tiendapuntos/canjear', { item_id: item.id, player_id: playerId, zone_id: zoneId }).then(function (res) {
                state.busy = false;
                if (!res.data.ok) {
                    confirm.disabled = false; confirm.textContent = redeemLabel;
                    if (res.status === 401) return fail('Tu sesión se cerró. Inicia sesión otra vez.');
                    return fail(res.data.message || 'No se pudo canjear.');
                }
                state.balance = parseInt(res.data.result.balance, 10) || 0;
                renderHead();
                showResult(item, res.data.result);
                reload();
            }).catch(function () { state.busy = false; confirm.disabled = false; confirm.textContent = 'Reintentar'; fail('Error de conexión. Revisa tu internet y toca de nuevo: si ya se canjeó, lo verás en "Mis canjes".'); });
        });
        openModal();
        if (idField) idField.input.focus();
    }

    function showResult(item, result) {
        modalBody.textContent = '';
        var box = el('div', 'tp-result');
        box.appendChild(el('div', 'tp-result-icon', '🎉'));
        box.appendChild(el('h3', '', '¡Listo!'));
        var text;
        if (item.kind === 'package') {
            text = result.delivered
                ? 'Tu premio fue entregado. Revisa tu cuenta del juego.'
                : 'Recibimos tu canje. Si el paquete es manual, lo entrega nuestro equipo en su horario de atención.';
        } else if (item.kind === 'card') {
            text = 'Te enviamos el código de la tarjeta al correo de tu cuenta. También lo ves en tu orden.';
        } else if (result.detail) {
            text = result.detail;
        } else {
            text = 'Canjeado. Ya está en tu cuenta.';
        }
        box.appendChild(el('p', '', text + ' Te quedan ' + fmt(result.balance) + ' puntos.'));
        if (result.order_number) {
            var p = el('p', '');
            var a = el('a', '', 'Ver mi orden #' + result.order_number);
            a.href = '/order/' + encodeURIComponent(result.order_number);
            p.appendChild(a);
            box.appendChild(p);
        }
        if (item.kind === 'frame_avatar' || item.kind === 'frame_banner' || item.kind === 'background' || item.kind === 'banner_photo' || item.kind === 'avatar_photo') {
            var goEdit = el('p', '');
            var editLink = el('a', '', 'Ir a ponerlo en mi perfil');
            editLink.href = '/profile/edit';
            goEdit.appendChild(editLink);
            box.appendChild(goEdit);
        }
        var done = el('button', 'tp-primary-btn', 'Seguir viendo la tienda'); done.type = 'button'; done.style.width = '100%';
        done.addEventListener('click', closeModal);
        box.appendChild(done);
        modalBody.appendChild(box);
        var title = box.querySelector('h3');
        if (title) title.id = 'tpModalTitle';
    }

    // ── Vista previa del perfil (marcos, fotos y fondos) ─────────────────────────────────────────────────────────
    // Se muestra la MISMA tarjeta del editor con el perfil de la cuenta, y encima el artículo que se está mirando (igual que hace el editor al elegirlo).
    // Los estilos del perfil (profile.css) se piden recién al abrir la primera vista previa, así la tienda no los carga de balde.
    var previewHtml = null;

    function ensureProfileCss() {
        return new Promise(function (resolve) {
            if (document.getElementById('tpProfileCss')) return resolve();
            var page = $('tpPage');
            var href = page ? page.getAttribute('data-profile-css') : '';
            if (!href) return resolve();
            var link = document.createElement('link');
            link.id = 'tpProfileCss';
            link.rel = 'stylesheet';
            link.href = href;
            link.onload = resolve;
            link.onerror = resolve;
            document.head.appendChild(link);
        });
    }

    function loadPreviewHtml() {
        if (previewHtml !== null) return Promise.resolve(previewHtml);
        return fetch('/api/tiendapuntos/vista-previa', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok) throw new Error((data && data.message) || 'No se pudo cargar tu perfil.');
                previewHtml = data.html;
                return previewHtml;
            });
    }

    function applyPreviewItem(scope, item) {
        var url = '/static/uploads/' + item.asset;
        if (item.kind === 'avatar_photo') {
            var av = scope.querySelector('#pfAvatar');
            if (!av) return;
            var img = av.querySelector('img');
            if (!img) { av.textContent = ''; img = document.createElement('img'); img.alt = ''; av.appendChild(img); }
            img.src = url;
        } else if (item.kind === 'banner_photo') {
            scope.style.setProperty('--bn-image', "url('" + url + "')");
        } else if (item.kind === 'background') {
            scope.setAttribute('data-bg', 'image');
            var video = scope.querySelector('.pf-bg-video');
            if (item.asset_video && video) {
                scope.style.removeProperty('--bg-image');
                video.src = url;
                video.hidden = false;
                try { var played = video.play(); if (played && played.catch) played.catch(function () { /* sin autoplay */ }); } catch (_) { /* nada */ }
            } else {
                if (video) { video.hidden = true; video.removeAttribute('src'); }
                scope.style.setProperty('--bg-image', "url('" + url + "')");
            }
        } else if (item.kind === 'frame_avatar') {
            var avOverlay = scope.querySelector('#pfAvOverlay');
            if (avOverlay) { avOverlay.src = url; avOverlay.hidden = false; }
        } else if (item.kind === 'frame_banner') {
            var bnOverlay = scope.querySelector('#pfBnOverlay');
            if (!bnOverlay) return;
            bnOverlay.src = url;
            bnOverlay.hidden = false;
            // un marco más alto que la portada "sobresale": se calcula igual que en el editor (0 a 50 %)
            var probe = new Image();
            probe.onload = function () {
                if (!probe.naturalWidth) return;
                var extra = Math.max(0, Math.min(50, Math.round(((probe.naturalHeight / probe.naturalWidth) / 0.4 - 1) / 2 * 100)));
                scope.style.setProperty('--bn-ov-extra', String(extra / 100));
                scope.setAttribute('data-bn-pop', extra > 0 ? 'on' : 'off');
            };
            probe.src = url;
        }
    }

    function openPreview(item) {
        modalBody.textContent = '';
        modalBody.appendChild(el('h3', '', 'Vista previa'));
        modalBody.appendChild(el('p', 'tp-modal-sub', 'Así te queda «' + item.name + '» en tu perfil.'));
        var stage = el('div', 'tp-preview pf-pop-preview');
        modalBody.appendChild(stage);
        if (item.description) modalBody.appendChild(el('p', 'tp-modal-desc', item.description));

        var actions = el('div', 'tp-modal-actions');
        var close = el('button', 'tp-ghost-btn', 'Cerrar'); close.type = 'button'; close.addEventListener('click', closeModal);
        actions.appendChild(close);
        var go = buttonFor(item);
        go.classList.remove('tp-card-btn');
        if (item.owned && state.loggedIn) {
            go = el('a', 'tp-primary-btn', 'Ponerlo en mi perfil');
            go.href = '/profile/edit';
        }
        actions.appendChild(go);
        modalBody.appendChild(actions);
        openModal();

        if (!state.loggedIn) {
            // sin cuenta no hay perfil que mostrar: se ve el artículo solo y se invita a entrar
            var note = el('div', 'tp-preview-note');
            stage.classList.remove('pf-pop-preview');
            if (!item.asset_video) {
                var picture = document.createElement('img');
                picture.src = '/static/uploads/' + item.asset;
                picture.alt = '';
                note.appendChild(picture);
            }
            note.appendChild(document.createTextNode('Inicia sesión con tu cuenta para ver cómo te queda en tu perfil.'));
            stage.appendChild(note);
            return;
        }
        stage.appendChild(el('div', 'tp-preview-note', 'Cargando tu perfil...'));
        Promise.all([ensureProfileCss(), loadPreviewHtml()]).then(function (results) {
            if (modal.hidden) return;                      // la ventana ya se cerró mientras cargaba
            stage.innerHTML = results[1];                  // HTML que armó el servidor (con escape): es la tarjeta del perfil de la cuenta
            var scope = stage.querySelector('#pfScope');
            if (scope) applyPreviewItem(scope, item);
        }).catch(function (err) {
            stage.textContent = '';
            stage.appendChild(el('div', 'tp-preview-note', (err && err.message) || 'No se pudo cargar tu perfil.'));
        });
    }

    // ── Historial ────────────────────────────────────────────────────────────────────────────────────────────────
    function openHistory() {
        modalBody.textContent = '';
        modalBody.appendChild(el('h3', '', 'Mis canjes'));
        var list = el('ul', 'tp-history');
        modalBody.appendChild(list);
        list.appendChild(el('li', '', 'Cargando...'));
        openModal();
        fetch('/api/tiendapuntos/historial', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                list.textContent = '';
                if (!data || !data.ok || !data.orders.length) { list.appendChild(el('li', '', 'Todavía no has canjeado nada.')); return; }
                data.orders.forEach(function (o) {
                    var li = el('li');
                    var left = el('div');
                    left.appendChild(el('span', '', o.item_name));
                    var info = (o.created_at ? dateLabel(o.created_at) : '') + (o.status === 'refunded' ? ' · devuelto' : '') + (o.order_number ? ' · orden #' + o.order_number : '');
                    left.appendChild(el('small', '', info));
                    li.appendChild(left);
                    li.appendChild(el('b', '', o.points_spent ? '-' + fmt(o.points_spent) : 'Gratis'));
                    list.appendChild(li);
                });
            })
            .catch(function () { list.textContent = ''; list.appendChild(el('li', '', 'No se pudo cargar. Intenta de nuevo.')); });
    }

    var historyBtn = $('tpHistoryBtn');
    if (historyBtn) historyBtn.addEventListener('click', openHistory);

    // La ruleta de "Canjear puntos" (main.js) avisa cuando se gasta un giro con los puntos de la cuenta: se actualiza el saldo y los botones.
    document.addEventListener('points-account-balance', function (evt) {
        state.balance = parseInt(evt.detail && evt.detail.balance, 10) || 0;
        renderHead();
        renderGrid();
    });

    // ── Arranque ─────────────────────────────────────────────────────────────────────────────────────────────────
    // /tiendapuntos?tab=marcos (el editor del perfil manda aquí con "Conseguir más"): abre esa pestaña si existe.
    // y &sub=frame_avatar (o g12 = el juego 12) abre la subpestaña.
    var wantedTab = '', wantedSub = '';
    try {
        var query = new URLSearchParams(window.location.search);
        wantedTab = query.get('tab') || '';
        wantedSub = query.get('sub') || '';
    } catch (_) { wantedTab = ''; wantedSub = ''; }
    if (wantedTab === 'tarjetas') wantedTab = 'recargas';   // las tarjetas ahora van dentro de Recargas
    // una subpestaña que es un TIPO (frame_banner, avatar_photo, background...) abre la pestaña a la que pertenece, aunque el enlace diga otra
    var ownerTab = wantedSub ? state.tabs.filter(function (t) { return t.kinds.indexOf(wantedSub) >= 0; })[0] : null;
    if (ownerTab) wantedTab = ownerTab.key;
    state.activeTab = state.tabs.some(function (t) { return t.key === wantedTab; }) ? wantedTab : (state.tabs.length ? state.tabs[0].key : '');
    if (state.activeTab && wantedSub) state.subs[state.activeTab] = wantedSub;
    renderAll();
})();
