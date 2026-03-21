"""
Constructores de plantillas HTML de correo para 3S Recargas.
Plantillas: orden_creada, orden_aprobada, orden_completada (con PIN/código), orden_rechazada, nueva_orden_admin.
"""

from app.utils.email import get_setting
from decimal import Decimal, ROUND_HALF_UP


def _format_order_amount(order):
    method_code = (getattr(order, 'payment_method', None) or '').strip().lower()
    try:
        from app.models import Setting, PaymentMethod
        usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
        usd_rate = Decimal(str(usd_rate_setting.value)) if usd_rate_setting and usd_rate_setting.value else Decimal('0')
        method = PaymentMethod.query.filter_by(code=method_code).first() if method_code else None
        currency = (method.account_currency or '').lower() if method and method.account_currency else 'bs'
    except Exception:
        usd_rate = Decimal('0')
        currency = 'bs'

    amt_usd = Decimal(str(float(getattr(order, 'amount', 0) or 0)))
    if currency == 'usd':
        return f"${float(amt_usd):.2f} USD"

    amt_bs = (amt_usd * (usd_rate or Decimal('0'))).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    try:
        amt_bs_int = int(amt_bs)
    except Exception:
        amt_bs_int = int(float(amt_bs or 0))
    return f"Bs {amt_bs_int}"


def _base_style():
    """Constantes CSS inline compartidas."""
    return {
        'bg': '#0f0f0f',
        'card_bg': '#1a1a1a',
        'accent': '#e63946',
        'accent_light': '#ff4d5a',
        'text': '#e0e0e0',
        'muted': '#999999',
        'border': '#2a2a2a',
        'success': '#22c55e',
        'warning': '#f59e0b',
        'danger': '#ef4444',
        'white': '#ffffff',
        'font': "'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
    }


def _brand_name():
    return get_setting('email_brand_name', '3S Recargas')


def _support_links():
    return {
        'email': get_setting('support_email', ''),
        'whatsapp': get_setting('support_whatsapp', ''),
        'site': get_setting('support_site_url', ''),
        'privacy': get_setting('privacy_url', ''),
    }


def _wrap_html(title, body_content):
    """Envuelve el contenido del cuerpo en la estructura HTML completa del correo."""
    s = _base_style()
    brand = _brand_name()
    support = _support_links()

    support_links_html = ''
    if support['whatsapp']:
        support_links_html += f'<a href="{support["whatsapp"]}" style="color:{s["accent_light"]}; text-decoration:none; margin-right:16px;">WhatsApp</a>'
    if support['email']:
        support_links_html += f'<a href="mailto:{support["email"]}" style="color:{s["accent_light"]}; text-decoration:none; margin-right:16px;">Email</a>'
    if support['site']:
        support_links_html += f'<a href="{support["site"]}" style="color:{s["accent_light"]}; text-decoration:none;">Sitio Web</a>'

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
</head>
<body style="margin:0; padding:0; background-color:{s['bg']}; font-family:{s['font']}; color:{s['text']}; -webkit-text-size-adjust:100%;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{s['bg']};">
<tr><td align="center" style="padding:24px 16px;">

<!-- Main container -->
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px; width:100%; background-color:{s['card_bg']}; border-radius:12px; overflow:hidden; border:1px solid {s['border']};">

<!-- Header -->
<tr>
<td style="background: linear-gradient(135deg, {s['accent']} 0%, #b91c2c 100%); padding:28px 32px; text-align:center;">
    <h1 style="margin:0; font-size:24px; font-weight:700; color:{s['white']}; letter-spacing:0.5px;">{brand}</h1>
</td>
</tr>

<!-- Body -->
<tr>
<td style="padding:32px 32px 24px 32px;">
    {body_content}
</td>
</tr>

<!-- Footer -->
<tr>
<td style="padding:20px 32px 28px 32px; border-top:1px solid {s['border']}; text-align:center;">
    {f'<p style="margin:0 0 8px 0; font-size:13px; color:{s["muted"]};">¿Necesitas ayuda?</p><p style="margin:0 0 12px 0; font-size:13px;">{support_links_html}</p>' if support_links_html else ''}
    <p style="margin:0; font-size:12px; color:{s['muted']};">&copy; {brand} &mdash; Todos los derechos reservados</p>
</td>
</tr>

</table>
</td></tr></table>
</body>
</html>"""


def _detail_row(label, value, value_color=None):
    """Fila de detalle individual para tablas de información de orden."""
    s = _base_style()
    vc = value_color or s['white']
    return f"""<tr>
