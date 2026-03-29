import json
from datetime import datetime

import requests
from flask import current_app

from ..models import Affiliate, AffiliateCommission, Pin, RevendedoresItemMapping, db
from .notifications import notify_order_approved, notify_order_completed


def process_affiliate_commission(order):
    if not order.affiliate_id:
        return
    affiliate = Affiliate.query.get(order.affiliate_id)
    if not affiliate or not affiliate.is_active:
        return
    commission_amount = round(float(order.amount) * float(affiliate.commission_rate) / 100, 2)
    commission = AffiliateCommission(
        affiliate_id=affiliate.id,
        order_id=order.id,
        amount=commission_amount,
    )
    affiliate.balance = float(affiliate.balance) + commission_amount
    affiliate.total_earned = float(affiliate.total_earned) + commission_amount
    db.session.add(commission)


def get_order_auto_mapping(order_obj):
    try:
        if not order_obj or not order_obj.package_id:
            return None
        return RevendedoresItemMapping.query.filter_by(
            store_package_id=int(order_obj.package_id),
            active=True,
            auto_enabled=True,
        ).first()
    except Exception:
        return None


def get_revendedores_env():
    base_url = current_app.config.get('REVENDEDORES_BASE_URL', '').rstrip('/')
    api_key = current_app.config.get('REVENDEDORES_API_KEY', '')
    catalog_path = '/api/v1/products'
    recharge_path = '/api/v1/recharge'
    return base_url, api_key, catalog_path, recharge_path


