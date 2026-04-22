import json
from datetime import datetime

import requests
from flask import current_app

from ..models import Affiliate, AffiliateCommission, Pin, RevendedoresItemMapping, db
from .minigames import ensure_minigame_opportunity
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
    mappings = get_order_auto_mappings(order_obj)
    return mappings[0] if mappings else None


def get_order_auto_mappings(order_obj):
    try:
        if not order_obj or not order_obj.package_id:
            return []
        mapping = RevendedoresItemMapping.query.filter_by(
            store_package_id=int(order_obj.package_id),
            active=True,
            auto_enabled=True,
        ).first()
        if not mapping:
            return []

        items = []
        for attr_name in ('catalog_item', 'catalog_item_2'):
            item = getattr(mapping, attr_name, None)
            if not item:
                continue
            items.append(item)

        return items
    except Exception:
        return []


def _load_revendedores_auto_response(order, catalog_items, base_state=None):
    auto_resp = {}
    if isinstance(base_state, dict):
        auto_resp = dict(base_state)
    else:
        try:
            auto_resp = json.loads(order.automation_response or '{}')
        except Exception:
            auto_resp = {}

    existing_steps = auto_resp.get('steps') if isinstance(auto_resp.get('steps'), list) else []
    steps = []

    for idx, item in enumerate(catalog_items):
        current = existing_steps[idx] if idx < len(existing_steps) and isinstance(existing_steps[idx], dict) else {}
        steps.append({
            'slot': idx + 1,
            'catalog_id': item.id,
            'remote_product_id': item.remote_product_id,
            'remote_product_name': item.remote_product_name or '',
            'remote_package_id': item.remote_package_id,
            'remote_package_name': item.remote_package_name or '',
            'success': bool(current.get('success')),
            'pending_verification': bool(current.get('pending_verification')),
            'rev_attempt': int(current.get('rev_attempt') or 0),
            'external_order_id': current.get('external_order_id') or '',
            'player_name': current.get('player_name') or '',
            'reference_no': current.get('reference_no') or '',
            'order_id': current.get('order_id'),
            'error': current.get('error') or '',
            'verified': bool(current.get('verified')),
        })

    if not existing_steps and auto_resp.get('source') == 'revendedores_api' and steps:
        steps[0].update({
            'success': bool(auto_resp.get('success')),
            'pending_verification': bool(auto_resp.get('pending_verification')),
            'rev_attempt': int(auto_resp.get('rev_attempt') or 0),
            'external_order_id': auto_resp.get('external_order_id') or '',
            'player_name': auto_resp.get('player_name') or '',
            'reference_no': auto_resp.get('reference_no') or '',
            'order_id': auto_resp.get('order_id'),
            'error': auto_resp.get('error') or '',
            'verified': bool(auto_resp.get('verified')),
        })

    auto_resp['source'] = 'revendedores_api'
    auto_resp['steps'] = steps
    auto_resp['pending_verification'] = any(step.get('pending_verification') for step in steps)
    auto_resp['current_step_index'] = next((idx for idx, step in enumerate(steps) if not step.get('success')), None)
    auto_resp['step_count'] = len(steps)
    return auto_resp


