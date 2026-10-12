"""API del chat de soporte para el cliente.

Todo es JSON y va bajo `/soporte`. El hilo se identifica con un token
opaco de 32 hex que se emite al abrir el chat: es la única credencial de
un invitado, así que viaja en cookie httponly (el camino normal) y también
se devuelve en el cuerpo para que el navegador lo guarde en localStorage.

Ese respaldo no es paranoia: Safari borra cookies de sitios que el usuario
no visita seguido, y perder la cookie significaría perder el hilo de un
cliente que está esperando respuesta. Cuando la cookie no llega, el JS
manda el token por cabecera `X-Support-Token`.
"""

from flask import Blueprint, current_app, jsonify, make_response, render_template, request
from flask_login import current_user

from ..models import Order, SupportMessage
from ..utils import support as support_service
from ..utils import support_ai
from ..utils import support_bot
from ..utils.locks import check_rate_limit
from ..utils.reference_extraction import extract_reference_from_image_bytes
from ..utils.support import SupportError
from ..utils.notifications import notify_support_chat_opened, notify_support_client_message

support_bp = Blueprint('support_bp', __name__, url_prefix='/soporte')

TOKEN_COOKIE = 'support_token'
TOKEN_HEADER = 'X-Support-Token'
COOKIE_MAX_AGE = 90 * 24 * 3600

# Consultas del menú de botones, por IP. El estado de un pedido se limita mucho
# más: se busca por número de orden o referencia y no debe poder adivinarse.
BOT_CATALOG_PER_MINUTE = 60
BOT_STATUS_PER_MINUTE = 8
BOT_STATUS_PER_HOUR = 40
# Leer un comprobante llama a un servicio de IA que cobra por imagen: tope bajo.
BOT_RECEIPT_PER_MINUTE = 5
BOT_RECEIPT_PER_HOUR = 20
BOT_RECEIPT_MAX_BYTES = 8 * 1024 * 1024
# Cada foto del chat la lee la IA (cuesta): un cliente normal manda 1 o 2.
CHAT_ATTACHMENTS_PER_HOUR = 20
RECEIPT_MIME = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}


def _request_token():
    return (request.headers.get(TOKEN_HEADER) or request.cookies.get(TOKEN_COOKIE) or '').strip()