<td style="padding:8px 0; color:{s['muted']}; font-size:14px; border-bottom:1px solid {s['border']}; width:40%;">{label}</td>
<td style="padding:8px 0; color:{vc}; font-size:14px; font-weight:600; border-bottom:1px solid {s['border']}; text-align:right;">{value}</td>
</tr>"""


def _status_badge(label, color):
    """Insignia de estado inline."""
    return f'<span style="display:inline-block; padding:4px 14px; background-color:{color}; color:#fff; border-radius:20px; font-size:13px; font-weight:600; letter-spacing:0.3px;">{label}</span>'


def _game_description(game):
    """Devuelve la descripción del juego limpia o cadena vacía si no existe."""
    if not game:
        return ''
    return (getattr(game, 'description', '') or '').strip()


# ──────────────────────────────────────────────────────────────────────
# ORDEN CREADA — se envía al cliente
# ──────────────────────────────────────────────────────────────────────

def build_order_created_email(order, package, game):
    """Construye HTML + texto para notificación de 'orden creada' al cliente."""
    s = _base_style()
    brand = _brand_name()
    amount_str = _format_order_amount(order)
    game_description = _game_description(game)

    body = f"""
<h2 style="margin:0 0 8px 0; font-size:20px; color:{s['white']};">¡Orden recibida!</h2>
<p style="margin:0 0 20px 0; font-size:15px; color:{s['text']}; line-height:1.6;">
    Hemos recibido tu orden <strong style="color:{s['accent_light']};">#{order.order_number}</strong>. 
    Estamos verificando tu pago. Te notificaremos cuando sea procesada.
</p>

{_status_badge('Pendiente de verificación', s['warning'])}

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:24px;">
{_detail_row('Orden', f'#{order.order_number}')}
{_detail_row('Juego', game.name if game else 'N/A')}
{f'{_detail_row("Descripción", game_description)}' if game_description else ''}
{_detail_row('Paquete', package.name if package else 'N/A')}
{_detail_row('Monto', amount_str, s['accent_light'])}
{_detail_row('Método de pago', (order.payment_method or '').upper())}
{_detail_row('Referencia', order.payment_reference or 'N/A')}
{f'{_detail_row("Jugador", order.player_nickname or order.player_id or "N/A")}' if order.player_id else ''}
</table>

<p style="margin:24px 0 0 0; font-size:13px; color:{s['muted']}; line-height:1.5;">
    El tiempo de procesamiento habitual es de <strong>5 a 30 minutos</strong> en horario de atención.
    Recibirás un correo cuando tu orden sea aprobada.
</p>
"""

    html = _wrap_html(f'Orden #{order.order_number} recibida - {brand}', body)

    text = f"""¡Orden recibida!

Tu orden #{order.order_number} ha sido registrada.
Juego: {game.name if game else 'N/A'}
{f'Descripción: {game_description}' if game_description else ''}
Paquete: {package.name if package else 'N/A'}
Monto: {amount_str}
Método: {(order.payment_method or '').upper()}
Referencia: {order.payment_reference or 'N/A'}

Estamos verificando tu pago. Te notificaremos cuando sea procesada.

— {brand}"""

    subject = f'Orden #{order.order_number} recibida - {brand}'
    return subject, html, text


# ──────────────────────────────────────────────────────────────────────
# ORDEN APROBADA — se envía al cliente (sin PIN)
# ──────────────────────────────────────────────────────────────────────

def build_order_approved_email(order, package, game):
    """Construye HTML + texto para notificación de 'orden aprobada' al cliente (sin PIN)."""
    s = _base_style()
    brand = _brand_name()
    amount_str = _format_order_amount(order)
    game_description = _game_description(game)

    body = f"""
<h2 style="margin:0 0 8px 0; font-size:20px; color:{s['white']};">¡Tu orden fue aprobada! ✅</h2>
<p style="margin:0 0 20px 0; font-size:15px; color:{s['text']}; line-height:1.6;">
    Tu orden <strong style="color:{s['accent_light']};">#{order.order_number}</strong> ha sido verificada y aprobada.
    Tu recarga está siendo procesada.
</p>

{_status_badge('Aprobada', s['success'])}

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:24px;">
{_detail_row('Orden', f'#{order.order_number}')}
{_detail_row('Juego', game.name if game else 'N/A')}
{f'{_detail_row("Descripción", game_description)}' if game_description else ''}
{_detail_row('Paquete', package.name if package else 'N/A')}
{_detail_row('Monto', amount_str, s['accent_light'])}
{f'{_detail_row("Jugador", order.player_nickname or order.player_id or "N/A")}' if order.player_id else ''}
</table>

