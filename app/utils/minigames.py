import json
from datetime import datetime

from flask import current_app

from ..models import Game, MiniGameCounter, OrderMiniGameOpportunity, Package, Setting, db

MINIGAME_GAME_DEFS = [
    {'key': 'ruleta', 'label': 'Ruleta', 'icon': '🎯'},
    {'key': 'tragaperras', 'label': 'Tragaperras', 'icon': '🎰'},
    {'key': 'caja_sorpresa', 'label': 'Caja sorpresa', 'icon': '🎁'},
]
MINIGAME_GAME_KEYS = {item['key'] for item in MINIGAME_GAME_DEFS}
MINIGAME_GLOBAL_COUNTER_KEY = 'global'
MINIGAME_SLOT_MISS_REELS = [
    {'icon': '💎', 'label': 'BONUS'},
    {'icon': '🪙', 'label': 'COIN'},
    {'icon': '⭐', 'label': 'LUCKY'},
]

# Los 3 juegos con minijuego dedicado tras la recarga. El resto de los
# juegos no ofrece minijuego en absoluto (se decide con
# order_qualifies_for_minigame más abajo). Cada slot lo configura el
# admin: a qué Game de la tienda corresponde y qué Package se entrega
# como premio real (diamantes/oro) al ID que hizo la recarga.
MINIGAME_SLOTS = [
    {'slot_key': 'free_fire', 'label': 'Free Fire'},
    {'slot_key': 'blood_strike', 'label': 'Blood Strike'},
    {'slot_key': 'mlbb', 'label': 'Mobile Legends'},
]
MINIGAME_SLOT_KEYS = {item['slot_key'] for item in MINIGAME_SLOTS}

# Relleno visual para que la ruleta/caja se vea con varios premios, tal
# como se pidió ("que estén varios premios pero que el único que sea
# ganable sea el primer paquete"). Estos nunca se entregan de verdad.
MINIGAME_DECORATIVE_LABELS = ['Bono Sorpresa', 'Súper Premio', 'Extra']

DEFAULT_MINIGAME_WIN_INTERVAL = 50


def get_minigame_game_defs():
    return [dict(item) for item in MINIGAME_GAME_DEFS]


def get_minigame_game_label(game_key):
    for item in MINIGAME_GAME_DEFS:
        if item['key'] == game_key:
            return item['label']
    return str(game_key or '').strip()


def is_minigame_dev_mode():
    try:
        return bool(current_app.config.get('MINIGAME_DEV_MODE'))
    except Exception:
        return False


def _get_setting_value(key):
    setting = Setting.query.filter_by(key=key).first()
    return setting.value if setting and setting.value else ''


