"""Asistente de IA (Gemini, DeepSeek u otra compatible) para el chat de soporte.

Solo resuelve preguntas, no vende: atiende las consultas sobre pedidos (en
qué estado está, si falta dinero, si la referencia todavía no aparece en el
banco, si el ID fue rechazado, o qué hacer si la recarga salió completada y
no se ve) y sobre la tienda (juegos, paquetes y precios, métodos de pago).
Para comprar, el cliente usa el botón "Comprar recarga" del chat. Todo lo
que dice sale de la base de datos (herramientas), nunca de lo que "se
imagina" el modelo.

Seguridad:
- El link de una orden solo se envía si el cliente demostró que es suya:
  dio el número de orden o la referencia del pago (o el comprobante), o el
  chat ya venía con esa orden. Con solo el ID de jugador (que en los juegos
  es público) se le dice el estado, pero no se le manda el link, porque la
  página de la orden muestra el correo del cliente.
- Nunca se le pasan al modelo correos, teléfonos ni notas internas.
- Un comprobante lo lee el servidor (ver receipt_check.py): referencia, monto, si ese
  pago ya está en una orden y si se pagó a los datos de la tienda. Si NO está en el
  sistema y SÍ es a nuestros datos, el servidor (no el modelo) adjunta el botón para
  crear la orden en el chat con ese comprobante.

Cuando no puede resolver (lo pide el cliente, ID inválido, orden rechazada,
o varios intentos sin encontrar su pedido) pasa el chat a un humano: se
apaga la IA en ese chat y se le pone la etiqueta configurada en el admin.

Además clasifica cada chat según su caso con las etiquetas que el admin ya
tiene (Pago no verificado, Recarga no llegó, ID incorrecto...): solo puede
PONER etiquetas existentes (herramienta etiquetar_chat o el campo `etiqueta`
de pasar_a_humano); no las crea, no las quita y "Sin identificar" lo mantiene
el sistema (support.sync_unidentified_tag).
"""

import json
import re
import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import current_app

from ..models import Order, Setting, SupportChat, SupportMessage, SupportTag, db
from . import receipt_check
from .locks import acquire_lock, release_lock
from .support import DEFAULT_CLIENT_NAME, add_tag
from .timezone import format_ve

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
# Gemini habla el mismo formato (OpenAI) por esta dirección: así el asistente usa la
# MISMA llave que la tienda ya tiene para leer comprobantes (GEMINI_API_KEY).
GEMINI_OPENAI_URL = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
DEFAULT_MODEL = 'deepseek-chat'
PROVIDERS = ('gemini', 'deepseek', 'custom')
PROVIDER_NAMES = {'gemini': 'Gemini', 'deepseek': 'DeepSeek', 'custom': 'la IA'}
DEFAULT_MAX_FAILED = 3
HISTORY_MESSAGES = 16
# Tope de respuestas automáticas por chat y por hora: cada una cuesta una llamada a la IA y sin tope
# alguien podría escribir sin parar para inflar la cuenta. Pasado el tope atiende una persona.
MAX_AI_REPLIES_PER_CHAT_PER_HOUR = 30
MAX_TOOL_ROUNDS = 4
REQUEST_TIMEOUT = 45
TYPING_SECONDS = 90
REFERENCE_MIN_DIGITS = 6
# Tope de TODA la tienda para buscar un pedido por su referencia (menú de estado + foto del comprobante + asistente).
REFERENCE_LOOKUPS_ALL_PER_MINUTE = 30
REFERENCE_LOOKUPS_ALL_PER_HOUR = 240

SETTING_KEYS = {
    'enabled': 'support_ai_enabled',
    'provider': 'support_ai_provider',
    # Cada proveedor tiene SU llave, para que una nunca viaje al servicio de otro:
    # DeepSeek usa `api_key`, la IA propia usa `custom_api_key` y Gemini no guarda
    # ninguna (usa la del servidor, la de los comprobantes).
    'api_key': 'support_ai_api_key',
    'custom_api_key': 'support_ai_custom_api_key',
    'model': 'support_ai_model',
    'handoff_tag_id': 'support_ai_handoff_tag_id',
    'max_failed': 'support_ai_max_failed',
    'instructions': 'support_ai_instructions',
    # Solo para la opción "otra IA" (p. ej. Ollama en una PC propia:
    # http://IP:11434/v1/chat/completions).
    'base_url': 'support_ai_base_url',
}


# ─── Configuración (admin → Soporte → Asistente IA) ─────────────────────────

def _setting(key, default=''):
    row = Setting.query.filter_by(key=SETTING_KEYS[key]).first()
    return (row.value or '').strip() if row and row.value is not None else default


def _gemini_key():
    """La llave de Gemini del servidor: la misma que lee los comprobantes."""
    return str(current_app.config.get('GEMINI_API_KEY') or '').strip()


def gemini_key_configured():
    return bool(_gemini_key())


def _gemini_models():
    from .reference_extraction import GEMINI_MODEL_CANDIDATES
    return list(GEMINI_MODEL_CANDIDATES)


def _infer_provider(saved_url):
    """Para ajustes guardados antes de existir el selector de proveedor."""
    saved_url = (saved_url or '').strip()
    if saved_url.startswith('https://api.deepseek.com'):
        return 'deepseek'
    if not saved_url or saved_url.startswith('https://generativelanguage.googleapis.com'):
        return 'gemini'
    return 'custom'


