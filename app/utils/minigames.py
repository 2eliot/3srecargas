import json
from datetime import datetime
from decimal import Decimal

from flask import current_app

from ..models import Discount, MiniGameCounter, OrderMiniGameOpportunity, Setting, db

MINIGAME_CYCLE_LENGTH = 200
MINIGAME_GAME_DEFS = [
    {'key': 'ruleta', 'label': 'Ruleta', 'icon': '🎯'},
    {'key': 'tragaperras', 'label': 'Tragaperras', 'icon': '🎰'},
    {'key': 'caja_sorpresa', 'label': 'Caja sorpresa', 'icon': '🎁'},
]
MINIGAME_GAME_KEYS = {item['key'] for item in MINIGAME_GAME_DEFS}
MINIGAME_REWARD_SCHEDULE = {
    40: {'kind': 'coupon', 'tier': 1, 'label': 'Cupón 1'},
    80: {'kind': 'coupon', 'tier': 2, 'label': 'Cupón 2'},
    120: {'kind': 'cash', 'tier': 1, 'label': '$1', 'amount': Decimal('1.00')},
    160: {'kind': 'coupon', 'tier': 3, 'label': 'Cupón 3'},
    200: {'kind': 'cash', 'tier': 3, 'label': '$3', 'amount': Decimal('3.00')},
}


def get_minigame_game_defs():
    return [dict(item) for item in MINIGAME_GAME_DEFS]


def get_minigame_game_label(game_key):
    for item in MINIGAME_GAME_DEFS:
        if item['key'] == game_key:
            return item['label']
    return str(game_key or '').strip()


def get_minigame_reward_for_position(position):
    reward = MINIGAME_REWARD_SCHEDULE.get(int(position or 0))
    if reward:
        return dict(reward)
    return {'kind': 'miss', 'tier': 0, 'label': 'Fallaste'}


def is_minigame_dev_mode():
    try:
        return bool(current_app.config.get('MINIGAME_DEV_MODE'))
    except Exception:
        return False


def order_qualifies_for_minigame(order):
    if not order:
        return False
    if is_minigame_dev_mode():
        return bool(order.status != 'rejected')
    if order.status == 'rejected':
        return False
    return bool(order.status == 'completed' or order.payment_verified_at)


def get_minigame_counter_scope_key(order_or_game):
    if not order_or_game:
        raise ValueError('No se pudo resolver el juego de la orden.')

    game_id = getattr(order_or_game, 'game_id', None)
    if game_id is None:
        game_id = getattr(order_or_game, 'id', None)
    if game_id is None:
        game = getattr(order_or_game, 'game', None)
        game_id = getattr(game, 'id', None)
    if game_id is None:
        raise ValueError('No se pudo resolver el juego de la orden.')

    return f'store_game:{int(game_id)}'


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


def _reward_setting_key(tier):
    return f'minigame_coupon_reward_{int(tier)}'


def get_minigame_reward_discount(tier):
    setting = Setting.query.filter_by(key=_reward_setting_key(tier)).first()
    raw_value = (setting.value if setting else '').strip()
    if not raw_value.isdigit():
        return None
    return Discount.query.filter_by(id=int(raw_value)).first()


def build_minigame_result_payload(game_key, reward, choice_index=None):
    reward_kind = reward.get('kind')
    reward_label = reward.get('label') or 'Fallaste'

    if game_key == 'ruleta':
        segments = [
            'FAILED', 'Cupón', '$3.00', 'FAILED', '$5.00',
            'Cupón', '$1.00', 'FAILED', 'FAILED', 'Cupón',
        ]
        if reward_kind == 'miss':
            target_label = 'FAILED'
        elif reward_kind == 'coupon':
            target_label = 'Cupón'
        elif reward_kind == 'cash' and reward.get('amount') is not None:
            target_label = f'${Decimal(reward.get("amount")):.2f}'
        else:
            target_label = str(reward_label or '').strip() or 'FAILED'
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
        center_symbol = {
            'miss': {'icon': '❌', 'label': 'FAILED'},
            'coupon': {'icon': '🎟️', 'label': reward_label},
            'cash': {'icon': '🔥' if reward.get('amount') == Decimal('3.00') else '💰', 'label': reward_label},
        }.get(reward_kind, {'icon': '❌', 'label': reward_label})
        return {
            'reels': [
                {'icon': '❌', 'label': 'FAILED'},
                center_symbol,
                {'icon': '🔥' if reward_kind == 'cash' else '❌', 'label': reward_label if reward_kind == 'cash' else 'FAILED'},
            ]
        }

    items = [
        {'icon': '🎟️', 'label': 'CUPÓN %'},
        {'icon': '🎟️', 'label': 'CUPÓN %'},
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '💰', 'label': '$1.00'},
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '🔥', 'label': '$3.00'},
        {'icon': '💎', 'label': '$5.00'},
    ]
    selected_index = int(choice_index or 0)
    if selected_index < 0 or selected_index >= len(items):
        selected_index = 0
    if reward_kind == 'coupon':
        items[selected_index] = {'icon': '🎟️', 'label': reward_label}
    elif reward_kind == 'cash':
        items[selected_index] = {'icon': '🔥' if reward.get('amount') == Decimal('3.00') else '💰', 'label': reward_label}
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

    counter = get_or_create_minigame_counter(order)
    counter.play_count = int(counter.play_count or 0) + 1
    position = ((counter.play_count - 1) % MINIGAME_CYCLE_LENGTH) + 1
    cycle_number = ((counter.play_count - 1) // MINIGAME_CYCLE_LENGTH) + 1
    if is_minigame_dev_mode():
        position = 40
        cycle_number = 1
    counter.last_position = position
    counter.updated_at = datetime.utcnow()

    reward = get_minigame_reward_for_position(position)
    reward_discount = None
    reward_discount_code = ''
    reward_amount = None

    if reward.get('kind') == 'coupon':
        reward_discount = get_minigame_reward_discount(reward.get('tier'))
        reward_discount_code = reward_discount.code if reward_discount else ''
    elif reward.get('kind') == 'cash':
        reward_amount = reward.get('amount')

    opportunity.status = 'played'
    opportunity.selected_game_key = game_key
    opportunity.selected_choice_index = int(choice_index) if choice_index is not None else None
    opportunity.result_kind = reward.get('kind')
    opportunity.reward_tier = int(reward.get('tier') or 0)
    opportunity.reward_label = reward.get('label')
    opportunity.reward_amount = reward_amount
    opportunity.reward_discount_id = reward_discount.id if reward_discount else None
    opportunity.reward_discount_code = reward_discount_code
    opportunity.counter_position = position
    opportunity.counter_cycle = cycle_number
    opportunity.played_at = datetime.utcnow()
    opportunity.result_payload = json.dumps(
        build_minigame_result_payload(game_key, reward, choice_index=choice_index),
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
        'opportunity': serialize_minigame_opportunity(opportunity),
    }