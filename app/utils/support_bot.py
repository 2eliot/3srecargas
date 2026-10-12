"""Menú de botones del chat de soporte: comprar recarga y estado de la recarga.

Es un menú fijo, sin IA: todo lo que muestra sale de la base de datos con
las mismas reglas que la tienda (el catálogo se pide a la misma función que
usa la tienda, `api_packages`), así que los precios del chat nunca pueden
diferir de los de la página. Funciona aunque el asistente de IA esté
apagado y no crea ningún chat: son consultas sueltas, solo se guarda un
hilo cuando el cliente escribe.

También guarda los ajustes que el admin cambia en Soporte → Asistente:
nombre del asistente, mensaje de bienvenida y qué botones se muestran.
"""

import re
from datetime import datetime, timedelta

from flask import current_app
from werkzeug.exceptions import NotFound

from ..models import Category, Game, Order, PaymentMethod, Setting, db
from .support_ai import describe_order, _missing_amount_bs

# ─── Ajustes (admin → Soporte → Asistente) ──────────────────────────────────

# (clave, texto del botón). El orden es el que se ve en el chat.
BOT_BUTTONS = [
    ('estado', 'Estado de mi recarga'),
    ('comprar', 'Comprar recarga'),
]
BOT_BUTTON_KEYS = [key for key, _ in BOT_BUTTONS]

DEFAULT_BOT_NAME = 'Asistente 3S'
DEFAULT_BOT_GREETING = ('¡Hola! 👋 Soy el asistente de 3S Recargas. Te ayudo con precios, recargas, '
                        'pagos y cualquier duda. ¿Qué necesitas?')

SETTING_KEYS = {
    'enabled': 'support_bot_enabled',
    'name': 'support_bot_name',
    'greeting': 'support_bot_greeting',
    'buttons': 'support_bot_buttons',
}


def _setting(key):
    row = Setting.query.filter_by(key=SETTING_KEYS[key]).first()
    return row.value if row is not None and row.value is not None else None


def get_bot_settings():
    """Sin nada guardado el menú viene encendido con los 4 botones."""
    enabled = _setting('enabled')
    buttons = _setting('buttons')
    chosen = [k for k in (buttons or '').split(',') if k in BOT_BUTTON_KEYS] if buttons is not None else BOT_BUTTON_KEYS
    return {
        'enabled': enabled != '0',
        'name': (_setting('name') or '').strip() or DEFAULT_BOT_NAME,
        'greeting': (_setting('greeting') or '').strip() or DEFAULT_BOT_GREETING,
        'buttons': [k for k in BOT_BUTTON_KEYS if k in chosen],
    }


def save_bot_settings(enabled, name, greeting, buttons):
    chosen = [k for k in BOT_BUTTON_KEYS if k in (buttons or [])]
    values = {
        'enabled': '1' if enabled else '0',
        'name': (name or '').strip()[:40],
        'greeting': (greeting or '').strip()[:400],
        # Vacío es válido (menú sin botones); por eso se guarda aunque no haya ninguno.
        'buttons': ','.join(chosen),
    }
    for key, value in values.items():
        row = Setting.query.filter_by(key=SETTING_KEYS[key]).first()
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=SETTING_KEYS[key], value=value))
    db.session.commit()


def public_bot_config():
    """Lo que necesita el navegador, con las etiquetas de los botones."""
    cfg = get_bot_settings()
    labels = dict(BOT_BUTTONS)
    return {
        'enabled': cfg['enabled'] and bool(cfg['buttons']),
        'name': cfg['name'],
        'greeting': cfg['greeting'],
        'buttons': [{'key': k, 'label': labels[k]} for k in cfg['buttons']],
    }


# ─── Catálogo ───────────────────────────────────────────────────────────────

def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _id_kind(category_slug):
    """Qué se le pide al cliente para recargar: su ID de jugador (juegos), su correo
    (wallet: ahí el "ID" es el correo) o solo un correo de entrega (tarjetas)."""
    slug = (category_slug or '').lower()
    return 'email' if slug == 'wallet' else 'none' if slug == 'tarjetas' else 'player'


