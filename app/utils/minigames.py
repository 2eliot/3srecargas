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
MINIGAME_CYCLE_LENGTH = 300
MINIGAME_GLOBAL_COUNTER_KEY = 'global'
MINIGAME_REWARD_DEFS = [
    {'tier': 1, 'kind': 'coupon', 'position': 60, 'default_label': 'Cupón 5%'},
    {'tier': 2, 'kind': 'coupon', 'position': 120, 'default_label': 'Cupón 10%'},
    {'tier': 3, 'kind': 'coupon', 'position': 180, 'default_label': 'Cupón 15%'},
    {'tier': 4, 'kind': 'coupon', 'position': 240, 'default_label': 'Cupón $1'},
    {'tier': 5, 'kind': 'coupon', 'position': 300, 'default_label': 'Cupón $3'},
    {'tier': 6, 'kind': 'coupon', 'position': None, 'default_label': 'Cupón $5', 'visible_only': True},
]
MINIGAME_REWARD_SCHEDULE = {
    int(item['position']): {
        'kind': item['kind'],
        'tier': item['tier'],
        'label': item['default_label'],
    }
    for item in MINIGAME_REWARD_DEFS
    if item.get('position')
}
MINIGAME_SLOT_MISS_REELS = [
    {'icon': '💎', 'label': 'BONUS'},
    {'icon': '🪙', 'label': 'COIN'},
    {'icon': '⭐', 'label': 'LUCKY'},
]


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
    return MINIGAME_GLOBAL_COUNTER_KEY


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


def _parse_reward_setting_ids(raw_value):
    values = []
    for item in str(raw_value or '').split(','):
        item = item.strip()
        if item.isdigit():
            values.append(int(item))
    return values


def get_minigame_reward_discount_ids(tier):
    setting = Setting.query.filter_by(key=_reward_setting_key(tier)).first()
    return _parse_reward_setting_ids(setting.value if setting else '')


def _format_minigame_discount_label(discount, fallback=''):
    if not discount:
        return str(fallback or '').strip() or 'Cupón'

    description = str(discount.description or '').strip()
    if description:
        return description

    if discount.discount_type == 'percentage':
        return f'Cupón {float(discount.discount_value or 0):.0f}%'

    value = float(discount.discount_value or 0)
    value_text = f'{value:.2f}'.rstrip('0').rstrip('.')
    return f'Cupón ${value_text}'


def get_minigame_reward_discount_pool(tier):
    configured_ids = get_minigame_reward_discount_ids(tier)
    if not configured_ids:
        return []

    discounts = Discount.query.filter(Discount.id.in_(configured_ids)).all()
    discounts_by_id = {discount.id: discount for discount in discounts}
    ordered = []
    for discount_id in configured_ids:
        discount = discounts_by_id.get(discount_id)
        if discount:
            ordered.append(discount)
    return ordered


def get_minigame_reward_discount(tier):
    pool = get_minigame_reward_discount_pool(tier)
    if not pool:
        return None

    pool_ids = [discount.id for discount in pool]
    assigned_ids = {
        int(discount_id)
        for (discount_id,) in (
            db.session.query(OrderMiniGameOpportunity.reward_discount_id)
            .filter(OrderMiniGameOpportunity.reward_discount_id.in_(pool_ids))
            .all()
        )
        if discount_id is not None
    }

    now = datetime.utcnow()
    for discount in pool:
        if not discount.is_active:
            continue
        if discount.expires_at and discount.expires_at < now:
            continue
        if discount.id in assigned_ids:
            continue
        return discount

    return None


def get_minigame_reward_label(tier, fallback=''):
    pool = get_minigame_reward_discount_pool(tier)
    if pool:
        return _format_minigame_discount_label(pool[0], fallback=fallback)
    return str(fallback or '').strip()