def get_ai_settings():
    try:
        max_failed = int(_setting('max_failed') or DEFAULT_MAX_FAILED)
    except ValueError:
        max_failed = DEFAULT_MAX_FAILED
    tag_id = _setting('handoff_tag_id')
    saved_url = _setting('base_url')
    provider = _setting('provider')
    if provider not in PROVIDERS:
        provider = _infer_provider(saved_url)
    saved_key = _setting('api_key')
    custom_key = _setting('custom_api_key')
    # Ajustes de antes del selector: una IA propia con llave la guardaba en `api_key`.
    if provider == 'custom' and not custom_key and not _setting('provider'):
        custom_key = saved_key
    model = _setting('model')

    if provider == 'gemini':
        base_url, api_key = GEMINI_OPENAI_URL, _gemini_key()
        # Un modelo de otro proveedor guardado de antes no sirve aquí.
        if not model.lower().startswith('gemini'):
            model = ''
    elif provider == 'deepseek':
        base_url, api_key = DEEPSEEK_URL, saved_key
        if not model or model.lower().startswith('gemini'):
            model = DEFAULT_MODEL
    else:
        base_url, api_key = saved_url or DEEPSEEK_URL, custom_key
        model = model or DEFAULT_MODEL

    return {
        'enabled': _setting('enabled') == '1',
        'provider': provider,
        'api_key': api_key,              # la que se usa de verdad
        'saved_api_key': saved_key,      # la de DeepSeek guardada en el panel
        'saved_custom_key': custom_key,  # la de la IA propia guardada en el panel
        'model': model or _gemini_models()[0],
        'model_is_default': not model if provider == 'gemini' else False,
        'handoff_tag_id': int(tag_id) if tag_id.isdigit() else None,
        'max_failed': max(1, min(max_failed, 10)),
        'instructions': _setting('instructions'),
        'base_url': base_url,
    }


def normalize_base_url(url):
    url = (url or '').strip()
    if not url:
        return DEEPSEEK_URL
    if not re.match(r'^https?://', url):
        raise ValueError('La URL de la IA debe empezar con http:// o https://')
    # Si pegan solo la base (…/v1), se completa la ruta del chat.
    if not url.rstrip('/').endswith('/chat/completions'):
        url = url.rstrip('/') + '/chat/completions'
    return url[:300]


def uses_deepseek(settings):
    return settings['provider'] == 'deepseek'


def save_ai_settings(enabled, api_key, model, handoff_tag_id, max_failed, instructions, base_url=None, provider=None,
                     custom_api_key=None):
    if provider not in PROVIDERS:
        # Sin proveedor (llamadas de antes del selector): DeepSeek salvo que traigan otra URL.
        provider = _infer_provider(base_url) if (base_url or '').strip() else 'deepseek'
    model = (model or '').strip()[:60]
    # Modelo de otro proveedor (se queda en el campo al cambiar de opción): se descarta.
    if (provider == 'gemini' and not model.lower().startswith('gemini')) or \
            (provider == 'deepseek' and model.lower().startswith('gemini')):
        model = ''
    values = {
        'provider': provider,
        'model': model,
        'handoff_tag_id': str(handoff_tag_id or ''),
        'max_failed': str(max_failed or DEFAULT_MAX_FAILED),
        'instructions': (instructions or '').strip()[:3000],
        'enabled': '1' if enabled else '',
        'base_url': (normalize_base_url(base_url) if provider == 'custom'
                     else GEMINI_OPENAI_URL if provider == 'gemini' else DEEPSEEK_URL),
    }
    # La clave solo se cambia si escribieron una nueva: el campo se muestra
    # vacío para no dejarla a la vista en el panel. Cada proveedor guarda la suya
    # y Gemini ninguna. (Sin `custom_api_key`, una llamada de antes con IA propia
    # trae su llave en `api_key`.)
    if provider == 'deepseek':
        typed, key_name = api_key, 'api_key'
    elif provider == 'custom':
        typed, key_name = (custom_api_key if custom_api_key is not None else api_key), 'custom_api_key'
    else:
        typed, key_name = '', ''
    if typed and typed.strip():
        values[key_name] = typed.strip()[:200]
    for key, value in values.items():
        row = Setting.query.filter_by(key=SETTING_KEYS[key]).first()
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=SETTING_KEYS[key], value=value))
    db.session.commit()


def _has_credentials(s):
    # DeepSeek y Gemini exigen llave; una IA propia (Ollama) normalmente no.
    return bool(s['api_key']) or s['provider'] == 'custom'


def is_ai_available():
    s = get_ai_settings()
    return s['enabled'] and _has_credentials(s)


def _headers(api_key):
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


# ─── Qué pasa con una orden (en palabras para el cliente) ───────────────────