def list_games():
    """Juegos activos que tienen al menos un paquete activo, en el orden de la tienda."""
    from ..routes.verify import verifiable_game_ids
    verifiable = verifiable_game_ids()
    games = (Game.query.join(Category, Category.id == Game.category_id)
             .filter(Game.is_active.is_(True))
             .order_by(Category.id, Game.is_automated.desc(), Game.position.asc(), Game.name.asc())
             .all())
    result = []
    for game in games:
        if not game.packages.filter_by(is_active=True).first():
            continue
        result.append({
            'id': game.id,
            'slug': game.slug,
            'name': game.name,
            'image': game.image or None,
            'category': game.category.name if game.category else '',
            'requires_zone': bool(game.requires_zone_id),
            'player_id_label': game.player_id_label or 'Player ID',
            'zone_id_label': game.zone_id_label or 'Zona',
            'player_id_numeric': (game.player_id_input_type or 'numeric') == 'numeric',
            'id_kind': _id_kind(game.category.slug if game.category else ''),
            'verifiable': game.id in verifiable,
        })
    return result


def payment_currencies():
    """En qué monedas se puede pagar ahora: las de los métodos de pago activos
    (`account_currency`). Si hay alguno en dólares se muestran dólares; si solo
    hay en bolívares, solo Bs; si hay de los dos, ambos. Sin ningún método
    activo se muestran los dos (no hay nada que decidir)."""
    rows = PaymentMethod.query.filter_by(is_active=True).all()
    found = {'usd' if (m.account_currency or 'bs').lower() == 'usd' else 'bs' for m in rows}
    if not found:
        found = {'usd', 'bs'}
    return {'usd': 'usd' in found, 'bs': 'bs' in found}


def game_packages(game_id):
    """Paquetes de un juego con sus precios. Devuelve None si el juego no existe
    o un dict con `error` cuando la tienda no está mostrando precios."""
    # Se le pide a la tienda misma: así lo agotado, lo cerrado por horario y
    # la tasa del día coinciden con lo que ve el cliente en la página.
    from ..routes.main import api_packages
    from .timezone import today_ve_str
    # La tienda solo da paquetes a páginas abiertas hoy (cabecera X-Page-Day):
    # esta consulta es de este instante, así que se hace como una página de hoy.
    try:
        with current_app.test_request_context(f'/api/packages/{game_id}', headers={'X-Page-Day': today_ve_str()}):
            data = api_packages(game_id).get_json()
    except NotFound:
        return None
    if not data or data.get('page_expired'):
        return {'error': 'Estamos actualizando los precios. Intenta de nuevo en un momento.'}

    from ..routes.verify import verifiable_game_ids
    game = data['game']
    rate = _number(game.get('bs_rate_override')) or _number(data.get('usd_rate_bs')) or 0.0
    packages = []
    for p in data['packages']:
        base = _number(p.get('price'))
        usd = _number(p.get('usd_price'))
        usd = usd if usd is not None else base
        fixed_bs = _number(p.get('bs_price'))
        bs = fixed_bs if fixed_bs and fixed_bs > 0 else (round(base * rate, 2) if base is not None and rate > 0 else None)
        note = 'Agotado' if p.get('out_of_stock') else ('Cerrado ahora' if p.get('closed_now') else '')
        packages.append({
            'id': p['id'], 'name': p['name'], 'usd': usd, 'bs': bs,
            'available': not note, 'note': note,
            'image': p.get('image') or None,
        })
    return {
        'game': {
            'id': game['id'], 'slug': game['slug'], 'name': game['name'],
            'requires_zone': bool(game.get('requires_zone_id')),
            'player_id_label': game.get('player_id_label') or 'Player ID',
            'zone_id_label': game.get('zone_id_label') or 'Zona',
            'player_id_numeric': (game.get('player_id_input_type') or 'numeric') == 'numeric',
            'id_kind': _id_kind(game.get('category_slug')),
            'verifiable': game['id'] in verifiable_game_ids(),
        },
        'packages': packages,
        'currencies': payment_currencies(),
    }


def payment_methods():
    """Nombres y moneda de los métodos activos (lo usa el asistente de IA para
    contestar "¿cómo pago?"). Los datos de pago (cuenta, teléfono...) NO se dan
    aquí: se le muestran al cliente al pagar."""
    rows = PaymentMethod.query.filter_by(is_active=True).order_by(PaymentMethod.sort_order).all()
    return [{'name': m.name, 'currency': 'USD' if (m.account_currency or 'bs').lower() == 'usd' else 'Bs'} for m in rows]


