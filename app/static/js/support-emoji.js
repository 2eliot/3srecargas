/* ── Selector de emojis del chat de soporte (cliente) ─────────────────────────────────────────────────────────
 *
 * Mismo selector que ya tiene el chat del admin: botón de carita junto al clip, ventana de cristal con pestañas por tema y "recientes".
 * El emoji se inserta donde está el cursor de la caja de escribir (#scInput) y se avisa con un evento "input" para que la caja crezca
 * y el botón de enviar se encienda (lo escucha support-chat.js). No toca nada más del chat.
 */
(function () {
    'use strict';

    var btn = document.getElementById('scEmojiBtn');
    var box = document.getElementById('scEmoji');
    var input = document.getElementById('scInput');
    var composer = document.getElementById('scComposer');
    if (!btn || !box || !input) return;

    var RECENT_KEY = 'sc:recent-emojis';
    var GROUPS = [
        { icon: '😀', name: 'Caras', list: '😀 😃 😄 😁 😆 😅 😂 🤣 🥲 😊 🙂 😉 😍 🥰 😘 😎 🤩 🤗 🤔 🙄 😬 😴 😮 😢 😭 😡 🤯 😱 🥳 😇 🫡 😌' },
        { icon: '👍', name: 'Gestos', list: '👍 👎 👏 🙌 🙏 💪 👌 ✌️ 🤞 🤝 👋 🫶 ☝️ 👇 👉 👈 ✋ 🤙 🫰 ✍️' },
        { icon: '🎮', name: 'Recargas', list: '🎮 🕹️ 💎 💰 💵 💳 🪙 🏆 🥇 🎁 🎉 🎊 🔥 ⚡ ✨ ⭐ 🌟 🚀 📱 💻 📦 🛒 🧾 🎯' },
        { icon: '✅', name: 'Avisos', list: '✅ ❌ ⚠️ ❗ ❓ ℹ️ 💬 📞 📧 🔒 🔑 ⏰ ⌛ 📅 🔔 📌 🔗 ❤️ 🧡 💛 💚 💙 💜 🖤 🤍' }
    ];

    function node(tag, className, text) {
        var el = document.createElement(tag);
        if (className) el.className = className;
        if (text) el.textContent = text;
        return el;
    }

    function readRecent() {
        try {
            var saved = JSON.parse(window.localStorage.getItem(RECENT_KEY) || '[]');
            return Array.isArray(saved) ? saved.filter(function (e) { return typeof e === 'string' && e.length <= 12; }).slice(0, 16) : [];
        } catch (_) { return []; }
    }
    function saveRecent(emoji) {
        try {
            var list = readRecent().filter(function (e) { return e !== emoji; });
            list.unshift(emoji);
            window.localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 16)));
        } catch (_) { /* sin almacenamiento: no pasa nada */ }
    }

    function insert(emoji) {
        var start = typeof input.selectionStart === 'number' ? input.selectionStart : input.value.length;
        var end = typeof input.selectionEnd === 'number' ? input.selectionEnd : start;
        var max = input.maxLength > 0 ? input.maxLength : 2000;
        if (input.value.length - (end - start) + emoji.length > max) return;
        input.value = input.value.slice(0, start) + emoji + input.value.slice(end);
        var pos = start + emoji.length;
        try { input.setSelectionRange(pos, pos); } catch (_) { /* nada */ }
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
        saveRecent(emoji);
    }

    function showGroup(group, tabs, title, grid) {
        Array.prototype.forEach.call(tabs.children, function (tab) { tab.classList.toggle('is-on', tab.getAttribute('data-group') === group.name); });
        title.textContent = group.name;
        grid.textContent = '';
        group.list.forEach(function (emoji) {
            var item = node('button', 'sc-emoji-item', emoji);
            item.type = 'button';
            item.setAttribute('aria-label', emoji);
            item.addEventListener('click', function () { insert(emoji); });
            grid.appendChild(item);
        });
    }

    function build() {
        box.textContent = '';
        var groups = GROUPS.map(function (g) { return { icon: g.icon, name: g.name, list: g.list.split(' ') }; });
        var recent = readRecent();
        if (recent.length) groups.unshift({ icon: '🕘', name: 'Recientes', list: recent });
        var tabs = node('div', 'sc-emoji-tabs');
        tabs.setAttribute('role', 'tablist');
        var title = node('p', 'sc-emoji-title');
        var grid = node('div', 'sc-emoji-grid');
        groups.forEach(function (group) {
            var tab = node('button', 'sc-emoji-tab', group.icon);
            tab.type = 'button';
            tab.setAttribute('data-group', group.name);
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-label', group.name);
            tab.title = group.name;
            tab.addEventListener('click', function () { showGroup(group, tabs, title, grid); });
            tabs.appendChild(tab);
        });
        box.appendChild(tabs);
        box.appendChild(title);
        box.appendChild(grid);
        showGroup(groups[0], tabs, title, grid);
    }

    function setOpen(open) {
        if (open) build();
        box.hidden = !open;
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    btn.addEventListener('click', function (evt) {
        evt.stopPropagation();
        setOpen(box.hidden);
    });
    // Tocar fuera, enviar el mensaje o Escape lo cierran.
    document.addEventListener('click', function (evt) {
        if (!box.hidden && !box.contains(evt.target) && !btn.contains(evt.target)) setOpen(false);
    });
    document.addEventListener('keydown', function (evt) {
        if (evt.key === 'Escape' && !box.hidden) { setOpen(false); input.focus(); }
    });
    if (composer) composer.addEventListener('submit', function () { if (!box.hidden) setOpen(false); });
})();
