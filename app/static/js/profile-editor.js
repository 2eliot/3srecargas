/* ── 3S Recargas — editor del perfil público ────────────────────────────────────────────────────────
 *
 * La vista previa es la MISMA tarjeta de la página pública (profiles/_scope.html): se dibuja con variables CSS y atributos
 * data-* en #pfScope. Aquí se leen los campos del formulario y se recalculan esas variables en vivo; es el espejo de
 * theme_vars() / theme_attrs() de app/utils/profiles.py (si se cambia uno hay que cambiar el otro). El servidor vuelve a
 * validar TODO al guardar: lo de aquí es solo para que se vea al instante.
 *
 * Cada grupo de opciones (presentación, foto, banner, fondo, colores) vive en una ventana (popup) que muestra el perfil PEQUEÑO
 * arriba y las opciones abajo: al abrirla, la vista previa se MUEVE a la ventana (es el mismo elemento, no una copia) y al cerrarla
 * vuelve a su lugar. Los marcos son imágenes: los de la tienda (todos gratis), uno propio o ninguno. Un perfil puede tener como
 * máximo MAX imágenes propias (el servidor lo vuelve a comprobar).
 */
(function () {
    'use strict';

    var form = document.getElementById('pfForm');
    var scope = document.getElementById('pfScope');
    if (!form || !scope) return;

    var FONTS = {
        outfit: "'Outfit', sans-serif", space: "'Archivo Black', 'Arial Black', sans-serif", cinzel: "'Cinzel', serif",
        orbitron: "'Orbitron', sans-serif", pacifico: "'Pacifico', cursive"
    };
    // Espejo de profile_social.level_ring(): aro automático según el nivel del mes (lo único que queda de los aros de color)
    var LEVEL_RINGS = {
        bronce: { style: 'gradient', c1: '#cd7f32', c2: '#8a4b14', w: 4, glow: 8, anim: 'none' },
        plata: { style: 'gradient', c1: '#e5e7eb', c2: '#9ca3af', w: 4, glow: 10, anim: 'none' },
        oro: { style: 'gradient', c1: '#ffe08a', c2: '#b8860b', w: 4, glow: 12, anim: 'none' },
        diamante: { style: 'gradient', c1: '#67e8f9', c2: '#3b82f6', w: 4, glow: 14, anim: 'none' },
        leyenda: { style: 'conic', c1: '#ff1a2e', c2: '#7a0010', w: 5, glow: 16, anim: 'spin' }
    };
    var NO_RING = { style: 'none', c1: '#ffffff', c2: '#ffffff', w: 0, glow: 0, anim: 'none' };
    var handleEl = scope.querySelector('.pf-handle');
    var HANDLE = handleEl ? handleEl.textContent.replace(/^@/, '') : '';
    var MAX_IMAGES = parseInt(form.getAttribute('data-max-images'), 10) || 5;
    var saveBar = document.getElementById('pfSaveBar');
    var dirty = false;
    var submitting = false;

    function field(name) { return form.elements[name]; }
    function value(name) { var el = field(name); return el ? el.value : ''; }
    function num(name, fallback) { var n = parseFloat(value(name)); return isNaN(n) ? fallback : n; }
    function checked(name) { var el = field(name); return !!(el && el.checked); }
    function radioValue(name) {
        var el = form.querySelector('input[name="' + name + '"]:checked');
        return el ? el.value : '';
    }

    /* ── Variables y atributos de la tarjeta ── */
    function applyTheme() {
        var auto = checked('avatar_ring_auto') && LEVEL_RINGS[scope.getAttribute('data-level') || ''];
        var ring = auto ? LEVEL_RINGS[scope.getAttribute('data-level')] : NO_RING;
        var ringNone = ring.style === 'none';
        // "Cuánto sobresale" del marco de la portada: solo cuenta si hay un marco puesto (el servidor hace lo mismo en theme_vars / theme_attrs)
        var bnExtra = Math.max(0, Math.min(50, Math.round(num('banner_ov_extra', 0))));
        var bnPop = bnExtra > 0 && !!frameUrlFor('banner');
        var vars = {
            '--bn-ov-extra': bnPop ? String(Math.round(bnExtra) / 100) : '0',
            '--pf-accent': value('accent'), '--pf-name': value('name_color'), '--pf-font': FONTS[value('font')] || FONTS.outfit,
            '--av-c1': ring.c1, '--av-c2': ring.c2,
            '--av-w': ringNone ? '0px' : ring.w + 'px', '--av-glow': ringNone ? '0px' : ring.glow + 'px',
            '--av-ov-scale': String(num('avatar_ov_scale', 140) / 100), '--av-ov-x': num('avatar_ov_x', 0) + '%', '--av-ov-y': num('avatar_ov_y', 0) + '%',
            '--bn-c1': value('banner_c1'), '--bn-c2': value('banner_c2'), '--bn-angle': num('banner_angle', 135) + 'deg',
            '--bn-fw': '0px', '--bn-fglow': '0px',
            '--bg-c1': value('background_c1'), '--bg-c2': value('background_c2'), '--bg-angle': num('background_angle', 160) + 'deg',
            '--bg-dim': String(num('background_dim', 35) / 100), '--bg-blur': num('background_blur', 0) + 'px'
        };
        Object.keys(vars).forEach(function (name) { scope.style.setProperty(name, vars[name]); });
        scope.setAttribute('data-shape', value('avatar_shape'));
        scope.setAttribute('data-ring', ring.style);
        scope.setAttribute('data-ring-anim', ring.anim);
        scope.setAttribute('data-frame', 'none');
        scope.setAttribute('data-bn-pop', bnPop ? 'on' : 'off');
        scope.setAttribute('data-bg', value('background_mode'));
    }

    /* ── Nombre, "acerca de mí" y fecha de registro ── */
    // Mismo criterio que contact_filter.py (el servidor decide al guardar): lo que parezca un enlace, red social, correo, teléfono o ID se
    // TACHA con asteriscos. Aquí se calcula igual para que la vista previa lo muestre al instante y avise junto al campo.
    var TLDS = '(?:com|net|org|gg|me|io|tv|ly|co|ve|app|link|bio|xyz|info|site|online|store|shop|page|dev|cc|us|es|mx|ar|cl|pe)';
    var URL_RE = new RegExp('(https?://|www\\.|\\b[a-z0-9-]{2,}\\.' + TLDS + '\\b|\\bt\\.me\\b|\\bwa\\.me\\b)', 'i');
    var EMAIL_RE = /[^\s@]+@[^\s@]+\.[^\s@]+/g;
    var HANDLE_RE = /(^|[^\w])@[\w.]{2,}/g;
    var DIGITS_RE = /(?:\d[\s.\-_]*){6,}/g;
    var PLATFORMS = ['instagram', 'facebook', 'whatsapp', 'telegram', 'discord', 'youtube', 'twitter', 'snapchat', 'tiktok', 'twitch'];
    var PLATFORM_RES = PLATFORMS.map(function (name) { return new RegExp(name.split('').join('[\\W_]*'), 'gi'); });
    function stars(text) { return text.replace(/\S/g, '*'); }
    function maskContact(text) {
        var v = String(text || '');
        if (!v.trim()) return v;
        v = v.replace(EMAIL_RE, stars);
        v = v.replace(/\S+/g, function (w) { return URL_RE.test(w.toLowerCase()) ? stars(w) : w; });
        v = v.replace(HANDLE_RE, function (m, lead) { return lead + stars(m.slice(lead.length)); });
        v = v.replace(DIGITS_RE, function (m) {
            var core = m.replace(/[\s.\-_]+$/, '');
            return stars(core) + m.slice(core.length);
        });
        PLATFORM_RES.forEach(function (re) { v = v.replace(re, stars); });
        return v;
    }
    function hasContact(text) { return maskContact(text) !== String(text || ''); }
    function showRuleWarnings() {
        ['display_name', 'bio', 'status_text'].forEach(function (name) {
            var el = field(name), warn = document.getElementById('warn_' + name);
            if (!el || !warn) return;
            warn.hidden = !hasContact(el.value);
        });
    }

    function applyText() {
        showRuleWarnings();
        var name = maskContact(value('display_name')).replace(/\s+/g, ' ').trim() || HANDLE;
        if (!/[A-Za-z0-9À-ɏ]/.test(name)) name = HANDLE;   // si no queda nada legible (era todo un ID o teléfono), se usa el usuario
        var nameEl = document.getElementById('pfName');
        if (nameEl) nameEl.textContent = name;
        var statusEl = document.getElementById('pfStatus');
        if (statusEl) {
            var status = maskContact(value('status_text')).replace(/\s+/g, ' ').trim();
            statusEl.textContent = status;
            statusEl.hidden = !status;
        }
        var statsWrap = document.getElementById('pfStatsWrap'), titleEl = document.getElementById('pfTitle'), xpLevelEl = document.getElementById('pfXpLevel');
        if (statsWrap) statsWrap.hidden = !checked('show_stats');
        if (titleEl) titleEl.hidden = !checked('show_stats');
        if (xpLevelEl) xpLevelEl.hidden = !checked('show_stats');
        var letter = scope.querySelector('.pf-letter');
        if (letter) letter.textContent = (name.charAt(0) || '?').toUpperCase();
        var bio = maskContact(value('bio')).replace(/\r/g, '').trim();
        var bioEl = document.getElementById('pfBio');
        if (bioEl) {
            bioEl.textContent = bio || 'Aún no escribes nada. ¡Cuéntales quién eres!';
            bioEl.classList.toggle('is-empty', !bio);
        }
        var counter = document.getElementById('bioCount');
        if (counter) counter.textContent = value('bio').length + ' / ' + (field('bio').maxLength || 300);
        Array.prototype.forEach.call(scope.querySelectorAll('[data-chip="since"]'), function (chip) { chip.hidden = !checked('show_since'); });
    }

    /* ── Imágenes propias (se ven en la vista previa antes de guardar) ── */
    var KINDS = ['avatar', 'banner', 'background', 'avatar_frame', 'banner_frame'];
    var media = {};
    function frameWrap(kind) { return form.querySelector('[data-frame-kind="' + kind + '"]'); }
    // La imagen PROPIA que ya estaba guardada de cada tipo (la lee de su zona "Elegir imagen", no de la vista previa: así una foto, portada o fondo de la
    // tienda de puntos que ya esté puesto no se confunde con una imagen propia ni cuenta en el tope de 5).
    function dropOrig(kind) {
        var zone = form.querySelector('[data-file-kind="' + kind + '"] [data-drop]');
        return { url: zone ? (zone.getAttribute('data-orig-url') || '') : '', video: !!zone && zone.getAttribute('data-orig-video') === '1' };
    }
    function originalMedia() {
        var avWrap = frameWrap('avatar'), bnWrap = frameWrap('banner');
        var bg = dropOrig('background');
        return {
            avatar: dropOrig('avatar').url,
            banner: dropOrig('banner').url,
            background: bg.video ? '' : bg.url,
            backgroundVideo: bg.video ? bg.url : '',
            avatar_frame: avWrap ? (avWrap.getAttribute('data-own-url') || '') : '',
            banner_frame: bnWrap ? (bnWrap.getAttribute('data-own-url') || '') : ''
        };
    }
    var orig = originalMedia();
    KINDS.forEach(function (kind) { media[kind] = { file: '', removed: false, isVideo: false }; });

    // Lo propio: la imagen que la cuenta subió (recién elegida o ya guardada). Es lo que cuenta para el tope de 5 imágenes.
    function ownEffective(kind) {
        if (media[kind].file) return media[kind].file;
        if (media[kind].removed) return '';
        // el fondo puede ser un VIDEO (MP4 o WEBM): se guarda aparte porque no se dibuja como imagen de fondo
        return orig[kind] || (kind === 'background' ? orig.backgroundVideo : '');
    }
    // Foto, portada o fondo de la tienda de puntos elegido en "Ver mis fotos / fondos" (el radio marcado que no es "Mi ..."), o null.
    var SHOP_CHOICE = { avatar: 'avatar_photo_choice', banner: 'banner_photo_choice', background: 'background_choice' };
    function shopRadio(kind) {
        var name = SHOP_CHOICE[kind];
        var radio = name ? form.querySelector('input[name="' + name + '"]:checked') : null;
        return radio && radio.value !== 'own' ? radio : null;
    }
    // Lo que se VE: lo de la tienda si está elegido; si no, lo propio.
    function effective(kind) {
        var radio = shopRadio(kind);
        if (radio && radio.getAttribute('data-asset-url')) return radio.getAttribute('data-asset-url');
        return ownEffective(kind);
    }
    // ¿lo que se ve de fondo es un video? (el de la tienda elegido, el recién elegido, o el que ya estaba guardado)
    function backgroundIsVideo() {
        var radio = shopRadio('background');
        if (radio) return radio.getAttribute('data-asset-video') === '1';
        if (media.background.file) return media.background.isVideo;
        return !media.background.removed && !orig.background && !!orig.backgroundVideo;
    }
    function setBackgroundVideo(url) {
        var el = scope.querySelector('.pf-bg-video');
        if (!el) return;
        if (url) {
            if (el.getAttribute('src') !== url) el.setAttribute('src', url);
            el.hidden = false;
            try { var p = el.play(); if (p && p.catch) p.catch(function () { /* el navegador no deja reproducir solo */ }); } catch (_) { /* nada */ }
        } else {
            try { el.pause(); } catch (_) { /* nada */ }
            el.hidden = true;
            el.removeAttribute('src');
        }
    }

    // El marco que se dibuja encima: el de la tienda elegido, el propio, o ninguno.
    function frameUrlFor(kind) {
        var choice = radioValue(kind + '_frame_choice');
        if (!choice || choice === 'none') return '';
        if (choice === 'own') return effective(kind + '_frame');
        var radio = form.querySelector('input[name="' + kind + '_frame_choice"][value="' + choice + '"]');
        return radio ? (radio.getAttribute('data-frame-url') || '') : '';
    }

    function setChoice(kind, choice) {
        Array.prototype.forEach.call(form.querySelectorAll('input[name="' + kind + '_frame_choice"]'), function (radio) { radio.checked = radio.value === choice; });
    }

    // La tarjeta "Mi marco" solo existe si hay un marco propio (ya guardado o recién elegido); si se quita, la selección vuelve a "Sin marco".
    function syncOwnCards() {
        ['avatar', 'banner'].forEach(function (kind) {
            var wrap = frameWrap(kind);
            if (!wrap) return;
            var card = wrap.querySelector('[data-own-card]');
            var has = !!effective(kind + '_frame');
            if (card) {
                card.hidden = !has;
                var radio = card.querySelector('input'), img = card.querySelector('img');
                if (has && radio) radio.setAttribute('data-frame-url', effective(kind + '_frame'));
                if (has && img && img.getAttribute('src') !== effective(kind + '_frame')) img.setAttribute('src', effective(kind + '_frame'));
            }
            if (!has && radioValue(kind + '_frame_choice') === 'own') setChoice(kind, 'none');
        });
    }

    function applyMedia() {
        // foto de perfil
        var av = document.getElementById('pfAvatar');
        var avUrl = effective('avatar');
        if (av) {
            var img = av.querySelector('img');
            if (avUrl) {
                if (!img) { av.textContent = ''; img = document.createElement('img'); img.alt = ''; av.appendChild(img); }
                if (img.getAttribute('src') !== avUrl) img.setAttribute('src', avUrl);
            } else if (img) {
                av.textContent = '';
                var span = document.createElement('span');
                span.className = 'pf-letter';
                av.appendChild(span);
                applyText();
            }
        }
        // banner y fondo (variables CSS con url())
        [['banner', '--bn-image'], ['background', '--bg-image']].forEach(function (pair) {
            var url = effective(pair[0]);
            if (pair[0] === 'background') {
                if (backgroundIsVideo()) { scope.style.removeProperty(pair[1]); setBackgroundVideo(url); return; }
                setBackgroundVideo('');
            }
            if (!url) scope.style.removeProperty(pair[1]);
            else scope.style.setProperty(pair[1], /^url\(/.test(url) ? url : "url('" + url + "')");
        });
        // marcos (imágenes encima de la foto y del banner)
        syncOwnCards();
        [['avatar', 'pfAvOverlay'], ['banner', 'pfBnOverlay']].forEach(function (pair) {
            var el = document.getElementById(pair[1]);
            var url = frameUrlFor(pair[0]);
            if (!el) return;
            if (url) { el.setAttribute('src', url); el.hidden = false; } else { el.hidden = true; el.removeAttribute('src'); }
        });
        applyImageLimit();
    }

    // Tope de imágenes propias: con el máximo ya subido, los espacios vacíos se apagan (para subir otra hay que quitar una).
    function applyImageLimit() {
        var count = KINDS.filter(function (kind) { return !!ownEffective(kind); }).length;
        Array.prototype.forEach.call(document.querySelectorAll('[data-image-count]'), function (out) { out.textContent = String(count); });
        Array.prototype.forEach.call(document.querySelectorAll('[data-image-meter]'), function (out) { out.classList.toggle('is-full', count >= MAX_IMAGES); });
        KINDS.forEach(function (kind) {
            var input = document.getElementById('f_' + kind);
            if (!input) return;
            var blocked = count >= MAX_IMAGES && !ownEffective(kind);
            input.disabled = blocked;
            var wrap = input.closest('[data-file-kind]');
            var note = wrap ? wrap.querySelector('.pf-file-limit') : null;
            if (note) note.hidden = !blocked;
            var zone = wrap ? wrap.querySelector('[data-drop]') : null;
            if (zone) zone.classList.toggle('is-disabled', blocked);
            renderDrop(kind);
        });
    }

    // Al elegir un marco de portada más ALTO que la portada (proporción 5:2 = 0.4 de alto por cada 1 de ancho), la barra "Cuánto sobresale" se pone sola
    // en lo que pide la imagen: (alto / ancho / 0.4 - 1) / 2, entre 0 y 50 %. Un marco normal (5:2) la deja en 0. Se puede cambiar a mano.
    function suggestBannerExtra(url) {
        var probe = new Image();
        probe.onload = function () {
            if (!probe.naturalWidth) return;
            var extra = Math.round(((probe.naturalHeight / probe.naturalWidth) / 0.4 - 1) / 2 * 100);
            extra = Math.max(0, Math.min(50, extra));
            var el = field('banner_ov_extra'), note = document.getElementById('bnExtraNote');
            if (!el) return;
            if (Math.round(num('banner_ov_extra', 0)) !== extra) {
                el.value = String(extra);
                refresh();
                markDirty();
            }
            if (note) {
                note.hidden = false;
                note.textContent = extra > 0
                    ? 'Tu imagen mide ' + probe.naturalWidth + ' × ' + probe.naturalHeight + ': puse «Cuánto sobresale» en ' + extra + ' % para que la franja del centro encaje con la portada.'
                    : 'Tu imagen mide ' + probe.naturalWidth + ' × ' + probe.naturalHeight + ': tiene la proporción de la portada, así que no sobresale (0 %). Para que algo sobresalga hazla más alta, por ejemplo 1500 × 1200.';
            }
        };
        probe.src = url;
    }

    /* ── Zona "Elegir imagen": miniatura, nombre y peso de lo elegido (o la imagen actual) y arrastrar-soltar ── */
    var DROP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="1.7"/><path d="m21 15-5-5L5 21"/></svg>';
    function fmtSize(bytes) { return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB'; }
    function renderDrop(kind) {
        var wrap = form.querySelector('[data-file-kind="' + kind + '"]');
        var zone = wrap ? wrap.querySelector('[data-drop]') : null;
        if (!zone) return;
        var thumb = zone.querySelector('[data-drop-thumb]'), title = zone.querySelector('[data-drop-title]'), sub = zone.querySelector('[data-drop-sub]');
        var input = wrap.querySelector('input[type="file"]');
        var orig = zone.getAttribute('data-orig-url') || '';
        var file = input && input.files && input.files[0];
        var hasNew = !!media[kind].file && !!file;
        var removed = !hasNew && !!media[kind].removed;
        zone.classList.toggle('has-file', hasNew);
        zone.classList.toggle('is-removed', removed);
        var shown = hasNew ? media[kind].file : orig;
        var shownIsVideo = hasNew ? !!media[kind].isVideo : zone.getAttribute('data-orig-video') === '1';
        if (thumb) {
            var img = thumb.querySelector('img');
            var vid = thumb.querySelector('video');
            if (shown && shownIsVideo) {
                if (!vid) {
                    thumb.textContent = '';
                    vid = document.createElement('video');
                    vid.muted = true; vid.loop = true; vid.autoplay = true; vid.playsInline = true;
                    vid.setAttribute('aria-hidden', 'true');
                    thumb.appendChild(vid);
                }
                if (vid.getAttribute('src') !== shown) { vid.setAttribute('src', shown); try { vid.play(); } catch (_) { /* nada */ } }
            } else if (shown) {
                if (vid) { thumb.textContent = ''; img = null; }
                if (!img) { thumb.textContent = ''; img = document.createElement('img'); img.alt = ''; thumb.appendChild(img); }
                if (img.getAttribute('src') !== shown) img.setAttribute('src', shown);
            } else if (img || vid || !thumb.querySelector('svg')) {
                thumb.innerHTML = DROP_ICON;
            }
        }
        if (hasNew) { title.textContent = file.name; sub.textContent = fmtSize(file.size) + ' · se guarda al presionar Guardar'; }
        else if (removed) { title.textContent = 'Se quitará al guardar'; sub.textContent = 'Toca para elegir otra en su lugar'; }
        else if (orig) { title.textContent = 'Cambiar imagen'; sub.textContent = 'Imagen actual · toca o arrastra otra'; }
        else { title.textContent = 'Elegir imagen'; sub.textContent = 'Toca aquí o arrastra tu imagen'; }
    }
    Array.prototype.forEach.call(form.querySelectorAll('[data-drop]'), function (zone) {
        var input = zone.querySelector('input[type="file"]');
        ['dragenter', 'dragover'].forEach(function (name) {
            zone.addEventListener(name, function (evt) { if (input.disabled) return; evt.preventDefault(); zone.classList.add('is-over'); });
        });
        ['dragleave', 'dragend'].forEach(function (name) { zone.addEventListener(name, function () { zone.classList.remove('is-over'); }); });
        zone.addEventListener('drop', function (evt) {
            evt.preventDefault();
            zone.classList.remove('is-over');
            if (input.disabled) return;
            var dropped = evt.dataTransfer && evt.dataTransfer.files && evt.dataTransfer.files[0];
            if (!dropped) return;
            try { var dt = new DataTransfer(); dt.items.add(dropped); input.files = dt.files; } catch (_) { return; }
            input.dispatchEvent(new Event('change', { bubbles: true }));
        });
    });

    Array.prototype.forEach.call(form.querySelectorAll('[data-live-file]'), function (input) {
        var wrap = input.closest('[data-file-kind]');
        var kind = wrap.getAttribute('data-file-kind');
        var max = parseFloat(wrap.getAttribute('data-max-mb')) || 4;
        var errorEl = wrap.querySelector('.pf-file-error');
        input.addEventListener('change', function () {
            var file = input.files && input.files[0];
            if (errorEl) { errorEl.hidden = true; errorEl.textContent = ''; }
            if (media[kind].file) { try { URL.revokeObjectURL(media[kind].file); } catch (_) { /* nada */ } }
            media[kind].file = '';
            media[kind].isVideo = false;
            if (file) {
                var videoMax = parseFloat(wrap.getAttribute('data-video-max-mb')) || 0;
                var isVideoFile = !!videoMax && (/^video\/(mp4|webm)$/.test(file.type) || /\.(mp4|webm)$/i.test(file.name));
                if (isVideoFile && file.size > videoMax * 1024 * 1024) {
                    input.value = '';
                    if (errorEl) { errorEl.textContent = 'Ese video pesa ' + (file.size / 1048576).toFixed(1) + ' MB: el máximo es ' + videoMax + ' MB. Usa uno más corto o más liviano.'; errorEl.hidden = false; }
                } else if (!isVideoFile && file.size > max * 1024 * 1024) {
                    input.value = '';
                    if (errorEl) { errorEl.textContent = 'Esa imagen pesa ' + (file.size / 1048576).toFixed(1) + ' MB: el máximo es ' + max + ' MB.'; errorEl.hidden = false; }
                } else if (!isVideoFile && !/^image\//.test(file.type)) {
                    input.value = '';
                    if (errorEl) { errorEl.textContent = videoMax ? 'Ese archivo no es una imagen ni un video MP4 o WEBM.' : 'Ese archivo no es una imagen.'; errorEl.hidden = false; }
                } else {
                    media[kind].file = URL.createObjectURL(file);
                    media[kind].isVideo = isVideoFile;
                    media[kind].removed = false;
                    var removeBox = wrap.querySelector('[data-live-remove]');
                    if (removeBox) removeBox.checked = false;   // elegir una nueva anula "Quitar la imagen actual"
                    if (kind === 'background' && (value('background_mode') === 'gradient' || value('background_mode') === 'color')) setBackgroundMode('image');
                    // Subir un marco propio lo pone puesto al instante.
                    if (kind === 'avatar_frame') { syncOwnCards(); setChoice('avatar', 'own'); }
                    if (kind === 'banner_frame') { syncOwnCards(); setChoice('banner', 'own'); suggestBannerExtra(media[kind].file); }
                }
            }
            refresh();
            markDirty();
        });
    });
    Array.prototype.forEach.call(form.querySelectorAll('[data-live-remove]'), function (box) {
        box.addEventListener('change', function () {
            media[box.getAttribute('data-live-remove')].removed = box.checked;
            refresh();
            markDirty();
        });
    });

    /* ── Valores que se muestran junto a las barras y colores ── */
    function applyReadouts() {
        Array.prototype.forEach.call(form.querySelectorAll('[data-val-for]'), function (out) {
            var el = document.getElementById(out.getAttribute('data-val-for'));
            if (el) out.textContent = el.value + (out.getAttribute('data-unit') || '').replace(/^ /, ' ');
        });
        Array.prototype.forEach.call(form.querySelectorAll('[data-hex-for]'), function (out) {
            var el = document.getElementById(out.getAttribute('data-hex-for'));
            if (el) out.textContent = el.value;
        });
        // relleno rojo de las barras hasta donde está la perilla
        Array.prototype.forEach.call(form.querySelectorAll('.pf-range input[type="range"]'), function (rng) {
            var lo = parseFloat(rng.min), hi = parseFloat(rng.max), cur = parseFloat(rng.value);
            var pct = hi > lo ? Math.max(0, Math.min(100, (cur - lo) / (hi - lo) * 100)) : 0;
            rng.style.setProperty('--pct', pct + '%');
        });
        // los botones de color (selector moderno) se repintan si el color cambió por código (estilos listos, círculos de color...)
        if (window.ModernColor) window.ModernColor.sync();
    }

    /* ── Estilos listos (banner, fondo y color principal) ── */
    var PRESET_PREFIX = { banner: 'banner_', background: 'background_' };
    function presetField(group, key) { return PRESET_PREFIX[group] + key; }
    function markPresets() {
        Array.prototype.forEach.call(form.querySelectorAll('[data-presets]'), function (row) {
            var group = row.getAttribute('data-presets');
            Array.prototype.forEach.call(row.querySelectorAll('[data-preset]'), function (btn) {
                var preset = JSON.parse(btn.getAttribute('data-preset'));
                var on = Object.keys(preset).every(function (key) {
                    if (key === 'name') return true;
                    var el = field(presetField(group, key));
                    return el && String(el.value).toLowerCase() === String(preset[key]).toLowerCase();
                });
                btn.classList.toggle('is-on', on);
            });
        });
        Array.prototype.forEach.call(form.querySelectorAll('[data-swatches] .pf-swatch'), function (sw) {
            sw.classList.toggle('is-on', value('accent').toLowerCase() === sw.getAttribute('data-color').toLowerCase());
        });
    }
    Array.prototype.forEach.call(form.querySelectorAll('[data-presets]'), function (row) {
        var group = row.getAttribute('data-presets');
        row.addEventListener('click', function (evt) {
            var btn = evt.target.closest('[data-preset]');
            if (!btn) return;
            var preset = JSON.parse(btn.getAttribute('data-preset'));
            Object.keys(preset).forEach(function (key) {
                if (key === 'name') return;
                var el = field(presetField(group, key));
                if (el) el.value = preset[key];
            });
            refresh();
            markDirty();
        });
    });
    Array.prototype.forEach.call(form.querySelectorAll('[data-swatches]'), function (row) {
        row.addEventListener('click', function (evt) {
            var sw = evt.target.closest('.pf-swatch');
            if (!sw) return;
            field('accent').value = sw.getAttribute('data-color');
            refresh();
            markDirty();
        });
    });

    // Una imagen de fondo solo se ve con el tipo "Mi imagen": al elegirla se pone sola, y si hay imagen pero el tipo no la usa, se avisa.
    function setBackgroundMode(mode) {
        Array.prototype.forEach.call(form.querySelectorAll('input[name="background_mode"]'), function (radio) { radio.checked = radio.value === mode; });
    }
    function updateBackgroundHint() {
        var hint = document.getElementById('bgUnusedHint');
        if (!hint) return;
        var mode = value('background_mode');
        hint.hidden = !(effective('background') && (mode === 'gradient' || mode === 'color'));
    }
    var useImageBtn = document.getElementById('bgUseImage');
    if (useImageBtn) useImageBtn.addEventListener('click', function () { setBackgroundMode('image'); refresh(); markDirty(); });

    function refresh() { applyTheme(); applyText(); applyMedia(); applyReadouts(); markPresets(); updateBackgroundHint(); }

    function markDirty() {
        dirty = true;
        if (saveBar) saveBar.classList.add('is-dirty');
    }

    /* ── Ventanas: el perfil pequeño arriba y las opciones abajo ── */
    var stageHost = document.getElementById('pfStageHost');
    var openedPop = null, lastFocus = null;

    /* "Volver" (y la X, tocar fuera o Escape): cierra la ventana SIN guardar y quita lo que se cambió dentro de ella. Solo "Guardar cambios" guarda.
       Al abrir la ventana se anota cómo estaba todo el formulario; al volver, si algo cambió, se pregunta y se deja como estaba. */
    var popSnapshot = null, popDirtyBefore = false;
    function takeSnapshot() {
        var items = [];
        Array.prototype.forEach.call(form.elements, function (el) {
            if (!el.name || /^(submit|button|reset|image)$/.test(el.type)) return;
            var item = { el: el, type: el.type };
            if (el.type === 'checkbox' || el.type === 'radio') item.checked = el.checked;
            else if (el.type === 'file') item.files = Array.prototype.slice.call(el.files || []);
            else item.value = el.value;
            items.push(item);
        });
        return items;
    }
    function sameFiles(a, b) {
        return a.length === b.length && a.every(function (f, i) { return f === b[i] || (f.name === b[i].name && f.size === b[i].size && f.lastModified === b[i].lastModified); });
    }
    function itemChanged(item) {
        var el = item.el;
        if (item.type === 'checkbox' || item.type === 'radio') return el.checked !== item.checked;
        if (item.type === 'file') return !sameFiles(Array.prototype.slice.call(el.files || []), item.files);
        return el.value !== item.value;
    }
    function restoreSnapshot(items) {
        // 1) las imágenes elegidas: se devuelve la elección anterior y el editor recalcula la vista previa con su evento "change"
        items.forEach(function (item) {
            if (item.type !== 'file' || !itemChanged(item)) return;
            try {
                var dt = new DataTransfer();
                item.files.forEach(function (f) { dt.items.add(f); });
                item.el.files = dt.files;
            } catch (_) { item.el.value = ''; }
            item.el.dispatchEvent(new Event('change', { bubbles: true }));
        });
        // 2) todo lo demás, después: así se pisa lo que hizo el cambio de archivo (poner "Mi marco", cambiar el tipo de fondo...)
        items.forEach(function (item) {
            if (item.type === 'file') return;
            if (item.type === 'checkbox' || item.type === 'radio') item.el.checked = item.checked;
            else item.el.value = item.value;
        });
        Array.prototype.forEach.call(form.querySelectorAll('[data-live-remove]'), function (box) { media[box.getAttribute('data-live-remove')].removed = box.checked; });
        refresh();
    }
    function requestClose() {
        if (!openedPop) return;
        if (popSnapshot && popSnapshot.some(itemChanged)) {
            if (!window.confirm('¿Volver sin guardar? Se quitan los cambios que hiciste en esta ventana.')) return;
            restoreSnapshot(popSnapshot);
            dirty = popDirtyBefore;
            if (saveBar) saveBar.classList.toggle('is-dirty', dirty);
        }
        closePopup();
    }

    function openPopup(id) {
        var pop = document.getElementById(id);
        if (!pop) return;
        if (openedPop) closePopup();
        popSnapshot = takeSnapshot();
        popDirtyBefore = dirty;
        lastFocus = document.activeElement;
        openedPop = pop;
        pop.hidden = false;
        var host = pop.querySelector('[data-pop-preview]');
        if (host) { host.appendChild(scope); scope.classList.add('is-mini'); }
        document.documentElement.style.overflow = 'hidden';
        window.requestAnimationFrame(function () { pop.classList.add('is-open'); });
        var sheet = pop.querySelector('.pf-pop-sheet');
        if (sheet) { sheet.setAttribute('tabindex', '-1'); sheet.focus({ preventScroll: true }); }
    }
    function closePopup() {
        if (!openedPop) return;
        var pop = openedPop;
        openedPop = null;
        pop.classList.remove('is-open');
        pop.hidden = true;
        if (stageHost) stageHost.appendChild(scope);
        scope.classList.remove('is-mini');
        document.documentElement.style.overflow = '';
        if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
    }
    document.addEventListener('click', function (evt) {
        var opener = evt.target.closest('[data-pop-open]');
        if (opener) { evt.preventDefault(); openPopup(opener.getAttribute('data-pop-open')); return; }
        if (evt.target.closest('[data-pop-close]')) { evt.preventDefault(); requestClose(); }
    });
    document.addEventListener('keydown', function (evt) { if (evt.key === 'Escape' && openedPop) requestClose(); });

    form.addEventListener('input', function (evt) {
        if (evt.target && evt.target.type === 'file') return;
        refresh();
        markDirty();
    });
    form.addEventListener('change', function (evt) {
        if (evt.target && evt.target.type === 'file') return;
        // Elegir un fondo de la tienda de puntos lo pone a verse (igual que al subir una imagen propia).
        if (evt.target && evt.target.name === 'background_choice' && evt.target.value !== 'own') setBackgroundMode('image');
        refresh();
        markDirty();
    });
    // "Ver mis marcos / fotos / fondos": abre y cierra la lista de lo que la cuenta consiguió en la tienda de puntos.
    form.addEventListener('click', function (evt) {
        var btn = evt.target.closest ? evt.target.closest('[data-owned-toggle]') : null;
        if (!btn) return;
        var wrap = btn.closest('[data-owned]');
        var panel = wrap ? wrap.querySelector('[data-owned-panel]') : null;
        if (!panel) return;
        var opening = panel.hidden;
        panel.hidden = !opening;
        btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
    });
    form.addEventListener('submit', function () {
        submitting = true;
        Array.prototype.forEach.call(form.querySelectorAll('[data-save-btn]'), function (btn) { btn.disabled = true; if (btn.firstChild) btn.firstChild.textContent = 'Guardando…'; });
    });
    window.addEventListener('beforeunload', function (evt) {
        if (dirty && !submitting) { evt.preventDefault(); evt.returnValue = ''; }
    });

    refresh();
    dirty = false;
    if (saveBar) saveBar.classList.remove('is-dirty');
})();
