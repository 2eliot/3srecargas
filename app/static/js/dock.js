/* ── 3S Recargas — barra flotante de abajo (dock) con efecto gota ───────────
 *
 * Cinco apartados (Perfil, Ranking, Inicio, Juegos, Soporte). El botón rojo marca el apartado elegido
 * y se queda ahí ("se guarda") hasta que elijas otro.
 *
 * Al cambiar de apartado pasa lo de la animación de referencia: el botón rojo se hunde como una gota en la
 * barra, la barra se hincha como una gota en el apartado nuevo y esa gota se despega y sale como el botón
 * rojo, mientras se abre el hueco curvo debajo. La forma de la barra se dibuja con una curva (clip-path)
 * que se recalcula en cada cuadro, así que el cristal sigue el movimiento.
 *
 * Todo texto entra con textContent, nunca innerHTML.
 */
(function () {
    'use strict';

    var dock = document.getElementById('nxDock');
    if (!dock) return;

    var bar = dock.querySelector('.nx-dock-bar');
    var edgePath = dock.querySelector('.nx-dock-edge path');
    var shadowPath = dock.querySelector('.nx-dock-shadow path');
    var svgs = dock.querySelectorAll('.nx-dock-edge, .nx-dock-shadow');
    var main = dock.querySelector('.nx-dock-main');
    var label = dock.querySelector('.nx-dock-main-label');
    var items = Array.prototype.slice.call(dock.querySelectorAll('.nx-dock-item'));
    var toast = document.getElementById('nxDockToast');
    var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    // Modo liviano (se puede encender o apagar en cualquier momento): sin animación de la barra, el cambio es inmediato.
    function isLite() { return document.documentElement.classList.contains('lite'); }
    if (!bar || !edgePath || !main || !items.length) return;

    // Medidas (px). TOP es lo que sobresale la forma por encima de la barra para poder dibujar la gota.
    var TOP = 28, H = 52, R = 18;
    var CIRCLE = 46;         // diámetro del botón rojo
    var GAP = 6;             // separación entre el botón y el borde del hueco (igual por todos lados)
    var DIP = 26;            // hondura del hueco: el botón asoma 3 px por encima de la barra, así que 23 + 6 - 3
    var FILLET = 7;          // suavizado de los hombros del hueco, donde se junta con el borde de arriba
    var BUMP = 15;           // alto de la gota que se hincha
    var HW_BUMP = 19;        // medio ancho de la gota
    // La animación es rápida a propósito (pedido del dueño): si tarda, la gente toca varias veces pensando que no se puso. Lo que hace el apartado
    // (abrir el ranking, los puntos, ir a otra página...) ocurre casi al instante, sin esperar a que termine la burbuja.
    var SINK_MS = 70, RISE_MS = 190;
    var ACTION_AT = 25;

    var W = 0;
    var state = { cx: 0, depth: DIP, scale: 1, dy: 0, opacity: 1 };
    var homeItem = dock.querySelector('[data-dock-home]') || items[0];   // la web siempre abre en Inicio
    // ...salvo en el perfil: ahí el botón rojo descansa en el apartado marcado con data-dock-selected (Perfil)
    var initialItem = dock.querySelector('[data-dock-selected]') || homeItem;
    // Desde otra página (el perfil), Ranking / Puntos llevan a la portada con #ranking o #puntos y main.js abre esa ventana al llegar (y borra el #):
    // la burbuja tiene que nacer ya en ese apartado, no en Inicio, o parece que "cambió sola" a Inicio.
    var hashOpen = String(window.nxHashOpen || window.location.hash || '').toLowerCase();
    var rankItem = items.filter(function (it) { return it.getAttribute('data-dock-proxy') === 'openRankingModalBtn'; })[0] || null;
    var pointsItem = items.filter(function (it) { return it.getAttribute('data-dock-proxy') === 'openPointsModalBtn'; })[0] || null;
    var gamesItem = dock.querySelector('[data-dock-popup]');
    var startItem = (hashOpen === '#ranking' && rankItem) || (hashOpen === '#puntos' && (pointsItem || gamesItem)) || initialItem;
    var selected = startItem;
    var anim = null;         // { from, to, start, item }
    var rafId = 0;
    var actionTimer = null;

    function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
    function lerp(a, b, t) { return a + (b - a) * t; }
    function easeOutCubic(t) { var u = 1 - t; return 1 - u * u * u; }
    function easeOutBack(t) { var c1 = 1.70158, c3 = c1 + 1, u = t - 1; return 1 + c3 * u * u * u + c1 * u * u; }

    // ── La forma de la barra ─────────────────────────────────────────────
    // Recta con esquinas redondas y, en la posición `cx`, un hueco (depth > 0) o una gota (depth < 0).
    //
    // El hueco es un círculo con el MISMO centro que el botón rojo y 6 px más de radio, así la separación es
    // igual por todos lados; donde se junta con el borde de arriba lleva un arco de suavizado (como la muesca
    // de la imagen de referencia). Si `depth` es menor que DIP (el botón se está hundiendo o naciendo), el
    // círculo se achica en la misma proporción.
    //
    // En los apartados de las puntas el suavizado se acorta lo que haga falta para no pisar la esquina
    // redonda de la barra.
    function dipPath(cx, depth) {
        var y0 = TOP, k = depth / DIP;
        var rho = (CIRCLE / 2 + GAP) * k;            // radio del hueco
        var c = rho - depth;                         // cuánto sube su centro por encima del borde de la barra
        var chord = Math.sqrt(Math.max(0, rho * rho - c * c));
        var f0 = FILLET * k;
        function fit(room) {                         // suavizado más grande que cabe antes de la esquina
            var f = Math.min(f0, (room * room - chord * chord) / (2 * (rho - c)));
            return f > 0.4 ? f : 0;
        }
        var fl = fit(cx - R - 0.5), fr = fit(W - R - 0.5 - cx);
        function reach(f) { return Math.sqrt(chord * chord + 2 * f * (rho - c)); }   // distancia horizontal al inicio de la curva
        var dl = reach(fl), dr = reach(fr);
        function joint(sign, f, d) {                 // punto donde el suavizado toca el círculo del hueco
            if (!f) return { x: cx + sign * chord, y: y0 };
            var fx = cx + sign * d, fy = y0 + f, kx = cx, ky = y0 - c;
            return { x: kx + (fx - kx) * rho / (rho + f), y: ky + (fy - ky) * rho / (rho + f) };
        }
        var pl = joint(-1, fl, dl), pr = joint(1, fr, dr);
        function n(v) { return v.toFixed(1); }
        var out = 'H' + n(cx - (fl ? dl : chord));
        if (fl) out += ' A' + n(fl) + ',' + n(fl) + ' 0 0 1 ' + n(pl.x) + ',' + n(pl.y);
        out += ' A' + n(rho) + ',' + n(rho) + ' 0 0 0 ' + n(pr.x) + ',' + n(pr.y);
        if (fr) out += ' A' + n(fr) + ',' + n(fr) + ' 0 0 1 ' + n(cx + dr) + ',' + n(y0);
        return out + ' H' + (W - R);
    }

    // La gota que se hincha al nacer el botón: una "S" suave, así se ve líquida.
    function bumpPath(cx, depth) {
        var hl = Math.max(8, Math.min(HW_BUMP, cx - R - 1));
        var hr = Math.max(8, Math.min(HW_BUMP, W - R - 1 - cx));
        var a = cx - hl, b = cx + hr, y0 = TOP, yb = TOP + depth;
        return 'H' + a.toFixed(1) +
            ' C' + (a + hl * 0.58).toFixed(1) + ',' + y0 + ' ' + (cx - hl * 0.42).toFixed(1) + ',' + yb.toFixed(1) + ' ' + cx.toFixed(1) + ',' + yb.toFixed(1) +
            ' C' + (cx + hr * 0.42).toFixed(1) + ',' + yb.toFixed(1) + ' ' + (b - hr * 0.58).toFixed(1) + ',' + y0 + ' ' + b.toFixed(1) + ',' + y0 +
            ' H' + (W - R);
    }

    function notch(cx, depth) {
        if (Math.abs(depth) < 0.2) return 'H' + (W - R);
        return depth > 0 ? dipPath(cx, depth) : bumpPath(cx, depth);
    }

    function shape() {
        var y0 = TOP, y1 = TOP + H;
        return 'M0,' + (y0 + R) + ' A' + R + ',' + R + ' 0 0 1 ' + R + ',' + y0 + ' ' + notch(state.cx, state.depth) +
            ' A' + R + ',' + R + ' 0 0 1 ' + W + ',' + (y0 + R) +
            ' V' + (y1 - R) + ' A' + R + ',' + R + ' 0 0 1 ' + (W - R) + ',' + y1 +
            ' H' + R + ' A' + R + ',' + R + ' 0 0 1 0,' + (y1 - R) + ' Z';
    }

    function render() {
        var d = shape();
        bar.style.clipPath = 'path("' + d + '")';
        edgePath.setAttribute('d', d);
        if (shadowPath) shadowPath.setAttribute('d', d);
        main.style.translate = (state.cx - CIRCLE / 2).toFixed(1) + 'px ' + state.dy.toFixed(1) + 'px';
        main.style.scale = state.scale.toFixed(3);
        main.style.opacity = state.opacity.toFixed(3);
        if (label) {
            label.style.left = state.cx.toFixed(1) + 'px';
            label.style.opacity = state.opacity.toFixed(3);
        }
    }

    // ── Medidas de la pantalla ───────────────────────────────────────────
    function slotX(item) {
        var d = dock.getBoundingClientRect();
        var r = item.getBoundingClientRect();
        return (r.left + r.width / 2) - d.left;
    }

    function measure() {
        W = dock.getBoundingClientRect().width;
        Array.prototype.forEach.call(svgs, function (svg) {
            svg.setAttribute('width', W.toFixed(1));
            svg.setAttribute('height', TOP + H);
            svg.setAttribute('viewBox', '0 0 ' + W.toFixed(1) + ' ' + (TOP + H));
        });
    }

    // ── Apartado elegido ─────────────────────────────────────────────────
    function paintSelected(item) {
        selected = item;
        items.forEach(function (it) {
            var on = it === item;
            it.classList.toggle('is-covered', on);
            if (on) it.setAttribute('aria-current', 'true'); else it.removeAttribute('aria-current');
        });
        var icon = item.querySelector('svg');
        var old = main.querySelector('svg');
        if (icon) {
            var clone = icon.cloneNode(true);
            if (old) main.replaceChild(clone, old); else main.appendChild(clone);
        }
        main.setAttribute('aria-label', item.getAttribute('data-dock-label') || item.getAttribute('aria-label') || '');
        if (label) label.textContent = item.getAttribute('data-dock-label') || '';
    }

    function setRest(item) {
        state.cx = slotX(item);
        state.depth = DIP; state.scale = 1; state.dy = 0; state.opacity = 1;
        paintSelected(item);
        render();
    }

    // ── La animación de gota ─────────────────────────────────────────────
    // Un solo reloj `t` (ms): primero el botón se hunde donde estaba; luego, en el apartado nuevo, la barra
    // se hincha como una gota, el botón sale de la punta y la gota se hunde en el hueco que se abre.
    function frameAt(t) {
        var a = anim;
        if (t < SINK_MS) {
            var p = clamp(t / SINK_MS, 0, 1), e = p * p * p;
            state.cx = a.fromX;
            // al hundirse el botón, el hueco se cierra y la barra asoma una gotita que se aplana
            state.depth = DIP * (1 - e) - BUMP * 0.5 * Math.sin(Math.PI * p) * p;
            state.scale = 1 - 0.85 * e;
            state.dy = 22 * e;
            state.opacity = 1 - clamp((p - 0.65) / 0.35, 0, 1);
            return;
        }
        state.cx = a.toX;
        var q = clamp((t - SINK_MS) / RISE_MS, 0, 1);
        var SPLIT = 0.42;
        if (q < SPLIT) {
            var u = q / SPLIT, eu = easeOutCubic(u);
            state.depth = -BUMP * eu;                                 // la gota sube
            state.scale = 0.16 + 0.12 * eu;
            state.dy = -14 * eu;                                       // el botón nace en la punta
            state.opacity = clamp((u - 0.25) / 0.4, 0, 1);
        } else {
            var v = (q - SPLIT) / (1 - SPLIT), ev = easeOutBack(v);
            state.depth = lerp(-BUMP, DIP, ev);                        // la gota se hunde y el hueco se abre
            state.scale = lerp(0.28, 1, ev);
            state.dy = -14 * (1 - ev);
            state.opacity = 1;
        }
    }

    function finishAnim() {
        if (!anim) return;
        cancelAnimationFrame(rafId);
        var a = anim;
        anim = null;
        setRest(a.item);
    }

    function step(now) {
        if (!anim) return;
        var t = now - anim.start;
        if (t >= SINK_MS && !anim.swapped) {            // el ícono y el nombre cambian justo cuando nace el botón
            anim.swapped = true;
            paintSelected(anim.item);
        }
        if (t >= SINK_MS + RISE_MS) { finishAnim(); return; }
        frameAt(t);
        render();
        rafId = requestAnimationFrame(step);
    }

    function beginAnim(item) {
        finishAnim();
        anim = { item: item, fromX: state.cx, toX: slotX(item), start: performance.now(), swapped: false };
        selected.classList.remove('is-covered');          // el ícono del apartado que se deja reaparece al hundirse el botón
        rafId = requestAnimationFrame(step);
    }

    // ── Lo que hace cada apartado ────────────────────────────────────────
    var toastTimer = null;
    function showToast(text) {
        if (!toast) return;
        toast.textContent = text;
        toast.hidden = false;
        toast.classList.remove('is-on');
        void toast.offsetWidth;                // reinicia la animación si se toca varias veces
        toast.classList.add('is-on');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { toast.classList.remove('is-on'); toast.hidden = true; }, 2400);
    }

    // ── Ranking y puntos: con su ventana abierta la barra se queda a la vista ──
    // Las ventanas las abre main.js (aria-hidden="false"); aquí solo se sube la barra por encima del fondo
    // oscuro (clase is-over-modal) y se deja libre el espacio de abajo para que no tape el contenido.
    var SITE_MODALS = {
        openRankingModalBtn: { modal: 'rankingModal', close: 'rankingModalCloseBtn' },
        openPointsModalBtn: { modal: 'pointsModal', close: 'pointsModalCloseBtn' }
    };
    function modalOpen(id) {
        var el = document.getElementById(id);
        return !!el && el.getAttribute('aria-hidden') === 'false';
    }
    function syncModalState() {
        var open = Object.keys(SITE_MODALS).some(function (k) { return modalOpen(SITE_MODALS[k].modal); });
        dock.classList.toggle('is-over-modal', open);
        document.body.classList.toggle('dock-over-modal', open);
    }
    function closeSiteModals(except) {
        Object.keys(SITE_MODALS).forEach(function (k) {
            if (k === except || !modalOpen(SITE_MODALS[k].modal)) return;
            var closeBtn = document.getElementById(SITE_MODALS[k].close);
            if (closeBtn) closeBtn.click();
        });
    }
    // La burbuja acompaña a la ventana: al abrirse el ranking (o los puntos) se pone en su apartado y, al cerrarlo (con la X, tocando fuera o el mismo
    // apartado), vuelve a donde descansa la página (Inicio en la portada).
    function moveTo(item) {
        if (!item || (item === selected && !anim)) return;
        if (anim && anim.item === item) return;
        if (reduceMotion || isLite()) { finishAnim(); setRest(item); return; }
        beginAnim(item);
    }
    function followModals() {
        syncModalState();
        var want = modalOpen('rankingModal') ? rankItem : (modalOpen('pointsModal') ? pointsItem : null);
        if (want) {
            moveTo(want);
            return;
        }
        if (modalOpen('rankingModal') || modalOpen('pointsModal')) return;        // sigue habiendo una ventana abierta sin apartado propio (Puntos desde Juegos)
        if (anim || popupOpen()) return;                                           // se está eligiendo otro apartado: no se le atraviesa
        if (selected === rankItem || selected === pointsItem || selected === gamesItem) moveTo(initialItem);
    }
    if (window.MutationObserver) {
        var modalWatcher = new MutationObserver(followModals);
        Object.keys(SITE_MODALS).forEach(function (k) {
            var el = document.getElementById(SITE_MODALS[k].modal);
            if (el) modalWatcher.observe(el, { attributes: true, attributeFilter: ['aria-hidden'] });
        });
    }

    // ── Ventana de Juegos (mini juegos y dinámicas) ──────────────────────
    var popup = document.getElementById('nxGamesPopup');
    var popupTimer = null;
    function popupOpen() { return !!popup && popup.classList.contains('is-open'); }
    function openPopup() {
        if (!popup) return;
        clearTimeout(popupTimer);
        popup.hidden = false;
        void popup.offsetWidth;                          // para que la entrada se anime
        popup.classList.add('is-open');
        var first = popup.querySelector('.nx-games-pop-close');
        if (first) first.focus({ preventScroll: true });
    }
    // `keepBubble`: quien cierra la ventana ya va a mover la burbuja a otro apartado (elegir otro, abrir Puntos). Si no, al cerrarla (con la X, tocando fuera,
    // Escape o tocando otra vez Juegos) la burbuja vuelve a donde descansa la página, igual que con el ranking y los puntos.
    function closePopup(immediate, keepBubble) {
        if (!popupOpen()) return;
        popup.classList.remove('is-open');
        clearTimeout(popupTimer);
        popupTimer = setTimeout(function () { popup.hidden = true; }, (immediate || reduceMotion || isLite()) ? 0 : 130);
        if (!keepBubble && selected === gamesItem && !anim && !modalOpen('rankingModal') && !modalOpen('pointsModal')) moveTo(initialItem);
    }
    if (popup) {
        popup.addEventListener('click', function (evt) {
            var from = evt.target && evt.target.closest ? evt.target : null;
            if (!from) return;
            if (from.closest('[data-games-close]')) { closePopup(); return; }
            if (from.closest('[data-games-points]')) {
                // Canjear puntos no tiene página propia: abre la misma ventana que el menú.
                closePopup(true, true);
                var openPoints = document.getElementById('openPointsModalBtn');
                if (openPoints) openPoints.click();
                else window.location.href = '/#puntos';   // en otra página (el perfil) la ventana de puntos está en la portada
            }
        });
        document.addEventListener('keydown', function (evt) { if (evt.key === 'Escape') closePopup(); });
    }

    function runAction(item) {
        if (item.hasAttribute('data-dock-here')) {
            // Ya estás en esa página (Perfil dentro del perfil): sube arriba en vez de recargar.
            window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
            return;
        }
        if (item.hasAttribute('data-dock-home')) {
            // Inicio: si ya estás en la portada, sube arriba en vez de recargar.
            if (window.location.pathname === '/' && !window.location.hash) {
                window.scrollTo({ top: 0, behavior: reduceMotion ? 'auto' : 'smooth' });
            } else {
                window.location.href = item.getAttribute('href') || '/';
            }
            return;
        }
        var proxy = item.getAttribute('data-dock-proxy');
        if (proxy) {
            // Ranking / puntos: abren el mismo cuadro que ya abre el menú (main.js lo conecta al botón del menú);
            // si ya está abierto, tocar otra vez el apartado lo cierra.
            if (SITE_MODALS[proxy] && modalOpen(SITE_MODALS[proxy].modal)) { closeSiteModals(); return; }
            closeSiteModals(proxy);   // si había OTRA ventana abierta (Puntos y se toca Ranking), se cierra: antes quedaban las dos una sobre otra
            var target = document.getElementById(proxy);
            if (target) target.click();
            return;
        }
        if (item.hasAttribute('data-dock-popup')) {
            // Juegos: abre la ventana de mini juegos y dinámicas; tocar otra vez el apartado la cierra.
            if (popupOpen()) closePopup(); else openPopup();
            return;
        }
        var soon = item.getAttribute('data-dock-soon');
        if (soon) { showToast(soon); return; }
        var href = item.getAttribute('href');
        if (href) window.location.href = href;
    }

    function choose(item) {
        clearTimeout(actionTimer);
        if (!item.hasAttribute('data-dock-popup')) closePopup(false, true);      // al elegir otro apartado, la ventana se cierra (la burbuja ya va a ese apartado)
        if (!item.hasAttribute('data-dock-proxy')) closeSiteModals(); // ...y también el ranking o los puntos si estaban abiertos
        if (item === selected && !anim) {                 // ya está ahí: solo repite lo que hace
            runAction(item);
            return;
        }
        if (anim && anim.item === item) return;           // ya va para ese apartado: un segundo toque no reinicia la animación ni repite la acción
        if (reduceMotion || isLite()) {
            finishAnim();
            setRest(item);
            runAction(item);
            return;
        }
        beginAnim(item);
        actionTimer = setTimeout(function () { runAction(item); }, ACTION_AT);
    }

    items.forEach(function (item) {
        item.addEventListener('click', function (evt) {
            // Ctrl/Cmd/clic medio en un enlace: que el navegador lo abra en otra pestaña como siempre.
            if (item.tagName === 'A' && (evt.ctrlKey || evt.metaKey || evt.shiftKey || evt.button === 1)) return;
            evt.preventDefault();
            choose(item);
        });
    });

    // El botón rojo repite lo que hace el apartado elegido.
    main.addEventListener('click', function (evt) {
        evt.preventDefault();
        if (anim) return;
        runAction(selected);
    });

    // Con el teclado abierto (escribiendo un ID, un código...) la barra estorba: se esconde.
    function typing(el) {
        return !!el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) && !/^(checkbox|radio|button|submit|range|file)$/i.test(el.type || '');
    }
    document.addEventListener('focusin', function (evt) { if (typing(evt.target)) dock.classList.add('is-away'); });
    document.addEventListener('focusout', function () {
        setTimeout(function () { if (!typing(document.activeElement)) dock.classList.remove('is-away'); }, 60);
    });

    // ── Arranque y cambios de tamaño ─────────────────────────────────────
    function layout() {
        finishAnim();
        measure();
        setRest(selected);
    }

    // Si la pestaña vuelve de otra página (botón "atrás"), la barra empieza otra vez en Inicio, sin animación.
    window.addEventListener('pageshow', function (evt) {
        if (evt.persisted) { clearTimeout(actionTimer); closePopup(true); finishAnim(); measure(); setRest(initialItem); }
    });
    var resizeTimer = null;
    window.addEventListener('resize', function () { clearTimeout(resizeTimer); resizeTimer = setTimeout(layout, 80); });

    measure();
    setRest(startItem);
    dock.classList.add('is-ready');

    // Para las pruebas: dibuja el cuadro `t` (ms) de la animación hacia el apartado `index`, sin moverse solo.
    dock.nxPreview = function (index, t) {
        finishAnim();
        var item = items[index];
        anim = { item: item, fromX: slotX(selected), toX: slotX(item), start: 0, swapped: false };
        if (t >= SINK_MS) paintSelected(item);
        frameAt(t);
        render();
    };
    dock.nxReset = function () { anim = null; setRest(initialItem); };
    dock.nxState = function () { return { cx: state.cx, depth: state.depth, scale: state.scale, dy: state.dy, opacity: state.opacity }; };
})();