def get_minigame_reward_definitions():
    reward_defs = []
    for item in MINIGAME_REWARD_DEFS:
        reward_defs.append({
            'tier': int(item['tier']),
            'kind': item['kind'],
            'position': item.get('position'),
            'default_label': item['default_label'],
            'label': get_minigame_reward_label(item['tier'], fallback=item['default_label']),
            'visible_only': bool(item.get('visible_only')),
        })
    return reward_defs


def build_minigame_roulette_segments():
    reward_defs = get_minigame_reward_definitions()
    labels = []
    for reward in reward_defs:
        labels.append('FAILED')
        labels.append(reward.get('label') or reward.get('default_label') or 'Cupón')
    return labels


def build_minigame_surprise_items():
    items = [
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '❌', 'label': 'FALLASTE'},
        {'icon': '❌', 'label': 'FALLASTE'},
    ]
    for reward in get_minigame_reward_definitions():
        items.append({
            'icon': '🎟️',
            'label': reward.get('label') or reward.get('default_label') or 'Cupón',
        })
    return items[:9]


def build_slot_reels_for_reward(reward):
    reward_kind = reward.get('kind')
    reward_label = reward.get('label') or 'Fallaste'

    if reward_kind == 'miss':
        return [dict(symbol) for symbol in MINIGAME_SLOT_MISS_REELS]

    if reward_kind == 'coupon':
        symbol = {'icon': '🎟️', 'label': reward_label}
    elif reward_kind == 'cash':
        symbol = {
            'icon': '🔥' if reward.get('amount') == Decimal('3.00') else '💰',
            'label': reward_label,
        }
    else:
        symbol = {'icon': '❌', 'label': reward_label}

    return [dict(symbol), dict(symbol), dict(symbol)]


def build_minigame_result_payload(game_key, reward, choice_index=None):
    reward_kind = reward.get('kind')
    reward_label = reward.get('label') or 'Fallaste'

    if game_key == 'ruleta':
        segments = build_minigame_roulette_segments()
        if reward_kind == 'miss':
            target_label = 'FAILED'
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
        return {
            'reels': build_slot_reels_for_reward(reward)
        }

    items = build_minigame_surprise_items()
    selected_index = int(choice_index or 0)
    if selected_index < 0 or selected_index >= len(items):
        selected_index = 0
    if reward_kind == 'coupon':
        items[selected_index] = {'icon': '🎟️', 'label': reward_label}
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
        position = 60
        cycle_number = 1
    counter.last_position = position
    counter.updated_at = datetime.utcnow()

    reward = get_minigame_reward_for_position(position)
    reward_discount = None
    reward_discount_code = ''
    reward_amount = None
    reward_label = get_minigame_reward_label(reward.get('tier'), fallback=reward.get('label'))

    if reward.get('kind') == 'coupon':
        reward_discount = get_minigame_reward_discount(reward.get('tier'))
        reward_discount_code = reward_discount.code if reward_discount else ''
        reward_label = _format_minigame_discount_label(reward_discount, fallback=reward_label)
    elif reward.get('kind') == 'cash':
        reward_amount = reward.get('amount')

    reward_payload = dict(reward)
    reward_payload['label'] = reward_label

    opportunity.status = 'played'
    opportunity.selected_game_key = game_key
    opportunity.selected_choice_index = int(choice_index) if choice_index is not None else None
    opportunity.result_kind = reward.get('kind')
    opportunity.reward_tier = int(reward.get('tier') or 0)
    opportunity.reward_label = reward_label
    opportunity.reward_amount = reward_amount
    opportunity.reward_discount_id = reward_discount.id if reward_discount else None
    opportunity.reward_discount_code = reward_discount_code
    opportunity.counter_position = position
    opportunity.counter_cycle = cycle_number
    opportunity.played_at = datetime.utcnow()
    opportunity.result_payload = json.dumps(
        build_minigame_result_payload(game_key, reward_payload, choice_index=choice_index),
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
        'reward_catalog': get_minigame_reward_definitions(),
        'opportunity': serialize_minigame_opportunity(opportunity),
    }