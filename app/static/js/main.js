/* ── 3S Recargas — Store JavaScript ─────────────────────── */

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
    var supportModal = document.getElementById('supportModal');
    var supportModalOpenBtn = document.getElementById('openSupportModalBtn');
    var supportModalCloseBtn = document.getElementById('supportModalCloseBtn');
    var pointsModal = document.getElementById('pointsModal');
    var pointsModalOpenBtn = document.getElementById('openPointsModalBtn');
    var pointsModalCloseBtn = document.getElementById('pointsModalCloseBtn');
    var supportForm = document.getElementById('supportForm');
    var supportIdentityInput = document.getElementById('supportOrderIdentity');
    var supportGameInput = document.getElementById('supportGame');
    var supportReasonInput = document.getElementById('supportReason');
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
        items: []
    };

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
    function loadGames(category) {
        var grid = document.getElementById('gamesGrid');
        grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">Cargando...</div>';
        updateGamesCarouselNav();

        fetch('/api/games?category=' + encodeURIComponent(category))
            .then(function (r) { return r.json(); })
            .then(function (data) { renderGames(data.games); })
            .catch(function () {
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

        var html =
            '<div class="ranking-shell">' +
                '<div class="ranking-prizes">' +
                    '<div class="ranking-prizes-title">Premio</div>';

        if (activeItem.reward_ladder && activeItem.reward_ladder.length) {
            activeItem.reward_ladder.forEach(function (reward) {
                html +=
                    '<div class="ranking-prize-item">' +
                        '<span>#' + escHtml(reward.position) + '</span>' +
                        '<strong>' + escHtml(formatRewardValue(reward.reward_label, activeItem.key)) + '</strong>' +
                    '</div>';
            });
        }

        html +=
                '</div>' +
                '<div class="ranking-main-card">' +
                    '<div class="ranking-live-chip">LIVE</div>';

        if (!activeItem.entries || activeItem.entries.length === 0) {
            html += '<div class="ranking-empty">Aún no hay posiciones registradas para este mes.</div>';
        } else {
            html +=
                '<div class="ranking-table-wrap">' +
                    '<table class="ranking-table">' +
                        '<thead>' +
                            '<tr>' +
                                '<th>#</th>' +
                                '<th>Jugador</th>' +
                                '<th>ID</th>' +
                                '<th>' + escHtml(activeItem.units_label || 'Total') + '</th>' +
                                '<th>Premio</th>' +
                            '</tr>' +
                        '</thead>' +
                        '<tbody>';

            activeItem.entries.forEach(function (entry) {
                var prizeClass = isPrizeLabel(entry.prize_label, entry.is_prize_eligible) ? ' style="color:#f8d16a;font-weight:800"' : '';
                html +=
                    '<tr>' +
                        '<td class="ranking-position-cell">#' + escHtml(entry.position) + '</td>' +
                        '<td>' + escHtml(entry.masked_nickname || 'Jugador***') + '</td>' +
                        '<td>' + escHtml(entry.masked_player_id || '----') + '</td>' +
                        '<td>' + escHtml(entry.total_units) + '</td>' +
                        '<td' + prizeClass + '>' + escHtml(formatRewardValue(entry.prize_label || 'Sin premio', activeItem.key)) + '</td>' +
                    '</tr>';
            });

            html += '</tbody></table></div>';
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
                    '<div class="ranking-current-title">Tu posición actual #' + escHtml(activeItem.current_position.position) + '</div>' +
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
                html += '<div class="ranking-current-hint">Ya estás en el primer puesto de este ranking.</div>';
            } else {
                html += '<div class="ranking-current-hint">Ya alcanzaste el puntaje del siguiente puesto. La tabla se reordenará cuando se actualice el ranking.</div>';
            }

            html += '</div>';
        } else {
            html += '<div class="ranking-current-card is-empty"><div class="ranking-current-title">Tu posición actual</div><div class="ranking-current-hint">Ingresa tu ID del juego actual o inicia sesión con tu cuenta de ese servicio para ver tu puesto.</div></div>';
        }

        html += '</div></div>';

        rankingBoardEl.innerHTML = html;
        rankingBoardEl.style.display = 'block';
        if (rankingStatusEl) {
            rankingStatusEl.style.display = 'none';
        }
        renderRankingTabs();
    }

    function fetchRankings(forceReload) {
        if (rankingState.loading) return;
        if (rankingState.loaded && !forceReload) {
            renderRankingBoard();
            return;
        }

        rankingState.loading = true;
        setRankingStatus('Cargando ranking...');

        fetch('/api/rankings' + getRankingLookupParams())
            .then(function (response) { return response.json(); })
            .then(function (data) {
                var rankings = data && Array.isArray(data.rankings) ? data.rankings : [];
                rankingState.items = rankings.filter(function (item) {
                    return item && item.enabled;
                });
                rankingState.activeKey = rankingState.items.length ? rankingState.items[0].key : null;
                rankingState.loaded = true;
                renderRankingTabs();
                renderRankingBoard();
            })
            .catch(function () {
                setRankingStatus('No se pudo cargar el ranking en este momento.');
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
        closeModal(rankingModal);
    }

    /* ── Canjear Puntos ───────────────────────────────────── */
    var pointsState = { gameId: null, playerId: '', spinCost: 5, spinning: false, gamesLoaded: false };

    function pointsEl(id) { return document.getElementById(id); }

    function loadPointsGames() {
        if (pointsState.gamesLoaded) return;
        var select = pointsEl('pointsGameSelect');
        if (!select) return;
        fetch('/api/points/games')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                pointsState.gamesLoaded = true;
                var games = (data && data.games) || [];
                pointsState.spinCost = (data && data.spin_cost) || 5;
                if (!games.length) {
                    select.innerHTML = '<option value="">Todavía no hay premios de puntos disponibles</option>';
                    return;
                }
                select.innerHTML = '<option value="">Selecciona un juego</option>' + games.map(function (g) {
                    return '<option value="' + g.game_id + '" data-prize="' + escHtml(g.prize_label) + '">' + escHtml(g.game_name) + '</option>';
                }).join('');
            })
            .catch(function () {
                select.innerHTML = '<option value="">Error al cargar juegos</option>';
            });
    }

    function resetPointsModal() {
        var lookupStep = pointsEl('pointsLookupStep');
        var spinStep = pointsEl('pointsSpinStep');
        if (lookupStep) lookupStep.style.display = 'block';
        if (spinStep) spinStep.style.display = 'none';
        var err = pointsEl('pointsLookupError');
        if (err) { err.style.display = 'none'; err.textContent = ''; }
        var result = pointsEl('pointsSpinResult');
        if (result) { result.style.display = 'none'; result.textContent = ''; }
    }

    function openPointsModal() {
        if (!pointsModal) return;
        loadPointsGames();
        resetPointsModal();
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
        }, POINTS_STRIP_SPIN_MS);
    }

    /* Modal del resultado: al ganar el regalo se abre por pasos (tiembla,
       revienta, aparece el premio); al perder se queda la equis quieta. */
    function showPointsResultModal(won, rewardLabel, gameName) {
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
                    (won ? '¡Abriendo Regalo!' : '¡Fallaste!') +
                '</h3>' +
                '<p class="points-result-text" id="pointsResultDesc">' +
                    (won
                        ? 'Descubriendo tu premio exclusivo...'
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
            titulo.textContent = '¡Ganaste ' + premio + '!';
            desc.textContent = '¡Felicidades! Se procesará al mismo ID'
                + (gameName ? ' de ' + gameName : '') + '.';
        }, 1600));
    }

    function showPointsBalance(data) {
        pointsState.gameId = data.game_id;
        pointsState.playerId = data.player_id;
        pointsState.spinCost = data.spin_cost;

        var select = pointsEl('pointsGameSelect');
        var selectedOption = select ? select.options[select.selectedIndex] : null;
        var prizeLabel = (selectedOption && selectedOption.getAttribute('data-prize')) || 'Premio';

        pointsEl('pointsLookupStep').style.display = 'none';
        pointsEl('pointsSpinStep').style.display = 'block';
        pointsEl('pointsSpinGameLabel').textContent = data.game_name;
        pointsEl('pointsSpinIdLabel').textContent = 'ID: ' + data.player_id;
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
                }, POINTS_STRIP_SPIN_MS);
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

    function prefillSupportForm() {
        var playerIdInput = document.getElementById('playerId');
        if (supportIdentityInput && !supportIdentityInput.value && playerIdInput && playerIdInput.value) {
            supportIdentityInput.value = playerIdInput.value.trim();
        }
        if (supportGameInput && !supportGameInput.value && currentGame && currentGame.name) {
            supportGameInput.value = currentGame.name;
        }
    }

    function buildWhatsAppSupportUrl(message) {
        var baseUrl = String(window.SUPPORT_WHATSAPP_URL || 'https://wa.me/19543789224').trim();
        var separator = baseUrl.indexOf('?') >= 0 ? '&' : '?';
        return baseUrl + separator + 'text=' + encodeURIComponent(message);
    }

    function openSupportModal() {
        if (!supportModal) return;
        prefillSupportForm();
        openModal(supportModal);
    }

    function closeSupportModal() {
        closeModal(supportModal);
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
    function fetchPackages(gameId) {
        console.log('Fetching packages for gameId:', gameId);
        fetch('/api/packages/' + gameId)
            .then(function (r) { 
                console.log('Response status:', r.status);
                return r.json(); 
            })
            .then(function (data) {
                console.log('Packages data:', data);
                manualSchedule = (data.game && data.game.manual_schedule) || null;
                applyGameToSidebar(data.game);
                renderPackages(data.packages);
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
                    openWalletNotice();
                } else {
                    closeWalletNotice();
                }
            })
            .catch(function (err) {
                console.error('Error fetching packages:', err);
                document.getElementById('packagesGrid').innerHTML =
                    '<div class="empty-state" style="grid-column:1/-1">Error al cargar paquetes.</div>';
            });
    }

    /* Texto del aviso de fuera de horario, con las horas que configuró el
       admin (y un respaldo por si el servidor no las mandó). */
    function getClosedNoticeText() {
        var openLabel = (manualSchedule && manualSchedule.open_label) || '5:00 a. m.';
        var closeLabel = (manualSchedule && manualSchedule.close_label) || '10:00 p. m.';
        return 'Estas recargas se hacen a mano y ahora estamos cerrados. ' +
               'Atendemos de ' + openLabel + ' a ' + closeLabel + ', hora de Venezuela.';
    }

    /* ── Render Package Items ─────────────────────────────── */
    function renderPackages(packages) {
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
            el.style.color = '#94a3b8';
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
                setNickUIErr((e && e.message) ? e.message : 'No se pudo verificar');
                setVerifyCachedNick(uid, '');
            })
            .finally(function() {
                if (requestSeq !== verifyState.requestSeq) return;
                verifyState.verifying = false;
            });
    }

    function getPlayerInputType(game) {
        if (!game) return 'numeric';
        if (game.category_slug === 'wallet') return 'email';
        if (game.category_slug === 'tarjetas') return 'none';

        var inputType = String(game.player_id_input_type || '').toLowerCase();
        return ['numeric', 'email', 'text'].indexOf(inputType) >= 0 ? inputType : 'numeric';
    }

    function getPlayerInputRequiredMessage(game) {
        if (getPlayerInputType(game) === 'email') return 'Por favor ingresa tu correo electrónico.';
        return 'Por favor ingresa tu ' + (game.player_id_label || 'ID del jugador') + '.';
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
        var sidebarGameName = document.getElementById('sidebarGameName');
        var sidebarTitle = document.getElementById('sidebarTitle');
        
        if (sidebarGameName) {
            sidebarGameName.textContent = 'Selecciona un paquete para continuar.';
        }
        if (sidebarTitle) {
            sidebarTitle.textContent = game.name;
        }

        var isWallet = game.category_slug === 'wallet';
        var isTarjetas = game.category_slug === 'tarjetas';
        var playerSection = document.getElementById('playerSection');
        var zoneGroup = document.getElementById('zoneIdGroup');
        var playerIdLabel = document.getElementById('playerIdLabel');
        var zoneIdLabel = document.getElementById('zoneIdLabel');
        var playerHint = document.getElementById('playerHint');
        var playerInput = document.getElementById('playerId');
        var playerInputType = getPlayerInputType(game);

        if (!playerSection) return;

        if (isWallet) {
            playerSection.style.display = 'block';
            if (playerIdLabel) playerIdLabel.textContent = 'Correo electrónico';
            if (playerInput) {
                playerInput.type = 'email';
                playerInput.placeholder = 'correo@ejemplo.com';
                playerInput.inputMode = 'email';
                playerInput.removeAttribute('pattern');
                playerInput.dataset.digitsOnly = '0';
            }
            if (playerHint) playerHint.textContent = 'Ingresa tu correo electrónico para recibir la recarga.';
            if (zoneGroup) zoneGroup.style.display = 'none';
        } else if (isTarjetas) {
            playerSection.style.display = 'none';
        } else {
            playerSection.style.display = 'block';
            if (playerIdLabel) {
                if (playerInputType === 'email') {
                    playerIdLabel.textContent = 'Correo electrónico';
                } else if (playerInputType === 'text') {
                    playerIdLabel.textContent = game.player_id_label || 'Dato del jugador';
                } else {
                    playerIdLabel.textContent = game.player_id_label || 'ID del jugador';
                }
            }
            if (playerInput) {
                playerInput.dataset.playerInputType = playerInputType;
                if (playerInputType === 'email') {
                    playerInput.type = 'email';
                    playerInput.placeholder = 'correo@ejemplo.com';
                    playerInput.inputMode = 'email';
                    playerInput.removeAttribute('pattern');
                    playerInput.dataset.digitsOnly = '0';
                } else if (playerInputType === 'text') {
                    playerInput.type = 'text';
                    playerInput.placeholder = 'Ingresa tu dato';
                    playerInput.inputMode = 'text';
                    playerInput.removeAttribute('pattern');
                    playerInput.dataset.digitsOnly = '0';
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
                if (playerInputType === 'email') {
                    playerHint.textContent = 'Ingresa correctamente tu correo electrónico para evitar errores en la recarga.';
                } else {
                    playerHint.textContent = 'Ingresa correctamente tu ' + (game.player_id_label || 'ID') + ' para evitar errores en la recarga.';
                }
            }

            if (game.requires_zone_id) {
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
            var bs = NaN;
            if (!isNaN(priceNum)) {
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
            if (totalBsEl) totalBsEl.classList.add('d-none');
        } else {
            var bs = getSelectedPaymentMethodUsesRate() ? (finalPrice * getGameBsRate()) : finalPrice;

            if (!isNaN(bs)) {
                totalEl.textContent = 'Bs ' + Math.round(bs).toLocaleString('es-VE');
            } else {
                totalEl.textContent = 'Bs 0';
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

            priceSpan.textContent = formatPackageAmount(finalUsd, currency, getGameBsRate());

            if (priceUsdSpan) {
                if (discountMeta) {
                    priceUsdSpan.textContent = formatPackageAmount(usd, currency, getGameBsRate());
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

    var walletNoticeModal = document.getElementById('walletNoticeModal');
    var walletNoticeAck = document.getElementById('walletNoticeAcknowledge');
    if (walletNoticeAck && walletNoticeModal) {
        walletNoticeAck.addEventListener('click', closeWalletNotice);
        walletNoticeModal.addEventListener('click', function (evt) {
            if (evt.target === walletNoticeModal) {
                closeWalletNotice();
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

    var pointsChangeIdBtnEl = document.getElementById('pointsChangeIdBtn');
    if (pointsChangeIdBtnEl) {
        pointsChangeIdBtnEl.addEventListener('click', resetPointsModal);
    }

    var pointsSpinBtnEl = document.getElementById('pointsSpinBtn');
    if (pointsSpinBtnEl) {
        pointsSpinBtnEl.addEventListener('click', handlePointsSpin);
    }

    if (rankingModalCloseBtn && rankingModal) {
        rankingModalCloseBtn.addEventListener('click', closeRankingModal);
        rankingModal.addEventListener('click', function (evt) {
            if (evt.target === rankingModal) {
                closeRankingModal();
            }
        });
    }

    if (supportModalOpenBtn) {
        supportModalOpenBtn.addEventListener('click', openSupportModal);
    }

    if (supportModalCloseBtn && supportModal) {
        supportModalCloseBtn.addEventListener('click', closeSupportModal);
        supportModal.addEventListener('click', function (evt) {
            if (evt.target === supportModal) {
                closeSupportModal();
            }
        });
    }

    if (rankingTabsEl) {
        rankingTabsEl.addEventListener('click', function (evt) {
            var tabBtn = evt.target && evt.target.closest('.ranking-tab');
            if (!tabBtn) return;
            rankingState.activeKey = tabBtn.dataset.rankingKey || null;
            renderRankingBoard();
        });
    }

    if (supportForm) {
        supportForm.addEventListener('submit', function (evt) {
            evt.preventDefault();

            var identity = supportIdentityInput ? String(supportIdentityInput.value || '').trim() : '';
            var gameName = supportGameInput ? String(supportGameInput.value || '').trim() : '';
            var reason = supportReasonInput ? String(supportReasonInput.value || '').trim() : '';
            var packageName = selectedPackage && selectedPackage.name ? String(selectedPackage.name).trim() : 'No especificado';

            if (!identity || !gameName || !reason) {
                return;
            }

            var lines = [
                'Hola, necesito soporte con un pedido de 3S Recargas.',
                '',
                'ID o correo: ' + identity,
                'Juego o servicio: ' + gameName,
                'Paquete: ' + packageName,
                'Motivo: ' + reason
            ];

            var url = buildWhatsAppSupportUrl(lines.join('\n'));
            window.open(url, '_blank', 'noopener');
            closeSupportModal();
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
            closeWalletNotice();
            closeDiscountInfoPopup();
            closeGameSelectionPopup();
            closeRankingModal();
            closeSupportModal();
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
       Se muestra cada vez que se entra al producto, que es lo que se pidió. */
    function openWalletNotice() {
        var modal = document.getElementById('walletNoticeModal');
        if (!modal) return;
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
    }

    function closeWalletNotice() {
        var modal = document.getElementById('walletNoticeModal');
        if (!modal) return;
        modal.style.display = 'none';
        modal.setAttribute('aria-hidden', 'true');
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

    // Inicializar cuando el DOM esté listo
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            console.log('DOM ready, running init...');
        });
    } else {
        console.log('DOM already ready');
    }

})();
