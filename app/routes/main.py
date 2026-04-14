import re
from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, jsonify, request, session, redirect, url_for, current_app, has_request_context
from flask_login import current_user

from ..models import (
    db, Game, Package, Category, PaymentMethod, Setting, RevendedoresItemMapping,
    Order, RankingArchive,
)
from ..utils.timezone import now_ve, ve_day_start_utc_naive

main_bp = Blueprint('main_bp', __name__)


def _get_setting_val(key, default=''):
    row = Setting.query.filter_by(key=key).first()
    return row.value if row else default


RANKING_DEFS = {
    'free_fire': {
        'label': 'Free Fire',
        'units_label': 'Diamantes',
        'setting_key': 'ranking_free_fire_game_id',
        'enabled_key': 'ranking_free_fire_enabled',
        'rewards': [6160, 2398, 1166, 572, 341],
        'aliases': ['free fire', 'freefire', 'ff'],
    },
    'blood_strike': {
        'label': 'Blood Strike',
        'units_label': 'Oro',
        'setting_key': 'ranking_blood_strike_game_id',
        'enabled_key': 'ranking_blood_strike_enabled',
        'rewards': [1500, 700, 350, 200, 120],
        'aliases': ['blood strike', 'bloodstrike'],
    },
}


def _month_range_for_ve(target_date=None):
    target_date = target_date or now_ve().date()
    month_start = date(target_date.year, target_date.month, 1)
    next_month = date(target_date.year + (1 if target_date.month == 12 else 0), 1 if target_date.month == 12 else target_date.month + 1, 1)
    return ve_day_start_utc_naive(month_start), ve_day_start_utc_naive(next_month)


def _resolve_ranking_game(config):
    game_id_raw = _get_setting_val(config['setting_key'], '')
    game_id = int(game_id_raw) if str(game_id_raw).isdigit() else None
    if game_id:
        game = Game.query.get(game_id)
        if game:
            return game

    aliases = [alias.strip().lower() for alias in (config.get('aliases') or []) if alias]
    if not aliases:
        return None

    all_games = Game.query.order_by(Game.is_active.desc(), Game.position.asc(), Game.name.asc()).all()
    for game in all_games:
        haystack = ' '.join([
            str(game.name or '').strip().lower(),
            str(game.slug or '').strip().lower(),
            str(game.description or '').strip().lower(),
        ])
        if any(alias in haystack for alias in aliases):
            return game

    return None


def _ranking_has_month_orders(game_id, target_date=None):
    if not game_id:
        return False

    start_at, end_at = _month_range_for_ve(target_date)
    return db.session.query(Order.id).filter(
        Order.game_id == game_id,
        Order.status.in_(['approved', 'completed']),
        Order.created_at >= start_at,
        Order.created_at < end_at,
    ).first() is not None


def _is_ranking_enabled(config, game, target_date=None):
    setting_row = Setting.query.filter_by(key=config['enabled_key']).first()
    if setting_row and str(setting_row.value or '').strip() != '':
        return str(setting_row.value).strip() == '1'
    return bool(game and _ranking_has_month_orders(game.id, target_date=target_date))


def _is_admin_ranking_view():
    if not has_request_context():
        return False
    return current_user.is_authenticated and current_user.__class__.__name__ == 'AdminUser'


def _mask_player_id(player_id):
    raw = (player_id or '').strip()
    if not raw:
        return '----'
    visible = raw[:4]
    return visible + '****'


def _mask_nickname(nickname):
    raw = (nickname or '').strip()
    if not raw:
        return 'Jugador***'
    visible = raw[:3]
    return visible + '***'


def _extract_order_units(order):
    package_name = (order.package.name if order.package else '') or ''
    package_desc = (order.package.description if order.package else '') or ''
    search_text = f'{package_name} {package_desc}'
    matches = re.findall(r'\d[\d.,]*', search_text)
    if matches:
        digits = re.sub(r'\D', '', matches[0])
        if digits:
            return int(digits)
    try:
        return int(float(order.amount or 0))
    except (TypeError, ValueError):
        return 0