def _money(value):
    return str(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _automation(order):
    try:
        return json.loads(order.automation_response or '{}') or {}
    except (TypeError, ValueError):
        return {}


def _missing_amount_bs(order):
    from .payment_verification import _get_expected_order_amount
    expected, error = _get_expected_order_amount(order)
    if error or expected is None:
        return None
    missing = expected - Decimal(str(order.paid_amount_bs or 0))
    return _money(missing) if missing > 0 else None


def _support_schedule():
    row = Setting.query.filter_by(key='support_schedule').first()
    return (row.value or '').strip() if row else ''


def describe_order(order):
    """Estado de la orden explicado para el cliente. Es lo único que el
    modelo sabe de ella: un código de situación, qué decirle y qué debe
    hacer."""
    auto = _automation(order)
    blocked = (auto.get('blocked_cause') or '').strip()
    age = datetime.utcnow() - (order.created_at or datetime.utcnow())
    situation, what, next_step = 'desconocido', '', ''

    if order.status == 'completed':
        situation = 'completada'
        what = 'La recarga ya fue enviada a su cuenta.'
        next_step = ('Si no la ve: cerrar sesión y volver a entrar o reiniciar el juego; a veces tarda unos minutos '
                     'en reflejarse. Si después de 30 minutos no aparece, pasarlo con un agente.')
    elif order.status == 'rejected':
        situation = 'rechazada'
        what = 'La orden fue rechazada.'
        next_step = 'Un agente debe revisar el caso: pasarlo a humano.'
    elif order.status == 'approved':
        if blocked == 'player':
            situation = 'id_invalido'
            what = 'El pago está confirmado, pero el juego rechazó el ID de jugador (ID inválido).'
            next_step = 'Un agente debe corregir el ID y reenviar la recarga: pedirle el ID correcto y pasarlo a humano.'
        elif blocked:
            situation = 'recarga_detenida'
            what = 'El pago está confirmado, pero la recarga se detuvo y necesita revisión del equipo.'
            next_step = 'Pasarlo a humano.'
        else:
            situation = 'recarga_en_proceso'
            what = 'El pago está confirmado y la recarga se está procesando.'
            next_step = 'Normalmente llega en pocos minutos. Si pasan más de 30 minutos, pasarlo a humano.'
    elif order.awaiting_payment_completion:
        missing = _missing_amount_bs(order)
        situation = 'falta_dinero'
        what = ('El pago llegó incompleto: el banco reportó menos de lo que cuesta el pedido'
                + (f' (faltan Bs {missing}).' if missing else '.'))
        next_step = ('Entrar al link de su orden y pagar ahí el monto restante; al confirmarse el pago la recarga '
                     'se hace automáticamente.')
    elif order.payment_verified_at:
        schedule = _support_schedule()
        situation = 'pago_confirmado_recarga_manual'
        what = 'El pago está confirmado. Este producto se recarga a mano por el equipo.'
        next_step = 'Se procesa en orden de llegada' + (f' dentro del horario: {schedule}.' if schedule else '.')
    elif (order.payment_verification_attempts or 0) >= 2 and age > timedelta(minutes=10):
        situation = 'referencia_no_encontrada'
        what = f'Todavía no encontramos el pago en el banco con la referencia que registró.'
        next_step = ('Verificar que la referencia y el monto sean correctos (a veces el banco tarda unos minutos). '
                     'Si está seguro de que pagó, que envíe el comprobante; si sigue sin aparecer, pasarlo a humano.')
    else:
        situation = 'verificando_pago'
        what = 'Estamos verificando el pago.'
        next_step = 'Suele tardar unos minutos. Puede seguir el estado en el link de su orden.'

    return {
        'numero_orden': order.order_number,
        'juego': order.game.name if order.game else '',
        'paquete': order.package.name if order.package else '',
        'id_jugador': order.player_id or '',
        'fecha': format_ve(order.created_at, '%d/%m/%Y %I:%M %p') if order.created_at else '',
        'referencia_registrada_ultimos_digitos': (order.payment_reference or '')[-6:],
        'situacion': situation,
        'que_pasa': what,
        'que_debe_hacer': next_step,
    }


# ─── Búsqueda de órdenes con permiso ────────────────────────────────────────

def _digits(value):
    return re.sub(r'\D', '', str(value or ''))


def _verified_ids(chat):
    ids = {int(x) for x in (chat.ai_verified_orders or '').split(',') if x.strip().isdigit()}
    if chat.order_id:
        ids.add(chat.order_id)
    if chat.context_order_number:
        order = Order.query.filter_by(order_number=chat.context_order_number).first()
        if order:
            ids.add(order.id)
    return ids


def _mark_verified(chat, orders):
    ids = _verified_ids(chat) | {o.id for o in orders}
    chat.ai_verified_orders = ','.join(str(i) for i in sorted(ids))[-500:]


def _owned_by_chat_user(chat, order):
    return bool(chat.user_id and order.user_id == chat.user_id)


def search_orders_for_chat(chat, order_number=None, reference=None, player_id=None):
    """Busca las órdenes y marca como 'comprobadas' las que el cliente
    identificó con un dato que solo tiene quien pagó (número de orden o
    referencia)."""
    since = datetime.utcnow() - timedelta(days=60)
    found, proven = [], []

    number = _digits(order_number)
    if number:
        order = Order.query.filter_by(order_number=number).first()
        if order:
            found.append(order)
            proven.append(order)

    ref = _digits(reference)
    proven_by_ref = []
    # La búsqueda por referencia comparte el tope de TODA la tienda con el menú de estado y la foto del comprobante (ver
    # reference_lookup_budget_ok): un número de 6 dígitos no debe poder adivinarse repartiendo intentos entre muchas rutas o conexiones.
    if len(ref) >= REFERENCE_MIN_DIGITS and reference_lookup_budget_ok():
        # La misma regla de la tienda para "es la misma operación": iguales, o una es el final de la otra.
        for order in receipt_check.reference_orders(ref, limit=3, since=since):
            if order not in found:
                found.append(order)
            proven.append(order)
            proven_by_ref.append(order.id)

    pid = str(player_id or '').strip()
    if pid:
        rows = (Order.query
                .filter(Order.player_id == pid, Order.created_at >= datetime.utcnow() - timedelta(days=30))
                .order_by(Order.created_at.desc()).limit(3).all())
        for order in rows:
            if order not in found:
                found.append(order)

    if proven:
        _mark_verified(chat, proven)
    verified = _verified_ids(chat)
    results = []
    for order in found[:5]:
        info = describe_order(order)
        info['puedo_enviar_link'] = order.id in verified or _owned_by_chat_user(chat, order)
        if order.id not in proven_by_ref:
            # Los últimos dígitos de la referencia son el segundo dato con el que se desbloquea un PIN o se prueba que un ID es tuyo:
            # solo se le muestran a quien acaba de escribir esa referencia. Con solo el ID de jugador, el número de orden o una sesión sin
            # clave (entrar con un ID) no se sabe si quien escribe es quien pagó.
            info.pop('referencia_registrada_ultimos_digitos', None)
        results.append(info)
    return results


def reference_lookup_budget_ok():
    """Tope de TODA la tienda (todas las conexiones juntas) para buscar un pedido por los 6 dígitos de su referencia, repartido entre el
    menú de estado, la foto del comprobante y el asistente. Se llama DESPUÉS de los topes por IP de cada ruta, así una sola conexión
    no puede gastarlo sola. Devuelve False cuando se agotó (el que pregunta recibe "intenta en unos minutos")."""
    from .locks import check_rate_limit
    minute_ok = check_rate_limit('support_ref_lookup_all', REFERENCE_LOOKUPS_ALL_PER_MINUTE, 60)
    hour_ok = check_rate_limit('support_ref_lookup_all_h', REFERENCE_LOOKUPS_ALL_PER_HOUR, 3600)
    return bool(minute_ok and hour_ok)


def orders_already_known(chat):
    """Órdenes que el chat ya trae (vino desde la página de su orden, o el
    admin la vinculó): se le dan al modelo desde el principio."""
    known = [Order.query.get(i) for i in _verified_ids(chat)]
    known += (Order.query.filter_by(user_id=chat.user_id).order_by(Order.created_at.desc()).limit(3).all()
              if chat.user_id else [])
    seen, out = set(), []
    for order in known:
        if order and order.id not in seen:
            seen.add(order.id)
            info = describe_order(order)
            info['puedo_enviar_link'] = True
            # Sin los últimos dígitos de la referencia: ese dato abre PINs y prueba "este ID es mío", y estas órdenes llegan por una sesión
            # sin clave o por un número de orden que el navegador mandó sin prueba.
            info.pop('referencia_registrada_ultimos_digitos', None)
            out.append(info)
    return out[:4]


# ─── Mensajes del asistente ─────────────────────────────────────────────────

def add_ai_message(chat, body, action_url=None, action_label=None):
    from .support import clean_body
    message = SupportMessage(
        chat_id=chat.id, sender='admin', is_ai=True,
        body=clean_body(body, allow_empty=bool(action_url)),
        action_url=action_url, action_label=(action_label or '')[:60] or None,
    )
    db.session.add(message)
    chat.status = 'waiting_client'
    chat.unread_client = (chat.unread_client or 0) + 1
    # Lo que la IA ya respondió no queda como pendiente para el equipo.
    chat.unread_admin = 0
    chat.last_message_at = datetime.utcnow()
    return message


def hand_off_to_human(chat, reason=''):
    """Apaga la IA en este chat, le pone la etiqueta configurada y lo deja
    como pendiente para el equipo (con aviso por correo)."""
    from .support import add_system_message, add_tag
    from .notifications import notify_support_client_message

    chat.ai_active = False
    chat.ai_handoff_at = datetime.utcnow()
    settings = get_ai_settings()
    tag = SupportTag.query.get(settings['handoff_tag_id']) if settings['handoff_tag_id'] else None
    if tag:
        add_tag(chat, tag)
    add_system_message(chat, 'El asistente pasó este chat a un agente humano.' + (f' Motivo: {reason}' if reason else ''))
    chat.status = 'open'
    chat.unread_admin = max(1, chat.unread_admin or 0)
    db.session.commit()
    last = (SupportMessage.query.filter_by(chat_id=chat.id, sender='client')
            .order_by(SupportMessage.id.desc()).first())
    if last:
        try:
            notify_support_client_message(chat, last)
        except Exception:
            pass


# ─── Etiquetas por caso ─────────────────────────────────────────────────────

# "Sin identificar" no se le deja a la IA: la mantiene sola `support.sync_unidentified_tag` (entra y sale según se sepa de quién es el chat).
AUTO_TAG_SLUGS = ('sin-identificar',)


def assignable_tags():
    """Etiquetas activas del catálogo que la IA puede poner. Solo las que ya existen: la IA no crea, edita ni quita etiquetas."""
    return (SupportTag.query
            .filter(SupportTag.is_active.is_(True), ~SupportTag.slug.in_(AUTO_TAG_SLUGS))
            .order_by(SupportTag.kind, SupportTag.sort_order, SupportTag.id).all())


def find_assignable_tag(name):
    """La etiqueta que corresponde a lo que escribió el modelo (por nombre o por slug, sin importar mayúsculas ni tildes), o None."""
    wanted = _norm(name)
    if not wanted:
        return None
    for tag in assignable_tags():
        # También vale "Nombre (Tipo)" tal como se ve en la lista del admin y en las reglas.
        if wanted in (_norm(tag.name), _norm(tag.slug), _norm(f'{tag.name} {tag.kind_label}')):
            return tag
    return None


MANUAL_PENDING_SITUATION = 'pago_confirmado_recarga_manual'     # ver describe_order: pagó, y la recarga la hace una persona del equipo


def auto_tag_manual_orders(chat, infos):
    """Pedido de recarga MANUAL con el pago ya confirmado: el sistema (no el modelo) le pone al chat la etiqueta "<JUEGO> (Pendiente)" del
    juego del pedido (TIKTOK (Pendiente), ZINLI (Pendiente)...) para que la asistente humana lo vea en su bandeja y haga la recarga. El nombre del
    juego se toma sin lo que va entre paréntesis ("TIKTOK (Monedas)" → "TIKTOK"). Solo con pedidos que el cliente demostró que son suyos
    (`puedo_enviar_link`). Si ese juego no tiene etiqueta, no hace nada: si el dueño crea "Telegram (Pendiente)", "Binance (Pendiente)"..., se usa sola."""
    for info in infos or []:
        if info.get('situacion') != MANUAL_PENDING_SITUATION or not info.get('puedo_enviar_link'):
            continue
        base = re.sub(r'\s*\([^)]*\)\s*$', '', str(info.get('juego') or '')).strip()
        tag = find_assignable_tag(f'{base} (Pendiente)') if base else None
        if tag:
            add_tag(chat, tag)


# ─── Catálogo para el asistente ─────────────────────────────────────────────

MAX_CATALOG_PACKAGES = 40


def _norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower()).strip()


