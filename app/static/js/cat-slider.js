/* ── 3S Recargas — cuadro de cristal que se desliza entre las pestañas de categorías ───────────────
 *
 * En la portada (Juegos / Tarjetas / Wallet) la categoría elegida lleva un cuadro de cristal por detrás. En
 * lugar de aparecer de golpe en cada pestaña, hay UN solo cuadro (.cat-slider) que se desliza de la pestaña
 * anterior a la nueva. main.js solo cambia la clase "active" de los botones; aquí se mira ese cambio y se mueve
 * el cuadro (la animación la hace el CSS con transition). Sin este script, la pestaña elegida lleva su propio
 * cuadro de cristal (regla :not(.has-slider) del CSS).
 */
(function () {
    'use strict';

    var bar = document.querySelector('.nx-store .categories-inner');
    var slider = bar && bar.querySelector('.cat-slider');
    if (!bar || !slider) return;

    var buttons = Array.prototype.slice.call(bar.querySelectorAll('.cat-btn'));
    if (!buttons.length) return;

    function place() {
        var active = bar.querySelector('.cat-btn.active');
        if (!active) { slider.style.width = '0px'; return; }
        var b = bar.getBoundingClientRect();
        var a = active.getBoundingClientRect();
        slider.style.width = a.width + 'px';
        slider.style.height = a.height + 'px';
        slider.style.transform = 'translate(' + (a.left - b.left - bar.clientLeft) + 'px, ' + (a.top - b.top - bar.clientTop) + 'px)';
        bar.classList.add('has-slider');
    }

    // Sitio inicial y cambios de tamaño: sin animación, para que no "viaje" desde el rincón ni se arrastre.
    function placeWithoutAnimation() {
        bar.classList.remove('is-ready');
        place();
        void slider.offsetWidth;
        requestAnimationFrame(function () { bar.classList.add('is-ready'); });
    }

    // main.js cambia la clase "active" al elegir otra categoría: se mueve el cuadro.
    if (window.MutationObserver) {
        var watcher = new MutationObserver(place);
        buttons.forEach(function (btn) { watcher.observe(btn, { attributes: true, attributeFilter: ['class'] }); });
    }

    var resizeTimer = null;
    window.addEventListener('resize', function () {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(placeWithoutAnimation, 60);
    });

    placeWithoutAnimation();
    // Con la tipografía ya cargada los anchos pueden cambiar un poco: se recoloca sin animación.
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(placeWithoutAnimation);
})();
