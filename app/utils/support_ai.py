"""Asistente de IA (DeepSeek) para el chat de soporte.

Atiende las consultas fáciles sobre pedidos: en qué estado está, si falta
dinero, si la referencia todavía no aparece en el banco, si el ID fue
rechazado, o qué hacer si la recarga salió completada y no se ve. Todo lo
que dice sobre una orden sale de la base de datos (herramientas), nunca de
lo que "se imagina" el modelo.

Seguridad:
- El link de una orden solo se envía si el cliente demostró que es suya:
  dio el número de orden o la referencia del pago (o el comprobante), o el
  chat ya venía con esa orden. Con solo el ID de jugador (que en los juegos
  es público) se le dice el estado, pero no se le manda el link, porque la
  página de la orden muestra el correo del cliente.
- Nunca se le pasan al modelo correos, teléfonos ni notas internas.

Cuando no puede resolver (lo pide el cliente, ID inválido, orden rechazada,
o varios intentos sin encontrar su pedido) pasa el chat a un humano: se
apaga la IA en ese chat y se le pone la etiqueta configurada en el admin.
"""

import json
import re
import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import current_app

from ..models import Order, Setting, SupportChat, SupportMessage, SupportTag, db
from .locks import acquire_lock, release_lock
from .timezone import format_ve

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
DEFAULT_MODEL = 'deepseek-chat'
DEFAULT_MAX_FAILED = 3
HISTORY_MESSAGES = 16
MAX_TOOL_ROUNDS = 4
REQUEST_TIMEOUT = 45
TYPING_SECONDS = 90
REFERENCE_MIN_DIGITS = 6

SETTING_KEYS = {
    'enabled': 'support_ai_enabled',
    'api_key': 'support_ai_api_key',
    'model': 'support_ai_model',
    'handoff_tag_id': 'support_ai_handoff_tag_id',
    'max_failed': 'support_ai_max_failed',
    'instructions': 'support_ai_instructions',
    # Por defecto la API de DeepSeek. Se puede apuntar a cualquier servicio
    # compatible (p. ej. Ollama en una PC propia: http://IP:11434/v1/chat/completions).
    'base_url': 'support_ai_base_url',
}


# ─── Configuración (admin → Configuración) ──────────────────────────────────

def _setting(key, default=''):
    row = Setting.query.filter_by(key=SETTING_KEYS[key]).first()
    return (row.value or '').strip() if row and row.value is not None else default


def get_ai_settings():
    try:
        max_failed = int(_setting('max_failed') or DEFAULT_MAX_FAILED)
    except ValueError:
        max_failed = DEFAULT_MAX_FAILED
    tag_id = _setting('handoff_tag_id')
    return {
        'enabled': _setting('enabled') == '1',
        'api_key': _setting('api_key'),
        'model': _setting('model') or DEFAULT_MODEL,
        'handoff_tag_id': int(tag_id) if tag_id.isdigit() else None,
        'max_failed': max(1, min(max_failed, 10)),
        'instructions': _setting('instructions'),
        'base_url': _setting('base_url') or DEEPSEEK_URL,
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
    return settings['base_url'].startswith('https://api.deepseek.com')


def save_ai_settings(enabled, api_key, model, handoff_tag_id, max_failed, instructions, base_url=None):
    values = {
        'enabled': '1' if enabled else '',
        'model': (model or DEFAULT_MODEL).strip()[:60],
        'handoff_tag_id': str(handoff_tag_id or ''),
        'max_failed': str(max_failed or DEFAULT_MAX_FAILED),
        'instructions': (instructions or '').strip()[:3000],
        'base_url': normalize_base_url(base_url),
    }
    # La clave solo se cambia si escribieron una nueva: el campo se muestra
    # vacío para no dejarla a la vista en el panel.
    if api_key and api_key.strip():
        values['api_key'] = api_key.strip()[:200]
    for key, value in values.items():
        row = Setting.query.filter_by(key=SETTING_KEYS[key]).first()
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=SETTING_KEYS[key], value=value))
    db.session.commit()


def _has_credentials(s):
    # DeepSeek exige clave; un servidor propio (Ollama) normalmente no.
    return bool(s['api_key']) or not uses_deepseek(s)


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
    if len(ref) >= REFERENCE_MIN_DIGITS:
        from sqlalchemy import or_
        rows = (Order.query
                .filter(Order.created_at >= since,
                        or_(Order.payment_reference.like(f'%{ref}'),
                            Order.ai_extracted_reference.like(f'%{ref}'),
                            Order.remainder_reference.like(f'%{ref}')))
                .order_by(Order.created_at.desc()).limit(3).all())
        for order in rows:
            if order not in found:
                found.append(order)
            proven.append(order)

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
        if not info['puedo_enviar_link']:
            # Encontrada solo por ID de jugador: no se sabe si es de quien escribe.
            info.pop('referencia_registrada_ultimos_digitos', None)
        results.append(info)
    return results


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


# ─── Conversación con DeepSeek ──────────────────────────────────────────────

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
            'name': 'pasar_a_humano',
            'description': 'Pasa el chat a un agente humano del equipo. Después de llamarla, despídete avisando que un agente lo atenderá.',
            'parameters': {
                'type': 'object',
                'properties': {'motivo': {'type': 'string', 'description': 'Motivo corto para el equipo'}},
                'required': ['motivo'],
            },
        },
    },
]


