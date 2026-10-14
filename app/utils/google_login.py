"""Entrar con Google (OAuth 2.0 / OpenID Connect, flujo de "código de autorización").

Sirve como segunda forma de entrar al historial de compras (aparte del juego + ID). Google solo se usa para saber el
CORREO verificado del cliente; con ese correo se crea (o se reabre) su cuenta de cliente y su historial son las órdenes
hechas con ese correo.

Está apagado hasta que el administrador pone el "Client ID" y el "Client secret" de Google (Configuración > Entrar con
Google) o las variables GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET en el .env.

Seguridad:
  * `state` aleatorio guardado en la sesión, de un solo uso y con vencimiento (evita que alguien te meta en SU cuenta).
  * `nonce` dentro del id_token, comparado con el guardado en la sesión.
  * El id_token llega directo del servidor de Google por HTTPS en respuesta a nuestro código + secreto (OpenID Connect
    Core 3.1.3.7: con eso no hace falta verificar la firma); igual se revisan emisor, destinatario (aud), vencimiento y
    que el correo esté VERIFICADO por Google.
  * Solo entra como cliente: nunca como administrador, aunque el correo sea el del admin.
"""
import base64
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import request, session, url_for

from ..models import Setting

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
ISSUERS = {'accounts.google.com', 'https://accounts.google.com'}
SESSION_KEY = 'google_oauth'
STATE_TTL_SECONDS = 10 * 60
REQUEST_TIMEOUT_SECONDS = 10
CLOCK_SKEW_SECONDS = 120


class GoogleLoginError(Exception):
    """Error que se le puede mostrar tal cual al cliente (en español)."""


def _setting(key):
    row = Setting.query.filter_by(key=key).first()
    return (row.value or '').strip() if row else ''


def get_config():
    """Llaves de Google y si el botón debe salir. Las del panel mandan sobre las del .env."""
    client_id = _setting('google_client_id') or (os.environ.get('GOOGLE_CLIENT_ID') or '').strip()
    client_secret = _setting('google_client_secret') or (os.environ.get('GOOGLE_CLIENT_SECRET') or '').strip()
    switch = _setting('google_login_enabled')          # '' = nunca se tocó: activo si hay llaves; '0' = apagado a mano
    return {
        'client_id': client_id,
        'client_secret': client_secret,
        'enabled': bool(client_id and client_secret) and switch != '0',
    }


def redirect_uri():
    """Dirección a la que Google devuelve al cliente (tiene que estar registrada tal cual en la consola de Google).

    Detrás de nginx/Cloudflare Flask ve http, así que el esquema se toma de X-Forwarded-Proto. Si alguien manda un Host
    inventado, Google rechaza la dirección (no está registrada): no hay forma de aprovecharlo."""
    scheme = (request.headers.get('X-Forwarded-Proto') or request.scheme or 'https').split(',')[0].strip().lower()
    if scheme not in ('http', 'https'):
        scheme = 'https'
    return f'{scheme}://{request.host}{url_for("auth_bp.google_login_callback")}'


def begin(next_url=None):
    """Prepara state y nonce y devuelve la dirección de Google a la que hay que mandar al cliente."""
    config = get_config()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    session[SESSION_KEY] = {'state': state, 'nonce': nonce, 'ts': int(time.time()), 'next': next_url or ''}
    params = {
        'client_id': config['client_id'],
        'redirect_uri': redirect_uri(),
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'nonce': nonce,
        'prompt': 'select_account',
    }
    return AUTH_URL + '?' + urlencode(params)


def _decode_jwt_payload(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode('ascii')).decode('utf-8'))
    except Exception:
        raise GoogleLoginError('Google respondió algo que no se pudo leer. Intenta de nuevo.')
    if not isinstance(data, dict):
        raise GoogleLoginError('Google respondió algo que no se pudo leer. Intenta de nuevo.')
    return data


def complete(args):
    """Termina el ingreso: revisa lo que devolvió Google y entrega {'email', 'name', 'sub', 'next'}.

    Lanza GoogleLoginError (con un mensaje para el cliente) si algo no cuadra."""
    pending = session.pop(SESSION_KEY, None)          # de un solo uso: un enlace repetido ya no sirve

    if args.get('error'):
        if args.get('error') == 'access_denied':
            raise GoogleLoginError('Cancelaste el acceso con Google. Puedes intentarlo de nuevo o entrar con tu ID.')
        raise GoogleLoginError('Google no pudo completar el acceso. Intenta de nuevo.')

    if not pending or not isinstance(pending, dict):
        raise GoogleLoginError('La sesión de acceso venció. Vuelve a tocar "Continuar con Google".')
    if int(time.time()) - int(pending.get('ts') or 0) > STATE_TTL_SECONDS:
        raise GoogleLoginError('Pasó demasiado tiempo. Vuelve a tocar "Continuar con Google".')
    state = str(args.get('state') or '')
    if not state or not hmac.compare_digest(state.encode('utf-8'), str(pending.get('state') or '').encode('utf-8')):
        raise GoogleLoginError('No se pudo comprobar el acceso con Google. Intenta de nuevo.')

    code = str(args.get('code') or '').strip()
    if not code:
        raise GoogleLoginError('Google no devolvió el código de acceso. Intenta de nuevo.')

    config = get_config()
    if not config['enabled']:
        raise GoogleLoginError('Entrar con Google no está disponible por ahora.')

    try:
        response = requests.post(
            TOKEN_URL,
            data={
                'code': code,
                'client_id': config['client_id'],
                'client_secret': config['client_secret'],
                'redirect_uri': redirect_uri(),
                'grant_type': 'authorization_code',
            },
            headers={'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise GoogleLoginError('No se pudo conectar con Google. Intenta de nuevo en un momento.')
    if response.status_code != 200:
        raise GoogleLoginError('Google rechazó el acceso. Intenta de nuevo.')
    try:
        id_token = (response.json() or {}).get('id_token') or ''
    except ValueError:
        id_token = ''
    if not id_token:
        raise GoogleLoginError('Google no entregó los datos de tu cuenta. Intenta de nuevo.')

    claims = _decode_jwt_payload(id_token)
    audience = claims.get('aud')
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get('iss') not in ISSUERS or config['client_id'] not in audiences:
        raise GoogleLoginError('Los datos de Google no corresponden a esta tienda. Intenta de nuevo.')
    try:
        expires = int(claims.get('exp') or 0)
    except (TypeError, ValueError):
        expires = 0
    if expires + CLOCK_SKEW_SECONDS < int(time.time()):
        raise GoogleLoginError('El acceso de Google venció. Intenta de nuevo.')
    nonce = str(claims.get('nonce') or '')
    if not nonce or not hmac.compare_digest(nonce.encode('utf-8'), str(pending.get('nonce') or '').encode('utf-8')):
        raise GoogleLoginError('No se pudo comprobar el acceso con Google. Intenta de nuevo.')

    email = str(claims.get('email') or '').strip().lower()
    verified = claims.get('email_verified')
    if isinstance(verified, str):
        verified = verified.strip().lower() == 'true'
    if not email or '@' not in email or verified is not True:
        raise GoogleLoginError('Tu cuenta de Google no tiene un correo verificado. Entra con tu ID.')

    return {
        'email': email[:255],
        'name': str(claims.get('name') or '').strip()[:120],
        'sub': str(claims.get('sub') or ''),
        'next': str(pending.get('next') or ''),
    }