def catalog_for_ai(game_query=None):
    """Lo que ve el asistente cuando pregunta por precios. Usa las mismas
    funciones que el menú de botones del chat (y por lo tanto la misma
    fuente que la tienda), así que no puede dar un precio distinto."""
    from . import support_bot

    games = support_bot.list_games()
    wanted = _norm(game_query)
    matches = []
    if wanted:
        matches = [g for g in games if _norm(g['name']) == wanted or _norm(g['slug']) == wanted]
        if not matches:
            matches = [g for g in games if wanted in _norm(g['name']) or _norm(g['name']) in wanted]
    if len(matches) != 1:
        return {
            'juegos': [{'nombre': g['name'], 'categoria': g['category']} for g in games],
            'nota': ('Estos son los juegos que se venden. Para ver precios, llama otra vez con el nombre '
                     'exacto de uno.' if games else 'Ahora mismo no hay juegos disponibles.'),
        }

    data = support_bot.game_packages(matches[0]['id'])
    if not data or data.get('error'):
        return {'error': (data or {}).get('error') or 'No se pudo consultar el juego.'}
    # Solo las monedas en que se puede pagar ahora (según los métodos de pago activos);
    # si un paquete no tiene precio en ellas, se da el que tenga.
    currencies = data.get('currencies') or {'usd': True, 'bs': True}
    packages = []
    for p in data['packages'][:MAX_CATALOG_PACKAGES]:
        item = {'nombre': p['name'], 'disponible': p['available'], 'nota': p['note']}
        if currencies['usd'] and p['usd'] is not None:
            item['precio_usd'] = p['usd']
        if currencies['bs'] and p['bs'] is not None:
            item['precio_bs'] = p['bs']
        if 'precio_usd' not in item and 'precio_bs' not in item:
            if p['usd'] is not None:
                item['precio_usd'] = p['usd']
            elif p['bs'] is not None:
                item['precio_bs'] = p['bs']
        packages.append(item)
    accepted = ' y '.join(name for key, name in (('usd', 'dólares ($)'), ('bs', 'bolívares (Bs)')) if currencies[key])
    return {
        'juego': data['game']['name'],
        'paquetes': packages,
        'nota': f'Se paga en {accepted}. Los montos en Bs usan la tasa de hoy: el total exacto se ve al pagar.',
    }


