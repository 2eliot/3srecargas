/* ── 3S Recargas — Store JavaScript ─────────────────────── */

/* Ayudas de red compartidas (cualquier página las puede usar): mensajes entendibles cuando se corta el internet y un tiempo máximo para
   que un botón no se quede "Procesando..." para siempre con una conexión colgada (3G inestable). */
window.nxFriendlyError = function (err, fallback) {
    var msg = err && err.message ? String(err.message) : '';
    var name = err && err.name ? String(err.name) : '';
    if (name === 'AbortError') return 'La conexión está muy lenta. Intenta de nuevo en un momento.';
    var offline = (typeof navigator !== 'undefined' && navigator.onLine === false)
        || (name === 'TypeError' && /failed to fetch|load failed|networkerror|network request failed|fetch/i.test(msg));
    if (offline) return 'Sin conexión. Revisa tu internet e intenta de nuevo.';
    return msg || fallback || 'Ocurrió un error. Intenta de nuevo.';
};
window.nxFetch = function (url, opts, ms) {
    if (typeof AbortController === 'undefined' || !window.fetch) return fetch(url, opts);
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, ms || 20000);
    var o = {};
    for (var k in (opts || {})) { if (Object.prototype.hasOwnProperty.call(opts, k)) o[k] = opts[k]; }
    if (!o.signal) o.signal = ctrl.signal;
    return fetch(url, o).then(function (r) { clearTimeout(timer); return r; }, function (e) { clearTimeout(timer); throw e; });
};
window.nxDebug = function () {};