def _get_ranking_entries(game_id, target_date=None):
    if not game_id:
        return []

    start_at, end_at = _month_range_for_ve(target_date)
    orders = (
        Order.query
        .filter(Order.game_id == game_id)
        .filter(Order.status.in_(['approved', 'completed']))
        .filter(Order.created_at >= start_at, Order.created_at < end_at)
        .order_by(Order.created_at.asc())
        .all()
    )

    grouped = {}
    for order in orders:
        player_id = (order.player_id or '').strip()
        if not player_id:
            continue

        bucket = grouped.setdefault(player_id, {
            'player_id': player_id,
            'nickname': (order.player_nickname or '').strip(),
            'total_units': 0,
            'total_spent': 0.0,
            'last_seen_at': order.created_at,
        })
        if order.player_nickname:
            bucket['nickname'] = (order.player_nickname or '').strip()
        bucket['total_units'] += _extract_order_units(order)
        try:
            bucket['total_spent'] += float(order.amount or 0)
        except (TypeError, ValueError):
            pass
        bucket['last_seen_at'] = order.created_at

    entries = sorted(
        grouped.values(),
        key=lambda item: (-item['total_units'], -item['total_spent'], item['player_id'])
    )

    return entries


def _find_current_position(entries, lookup_identifier):
    lookup_identifier = (lookup_identifier or '').strip()
    if not lookup_identifier:
        return None

    current_index = None
    for index, entry in enumerate(entries):
        if (entry.get('player_id') or '').strip() == lookup_identifier:
            current_index = index
            break

    if current_index is None:
        return None

    entry = entries[current_index]
    current_units = int(entry.get('total_units') or 0)
    higher_entry = entries[current_index - 1] if current_index > 0 else None
    lower_entry = entries[current_index + 1] if current_index + 1 < len(entries) else None

    next_target_units = int(higher_entry.get('total_units') or 0) if higher_entry else current_units
    lower_units = int(lower_entry.get('total_units') or 0) if lower_entry else 0
    missing_units = max(next_target_units - current_units, 0) if higher_entry else 0

    progress_percent = 100
    if higher_entry:
        denominator = max(next_target_units - lower_units, 1)
        numerator = max(current_units - lower_units, 0)
        progress_percent = max(6, min(99, int(round((numerator / denominator) * 100))))

    return {
        'position': current_index + 1,
        'masked_player_id': _mask_player_id(entry.get('player_id')),
        'masked_nickname': _mask_nickname(entry.get('nickname')),
        'total_units': current_units,
        'next_target_units': next_target_units,
        'missing_units': missing_units,
        'progress_percent': progress_percent,
        'is_top_ten': current_index < 10,
    }


def _resolve_ranking_lookup_identifier(game):
    if not has_request_context():
        return ''

    lookup_game_id = request.args.get('lookup_game_id', type=int)
    lookup_identifier = (request.args.get('lookup_identifier') or '').strip()
    if lookup_game_id and lookup_identifier and game and game.id == lookup_game_id:
        return lookup_identifier

    if current_user.is_authenticated and current_user.__class__.__name__ == 'User':
        user_scope = (getattr(current_user, 'account_scope', '') or '').strip()
        expected_scope = f'game:{game.id}' if game else ''
        if user_scope == expected_scope:
            return (getattr(current_user, 'account_identifier', '') or '').strip()

    return ''


def _get_previous_archive_payload(ranking_key):
    if not _is_admin_ranking_view():
        return None

    today = now_ve().date()
    previous_month_last_day = today.replace(day=1) - timedelta(days=1)
    archive_rows = (
        RankingArchive.query
        .filter_by(
            ranking_key=ranking_key,
            year=previous_month_last_day.year,
            month=previous_month_last_day.month,
        )
        .order_by(RankingArchive.position.asc())
        .all()
    )
    if not archive_rows:
        return None

    return {
        'label': f'{previous_month_last_day.month:02d}/{previous_month_last_day.year}',
        'entries': [
            {
                'position': row.position,
                'masked_player_id': row.masked_player_id,
                'masked_nickname': row.masked_nickname,
                'total_units': row.total_units,
                'prize_label': row.prize_label,
            }
            for row in archive_rows
        ],
    }