def _system_prompt(chat, settings, known_orders):
    schedule = _support_schedule()
    parts = [
        'Eres el asistente virtual de soporte de 3S Recargas, una tienda venezolana de recargas de juegos '
        '(diamantes de Free Fire, Blood Strike y otros). Hablas con un cliente por el chat de la web.',
        'Reglas:',
        '- Responde en español, breve (1 a 4 frases), amable y claro. Sin markdown ni asteriscos.',
        '- NUNCA inventes el estado de un pedido: consulta siempre con la herramienta buscar_ordenes y usa '
        'solo lo que devuelve (que_pasa y que_debe_hacer).',
        '- Para consultar un pedido pide cualquiera de estos: el número de orden, la referencia del pago '
        '(al menos los últimos 6 dígitos) o una foto del comprobante. También sirve el ID de jugador, pero '
        'con solo el ID no puedes enviar el link de la orden (por seguridad): en ese caso dile el estado y '
        'pídele la referencia o el número de orden para mandarle el link.',
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
        '- Si te pregunta algo general de la tienda que no sabes, dilo y ofrece pasarlo con un agente.',
        f'El cliente se llama {chat.client_name}.',
    ]
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
                text += f' {m.ai_note}'
        if m.sender == 'client':
            messages.append({'role': 'user', 'content': text.strip() or '(mensaje vacío)'})
        else:
            messages.append({'role': 'assistant', 'content': text.strip() or '(link enviado)'})
    return messages


def _call_deepseek(settings, messages):
    response = requests.post(
        settings['base_url'],
        headers=_headers(settings['api_key']),
        json={
            'model': settings['model'],
            'messages': messages,
            'tools': TOOLS,
            'temperature': 0.3,
            'max_tokens': 500,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f'La IA respondió {response.status_code}: {response.text[:200]}')
    return response.json()['choices'][0]['message']


def _read_pending_attachments(chat):
    """Si el cliente mandó una foto (comprobante), se lee la referencia con
    el lector de comprobantes que ya usa la tienda y se guarda como nota
    interna del mensaje para que el asistente la use."""
    import os
    from .reference_extraction import extract_reference_from_image_path
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
            result = extract_reference_from_image_path(
                os.path.join(current_app.config['UPLOAD_FOLDER'], m.attachment))
        except Exception:
            result = {}
        ref = (result or {}).get('reference') or ''
        m.ai_note = (f'(El sistema leyó en el comprobante la referencia: {ref})' if ref
                     else '(No se pudo leer una referencia en la imagen)')
    db.session.commit()


def generate_reply(chat_id):
    """Un turno del asistente: lee el hilo, consulta lo que haga falta y
    responde. Devuelve True si respondió."""
    chat = SupportChat.query.get(chat_id)
    settings = get_ai_settings()
    if not chat or chat.is_blocked or not chat.ai_active or not (settings['enabled'] and _has_credentials(settings)):
        return False

    _read_pending_attachments(chat)
    known = orders_already_known(chat)
    messages = [{'role': 'system', 'content': _system_prompt(chat, settings, known)}] + _history(chat)

    action = None
    handoff_reason = None
    searched_without_results = False
    reply_text = ''

    for _round in range(MAX_TOOL_ROUNDS):
        answer = _call_deepseek(settings, messages)
        calls = answer.get('tool_calls') or []
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
                    result = {'ok': False, 'nota': 'No puedes enviar ese link: pide la referencia del pago o el número de orden.'}
            elif name == 'pasar_a_humano':
                handoff_reason = (args.get('motivo') or 'Solicitado por el asistente')[:150]
                result = {'ok': True}
            else:
                result = {'error': 'herramienta desconocida'}
            messages.append({'role': 'tool', 'tool_call_id': call.get('id'),
                             'content': json.dumps(result, ensure_ascii=False)})
        db.session.commit()

    if searched_without_results:
        chat.ai_failed = (chat.ai_failed or 0) + 1
    if not handoff_reason and (chat.ai_failed or 0) >= settings['max_failed']:
        handoff_reason = f'{chat.ai_failed} intentos sin encontrar el pedido'
        reply_text = ('No logro encontrar tu pedido con esos datos. Te paso con un agente de nuestro equipo '
                      'para que lo revise, en breve te responden por aquí.')

    if not reply_text and handoff_reason:
        reply_text = 'Te paso con un agente de nuestro equipo, en breve te responden por aquí.'
    if reply_text or action:
        add_ai_message(chat, reply_text, *(action or (None, None)))
    db.session.commit()
    if handoff_reason:
        hand_off_to_human(chat, handoff_reason)
    return bool(reply_text or action)


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
    key = (api_key or '').strip() or settings['api_key']
    name = 'DeepSeek' if uses_deepseek(settings) else 'la IA'
    if uses_deepseek(settings) and not key:
        return False, 'Falta la API key de DeepSeek.'
    try:
        response = requests.post(
            settings['base_url'],
            headers=_headers(key),
            json={'model': model or settings['model'], 'max_tokens': 10,
                  'messages': [{'role': 'user', 'content': 'Responde solo: ok'}]},
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, f'No se pudo conectar con {name} ({settings["base_url"]}): {exc}'
    if response.status_code == 401:
        return False, 'La API key no es válida.'
    if response.status_code == 402:
        return False, 'La cuenta de DeepSeek no tiene saldo.'
    if response.status_code == 404:
        return False, f'{name} respondió 404: revisa la URL y que el modelo "{settings["model"]}" exista.'
    if response.status_code >= 400:
        return False, f'{name} respondió {response.status_code}: {response.text[:150]}'
    return True, f'Conexión correcta con {name} (modelo {settings["model"]}).'