(function () {
    'use strict';

    var activeGameId   = null;
    var activeGameSlug = '';
    var activeCategory = window.ACTIVE_CATEGORY || 'juegos';
    var currentGame    = null;
    var selectedPackage = null;
    var appliedDiscountCode = '';
    // Horario de atención de las recargas manuales, tal como lo devuelve el
    // servidor con los paquetes. Solo se usa para los textos: quien decide si
    // está abierto es el backend.
    var manualSchedule = null;
    var usdRate = typeof window.USD_RATE_BS === 'number' ? window.USD_RATE_BS : 0;
    // Frescura del catálogo. La gente deja la tienda abierta en el teléfono
    // durante días y volvía a comprar con los precios y la tasa de cuando
    // la abrió (el total en Bs se calcula aquí, en el navegador). Los
    // paquetes y la tasa se vuelven a pedir al volver a la pestaña, al
    // tocar un paquete o cada rato si llevan más de unos minutos cargados.
    var CATALOG_STALE_MS = 5 * 60 * 1000;
    // Al elegir un paquete se pide el catálogo de nuevo si tiene más de un
    // minuto: es el equivalente a la página de checkout de Inefablestore,
    // que vuelve a pedir tasa y precios cada vez que se abre.
    var CATALOG_STALE_ON_SELECT_MS = 60 * 1000;
    var CATALOG_POLL_MS = 60 * 1000;
    var catalogLoadedAt = 0;
    var catalogRefreshing = false;
    var packagesRequestSeq = 0;
    var lastShownTotal = null;   // { currency, amount, usd } — lo que ve el cliente
    // Día (Venezuela) en que se abrió la página. Viaja en cada consulta:
    // el servidor no atiende páginas de otro día (ver page_day_expired).
    // Se lee en cada consulta (no al cargar): antes se capturaba vacío
    // porque el HTML lo definía después de este archivo, y el servidor
    // tomaba "vacío" como otro día -> recarga en bucle.
    function pageDay() { return String(window.PAGE_DAY_VE || ''); }
    function storeHeaders() { return { 'X-Page-Day': pageDay() }; }

    /* Un código de Binance a la vista bloquea cualquier recarga.

       Ese código de 6 dígitos es el memo que el cliente escribe en la app
       de Binance para que su pago se reconozca. Si la página se recarga
       mientras él está pagando en la app, vuelve a una pantalla que puede
       enseñarle otro código, y el pago que ya envió se queda sin orden a
       la que atarse.

       Se comprueba aquí, en el único punto por el que pasan todas las
       recargas automáticas, y no en cada sitio que las dispara: eran tres
       —cambio de día, versión nueva y `page_expired` al elegir paquete— y
       la última no miraba absolutamente nada. */
    function hayPagoBinanceEnCurso() {
        var ids = ['nxBinanceCode', 'binanceCodeDisplay'];
        for (var i = 0; i < ids.length; i++) {
            var el = document.getElementById(ids[i]);
            if (!el || el.hidden) continue;
            if (!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) continue;
            if (/\d{6}/.test(el.textContent || '')) return true;
        }
        return false;
    }

    /* Seguro anti-bucle: ninguna recarga automática se repite en menos de
       un minuto. Si algo falla, la página se queda quieta en vez de
       parpadear sin fin delante del cliente. */
    function safeAutoReload(reason) {
        if (hayPagoBinanceEnCurso()) {
            console.warn('Recarga automática omitida (pago Binance en curso):', reason);
            return false;
        }
        var key = 'nx:auto-reload';
        var last = 0;
        try { last = parseInt(sessionStorage.getItem(key) || '0', 10) || 0; } catch (_) {}
        if (Date.now() - last < 60 * 1000) {
            console.warn('Recarga automática omitida (hubo una hace menos de 1 min):', reason);
            return false;
        }
        try { sessionStorage.setItem(key, String(Date.now())); } catch (_) {}
        window.location.reload();
        return true;
    }
    window.nxSafeAutoReload = safeAutoReload;
    var defaultPackageId = (typeof window.DEFAULT_PACKAGE_ID === 'number' ? window.DEFAULT_PACKAGE_ID : null);
    var gamesViewportEl = document.getElementById('gamesViewport');
    var gamesGridEl = document.getElementById('gamesGrid');
    var gamesPrevBtn = document.getElementById('gamesPrev');
    var gamesNextBtn = document.getElementById('gamesNext');
    var applyDiscountBtn = document.getElementById('applyDiscountBtn');
    var discountApplyFeedback = document.getElementById('discountApplyFeedback');
    var manualInfoPopup = document.getElementById('manualInfoPopup');
    var manualInfoCloseBtn = document.getElementById('manualInfoCloseBtn');
    var discountInfoPopup = document.getElementById('discountInfoPopup');
    var discountInfoCloseBtn = document.getElementById('discountInfoCloseBtn');
    var gameSelectionPopup = document.getElementById('gameSelectionPopup');
    var gameSelectionPopupCloseBtn = document.getElementById('gameSelectionPopupCloseBtn');
    var pkgOneTimePopup = document.getElementById('pkgOneTimePopup');
    var pkgOneTimeCloseBtn = document.getElementById('pkgOneTimeCloseBtn');
    var pkgAnnouncementShown = {};
    var communityPopup = document.getElementById('communityPopup');
    var communityPopupCloseX = document.getElementById('communityPopupCloseX');
    var communityPopupCloseCount = document.getElementById('communityPopupCloseCount');
    var communityPopupJoinBtn = document.getElementById('communityPopupJoinBtn');
    var communityPopupContinueBtn = document.getElementById('communityPopupContinueBtn');
    var communityPopupMuteBtn = document.getElementById('communityPopupMuteBtn');
    var contactEmailInput = document.getElementById('email');
    var phoneFieldStack = document.getElementById('phoneFieldStack');
    var phoneCountryCodeInput = document.getElementById('phoneCountryCode');
    var phoneCountryTrigger = document.getElementById('phoneCountryTrigger');
    var phoneCountryMenu = document.getElementById('phoneCountryMenu');
    var phoneCountryDisplay = document.getElementById('phoneCountryDisplay');
    var phoneCountryOptions = Array.prototype.slice.call(document.querySelectorAll('.phone-country-option'));
    var phoneLocalInput = document.getElementById('phoneLocal');
    var phoneHiddenInput = document.getElementById('phone');
    var rememberDataInput = document.getElementById('rememberData');
    var rememberedContactKey = 'store:remembered-contact';
    var rankingModal = document.getElementById('rankingModal');
    var rankingModalOpenBtn = document.getElementById('openRankingModalBtn');
    var rankingModalCloseBtn = document.getElementById('rankingModalCloseBtn');
    var rankingTabsEl = document.getElementById('rankingTabs');
    var rankingStatusEl = document.getElementById('rankingStatus');
    var rankingBoardEl = document.getElementById('rankingBoard');
    var pointsModal = document.getElementById('pointsModal');
    var pointsModalOpenBtn = document.getElementById('openPointsModalBtn');
    var pointsModalCloseBtn = document.getElementById('pointsModalCloseBtn');
    var gamesCarouselState = {
        pointerId: null,
        startX: 0,
        startScrollLeft: 0,
        dragged: false,
        suppressClickUntil: 0,
        pressedCard: null
    };
    var gamesGridResizeObserver = null;
    var rankingState = {
        loading: false,
        loaded: false,
        activeKey: null,
        items: [],
        showAllPositions: false,
        lookupInputValue: ''
    };
    var RANKING_COLLAPSED_ROWS = 6;

    function gamesGridHasOverflow() {
        if (!gamesViewportEl) return false;
        return (gamesViewportEl.scrollWidth - gamesViewportEl.clientWidth) > 4;
    }

    function updateGamesCarouselNav() {
        if (!gamesViewportEl) return;

        var maxScrollLeft = Math.max(0, gamesViewportEl.scrollWidth - gamesViewportEl.clientWidth);
        var currentScrollLeft = Math.max(0, gamesViewportEl.scrollLeft);
        var canScroll = maxScrollLeft > 4;

        if (gamesPrevBtn) {
            gamesPrevBtn.disabled = !canScroll || currentScrollLeft <= 4;
            gamesPrevBtn.classList.toggle('is-hidden', !canScroll);
        }

        if (gamesNextBtn) {
            gamesNextBtn.disabled = !canScroll || currentScrollLeft >= (maxScrollLeft - 4);
            gamesNextBtn.classList.toggle('is-hidden', !canScroll);
        }
    }

    function scrollGames(direction) {
        if (!gamesViewportEl) return;
        var firstCard = gamesGridEl.querySelector('.game-card');
        var computedStyle = firstCard ? window.getComputedStyle(firstCard) : null;
        var gap = computedStyle ? parseFloat(computedStyle.marginRight || '0') : 0;
        var cardWidth = firstCard ? firstCard.offsetWidth + gap + 12 : 180;
        gamesViewportEl.scrollBy({ left: direction * cardWidth * 3, behavior: 'smooth' });
        window.setTimeout(updateGamesCarouselNav, 220);
    }

    function endGamesCarouselDrag() {
        if (!gamesViewportEl) return;

        if (gamesCarouselState.dragged) {
            gamesCarouselState.suppressClickUntil = Date.now() + 250;
        }

        gamesCarouselState.pointerId = null;
        gamesCarouselState.dragged = false;
        gamesCarouselState.pressedCard = null;
        gamesViewportEl.classList.remove('is-dragging');
        updateGamesCarouselNav();
    }

    function initGamesCarouselInteractions() {
        if (!gamesViewportEl || !gamesGridEl || gamesViewportEl.dataset.carouselReady === '1') return;

        gamesViewportEl.addEventListener('scroll', updateGamesCarouselNav, { passive: true });
        gamesViewportEl.addEventListener('dragstart', function (event) {
            event.preventDefault();
        });

        gamesViewportEl.addEventListener('pointerdown', function (event) {
            if (!gamesGridHasOverflow()) return;
            if (event.pointerType === 'mouse' && event.button !== 0) return;

            gamesCarouselState.pointerId = event.pointerId;
            gamesCarouselState.startX = event.clientX;
            gamesCarouselState.startScrollLeft = gamesViewportEl.scrollLeft;
            gamesCarouselState.dragged = false;
            gamesCarouselState.pressedCard = event.target && event.target.closest ? event.target.closest('.game-card') : null;
            gamesViewportEl.classList.add('is-dragging');
        });

        gamesViewportEl.addEventListener('pointermove', function (event) {
            if (gamesCarouselState.pointerId !== event.pointerId) return;

            var deltaX = event.clientX - gamesCarouselState.startX;
            if (Math.abs(deltaX) > 6) {
                gamesCarouselState.dragged = true;
            }

            if (!gamesCarouselState.dragged) return;

            gamesViewportEl.scrollLeft = gamesCarouselState.startScrollLeft - deltaX;
            event.preventDefault();
        });

        ['pointerup', 'pointercancel', 'lostpointercapture', 'pointerleave'].forEach(function (eventName) {
            gamesViewportEl.addEventListener(eventName, function (event) {
                if (gamesCarouselState.pointerId !== null && event.pointerId !== gamesCarouselState.pointerId) return;

                if (eventName === 'pointerup' && !gamesCarouselState.dragged && gamesCarouselState.pressedCard) {
                    gamesCarouselState.suppressClickUntil = Date.now() + 350;
                    handleGameClick(gamesCarouselState.pressedCard);
                    event.preventDefault();
                }

                endGamesCarouselDrag();
            });
        });

        gamesViewportEl.addEventListener('click', function (event) {
            if (Date.now() >= gamesCarouselState.suppressClickUntil) return;
            event.preventDefault();
            event.stopPropagation();
        }, true);

        gamesViewportEl.dataset.carouselReady = '1';
        updateGamesCarouselNav();

        if ('ResizeObserver' in window) {
            gamesGridResizeObserver = new ResizeObserver(function () {
                updateGamesCarouselNav();
            });
            gamesGridResizeObserver.observe(gamesViewportEl);
            gamesGridResizeObserver.observe(gamesGridEl);
        }

        window.requestAnimationFrame(updateGamesCarouselNav);
        window.setTimeout(updateGamesCarouselNav, 250);
    }

    if (gamesPrevBtn) {
        gamesPrevBtn.addEventListener('click', function () { scrollGames(-1); });
    }
    if (gamesNextBtn) {
        gamesNextBtn.addEventListener('click', function () { scrollGames(1); });
    }

    initGamesCarouselInteractions();
    window.addEventListener('resize', updateGamesCarouselNav);

    initRememberedContact();

    /* ── Category Buttons ─────────────────────────────────── */
    document.querySelectorAll('.cat-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var cat = this.dataset.category;
            if (cat === activeCategory) return;

            document.querySelectorAll('.cat-btn').forEach(function (b) {
                b.classList.remove('active');
            });
            this.classList.add('active');
            activeCategory = cat;

            closePackages();
            updateCategoryUrl(cat);
            loadGames(cat);
        });
    });

    /* ── Load Games via AJAX ──────────────────────────────── */
    // Cada pedido lleva un número: si el cliente cambia de categoría rápido, la respuesta de la categoría anterior llega tarde y NO debe pintarse (antes
    // aparecían juegos de otra categoría).
    var gamesRequestSeq = 0;
    function loadGames(category) {
        var grid = document.getElementById('gamesGrid');
        grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">Cargando...</div>';
        updateGamesCarouselNav();
        var seq = ++gamesRequestSeq;

        fetch('/api/games?category=' + encodeURIComponent(category), { headers: storeHeaders() })
            .then(function (r) { return r.json(); })
            .then(function (data) { if (seq === gamesRequestSeq) renderGames(data.games); })
            .catch(function () {
                if (seq !== gamesRequestSeq) return;
                grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">Error al cargar juegos.</div>';
                updateGamesCarouselNav();
            });
    }

    function updateStepsTheme() {
        var form = document.getElementById('quickCheckoutForm');
        if (!form) return;

        var checked = document.querySelector('input[name="payment_method"]:checked');
        if (!checked) {
            form.classList.remove('steps-red');
            return;
        }

        var code = String(checked.value || '').toLowerCase();
        var usesRate = checked.dataset.usesRate === '1';

        if (code === 'binance' && !usesRate) {
            form.classList.add('steps-red');
        } else {
            form.classList.remove('steps-red');
        }
    }

    function getSelectedPaymentCurrency() {
        var checked = document.querySelector('input[name="payment_method"]:checked');
        if (!checked) return 'bs';
        var c = String(checked.dataset.accountCurrency || 'bs').toLowerCase();
        return (c === 'usd') ? 'usd' : 'bs';
    }

    function normalizePhoneValue(value) {
        return String(value || '').replace(/[^\d+]/g, '').trim();
    }

    function splitPhoneValue(rawPhone) {
        var normalized = normalizePhoneValue(rawPhone);
        var result = { countryCode: '+58', localNumber: '' };

        if (!normalized) return result;

        var options = phoneCountryCodeInput ? Array.prototype.slice.call(phoneCountryCodeInput.options) : [];
        options.sort(function (a, b) { return b.value.length - a.value.length; });

        for (var i = 0; i < options.length; i += 1) {
            var code = String(options[i].value || '');
            if (normalized.indexOf(code) === 0) {
                result.countryCode = code;
                result.localNumber = normalized.slice(code.length);
                return result;
            }
        }

        result.localNumber = normalized.replace(/^\+/, '');
        return result;
    }

    function syncPhoneHiddenValue() {
        if (!phoneHiddenInput) return;
        var code = phoneCountryCodeInput ? String(phoneCountryCodeInput.value || '+58').trim() : '+58';
        var localNumber = phoneLocalInput ? String(phoneLocalInput.value || '').replace(/[^\d]/g, '') : '';
        phoneHiddenInput.value = localNumber ? (code + ' ' + localNumber) : '';
    }

    function updatePhoneCountryDisplay() {
        if (!phoneCountryCodeInput || !phoneCountryDisplay) return;
        var selectedOption = phoneCountryCodeInput.options[phoneCountryCodeInput.selectedIndex];
        if (!selectedOption) return;
        var flagSrc = String(selectedOption.getAttribute('data-flag-src') || '').trim();
        var countryName = String(selectedOption.getAttribute('data-country-name') || '').trim();
        var code = String(selectedOption.value || '').trim();
        phoneCountryDisplay.innerHTML = '';

        if (flagSrc) {
            var flagImg = document.createElement('img');
            flagImg.src = flagSrc;
            flagImg.alt = countryName || code;
            flagImg.width = 20;
            flagImg.height = 15;
            flagImg.className = 'phone-country-flag';
            phoneCountryDisplay.appendChild(flagImg);
        }

        var codeSpan = document.createElement('span');
        codeSpan.className = 'phone-country-code';
        codeSpan.textContent = code;
        phoneCountryDisplay.appendChild(codeSpan);

        phoneCountryOptions.forEach(function (optionBtn) {
            var isSelected = optionBtn.dataset.code === code;
            optionBtn.classList.toggle('is-selected', isSelected);
            optionBtn.setAttribute('aria-selected', isSelected ? 'true' : 'false');
        });
    }

    function openPhoneCountryMenu() {
        if (!phoneCountryTrigger || !phoneFieldStack) return;
        phoneFieldStack.classList.add('is-open');
        phoneCountryTrigger.setAttribute('aria-expanded', 'true');
    }

    function closePhoneCountryMenu() {
        if (!phoneCountryTrigger || !phoneFieldStack) return;
        phoneFieldStack.classList.remove('is-open');
        phoneCountryTrigger.setAttribute('aria-expanded', 'false');
    }

    function openModal(modalEl) {
        if (!modalEl) return;
        modalEl.style.display = 'block';
        modalEl.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');
    }

    function closeModal(modalEl) {
        if (!modalEl) return;
        modalEl.style.display = 'none';
        modalEl.setAttribute('aria-hidden', 'true');
        if (!document.querySelector('.overlay-modal[aria-hidden="false"]')) {
            document.body.classList.remove('modal-open');
        }
    }

    function setRankingStatus(text) {
        if (rankingStatusEl) {
            rankingStatusEl.textContent = text;
            rankingStatusEl.style.display = 'block';
        }
        if (rankingBoardEl) {
            rankingBoardEl.style.display = 'none';
        }
    }

    function isPrizeLabel(prizeLabel, isPrizeEligible) {
        return !!isPrizeEligible || /^premio/i.test(String(prizeLabel || ''));
    }

    function getRankingLookupParams() {
        var playerIdInput = document.getElementById('playerId');
        var lookupIdentifier = playerIdInput ? String(playerIdInput.value || '').trim() : '';
        var lookupGameId = currentGame && currentGame.id ? currentGame.id : null;

        if (!lookupIdentifier || !lookupGameId) {
            return '';
        }

        return '?lookup_game_id=' + encodeURIComponent(String(lookupGameId)) + '&lookup_identifier=' + encodeURIComponent(lookupIdentifier);
    }

    function formatRewardValue(value, rankingKey) {
        if (value === null || typeof value === 'undefined' || value === '') {
            return 'Sin premio';
        }

        var text = String(value).trim();
        if (!text) {
            return 'Sin premio';
        }

        if (rankingKey === 'free_fire' && /^\d+(?:[.,]\d+)?$/.test(text)) {
            return '💎 ' + text;
        }

        return text;
    }

    // Valor del premio de un puesto: si es una cantidad de diamantes ("572", "572 💎", "572 diamantes") sale el número grande con una gema dibujada;
    // si es otra cosa (el nombre de un paquete) sale como texto.
    var RANKING_GEM_SVG =
        '<svg class="rk-prize-gem" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
            '<path fill="#0ea5e9" d="M2 9h20L12 21z"/><path fill="#38bdf8" d="M6 3h12l4 6H2z"/>' +
            '<path fill="#bae6fd" d="M6 3l2.4 6L12 3z"/><path fill="#7dd3fc" d="M12 3l3.6 6L18 3z"/><path fill="#0284c7" d="M8.4 9 12 21 15.6 9z"/>' +
        '</svg>';

    function rankingPrizeValueHtml(label) {
        var text = (label === null || typeof label === 'undefined') ? '' : String(label).trim();
        if (!text) return '<b class="rk-prize-text is-none">Sin premio</b>';
        var match = text.match(/^(\d[\d.,]*)\s*(?:\ud83d\udc8e|diamantes?|dm)?\s*$/i);
        if (match) return '<b class="rk-prize-num">' + escHtml(match[1]) + '</b>' + RANKING_GEM_SVG;
        return '<b class="rk-prize-text">' + escHtml(text) + '</b>';
    }

    function renderRankingTabs() {
        if (!rankingTabsEl) return;
        rankingTabsEl.innerHTML = '';

        rankingState.items.forEach(function (item) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'ranking-tab' + (item.key === rankingState.activeKey ? ' active' : '');
            btn.dataset.rankingKey = item.key;
            btn.textContent = item.label;
            rankingTabsEl.appendChild(btn);
        });
    }

    // Silueta neutra del jugador en el ranking. El ranking mensual es solo por ID: no muestra fotos de perfil ni abre perfiles (las cuentas son
    // independientes de los IDs, decisión del dueño 2026-10-14).
    function rankingAvatarHtml(entry, className) {
        return '<span class="' + className + '" aria-hidden="true">' +
            '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="8.2" r="4.1"/><path d="M4 21c0-4.4 3.6-7.6 8-7.6s8 3.2 8 7.6z"/></svg>' +
        '</span>';
    }

    var RANKING_ACCOUNT_AVATAR_RE = /^uploads\/profiles\/\d+\/[a-z_]+_[0-9a-f]{12}\.(webp|gif)$/;
    var RANKING_ACCOUNT_HANDLE_RE = /^[A-Za-z0-9._-]{3,20}$/;
    var RANKING_SILHOUETTE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="12" cy="8.2" r="4.1"/><path d="M4 21c0-4.4 3.6-7.6 8-7.6s8 3.2 8 7.6z"/></svg>';
    function rankingAccountUnits(entry) {
        return entry && entry.total_units !== null && typeof entry.total_units !== 'undefined' ? String(entry.total_units) : '—';
    }

    function rankingAccountAvatarHtml(entry, className) {
        var photo = !!(entry && typeof entry.avatar === 'string' && RANKING_ACCOUNT_AVATAR_RE.test(entry.avatar));
        var inner = photo ? '<img src="/static/' + escHtml(entry.avatar) + '" alt="" loading="lazy" decoding="async">' : RANKING_SILHOUETTE_SVG;
        return '<span class="' + className + (photo ? ' has-photo' : '') + '" aria-hidden="true">' + inner + '</span>';
    }

    // Atributos para que una cuenta del ranking se pueda tocar (y se abra el cuadro con "Ver perfil"). Solo si su usuario es válido: sin usuario no hay perfil.
    function rankingAccountTapAttrs(entry, item) {
        var handle = entry && typeof entry.handle === 'string' && RANKING_ACCOUNT_HANDLE_RE.test(entry.handle) ? entry.handle : '';
        if (!handle) return '';
        var photo = typeof entry.avatar === 'string' && RANKING_ACCOUNT_AVATAR_RE.test(entry.avatar) ? entry.avatar : '';
        return ' role="button" tabindex="0" data-rk-handle="' + escHtml(handle) + '" data-rk-name="' + escHtml(entry.name || handle) + '"' +
            ' data-rk-avatar="' + escHtml(photo) + '" data-rk-pos="' + escHtml(entry.position) + '" data-rk-score="' + escHtml(rankingAccountScoreText(entry, item, true)) + '"';
    }

    // Cuadro pequeño que sale al tocar una cuenta del ranking: foto, nombre, @usuario y el botón "Ver perfil" (lleva a /u/<usuario>).
    var rkPopReturnFocus = null;
    function closeRankingProfilePop() {
        var pop = document.getElementById('rkProfilePop');
        if (pop) pop.remove();
        document.removeEventListener('keydown', onRankingPopKey, true);
        if (rkPopReturnFocus && document.contains(rkPopReturnFocus)) { try { rkPopReturnFocus.focus(); } catch (err) { /* nada */ } }
        rkPopReturnFocus = null;
    }
    function onRankingPopKey(evt) {
        if (evt.key === 'Escape') { evt.preventDefault(); evt.stopImmediatePropagation(); closeRankingProfilePop(); }
    }
    function openRankingProfilePop(source) {
        var handle = source.getAttribute('data-rk-handle') || '';
        if (!RANKING_ACCOUNT_HANDLE_RE.test(handle)) return;
        closeRankingProfilePop();
        var photo = source.getAttribute('data-rk-avatar') || '';
        var name = source.getAttribute('data-rk-name') || handle;
        var pos = source.getAttribute('data-rk-pos') || '';
        var score = source.getAttribute('data-rk-score') || '';
        var avatarInner = RANKING_ACCOUNT_AVATAR_RE.test(photo) ? '<img src="/static/' + escHtml(photo) + '" alt="" decoding="async">' : RANKING_SILHOUETTE_SVG;
        var pop = document.createElement('div');
        pop.className = 'rk-pop';
        pop.id = 'rkProfilePop';
        pop.setAttribute('role', 'dialog');
        pop.setAttribute('aria-modal', 'true');
        pop.setAttribute('aria-label', 'Perfil de ' + name);
        pop.innerHTML =
            '<div class="rk-pop-backdrop" data-rk-close></div>' +
            '<div class="rk-pop-card">' +
                '<button type="button" class="rk-pop-x" data-rk-close aria-label="Cerrar">✕</button>' +
                '<span class="rk-pop-avatar" aria-hidden="true">' + avatarInner + '</span>' +
                '<strong class="rk-pop-name">' + escHtml(name) + '</strong>' +
                '<span class="rk-pop-sub">@' + escHtml(handle) + (pos ? ' · Puesto #' + escHtml(pos) : '') + '</span>' +
                (score ? '<p class="rk-pop-note">' + escHtml(score) + '</p>' : '') +
                '<a class="rk-pop-btn" href="/u/' + escHtml(handle) + '" id="rkProfilePopGo">Ver perfil</a>' +
            '</div>';
        rkPopReturnFocus = source;
        pop.addEventListener('click', function (evt) {
            if (evt.target.closest('[data-rk-close]')) closeRankingProfilePop();
        });
        document.body.appendChild(pop);
        document.addEventListener('keydown', onRankingPopKey, true);
        var go = document.getElementById('rkProfilePopGo');
        if (go) go.focus();
    }

    // Recuadro de premios de un ranking (uno por puesto). Sin premios configurados no se muestra nada. Con más de 5 puestos se acomoda en filas de 5.
    function rankingLadderHtml(ladder) {
        if (!ladder || !ladder.length) return '';
        var html =
            '<div class="ranking-prizes" style="--n:' + Math.max(1, ladder.length) + ';--cols:' + Math.min(Math.max(1, ladder.length), 5) + '">' +
                '<div class="ranking-prizes-title">Premios</div>';
        ladder.forEach(function (reward) {
            html +=
                '<div class="ranking-prize-item ranking-podium-' + escHtml(reward.position) + '">' +
                    '<span title="Puesto ' + escHtml(reward.position) + '">' + escHtml(reward.position) + '</span>' +
                    '<strong>' + rankingPrizeValueHtml(reward.reward_label) + '</strong>' +
                '</div>';
        });
        return html + '</div>';
    }

    // Texto de lo que lleva una cuenta en su fila: diamantes del mes (ranking de cuentas) o puntos de nivel con su "Nv" (ranking de niveles).
    // `raw`: texto sin escapar (para guardarlo en un atributo, que se escapa una vez al ponerlo y otra al mostrarlo); sin `raw` sale ya escapado para HTML.
    function rankingAccountScoreText(entry, item, raw) {
        var esc = raw ? function (v) { return String(v == null ? '' : v); } : escHtml;
        var base = rankingAccountUnits(entry) + ' ' + esc(item.units_label || '');
        if (item.kind === 'levels' && entry && entry.level) return 'Nv ' + esc(entry.level) + ' · ' + base;
        return base;
    }

    // ── Pestañas de CUENTAS (las que más recargan en el mes) y de NIVELES (las de más nivel). Las crea y las premia el dueño; el premio de cada puesto
    // puede ser puntos de la tienda para el perfil o un texto. Aquí la foto SÍ lleva al perfil público de la cuenta (el ranking de juegos es solo por ID).
    function renderAccountsRanking(item) {
        var isLevels = item.kind === 'levels';
        var unitsText = escHtml(item.units_label || (isLevels ? 'Puntos de nivel' : 'Diamantes recargados'));
        var entries = item.entries || [];
        var viewer = item.viewer || {};
        var html = '<div class="ranking-shell">';

        html += rankingLadderHtml(item.reward_ladder);

        html += '<div class="ranking-main-card"><div class="ranking-live-chip">LIVE</div>';

        if (!entries.length) {
            html += '<div class="ranking-empty">' + (isLevels
                ? 'Todavía nadie tiene puntos de nivel. ¡Sé el primero!'
                : 'Todavía nadie recargó con su cuenta este mes. ¡Sé el primero!') + '</div>';
        } else {
            var byPosition = {};
            entries.forEach(function (entry) { byPosition[entry.position] = entry; });
            html += '<div class="ranking-podium">';
            [2, 1, 3].forEach(function (pos) {
                var entry = byPosition[pos];
                if (!entry) {
                    html += '<div class="rk-pod is-' + pos + ' is-empty" aria-hidden="true"></div>';
                    return;
                }
                html +=
                    '<div class="rk-pod is-' + pos + (rankingAccountTapAttrs(entry, item) ? ' is-tappable' : '') + '"' + rankingAccountTapAttrs(entry, item) + '>' +
                        (pos === 1 ? '<span class="rk-crown" aria-hidden="true">👑</span>' : '') +
                        rankingAccountAvatarHtml(entry, 'rk-pod-avatar') +
                        '<strong class="rk-pod-name">' + escHtml(entry.name || 'Cuenta') + '</strong>' +
                        '<span class="rk-pod-id">@' + escHtml(entry.handle || '') + '</span>' +
                        '<span class="rk-pod-units">' + rankingAccountScoreText(entry, item) + '</span>' +
                        '<div class="rk-pod-block"><span>' + pos + '</span></div>' +
                    '</div>';
            });
            html += '</div>';

            var restEntries = entries.filter(function (entry) { return Number(entry.position) > 3; });
            if (restEntries.length) {
                var restLimit = Math.max(RANKING_COLLAPSED_ROWS - 3, 1);
                var visibleRest = rankingState.showAllPositions ? restEntries : restEntries.slice(0, restLimit);
                html += '<div class="rk-list"><div class="rk-list-title">Otras cuentas</div>';
                visibleRest.forEach(function (entry) {
                    html +=
                        '<div class="rk-row' + (rankingAccountTapAttrs(entry, item) ? ' is-tappable' : '') + '"' + rankingAccountTapAttrs(entry, item) + '>' +
                            '<span class="rk-row-pos">#' + escHtml(entry.position) + '</span>' +
                            rankingAccountAvatarHtml(entry, 'rk-row-avatar') +
                            '<span class="rk-row-text"><strong>' + escHtml(entry.name || 'Cuenta') + '</strong><small>@' + escHtml(entry.handle || '') + '</small></span>' +
                            '<span class="rk-row-units">' + escHtml(rankingAccountUnits(entry)) + '<small>' + (isLevels && entry.level ? 'Nv ' + escHtml(entry.level) + ' · ' : '') + unitsText + '</small></span>' +
                        '</div>';
                });
                html += '</div>';
                if (restEntries.length > restLimit) {
                    html +=
                        '<button type="button" class="ranking-show-more-btn" id="rankingShowMoreBtn">' +
                            (rankingState.showAllPositions ? '▲ Ver menos' : '▼ Ver más (hasta Top ' + entries.length + ')') +
                        '</button>';
                }
            }
        }

        if (item.previous_winners && item.previous_winners.entries && item.previous_winners.entries.length) {
            html +=
                '<div class="ranking-archive-card">' +
                    '<div class="ranking-archive-title">Ganadores de ' + escHtml(item.previous_winners.label || 'el mes pasado') + '</div>' +
                    '<div class="ranking-archive-list">';
            item.previous_winners.entries.forEach(function (entry) {
                html +=
                    '<div class="ranking-archive-item">' +
                        '<strong>#' + escHtml(entry.position) + '</strong>' +
                        '<span>' + escHtml(entry.name || 'Cuenta') + '</span>' +
                        '<span>@' + escHtml(entry.handle || '') + (entry.prize_label ? ' · ' + escHtml(entry.prize_label) : '') + '</span>' +
                    '</div>';
            });
            html += '</div></div>';
        }

        if (item.current_position) {
            var mine = item.current_position;
            html +=
                '<div class="ranking-current-card">' +
                    '<div class="ranking-current-title">Tu posición actual <span class="ranking-current-position-num">#' + escHtml(mine.position) + '</span></div>' +
                    '<div class="ranking-current-meta"><strong>' + (isLevels && mine.level ? 'Nv ' + escHtml(mine.level) + ' · ' : '') + escHtml(mine.total_units) + ' ' + unitsText + '</strong></div>' +
                    '<div class="ranking-current-hint">' + (isLevels ? 'Sigue sumando puntos de nivel para subir de puesto.' : 'Sigue recargando con tu cuenta para subir de puesto.') + '</div>' +
                '</div>';
        } else if (viewer.is_account) {
            html +=
                '<div class="ranking-current-card is-empty">' +
                    '<div class="ranking-current-title">Tu posición actual</div>' +
                    '<div class="ranking-current-hint">' + (isLevels
                        ? 'Aún no estás en el ranking. Suma puntos de nivel con tu cuenta. Para salir aquí tu perfil debe ser público y tener encendido "Aparecer en el Top y en el Ranking" (en Personalizar mi perfil).'
                        : 'Aún no estás en el ranking. Recarga con tu cuenta este mes. Para salir aquí tu perfil debe ser público y tener encendido "Aparecer en el Top y en el Ranking" (en Personalizar mi perfil).') + '</div>' +
                '</div>';
        } else {
            html +=
                '<div class="ranking-current-card is-empty">' +
                    '<div class="ranking-current-title">¿Quieres entrar al ranking?</div>' +
                    '<div class="ranking-current-hint">' + (isLevels
                        ? 'Crea tu cuenta gratis y sube de nivel: tus compras, misiones y logros suman puntos para este ranking.'
                        : 'Crea tu cuenta gratis y recarga con ella: tus compras del mes cuentan para este ranking.') + '</div>' +
                    '<div class="ranking-cta-row">' +
                        '<a class="ranking-cta" href="/register">Crear mi cuenta</a>' +
                        '<a class="ranking-cta is-ghost" href="/login">Ya tengo cuenta</a>' +
                    '</div>' +
                '</div>';
        }

        html += '</div></div>';

        rankingBoardEl.innerHTML = html;
        rankingBoardEl.style.display = 'block';
        if (rankingStatusEl) {
            rankingStatusEl.style.display = 'none';
        }
        renderRankingTabs();
    }

    function renderRankingBoard() {
        if (!rankingBoardEl) return;

        var activeItem = null;
        for (var i = 0; i < rankingState.items.length; i += 1) {
            if (rankingState.items[i].key === rankingState.activeKey) {
                activeItem = rankingState.items[i];
                break;
            }
        }

        if (!activeItem) {
            setRankingStatus('No hay rankings disponibles en este momento.');
            return;
        }

        if (activeItem.kind === 'accounts' || activeItem.kind === 'levels') {
            renderAccountsRanking(activeItem);
            return;
        }

        var html = '<div class="ranking-shell">' + rankingLadderHtml(activeItem.reward_ladder || []);

        html +=
                '<div class="ranking-main-card">' +
                    '<div class="ranking-live-chip">LIVE</div>';

        if (!activeItem.entries || activeItem.entries.length === 0) {
            html += '<div class="ranking-empty">Aún no hay posiciones registradas para este mes.</div>';
        } else {
            var unitsText = escHtml(activeItem.units_label || 'Total');
            var byPosition = {};
            activeItem.entries.forEach(function (entry) { byPosition[entry.position] = entry; });

            // Podio: los 3 primeros arriba (el 1.º al centro y más alto, el 2.º a su izquierda y el 3.º a su derecha).
            html += '<div class="ranking-podium">';
            [2, 1, 3].forEach(function (pos) {
                var entry = byPosition[pos];
                if (!entry) {
                    html += '<div class="rk-pod is-' + pos + ' is-empty" aria-hidden="true"></div>';
                    return;
                }
                html +=
                    '<div class="rk-pod is-' + pos + '">' +
                        (pos === 1 ? '<span class="rk-crown" aria-hidden="true">👑</span>' : '') +
                        rankingAvatarHtml(entry, 'rk-pod-avatar') +
                        '<strong class="rk-pod-name">' + escHtml(entry.masked_nickname || 'Jugador***') + '</strong>' +
                        '<span class="rk-pod-id">' + escHtml(entry.masked_player_id || '----') + '</span>' +
                        '<span class="rk-pod-units">' + escHtml(entry.total_units) + ' ' + unitsText + '</span>' +
                        '<div class="rk-pod-block"><span>' + pos + '</span></div>' +
                    '</div>';
            });
            html += '</div>';

            // Lista de abajo: del 4.º en adelante (con "Ver más" hasta el Top 10).
            var restEntries = activeItem.entries.filter(function (entry) { return Number(entry.position) > 3; });
            if (restEntries.length) {
                var restLimit = Math.max(RANKING_COLLAPSED_ROWS - 3, 1);
                var visibleRest = rankingState.showAllPositions ? restEntries : restEntries.slice(0, restLimit);
                html += '<div class="rk-list"><div class="rk-list-title">Otros participantes</div>';
                visibleRest.forEach(function (entry) {
                    html +=
                        '<div class="rk-row">' +
                            '<span class="rk-row-pos">#' + escHtml(entry.position) + '</span>' +
                            rankingAvatarHtml(entry, 'rk-row-avatar') +
                            '<span class="rk-row-text">' +
                                '<strong>' + escHtml(entry.masked_nickname || 'Jugador***') + '</strong>' +
                                '<small>' + escHtml(entry.masked_player_id || '----') + '</small>' +
                            '</span>' +
                            '<span class="rk-row-units">' + escHtml(entry.total_units) + '<small>' + unitsText + '</small></span>' +
                        '</div>';
                });
                html += '</div>';
                if (restEntries.length > restLimit) {
                    html +=
                        '<button type="button" class="ranking-show-more-btn" id="rankingShowMoreBtn">' +
                            (rankingState.showAllPositions ? '▲ Ver menos' : '▼ Ver más (hasta Top ' + activeItem.entries.length + ')') +
                        '</button>';
                }
            }
        }

        if (activeItem.previous_winners && activeItem.previous_winners.entries && activeItem.previous_winners.entries.length) {
            html +=
                '<div class="ranking-archive-card">' +
                    '<div class="ranking-archive-title">Ganadores archivados ' + escHtml(activeItem.previous_winners.label || '') + '</div>' +
                    '<div class="ranking-archive-list">';

            activeItem.previous_winners.entries.forEach(function (entry) {
                html +=
                    '<div class="ranking-archive-item">' +
                        '<strong>#' + escHtml(entry.position) + '</strong>' +
                        '<span>' + escHtml(entry.masked_nickname || 'Jugador***') + '</span>' +
                        '<span>' + escHtml(formatRewardValue(entry.prize_label || 'Sin premio', activeItem.key)) + '</span>' +
                    '</div>';
            });

            html += '</div></div>';
        }

        if (activeItem.current_position) {
            html +=
                '<div class="ranking-current-card">' +
                    '<div class="ranking-current-title">Tu posición actual <span class="ranking-current-position-num">#' + escHtml(activeItem.current_position.position) + '</span></div>' +
                    '<div class="ranking-current-meta">' +
                        '<span>' + escHtml(activeItem.current_position.masked_player_id || '----') + '</span>' +
                        '<strong>' + escHtml(activeItem.current_position.total_units) + ' ' + escHtml(activeItem.units_label || '') + '</strong>' +
                    '</div>' +
                    '<div class="ranking-progress">' +
                        '<div class="ranking-progress-bar" style="width:' + escHtml(activeItem.current_position.progress_percent) + '%"></div>' +
                        '<span>' + escHtml(activeItem.current_position.progress_percent) + '%</span>' +
                    '</div>';

            if (activeItem.current_position.missing_units > 0) {
                html += '<div class="ranking-current-hint">Te faltan ' + escHtml(activeItem.current_position.missing_units) + ' ' + escHtml(activeItem.units_label || '') + ' para el siguiente puesto.</div>';
            } else if (Number(activeItem.current_position.position) === 1) {
                html += '<div class="ranking-current-hint">Eres el líder de este ranking. Recarga más para mantener tu puesto y que nadie te alcance.</div>';
            } else {
                html += '<div class="ranking-current-hint">Recarga más para subir al siguiente puesto en la próxima actualización del ranking.</div>';
            }

            html += '</div>';
        } else {
            html +=
                '<div class="ranking-current-card is-empty">' +
                    '<div class="ranking-current-title">Tu posición actual</div>' +
                    '<div class="ranking-lookup-row">' +
                        '<input type="text" inputmode="numeric" id="rankingLookupIdInput" placeholder="Tu ID de ' + escHtml(activeItem.game_name || 'juego') + '..." value="' + escHtml(rankingState.lookupInputValue || '') + '">' +
                        '<button type="button" id="rankingLookupBtn">Ver</button>' +
                    '</div>' +
                    '<p class="ranking-lookup-msg" id="rankingLookupMsg"></p>' +
                '</div>';
        }

        html += '</div></div>';

        rankingBoardEl.innerHTML = html;
        rankingBoardEl.style.display = 'block';
        if (rankingStatusEl) {
            rankingStatusEl.style.display = 'none';
        }
        renderRankingTabs();
    }

    function runRankingPositionLookup() {
        var input = document.getElementById('rankingLookupIdInput');
        var msgEl = document.getElementById('rankingLookupMsg');
        var btn = document.getElementById('rankingLookupBtn');
        if (!input || !msgEl) return;

        var activeItem = null;
        for (var i = 0; i < rankingState.items.length; i += 1) {
            if (rankingState.items[i].key === rankingState.activeKey) {
                activeItem = rankingState.items[i];
                break;
            }
        }
        if (!activeItem || !activeItem.game_id) return;

        var identifier = String(input.value || '').trim();
        rankingState.lookupInputValue = identifier;

        msgEl.className = 'ranking-lookup-msg';
        if (!identifier) {
            msgEl.textContent = 'Escribe tu ID primero.';
            msgEl.classList.add('is-error');
            return;
        }

        if (btn) { btn.disabled = true; btn.textContent = 'Buscando...'; }
        msgEl.textContent = '';

        fetch('/promos/api/verify-player?game_id=' + encodeURIComponent(activeItem.game_id) + '&player_id=' + encodeURIComponent(identifier))
            .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
            .then(function (result) {
                var verifyOk = result.data && result.data.ok;
                // 403 = este juego no tiene API de verificación configurada:
                // no bloquea la búsqueda, solo no se puede confirmar el ID de antemano.
                var verifyUnavailable = result.status === 403;
                if (!verifyOk && !verifyUnavailable) {
                    throw new Error((result.data && result.data.error) || 'ID no encontrado');
                }
                return fetch('/api/rankings?lookup_game_id=' + encodeURIComponent(activeItem.game_id) + '&lookup_identifier=' + encodeURIComponent(identifier));
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var rankings = data && Array.isArray(data.rankings) ? data.rankings : [];
                var updated = null;
                for (var k = 0; k < rankings.length; k += 1) {
                    if (rankings[k] && rankings[k].key === rankingState.activeKey) { updated = rankings[k]; break; }
                }
                if (updated) {
                    for (var j = 0; j < rankingState.items.length; j += 1) {
                        if (rankingState.items[j].key === updated.key) {
                            rankingState.items[j].current_position = updated.current_position;
                            break;
                        }
                    }
                }
                if (updated && updated.current_position) {
                    renderRankingBoard();
                } else {
                    msgEl.textContent = 'Ese ID no aparece en el ranking de este mes.';
                    msgEl.classList.add('is-error');
                    if (btn) { btn.disabled = false; btn.textContent = 'Ver'; }
                }
            })
            .catch(function (err) {
                msgEl.textContent = window.nxFriendlyError(err, 'ID no encontrado');
                msgEl.classList.add('is-error');
                if (btn) { btn.disabled = false; btn.textContent = 'Ver'; }
            });
    }

    function fetchRankings(forceReload) {
        if (rankingState.loading) return;
        if (rankingState.loaded && !forceReload) {
            renderRankingBoard();
            return;
        }

        rankingState.loading = true;
        setRankingStatus('Cargando ranking...');

        window.nxFetch('/api/rankings' + getRankingLookupParams(), undefined, 20000)
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (data && data.error && !(Array.isArray(data.rankings) && data.rankings.length)) {
                    setRankingStatus(data.error);
                    return;
                }
                var rankings = data && Array.isArray(data.rankings) ? data.rankings : [];
                rankingState.items = rankings.filter(function (item) {
                    return item && item.enabled;
                });
                rankingState.activeKey = rankingState.items.length ? rankingState.items[0].key : null;
                rankingState.loaded = true;
                renderRankingTabs();
                renderRankingBoard();
            })
            .catch(function (err) {
                setRankingStatus(window.nxFriendlyError(err, 'No se pudo cargar el ranking en este momento.'));
            })
            .finally(function () {
                rankingState.loading = false;
            });
    }

    function isRankingModalOpen() {
        return !!(rankingModal && rankingModal.getAttribute('aria-hidden') === 'false');
    }

    function invalidateRankingLookup() {
        rankingState.loaded = false;
    }

    function refreshRankingLookupIfVisible() {
        invalidateRankingLookup();
        if (isRankingModalOpen()) {
            fetchRankings(true);
        }
    }

    function openRankingModal() {
        if (!rankingModal) return;
        openModal(rankingModal);
        fetchRankings(true);
    }

    function closeRankingModal() {
        closeRankingProfilePop();
        closeModal(rankingModal);
    }

    /* ── Canjear Puntos ───────────────────────────────────── */
    // gameCaps: { [gameId]: { name, prizeLabel, hasSpin, hasRedeem } } — un
    // juego puede tener ruleta, canje directo, o ambos; el paso de elección
    // (o el salto directo a uno solo) depende de esto.
    var pointsState = {
        gameId: null, playerId: '', spinCost: 5, spinning: false, gamesLoaded: false,
        gameCaps: {}, lastBalanceData: null, selectedRedeemOptionId: null, redeemBusy: false,
        walletBalance: 0, walletBusy: false
    };

    function pointsEl(id) { return document.getElementById(id); }

    function loadPointsGames() {
        if (pointsState.gamesLoaded) return;
        var select = pointsEl('pointsGameSelect');
        if (!select) return;

        Promise.all([
            fetch('/api/points/games').then(function (r) { return r.json(); }).catch(function () { return null; }),
            fetch('/api/points/redeem-games').then(function (r) { return r.json(); }).catch(function () { return null; })
        ]).then(function (results) {
            var spinData = results[0];
            var redeemData = results[1];
            if (!spinData && !redeemData) {   // sin conexión: no se da por cargado, al abrir otra vez se vuelve a pedir (antes quedaba vacío hasta recargar)
                select.innerHTML = '<option value="">No se pudo cargar. Cierra y vuelve a abrir esta ventana.</option>';
                return;
            }
            pointsState.gamesLoaded = true;
            pointsState.spinCost = (spinData && spinData.spin_cost) || 5;

            var caps = {};
            ((spinData && spinData.games) || []).forEach(function (g) {
                caps[g.game_id] = caps[g.game_id] || { name: g.game_name };
                caps[g.game_id].hasSpin = true;
                caps[g.game_id].prizeLabel = g.prize_label;
            });
            ((redeemData && redeemData.games) || []).forEach(function (g) {
                caps[g.game_id] = caps[g.game_id] || { name: g.game_name };
                caps[g.game_id].hasRedeem = true;
            });
            pointsState.gameCaps = caps;

            var gameIds = Object.keys(caps);
            if (!gameIds.length) {
                select.innerHTML = '<option value="">Todavía no hay premios de puntos disponibles</option>';
                return;
            }
            select.innerHTML = '<option value="">Selecciona un juego</option>' + gameIds.map(function (id) {
                var g = caps[id];
                return '<option value="' + id + '" data-prize="' + escHtml(g.prizeLabel || '') + '">' + escHtml(g.name) + '</option>';
            }).join('');
        });
    }

    function hidePointsSteps() {
        ['pointsChoiceStep', 'pointsSpinStep', 'pointsRedeemStep'].forEach(function (id) {
            var el = pointsEl(id);
            if (el) el.style.display = 'none';
        });
    }

    // ── Puntos del PERFIL: solo para cuentas que tienen puntos (premio de ranking o puestos por el administrador). Los puntos de la tienda funcionan
    // por ID, así que aquí se pregunta a qué ID (juego + ID) se los vas a recargar. Si el perfil no tiene puntos, no se ve nada de esto.
    function renderPointsWallet() {
        var balance = Math.max(0, parseInt(pointsState.walletBalance, 10) || 0);
        var has = balance > 0;
        var box = pointsEl('pointsWalletBox');
        var step = pointsEl('pointsLookupStep');
        if (box) box.style.display = has ? 'flex' : 'none';
        Array.prototype.forEach.call(document.querySelectorAll('#pointsModal .points-wallet-only'), function (el) {
            el.style.display = has ? 'block' : 'none';
        });
        if (step) step.classList.toggle('has-wallet', has);
        var label = pointsEl('pointsWalletBalance');
        if (label) label.textContent = String(balance);
        var amount = pointsEl('pointsWalletAmount');
        if (amount) {
            amount.max = String(balance);
            amount.placeholder = 'Máx. ' + balance;
        }
    }

    function loadPointsWallet() {
        fetch('/api/points/wallet', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                pointsState.walletBalance = (data && data.ok && data.logged_in) ? (parseInt(data.balance, 10) || 0) : 0;
                renderPointsWallet();
            })
            .catch(function () { /* sin conexión: el canje normal por ID sigue funcionando */ });
    }

    function handlePointsWalletTransfer() {
        if (pointsState.walletBusy) return;
        var select = pointsEl('pointsGameSelect');
        var idInput = pointsEl('pointsPlayerIdInput');
        var amountInput = pointsEl('pointsWalletAmount');
        var errEl = pointsEl('pointsLookupError');
        var btn = pointsEl('pointsWalletBtn');
        var gameId = select ? select.value : '';
        var playerId = idInput ? idInput.value.trim() : '';
        var amount = amountInput ? parseInt(amountInput.value, 10) : NaN;

        function fail(text) {
            if (errEl) { errEl.textContent = text; errEl.style.display = 'block'; }
        }
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
        if (!gameId) return fail('Elige un juego.');
        if (!playerId) return fail('Ingresa el ID al que quieres recargar tus puntos.');
        if (!amount || amount < 1) return fail('Escribe cuántos puntos quieres recargar.');
        if (amount > pointsState.walletBalance) return fail('Tu perfil solo tiene ' + pointsState.walletBalance + ' puntos.');

        pointsState.walletBusy = true;
        if (btn) { btn.disabled = true; btn.textContent = 'RECARGANDO...'; }
        fetch('/api/points/transfer', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ game_id: gameId, player_id: playerId, amount: amount })
        })
            .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
            .then(function (res) {
                pointsState.walletBusy = false;
                if (btn) { btn.disabled = false; btn.textContent = 'RECARGAR PUNTOS A ESTE ID'; }
                if (!res.data.ok) {
                    fail(res.data.message || 'No se pudieron pasar los puntos.');
                    return;
                }
                pointsState.walletBalance = parseInt(res.data.wallet_balance, 10) || 0;
                renderPointsWallet();
                if (amountInput) amountInput.value = '';
                var note = pointsEl('pointsTransferNote');
                if (note) {
                    note.textContent = '✔ Se recargaron ' + amount + ' puntos al ID ' + playerId + (res.data.points && res.data.points.player_nick ? ' (' + res.data.points.player_nick + ')' : '') + '.';
                    note.style.display = 'block';
                }
                showPointsBalance(res.data.points);
            })
            .catch(function () {
                pointsState.walletBusy = false;
                if (btn) { btn.disabled = false; btn.textContent = 'RECARGAR PUNTOS A ESTE ID'; }
                fail('Error de conexión. Intenta de nuevo.');
            });
    }

    function resetPointsModal() {
        var lookupStep = pointsEl('pointsLookupStep');
        if (lookupStep) lookupStep.style.display = 'block';
        renderPointsWallet();
        var transferNote = pointsEl('pointsTransferNote');
        if (transferNote) { transferNote.style.display = 'none'; transferNote.textContent = ''; }
        hidePointsSteps();
        var err = pointsEl('pointsLookupError');
        if (err) { err.style.display = 'none'; err.textContent = ''; }
        var result = pointsEl('pointsSpinResult');
        if (result) { result.style.display = 'none'; result.textContent = ''; }
        var redeemErr = pointsEl('pointsRedeemError');
        if (redeemErr) { redeemErr.style.display = 'none'; redeemErr.textContent = ''; }
        var redeemResult = pointsEl('pointsRedeemResult');
        if (redeemResult) { redeemResult.style.display = 'none'; redeemResult.textContent = ''; }
        pointsState.selectedRedeemOptionId = null;
    }

    function openPointsModal() {
        if (!pointsModal) return;
        loadPointsGames();
        resetPointsModal();
        loadPointsWallet();
        openModal(pointsModal);
    }

    function closePointsModal() {
        closeModal(pointsModal);
    }

    // Relleno de la tira: cajas de premio y de nada, mezcladas. La casilla
    // de premio dice "PREMIO" a secas y nunca el premio real (ej. "110💎"):
    // el valor se revela recién en el modal, después del giro.
    var POINTS_ITEM_PRIZE = { icon: '🎁', label: 'PREMIO', prize: true };
    var POINTS_ITEM_MISS = { icon: '❌', label: 'NADA', prize: false };
    var POINTS_ITEM_POOL = [
        POINTS_ITEM_MISS, POINTS_ITEM_PRIZE,
        POINTS_ITEM_MISS, POINTS_ITEM_PRIZE,
        POINTS_ITEM_MISS, POINTS_ITEM_PRIZE
    ];
    var POINTS_STRIP_TOTAL = 70;
    var POINTS_STRIP_WINNER_INDEX = 52;
    var POINTS_STRIP_ITEM_WIDTH = 112;
    var POINTS_STRIP_ITEM_STRIDE = 124; // 112px de ancho + 12px de gap
    var POINTS_STRIP_SPIN_MS = 4600;    // los 4.5s de la transicion + margen
    // En modo liviano no hay animación (la tira salta al resultado), así que no se espera lo que dura el giro.
    function pointsSpinMs() { return document.documentElement.classList.contains('lite') ? 250 : POINTS_STRIP_SPIN_MS; }
    var pointsStripItems = [];

    /* Cuánto hay que desplazar la tira para dejar ese ítem bajo el láser.
       El láser está en el centro del visor, así que se descuenta media
       ventana y se recentra sobre la caja. */
    function pointsStripOffsetFor(index, jitter) {
        var track = pointsEl('pointsStripTrack');
        var ancho = (track && track.parentElement) ? track.parentElement.clientWidth : 0;
        return (index * POINTS_STRIP_ITEM_STRIDE)
            - (ancho / 2)
            + (POINTS_STRIP_ITEM_WIDTH / 2)
            + (jitter || 0);
    }

    function renderPointsStrip() {
        var track = pointsEl('pointsStripTrack');
        if (!track) return;
        track.innerHTML = pointsStripItems.map(function (item) {
            return '<div class="points-strip-item">' +
                '<span class="icon">' + item.icon + '</span>' +
                '<span class="label">' + escHtml(item.label) + '</span>' +
            '</div>';
        }).join('');
    }

    /* Arma la tira de cero y la deja en su posición de arranque. */
    function buildPointsStrip() {
        var track = pointsEl('pointsStripTrack');
        if (!track) return;
        pointsStripItems = [];
        for (var i = 0; i < POINTS_STRIP_TOTAL; i++) {
            pointsStripItems.push(POINTS_ITEM_POOL[Math.floor(Math.random() * POINTS_ITEM_POOL.length)]);
        }
        renderPointsStrip();
        track.style.transition = 'none';
        track.style.transform = 'translateX(0px)';
    }

    function spinPointsStripTo(won) {
        var track = pointsEl('pointsStripTrack');
        var viewport = track ? track.parentElement : null;
        if (!track) return;

        // La casilla donde para es la que manda: se reescribe con el
        // resultado que ya decidió el servidor, para que lo que se ve bajo
        // el láser diga exactamente lo mismo que el mensaje de después.
        pointsStripItems[POINTS_STRIP_WINNER_INDEX] = won ? POINTS_ITEM_PRIZE : POINTS_ITEM_MISS;
        renderPointsStrip();

        var card = pointsEl('pointsModal') ? pointsEl('pointsModal').querySelector('.points-modal-card') : null;
        if (viewport) viewport.classList.add('is-rolling');
        if (card) card.classList.add('is-rolling');

        track.style.transition = 'none';
        track.style.transform = 'translateX(0px)';
        void track.offsetWidth;   // reflow: sin esto el navegador une los dos estados

        // Un pelín descentrada a propósito: una caja real no frena clavada
        // en el medio.
        var jitter = (Math.random() * 20) - 10;
        track.style.transition = 'transform 4.5s cubic-bezier(.08, .82, .15, 1)';
        track.style.transform = 'translateX(-' + pointsStripOffsetFor(POINTS_STRIP_WINNER_INDEX, jitter) + 'px)';

        window.setTimeout(function () {
            if (viewport) viewport.classList.remove('is-rolling');
            if (card) card.classList.remove('is-rolling');
            var items = track.querySelectorAll('.points-strip-item');
            var winnerEl = items[POINTS_STRIP_WINNER_INDEX];
            if (winnerEl && won) winnerEl.classList.add('is-winner');
        }, pointsSpinMs());
    }

    /* Modal del resultado: al ganar el regalo se abre por pasos (tiembla,
       revienta, aparece el premio); al perder se queda la equis quieta.
       `directRedeem` es para el canje directo (sin ruleta): no hay chance
       de perder, así que el texto habla de "canjeaste" en vez de "ganaste". */
    function showPointsResultModal(won, rewardLabel, gameName, directRedeem) {
        var previo = document.getElementById('pointsResultModal');
        if (previo) previo.remove();

        var premio = String(rewardLabel || 'tu premio').trim();
        var modal = document.createElement('div');
        modal.className = 'points-result-modal is-open';
        modal.id = 'pointsResultModal';
        modal.innerHTML =
            '<div class="points-result-card" role="dialog" aria-modal="true">' +
                '<div class="points-result-glow"></div>' +
                '<div class="points-gift-stage">' +
                    '<div class="points-gift-emoji" id="pointsGiftEmoji">' + (won ? '🎁' : '❌') + '</div>' +
                '</div>' +
                '<h3 class="points-result-title" id="pointsResultTitle">' +
                    (won ? (directRedeem ? '¡Procesando canje!' : '¡Abriendo Regalo!') : '¡Fallaste!') +
                '</h3>' +
                '<p class="points-result-text" id="pointsResultDesc">' +
                    (won
                        ? (directRedeem ? 'Preparando tu paquete...' : 'Descubriendo tu premio exclusivo...')
                        : 'No has obtenido recompensa en esta caja.') +
                '</p>' +
                '<button type="button" class="points-result-close">CONTINUAR</button>' +
            '</div>';

        var timers = [];
        function cerrar() {
            timers.forEach(window.clearTimeout);
            timers = [];
            modal.classList.remove('is-visible');
            window.setTimeout(function () { modal.remove(); }, 300);
            // La tira vuelve a barajarse para el siguiente giro.
            buildPointsStrip();
        }
        modal.addEventListener('click', function (evt) {
            if (evt.target === modal || evt.target.classList.contains('points-result-close')) cerrar();
        });
        document.body.appendChild(modal);
        window.requestAnimationFrame(function () { modal.classList.add('is-visible'); });

        if (!won) return;

        var emoji = modal.querySelector('#pointsGiftEmoji');
        var titulo = modal.querySelector('#pointsResultTitle');
        var desc = modal.querySelector('#pointsResultDesc');
        emoji.className = 'points-gift-emoji is-shaking';
        timers.push(window.setTimeout(function () {
            emoji.textContent = '📦✨';
            emoji.className = 'points-gift-emoji is-popping';
        }, 800));
        timers.push(window.setTimeout(function () {
            emoji.textContent = '🎁💎';
            emoji.className = 'points-gift-emoji';
            titulo.textContent = directRedeem ? ('¡Canjeaste ' + premio + '!') : ('¡Ganaste ' + premio + '!');
            desc.textContent = '¡Felicidades! Se procesará al mismo ID'
                + (gameName ? ' de ' + gameName : '') + '.';
        }, 1600));
    }

    // El ID solo, o "ID + nombre real" cuando el juego tiene verificador
    // configurado y ya se consiguió el nick al consultar el saldo.
    function pointsIdLabelText(data) {
        var text = 'ID: ' + data.player_id;
        if (data.player_nick) text += ' • ' + data.player_nick;
        return text;
    }

    function enterPointsSpinStep(data) {
        var select = pointsEl('pointsGameSelect');
        var selectedOption = select ? select.options[select.selectedIndex] : null;
        var prizeLabel = (selectedOption && selectedOption.getAttribute('data-prize')) || 'Premio';

        hidePointsSteps();
        pointsEl('pointsSpinStep').style.display = 'block';
        pointsEl('pointsSpinGameLabel').textContent = data.game_name;
        pointsEl('pointsSpinIdLabel').textContent = pointsIdLabelText(data);
        pointsEl('pointsBalanceValue').textContent = data.balance;
        pointsEl('pointsPrizeLabel').textContent = prizeLabel;
        pointsEl('pointsSpinCostLabel').textContent = data.spin_cost;

        var spinBtn = pointsEl('pointsSpinBtn');
        if (spinBtn) spinBtn.disabled = data.balance < data.spin_cost;

        var statusEl = pointsEl('pointsStatusMsg');
        if (statusEl) {
            statusEl.innerHTML = 'Pulsa <b>GIRAR</b> para abrir tu caja (' + data.spin_cost + ' puntos).';
        }

        buildPointsStrip();
    }

    /* Pinta la lista de paquetes canjeables directo. Los que cuestan más de
       lo que el cliente tiene quedan visibles pero deshabilitados, así ve
       la lista completa de premios aunque no le alcance todavía. */
    function renderPointsRedeemOptions(options, balance) {
        var list = pointsEl('pointsRedeemList');
        var btn = pointsEl('pointsRedeemBtn');
        if (!list) return;
        pointsState.selectedRedeemOptionId = null;
        if (btn) btn.disabled = true;

        if (!options.length) {
            list.innerHTML = '<p class="points-modal-hint" style="padding:0;margin:0;">Todavía no hay paquetes de canje directo para este juego.</p>';
            return;
        }

        list.innerHTML = options.map(function (opt) {
            var disabled = opt.points_cost > balance;
            return '<div class="points-redeem-option' + (disabled ? ' is-disabled' : '') + '" data-option-id="' + opt.option_id + '">' +
                '<span class="points-redeem-option-name">' + escHtml(opt.package_name) + '</span>' +
                '<span class="points-redeem-option-cost">' + opt.points_cost + ' pts</span>' +
            '</div>';
        }).join('');

        Array.prototype.forEach.call(list.querySelectorAll('.points-redeem-option'), function (row) {
            row.addEventListener('click', function () {
                if (row.classList.contains('is-disabled')) return;
                Array.prototype.forEach.call(list.querySelectorAll('.points-redeem-option'), function (r) {
                    r.classList.remove('is-selected');
                });
                row.classList.add('is-selected');
                pointsState.selectedRedeemOptionId = row.getAttribute('data-option-id');
                if (btn) btn.disabled = false;
            });
        });
    }

    function enterPointsRedeemStep(data) {
        hidePointsSteps();
        pointsEl('pointsRedeemStep').style.display = 'block';
        pointsEl('pointsRedeemGameLabel').textContent = data.game_name;
        pointsEl('pointsRedeemIdLabel').textContent = pointsIdLabelText(data);
        pointsEl('pointsRedeemBalanceValue').textContent = data.balance;
        var errEl = pointsEl('pointsRedeemError');
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
        var resultEl = pointsEl('pointsRedeemResult');
        if (resultEl) { resultEl.style.display = 'none'; resultEl.textContent = ''; }

        var list = pointsEl('pointsRedeemList');
        if (list) list.innerHTML = '<p class="points-modal-hint" style="padding:0;margin:0;">Cargando paquetes...</p>';

        fetch('/api/points/redeem-options?game_id=' + encodeURIComponent(data.game_id))
            .then(function (r) { return r.json(); })
            .then(function (res) {
                renderPointsRedeemOptions((res && res.options) || [], data.balance);
            })
            .catch(function () {
                if (list) list.innerHTML = '<p class="points-modal-error" style="margin:0;display:block;">No se pudieron cargar los paquetes.</p>';
            });
    }

    function enterPointsChoiceStep(data) {
        hidePointsSteps();
        pointsEl('pointsChoiceStep').style.display = 'block';
        pointsEl('pointsChoiceGameLabel').textContent = data.game_name;
        pointsEl('pointsChoiceIdLabel').textContent = pointsIdLabelText(data);
        pointsEl('pointsChoiceBalanceValue').textContent = data.balance;
    }

    function showPointsBalance(data) {
        pointsState.gameId = data.game_id;
        pointsState.playerId = data.player_id;
        pointsState.spinCost = data.spin_cost;
        pointsState.lastBalanceData = data;

        var caps = pointsState.gameCaps[data.game_id] || {};
        pointsEl('pointsLookupStep').style.display = 'none';

        if (caps.hasSpin && caps.hasRedeem) {
            enterPointsChoiceStep(data);
        } else if (caps.hasRedeem) {
            enterPointsRedeemStep(data);
        } else {
            enterPointsSpinStep(data);
        }
    }

    function handlePointsLookup() {
        var select = pointsEl('pointsGameSelect');
        var idInput = pointsEl('pointsPlayerIdInput');
        var errEl = pointsEl('pointsLookupError');
        var gameId = select ? select.value : '';
        var playerId = idInput ? idInput.value.trim() : '';

        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }

        if (!gameId) {
            if (errEl) { errEl.textContent = 'Elige un juego.'; errEl.style.display = 'block'; }
            return;
        }
        if (!playerId) {
            if (errEl) { errEl.textContent = 'Ingresa tu ID.'; errEl.style.display = 'block'; }
            return;
        }

        var btn = pointsEl('pointsLookupBtn');
        if (btn) { btn.disabled = true; btn.textContent = 'Consultando...'; }

        fetch('/api/points/balance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ game_id: gameId, player_id: playerId })
        })
            .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
            .then(function (res) {
                if (btn) { btn.disabled = false; btn.textContent = 'CONSULTAR SALDO'; }
                if (!res.data.ok) {
                    if (errEl) { errEl.textContent = res.data.message || 'No se pudo consultar el saldo.'; errEl.style.display = 'block'; }
                    return;
                }
                showPointsBalance(res.data);
            })
            .catch(function () {
                if (btn) { btn.disabled = false; btn.textContent = 'CONSULTAR SALDO'; }
                if (errEl) { errEl.textContent = 'Error de conexión. Intenta de nuevo.'; errEl.style.display = 'block'; }
            });
    }

    function handlePointsSpin() {
        if (pointsState.spinning || !pointsState.gameId || !pointsState.playerId) return;
        var spinBtn = pointsEl('pointsSpinBtn');
        var changeBtn = pointsEl('pointsChangeIdBtn');
        var resultEl = pointsEl('pointsSpinResult');
        if (resultEl) { resultEl.style.display = 'none'; resultEl.className = 'points-modal-result'; }

        pointsState.spinning = true;
        if (spinBtn) { spinBtn.disabled = true; spinBtn.textContent = 'GIRANDO...'; }
        if (changeBtn) changeBtn.disabled = true;

        fetch('/api/points/spin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ game_id: pointsState.gameId, player_id: pointsState.playerId })
        })
            .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
            .then(function (res) {
                pointsState.spinning = false;
                if (changeBtn) changeBtn.disabled = false;
                if (!res.data.ok) {
                    if (spinBtn) { spinBtn.disabled = false; spinBtn.textContent = 'GIRAR'; }
                    if (resultEl) {
                        resultEl.textContent = res.data.message || 'No se pudo procesar el giro.';
                        resultEl.classList.add('is-miss');
                        resultEl.style.display = 'block';
                    }
                    return;
                }

                var result = res.data.result;
                var statusEl = pointsEl('pointsStatusMsg');
                pointsEl('pointsBalanceValue').textContent = result.points_balance;
                if (statusEl) statusEl.textContent = 'Abriendo caja...';
                spinPointsStripTo(result.won);

                window.setTimeout(function () {
                    if (spinBtn) {
                        spinBtn.disabled = result.points_balance < pointsState.spinCost;
                        spinBtn.textContent = 'GIRAR';
                    }
                    if (statusEl) {
                        statusEl.innerHTML = 'Pulsa <b>GIRAR</b> para abrir tu caja ('
                            + pointsState.spinCost + ' puntos).';
                    }
                    var gameLabel = pointsEl('pointsSpinGameLabel');
                    showPointsResultModal(
                        result.won,
                        result.reward_label,
                        gameLabel ? gameLabel.textContent : ''
                    );
                    // Justo cuando la tira frena, para que el modal no se
                    // adelante al resultado que se ve bajo el láser.
                }, pointsSpinMs());
            })
            .catch(function () {
                pointsState.spinning = false;
                if (spinBtn) { spinBtn.disabled = false; spinBtn.textContent = 'GIRAR'; }
                if (changeBtn) changeBtn.disabled = false;
                if (resultEl) {
                    resultEl.textContent = 'Error de conexión. Intenta de nuevo.';
                    resultEl.classList.add('is-miss');
                    resultEl.style.display = 'block';
                }
            });
    }

    function handlePointsRedeemSubmit() {
        if (pointsState.redeemBusy || !pointsState.gameId || !pointsState.playerId || !pointsState.selectedRedeemOptionId) return;

        var btn = pointsEl('pointsRedeemBtn');
        var backBtn = pointsEl('pointsRedeemBackBtn');
        var errEl = pointsEl('pointsRedeemError');
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }

        pointsState.redeemBusy = true;
        if (btn) { btn.disabled = true; btn.textContent = 'CANJEANDO...'; }
        if (backBtn) backBtn.disabled = true;

        fetch('/api/points/redeem', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                game_id: pointsState.gameId,
                player_id: pointsState.playerId,
                option_id: pointsState.selectedRedeemOptionId
            })
        })
            .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
            .then(function (res) {
                pointsState.redeemBusy = false;
                if (backBtn) backBtn.disabled = false;
                if (btn) { btn.disabled = true; btn.textContent = 'CANJEAR'; }

                if (!res.data.ok) {
                    if (errEl) {
                        errEl.textContent = res.data.message || 'No se pudo procesar el canje.';
                        errEl.style.display = 'block';
                    }
                    return;
                }

                var result = res.data.result;
                pointsEl('pointsRedeemBalanceValue').textContent = result.points_balance;
                // El saldo guardado en memoria también tiene que quedar al
                // día: es lo que reusan el paso de elección y "Volver" para
                // no tener que volver a consultar el servidor, y si se queda
                // con el saldo de antes del canje, "Volver" lo muestra viejo.
                if (pointsState.lastBalanceData) pointsState.lastBalanceData.balance = result.points_balance;

                var gameLabel = pointsEl('pointsRedeemGameLabel');
                showPointsResultModal(true, result.package_name, gameLabel ? gameLabel.textContent : '', true);

                // Se vuelve a pedir la lista: ya gastó el canje del día, así
                // que todas las opciones deben verse deshabilitadas ahora.
                fetch('/api/points/redeem-options?game_id=' + encodeURIComponent(pointsState.gameId))
                    .then(function (r) { return r.json(); })
                    .then(function (optRes) {
                        renderPointsRedeemOptions((optRes && optRes.options) || [], 0);
                    })
                    .catch(function () {});
            })
            .catch(function () {
                pointsState.redeemBusy = false;
                if (backBtn) backBtn.disabled = false;
                if (btn) { btn.disabled = false; btn.textContent = 'CANJEAR'; }
                if (errEl) {
                    errEl.textContent = 'Error de conexión. Intenta de nuevo.';
                    errEl.style.display = 'block';
                }
            });
    }

    function getRememberedContact() {
        try {
            var raw = localStorage.getItem(rememberedContactKey);
            return raw ? JSON.parse(raw) : null;
        } catch (_) {
            return null;
        }
    }

    function saveRememberedContact() {
        if (!rememberDataInput) return;
        syncPhoneHiddenValue();

        if (!rememberDataInput.checked) {
            try { localStorage.removeItem(rememberedContactKey); } catch (_) {}
            return;
        }

        var payload = {
            email: contactEmailInput ? String(contactEmailInput.value || '').trim() : '',
            phone: phoneHiddenInput ? String(phoneHiddenInput.value || '').trim() : ''
        };

        try {
            localStorage.setItem(rememberedContactKey, JSON.stringify(payload));
        } catch (_) {}
    }

    function applyContactPrefill(data, shouldCheckRemember) {
        if (!data) return;

        if (contactEmailInput && !contactEmailInput.value && data.email) {
            contactEmailInput.value = data.email;
        }

        if (data.phone && phoneCountryCodeInput && phoneLocalInput) {
            var parts = splitPhoneValue(data.phone);
            phoneCountryCodeInput.value = parts.countryCode;
            phoneLocalInput.value = parts.localNumber;
        }

        if (rememberDataInput && shouldCheckRemember) {
            rememberDataInput.checked = true;
        }

        updatePhoneCountryDisplay();
        syncPhoneHiddenValue();
    }

    function initRememberedContact() {
        if (phoneCountryCodeInput && !phoneCountryCodeInput.value) {
            phoneCountryCodeInput.value = '+58';
        }

        applyContactPrefill(window.CONTACT_PREFILL || null, false);

        var remembered = getRememberedContact();
        if (remembered) {
            applyContactPrefill(remembered, true);
        }

        if (phoneCountryCodeInput) {
            phoneCountryCodeInput.addEventListener('change', function () {
                updatePhoneCountryDisplay();
                syncPhoneHiddenValue();
            });
        }

        if (phoneLocalInput) {
            phoneLocalInput.addEventListener('input', function () {
                this.value = String(this.value || '').replace(/[^\d]/g, '');
                syncPhoneHiddenValue();
            });
        }

        if (contactEmailInput) {
            contactEmailInput.addEventListener('input', function () {
                if (rememberDataInput && rememberDataInput.checked) {
                    saveRememberedContact();
                }
            });
        }

        if (rememberDataInput) {
            rememberDataInput.addEventListener('change', saveRememberedContact);
        }

        updatePhoneCountryDisplay();
        syncPhoneHiddenValue();
    }

    /* ── Enlace directo al juego ──────────────────────────────
       Al elegir un producto la barra de direcciones pasa a /?juego=<slug>:
       se copia de ahí como cualquier enlace y el que la abra entra con ese
       juego ya seleccionado. También hace que recargar no pierda la
       selección. replaceState y no pushState: cada tarjeta tocada no debe
       convertirse en un paso más del botón "atrás" del teléfono. */
    function gameShareUrl(slug) {
        var url = new URL(window.location.href);
        // El juego ya dice a qué categoría pertenece; el ?cat= solo estorba.
        url.searchParams.delete('cat');
        if (slug) {
            url.searchParams.set('juego', slug);
        } else {
            url.searchParams.delete('juego');
        }
        return url.toString();
    }

    function updateCategoryUrl(slug) {
        // Sin juego abierto, lo que vale la pena conservar en el enlace es
        // la pestaña de categoría que se está mirando.
        try {
            var url = new URL(window.location.href);
            url.searchParams.delete('juego');
            if (slug && slug !== 'juegos') {
                url.searchParams.set('cat', slug);
            } else {
                url.searchParams.delete('cat');
            }
            window.history.replaceState(null, '', url.toString());
        } catch (_) {}
    }

    function updateGameUrl(slug) {
        try {
            window.history.replaceState(null, '', gameShareUrl(slug));
        } catch (_) {}
    }

    /* ── Render Game Cards ────────────────────────────────── */
    function renderGames(games) {
        var grid = document.getElementById('gamesGrid');
        grid.innerHTML = '';

        if (!games || games.length === 0) {
            grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">No hay productos en esta categoría aún.</div>';
            updateGamesCarouselNav();
            return;
        }

        games.forEach(function (game) {
            var card = document.createElement('div');
            card.className = 'game-card' + (game.is_automated ? ' is-automated' : '');
            card.dataset.gameId   = game.id;
            card.dataset.gameSlug = game.slug || '';
            card.dataset.gameName = game.name;

            var imgHtml = game.image
                ? '<img src="/static/uploads/' + escHtml(game.image) + '" alt="' + escHtml(game.name) + '" loading="lazy">'
                : '<div class="game-img-placeholder"><span>' + escHtml(game.name.charAt(0).toUpperCase()) + '</span></div>';

            card.innerHTML =
                '<div class="game-img-wrapper">' + imgHtml + '</div>' +
                '<div class="game-title">' + escHtml(game.name) + '</div>';

            card.addEventListener('click', function () { handleGameClick(card); });
            grid.appendChild(card);
        });

        updateGamesCarouselNav();
    }

    /* ── Handle Game Card Click ───────────────────────────── */
    function handleGameClick(card) {
        var gameId = parseInt(card.dataset.gameId);

        if (activeGameId === gameId) {
            closePackages();
            return;
        }

        document.querySelectorAll('.game-card').forEach(function (c) {
            c.classList.remove('active');
        });
        card.classList.add('active');
        activeGameId = gameId;
        activeGameSlug = card.dataset.gameSlug || '';
        updateGameUrl(activeGameSlug);

        showPackagesPanel(card, card.dataset.gameName);
        fetchPackages(gameId);
    }

    /* ── Insert & Show Packages Panel Below the Row ───────── */
    function showPackagesPanel(clickedCard, gameName) {
        var panel = document.getElementById('packagesPanel');
        var section = document.getElementById('gamesSection');
        var host = document.getElementById('packagesPanelHost');

        if (!panel) {
            panel = document.createElement('div');
            panel.id = 'packagesPanel';
            panel.className = 'packages-panel';
            panel.style.display = 'none';
            panel.innerHTML = 
                '<div class="packages-panel-header">' +
                    '<div class="panel-title-row">' +
                        '<span class="panel-game-icon" id="panelGameIcon"></span>' +
                        '<h3 id="packagesPanelTitle"></h3>' +
                    '</div>' +
                    '<button class="close-packages-btn" onclick="closePackages()" aria-label="Cerrar">✕</button>' +
                '</div>' +
                '<p class="panel-hint">Selecciona un paquete para continuar</p>' +
                '<div class="packages-grid" id="packagesGrid">' +
                    '<div class="pkg-loading"><div class="spinner"></div></div>' +
                '</div>';
        }

        var titleEl = document.getElementById('packagesPanelTitle');
        if (titleEl) {
            titleEl.textContent = gameName;
        }

        var gridEl = document.getElementById('packagesGrid');
        if (gridEl) {
            gridEl.innerHTML = '<div class="pkg-loading"><div class="spinner"></div></div>';
        }

        if (host) {
            if (panel.parentNode !== host) {
                host.appendChild(panel);
            }
        } else {
            if (section && panel.parentNode !== section) {
                section.appendChild(panel);
            }

            var carousel = document.querySelector('.games-carousel');
            if (section && carousel && carousel.nextSibling !== panel) {
                section.insertBefore(panel, carousel.nextSibling);
            }
        }

        panel.style.display = 'block';
    }

    /* ── Fetch Packages via AJAX ──────────────────────────── */
    /* ¿Lo que trae el servidor difiere de lo que la página tiene en memoria?
       Se miran la tasa, la tasa propia del juego y el paquete elegido. */
    function catalogChanged(data) {
        if (typeof data.usd_rate_bs === 'number' && data.usd_rate_bs > 0 && Math.abs(data.usd_rate_bs - usdRate) > 0.0001) return true;
        var oldOverride = (currentGame && currentGame.bs_rate_override != null) ? String(currentGame.bs_rate_override) : '';
        var newOverride = (data.game && data.game.bs_rate_override != null) ? String(data.game.bs_rate_override) : '';
        if (parseFloat(oldOverride || '0') !== parseFloat(newOverride || '0')) return true;
        if (selectedPackage) {
            var fresh = (data.packages || []).filter(function (p) { return String(p.id) === String(selectedPackage.id); })[0];
            if (!fresh) return true;
            if (String(fresh.price) !== String(selectedPackage.price)) return true;
            if (String(fresh.usd_price || '') !== String(selectedPackage.usd_price || '')) return true;
            if (String(fresh.bs_price || '') !== String(selectedPackage.bs_price || '')) return true;
            if (!!fresh.out_of_stock !== !!selectedPackage.out_of_stock || !!fresh.closed_now !== !!selectedPackage.closed_now) return true;
        }
        return false;
    }

    function fetchPackages(gameId, opts) {
        opts = opts || {};
        var seq = ++packagesRequestSeq;
        var keepSelection = !!opts.keepSelection;
        var quiet = !!opts.quiet;   // solo repintar si algo cambió
        var previousPackageId = (keepSelection && selectedPackage) ? String(selectedPackage.id) : null;
        var previousTotal = lastShownTotal ? (lastShownTotal.currency + ':' + lastShownTotal.amount) : null;
        catalogRefreshing = true;
        window.nxDebug('Fetching packages for gameId:', gameId);
        fetch('/api/packages/' + gameId, { cache: 'no-store', credentials: 'same-origin', headers: storeHeaders() })
            .then(function (r) {
                window.nxDebug('Response status:', r.status);
                return r.json();
            })
            .then(function (data) {
                // Llegó tarde: el cliente ya cambió de juego.
                if (seq !== packagesRequestSeq || gameId !== activeGameId) return;
                window.nxDebug('Packages data:', data);
                if (data && data.page_expired) {
                    // Esta página es de otro día: se recarga y listo. Si ya
                    // se recargó hace nada, se pinta lo que vino (la tarjeta).
                    if (safeAutoReload('page_expired')) return;
                }
                if (quiet && !catalogChanged(data)) {
                    catalogLoadedAt = Date.now();
                    return;   // todo igual: no se toca la pantalla
                }
                if (typeof data.usd_rate_bs === 'number' && data.usd_rate_bs > 0) {
                    usdRate = data.usd_rate_bs;
                }
                catalogLoadedAt = Date.now();
                manualSchedule = (data.game && data.game.manual_schedule) || null;
                applyGameToSidebar(data.game);
                if (packageSections.gameId !== gameId) { packageSections.gameId = gameId; packageSections.active = null; }
                renderPackages(data.packages);
                if (previousPackageId) {
                    reselectPackage(previousPackageId, previousTotal);
                }
                applyDeepLink();
                if (data.game && data.game.requires_manual_login_popup) {
                    openManualInfoPopup();
                } else {
                    closeManualInfoPopup();
                }
                if (data.game && data.game.show_selection_popup) {
                    openGameSelectionPopup();
                } else {
                    closeGameSelectionPopup();
                }
                if (data.game && data.game.requires_wallet_notice) {
                    showWalletNote(data.game.manual_schedule);
                } else {
                    hideWalletNote();
                }
            })
            .catch(function (err) {
                if (seq !== packagesRequestSeq) return;
                console.error('Error fetching packages:', err);
                if (keepSelection) return;   // el refresco silencioso no borra lo que ya se ve
                document.getElementById('packagesGrid').innerHTML =
                    '<div class="empty-state" style="grid-column:1/-1">Error al cargar paquetes.</div>';
            })
            .finally(function () {
                if (seq === packagesRequestSeq) catalogRefreshing = false;
            });
    }

    /* Tras refrescar el catálogo, vuelve a dejar marcado el paquete que el
       cliente tenía elegido (el click real hace que index.html repinte
       método, datos de pago y monto) y avisa si el total cambió. */
    /* Enlace de compra del chat de soporte (/?juego=…&paquete=…&jugador=…):
       elige el paquete y escribe el ID, una sola vez. Los valores ya vienen
       validados del servidor. El ID se pone aunque haya uno recordado: es el
       que el cliente acaba de escribir en el chat. */
    function applyDeepLink() {
        var packageId = window.PRESELECT_PACKAGE;
        if (!packageId) return;
        window.PRESELECT_PACKAGE = null;   // solo la primera vez: después manda el cliente
        var item = findPackageItem(packageId);
        if (item && !item.disabled) item.click();
        [['playerId', window.PRESELECT_PLAYER_ID], ['zoneId', window.PRESELECT_ZONE_ID]].forEach(function (pair) {
            var input = document.getElementById(pair[0]);
            if (!input || !pair[1]) return;
            input.value = pair[1];
            input.dispatchEvent(new Event('input', { bubbles: true }));
        });
    }

    function reselectPackage(packageId, previousTotal) {
        var item = findPackageItem(packageId);
        if (!item || item.disabled) {
            notifyCatalogChange('Este paquete ya no está disponible. Elige otro para continuar.');
            return;
        }
        item.click();
        var nowTotal = lastShownTotal ? (lastShownTotal.currency + ':' + lastShownTotal.amount) : null;
        if (previousTotal && nowTotal && previousTotal !== nowTotal) {
            showPriceUpdateBanner('Los precios se actualizaron. El monto a pagar ahora es ' + formatShownTotal(lastShownTotal) + '.');
        }
    }

    /* Aviso fijo dentro del formulario de compra, con botón "Actualizar"
       que recarga la página. El monto ya quedó corregido en pantalla; el
       botón es para que el cliente lo confirme con la página limpia. */
    function showPriceUpdateBanner(message) {
        var form = document.getElementById('quickCheckoutForm');
        if (!form) { notifyCatalogChange('⚠️ ' + message); return; }
        var banner = document.getElementById('nxPriceUpdateBanner');
        if (!banner) {
            banner = document.createElement('div');
            banner.id = 'nxPriceUpdateBanner';
            banner.setAttribute('role', 'status');
            banner.style.cssText = 'display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:0 0 12px;padding:10px 12px;border-radius:12px;' +
                'background:rgba(255,196,0,.12);border:1px solid rgba(255,196,0,.55);color:#ffd766;font-size:13px;line-height:1.35;';
            var text = document.createElement('span');
            text.className = 'nx-price-update-text';
            text.style.cssText = 'flex:1 1 180px;';
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.textContent = 'Actualizar';
            btn.style.cssText = 'flex:0 0 auto;padding:8px 14px;border-radius:10px;border:0;font-weight:700;cursor:pointer;background:#ffc400;color:#1a1400;';
            btn.addEventListener('click', function () { window.location.reload(); });
            banner.appendChild(text);
            banner.appendChild(btn);
            form.insertBefore(banner, form.firstChild);
        }
        banner.querySelector('.nx-price-update-text').textContent = '⚠️ ' + message;
        banner.style.display = 'flex';
        try { banner.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (_) {}
    }

    function formatShownTotal(total) {
        if (!total) return '';
        if (total.currency === 'usd') return '$' + Number(total.amount).toFixed(2);
        return 'Bs ' + Math.round(Number(total.amount)).toLocaleString('es-VE');
    }

    function notifyCatalogChange(message) {
        if (typeof window.nxShowToast === 'function') {
            window.nxShowToast(message, 9000);
        } else {
            console.warn(message);
        }
    }

    var selectCheckTimer = null;
    function checkCatalogOnSelect() {
        if (!activeGameId) return;
        // La reselección tras un refresco también dispara click: no encadenar.
        if (catalogRefreshing) return;
        if (selectCheckTimer) clearTimeout(selectCheckTimer);
        selectCheckTimer = setTimeout(function () {
            selectCheckTimer = null;
            if (!activeGameId || catalogRefreshing) return;
            fetchPackages(activeGameId, { keepSelection: true, quiet: true });
        }, 50);
    }

    /* Vuelve a pedir paquetes + tasa si llevan un rato cargados. */
    function refreshCatalog(force, maxAgeMs) {
        if (!activeGameId || catalogRefreshing) return;
        var maxAge = (typeof maxAgeMs === 'number') ? maxAgeMs : CATALOG_STALE_MS;
        if (!force && catalogLoadedAt && (Date.now() - catalogLoadedAt) < maxAge) return;
        fetchPackages(activeGameId, { keepSelection: true });
    }

    /* Repinta las tarjetas de juego de la categoría activa sin tocar el
       panel de paquetes ni la selección: sirve para que una imagen o un
       juego nuevo aparezcan sin recargar la página. */
    function refreshGames() {
        var grid = document.getElementById('gamesGrid');
        if (!grid) return;
        var scrollBefore = gamesViewportEl ? gamesViewportEl.scrollLeft : 0;
        var seq = ++gamesRequestSeq;
        fetch('/api/games?category=' + encodeURIComponent(activeCategory), { cache: 'no-store', credentials: 'same-origin', headers: storeHeaders() })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (seq !== gamesRequestSeq) return;   // el cliente ya cambió de categoría: esta respuesta es vieja
                if (!data || !Array.isArray(data.games) || !data.games.length) return;
                renderGames(data.games);
                if (activeGameId) {
                    var activeCard = grid.querySelector('.game-card[data-game-id="' + activeGameId + '"]');
                    if (activeCard) activeCard.classList.add('active');
                }
                if (gamesViewportEl) gamesViewportEl.scrollLeft = scrollBefore;
            })
            .catch(function () { /* se queda lo que ya se ve */ });
    }

    /* Refresco en silencio de todo lo que pinta main.js: juegos, paquetes
       y tasa. Lo llama version-watch.js cuando cambia el sello del
       catálogo (el admin tocó precios, imágenes, paquetes o la tasa). */
    function softRefreshCatalog() {
        refreshGames();
        refreshCatalog(true);
    }

    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') refreshCatalog(false);
    });
    window.addEventListener('pageshow', function (ev) {
        if (ev.persisted) refreshCatalog(true);
    });
    setInterval(function () {
        if (document.visibilityState === 'visible') refreshCatalog(false);
    }, CATALOG_POLL_MS);

    // index.html los usa: manda el monto visto al servidor y refresca si
    // el servidor responde que el precio cambió.
    window.nxShownTotal = function () { return lastShownTotal; };
    window.nxRefreshCatalog = refreshCatalog;
    window.nxSoftRefresh = softRefreshCatalog;

    /* Texto del aviso de fuera de horario, con las horas que configuró el
       admin (y un respaldo por si el servidor no las mandó). */
    function getClosedNoticeText() {
        if (manualSchedule && manualSchedule.force_closed) {
            return 'Estas recargas se hacen a mano y ahora mismo no hay nadie disponible para procesarlas. Vuelve a intentarlo más tarde.';
        }
        var openLabel = (manualSchedule && manualSchedule.open_label) || '5:00 a. m.';
        var closeLabel = (manualSchedule && manualSchedule.close_label) || '10:00 p. m.';
        return 'Estas recargas se hacen a mano y ahora estamos cerrados. ' +
               'Atendemos de ' + openLabel + ' a ' + closeLabel + ', hora de Venezuela.';
    }

    /* ── Render Package Items ─────────────────────────────── */
    /* ── Apartados de los paquetes: Moneda principal / Pases / Tarjetas ──────────────────────────────────
       El dueño elige en el admin en qué apartado va cada paquete. Si el juego tiene paquetes en más de uno,
       arriba de la lista sale una barrita de cristal (como la de Juegos / Tarjetas / Wallet, más chica) con un
       cuadro que se desliza; si solo tiene uno, no sale nada y todo se ve como siempre. */
    var SECTION_ORDER = ['main', 'pass', 'card'];
    var SECTION_LABELS = { main: 'Moneda', pass: 'Pases', card: 'Tarjetas' };
    var SECTION_TITLES = { main: 'Moneda principal', pass: 'Pases', card: 'Tarjetas' };
    var packageSections = { all: [], active: null, gameId: null, present: [] };

    function pkgSection(pkg) {
        var s = pkg && pkg.section;
        return SECTION_ORDER.indexOf(s) >= 0 ? s : 'main';
    }

    function placeSectionSlider(animate) {
        var bar = document.getElementById('pkgSections');
        if (!bar || bar.hidden) return;
        var slider = bar.querySelector('.pkg-sec-slider');
        var active = bar.querySelector('.pkg-sec-btn.is-active');
        if (!slider || !active) return;
        if (!animate) bar.classList.remove('is-ready');
        var b = bar.getBoundingClientRect();
        var a = active.getBoundingClientRect();
        slider.style.width = a.width + 'px';
        slider.style.height = a.height + 'px';
        slider.style.transform = 'translate(' + (a.left - b.left - bar.clientLeft) + 'px, ' + (a.top - b.top - bar.clientTop) + 'px)';
        if (!animate) {
            void slider.offsetWidth;
            requestAnimationFrame(function () { bar.classList.add('is-ready'); });
        }
    }

    function drawSectionTabs() {
        var bar = document.getElementById('pkgSections');
        if (!bar) return;
        var present = packageSections.present;
        bar.innerHTML = '';
        bar.classList.remove('is-ready');
        if (present.length < 2) { bar.hidden = true; return; }
        bar.hidden = false;
        var slider = document.createElement('span');
        slider.className = 'pkg-sec-slider';
        slider.setAttribute('aria-hidden', 'true');
        bar.appendChild(slider);
        present.forEach(function (section) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'pkg-sec-btn' + (section === packageSections.active ? ' is-active' : '');
            btn.dataset.section = section;
            btn.setAttribute('role', 'tab');
            btn.setAttribute('aria-selected', section === packageSections.active ? 'true' : 'false');
            btn.title = SECTION_TITLES[section];
            btn.textContent = SECTION_LABELS[section];
            btn.addEventListener('click', function () { showSection(section); });
            bar.appendChild(btn);
        });
        placeSectionSlider(false);
        watchSectionBar();
    }

    function packagesOfSection(section) {
        if (packageSections.present.length < 2) return packageSections.all;
        return packageSections.all.filter(function (p) { return pkgSection(p) === section; });
    }

    function showSection(section) {
        if (section === packageSections.active || packageSections.present.indexOf(section) < 0) return;
        packageSections.active = section;
        var bar = document.getElementById('pkgSections');
        if (bar) {
            Array.prototype.forEach.call(bar.querySelectorAll('.pkg-sec-btn'), function (btn) {
                var on = btn.dataset.section === section;
                btn.classList.toggle('is-active', on);
                btn.setAttribute('aria-selected', on ? 'true' : 'false');
            });
            placeSectionSlider(true);
        }
        paintPackages(packagesOfSection(section));   // al cambiar de apartado se limpia la selección, como al cambiar de juego
    }

    /* Busca la tarjeta de un paquete; si está en otro apartado, abre ese apartado primero. */
    function findPackageItem(packageId) {
        var selector = '.package-item[data-package-id="' + packageId + '"]';
        var item = document.querySelector(selector);
        if (item) return item;
        var pkg = packageSections.all.filter(function (p) { return String(p.id) === String(packageId); })[0];
        if (pkg && pkgSection(pkg) !== packageSections.active) {
            showSection(pkgSection(pkg));
            return document.querySelector(selector);
        }
        return null;
    }

    window.addEventListener('resize', function () { placeSectionSlider(false); });

    /* Si cambia el ancho de la barra (cargó la tipografía, o el nombre elegido se pone más grueso), el cuadro de cristal se reajusta. */
    var sectionBarWatcher = null;
    function watchSectionBar() {
        var bar = document.getElementById('pkgSections');
        if (!bar) return;
        if (!sectionBarWatcher && window.ResizeObserver) {
            sectionBarWatcher = new ResizeObserver(function () { placeSectionSlider(true); });
            sectionBarWatcher.observe(bar);
        }
        if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { placeSectionSlider(false); });
    }

    function renderPackages(packages) {
        packageSections.all = packages || [];
        packageSections.present = SECTION_ORDER.filter(function (s) {
            return packageSections.all.some(function (p) { return pkgSection(p) === s; });
        });
        if (packageSections.present.indexOf(packageSections.active) < 0) packageSections.active = packageSections.present[0] || null;
        drawSectionTabs();
        paintPackages(packagesOfSection(packageSections.active));
    }

    function paintPackages(packages) {
        var grid = document.getElementById('packagesGrid');
        grid.innerHTML = '';

        if (!packages || packages.length === 0) {
            grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">No hay paquetes disponibles.</div>';
            selectedPackage = null;
            updateSidebarForPackage(null);
            return;
        }

        var autoPkgs   = packages.filter(function (p) { return p.is_auto; });
        var manualPkgs = packages.filter(function (p) { return !p.is_auto; });

        function getPackageUsdPrice(pkg) {
            var exclusiveUsd = parseFloat(pkg.usd_price);
            if (!isNaN(exclusiveUsd)) {
                return exclusiveUsd;
            }
            return parseFloat(pkg.price);
        }

        function buildItem(pkg) {
            var item = document.createElement('button');
            item.type      = 'button';
            item.className = 'package-item';
            item.dataset.packageId = pkg.id;

            var imgHtml = pkg.image
                ? '<img src="/static/uploads/' + escHtml(pkg.image) + '" alt="' + escHtml(pkg.name) + '">'
                : '<div class="pkg-img-placeholder">' + escHtml(pkg.name.charAt(0).toUpperCase()) + '</div>';

            var priceUsd = getPackageUsdPrice(pkg);
            item.dataset.priceUsd = String(priceUsd);
            var exclusiveBs = parseFloat(pkg.bs_price);
            item.dataset.priceBs = isNaN(exclusiveBs) ? '' : String(exclusiveBs);
            item.dataset.priceBase = String(parseFloat(pkg.price));

            // Los puntos que deja la compra. El backend ya manda 0 en lo que
            // no otorga puntos (tarjetas y wallet), asi que aqui basta con no
            // pintar el badge cuando no hay nada que prometer.
            var pkgPoints = parseInt(pkg.points, 10);
            var pointsHtml = (!isNaN(pkgPoints) && pkgPoints > 0)
                ? '<span class="pkg-points">+' + pkgPoints + ' pts</span>'
                : '';

            var outOfStock = !!pkg.out_of_stock;
            // El paquete es manual y estamos fuera del horario de atención.
            // Agotado manda sobre cerrado: si no hay stock, da igual la hora.
            var closedNow = !outOfStock && !!pkg.closed_now;
            var badge = '';
            if (outOfStock) {
                badge = '<span class="pkg-stock-badge">Agotado</span>';
            } else if (closedNow) {
                badge = '<span class="pkg-stock-badge is-closed">Cerrado</span>';
            }

            item.innerHTML =
                imgHtml +
                badge +
                '<div class="pkg-info">' +
                    '<h4>' + escHtml(pkg.name) + '</h4>' +
                    '<span class="price"></span>' +
                    '<span class="price-usd"></span>' +
                    pointsHtml +
                '</div>';

            if (outOfStock || closedNow) {
                item.classList.add('is-out-of-stock');
                if (closedNow) item.classList.add('is-closed-now');
                item.disabled = true;
                item.setAttribute('aria-disabled', 'true');
                item.title = closedNow ? getClosedNoticeText() : 'Agotado por ahora';
                // El botón ya está disabled, así que el click no dispara nada:
                // el CSS no necesita pointer-events:none y así el cursor
                // not-allowed sí llega a verse al pasar por encima.
                item.addEventListener('click', function (evt) {
                    evt.preventDefault();
                });
            } else {
                item.addEventListener('click', function () {
                    selectPackage(pkg, item);
                    // Cada selección consulta el servidor (como la página de
                    // checkout de Inefablestore): si tasa o precio cambiaron,
                    // se repinta y sale el aviso con "Actualizar".
                    checkCatalogOnSelect();
                });
            }
            return item;
        }

        function addSectionLabel(text, sectionType) {
            var lbl = document.createElement('div');
            lbl.className = 'pkg-section-label';
            if (sectionType === 'manual') {
                lbl.innerHTML = '<span>' + escHtml(text) + '</span><button type="button" class="pkg-section-help" id="manualInfoTrigger" aria-label="Información sobre recarga manual">?</button>';
            } else {
                lbl.textContent = text;
            }
            grid.appendChild(lbl);
        }

        if (autoPkgs.length > 0) {
            autoPkgs.forEach(function (pkg) { grid.appendChild(buildItem(pkg)); });
        }

        if (manualPkgs.length > 0) {
            if (autoPkgs.length > 0) {
                var sep = document.createElement('div');
                sep.className = 'pkg-section-sep';
                grid.appendChild(sep);
            }
            // Fuera de horario, un aviso arriba del bloque explica por qué
            // estos paquetes están apagados: sin esto solo se ve el gris.
            if (manualPkgs.some(function (pkg) { return !!pkg.closed_now; })) {
                var notice = document.createElement('div');
                notice.className = 'pkg-closed-notice';
                notice.textContent = getClosedNoticeText();
                grid.appendChild(notice);
            }
            manualPkgs.forEach(function (pkg) { grid.appendChild(buildItem(pkg)); });
        }

        // Reset any previous selection when loading new packages
        selectedPackage = null;
        updateSidebarForPackage(null);
        refreshPackagePriceViews();
    }

    /* ── Player ID Verification (replicated from Inefablestore) ── */
    var verifyState = {
        verifying: false,
        verifiedNick: '',
        lastUidRequested: '',
        lastUidVerified: '',
        inflightController: null,
        verifyTimer: null,
        requestSeq: 0,
        scrapeEnabled: false,
        isFFVerify: false,
        isBSVerify: false,
        gameId: null
    };

    function verifyCacheKey(uid) {
        return 'ffnick:' + String(uid || '').trim();
    }
    function getVerifyCachedNick(uid) {
        try { return (localStorage.getItem(verifyCacheKey(uid)) || '').toString().trim(); } catch (_) { return ''; }
    }
    function setVerifyCachedNick(uid, nick) {
        try { localStorage.setItem(verifyCacheKey(uid), (nick || '').toString()); } catch (_) {}
    }

    function setNickUIOk(nick) {
        verifyState.verifiedNick = nick || '';
        var el = document.getElementById('playerNickname');
        var btn = document.getElementById('btnVerifyPlayer');
        var hidden = document.getElementById('playerNicknameHidden');
        if (hidden) hidden.value = nick || '';
        if (!el) return;
        if (!nick) return;
        el.style.color = '#22c55e';
        el.textContent = 'Nick: ' + nick;
        el.style.display = 'block';
        if (btn) {
            btn.textContent = 'Verificado';
            btn.disabled = true;
        }
    }
    function setNickUILoading() {
        var el = document.getElementById('playerNickname');
        var btn = document.getElementById('btnVerifyPlayer');
        if (el) {
            el.style.color = '#a3a3a3';
            el.textContent = 'Verificando...';
            el.style.display = 'block';
        }
        if (btn) {
            btn.textContent = 'Verificando...';
            btn.disabled = true;
        }
    }
    function setNickUIErr(msg) {
        verifyState.verifiedNick = '';
        var el = document.getElementById('playerNickname');
        var btn = document.getElementById('btnVerifyPlayer');
        if (el) {
            el.style.color = '#fca5a5';
            el.textContent = msg || 'No se pudo verificar';
            el.style.display = 'block';
        }
        if (btn) {
            // Habilitado a propósito: a veces el ID todavía no está indexado
            // del lado de FFMania (cuentas nuevas o poco consultadas) y basta
            // con reintentar en unos segundos. Antes quedaba deshabilitado
            // mostrando "Revisar ID" sin que el clic hiciera nada.
            btn.textContent = 'Reintentar';
            btn.disabled = false;
        }
    }
    function resetNickUI() {
        verifyState.verifiedNick = '';
        verifyState.lastUidVerified = '';
        var el = document.getElementById('playerNickname');
        var btn = document.getElementById('btnVerifyPlayer');
        var hidden = document.getElementById('playerNicknameHidden');
        if (hidden) hidden.value = '';
        if (el) { el.textContent = ''; el.style.display = 'none'; }
        if (btn) { btn.textContent = 'Esperando ID'; btn.disabled = true; }
    }

    function scheduleAutoVerify(delayMs, silent) {
        var input = document.getElementById('playerId');
        if (!input) return;
        if (input.dataset.digitsOnly !== '1') {
            if (verifyState.verifyTimer) {
                clearTimeout(verifyState.verifyTimer);
                verifyState.verifyTimer = null;
            }
            return;
        }
        var uid = (input.value || '').trim();
        if (verifyState.verifyTimer) {
            clearTimeout(verifyState.verifyTimer);
            verifyState.verifyTimer = null;
        }
        if (!uid) {
            if (verifyState.inflightController) {
                try { verifyState.inflightController.abort(); } catch (_) {}
                verifyState.inflightController = null;
            }
            resetNickUI();
            return;
        }
        verifyState.verifyTimer = setTimeout(function() {
            doVerifyPlayer({ silent: !!silent });
        }, delayMs);
    }

    function doVerifyPlayer(opts) {
        var silent = !!(opts && opts.silent);
        var input = document.getElementById('playerId');
        if (!input) return;
        if (input.dataset.digitsOnly !== '1') {
            return;
        }
        var uid = (input.value || '').trim();
        if (!uid) { if (!silent) setNickUIErr('Ingresa tu ID'); return; }
        if (input.dataset.digitsOnly === '1' && !/^\d+$/.test(uid)) { if (!silent) setNickUIErr('El ID debe ser numérico'); return; }
        if (uid === verifyState.lastUidRequested && verifyState.verifying) return;

        if (uid === verifyState.lastUidVerified) {
            var n0 = getVerifyCachedNick(uid);
            if (n0) { setNickUIOk(n0); return; }
        }
        var cached = getVerifyCachedNick(uid);
        if (cached) {
            verifyState.lastUidVerified = uid;
            setNickUIOk(cached);
            return;
        }

        if (verifyState.inflightController) {
            try { verifyState.inflightController.abort(); } catch (_) {}
            verifyState.inflightController = null;
        }
        verifyState.inflightController = new AbortController();
        verifyState.lastUidRequested = uid;
        verifyState.verifying = true;
        verifyState.requestSeq += 1;
        var requestSeq = verifyState.requestSeq;

        setNickUILoading();

        var verifyPath = verifyState.isBSVerify
            ? '/store/player/verify/bloodstrike'
            : '/store/player/verify';
        var url = verifyPath + '?gid=' + encodeURIComponent(verifyState.gameId || '') + '&uid=' + encodeURIComponent(uid);

        fetch(url, { signal: verifyState.inflightController.signal })
            .then(function(res) {
                return res.json().then(function(data) {
                    if (requestSeq !== verifyState.requestSeq || uid !== verifyState.lastUidRequested) return;
                    if (!res.ok || !data || !data.ok) throw new Error((data && data.error) || 'No se pudo verificar');
                    var nick = (data.nick || '').toString().trim();
                    if (!nick) throw new Error('ID no encontrado');
                    setVerifyCachedNick(uid, nick);
                    verifyState.lastUidVerified = uid;
                    setNickUIOk(nick);
                });
            })
            .catch(function(e) {
                if (e && e.name === 'AbortError') return;
                if (requestSeq !== verifyState.requestSeq) return;
                setNickUIErr(window.nxFriendlyError(e, 'No se pudo verificar'));
                setVerifyCachedNick(uid, '');
            })
            .finally(function() {
                if (requestSeq !== verifyState.requestSeq) return;
                verifyState.verifying = false;
            });
    }

    var PLAYER_FIELD_DEFAULT_LABELS = { numeric: 'Player ID', email: 'Correo electrónico', username: 'Nombre de usuario', phone: 'Número de teléfono', text: 'Dato del jugador' };

    // Qué dato se le pide al cliente: numeric (ID), email, username, phone, text, o none (tarjetas). Viene calculado del servidor
    // (también en Wallet); lo de abajo es solo un respaldo para datos viejos.
    function getPlayerInputType(game) {
        if (!game) return 'numeric';
        var kind = String(game.player_field_kind || '').toLowerCase();
        if (['numeric', 'email', 'username', 'phone', 'text', 'none'].indexOf(kind) >= 0) return kind;
        if (game.category_slug === 'wallet') return 'email';
        if (game.category_slug === 'tarjetas') return 'none';

        var inputType = String(game.player_id_input_type || '').toLowerCase();
        return ['numeric', 'email', 'text', 'username', 'phone'].indexOf(inputType) >= 0 ? inputType : 'numeric';
    }

    // El nombre que ve el cliente: el que puso el dueño (o el normal del tipo si no puso uno).
    function getPlayerFieldLabel(game) {
        var label = String((game && (game.player_field_label || game.player_id_label)) || '').trim();
        if (label) return label;
        return PLAYER_FIELD_DEFAULT_LABELS[getPlayerInputType(game)] || 'ID del jugador';
    }

    function getPlayerInputRequiredMessage(game) {
        if (getPlayerInputType(game) === 'email') return 'Por favor ingresa tu correo electrónico.';
        return 'Por favor ingresa tu ' + getPlayerFieldLabel(game) + '.';
    }

    /* ── Aviso de orden con pago incompleto (cualquier juego) ──────────── */
    var pendingCompletionState = { lastChecked: '', timer: null };

    function pendingCompletionDismissKey(orderNumber) {
        return 'nx_pending_completion_ignored_' + orderNumber;
    }

    function isPendingCompletionDismissed(orderNumber) {
        try { return sessionStorage.getItem(pendingCompletionDismissKey(orderNumber)) === '1'; } catch (_) { return false; }
    }

    function markPendingCompletionDismissed(orderNumber) {
        try { sessionStorage.setItem(pendingCompletionDismissKey(orderNumber), '1'); } catch (_) {}
    }

    function showPendingCompletionPopup(data) {
        var popup = document.getElementById('pendingCompletionPopup');
        if (!popup) return;
        var orderNumberEl = document.getElementById('pendingCompletionOrderNumber');
        var packageEl = document.getElementById('pendingCompletionPackage');
        var playerIdEl = document.getElementById('pendingCompletionPlayerId');
        var nicknameWrap = document.getElementById('pendingCompletionNicknameWrap');
        var nicknameEl = document.getElementById('pendingCompletionNickname');
        var missingEl = document.getElementById('pendingCompletionMissing');
        var goBtn = document.getElementById('pendingCompletionGoBtn');
        var ignoreBtn = document.getElementById('pendingCompletionIgnoreBtn');
        if (orderNumberEl) orderNumberEl.textContent = '#' + data.order_number;
        if (packageEl) packageEl.textContent = data.package_name || '';
        if (playerIdEl) playerIdEl.textContent = data.player_id || '';
        if (nicknameWrap && nicknameEl) {
            if (data.player_nickname) {
                nicknameEl.textContent = data.player_nickname;
                nicknameWrap.hidden = false;
            } else {
                nicknameWrap.hidden = true;
            }
        }
        if (missingEl) missingEl.textContent = data.missing_amount || '0';
        if (goBtn) goBtn.onclick = function () { window.location.href = data.order_url; };
        if (ignoreBtn) ignoreBtn.onclick = function () {
            markPendingCompletionDismissed(data.order_number);
            popup.style.display = 'none';
            popup.setAttribute('aria-hidden', 'true');
            document.body.classList.remove('modal-open');
        };
        popup.style.display = 'flex';
        popup.setAttribute('aria-hidden', 'false');
        document.body.classList.add('modal-open');
    }

    function checkPendingCompletion(uid) {
        if (!currentGame || !currentGame.id || !uid) return;
        var key = currentGame.id + ':' + uid;
        if (pendingCompletionState.lastChecked === key) return;
        pendingCompletionState.lastChecked = key;

        fetch('/order/pending-completion?game_id=' + encodeURIComponent(currentGame.id) + '&player_id=' + encodeURIComponent(uid))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.ok || !data.has_pending) return;
                if (isPendingCompletionDismissed(data.order_number)) return;
                showPendingCompletionPopup(data);
            })
            .catch(function () {});
    }

    function schedulePendingCompletionCheck() {
        var input = document.getElementById('playerId');
        if (!input) return;
        if (pendingCompletionState.timer) {
            clearTimeout(pendingCompletionState.timer);
            pendingCompletionState.timer = null;
        }
        var uid = (input.value || '').trim();
        if (!uid) return;
        pendingCompletionState.timer = setTimeout(function () {
            checkPendingCompletion(uid);
        }, 900);
    }

    function setupPendingCompletionListener() {
        var input = document.getElementById('playerId');
        if (!input || input.dataset.pendingCheckBound) return;
        input.addEventListener('input', schedulePendingCompletionCheck);
        input.addEventListener('blur', schedulePendingCompletionCheck);
        input.dataset.pendingCheckBound = '1';
    }

    function setupVerifyListeners() {
        var btn = document.getElementById('btnVerifyPlayer');
        var input = document.getElementById('playerId');

        function sanitizePlayerIdValue(value) {
            return String(value || '').replace(/\D+/g, '');
        }

        if (btn && !btn.dataset.verifyBound) {
            btn.setAttribute('aria-hidden', 'false');
            btn.addEventListener('click', function() { doVerifyPlayer({ silent: false }); });
            btn.dataset.verifyBound = '1';
        }
        if (input && !input.dataset.verifyBound) {
            input.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') { e.preventDefault(); doVerifyPlayer({ silent: false }); }
            });
            input.addEventListener('input', function() {
                if (input.dataset.digitsOnly === '1') {
                    var sanitized = sanitizePlayerIdValue(input.value);
                    if (input.value !== sanitized) {
                        input.value = sanitized;
                    }
                }

                var uid = input.dataset.digitsOnly === '1'
                    ? (input.value || '')
                    : (input.value || '').trim();
                if (!uid) { resetNickUI(); return; }
                resetNickUI();
                refreshRankingLookupIfVisible();
                scheduleAutoVerify(900, false);
            });
            input.addEventListener('change', function() {
                refreshRankingLookupIfVisible();
                scheduleAutoVerify(0, false);
            });
            input.addEventListener('blur', function() {
                var uid = (input.value || '').trim();
                if (!uid || uid === verifyState.lastUidVerified) return;
                refreshRankingLookupIfVisible();
                scheduleAutoVerify(0, false);
            });
            input.dataset.verifyBound = '1';
        }
    }

    /* ── Limpieza al escribir o pegar: el correo y el nombre de usuario no llevan espacios; el teléfono solo números y un + al inicio ── */
    function removeSpaces(value) { return String(value || '').replace(/\s+/g, ''); }

    function cleanPhoneValue(value) {
        var v = String(value || '').replace(/[^\d+]/g, '');
        return (v.charAt(0) === '+' ? '+' : '') + v.replace(/\+/g, '');
    }

    // La función que limpia ese campo, o null si el campo no se limpia.
    function liveCleanerFor(el) {
        if (!el || el.tagName !== 'INPUT') return null;
        if (el.id === 'email') return removeSpaces;
        if (el.id !== 'playerId') return null;
        var kind = el.dataset.playerInputType;
        if (kind === 'username' || kind === 'email') return removeSpaces;
        if (kind === 'phone') return cleanPhoneValue;
        return null;
    }

    function cleanLive(input, cleaner) {
        var value = String(input.value || '');
        var cleaned = cleaner(value);
        if (cleaned === value) return;
        var caret = null;
        try { caret = input.selectionStart; } catch (_) { caret = null; }
        var keep = (caret === null || caret === undefined) ? null : cleaner(value.slice(0, caret)).length;
        input.value = cleaned;
        if (keep !== null) { try { input.setSelectionRange(keep, keep); } catch (_) {} }
    }

    document.addEventListener('keydown', function (evt) {
        if ((evt.key === ' ' || evt.code === 'Space') && liveCleanerFor(evt.target) === removeSpaces) evt.preventDefault();
    });
    document.addEventListener('input', function (evt) {
        var cleaner = liveCleanerFor(evt.target);
        if (cleaner) cleanLive(evt.target, cleaner);
    });

    function updateVerifyUI(game) {
        var btn = document.getElementById('btnVerifyPlayer');
        var nicknameEl = document.getElementById('playerNickname');
        var hasVerify = game && getPlayerInputType(game) === 'numeric' && game.scrape_enabled && (game.is_ff_verify || game.is_bs_verify);

        verifyState.scrapeEnabled = !!(game && game.scrape_enabled);
        verifyState.isFFVerify = !!(game && game.is_ff_verify);
        verifyState.isBSVerify = !!(game && game.is_bs_verify);
        verifyState.gameId = game ? String(game.id) : null;

        if (hasVerify) {
            if (btn) {
                btn.style.display = '';
                btn.textContent = 'Esperando ID';
                btn.disabled = true;
            }
            resetNickUI();
            setupVerifyListeners();
        } else {
            if (verifyState.verifyTimer) {
                clearTimeout(verifyState.verifyTimer);
                verifyState.verifyTimer = null;
            }
            if (verifyState.inflightController) {
                try { verifyState.inflightController.abort(); } catch (_) {}
                verifyState.inflightController = null;
            }
            verifyState.verifying = false;
            verifyState.lastUidRequested = '';
            verifyState.lastUidVerified = '';
            if (btn) btn.style.display = 'none';
            if (nicknameEl) { nicknameEl.textContent = ''; nicknameEl.style.display = 'none'; }
        }
    }

    /* ── Update Sidebar with Game Info ───────────────────── */
    function applyGameToSidebar(game) {
        currentGame = game;
        invalidateRankingLookup();
        setupPendingCompletionListener();
        var sidebarGameName = document.getElementById('sidebarGameName');
        var sidebarTitle = document.getElementById('sidebarTitle');
        
        if (sidebarGameName) {
            sidebarGameName.textContent = 'Selecciona un paquete para continuar.';
        }
        if (sidebarTitle) {
            sidebarTitle.textContent = game.name;
        }

        var isWallet = game.category_slug === 'wallet';
        var playerSection = document.getElementById('playerSection');
        var zoneGroup = document.getElementById('zoneIdGroup');
        var playerIdLabel = document.getElementById('playerIdLabel');
        var zoneIdLabel = document.getElementById('zoneIdLabel');
        var playerHint = document.getElementById('playerHint');
        var playerInput = document.getElementById('playerId');
        var playerInputType = getPlayerInputType(game);

        if (!playerSection) return;

        if (playerInputType === 'none') {
            // Tarjetas: no hay cuadro de ID (el correo de entrega se pide más abajo, en el pago).
            playerSection.style.display = 'none';
        } else {
            playerSection.style.display = 'block';
            var fieldLabel = getPlayerFieldLabel(game);
            if (playerIdLabel) playerIdLabel.textContent = fieldLabel;
            if (playerInput) {
                playerInput.dataset.playerInputType = playerInputType;
                playerInput.removeAttribute('pattern');
                playerInput.removeAttribute('autocapitalize');
                playerInput.removeAttribute('spellcheck');
                playerInput.dataset.digitsOnly = '0';
                if (playerInputType === 'email') {
                    playerInput.type = 'email';
                    playerInput.placeholder = 'correo@ejemplo.com';
                    playerInput.inputMode = 'email';
                    playerInput.value = String(playerInput.value || '').replace(/\s+/g, '');
                } else if (playerInputType === 'username') {
                    playerInput.type = 'text';
                    playerInput.placeholder = 'Ingresa tu usuario';
                    playerInput.inputMode = 'text';
                    playerInput.setAttribute('autocapitalize', 'none');
                    playerInput.setAttribute('spellcheck', 'false');
                    playerInput.value = String(playerInput.value || '').replace(/\s+/g, '');
                } else if (playerInputType === 'phone') {
                    playerInput.type = 'tel';
                    playerInput.placeholder = 'Ej: +584141234567';
                    playerInput.inputMode = 'tel';
                    playerInput.value = cleanPhoneValue(playerInput.value);
                } else if (playerInputType === 'text') {
                    playerInput.type = 'text';
                    playerInput.placeholder = 'Ingresa tu dato';
                    playerInput.inputMode = 'text';
                    playerInput.value = String(playerInput.value || '').trim();
                } else {
                    playerInput.type = 'text';
                    playerInput.placeholder = 'Ingresa tu ID';
                    playerInput.inputMode = 'numeric';
                    playerInput.pattern = '[0-9]*';
                    playerInput.dataset.digitsOnly = '1';
                    playerInput.value = String(playerInput.value || '').replace(/\D+/g, '');
                }
            }
            if (playerHint) {
                if (playerInputType === 'email' && isWallet) {
                    playerHint.textContent = 'Ingresa tu correo electrónico para recibir la recarga.';
                } else if (playerInputType === 'email') {
                    playerHint.textContent = 'Ingresa correctamente tu correo electrónico (sin espacios) para evitar errores en la recarga.';
                } else if (playerInputType === 'username') {
                    playerHint.textContent = 'Ingresa correctamente tu ' + fieldLabel + ' (sin espacios) para evitar errores en la recarga.';
                } else if (playerInputType === 'phone') {
                    playerHint.textContent = 'Ingresa correctamente tu ' + fieldLabel + ' (solo números; puede empezar con +) para evitar errores en la recarga.';
                } else {
                    playerHint.textContent = 'Ingresa correctamente tu ' + fieldLabel + ' para evitar errores en la recarga.';
                }
            }

            if (!isWallet && game.requires_zone_id) {
                if (zoneGroup) zoneGroup.style.display = 'block';
                if (zoneIdLabel) zoneIdLabel.textContent = game.zone_id_label || 'Zona / Región';
            } else {
                if (zoneGroup) zoneGroup.style.display = 'none';
            }
        }

        updateVerifyUI(game);
    }

    /* ── Select Package & bind form ───────────────────────── */
    function selectPackage(pkg, element) {
        selectedPackage = pkg;

        // Visual selection
        document.querySelectorAll('.package-item').forEach(function (el) {
            el.classList.remove('selected');
        });
        if (element) {
            element.classList.add('selected');
        }

        updateSidebarForPackage(pkg);
        maybeShowPackageAnnouncement(pkg);
    }

    function updateSidebarForPackage(pkg) {
        var form = document.getElementById('quickCheckoutForm');
        var submitBtn = document.getElementById('sidebarSubmitBtn');
        var submitLabel = document.getElementById('sidebarSubmitLabel');
        var hiddenInput = document.getElementById('selectedPackageId');

        if (!form || !submitBtn || !submitLabel) return;

        if (!pkg) {
            form.action = '';
            if (hiddenInput) hiddenInput.value = '';
            submitBtn.disabled = true;
            submitLabel.textContent = 'Selecciona un paquete para continuar';
            updateTotals(null);
            return;
        }

        form.action = '/checkout/' + pkg.id;
        if (hiddenInput) hiddenInput.value = String(pkg.id);
        submitBtn.disabled = false;
        var priceNum = getEffectivePackagePrice(getSelectedPackageBasePrice(pkg));
        var currency = getSelectedPaymentCurrency();

        if (currency === 'usd') {
            submitLabel.textContent = 'Comprar — $' + (isNaN(priceNum) ? '0.00' : priceNum.toFixed(2));
        } else {
            var bsFixed = getPackageBsFixedAmount(pkg, getSelectedPackageBasePrice(pkg));
            var bs = bsFixed !== null ? bsFixed : NaN;
            if (bsFixed === null && !isNaN(priceNum)) {
                bs = getSelectedPaymentMethodUsesRate() ? (priceNum * getGameBsRate()) : priceNum;
            }
            submitLabel.textContent = 'Comprar — Bs ' + (isNaN(bs) ? '0' : Math.round(bs).toLocaleString('es-VE'));
        }
        updateTotals(getSelectedPackageBasePrice(pkg));
    }

    function getSelectedPackageBasePrice(pkg) {
        if (!pkg) return NaN;
        if (getSelectedPaymentCurrency() === 'usd' && pkg.usd_price !== null && pkg.usd_price !== undefined && pkg.usd_price !== '') {
            return parseFloat(pkg.usd_price);
        }
        return parseFloat(pkg.price);
    }

    // Precio exclusivo en Bs del paquete (bypasea la tasa), con el mismo %
    // de descuento aplicado que se calculó sobre el precio base en dólares.
    // Devuelve null si no aplica (no hay bs_price, o el método no cobra en Bs).
    function getPackageBsFixedAmount(pkg, usdBaseNum) {
        if (!pkg || getSelectedPaymentCurrency() !== 'bs') return null;
        var bsPrice = parseFloat(pkg.bs_price);
        if (isNaN(bsPrice) || bsPrice <= 0) return null;

        var baseNum = parseFloat(usdBaseNum);
        var ratio = 0;
        if (!isNaN(baseNum) && baseNum > 0) {
            var discountMeta = getValidDiscountMeta(getActiveDiscountCode(), baseNum);
            if (discountMeta) {
                ratio = Math.max(Math.min(discountMeta.amount / baseNum, 1), 0);
            }
        }
        return Math.max(bsPrice - (bsPrice * ratio), 0);
    }

    function getGameBsRate() {
        if (currentGame && currentGame.bs_rate_override !== null && currentGame.bs_rate_override !== undefined && currentGame.bs_rate_override !== '') {
            var overrideRate = parseFloat(currentGame.bs_rate_override);
            if (!isNaN(overrideRate) && overrideRate > 0) {
                return overrideRate;
            }
        }
        return usdRate;
    }

    function getSelectedPaymentMethodUsesRate() {
        var checked = document.querySelector('input[name="payment_method"]:checked');
        if (!checked) return false;
        return checked.dataset.usesRate === '1';
    }

    function getActiveDiscountCode() {
        return String(appliedDiscountCode || '').trim().toUpperCase();
    }

    function getEffectivePackagePrice(price) {
        var numericPrice = typeof price === 'number' ? price : parseFloat(price);
        if (isNaN(numericPrice)) return NaN;

        var discountMeta = getValidDiscountMeta(getActiveDiscountCode(), numericPrice);
        if (!discountMeta) {
            return numericPrice;
        }

        return Math.max(numericPrice - discountMeta.amount, 0);
    }

    function formatPackageAmount(amount, currency, rateOverride) {
        if (isNaN(amount)) {
            return currency === 'usd' ? '$0.00' : 'Bs 0';
        }

        if (currency === 'usd') {
            return '$' + amount.toFixed(2);
        }

        var packageRate = (!isNaN(rateOverride) && rateOverride > 0) ? rateOverride : usdRate;
        var bsAmount = getSelectedPaymentMethodUsesRate() ? (amount * packageRate) : amount;
        return 'Bs ' + (isNaN(bsAmount) ? '0' : Math.round(bsAmount).toLocaleString('es-VE'));
    }

    function syncDiscountPricingViews() {
        refreshPackagePriceViews();
        if (selectedPackage) {
            updateSidebarForPackage(selectedPackage);
        } else {
            updateTotals(null);
        }
    }

    function updateTotals(price) {
        var totalEl = document.getElementById('sidebarTotal');
        var totalBsEl = document.getElementById('sidebarTotalBs');
        if (!totalEl) return;

        if (!price) {
            lastShownTotal = null;
            totalEl.textContent = '-';
            if (totalBsEl) {
                totalBsEl.classList.add('d-none');
                totalBsEl.textContent = '≈ Bs 0,00';
            }
            return;
        }

        var priceNum = parseFloat(price);
        if (isNaN(priceNum)) return;
        var finalPrice = getEffectivePackagePrice(priceNum);

        var currency = getSelectedPaymentCurrency();
        if (currency === 'usd') {
            totalEl.textContent = '$' + finalPrice.toFixed(2);
            lastShownTotal = { currency: 'usd', amount: Number(finalPrice.toFixed(2)), usd: finalPrice };
            if (totalBsEl) totalBsEl.classList.add('d-none');
        } else {
            var bsFixed = getPackageBsFixedAmount(selectedPackage, priceNum);
            var bs = bsFixed !== null ? bsFixed : (getSelectedPaymentMethodUsesRate() ? (finalPrice * getGameBsRate()) : finalPrice);

            if (!isNaN(bs)) {
                totalEl.textContent = 'Bs ' + Math.round(bs).toLocaleString('es-VE');
                lastShownTotal = { currency: 'bs', amount: Math.round(bs), usd: finalPrice };
            } else {
                totalEl.textContent = 'Bs 0';
                lastShownTotal = null;
            }
            if (totalBsEl) {
                totalBsEl.classList.add('d-none');
            }
        }
    }

    function getValidDiscountMeta(code, priceNum) {
        if (!code || !window.validDiscounts || !window.validDiscounts[code]) {
            return null;
        }

        var discount = window.validDiscounts[code];
        var numericPrice = typeof priceNum === 'number' ? priceNum : parseFloat(priceNum);
        var amount = 0;

        if (!isNaN(numericPrice) && discount.min_amount && numericPrice < parseFloat(discount.min_amount)) {
            return null;
        }

        if (isNaN(numericPrice)) {
            return {
                code: code,
                source: discount.source || 'discount',
                amount: 0,
                config: discount
            };
        }

        if (discount.discount_type === 'percentage') {
            amount = numericPrice * parseFloat(discount.discount_value) / 100;
            if (discount.max_discount && amount > parseFloat(discount.max_discount)) {
                amount = parseFloat(discount.max_discount);
            }
        } else {
            amount = parseFloat(discount.discount_value);
            if (amount > numericPrice) {
                amount = numericPrice;
            }
        }

        // El servidor redondea el descuento de afiliado a centavos (ver
        // get_checkout_discount_result); si aquí no se hace igual, el total
        // en Bs sale unos bolívares distinto al de la orden.
        if ((discount.source || 'discount') === 'affiliate') {
            amount = Math.round(amount * 100) / 100;
            if (amount <= 0) amount = 0.01;
            if (amount > numericPrice) amount = Math.round(numericPrice * 100) / 100;
        }

        if (!(amount > 0)) {
            return null;
        }

        return {
            code: code,
            source: discount.source || 'discount',
            amount: amount,
            config: discount
        };
    }

    function setDiscountFeedback(message, kind) {
        if (!discountApplyFeedback) return;
        discountApplyFeedback.textContent = message || '';
        discountApplyFeedback.classList.remove('is-success', 'is-error');
        if (kind) {
            discountApplyFeedback.classList.add(kind);
        }
    }

    // El servidor ya no entrega la lista de todos los códigos (se podían ver
    // los privados): se pregunta solo por el que escribió el cliente.
    function lookupDiscountCode(code) {
        window.validDiscounts = window.validDiscounts || {};
        if (window.validDiscounts[code]) return Promise.resolve();
        return fetch('/api/discounts?code=' + encodeURIComponent(code) + '&ts=' + Date.now(), { cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var found = (data && data.discounts) || {};
                Object.keys(found).forEach(function (key) {
                    window.validDiscounts[key] = found[key];
                });
            })
            .catch(function () {});
    }

    function applyDiscountCode() {
        if (!affInput) return;

        affInput.dispatchEvent(new Event('input', { bubbles: true }));
        var code = affInput.value.trim().toUpperCase();
        if (!code) {
            appliedDiscountCode = '';
            syncDiscountPricingViews();
            setDiscountFeedback('Escribe un código para aplicarlo.', 'is-error');
            affInput.focus();
            return;
        }

        setDiscountFeedback('Verificando código...', null);
        lookupDiscountCode(code).then(function () {
            // Si mientras tanto el cliente cambió el código, este resultado ya no aplica.
            if (affInput.value.trim().toUpperCase() !== code) return;
            finishApplyDiscountCode(code);
        });
    }

    function finishApplyDiscountCode(code) {
        var packagePrice = selectedPackage ? getSelectedPackageBasePrice(selectedPackage) : NaN;
        var knownCode = !!(window.validDiscounts && window.validDiscounts[code]);
        var discountMeta = getValidDiscountMeta(code, packagePrice);

        if (discountMeta) {
            appliedDiscountCode = code;
            syncDiscountPricingViews();
            setDiscountFeedback('Descuento ' + code + ' aplicado.', 'is-success');
            return;
        }

        if (knownCode) {
            appliedDiscountCode = code;
        } else {
            appliedDiscountCode = '';
        }

        if (knownCode && selectedPackage) {
            setDiscountFeedback('Ese código existe, pero no aplica para este monto.', 'is-error');
        } else if (knownCode) {
            setDiscountFeedback('Código reconocido. Selecciona un paquete para calcular el descuento.', 'is-success');
        } else {
            // El código no existe: se vacía el campo para que no viaje con la
            // orden. Es la casilla donde el cliente pega su referencia por
            // error, y antes ese valor llegaba hasta el final del checkout.
            affInput.value = '';
            setDiscountFeedback('Ese código no es válido. Si es tu referencia de pago, va más abajo, en "Coloca tu referencia aquí".', 'is-error');
        }

        syncDiscountPricingViews();
        affInput.focus();
    }

    function refreshPackagePriceViews() {
        var items = document.querySelectorAll('.package-item');
        var currency = getSelectedPaymentCurrency();
        var activeCode = getActiveDiscountCode();
        items.forEach(function (item) {
            var priceSpan = item.querySelector('.price');
            var priceUsdSpan = item.querySelector('.price-usd');
            var usdStr = item.dataset.priceUsd;
            if (!priceSpan || !usdStr) return;
            var usd = parseFloat(usdStr);
            if (isNaN(usd)) return;
            var discountMeta = getValidDiscountMeta(activeCode, usd);
            var finalUsd = discountMeta ? Math.max(usd - discountMeta.amount, 0) : usd;

            var bsPriceNum = parseFloat(item.dataset.priceBs);
            var hasBsFixed = currency === 'bs' && !isNaN(bsPriceNum) && bsPriceNum > 0;
            var bsRatio = 0;
            if (hasBsFixed && discountMeta && usd > 0) {
                bsRatio = Math.max(Math.min(discountMeta.amount / usd, 1), 0);
            }

            if (hasBsFixed) {
                priceSpan.textContent = 'Bs ' + Math.round(Math.max(bsPriceNum - (bsPriceNum * bsRatio), 0)).toLocaleString('es-VE');
            } else {
                priceSpan.textContent = formatPackageAmount(finalUsd, currency, getGameBsRate());
            }

            if (priceUsdSpan) {
                if (discountMeta) {
                    priceUsdSpan.textContent = hasBsFixed
                        ? 'Bs ' + Math.round(bsPriceNum).toLocaleString('es-VE')
                        : formatPackageAmount(usd, currency, getGameBsRate());
                    priceUsdSpan.style.display = 'block';
                    item.classList.add('has-discount');
                } else {
                    priceUsdSpan.textContent = '';
                    priceUsdSpan.style.display = 'none';
                    item.classList.remove('has-discount');
                }
            }
        });

        if (selectedPackage) {
            updateTotals(getSelectedPackageBasePrice(selectedPackage));
        }
    }

    /* ── Close Packages Panel ─────────────────────────────── */
    window.closePackages = function () {
        var panel = document.getElementById('packagesPanel');
        if (panel) panel.style.display = 'none';
        activeGameId = null;
        activeGameSlug = '';
        updateGameUrl('');
        document.querySelectorAll('.game-card').forEach(function (c) {
            c.classList.remove('active');
        });
    };

    /* ── Get Current Grid Column Count ───────────────────── */
    function getGridColumns() {
        return window.innerWidth <= 640 ? 2 : 4;
    }

    /* ── Re-position Panel on Resize ─────────────────────── */
    var resizeTimer;
    window.addEventListener('resize', function () {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () {
            if (activeGameId !== null) {
                if (document.getElementById('packagesPanelHost')) {
                    return;
                }
                var card = document.querySelector('.game-card.active');
                if (card) {
                    var panel = document.getElementById('packagesPanel');
                    var grid  = document.getElementById('gamesGrid');
                    if (window.innerWidth <= 640) {
                        grid.parentNode.appendChild(panel);
                    } else {
                        var cards = Array.from(grid.querySelectorAll('.game-card'));
                        var idx   = cards.indexOf(card);
                        var cols  = getGridColumns();
                        var row   = Math.floor(idx / cols);
                        var lastIdx  = Math.min((row + 1) * cols - 1, cards.length - 1);
                        var insertAfter = cards[lastIdx];
                        grid.insertBefore(panel, insertAfter.nextSibling || null);
                    }
                }
            }
        }, 150);
    });

    /* ── Bind Initial Game Cards (SSR) ───────────────────── */
    var initialCards = document.querySelectorAll('.game-card');
    initialCards.forEach(function (card) {
        card.addEventListener('click', function () { handleGameClick(card); });
    });

    // Al cargar se abre un juego solo: el que venga en el enlace
    // (/?juego=<slug>) y, si no vino ninguno, el primero de la fila.
    if (initialCards.length > 0) {
        var slugPedido = (window.PRESELECT_GAME || '').toString().trim();
        var cardPedida = null;
        if (slugPedido) {
            initialCards.forEach(function (card) {
                if (!cardPedida && card.dataset.gameSlug === slugPedido) cardPedida = card;
            });
        }
        handleGameClick(cardPedida || initialCards[0]);
        if (cardPedida && cardPedida.scrollIntoView) {
            // El juego compartido puede estar fuera de la parte visible del
            // carrusel: se trae a la vista para que se note cuál se abrió.
            try {
                cardPedida.scrollIntoView({block: 'nearest', inline: 'center'});
            } catch (_) {
                cardPedida.scrollIntoView();
            }
        }
    }

    /* ── Affiliate code: auto-uppercase ──────────────────── */
    var affInput = document.getElementById('affiliate_code');
    if (affInput) {
        affInput.addEventListener('input', function () {
            this.value = this.value.toUpperCase();
            if (!this.value.trim()) {
                appliedDiscountCode = '';
                setDiscountFeedback('', null);
                syncDiscountPricingViews();
            } else if (this.value.trim() !== getActiveDiscountCode()) {
                appliedDiscountCode = '';
                syncDiscountPricingViews();
            }
        });

        affInput.addEventListener('keydown', function (evt) {
            if (evt.key === 'Enter') {
                evt.preventDefault();
                applyDiscountCode();
            }
        });
    }

    if (applyDiscountBtn && affInput) {
        applyDiscountBtn.addEventListener('click', function () {
            applyDiscountCode();
        });
    }

    if (manualInfoCloseBtn && manualInfoPopup) {
        manualInfoCloseBtn.addEventListener('click', closeManualInfoPopup);
        manualInfoPopup.addEventListener('click', function (evt) {
            if (evt.target === manualInfoPopup) {
                closeManualInfoPopup();
            }
        });
    }

    if (discountInfoCloseBtn && discountInfoPopup) {
        discountInfoCloseBtn.addEventListener('click', closeDiscountInfoPopup);
        discountInfoPopup.addEventListener('click', function (evt) {
            if (evt.target === discountInfoPopup) {
                closeDiscountInfoPopup();
            }
        });
    }

    if (gameSelectionPopupCloseBtn && gameSelectionPopup) {
        gameSelectionPopupCloseBtn.addEventListener('click', closeGameSelectionPopup);
        gameSelectionPopup.addEventListener('click', function (evt) {
            if (evt.target === gameSelectionPopup) {
                closeGameSelectionPopup();
            }
        });
    }

    if (pkgOneTimeCloseBtn && pkgOneTimePopup) {
        pkgOneTimeCloseBtn.addEventListener('click', closePkgOneTimePopup);
        pkgOneTimePopup.addEventListener('click', function (evt) {
            if (evt.target === pkgOneTimePopup) {
                closePkgOneTimePopup();
            }
        });
    }

    if (rankingModalOpenBtn) {
        rankingModalOpenBtn.addEventListener('click', openRankingModal);
    }

    if (pointsModalOpenBtn) {
        pointsModalOpenBtn.addEventListener('click', openPointsModal);
    }

    if (pointsModalCloseBtn && pointsModal) {
        pointsModalCloseBtn.addEventListener('click', closePointsModal);
        pointsModal.addEventListener('click', function (evt) {
            if (evt.target === pointsModal) {
                closePointsModal();
            }
        });
    }

    var pointsLookupBtnEl = document.getElementById('pointsLookupBtn');
    if (pointsLookupBtnEl) {
        pointsLookupBtnEl.addEventListener('click', handlePointsLookup);
    }

    var pointsWalletBtnEl = document.getElementById('pointsWalletBtn');
    if (pointsWalletBtnEl) {
        pointsWalletBtnEl.addEventListener('click', handlePointsWalletTransfer);
    }

    var pointsChangeIdBtnEl = document.getElementById('pointsChangeIdBtn');
    if (pointsChangeIdBtnEl) {
        pointsChangeIdBtnEl.addEventListener('click', resetPointsModal);
    }

    var pointsSpinBtnEl = document.getElementById('pointsSpinBtn');
    if (pointsSpinBtnEl) {
        pointsSpinBtnEl.addEventListener('click', handlePointsSpin);
    }

    var pointsGoSpinBtnEl = document.getElementById('pointsGoSpinBtn');
    if (pointsGoSpinBtnEl) {
        pointsGoSpinBtnEl.addEventListener('click', function () {
            if (pointsState.lastBalanceData) enterPointsSpinStep(pointsState.lastBalanceData);
        });
    }

    var pointsGoRedeemBtnEl = document.getElementById('pointsGoRedeemBtn');
    if (pointsGoRedeemBtnEl) {
        pointsGoRedeemBtnEl.addEventListener('click', function () {
            if (pointsState.lastBalanceData) enterPointsRedeemStep(pointsState.lastBalanceData);
        });
    }

    var pointsChoiceChangeIdBtnEl = document.getElementById('pointsChoiceChangeIdBtn');
    if (pointsChoiceChangeIdBtnEl) {
        pointsChoiceChangeIdBtnEl.addEventListener('click', resetPointsModal);
    }

    var pointsRedeemBackBtnEl = document.getElementById('pointsRedeemBackBtn');
    if (pointsRedeemBackBtnEl) {
        pointsRedeemBackBtnEl.addEventListener('click', function () {
            var caps = pointsState.gameCaps[pointsState.gameId] || {};
            if (caps.hasSpin && pointsState.lastBalanceData) {
                enterPointsChoiceStep(pointsState.lastBalanceData);
            } else {
                resetPointsModal();
            }
        });
    }

    var pointsRedeemBtnEl = document.getElementById('pointsRedeemBtn');
    if (pointsRedeemBtnEl) {
        pointsRedeemBtnEl.addEventListener('click', handlePointsRedeemSubmit);
    }

    if (rankingModalCloseBtn && rankingModal) {
        rankingModalCloseBtn.addEventListener('click', closeRankingModal);
        rankingModal.addEventListener('click', function (evt) {
            if (evt.target === rankingModal) {
                closeRankingModal();
            }
        });
    }

    if (rankingBoardEl) {
        rankingBoardEl.addEventListener('click', function (evt) {
            var tappable = evt.target.closest('[data-rk-handle]');
            if (tappable && rankingBoardEl.contains(tappable)) openRankingProfilePop(tappable);
        });
        rankingBoardEl.addEventListener('keydown', function (evt) {
            if (evt.key !== 'Enter' && evt.key !== ' ') return;
            var tappable = evt.target.closest ? evt.target.closest('[data-rk-handle]') : null;
            if (tappable && rankingBoardEl.contains(tappable)) { evt.preventDefault(); openRankingProfilePop(tappable); }
        });
    }

    if (rankingTabsEl) {
        rankingTabsEl.addEventListener('click', function (evt) {
            var tabBtn = evt.target && evt.target.closest('.ranking-tab');
            if (!tabBtn) return;
            rankingState.activeKey = tabBtn.dataset.rankingKey || null;
            rankingState.showAllPositions = false;
            renderRankingBoard();
        });
    }

    if (rankingBoardEl) {
        rankingBoardEl.addEventListener('click', function (evt) {
            var moreBtn = evt.target && evt.target.closest('#rankingShowMoreBtn');
            if (moreBtn) {
                rankingState.showAllPositions = !rankingState.showAllPositions;
                renderRankingBoard();
                return;
            }
            var lookupBtn = evt.target && evt.target.closest('#rankingLookupBtn');
            if (lookupBtn) {
                runRankingPositionLookup();
                return;
            }
        });
        rankingBoardEl.addEventListener('keydown', function (evt) {
            if (evt.key === 'Enter' && evt.target && evt.target.id === 'rankingLookupIdInput') {
                evt.preventDefault();
                runRankingPositionLookup();
                return;
            }
        });
        rankingBoardEl.addEventListener('input', function (evt) {
            if (evt.target && evt.target.id === 'rankingLookupIdInput') {
                rankingState.lookupInputValue = evt.target.value;
            }
        });
    }

    document.addEventListener('click', function (evt) {
        var phoneOption = evt.target && evt.target.closest('.phone-country-option');
        if (phoneOption) {
            evt.preventDefault();
            if (phoneCountryCodeInput) {
                phoneCountryCodeInput.value = phoneOption.dataset.code || '+58';
            }
            updatePhoneCountryDisplay();
            syncPhoneHiddenValue();
            closePhoneCountryMenu();
            return;
        }

        var phoneTrigger = evt.target && evt.target.closest('#phoneCountryTrigger');
        if (phoneTrigger) {
            evt.preventDefault();
            if (phoneFieldStack && phoneFieldStack.classList.contains('is-open')) {
                closePhoneCountryMenu();
            } else {
                openPhoneCountryMenu();
            }
            return;
        }

        var insidePhoneSelector = evt.target && evt.target.closest('.phone-field-stack');
        if (!insidePhoneSelector) {
            closePhoneCountryMenu();
        }

        var trigger = evt.target && evt.target.closest('#manualInfoTrigger');
        if (trigger) {
            evt.preventDefault();
            openManualInfoPopup();
            return;
        }

        trigger = evt.target && evt.target.closest('#discountInfoTrigger');
        if (!trigger) return;
        evt.preventDefault();
        openDiscountInfoPopup();
    });

    document.addEventListener('keydown', function (evt) {
        if (evt.key === 'Escape') {
            closePhoneCountryMenu();
            closeManualInfoPopup();
            closeDiscountInfoPopup();
            closeGameSelectionPopup();
            closeRankingModal();
        }
    });

    function openManualInfoPopup() {
        if (!manualInfoPopup) return;
        manualInfoPopup.style.display = 'flex';
        manualInfoPopup.setAttribute('aria-hidden', 'false');
    }

    function closeManualInfoPopup() {
        if (!manualInfoPopup) return;
        manualInfoPopup.style.display = 'none';
        manualInfoPopup.setAttribute('aria-hidden', 'true');
    }

    /* Aviso de Wallet (Zinli, TikTok, Binance): plazo y horario de gestión.
       Ya no es una ventana que tapa la pantalla: es un cuadrito chico sobre los paquetes. El servidor solo lo pide
       (requires_wallet_notice) si el juego es de Wallet y el dueño no lo apagó en Admin → Juegos. El horario sale
       de la configuración de hoy que trae la misma respuesta de paquetes. */
    function showWalletNote(schedule) {
        var note = document.getElementById('walletNoticeInline');
        if (!note) return;
        var open = document.getElementById('walletNoticeOpen');
        var close = document.getElementById('walletNoticeClose');
        if (schedule && schedule.open_label && open) open.textContent = schedule.open_label;
        if (schedule && schedule.close_label && close) close.textContent = schedule.close_label;
        note.hidden = false;
    }

    function hideWalletNote() {
        var note = document.getElementById('walletNoticeInline');
        if (note) note.hidden = true;
    }

    function openDiscountInfoPopup() {
        if (!discountInfoPopup) return;
        discountInfoPopup.style.display = 'flex';
        discountInfoPopup.setAttribute('aria-hidden', 'false');
    }

    function closeDiscountInfoPopup() {
        if (!discountInfoPopup) return;
        discountInfoPopup.style.display = 'none';
        discountInfoPopup.setAttribute('aria-hidden', 'true');
    }

    function openGameSelectionPopup() {
        if (!gameSelectionPopup) return;
        gameSelectionPopup.style.display = 'flex';
        gameSelectionPopup.setAttribute('aria-hidden', 'false');
    }

    function closeGameSelectionPopup() {
        if (!gameSelectionPopup) return;
        gameSelectionPopup.style.display = 'none';
        gameSelectionPopup.setAttribute('aria-hidden', 'true');
    }

    function openPkgOneTimePopup() {
        if (!pkgOneTimePopup) return;
        pkgOneTimePopup.style.display = 'flex';
        pkgOneTimePopup.setAttribute('aria-hidden', 'false');
    }

    function closePkgOneTimePopup() {
        if (!pkgOneTimePopup) return;
        pkgOneTimePopup.style.display = 'none';
        pkgOneTimePopup.setAttribute('aria-hidden', 'true');
    }

    /* ── Popup "Únete a nuestra comunidad" (recurrente) ─────────
       Reaparece cada N horas (configurable en el admin) mientras el
       cliente navega, salvo que marque "No mostrar más por hoy". */
    var COMMUNITY_LAST_SHOWN_KEY = 'store:community-popup-last-shown';
    var COMMUNITY_MUTED_UNTIL_KEY = 'store:community-popup-muted-until';
    var communityPopupCloseTimer = null;

    function communityPopupConfig() {
        return window.NX_COMMUNITY_POPUP || { enabled: false, intervalHours: 3, whatsappUrl: '' };
    }

    function isCommunityPopupMuted() {
        try {
            var mutedUntil = parseInt(localStorage.getItem(COMMUNITY_MUTED_UNTIL_KEY) || '0', 10);
            return !!mutedUntil && Date.now() < mutedUntil;
        } catch (_) {
            return false;
        }
    }

    function communityPopupDue() {
        var cfg = communityPopupConfig();
        if (!cfg.enabled || isCommunityPopupMuted()) return false;
        try {
            var lastShown = parseInt(localStorage.getItem(COMMUNITY_LAST_SHOWN_KEY) || '0', 10);
            var intervalMs = Math.max(1, cfg.intervalHours || 3) * 60 * 60 * 1000;
            return !lastShown || (Date.now() - lastShown) >= intervalMs;
        } catch (_) {
            return true;
        }
    }

    function openCommunityPopup() {
        if (!communityPopup) return;
        if (communityPopupJoinBtn) {
            communityPopupJoinBtn.href = communityPopupConfig().whatsappUrl || 'https://whatsapp.com';
        }
        communityPopup.style.display = 'flex';
        communityPopup.setAttribute('aria-hidden', 'false');
        try { localStorage.setItem(COMMUNITY_LAST_SHOWN_KEY, String(Date.now())); } catch (_) {}

        // Botón de cerrar deshabilitado con cuenta regresiva de 4s, para que
        // el aviso no se cierre por accidente al primer toque.
        var count = 4;
        if (communityPopupCloseX) communityPopupCloseX.disabled = true;
        if (communityPopupCloseCount) communityPopupCloseCount.textContent = String(count);
        clearInterval(communityPopupCloseTimer);
        communityPopupCloseTimer = setInterval(function () {
            count -= 1;
            if (communityPopupCloseCount) communityPopupCloseCount.textContent = String(Math.max(count, 0));
            if (count <= 0) {
                clearInterval(communityPopupCloseTimer);
                if (communityPopupCloseX) communityPopupCloseX.disabled = false;
            }
        }, 1000);
    }

    function closeCommunityPopup() {
        if (!communityPopup) return;
        communityPopup.style.display = 'none';
        communityPopup.setAttribute('aria-hidden', 'true');
        clearInterval(communityPopupCloseTimer);
    }

    function muteCommunityPopupForToday() {
        try {
            var endOfDay = new Date();
            endOfDay.setHours(23, 59, 59, 999);
            localStorage.setItem(COMMUNITY_MUTED_UNTIL_KEY, String(endOfDay.getTime()));
        } catch (_) {}
        closeCommunityPopup();
    }

    if (communityPopupCloseX) {
        communityPopupCloseX.addEventListener('click', function () {
            if (!communityPopupCloseX.disabled) closeCommunityPopup();
        });
    }
    if (communityPopupContinueBtn) {
        communityPopupContinueBtn.addEventListener('click', closeCommunityPopup);
    }
    if (communityPopupMuteBtn) {
        communityPopupMuteBtn.addEventListener('click', muteCommunityPopupForToday);
    }

    function scheduleCommunityPopupCheck() {
        if (!communityPopupConfig().enabled) return;
        if (communityPopupDue()) {
            window.setTimeout(openCommunityPopup, 2500);
        }
        // Revisa cada minuto por si el cliente se queda navegando más
        // tiempo del intervalo configurado, sin necesidad de recargar.
        window.setInterval(function () {
            if (communityPopup && communityPopup.style.display === 'flex') return;
            if (communityPopupDue()) openCommunityPopup();
        }, 60000);
    }
    scheduleCommunityPopupCheck();

    /* ── Notificaciones push (opt-in en 2 pasos) ─────────────
       Paso 1: aviso propio (banner) — no el permiso nativo de golpe.
       Paso 2: si aceptan, recién ahí se dispara el permiso real del
       navegador y se registra la suscripción. */
    var PUSH_DISMISSED_KEY = 'store:push-prompt-dismissed';

    function pushSupported() {
        return ('serviceWorker' in navigator) && ('PushManager' in window) && ('Notification' in window);
    }

    function urlBase64ToUint8Array(base64String) {
        var padding = '='.repeat((4 - base64String.length % 4) % 4);
        var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        var rawData = window.atob(base64);
        var outputArray = new Uint8Array(rawData.length);
        for (var i = 0; i < rawData.length; ++i) {
            outputArray[i] = rawData.charCodeAt(i);
        }
        return outputArray;
    }

    function maybeShowPushPrompt() {
        if (!pushSupported()) return;
        if (Notification.permission !== 'default') return;
        try {
            if (localStorage.getItem(PUSH_DISMISSED_KEY) === '1') return;
        } catch (_) {}

        var banner = document.getElementById('pushPromptBanner');
        if (!banner) return;
        window.setTimeout(function () {
            if (Notification.permission === 'default') {
                banner.style.display = 'flex';
            }
        }, 4000);
    }

    function hidePushPrompt(remember) {
        var banner = document.getElementById('pushPromptBanner');
        if (banner) banner.style.display = 'none';
        if (remember) {
            try { localStorage.setItem(PUSH_DISMISSED_KEY, '1'); } catch (_) {}
        }
    }

    function activatePushNotifications() {
        if (!pushSupported()) { hidePushPrompt(true); return; }

        navigator.serviceWorker.register('/sw.js')
            .then(function (registration) {
                return Notification.requestPermission().then(function (permission) {
                    if (permission !== 'granted') {
                        hidePushPrompt(true);
                        return null;
                    }
                    return fetch('/push/vapid-public-key')
                        .then(function (r) { return r.json(); })
                        .then(function (data) {
                            return registration.pushManager.subscribe({
                                userVisibleOnly: true,
                                applicationServerKey: urlBase64ToUint8Array(data.publicKey),
                            });
                        });
                });
            })
            .then(function (subscription) {
                if (!subscription) return;
                var raw = subscription.toJSON();
                return fetch('/push/subscribe', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        endpoint: raw.endpoint,
                        keys: raw.keys,
                        order_number: window.CURRENT_ORDER_NUMBER || '',
                    }),
                });
            })
            .then(function () {
                hidePushPrompt(true);
            })
            .catch(function () {
                hidePushPrompt(true);
            });
    }

    var pushAcceptBtnEl = document.getElementById('pushPromptAcceptBtn');
    if (pushAcceptBtnEl) {
        pushAcceptBtnEl.addEventListener('click', activatePushNotifications);
    }
    var pushDismissBtnEl = document.getElementById('pushPromptDismissBtn');
    if (pushDismissBtnEl) {
        pushDismissBtnEl.addEventListener('click', function () { hidePushPrompt(true); });
    }
    maybeShowPushPrompt();

    /* Muestra el aviso configurado para el paquete elegido (si tiene uno),
       una sola vez por paquete durante esta visita. */
    function maybeShowPackageAnnouncement(pkg) {
        if (!pkg || !pkg.announcement_type) return;
        if (pkgAnnouncementShown[pkg.id]) return;
        pkgAnnouncementShown[pkg.id] = true;

        if (pkg.announcement_type === 'one_time_purchase') {
            openPkgOneTimePopup();
        } else if (pkg.announcement_type === 'redeem_code') {
            openGameSelectionPopup();
        }
    }

    /* ── Quick checkout form submit UX ───────────────────── */
    var quickForm = document.getElementById('quickCheckoutForm');
    if (quickForm) {
        quickForm.addEventListener('submit', function (e) {
            syncPhoneHiddenValue();
            saveRememberedContact();

            if (!selectedPackage) {
                e.preventDefault();
                alert('Primero selecciona un paquete.');
                return;
            }
            
            if (currentGame && currentGame.category_slug !== 'tarjetas') {
                var playerIdInput = document.getElementById('playerId');
                if (playerIdInput && !playerIdInput.value.trim()) {
                    e.preventDefault();
                    alert(getPlayerInputRequiredMessage(currentGame));
                    playerIdInput.focus();
                    return;
                }
            }
            
            var btn = document.getElementById('sidebarSubmitBtn');
            var label = document.getElementById('sidebarSubmitLabel');
            if (btn && label) {
                btn.disabled = true;
                label.textContent = 'Procesando...';
            }
        });

        // Recalcular total en Bs cuando cambia el método de pago
        document.querySelectorAll('input[name="payment_method"]').forEach(function (input) {
            input.addEventListener('change', function () {
                if (selectedPackage) {
                    updateTotals(selectedPackage.price);
                    updateSidebarForPackage(selectedPackage);
                } else {
                    updateTotals(null);
                }
                refreshPackagePriceViews();
                updateStepsTheme();
            });
        });

        // Recalcular total cuando cambia el código de descuento
        var discountInput = document.getElementById('affiliate_code');
        if (discountInput) {
            discountInput.addEventListener('input', function () {
                if (selectedPackage) {
                    updateTotals(selectedPackage.price);
                    updateSidebarForPackage(selectedPackage);
                } else {
                    refreshPackagePriceViews();
                }
            });
        }

        updateStepsTheme();
    }

    /* ── HTML escape helper ───────────────────────────────── */
    function escHtml(str) {
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // Llegar desde otra página (la barra de abajo del perfil) con #ranking o #puntos abre esa ventana. La dirección se limpia para que
    // recargar o volver atrás no la reabra.
    (function openFromHash() {
        var hash = (window.location.hash || '').toLowerCase();
        if (hash !== '#ranking' && hash !== '#puntos') return;
        window.nxHashOpen = hash;   // la barra de abajo (dock.js) lo lee para nacer con la burbuja ya en Ranking / Puntos
        try { window.history.replaceState(null, '', window.location.pathname + window.location.search); } catch (err) { /* nada */ }
        window.setTimeout(function () {
            if (hash === '#ranking') openRankingModal(); else openPointsModal();
        }, 250);
    })();

    // Inicializar cuando el DOM esté listo
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            window.nxDebug('DOM ready, running init...');
        });
    } else {
        window.nxDebug('DOM already ready');
    }

})();