def _build_ranking_payload(ranking_key, target_date=None):
    config = RANKING_DEFS[ranking_key]
    game = _resolve_ranking_game(config)
    enabled = _is_ranking_enabled(config, game, target_date=target_date)

    payload = {
        'key': ranking_key,
        'label': config['label'],
        'units_label': config['units_label'],
        'enabled': enabled and game is not None,
        'game_id': game.id if game else None,
        'game_name': game.name if game else config['label'],
        'entries': [],
        'reward_ladder': [],
        'current_position': None,
        'previous_winners': None,
        'prize_note': 'Se muestran 10 posiciones, pero solo el Top 5 recibe premio al cerrar el mes.',
    }

    if not payload['enabled']:
        return payload

    entries = _get_ranking_entries(game.id, target_date=target_date)
    rewards = config.get('rewards') or []
    for reward_index, reward_value in enumerate(rewards, start=1):
        payload['reward_ladder'].append({
            'position': reward_index,
            'reward_value': reward_value,
            'reward_label': str(reward_value),
        })

    for index, entry in enumerate(entries[:10], start=1):
        reward_value = rewards[index - 1] if index - 1 < len(rewards) else None
        payload['entries'].append({
            'position': index,
            'masked_player_id': _mask_player_id(entry['player_id']),
            'masked_nickname': _mask_nickname(entry['nickname']),
            'total_units': entry['total_units'],
            'prize_label': str(reward_value) if reward_value is not None else 'Sin premio',
            'reward_value': reward_value,
            'is_prize_eligible': index <= 5,
        })

    lookup_identifier = _resolve_ranking_lookup_identifier(game)
    payload['current_position'] = _find_current_position(entries, lookup_identifier)
    payload['previous_winners'] = _get_previous_archive_payload(ranking_key)

    return payload


def archive_previous_month_rankings_if_needed():
    today = now_ve().date()
    previous_month_last_day = today.replace(day=1) - timedelta(days=1)
    year = previous_month_last_day.year
    month = previous_month_last_day.month

    for ranking_key, config in RANKING_DEFS.items():
        enabled = _get_setting_val(config['enabled_key'], '0') == '1'
        if not enabled:
            continue

        existing = RankingArchive.query.filter_by(ranking_key=ranking_key, year=year, month=month).first()
        if existing:
            continue

        payload = _build_ranking_payload(ranking_key, target_date=previous_month_last_day)
        if not payload['enabled'] or not payload['entries']:
            continue

        for entry in payload['entries'][:5]:
            db.session.add(RankingArchive(
                ranking_key=ranking_key,
                year=year,
                month=month,
                position=entry['position'],
                game_name=payload['game_name'],
                masked_player_id=entry['masked_player_id'],
                masked_nickname=entry['masked_nickname'],
                total_units=entry['total_units'],
                prize_label=entry['prize_label'],
            ))

    db.session.commit()


@main_bp.route('/')
def index():
    cat_slug = request.args.get('cat', 'juegos')
    category = Category.query.filter_by(slug=cat_slug).first()
    if not category:
        category = Category.query.filter_by(slug='juegos').first()

    games = _get_games_for_category(category)
    categories = Category.query.all()
    payment_methods = (
        PaymentMethod.query
        .filter_by(is_active=True)
        .order_by(PaymentMethod.sort_order)
        .all()
    )
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    usd_rate = float(usd_rate_setting.value) if usd_rate_setting else 0.0
    default_pkg_setting = Setting.query.filter_by(key='default_auto_package_id').first()
    default_package_id = int(default_pkg_setting.value) if default_pkg_setting else None
    return render_template(
        'index.html',
        games=games,
        categories=categories,
        active_category=category,
        payment_methods=payment_methods,
        usd_rate=usd_rate,
        default_package_id=default_package_id,
    )