def process_revendedores_queue(order, base_state=None):
    catalog_items = get_order_auto_mappings(order)
    if not catalog_items:
        return None

    base_url, api_key, _, recharge_path = get_revendedores_env()
    auto_resp = _load_revendedores_auto_response(order, catalog_items, base_state=base_state)
    steps = auto_resp.get('steps') or []

    if not base_url or not api_key:
        auto_resp['pending_verification'] = False
        auto_resp['last_error'] = 'Revendedores API no configurada.'
        order.automation_response = json.dumps(auto_resp)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {
            'ok': False,
            'changed': False,
            'pending_verification': False,
            'message': 'Revendedores API no configurada.',
            'category': 'warning',
        }

    for step_index, step in enumerate(steps):
        if step.get('success'):
            continue

        rev_attempt = int(step.get('rev_attempt') or 0) + 1
        ext_order_id = f'{order.order_number}-s{step_index + 1}-{rev_attempt}'
        rev_payload = {
            'product_id': step.get('remote_product_id'),
            'package_id': step.get('remote_package_id'),
            'player_id': str(order.player_id or '').strip(),
            'external_order_id': ext_order_id,
        }
        if order.zone_id:
            rev_payload['player_id2'] = str(order.zone_id).strip()

        try:
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
                step.update({
                    'success': True,
                    'pending_verification': False,
                    'rev_attempt': rev_attempt,
                    'external_order_id': ext_order_id,
                    'player_name': player_name,
                    'reference_no': ref_no,
                    'order_id': rev_data.get('order_id'),
                    'verified': True,
                    'error': '',
                })
                note = f"[Revendedores API][Paso {step_index + 1}] Ref: {ref_no}, Player: {player_name}" if (ref_no or player_name) else f"[Revendedores API][Paso {step_index + 1}] Recarga completada."
                order.notes = ((order.notes or '') + '\n' + note).strip()
                continue

            rev_error = rev_data.get('error', resp.text[:200] if not resp.ok else 'Error desconocido')
            step.update({
                'success': False,
                'pending_verification': True,
                'rev_attempt': rev_attempt,
                'external_order_id': ext_order_id,
                'error': rev_error,
            })
            auto_resp['pending_verification'] = True
            auto_resp['current_step_index'] = step_index
            auto_resp['external_order_id'] = ext_order_id
            auto_resp['last_error'] = rev_error
            order.automation_response = json.dumps(auto_resp)
            db.session.commit()
            return {
                'ok': False,
                'changed': False,
                'pending_verification': True,
                'current_step_index': step_index,
                'message': f'Revendedores reportó error en el paso {step_index + 1}: {rev_error}. Verificando si se procesó para continuar con el siguiente.',
                'category': 'warning',
            }
        except Exception as exc:
            step.update({
                'success': False,
                'pending_verification': True,
                'rev_attempt': rev_attempt,
                'external_order_id': ext_order_id,
                'error': str(exc),
            })
            auto_resp['pending_verification'] = True
            auto_resp['current_step_index'] = step_index
            auto_resp['external_order_id'] = ext_order_id
            auto_resp['last_error'] = str(exc)
            order.automation_response = json.dumps(auto_resp)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {
                'ok': False,
                'changed': False,
                'pending_verification': True,
                'current_step_index': step_index,
                'message': f'Error contactando Revendedores API en el paso {step_index + 1}: {exc}. Verificando si se procesó para continuar con el siguiente.',
                'category': 'warning',
            }

    auto_resp['pending_verification'] = False
    auto_resp['current_step_index'] = None
    auto_resp['success'] = True
    order.status = 'completed'
    ensure_minigame_opportunity(order)
    order.automation_response = json.dumps(auto_resp)
    order.updated_at = datetime.utcnow()
    process_affiliate_commission(order)
    db.session.commit()
    try:
        notify_order_completed(order, order.package, order.game)
    except Exception:
        pass
    return {
        'ok': True,
        'changed': True,
        'pending_verification': False,
        'message': f'Orden #{order.order_number} completada vía Revendedores API en {len(steps)} paso(s).',
        'category': 'success',
    }


def get_revendedores_env():
    base_url = current_app.config.get('REVENDEDORES_BASE_URL', '').rstrip('/')
    api_key = current_app.config.get('REVENDEDORES_API_KEY', '')
    catalog_path = '/api/v1/products'
    recharge_path = '/api/v1/recharge'
    return base_url, api_key, catalog_path, recharge_path


def approve_order(order, delivery_proof_path=None):
    if order.status != 'pending':
        return {
            'ok': False,
            'changed': False,
            'message': 'Solo se pueden aprobar órdenes pendientes.',
            'category': 'warning',
        }

    rev_mapping = get_order_auto_mapping(order)
    if rev_mapping:
        result = process_revendedores_queue(order)
        if result:
            return result

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
                ensure_minigame_opportunity(order)
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
        ensure_minigame_opportunity(order)
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
    if delivery_proof_path:
        order.delivery_proof = delivery_proof_path
    order.updated_at = datetime.utcnow()
    process_affiliate_commission(order)
    db.session.commit()
    try:
        notify_order_approved(order, order.package, order.game, delivery_proof_path=delivery_proof_path)
    except Exception:
        pass
    return {
        'ok': True,
        'changed': True,
        'message': f'Orden #{order.order_number} aprobada.',
        'category': 'success',
    }