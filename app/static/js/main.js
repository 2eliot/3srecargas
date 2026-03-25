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
        var grid  = document.getElementById('gamesGrid');
        var panel = document.getElementById('packagesPanel');
        var section = document.getElementById('gamesSection');

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

        if (section && panel.parentNode !== section) {
            section.appendChild(panel);
        }

        var carousel = document.querySelector('.games-carousel');
        if (section && carousel && carousel.nextSibling !== panel) {
            section.insertBefore(panel, carousel.nextSibling);
        }

        panel.style.display = 'block';
        panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

        var firstButton = null;
        var firstPkgForDefault = null;

        packages.forEach(function (pkg) {
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

            grid.appendChild(item);

            if (!firstButton) {
                firstButton = item;
                firstPkgForDefault = pkg;
            }

            if (defaultPackageId && pkg.id === defaultPackageId) {
                firstButton = item;
                firstPkgForDefault = pkg;
            }
        });

        // Reset any previous selection when loading new packages
        selectedPackage = null;
        updateSidebarForPackage(null);
        refreshPackagePriceViews();

        // Auto-seleccionar el primer paquete (o el configurado)
        if (firstButton && firstPkgForDefault) {
            selectPackage(firstPkgForDefault, firstButton);
        }
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
            var bs = (usdRate && !isNaN(priceNum)) ? (priceNum * usdRate) : NaN;
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

        // Obtener código de descuento
        var discountCode = document.getElementById('affiliate_code') ? 
            document.getElementById('affiliate_code').value.trim().toUpperCase() : '';
        
        // Aplicar descuento si hay un código válido
        var finalAmount = priceNum;
        var discountAmount = 0;
        
        if (discountCode && window.validDiscounts && window.validDiscounts[discountCode]) {
            var discount = window.validDiscounts[discountCode];
            if (discount.discount_type === 'percentage') {
                discountAmount = priceNum * parseFloat(discount.discount_value) / 100;
                if (discount.max_discount && discountAmount > parseFloat(discount.max_discount)) {
                    discountAmount = parseFloat(discount.max_discount);
                }
            } else { // fixed
                discountAmount = parseFloat(discount.discount_value);
                if (discountAmount > priceNum) {
                    discountAmount = priceNum;
                }
            }
            finalAmount = priceNum - discountAmount;
        }

        var currency = getSelectedPaymentCurrency();
        if (currency === 'usd') {
            if (discountAmount > 0) {
                totalEl.innerHTML = '<span style="text-decoration: line-through; color: #999;">$' + priceNum.toFixed(2) + '</span> $' + finalAmount.toFixed(2) + ' <span style="color: var(--accent); font-size: 12px;">(Ahorrado: $' + discountAmount.toFixed(2) + ')</span>';
            } else {
                totalEl.textContent = '$' + finalAmount.toFixed(2);
            }
            if (totalBsEl) totalBsEl.classList.add('d-none');
        } else {
            if (usdRate) {
                var bs = finalAmount * usdRate;
                var originalBs = priceNum * usdRate;
                if (discountAmount > 0) {
                    totalEl.innerHTML = '<span style="text-decoration: line-through; color: #999;">Bs ' + Math.round(originalBs).toLocaleString('es-VE') + '</span> Bs ' + Math.round(bs).toLocaleString('es-VE') + ' <span style="color: var(--accent); font-size: 12px;">(Ahorrado: Bs ' + Math.round(discountAmount * usdRate).toLocaleString('es-VE') + ')</span>';
                } else {
                    totalEl.textContent = 'Bs ' + Math.round(bs).toLocaleString('es-VE');
                }
            } else {
                totalEl.textContent = 'Bs 0';
            }
            if (totalBsEl) {
                totalBsEl.classList.add('d-none');
            }
        }
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
        });
    }

    /* ── Quick checkout form submit UX ───────────────────── */
    var quickForm = document.getElementById('quickCheckoutForm');
    if (quickForm) {
        quickForm.addEventListener('submit', function (e) {
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
