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

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user

from ..models import Order, SupportMessage
from ..utils import support as support_service
from ..utils.support import SupportError
from ..utils.notifications import notify_support_chat_opened, notify_support_client_message

support_bp = Blueprint('support_bp', __name__, url_prefix='/soporte')

TOKEN_COOKIE = 'support_token'
TOKEN_HEADER = 'X-Support-Token'
COOKIE_MAX_AGE = 90 * 24 * 3600


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
    chat = support_service.get_chat_by_token(_request_token())
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
    return jsonify({'ok': False, 'error': error.message}), error.status


@support_bp.route('/')
def page():
    """La pantalla del chat.

    Es una pagina propia y no un modal porque el chat de una recarga
    manual no es un aviso de paso: el cliente ya pago y se queda ahi
    hasta que le resuelvan. Una pestana se puede dejar abierta en
    segundo plano, compartir por su URL y recargar sin perder nada;
    un modal se cierra sin querer con un toque fuera.
    """
    chat = support_service.get_chat_by_token(_request_token())

    # `?orden=` llega desde el checkout de un pedido manual. Los datos se
    # buscan aqui y no se aceptan del navegador: el mensaje que se le
    # propone al cliente lo arma el servidor con la orden real.
    order = None
    order_number = (request.args.get('orden') or '').strip()
    if order_number:
        order = Order.query.filter_by(order_number=order_number).first()

    suggested = ''
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

    # Un chat cerrado sigue siendo visible: el cliente lee lo que se le
    # respondio y, si escribe otra vez, se reabre solo.
    messages = _messages_payload(chat) if chat and not chat.is_blocked else []
    if chat and not chat.is_blocked:
        support_service.mark_read_by_client(chat)

    return render_template(
        'support_chat.html',
        chat=chat if (chat and not chat.is_blocked) else None,
        messages=messages,
        order=order,
        suggested_message=suggested,
        idle_minutes=support_service.get_idle_close_minutes(),
    )


@support_bp.route('/iniciar', methods=['POST'])
def start():
    data = request.get_json(silent=True) or {}

    # Campo trampa: está oculto por CSS, una persona nunca lo rellena. Se
    # responde 200 con un chat falso a propósito — devolver un error le
    # diría al bot exactamente qué campo delatarlo.
    if str(data.get('website') or '').strip():
        return jsonify({'ok': True, 'token': '', 'chat': {'code': '#0000', 'status': 'open'},
                        'messages': []})

    chat, is_new = support_service.reopen_or_start(
        token=_request_token(),
        name=data.get('name'),
        context=data.get('context'),
        client_ip=_client_ip(),
        user_agent=request.headers.get('User-Agent', ''),
        user_id=_current_user_id(),
        email=data.get('email'),
    )

    first_message = support_service.clean_body(data.get('message'), allow_empty=True)
    if first_message:
        support_service.add_client_message(chat, first_message)

    if is_new:
        notify_support_chat_opened(chat)

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
    })


@support_bp.route('/mensaje', methods=['POST'])
def send_message():
    chat = _load_chat_or_error()
    data = request.get_json(silent=True) or {}

    message = support_service.add_client_message(chat, data.get('body'))
    notify_support_client_message(chat, message)

    return jsonify({
        'ok': True,
        'chat': support_service.serialize_chat_for_client(chat),
        'message': support_service.serialize_message(message),
    })


@support_bp.route('/adjunto', methods=['POST'])
def send_attachment():
    chat = _load_chat_or_error()

    attachment = support_service.save_attachment(request.files.get('file'))
    if not attachment:
        raise SupportError('No se recibió ninguna imagen.')

    message = support_service.add_client_message(
        chat, request.form.get('body'), attachment=attachment
    )
    notify_support_client_message(chat, message)

    return jsonify({
        'ok': True,
        'message': support_service.serialize_message(message),
    })


@support_bp.route('/cerrar', methods=['POST'])
def close():
    chat = _load_chat_or_error()
    support_service.close_chat(chat)
    return jsonify({'ok': True, 'chat': support_service.serialize_chat_for_client(chat)})