@main_bp.route('/api/games')
def api_games():
    cat_slug = request.args.get('category', 'juegos')
    category = Category.query.filter_by(slug=cat_slug).first()
    if not category:
        return jsonify({'games': []})
    games = _get_games_for_category(category)
    return jsonify({'games': [g.to_dict() for g in games]})


@main_bp.route('/api/packages/<int:game_id>')
def api_packages(game_id):
    game = Game.query.filter_by(id=game_id, is_active=True).first_or_404()
    packages = game.packages.filter_by(is_active=True).all()

    # Include verification config for this game
    active_login_game_id = _get_setting_val('active_login_game_id', '')
    bs_package_id = _get_setting_val('bs_package_id', '')
    scrape_enabled = current_app.config.get('SCRAPE_ENABLED', True)

    game_dict = game.to_dict()
    game_dict['scrape_enabled'] = scrape_enabled
    game_dict['is_ff_verify'] = (active_login_game_id and str(game.id) == str(active_login_game_id))
    game_dict['is_bs_verify'] = (bs_package_id and str(game.id) == str(bs_package_id))

    # Determine which packages have an active auto-mapping (revendedores)
    pkg_ids = [p.id for p in packages]
    auto_mapped_ids = set(
        m.store_package_id for m in
        RevendedoresItemMapping.query.filter(
            RevendedoresItemMapping.store_package_id.in_(pkg_ids),
            RevendedoresItemMapping.active == True,
            RevendedoresItemMapping.auto_enabled == True,
        ).all()
    ) if pkg_ids else set()

    is_tarjetas = (game.category and game.category.slug == 'tarjetas')

    pkg_list = []
    for p in packages:
        d = p.to_dict()
        d['is_auto'] = bool(p.is_automated or is_tarjetas or (p.id in auto_mapped_ids))
        pkg_list.append(d)

    return jsonify({
        'game': game_dict,
        'packages': pkg_list,
    })


@main_bp.route('/api/discounts')
def get_discounts():
    """API para obtener códigos de descuento válidos"""
    from ..models import Discount, Affiliate

    discounts = Discount.query.filter_by(is_active=True).all()
    payload = {
        d.code.upper(): {
            'discount_type': d.discount_type,
            'discount_value': str(d.discount_value),
            'min_amount': str(d.min_amount) if d.min_amount else None,
            'max_discount': str(d.max_discount) if d.max_discount else None,
            'source': 'discount',
        } for d in discounts
    }

    affiliates = Affiliate.query.filter_by(is_active=True).all()
    for affiliate in affiliates:
        code = (affiliate.code or '').strip().upper()
        if not code or code in payload:
            continue

        rate = float(affiliate.client_discount_rate or 0)
        if rate <= 0:
            rate = float(affiliate.commission_rate or 0)
        if rate <= 0:
            continue

        payload[code] = {
            'discount_type': 'percentage',
            'discount_value': str(rate),
            'min_amount': None,
            'max_discount': None,
            'source': 'affiliate',
        }

    return jsonify({
        'discounts': payload
    })


@main_bp.route('/api/rankings')
def api_rankings():
    archive_previous_month_rankings_if_needed()
    rankings = [_build_ranking_payload('free_fire'), _build_ranking_payload('blood_strike')]
    return jsonify({'rankings': rankings})


def _get_games_for_category(category):
    if not category:
        return []
    games = (
        Game.query
        .filter_by(category_id=category.id, is_active=True)
        .order_by(Game.is_automated.desc(), Game.position.asc(), Game.name.asc())
        .all()
    )
    # Si no hay juegos en esta categoría, traer de otras en orden (excepto wallet)
    if not games and category.slug != 'wallet':
        slugs_order = ['juegos', 'tarjetas', 'wallet']
        for slug in slugs_order:
            if slug == category.slug:
                continue
            other_cat = Category.query.filter_by(slug=slug).first()
            if other_cat:
                fallback = (
                    Game.query
                    .filter_by(category_id=other_cat.id, is_active=True)
                    .order_by(Game.is_automated.desc(), Game.position.asc(), Game.name.asc())
                    .all()
                )
                if fallback:
                    games = fallback
                    break
    return games