def payment_method_cards():
    """Métodos de pago para comprar desde el chat, con los datos para pagar tal
    como los muestra la tienda (son públicos: la tienda los entrega a cualquier
    visitante): en Bs, banco + cédula/RIF + teléfono; en dólares, correo + Pay ID +
    teléfono. Binance con verificación automática trae su dirección y código en
    la cotización del checkout, no aquí."""
    rows = PaymentMethod.query.filter_by(is_active=True).order_by(PaymentMethod.sort_order).all()
    cards = []
    for m in rows:
        currency = 'usd' if (m.account_currency or 'bs').lower() == 'usd' else 'bs'
        data = []
        if currency == 'bs':
            data.append(('Banco', m.bank_name or m.name))
            if m.id_number:
                data.append(('Cédula/RIF', m.id_number))
            if m.contact_phone:
                data.append(('Teléfono', m.contact_phone))
        else:
            if m.contact_email:
                data.append(('Correo', m.contact_email))
            if m.pay_id:
                data.append(('Pay ID', m.pay_id))
            if m.contact_phone:
                data.append(('Teléfono', m.contact_phone))
        cards.append({
            'code': m.code, 'name': m.name, 'currency': currency,
            'rows': [{'label': label, 'value': value} for label, value in data],
        })
    return cards


# ─── Estado de un pedido ────────────────────────────────────────────────────

REFERENCE_MIN_DIGITS = 6
MAX_RESULTS = 3

# situación (ver support_ai.describe_order) → cómo se le explica al cliente.
# `tone` pinta la tarjeta; `human` muestra el botón "Hablar con soporte".
_STATUS_TEXT = {
    'completada': ('Completada', 'ok', False,
                   'La recarga ya fue enviada a tu cuenta. Si no la ves, cierra sesión y vuelve a entrar o '
                   'reinicia el juego: a veces tarda unos minutos en reflejarse.'),
    'recarga_en_proceso': ('En proceso', 'info', False,
                           'Tu pago está confirmado y la recarga se está procesando. Normalmente llega en pocos minutos.'),
    'verificando_pago': ('Verificando pago', 'info', False,
                         'Estamos verificando tu pago. Suele tardar unos minutos.'),
    'pago_confirmado_recarga_manual': ('Pago confirmado', 'ok', False,
                                       'Tu pago está confirmado. Este producto lo recarga el equipo a mano, '
                                       'en orden de llegada.'),
    'falta_dinero': ('Pago incompleto', 'warn', False,
                     'El pago llegó incompleto. Entra a tu orden para pagar el monto que falta: la recarga se '
                     'hace sola al confirmarse.'),
    'referencia_no_encontrada': ('Pago no encontrado', 'warn', True,
                                 'Todavía no vemos tu pago en el banco. Revisa que la referencia y el monto sean '
                                 'correctos; si ya pagaste, escríbenos con tu comprobante.'),
    'id_invalido': ('ID inválido', 'bad', True,
                    'El pago está confirmado pero el juego rechazó el ID de jugador. Escríbenos para corregirlo.'),
    'recarga_detenida': ('En revisión', 'bad', True,
                         'Tu pago está confirmado pero la recarga necesita revisión del equipo. Escríbenos y te ayudamos.'),
    'rechazada': ('Rechazada', 'bad', True,
                  'Esta orden fue rechazada. Escríbenos para revisar tu caso.'),
}


def _digits(value):
    return re.sub(r'\D', '', str(value or ''))


def find_orders(query):
    """Busca por número de orden (12 dígitos) o por referencia del pago
    (mínimo los últimos 6 dígitos): datos que solo tiene quien compró.
    El ID de jugador NO sirve aquí porque en los juegos es público."""
    digits = _digits(query)
    if len(digits) == 12:
        order = Order.query.filter_by(order_number=digits).first()
        if order:
            return [order]
    if len(digits) >= REFERENCE_MIN_DIGITS:
        from .receipt_check import reference_orders
        since = datetime.utcnow() - timedelta(days=60)
        # La misma regla de la tienda para "es la misma operación": iguales, o una es el final de la otra
        # (el comprobante trae el número completo y el cliente a veces escribió solo los últimos dígitos).
        return reference_orders(digits, limit=MAX_RESULTS, since=since)
    return []


def order_card(order):
    """Tarjeta para el cliente. No lleva ID de jugador, correo, teléfono ni
    la referencia: solo lo necesario para reconocer su pedido y saber qué pasa."""
    info = describe_order(order)
    label, tone, human, text = _STATUS_TEXT.get(
        info['situacion'], ('En revisión', 'info', True, 'Escríbenos y revisamos tu pedido.'))
    if info['situacion'] == 'falta_dinero':
        missing = _missing_amount_bs(order)
        if missing:
            text = f'El pago llegó incompleto: faltan Bs {missing}. Entra a tu orden para pagar el resto; la recarga se hace sola al confirmarse.'
    return {
        'number': order.order_number,
        'game': info['juego'],
        'package': info['paquete'],
        'date': info['fecha'],
        'status': label,
        'tone': tone,
        'text': text,
        'needs_support': human,
        'url': f'/order/{order.order_number}',
    }