def _client_ip():
    """IP real detrás de nginx. `remote_addr` a secas sería siempre
    127.0.0.1 y el límite por IP no filtraría absolutamente nada."""
    forwarded = (request.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
    return forwarded or request.remote_addr or ''


def _current_user_id():
    if current_user.is_authenticated and current_user.__class__.__name__ == 'User':
        return current_user.id
    return None


def _attach_token(response, token):
    response.set_cookie(
        TOKEN_COOKIE, token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite='Lax',
        secure=request.is_secure,
    )
    return response


def _load_chat_or_error():
    chat, expired = support_service.get_client_chat(_request_token())
    if expired:
        # Pasaron 24 h desde el último mensaje: ese chat ya no existe para este navegador.
        raise SupportError('Esta conversación ya no está disponible. Escribe de nuevo para empezar otra.', status=410)
    if not chat:
        raise SupportError('No encontramos tu chat. Vuelve a abrirlo.', status=404)
    if chat.is_blocked:
        raise SupportError('Este chat fue cerrado por el equipo de soporte.', status=403)
    return chat


def _messages_payload(chat, after_id=0):
    query = SupportMessage.query.filter(SupportMessage.chat_id == chat.id)
    if after_id:
        query = query.filter(SupportMessage.id > after_id)
    messages = query.order_by(SupportMessage.id).all()
    return [support_service.serialize_message(m) for m in messages]


@support_bp.errorhandler(SupportError)
def _handle_support_error(error):
    response = jsonify({'ok': False, 'error': error.message, 'expired': error.status == 410})
    response.status_code = error.status
    if error.status == 410:
        # Se borra también la cookie: el navegador se queda sin rastro del chat.
        response.delete_cookie(TOKEN_COOKIE, path='/', samesite='Lax', secure=request.is_secure, httponly=True)
    return response


@support_bp.route('/')
def page():
    """La pantalla del chat.

    Es una pagina propia y no un modal porque el chat de una recarga
    manual no es un aviso de paso: el cliente ya pago y se queda ahi
    hasta que le resuelvan. Una pestana se puede dejar abierta en
    segundo plano, compartir por su URL y recargar sin perder nada;
    un modal se cierra sin querer con un toque fuera.
    """
    chat, chat_expired = support_service.get_client_chat(_request_token())

    # `?orden=` llega desde el checkout de un pedido manual. Los datos se
    # buscan aqui y no se aceptan del navegador: el mensaje que se le
    # propone al cliente lo arma el servidor con la orden real.
    order = None
    order_number = (request.args.get('orden') or '').strip()
    if order_number:
        order = Order.query.filter_by(order_number=order_number).first()

    suggested = ''
    auto_start = False
    auto_start_email = ''
    auto_send = False
    if order:
        lines = [
            'Hola, ya completé mi pedido manual y necesito finalizar la recarga.',
            f'Número de pedido: #{order.order_number}',
        ]
        if order.game:
            lines.append(f'Juego: {order.game.name}')
        if order.package:
            lines.append(f'Paquete adquirido: {order.package.name}')
        if order.player_id and order.game:
            lines.append(f'{order.game.player_id_label}: {order.player_id}')
        if order.zone_id and order.game:
            lines.append(f'{order.game.zone_id_label}: {order.zone_id}')
        suggested = '\n'.join(lines)
    elif (request.args.get('motivo') or '').strip() == 'clave_mini':
        # Igual filosofía que `?orden=`: el texto lo arma el servidor, el
        # navegador solo manda el correo que el mini ya había escrito en el
        # formulario de login.
        correo_mini = (request.args.get('correo') or '').strip()
        lines = ['Soy mini, este es mi correo:']
        if correo_mini:
            lines.append(correo_mini)
        lines.append('Olvidé mi contraseña.')
        suggested = '\n'.join(lines)
        # El botón "Olvidé mi contraseña" ya pidió el correo antes de
        # llegar aquí, así que el chat se manda solo, sin que el mini tenga
        # que volver a escribir nada. Si el navegador no tiene chat todavía
        # se abre uno nuevo; si ya tenía uno (un chat es por navegador, no
        # por motivo), el mensaje se agrega ahí en vez de perderse.
        if not chat:
            auto_start = True
            auto_start_email = correo_mini
        elif not chat.is_blocked:
            auto_send = True

    # Un chat cerrado sigue siendo visible: el cliente lee lo que se le
    # respondio y, si escribe otra vez, se reabre solo.
    messages = _messages_payload(chat) if chat and not chat.is_blocked else []
    if chat and not chat.is_blocked:
        support_service.mark_read_by_client(chat)

    response = make_response(render_template(
        'support_chat.html',
        reset_token=chat_expired,
        chat=chat if (chat and not chat.is_blocked) else None,
        messages=messages,
        order=order,
        suggested_message=suggested,
        auto_start=auto_start,
        auto_start_email=auto_start_email,
        auto_send=auto_send,
        idle_minutes=support_service.get_idle_close_minutes(),
        bot=support_bot.public_bot_config(),
        visible_hours=support_service.CLIENT_VISIBLE_HOURS,
    ))
    if chat_expired:
        # Pasaron 24 h desde el último mensaje: el navegador olvida el chat (cookie y, con
        # `reset_token`, también el respaldo del almacenamiento local).
        response.delete_cookie(TOKEN_COOKIE, path='/', samesite='Lax', secure=request.is_secure, httponly=True)
    return response


@support_bp.route('/iniciar', methods=['POST'])
def start():
    """Crea el chat con el primer mensaje (o el primer adjunto).

    La pantalla es un chat normal, sin formulario: el hilo no existe hasta
    que el cliente escribe, así que abrir "Soporte" sin mandar nada no deja
    chats vacíos en la bandeja. `name` y `email` son opcionales; solo los
    manda el flujo de "Olvidé mi contraseña" de los minis.
    """
    data = request.get_json(silent=True) or {}

    # Se valida antes de crear nada: sin mensaje ni adjunto no hay chat.
    first_message = support_service.clean_body(
        data.get('message'), allow_empty=bool(data.get('has_attachment'))
    )

    chat, is_new = support_service.reopen_or_start(
        token=_request_token(),
        name=data.get('name'),
        context=data.get('context'),
        client_ip=_client_ip(),
        user_agent=request.headers.get('User-Agent', ''),
        user_id=_current_user_id(),
        email=data.get('email'),
    )

    if first_message:
        support_service.add_client_message(chat, first_message)

    # Con el asistente activo, él atiende primero; el equipo recibe el aviso
    # solo si el asistente pasa el chat a una persona.
    ai_handles = support_ai.should_handle(chat)
    if is_new and not ai_handles:
        notify_support_chat_opened(chat)
    if first_message and ai_handles and not data.get('has_attachment'):
        support_ai.schedule_reply(chat)

    support_service.mark_read_by_client(chat)

    response = jsonify({
        'ok': True,
        'token': chat.public_token,
        'new': is_new,
        'chat': support_service.serialize_chat_for_client(chat),
        'messages': _messages_payload(chat),
    })
    return _attach_token(response, chat.public_token)


@support_bp.route('/hilo', methods=['GET'])
def thread():
    """Sondeo incremental. Con `after_id` la respuesta normal es una lista
    vacía de unos pocos bytes, que es lo que permite preguntar cada 5
    segundos sin que se note."""
    support_service.close_idle_chats()
    chat = _load_chat_or_error()

    try:
        after_id = int(request.args.get('after_id') or 0)
    except (TypeError, ValueError):
        after_id = 0

    messages = _messages_payload(chat, after_id)
    if messages:
        support_service.mark_read_by_client(chat)

    return jsonify({
        'ok': True,
        'chat': support_service.serialize_chat_for_client(chat),
        'messages': messages,
        'deleted_ids': support_service.recently_deleted_ids(chat),
    })


@support_bp.route('/mensaje', methods=['POST'])
def send_message():
    chat = _load_chat_or_error()
    data = request.get_json(silent=True) or {}

    message = support_service.add_client_message(chat, data.get('body'))
    if not support_ai.schedule_reply(chat):
        notify_support_client_message(chat, message)

    return jsonify({
        'ok': True,
        'chat': support_service.serialize_chat_for_client(chat),
        'message': support_service.serialize_message(message),
    })


@support_bp.route('/adjunto', methods=['POST'])
def send_attachment():
    chat = _load_chat_or_error()
    if not check_rate_limit(f'support_attach:{_client_ip()}', CHAT_ATTACHMENTS_PER_HOUR, 3600):
        raise SupportError('Enviaste muchas imágenes seguidas. Espera un rato o cuéntanos con texto.', status=429)

    attachment = support_service.save_attachment(request.files.get('file'))
    if not attachment:
        raise SupportError('No se recibió ninguna imagen.')

    message = support_service.add_client_message(
        chat, request.form.get('body'), attachment=attachment
    )
    # El asistente lee la referencia del comprobante y busca la orden.
    if not support_ai.schedule_reply(chat):
        notify_support_client_message(chat, message)

    return jsonify({
        'ok': True,
        'chat': support_service.serialize_chat_for_client(chat),
        'message': support_service.serialize_message(message),
    })


@support_bp.route('/cerrar', methods=['POST'])
def close():
    chat = _load_chat_or_error()
    support_service.close_chat(chat)
    return jsonify({'ok': True, 'chat': support_service.serialize_chat_for_client(chat)})


# ─── Menú de botones (precios, comprar, estado de la recarga) ───────────────
#
# Consultas sueltas, sin chat: no crean ningún hilo ni necesitan cookie.

def _bot_json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response


def _bot_guard():
    """Devuelve una respuesta de error si el menú está apagado o la IP se pasó."""
    if not support_bot.public_bot_config()['enabled']:
        return _bot_json({'ok': False, 'error': 'El menú no está disponible.'}, 404)
    if not check_rate_limit(f'support_bot:{_client_ip()}', BOT_CATALOG_PER_MINUTE, 60):
        return _bot_json({'ok': False, 'error': 'Demasiadas consultas seguidas. Espera un momento.'}, 429)
    return None


@support_bp.route('/bot/juegos')
def bot_games():
    blocked = _bot_guard()
    if blocked:
        return blocked
    return _bot_json({'ok': True, 'games': support_bot.list_games()})


@support_bp.route('/bot/paquetes/<int:game_id>')
def bot_packages(game_id):
    blocked = _bot_guard()
    if blocked:
        return blocked
    data = support_bot.game_packages(game_id)
    if data is None:
        return _bot_json({'ok': False, 'error': 'No encontramos ese juego.'}, 404)
    if data.get('error'):
        return _bot_json({'ok': False, 'error': data['error']}, 503)
    return _bot_json({'ok': True, **data})


@support_bp.route('/bot/pago/metodos')
def bot_payment_methods():
    blocked = _bot_guard()
    if blocked:
        return blocked
    return _bot_json({'ok': True, 'methods': support_bot.payment_method_cards()})


@support_bp.route('/bot/estado', methods=['POST'])
def bot_status():
    blocked = _bot_guard()
    if blocked:
        return blocked
    ip = _client_ip()
    if (not check_rate_limit(f'support_bot_status:{ip}', BOT_STATUS_PER_MINUTE, 60)
            or not check_rate_limit(f'support_bot_status_h:{ip}', BOT_STATUS_PER_HOUR, 3600)):
        return _bot_json({'ok': False, 'error': 'Demasiadas consultas seguidas. Espera un momento.'}, 429)

    data = request.get_json(silent=True)
    query = data.get('query') if isinstance(data, dict) else ''
    digits = ''.join(ch for ch in str(query or '') if ch.isdigit())
    if len(digits) < support_bot.REFERENCE_MIN_DIGITS:
        return _bot_json({'ok': False, 'error': 'Escribe tu número de orden (12 dígitos) o la referencia de tu pago.'}, 400)
    orders = support_bot.find_orders(digits)
    return _bot_json({'ok': True, 'orders': [support_bot.order_card(o) for o in orders]})


@support_bp.route('/bot/comprobante', methods=['POST'])
def bot_receipt():
    """Estado de un pedido a partir de la FOTO del comprobante: la IA de lectura
    de comprobantes de la tienda saca la referencia y con ella se busca el
    pedido. La imagen se lee en memoria y no se guarda."""
    blocked = _bot_guard()
    if blocked:
        return blocked
    ip = _client_ip()
    if (not check_rate_limit(f'support_bot_ocr:{ip}', BOT_RECEIPT_PER_MINUTE, 60)
            or not check_rate_limit(f'support_bot_ocr_h:{ip}', BOT_RECEIPT_PER_HOUR, 3600)):
        return _bot_json({'ok': False, 'error': 'Demasiadas fotos seguidas. Espera un momento o escribe tu referencia.'}, 429)

    upload = request.files.get('file')
    if not upload or not upload.filename:
        return _bot_json({'ok': False, 'error': 'Adjunta la foto de tu comprobante.'}, 400)
    extension = upload.filename.rsplit('.', 1)[-1].lower() if '.' in upload.filename else ''
    if extension not in RECEIPT_MIME:
        return _bot_json({'ok': False, 'error': 'Solo se aceptan fotos (JPG, PNG, WEBP o GIF).'}, 400)
    image = upload.read(BOT_RECEIPT_MAX_BYTES + 1)
    if len(image) > BOT_RECEIPT_MAX_BYTES:
        return _bot_json({'ok': False, 'error': 'La foto pesa demasiado (máximo 8 MB).'}, 400)

    result = extract_reference_from_image_bytes(image, mime_type=RECEIPT_MIME[extension], filename=upload.filename)
    if not result.get('ok'):
        # El motivo real (llave sin configurar, servicio caído...) es del admin, no del cliente.
        current_app.logger.warning('[SupportBot] no se pudo leer un comprobante: %s', result.get('message'))
        return _bot_json({'ok': False, 'error': 'No pude leer la foto ahora. Escribe tu número de orden o la referencia del pago.'}, 503)

    reference = result.get('reference') or ''
    orders = support_bot.find_orders(reference) if reference else []
    return _bot_json({'ok': True, 'reference': reference, 'orders': [support_bot.order_card(o) for o in orders]})