def _get_setting_int(key):
    raw = _get_setting_value(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def get_minigame_win_interval():
    """Cada cuántos giros (de todos los clientes, por juego) se otorga el
    premio real. Configurable en el admin, con 50 como valor por defecto."""
    value = _get_setting_int('minigame_win_every_n_spins')
    return value if value and value > 0 else DEFAULT_MINIGAME_WIN_INTERVAL


def get_minigame_slot_defs():
    return [dict(item) for item in MINIGAME_SLOTS]


def get_minigame_slot_config(slot_key):
    slot_key = str(slot_key or '').strip()
    slot_def = next((item for item in MINIGAME_SLOTS if item['slot_key'] == slot_key), None)
    label = slot_def['label'] if slot_def else slot_key

    game_id = _get_setting_int(f'minigame_slot_{slot_key}_game_id')
    package_id = _get_setting_int(f'minigame_slot_{slot_key}_prize_package_id')
    game = Game.query.get(game_id) if game_id else None
    package = Package.query.get(package_id) if package_id else None

    return {
        'slot_key': slot_key,
        'label': label,
        'game': game,
        'prize_package': package,
        'enabled': bool(game and package and game.is_active and package.is_active),
    }


def get_minigame_slots_config():
    return [get_minigame_slot_config(item['slot_key']) for item in MINIGAME_SLOTS]


def get_minigame_slot_for_game(game_id):
    if not game_id:
        return None
    for slot_def in MINIGAME_SLOTS:
        cfg = get_minigame_slot_config(slot_def['slot_key'])
        if cfg['enabled'] and cfg['game'] and cfg['game'].id == int(game_id):
            return cfg
    return None


def order_qualifies_for_minigame(order):
    if not order:
        return False
    if is_minigame_dev_mode():
        return bool(order.status != 'rejected')
    if order.status == 'rejected':
        return False
    if not (order.status == 'completed' or order.payment_verified_at):
        return False
    # Solo los 3 juegos configurados (Free Fire / Blood Strike / MLBB)
    # ofrecen minijuego; el resto no tiene ruleta tras la recarga.
    return bool(get_minigame_slot_for_game(order.game_id))


def get_minigame_counter_scope_key(order_or_game):
    """El contador de giros es POR JUEGO (no global ni por cliente): así
    'cada 50 giros' cuenta los giros de todos los clientes en ese juego."""
    if order_or_game is None:
        return MINIGAME_GLOBAL_COUNTER_KEY

    game_id = getattr(order_or_game, 'game_id', None)
    if game_id is None and isinstance(order_or_game, Game):
        game_id = order_or_game.id
    if not game_id:
        return MINIGAME_GLOBAL_COUNTER_KEY

    slot = get_minigame_slot_for_game(game_id)
    return slot['slot_key'] if slot else MINIGAME_GLOBAL_COUNTER_KEY


def get_or_create_minigame_counter(order_or_game):
    scope_key = get_minigame_counter_scope_key(order_or_game)

    counter = MiniGameCounter.query.filter_by(game_key=scope_key).first()
    if counter:
        return counter

    counter = MiniGameCounter(game_key=scope_key, play_count=0, last_position=0)
    db.session.add(counter)
    db.session.flush()
    return counter


def ensure_minigame_opportunity(order):
    if not order_qualifies_for_minigame(order):
        return None

    opportunity = OrderMiniGameOpportunity.query.filter_by(order_id=order.id).first()
    if opportunity:
        return opportunity

    opportunity = OrderMiniGameOpportunity(order_id=order.id, status='available')
    db.session.add(opportunity)
    db.session.flush()
    return opportunity


def get_minigame_reward_catalog_for_order(order):
    """Catálogo de premios a mostrar en la ruleta/caja para esta orden:
    el premio real primero (el único ganable) más relleno decorativo."""
    if not order:
        return []
    slot = get_minigame_slot_for_game(order.game_id)
    if not slot or not slot.get('enabled'):
        return []

    prize_label = slot['prize_package'].name if slot['prize_package'] else 'Premio'
    catalog = [{'label': prize_label, 'winnable': True}]
    for decor_label in MINIGAME_DECORATIVE_LABELS:
        catalog.append({'label': decor_label, 'winnable': False})
    return catalog


def build_minigame_roulette_segments(catalog):
    labels = []
    for reward in catalog:
        labels.append('FAILED')
        labels.append(reward.get('label') or 'Premio')
    return labels or ['FAILED', 'Premio']


def build_minigame_surprise_items(catalog):
    items = [
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '❌', 'label': 'FALLASTE'},
    ]
    for reward in catalog:
        items.append({'icon': '🎁', 'label': reward.get('label') or 'Premio'})
    return items[:9]


def build_slot_reels_for_reward(reward):
    reward_kind = reward.get('kind')
    reward_label = reward.get('label') or 'Fallaste'

    if reward_kind != 'game_prize':
        return [dict(symbol) for symbol in MINIGAME_SLOT_MISS_REELS]

    symbol = {'icon': '🎁', 'label': reward_label}
    return [dict(symbol), dict(symbol), dict(symbol)]


def build_minigame_result_payload(game_key, reward, choice_index=None, catalog=None):
    catalog = catalog if catalog is not None else []
    reward_kind = reward.get('kind')
    reward_label = reward.get('label') or 'Fallaste'

    if game_key == 'ruleta':
        segments = build_minigame_roulette_segments(catalog)
        target_label = reward_label if reward_kind == 'game_prize' else 'FAILED'
        target_index = 0
        for index, label in enumerate(segments):
            if label.lower() == str(target_label).lower():
                target_index = index
                break
        return {
            'segments': segments,
            'target_index': target_index,
            'target_label': reward_label,
        }

    if game_key == 'tragaperras':
        return {
            'reels': build_slot_reels_for_reward(reward)
        }

    items = build_minigame_surprise_items(catalog)
    selected_index = int(choice_index or 0)
    if selected_index < 0 or selected_index >= len(items):
        selected_index = 0
    if reward_kind == 'game_prize':
        items[selected_index] = {'icon': '🎁', 'label': reward_label}
    else:
        items[selected_index] = {'icon': '❌', 'label': 'FALLASTE'}
    return {
        'items': items,
        'selected_index': selected_index,
    }


def select_order_minigame(order, game_key):
    game_key = str(game_key or '').strip().lower()
    if game_key not in MINIGAME_GAME_KEYS:
        raise ValueError('Juego inválido.')

    opportunity = ensure_minigame_opportunity(order)
    if not opportunity:
        raise ValueError('Esta orden todavía no tiene una oportunidad activa.')
    if opportunity.status == 'played':
        return opportunity
    if opportunity.selected_game_key and opportunity.selected_game_key != game_key:
        raise ValueError('Ya seleccionaste otro minijuego para esta oportunidad.')

    opportunity.selected_game_key = game_key
    if opportunity.status == 'available':
        opportunity.status = 'selected'
    opportunity.updated_at = datetime.utcnow()
    db.session.flush()
    return opportunity


