/* ── 3S Recargas — Store JavaScript ─────────────────────── */

(function () {
    'use strict';

    var activeGameId   = null;
    var activeCategory = window.ACTIVE_CATEGORY || 'juegos';
    var currentGame    = null;
    var selectedPackage = null;
    var usdRate = typeof window.USD_RATE_BS === 'number' ? window.USD_RATE_BS : 0;
    var defaultPackageId = (typeof window.DEFAULT_PACKAGE_ID === 'number' ? window.DEFAULT_PACKAGE_ID : null);
    var gamesGridEl = document.getElementById('gamesGrid');
    var gamesPrevBtn = document.getElementById('gamesPrev');
    var gamesNextBtn = document.getElementById('gamesNext');
    var applyDiscountBtn = document.getElementById('applyDiscountBtn');
    var discountApplyFeedback = document.getElementById('discountApplyFeedback');
    var manualInfoPopup = document.getElementById('manualInfoPopup');
    var manualInfoCloseBtn = document.getElementById('manualInfoCloseBtn');
    var discountInfoPopup = document.getElementById('discountInfoPopup');
    var discountInfoCloseBtn = document.getElementById('discountInfoCloseBtn');
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
    var supportForm = document.getElementById('supportForm');
    var supportIdentityInput = document.getElementById('supportOrderIdentity');
    var supportGameInput = document.getElementById('supportGame');
    var supportReasonInput = document.getElementById('supportReason');
    var rankingState = {
        loading: false,
        loaded: false,
        activeKey: null,
        items: []
    };

    function scrollGames(direction) {
        if (!gamesGridEl) return;
        var firstCard = gamesGridEl.querySelector('.game-card');
        var cardWidth = firstCard ? firstCard.offsetWidth + 8 : 180;
        gamesGridEl.scrollBy({ left: direction * cardWidth * 3, behavior: 'smooth' });
    }

    if (gamesPrevBtn) {
        gamesPrevBtn.addEventListener('click', function () { scrollGames(-1); });
    }
    if (gamesNextBtn) {
        gamesNextBtn.addEventListener('click', function () { scrollGames(1); });
    }

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
            loadGames(cat);
        });
    });

    /* ── Load Games via AJAX ──────────────────────────────── */
    function loadGames(category) {
        var grid = document.getElementById('gamesGrid');
        grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">Cargando...</div>';

        fetch('/api/games?category=' + encodeURIComponent(category))
            .then(function (r) { return r.json(); })
            .then(function (data) { renderGames(data.games); })
            .catch(function () {
                grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">Error al cargar juegos.</div>';
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
        var flag = String(selectedOption.getAttribute('data-flag') || '').trim();
        var code = String(selectedOption.value || '').trim();
        phoneCountryDisplay.textContent = (flag ? flag + ' ' : '') + code;

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

    function formatRewardValue(value) {
        if (value === null || typeof value === 'undefined' || value === '') {
            return 'Sin premio';
        }
        return String(value);
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
                        '<strong>' + escHtml(formatRewardValue(reward.reward_label)) + '</strong>' +
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
                        '<td' + prizeClass + '>' + escHtml(formatRewardValue(entry.prize_label || 'Sin premio')) + '</td>' +
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
                        '<span>' + escHtml(entry.prize_label || 'Sin premio') + '</span>' +
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
            } else {
                html += '<div class="ranking-current-hint">Ya estás en el primer puesto de este ranking.</div>';
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

    function openRankingModal() {
        if (!rankingModal) return;
        openModal(rankingModal);
        fetchRankings(false);
    }

    function closeRankingModal() {
        closeModal(rankingModal);
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

    /* ── Render Game Cards ────────────────────────────────── */
    function renderGames(games) {
        var grid = document.getElementById('gamesGrid');
        grid.innerHTML = '';

        if (!games || games.length === 0) {
            grid.innerHTML = '<div class="empty-state" style="grid-column:1/-1">No hay productos en esta categoría aún.</div>';
            return;
        }

        games.forEach(function (game) {
            var card = document.createElement('div');
            card.className = 'game-card' + (game.is_automated ? ' is-automated' : '');
            card.dataset.gameId   = game.id;
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
                applyGameToSidebar(data.game);
                renderPackages(data.packages);
            })
            .catch(function (err) {
                console.error('Error fetching packages:', err);
                document.getElementById('packagesGrid').innerHTML =
                    '<div class="empty-state" style="grid-column:1/-1">Error al cargar paquetes.</div>';
            });
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

        function buildItem(pkg) {
            var item = document.createElement('button');
            item.type      = 'button';
            item.className = 'package-item';
            item.dataset.packageId = pkg.id;

            var imgHtml = pkg.image
                ? '<img src="/static/uploads/' + escHtml(pkg.image) + '" alt="' + escHtml(pkg.name) + '">'
                : '<div class="pkg-img-placeholder">' + escHtml(pkg.name.charAt(0).toUpperCase()) + '</div>';

            var priceUsd = parseFloat(pkg.price);
            item.dataset.priceUsd = String(priceUsd);

            item.innerHTML =
                imgHtml +
                '<div class="pkg-info">' +
                    '<h4>' + escHtml(pkg.name) + '</h4>' +
                    '<span class="price"></span>' +
                    '<span class="price-usd"></span>' +
                '</div>';
            item.addEventListener('click', function () {
                selectPackage(pkg, item);
            });
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
            addSectionLabel('⚡ Automático 24/7', 'auto');
            autoPkgs.forEach(function (pkg) { grid.appendChild(buildItem(pkg)); });
        }

        if (manualPkgs.length > 0) {
            if (autoPkgs.length > 0) {
                var sep = document.createElement('div');
                sep.className = 'pkg-section-sep';
                grid.appendChild(sep);
            }
            addSectionLabel('Recarga manual', 'manual');
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
            btn.textContent = 'Revisar ID';
            btn.disabled = true;
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
        var uid = (input.value || '').trim();
        if (!uid) { if (!silent) setNickUIErr('Ingresa tu ID'); return; }
        if (!/^\d+$/.test(uid)) { if (!silent) setNickUIErr('El ID debe ser numérico'); return; }
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

    function setupVerifyListeners() {
        var btn = document.getElementById('btnVerifyPlayer');
        var input = document.getElementById('playerId');
        if (btn && !btn.dataset.verifyBound) {
            btn.setAttribute('aria-hidden', 'false');
            btn.dataset.verifyBound = '1';
        }
        if (input && !input.dataset.verifyBound) {
            input.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') { e.preventDefault(); doVerifyPlayer({ silent: false }); }
            });
            input.addEventListener('input', function() {
                var uid = (input.value || '').trim();
                if (!uid) { resetNickUI(); return; }
                resetNickUI();
                scheduleAutoVerify(900, false);
            });
            input.addEventListener('change', function() {
                scheduleAutoVerify(0, false);
            });
            input.addEventListener('blur', function() {
                var uid = (input.value || '').trim();
                if (!uid || uid === verifyState.lastUidVerified) return;
                scheduleAutoVerify(0, false);
            });
            input.dataset.verifyBound = '1';
        }
    }

    function updateVerifyUI(game) {
        var btn = document.getElementById('btnVerifyPlayer');
        var nicknameEl = document.getElementById('playerNickname');
        var hasVerify = game && game.scrape_enabled && (game.is_ff_verify || game.is_bs_verify);

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
            if (btn) btn.style.display = 'none';
            if (nicknameEl) { nicknameEl.textContent = ''; nicknameEl.style.display = 'none'; }
        }
    }

    /* ── Update Sidebar with Game Info ───────────────────── */
    function applyGameToSidebar(game) {
        currentGame = game;
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

        if (!playerSection) return;

        if (isWallet) {
            playerSection.style.display = 'block';
            if (playerIdLabel) playerIdLabel.textContent = 'Correo electrónico';
            if (playerInput) {
                playerInput.type = 'email';
                playerInput.placeholder = 'correo@ejemplo.com';
            }
            if (playerHint) playerHint.textContent = 'Ingresa tu correo electrónico para recibir la recarga.';
            if (zoneGroup) zoneGroup.style.display = 'none';
        } else if (isTarjetas) {
            playerSection.style.display = 'none';
        } else {
            playerSection.style.display = 'block';
            if (playerIdLabel) playerIdLabel.textContent = game.player_id_label || 'ID del jugador';
            if (playerInput) {
                playerInput.type = 'text';
                playerInput.placeholder = 'Ingresa tu ID';
            }
            if (playerHint) {
                playerHint.textContent = 'Ingresa correctamente tu ' + (game.player_id_label || 'ID') + ' para evitar errores en la recarga.';
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
        var priceNum = parseFloat(pkg.price);
        var currency = getSelectedPaymentCurrency();

        if (currency === 'usd') {
            submitLabel.textContent = 'Comprar — $' + (isNaN(priceNum) ? '0.00' : priceNum.toFixed(2));
        } else {
            var bs = NaN;
            if (!isNaN(priceNum)) {
                bs = getSelectedPaymentMethodUsesRate() ? (priceNum * usdRate) : priceNum;
            }
            submitLabel.textContent = 'Comprar — Bs ' + (isNaN(bs) ? '0' : Math.round(bs).toLocaleString('es-VE'));
        }
        updateTotals(pkg.price);
    }

    function getSelectedPaymentMethodUsesRate() {
        var checked = document.querySelector('input[name="payment_method"]:checked');
        if (!checked) return false;
        return checked.dataset.usesRate === '1';
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

        var currency = getSelectedPaymentCurrency();
        if (currency === 'usd') {
            totalEl.textContent = '$' + priceNum.toFixed(2);
            if (totalBsEl) totalBsEl.classList.add('d-none');
        } else {
            var bs = getSelectedPaymentMethodUsesRate() ? (priceNum * usdRate) : priceNum;

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
            setDiscountFeedback('Escribe un código para aplicarlo.', 'is-error');
            affInput.focus();
            return;
        }

        var packagePrice = selectedPackage ? parseFloat(selectedPackage.price) : NaN;
        var knownCode = !!(window.validDiscounts && window.validDiscounts[code]);
        var discountMeta = getValidDiscountMeta(code, packagePrice);

        if (discountMeta) {
            setDiscountFeedback('Descuento ' + code + ' aplicado.', 'is-success');
            return;
        }

        if (knownCode && selectedPackage) {
            setDiscountFeedback('Ese código existe, pero no aplica para este monto.', 'is-error');
        } else if (knownCode) {
            setDiscountFeedback('Código reconocido. Selecciona un paquete para calcular el descuento.', 'is-success');
        } else {
            setDiscountFeedback('Código no válido o inactivo.', 'is-error');
        }

        if (selectedPackage) {
            updateTotals(selectedPackage.price);
            updateSidebarForPackage(selectedPackage);
        }
        affInput.focus();
    }

    function refreshPackagePriceViews() {
        var items = document.querySelectorAll('.package-item');
        var currency = getSelectedPaymentCurrency();
        items.forEach(function (item) {
            var priceSpan = item.querySelector('.price');
            var priceUsdSpan = item.querySelector('.price-usd');
            var usdStr = item.dataset.priceUsd;
            if (!priceSpan || !usdStr) return;
            var usd = parseFloat(usdStr);
            if (isNaN(usd)) return;

            if (currency === 'usd') {
                priceSpan.textContent = '$' + usd.toFixed(2);
            } else if (usdRate) {
                var bs = usd * usdRate;
                priceSpan.textContent = 'Bs ' + Math.round(bs).toLocaleString('es-VE');
            } else {
                priceSpan.textContent = 'Bs 0';
            }

            if (priceUsdSpan) {
                priceUsdSpan.textContent = '';
                priceUsdSpan.style.display = 'none';
            }
        });

        if (selectedPackage) {
            updateTotals(selectedPackage.price);
        }
    }

    /* ── Close Packages Panel ─────────────────────────────── */
    window.closePackages = function () {
        var panel = document.getElementById('packagesPanel');
        if (panel) panel.style.display = 'none';
        activeGameId = null;
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

    // Seleccionar automáticamente el primer juego al cargar
    if (initialCards.length > 0) {
        handleGameClick(initialCards[0]);
    }

    /* ── Affiliate code: auto-uppercase ──────────────────── */
    var affInput = document.getElementById('affiliate_code');
    if (affInput) {
        affInput.addEventListener('input', function () {
            this.value = this.value.toUpperCase();
            if (!this.value.trim()) {
                setDiscountFeedback('', null);
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

    if (rankingModalOpenBtn) {
        rankingModalOpenBtn.addEventListener('click', openRankingModal);
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
            closeDiscountInfoPopup();
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
            
            // Validar player ID solo si no es wallet ni tarjetas
            if (currentGame && currentGame.category_slug !== 'wallet' && currentGame.category_slug !== 'tarjetas') {
                var playerIdInput = document.getElementById('playerId');
                if (playerIdInput && !playerIdInput.value.trim()) {
                    e.preventDefault();
                    alert('Por favor ingresa tu ID del jugador.');
                    playerIdInput.focus();
                    return;
                }
            }
            
            // Validar correo/teléfono en wallet
            if (currentGame && currentGame.category_slug === 'wallet') {
                var walletInput = document.getElementById('playerId');
                if (walletInput && !walletInput.value.trim()) {
                    e.preventDefault();
                    alert('Por favor ingresa tu correo electrónico.');
                    walletInput.focus();
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