<p style="margin:24px 0 0 0; font-size:14px; color:{s['text']}; line-height:1.5;">
    ¡Gracias por tu compra! Si tienes alguna duda, no dudes en contactarnos.
</p>
"""

    html = _wrap_html(f'Orden #{order.order_number} aprobada - {brand}', body)

    text = f"""¡Tu orden fue aprobada!

Tu orden #{order.order_number} ha sido verificada y aprobada.
Juego: {game.name if game else 'N/A'}
{f'Descripción: {game_description}' if game_description else ''}
Paquete: {package.name if package else 'N/A'}
Monto: {amount_str}

¡Gracias por tu compra!

— {brand}"""

    subject = f'Orden #{order.order_number} aprobada - {brand}'
    return subject, html, text


# ──────────────────────────────────────────────────────────────────────
# ORDEN COMPLETADA CON PIN/CÓDIGO — se envía al cliente
# ──────────────────────────────────────────────────────────────────────

def build_order_completed_pin_email(order, package, game, pin_code=None):
    """Construye HTML + texto para 'orden completada' con entrega de PIN/código."""
    s = _base_style()
    brand = _brand_name()
    amount_str = _format_order_amount(order)
    code = pin_code or ''
    game_description = _game_description(game)

    code_html = ''
    if code:
        code_html = f"""
<div style="margin:24px 0; padding:20px; background-color:#0f0f0f; border:2px dashed {s['accent']}; border-radius:10px; text-align:center;">
    <p style="margin:0 0 8px 0; font-size:13px; color:{s['muted']}; text-transform:uppercase; letter-spacing:1px;">Tu código</p>
    <p style="margin:0; font-size:28px; font-weight:700; color:{s['accent_light']}; letter-spacing:2px; font-family:monospace;">{code}</p>
    <p style="margin:8px 0 0 0; font-size:12px; color:{s['muted']};">Copia este código y canjéalo en la plataforma correspondiente</p>
</div>
"""

    body = f"""
<h2 style="margin:0 0 8px 0; font-size:20px; color:{s['white']};">¡Orden completada! 🎉</h2>
<p style="margin:0 0 20px 0; font-size:15px; color:{s['text']}; line-height:1.6;">
    Tu orden <strong style="color:{s['accent_light']};">#{order.order_number}</strong> ha sido procesada exitosamente.
    {('Aquí tienes tu código:' if code else 'Tu recarga ha sido aplicada.')}
</p>

{_status_badge('Completada', s['success'])}

{code_html}

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;">
{_detail_row('Orden', f'#{order.order_number}')}
{_detail_row('Juego', game.name if game else 'N/A')}
{f'{_detail_row("Descripción", game_description)}' if game_description else ''}
{_detail_row('Paquete', package.name if package else 'N/A')}
{_detail_row('Monto', amount_str, s['accent_light'])}
{f'{_detail_row("Jugador", order.player_nickname or order.player_id or "N/A")}' if order.player_id else ''}
</table>

<p style="margin:24px 0 0 0; font-size:14px; color:{s['text']}; line-height:1.5;">
    ¡Gracias por tu compra! Esperamos verte pronto de nuevo. 🙌
</p>
"""

    html = _wrap_html(f'Orden #{order.order_number} completada - {brand}', body)

    text = f"""¡Orden completada!

Tu orden #{order.order_number} ha sido procesada exitosamente.
Juego: {game.name if game else 'N/A'}
{f'Descripción: {game_description}' if game_description else ''}
Paquete: {package.name if package else 'N/A'}
Monto: {amount_str}
{f'Código: {code}' if code else ''}

¡Gracias por tu compra!

— {brand}"""

    subject = f'Orden #{order.order_number} completada - {brand}'
    return subject, html, text


# ──────────────────────────────────────────────────────────────────────
# ORDEN RECHAZADA — se envía al cliente
# ──────────────────────────────────────────────────────────────────────

def build_order_rejected_email(order, package, game, reason=None):
    """Construye HTML + texto para notificación de 'orden rechazada' al cliente."""
    s = _base_style()
    brand = _brand_name()
    amount_str = _format_order_amount(order)
    reason_text = reason or order.notes or ''
    game_description = _game_description(game)

    reason_html = ''
    if reason_text:
        reason_html = f"""