def play_order_minigame(order, game_key, choice_index=None):
    game_key = str(game_key or '').strip().lower()
    if game_key not in MINIGAME_GAME_KEYS:
        raise ValueError('Juego inválido.')

    opportunity = ensure_minigame_opportunity(order)
    if not opportunity:
        raise ValueError('Esta orden todavía no tiene una oportunidad activa.')
    if opportunity.status == 'played':
        return opportunity

    if opportunity.selected_game_key and opportunity.selected_game_key != game_key:
        raise ValueError('Esta oportunidad ya quedó bloqueada a otro minijuego.')

    dev_mode = is_minigame_dev_mode()
    slot = get_minigame_slot_for_game(order.game_id)
    if not slot and not dev_mode:
        raise ValueError('Este juego no tiene minijuego habilitado en este momento.')

    win_interval = get_minigame_win_interval()
    counter = get_or_create_minigame_counter(order)
    counter.play_count = int(counter.play_count or 0) + 1
    counter.last_position = counter.play_count
    counter.updated_at = datetime.utcnow()

    is_win = bool(slot) and (counter.play_count % win_interval == 0)
    if dev_mode and not is_win:
        is_win = True  # en modo prueba, siempre se gana para poder revisar el flujo

    prize_order = None
    if is_win and slot:
        # Reusa el mismo camino de entrega automática que cualquier compra
        # real (PIN propio o Revendedores) en vez de reinventar la lógica.
        from .order_processing import deliver_prize_to_player

        prize_label = slot['prize_package'].name if slot['prize_package'] else 'Premio'
        prize_order, approval = deliver_prize_to_player(
            slot['game'], slot['prize_package'], order.player_id,
            zone_id=order.zone_id,
            note=f'Premio de minijuego ganado en la orden #{order.order_number}.',
            reference_prefix='MINIJUEGO',
        )
        reward_kind = 'game_prize'
        reward_label = prize_label
        if prize_order and approval and not approval.get('ok'):
            # La orden del premio quedó registrada pero no se pudo entregar
            # sola (p.ej. sin stock en ese momento); el admin la completa a
            # mano desde Órdenes, igual que cualquier compra normal sin stock.
            reward_label = f'{prize_label} (pendiente de entrega)'
    elif is_win:
        # Solo posible en modo prueba con un juego sin slot configurado.
        reward_kind = 'game_prize'
        reward_label = 'Premio de prueba'
    else:
        reward_kind = 'miss'
        reward_label = 'Fallaste'

    reward_payload = {'kind': reward_kind, 'label': reward_label}
    catalog = get_minigame_reward_catalog_for_order(order)

    opportunity.status = 'played'
    opportunity.selected_game_key = game_key
    opportunity.selected_choice_index = int(choice_index) if choice_index is not None else None
    opportunity.result_kind = reward_kind
    opportunity.reward_tier = 1 if is_win else 0
    opportunity.reward_label = reward_label
    opportunity.reward_amount = None
    opportunity.reward_discount_id = None
    opportunity.reward_discount_code = ''
    opportunity.prize_order_id = prize_order.id if prize_order else None
    opportunity.counter_position = counter.play_count
    opportunity.counter_cycle = ((counter.play_count - 1) // win_interval) + 1
    opportunity.played_at = datetime.utcnow()
    opportunity.result_payload = json.dumps(
        build_minigame_result_payload(game_key, reward_payload, choice_index=choice_index, catalog=catalog),
        ensure_ascii=False,
    )
    opportunity.updated_at = datetime.utcnow()
    db.session.flush()
    return opportunity


def serialize_minigame_opportunity(opportunity):
    if not opportunity:
        return None

    result_payload = {}
    if opportunity.result_payload:
        try:
            result_payload = json.loads(opportunity.result_payload)
        except Exception:
            result_payload = {}

    reward_amount = None
    if opportunity.reward_amount is not None:
        reward_amount = float(opportunity.reward_amount)

    return {
        'status': opportunity.status,
        'selected_game_key': opportunity.selected_game_key,
        'selected_game_label': get_minigame_game_label(opportunity.selected_game_key),
        'selected_choice_index': opportunity.selected_choice_index,
        'played': opportunity.status == 'played',
        'result_kind': opportunity.result_kind,
        'reward_tier': opportunity.reward_tier,
        'reward_label': opportunity.reward_label,
        'reward_amount': reward_amount,
        'reward_discount_code': opportunity.reward_discount_code,
        'counter_position': opportunity.counter_position,
        'counter_cycle': opportunity.counter_cycle,
        'played_at': opportunity.played_at.isoformat() if opportunity.played_at else None,
        'result_payload': result_payload,
    }


def get_order_minigame_state(order, create_if_needed=True):
    opportunity = OrderMiniGameOpportunity.query.filter_by(order_id=order.id).first() if order else None
    if create_if_needed and not opportunity:
        opportunity = ensure_minigame_opportunity(order)

    return {
        'eligible': bool(opportunity),
        'qualifies': order_qualifies_for_minigame(order),
        'dev_mode': is_minigame_dev_mode(),
        'games': get_minigame_game_defs(),
        'reward_catalog': get_minigame_reward_catalog_for_order(order),
        'opportunity': serialize_minigame_opportunity(opportunity),
    }