def approve_order(order):
    if order.status != 'pending':
        return {
            'ok': False,
            'changed': False,
            'message': 'Solo se pueden aprobar órdenes pendientes.',
            'category': 'warning',
        }

    rev_mapping = get_order_auto_mapping(order)
    if rev_mapping:
        try:
            base_url, api_key, _, recharge_path = get_revendedores_env()
            catalog_item = rev_mapping.catalog_item
            if base_url and api_key and catalog_item:
                auto_resp = {}
                try:
                    auto_resp = json.loads(order.automation_response or '{}')
                except Exception:
                    auto_resp = {}

                prev_attempt = 0
                try:
                    if (auto_resp.get('source') or '') == 'revendedores_api':
                        prev_attempt = int(auto_resp.get('rev_attempt') or 0)
                except Exception:
                    prev_attempt = 0

                rev_attempt = prev_attempt + 1
                ext_order_id = f'{order.order_number}-{rev_attempt}'

                rev_payload = {
                    'product_id': catalog_item.remote_product_id,
                    'package_id': catalog_item.remote_package_id,
                    'player_id': str(order.player_id or '').strip(),
                    'external_order_id': ext_order_id,
                }
                if order.zone_id:
                    rev_payload['player_id2'] = str(order.zone_id).strip()

                resp = requests.post(
                    f'{base_url}{recharge_path}',
                    json=rev_payload,
                    headers={'X-API-Key': api_key, 'Content-Type': 'application/json'},
                    timeout=120,
                )
                rev_data = resp.json() if resp.ok else {}
                rev_ok = rev_data.get('ok', False)

                if rev_ok:
                    player_name = rev_data.get('player_name', '')
                    ref_no = rev_data.get('reference_no', '')
                    order.status = 'completed'
                    order.automation_response = json.dumps({
                        'source': 'revendedores_api',
                        'success': True,
                        'rev_attempt': rev_attempt,
                        'external_order_id': ext_order_id,
                        'player_name': player_name,
                        'reference_no': ref_no,
                        'order_id': rev_data.get('order_id'),
                    })
                    order.notes = (order.notes or '') + f'\n[Revendedores API] Ref: {ref_no}, Player: {player_name}'
                    order.updated_at = datetime.utcnow()
                    process_affiliate_commission(order)
                    db.session.commit()
                    try:
                        notify_order_completed(order, order.package, order.game)
                    except Exception:
                        pass
                    extra = f' (Jugador: {player_name})' if player_name else ''
                    return {
                        'ok': True,
                        'changed': True,
                        'message': f'Orden #{order.order_number} completada vía Revendedores API.{extra}',
                        'category': 'success',
                    }

                rev_error = rev_data.get('error', resp.text[:200] if not resp.ok else 'Error desconocido')
                order.automation_response = json.dumps({
                    'source': 'revendedores_api',
                    'pending_verification': True,
                    'rev_attempt': rev_attempt,
                    'external_order_id': ext_order_id,
                    'error': rev_error,
                })
                db.session.commit()
                return {
                    'ok': False,
                    'changed': False,
                    'message': f'Revendedores reportó error: {rev_error}. Verificando si la recarga se procesó...',
                    'category': 'warning',
                }
        except Exception as exc:
            order.automation_response = json.dumps({
                'source': 'revendedores_api',
                'pending_verification': True,
                'external_order_id': f'{order.order_number}-1',
                'error': str(exc),
            })
            try:
                db.session.commit()
            except Exception:
                pass
            return {
                'ok': False,
                'changed': False,
                'message': f'Error contactando Revendedores API: {exc}. Verificando si se procesó...',
                'category': 'warning',
            }

    package = order.package
    category_slug = (order.game.category.slug if order.game and order.game.category else '').lower()
    needs_pin_delivery = package.is_automated or category_slug == 'tarjetas'

    pin = None
    if needs_pin_delivery:
        pin = (
            Pin.query
            .filter_by(package_id=package.id, is_used=False)
            .order_by(Pin.created_at.asc())
            .first()
        )
        if not pin:
            return {
                'ok': False,
                'changed': False,
                'message': 'Sin stock de códigos para este paquete. Carga PINs primero.',
                'category': 'danger',
            }

    if package.is_automated:
        vps_url = current_app.config.get('VPS_REDEEM_URL')
        vps_timeout = current_app.config.get('VPS_TIMEOUT', 120)

        payload = {
            'pin_key': str(pin.code).strip(),
            'player_id': str(order.player_id).strip(),
            'full_name': current_app.config.get('VPS_FULL_NAME', 'Usuario Recarga'),
            'birth_date': current_app.config.get('VPS_BIRTH_DATE', '01/01/1995'),
            'country': current_app.config.get('VPS_COUNTRY', 'Venezuela'),
            'request_id': order.order_number,
        }

        try:
            resp = requests.post(
                vps_url,
                json=payload,
                timeout=vps_timeout,
                headers={'Content-Type': 'application/json'},
            )

            try:
                data = resp.json()
            except Exception:
                data = {}

            exito = data.get('success') or data.get('exito') or data.get('status') == 'ok'
            mensaje = data.get('message') or data.get('mensaje') or data.get('error') or ''
            player_name = data.get('player_name') or data.get('nombre_jugador') or ''

            if not exito and resp.status_code != 200:
                exito = False
            elif resp.status_code == 200 and not data:
                exito = True
                mensaje = 'Recarga procesada (VPS)'

            if exito:
                pin.is_used = True
                pin.used_at = datetime.utcnow()
                pin.order_id = order.id
                order.status = 'completed'
                order.pin_id = pin.id
                order.pin_delivered = pin.code
                order.automation_response = json.dumps({
                    'success': True,
                    'message': mensaje,
                    'player_name': player_name,
                })
                order.updated_at = datetime.utcnow()
                process_affiliate_commission(order)
                db.session.commit()
                try:
                    notify_order_completed(order, order.package, order.game)
                except Exception:
                    pass
                extra = f' (Jugador: {player_name})' if player_name else ''
                return {
                    'ok': True,
                    'changed': True,
                    'message': f'Orden #{order.order_number} completada vía automatización.{extra}',
                    'category': 'success',
                }

            return {
                'ok': False,
                'changed': False,
                'message': (
                    f'Redención fallida: {mensaje or "Error desconocido del VPS"}. '
                    'El PIN se mantiene en stock. La orden sigue pendiente.'
                ),
                'category': 'danger',
            }
        except requests.exceptions.Timeout:
            return {
                'ok': False,
                'changed': False,
                'message': f'El VPS no respondió en {vps_timeout}s. Reintenta más tarde. El PIN no fue consumido.',
                'category': 'danger',
            }
        except requests.exceptions.ConnectionError:
            return {
                'ok': False,
                'changed': False,
                'message': 'No se pudo conectar al bot de recarga. Verifica que el servicio esté activo en el VPS.',
                'category': 'danger',
            }
        except Exception as exc:
            return {
                'ok': False,
                'changed': False,
                'message': f'Error inesperado al contactar el VPS: {exc}',
                'category': 'danger',
            }

    if needs_pin_delivery:
        pin.is_used = True
        pin.used_at = datetime.utcnow()
        pin.order_id = order.id
        order.status = 'completed'
        order.pin_id = pin.id
        order.pin_delivered = pin.code
        order.updated_at = datetime.utcnow()
        process_affiliate_commission(order)
        db.session.commit()
        try:
            notify_order_completed(order, order.package, order.game, pin_code=pin.code)
        except Exception:
            pass
        return {
            'ok': True,
            'changed': True,
            'message': f'Orden #{order.order_number} completada y PIN entregado.',
            'category': 'success',
        }

    order.status = 'approved'
    order.updated_at = datetime.utcnow()
    process_affiliate_commission(order)
    db.session.commit()
    try:
        notify_order_approved(order, order.package, order.game)
    except Exception:
        pass
    return {
        'ok': True,
        'changed': True,
        'message': f'Orden #{order.order_number} aprobada.',
        'category': 'success',
    }