<div style="margin:20px 0; padding:16px; background-color:rgba(239,68,68,0.1); border-left:4px solid {s['danger']}; border-radius:6px;">
    <p style="margin:0 0 4px 0; font-size:12px; color:{s['danger']}; text-transform:uppercase; letter-spacing:0.5px; font-weight:600;">Motivo</p>
    <p style="margin:0; font-size:14px; color:{s['text']}; line-height:1.5;">{reason_text}</p>
</div>
"""

    body = f"""
<h2 style="margin:0 0 8px 0; font-size:20px; color:{s['white']};">Orden rechazada</h2>
<p style="margin:0 0 20px 0; font-size:15px; color:{s['text']}; line-height:1.6;">
    Lamentamos informarte que tu orden <strong style="color:{s['accent_light']};">#{order.order_number}</strong> 
    no pudo ser procesada.
</p>

{_status_badge('Rechazada', s['danger'])}

{reason_html}

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;">
{_detail_row('Orden', f'#{order.order_number}')}
{_detail_row('Juego', game.name if game else 'N/A')}
{f'{_detail_row("Descripción", game_description)}' if game_description else ''}
{_detail_row('Paquete', package.name if package else 'N/A')}
{_detail_row('Monto', amount_str, s['accent_light'])}
{_detail_row('Referencia', order.payment_reference or 'N/A')}
</table>

<p style="margin:24px 0 0 0; font-size:14px; color:{s['text']}; line-height:1.5;">
    Si crees que esto es un error, por favor contáctanos con tu número de orden para que podamos revisar tu caso.
</p>
"""

    html = _wrap_html(f'Orden #{order.order_number} rechazada - {brand}', body)

    text = f"""Orden rechazada

Tu orden #{order.order_number} no pudo ser procesada.
{f'Motivo: {reason_text}' if reason_text else ''}
Juego: {game.name if game else 'N/A'}
{f'Descripción: {game_description}' if game_description else ''}
Paquete: {package.name if package else 'N/A'}
Monto: {amount_str}
Referencia: {order.payment_reference or 'N/A'}

Si crees que es un error, contáctanos con tu número de orden.

— {brand}"""

    subject = f'Orden #{order.order_number} rechazada - {brand}'
    return subject, html, text


# ──────────────────────────────────────────────────────────────────────
# ADMIN — notificación de nueva orden
# ──────────────────────────────────────────────────────────────────────

def build_admin_new_order_email(order, package, game):
    """Construye HTML + texto para notificación al admin de nueva orden."""
    s = _base_style()
    brand = _brand_name()
    amount_str = _format_order_amount(order)

    body = f"""
<h2 style="margin:0 0 8px 0; font-size:20px; color:{s['white']};">Nueva orden recibida 🔔</h2>
<p style="margin:0 0 20px 0; font-size:15px; color:{s['text']}; line-height:1.6;">
    Se ha registrado una nueva orden <strong style="color:{s['accent_light']};">#{order.order_number}</strong> 
    que requiere tu atención.
</p>

<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
{_detail_row('Orden', f'#{order.order_number}')}
{_detail_row('Juego', game.name if game else 'N/A')}
{_detail_row('Paquete', package.name if package else 'N/A')}
{_detail_row('Monto', amount_str, s['accent_light'])}
{_detail_row('Método', (order.payment_method or '').upper())}
{_detail_row('Referencia', order.payment_reference or 'N/A')}
{_detail_row('Email cliente', order.email or 'N/A')}
{f'{_detail_row("ID Jugador", order.player_id or "N/A")}' if order.player_id else ''}
{f'{_detail_row("Nickname", order.player_nickname or "N/A")}' if order.player_nickname else ''}
{f'{_detail_row("Código afiliado", order.affiliate_code)}' if order.affiliate_code else ''}
</table>

<div style="margin-top:24px; text-align:center;">
    <p style="margin:0; font-size:14px; color:{s['muted']};">Ingresa al panel de administración para procesar esta orden.</p>
</div>
"""

    html = _wrap_html(f'Nueva orden #{order.order_number} - {brand}', body)

    text = f"""Nueva orden recibida

Orden: #{order.order_number}
Juego: {game.name if game else 'N/A'}
Paquete: {package.name if package else 'N/A'}
Monto: {amount_str}
Método: {(order.payment_method or '').upper()}
Referencia: {order.payment_reference or 'N/A'}
Email cliente: {order.email or 'N/A'}

Ingresa al panel de administración para procesar esta orden.

— {brand}"""

    subject = f'[{brand}] Nueva orden #{order.order_number}'
    return subject, html, text