def payment_methods_for_ai():
    from . import support_bot
    methods = support_bot.payment_methods()
    return {
        'metodos': methods,
        'nota': 'Los datos para pagar (cuenta, teléfono...) se muestran al cliente en la tienda al pagar: no los des tú.',
    } if methods else {'metodos': [], 'nota': 'Ahora mismo no hay métodos de pago disponibles.'}


# ─── Conversación con la IA ─────────────────────────────────────────────────

TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'buscar_ordenes',
            'description': ('Busca pedidos en la base de datos de la tienda y devuelve su estado real. '
                            'Usa al menos uno: número de orden (12 dígitos), referencia del pago '
                            '(mínimo los últimos 6 dígitos) o ID de jugador.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'numero_orden': {'type': 'string', 'description': 'Número de orden de 12 dígitos'},
                    'referencia': {'type': 'string', 'description': 'Referencia o número de operación del pago'},
                    'id_jugador': {'type': 'string', 'description': 'ID del jugador en el juego'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'enviar_link_orden',
            'description': ('Adjunta a tu respuesta un botón con el link de la orden. Solo funciona con órdenes '
                            'marcadas puedo_enviar_link=true.'),
            'parameters': {
                'type': 'object',
                'properties': {'numero_orden': {'type': 'string'}},
                'required': ['numero_orden'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'consultar_catalogo',
            'description': ('Precios y paquetes de la tienda, en vivo. Sin argumentos devuelve los juegos que se '
                            'venden; con el nombre de un juego devuelve sus paquetes con precio en USD y en Bs '
                            'y si están disponibles.'),
            'parameters': {
                'type': 'object',
                'properties': {'juego': {'type': 'string', 'description': 'Nombre del juego (ej. Free Fire)'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'metodos_de_pago',
            'description': 'Métodos de pago que acepta la tienda ahora mismo (nombre y moneda).',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'etiquetar_chat',
            'description': ('Pone al chat una etiqueta que clasifica el caso del cliente (para que el equipo lo encuentre y lo '
                            'ordene). Solo sirven las etiquetas de la lista de las reglas, con su nombre exacto. Puedes poner '
                            'más de una.'),
            'parameters': {
                'type': 'object',
                'properties': {'etiqueta': {'type': 'string', 'description': 'Nombre exacto de la etiqueta'}},
                'required': ['etiqueta'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'pasar_a_humano',
            'description': ('Pasa el chat a un agente humano del equipo. Si sabes el caso, pon en `etiqueta` el nombre exacto de la etiqueta '
                            'que corresponde. Después de llamarla, despídete avisando que un agente lo atenderá.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'motivo': {'type': 'string', 'description': 'Motivo corto para el equipo'},
                    'etiqueta': {'type': 'string', 'description': 'Opcional: nombre exacto de la etiqueta del caso'},
                },
                'required': ['motivo'],
            },
        },
    },
]


def _system_prompt(chat, settings, known_orders):
    from . import support_bot
    schedule = _support_schedule()
    buy_in_chat = support_bot.public_bot_config()['enabled']
    parts = [
        'Eres el asistente virtual de soporte de 3S Recargas, una tienda venezolana de recargas de juegos '
        '(diamantes de Free Fire, Blood Strike y otros). Hablas con un cliente por el chat de la web.',
        'Reglas:',
        '- Responde en español, breve (1 a 4 frases), amable y claro. Sin markdown ni asteriscos.',
        '- NUNCA inventes el estado de un pedido: consulta siempre con la herramienta buscar_ordenes y usa '
        'solo lo que devuelve (que_pasa y que_debe_hacer).',
        '- Para consultar un pedido NUNCA pidas el número de orden ni el ID de jugador: pide solo el comprobante '
        'del pago (la foto o captura). El sistema lee la referencia y te cuenta si ya hay una orden con ese pago. '
        'Si no puede mandar la foto, acepta que escriba la referencia (al menos los últimos 6 dígitos); si tampoco '
        'la tiene, pasa a humano. Si el cliente te da por su cuenta un número de orden o un ID de jugador, úsalos '
        'con buscar_ordenes; con solo el ID no puedes enviar el link de la orden (por seguridad): dile el estado y '
        'pídele el comprobante del pago.',
        '- Cuando la orden tenga puedo_enviar_link=true y el cliente deba hacer algo en ella (completar un '
        'pago que falta, ver el estado), usa enviar_link_orden.',
        '- situacion=falta_dinero: explícale cuánto falta, envíale el link y dile que en esa página puede '
        'pagar el restante y la recarga se hace sola al confirmarse el pago.',
        '- situacion=completada y dice que no le llegó: dale los datos de la orden (paquete, ID, fecha) y '
        'dile que cierre sesión y vuelva a entrar o reinicie el juego. Si ya lo hizo y pasaron más de '
        '30 minutos, pasa a humano.',
        '- situacion=id_invalido, rechazada o recarga_detenida: explícale y pasa a humano.',
        '- situacion=referencia_no_encontrada: pídele que revise la referencia y el monto y que envíe el '
        'comprobante; si insiste en que pagó bien, pasa a humano.',
        '- Llama pasar_a_humano si el cliente pide hablar con una persona, si es un reclamo o algo que no '
        'puedes resolver con los datos, o si ya intentaste varias veces sin encontrar su pedido.',
        '- No prometas reembolsos ni cambios, no pidas contraseñas ni datos bancarios, y nunca des datos de '
        'pedidos que no sean de quien escribe.',
        '- Puedes responder precios y paquetes con consultar_catalogo y métodos de pago con metodos_de_pago. '
        'NUNCA inventes precios ni paquetes: di solo lo que devuelven. Los montos en Bs dependen de la tasa '
        'del día, avísalo. Si un paquete sale no disponible, dilo.',
        '- Tú solo resuelves preguntas: no tomas pedidos ni pagos por el chat. Si el cliente quiere comprar, '
        'dile que toque el botón "Comprar recarga" que está abajo del chat (o entre a la tienda) y ahí elige '
        'su paquete y paga. No des datos de cuentas de pago: se ven al pagar.',
        '- Cuando el cliente manda la foto de un comprobante, el sistema la lee y te lo cuenta entre paréntesis '
        '("Lectura automática del comprobante"): referencia, monto, si ya hay una orden con ese pago y si fue '
        'pagado a los datos de la tienda. Usa SOLO eso: no adivines nada más de la imagen.',
        '- Comprobante con "ya hay una orden: SÍ": usa buscar_ordenes con esa referencia y dile el estado como siempre. '
        'Si dice que es una orden de hace más de 60 días, llama pasar_a_humano.',
        ('- Comprobante con "ya hay una orden: NO" y "pagado a los datos de la tienda: SÍ": dile que ese pago todavía '
         'no tiene una orden y que puede crearla aquí mismo con ese comprobante; el sistema le pone debajo de tu '
         'mensaje el botón "Crear mi orden con este comprobante". No uses buscar_ordenes ni le pidas más datos: '
         'solo invítalo a tocar el botón.' if buy_in_chat else
         '- Comprobante con "ya hay una orden: NO" y "pagado a los datos de la tienda: SÍ": dile que ese pago todavía '
         'no tiene una orden y que la haga en la tienda adjuntando ese comprobante.'),
        '- Comprobante pagado a otros datos, o que no se pudo comprobar a quién se pagó: NO lo invites a crear la '
        'orden. Dile en una frase que no puedes confirmar ese pago y llama pasar_a_humano para que el equipo lo revise.',
        '- Si no se pudo leer el comprobante, pídele una foto más clara o que escriba la referencia.',
        '- Si te pregunta algo general de la tienda que no sabes, dilo y ofrece pasarlo con un agente.',
        (f'El cliente se llama {chat.client_name}.' if chat.client_name != DEFAULT_CLIENT_NAME
         else 'No sabemos cómo se llama el cliente: no lo llames por ningún nombre.'),
    ]
    tags = assignable_tags()
    if tags:
        parts.append('- Etiquetas para clasificar el caso del chat: ' + '; '.join(f'{t.name} ({t.kind_label})' for t in tags) + '. En cuanto '
                     'identifiques el caso, ponla con etiquetar_chat (o en el campo `etiqueta` de pasar_a_humano), con el nombre exacto, sin '
                     'el paréntesis del tipo. Puedes poner más de una. Si ninguna encaja, no pongas ninguna. Nunca inventes etiquetas.')
    if schedule:
        parts.append(f'Horario de atención del equipo humano: {schedule}.')
    if known_orders:
        parts.append('Pedidos que ya sabemos que son de este cliente: '
                     + json.dumps(known_orders, ensure_ascii=False))
    if settings['instructions']:
        parts.append('Instrucciones adicionales de la tienda: ' + settings['instructions'])
    return '\n'.join(parts)


def _history(chat):
    rows = (SupportMessage.query
            .filter(SupportMessage.chat_id == chat.id, SupportMessage.sender.in_(('client', 'admin')))
            .order_by(SupportMessage.id.desc()).limit(HISTORY_MESSAGES).all())
    messages = []
    for m in reversed(rows):
        if m.is_deleted:
            continue
        text = m.body or ''
        if m.attachment:
            text += '\n[Adjuntó una imagen]'
            if m.ai_note:
                text += f' {receipt_check.note_for_model(m.ai_note)}'
        if m.sender == 'client':
            messages.append({'role': 'user', 'content': text.strip() or '(mensaje vacío)'})
        else:
            messages.append({'role': 'assistant', 'content': text.strip() or '(link enviado)'})
    return messages


def _post_chat(settings, api_key, body):
    """POST al servicio de IA. Con Gemini y el modelo por defecto, si Google retira
    o no ofrece un modelo se prueba el siguiente de la lista (igual que el lector
    de comprobantes). Devuelve la última respuesta."""
    from .reference_extraction import _should_retry_with_fallback

    models = [settings['model']]
    if settings['provider'] == 'gemini' and settings.get('model_is_default'):
        models = _gemini_models()
    response = None
    for index, model in enumerate(models):
        response = requests.post(
            settings['base_url'], headers=_headers(api_key),
            json={**body, 'model': model}, timeout=REQUEST_TIMEOUT,
        )
        if response.status_code < 400 or index == len(models) - 1:
            break
        if not _should_retry_with_fallback(response.status_code, response.text):
            break
    return response


def _call_ai(settings, messages):
    # Los modelos "con pensamiento" de Gemini gastan parte del límite pensando.
    response = _post_chat(settings, settings['api_key'], {
        'messages': messages,
        'tools': TOOLS,
        'temperature': 0.3,
        'max_tokens': 1500 if settings['provider'] == 'gemini' else 500,
    })
    if response.status_code >= 400:
        raise RuntimeError(f'La IA respondió {response.status_code}: {response.text[:200]}')
    return response.json()['choices'][0]['message']


def _read_pending_attachments(chat, instructions=''):
    """Si el cliente mandó una foto (comprobante), se lee con el lector de
    comprobantes que ya usa la tienda (referencia, monto y a quién se pagó), se
    comprueba si ese pago ya está en una orden y si es a los datos de la tienda,
    y el resultado se guarda como nota interna del mensaje (ver receipt_check)."""
    import os
    from . import receipt_check
    from .reference_extraction import extract_receipt_details_from_image_path
    from .support import is_video_attachment

    pending = (SupportMessage.query
               .filter(SupportMessage.chat_id == chat.id, SupportMessage.sender == 'client',
                       SupportMessage.attachment.isnot(None), SupportMessage.ai_note.is_(None))
               .order_by(SupportMessage.id.desc()).limit(2).all())
    for m in pending:
        if is_video_attachment(m.attachment):
            m.ai_note = '(es un video, no se puede leer)'
            continue
        try:
            result = extract_receipt_details_from_image_path(
                os.path.join(current_app.config['UPLOAD_FOLDER'], m.attachment))
            if not (result or {}).get('ok'):
                # El motivo real (llave sin configurar, servicio caído...) es del admin: al cliente solo se le pide otra foto.
                current_app.logger.warning('[SupportAI] no se pudo leer un comprobante: %s', (result or {}).get('message'))
            note = receipt_check.analyze(result, instructions)
        except Exception:
            current_app.logger.exception('[SupportAI] no se pudo analizar un comprobante')
            note = {'v': receipt_check.NOTE_VERSION, 'read': False}
        m.ai_note = receipt_check.encode_note(note)
    db.session.commit()


RECEIPT_BUTTON_PREFIX = '#comprar:'
RECEIPT_BUTTON_LABEL = 'Crear mi orden con este comprobante'
RECEIPT_INVITE_TEXT = ('Ese pago todavía no tiene una orden en nuestro sistema y sí fue hecho a nuestros datos. '
                       'Puedes crear tu orden aquí mismo con ese comprobante: toca el botón de abajo.')


def _receipt_invitation(chat):
    """(url, etiqueta) del botón para crear la orden con el comprobante que el
    cliente acaba de mandar, o None. La decide el servidor con la nota guardada,
    no el modelo: hace falta que el comprobante se haya leído, que su referencia
    no esté en ninguna orden y que el pago sea a los datos de la tienda."""
    from . import receipt_check, support_bot

    if not support_bot.public_bot_config()['enabled']:
        return None    # la compra en el chat va con el menú de botones
    last = (SupportMessage.query
            .filter(SupportMessage.chat_id == chat.id, SupportMessage.sender == 'client',
                    SupportMessage.attachment.isnot(None))
            .order_by(SupportMessage.id.desc()).first())
    if not last or last.is_deleted or not last.ai_note:
        return None
    if last.created_at and last.created_at < datetime.utcnow() - timedelta(minutes=receipt_check.INVITE_MAX_AGE_MINUTES):
        return None
    note = receipt_check.decode_note(last.ai_note)
    if not receipt_check.can_invite(note) or receipt_check.reference_orders(note['reference']):
        return None
    url = f'{RECEIPT_BUTTON_PREFIX}{last.id}'
    if SupportMessage.query.filter_by(chat_id=chat.id, action_url=url).first():
        return None    # ya se le ofreció con este comprobante
    return url, RECEIPT_BUTTON_LABEL


def generate_reply(chat_id):
    """Un turno del asistente: lee el hilo, consulta lo que haga falta y
    responde. Devuelve True si respondió."""
    chat = SupportChat.query.get(chat_id)
    settings = get_ai_settings()
    if not chat or chat.is_blocked or not chat.ai_active or not (settings['enabled'] and _has_credentials(settings)):
        return False

    _read_pending_attachments(chat, settings['instructions'])
    last_client = (SupportMessage.query.filter_by(chat_id=chat.id, sender='client')
                   .order_by(SupportMessage.id.desc()).first())
    answering_id = last_client.id if last_client else 0      # el mensaje del cliente al que este turno va a contestar
    known = orders_already_known(chat)
    auto_tag_manual_orders(chat, known)       # pedido manual ya pagado → etiqueta del juego, sin depender de que el modelo se acuerde
    messages = [{'role': 'system', 'content': _system_prompt(chat, settings, known)}] + _history(chat)

    action = None
    handoff_reason = None
    searched_without_results = False
    reply_text = ''

    for _round in range(MAX_TOOL_ROUNDS):
        answer = _call_ai(settings, messages)
        calls = answer.get('tool_calls') or []
        # Algunos servicios (Gemini) no numeran las llamadas: se numeran aquí para
        # que cada resultado se pueda emparejar con la suya.
        for index, call in enumerate(calls):
            if not call.get('id'):
                call['id'] = f'call_{_round}_{index}'
        if not calls:
            reply_text = (answer.get('content') or '').strip()
            break
        messages.append({'role': 'assistant', 'content': answer.get('content') or '', 'tool_calls': calls})
        for call in calls:
            name = call.get('function', {}).get('name')
            try:
                args = json.loads(call.get('function', {}).get('arguments') or '{}')
            except ValueError:
                args = {}
            if name == 'buscar_ordenes':
                results = search_orders_for_chat(chat, args.get('numero_orden'), args.get('referencia'),
                                                 args.get('id_jugador'))
                auto_tag_manual_orders(chat, results)
                if not results:
                    searched_without_results = True
                result = {'ordenes': results} if results else {
                    'ordenes': [], 'nota': 'No se encontró ningún pedido con esos datos.'}
            elif name == 'enviar_link_orden':
                number = _digits(args.get('numero_orden'))
                order = Order.query.filter_by(order_number=number).first() if number else None
                if order and (order.id in _verified_ids(chat) or _owned_by_chat_user(chat, order)):
                    action = (f'/order/{order.order_number}', f'Ver mi orden #{order.order_number}')
                    result = {'ok': True, 'nota': 'El botón con el link se adjuntará a tu respuesta.'}
                else:
                    result = {'ok': False, 'nota': 'No puedes enviar ese link: pídele el comprobante del pago (la foto o la referencia).'}
            elif name == 'consultar_catalogo':
                result = catalog_for_ai(args.get('juego'))
            elif name == 'metodos_de_pago':
                result = payment_methods_for_ai()
            elif name == 'etiquetar_chat':
                tag = find_assignable_tag(args.get('etiqueta'))
                if tag:
                    add_tag(chat, tag)
                    result = {'ok': True, 'nota': f'Etiqueta «{tag.name}» puesta al chat.'}
                else:
                    result = {'ok': False, 'nota': 'Esa etiqueta no existe. Usa exactamente una de: '
                                                   + '; '.join(t.name for t in assignable_tags())}
            elif name == 'pasar_a_humano':
                handoff_reason = (args.get('motivo') or 'Solicitado por el asistente')[:150]
                case_tag = find_assignable_tag(args.get('etiqueta'))
                if case_tag:
                    add_tag(chat, case_tag)
                result = {'ok': True}
            else:
                result = {'error': 'herramienta desconocida'}
            messages.append({'role': 'tool', 'tool_call_id': call.get('id'),
                             'content': json.dumps(result, ensure_ascii=False)})
        db.session.commit()

    # Mientras la IA pensaba, otro turno del asistente (dos mensajes seguidos del cliente) o una persona del equipo pudo contestar ese
    # mismo mensaje: la respuesta no se repite (el cliente veía dos veces el mismo texto y el mismo botón).
    db.session.commit()
    db.session.refresh(chat)
    if not chat.ai_active or (answering_id and _answered_since(chat.id, answering_id)):
        return False

    # Un comprobante pagado a nuestros datos que todavía no tiene orden no es un
    # "intento fallido": no hay pedido que encontrar, hay que ofrecerle crearlo.
    invite = None if handoff_reason else _receipt_invitation(chat)
    if searched_without_results and not invite:
        chat.ai_failed = (chat.ai_failed or 0) + 1
    if not handoff_reason and not invite and (chat.ai_failed or 0) >= settings['max_failed']:
        handoff_reason = f'{chat.ai_failed} intentos sin encontrar el pedido'
        reply_text = ('No logro encontrar tu pedido con esos datos. Te paso con un agente de nuestro equipo '
                      'para que lo revise, en breve te responden por aquí.')
    if invite and not action:
        action = invite
        reply_text = reply_text or RECEIPT_INVITE_TEXT

    if not reply_text and handoff_reason:
        reply_text = 'Te paso con un agente de nuestro equipo, en breve te responden por aquí.'
    if reply_text or action:
        add_ai_message(chat, reply_text, *(action or (None, None)))
    db.session.commit()
    if handoff_reason:
        hand_off_to_human(chat, handoff_reason)
    return bool(reply_text or action)


def _answered_since(chat_id, client_message_id):
    """True si después de ese mensaje del cliente ya hay una respuesta (del asistente o de una persona del equipo)."""
    return (db.session.query(SupportMessage.id)
            .filter(SupportMessage.chat_id == chat_id, SupportMessage.sender == 'admin', SupportMessage.id > client_message_id)
            .first()) is not None


def _pending_client_message(chat_id):
    """True si el último mensaje real del hilo es del cliente (falta responder)."""
    last = (SupportMessage.query
            .filter(SupportMessage.chat_id == chat_id, SupportMessage.sender.in_(('client', 'admin')))
            .order_by(SupportMessage.id.desc()).first())
    return bool(last and last.sender == 'client')


def _run(app, chat_id):
    with app.app_context():
        lock_key = f'support_ai:{chat_id}'
        holder = f'ai-{threading.get_ident()}-{datetime.utcnow().timestamp()}'
        if not acquire_lock(lock_key, 120, holder):
            return  # ya hay un turno en curso; al terminar revisa si quedó algo sin responder
        chat = SupportChat.query.get(chat_id)
        try:
            for _ in range(3):
                if not _pending_client_message(chat_id):
                    break
                chat = SupportChat.query.get(chat_id)
                chat.ai_typing_at = datetime.utcnow()
                db.session.commit()
                if not generate_reply(chat_id):
                    break
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning('[SupportAI] chat %s: %s', chat_id, exc)
            chat = SupportChat.query.get(chat_id)
            if chat and chat.ai_active:
                # Si la IA falla (clave vencida, sin saldo, caída), el
                # cliente no se queda sin respuesta: pasa a un humano.
                try:
                    add_ai_message(chat, 'Te paso con un agente de nuestro equipo, en breve te responden por aquí.')
                    db.session.commit()
                    hand_off_to_human(chat, f'El asistente falló: {str(exc)[:80]}')
                except Exception:
                    db.session.rollback()
        finally:
            chat = SupportChat.query.get(chat_id)
            if chat:
                chat.ai_typing_at = None
                db.session.commit()
            release_lock(lock_key, holder)


def should_handle(chat):
    return bool(chat and chat.ai_active and not chat.is_blocked and is_ai_available())


def schedule_reply(chat):
    """Responde en segundo plano: el mensaje del cliente se guarda al
    instante y la respuesta aparece sola en el chat (con 'escribiendo...')."""
    if not should_handle(chat):
        return False
    recent = (SupportMessage.query
              .filter(SupportMessage.chat_id == chat.id, SupportMessage.is_ai.is_(True),
                      SupportMessage.created_at >= datetime.utcnow() - timedelta(hours=1))
              .count())
    if recent >= MAX_AI_REPLIES_PER_CHAT_PER_HOUR:
        return False   # el que llama avisa al equipo, como cuando la IA está apagada
    chat.ai_typing_at = datetime.utcnow()
    db.session.commit()
    app = current_app._get_current_object()
    threading.Thread(target=_run, args=(app, chat.id), daemon=True).start()
    return True


def is_typing(chat):
    return bool(chat.ai_typing_at and datetime.utcnow() - chat.ai_typing_at < timedelta(seconds=TYPING_SECONDS))


def test_connection(api_key=None, model=None):
    """Prueba rápida desde el admin: una pregunta corta a la IA configurada."""
    settings = get_ai_settings()
    provider = settings['provider']
    name = PROVIDER_NAMES[provider]
    if provider == 'gemini':
        key = settings['api_key']
        if not key:
            return False, ('Falta la llave de Gemini en el servidor (GEMINI_API_KEY): es la misma que usa '
                           'la tienda para leer comprobantes.')
    else:
        key = (api_key or '').strip() or settings['api_key']
        if provider == 'deepseek' and not key:
            return False, 'Falta la API key de DeepSeek.'
    if model and provider != 'gemini':
        settings = {**settings, 'model': model}
    try:
        response = _post_chat(settings, key, {
            'max_tokens': 200 if provider == 'gemini' else 10,
            'messages': [{'role': 'user', 'content': 'Responde solo: ok'}],
        })
    except requests.RequestException as exc:
        return False, f'No se pudo conectar con {name} ({settings["base_url"]}): {exc}'
    if response.status_code in (401, 403):
        return False, ('La llave de Gemini no es válida o no tiene permiso.' if provider == 'gemini'
                       else 'La API key no es válida.')
    if response.status_code == 402:
        return False, f'La cuenta de {name} no tiene saldo.'
    if response.status_code == 429:
        return False, f'{name} dice que se pasó el límite de uso por ahora. Intenta en unos minutos.'
    if response.status_code == 404:
        return False, f'{name} respondió 404: revisa la URL y que el modelo "{settings["model"]}" exista.'
    if response.status_code >= 400:
        return False, f'{name} respondió {response.status_code}: {response.text[:150]}'
    return True, f'Conexión correcta con {name} (modelo {settings["model"]}).'
