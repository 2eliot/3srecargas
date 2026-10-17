import re
import threading
import time
from datetime import date, datetime, timedelta
from uuid import uuid4

from flask import Blueprint, render_template, jsonify, request, session, redirect, url_for, current_app, has_request_context
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..models import (
    db, Game, Package, Category, PaymentMethod, Setting,
    Order, RankingArchive, RankingBoard, User, UserProfile,
)
from ..utils.availability import (
    format_hour,
    get_manual_schedule,
    manual_service_is_open,
    package_ids_with_auto_mapping,
    package_is_closed_now,
    package_is_out_of_stock,
)
from ..utils.points import package_points_preview
from ..utils.order_units import extract_order_units
from ..utils import ranking_boards as RB
from ..utils.timezone import now_ve, ve_day_start_utc_naive, today_ve_str
from ..utils.catalog_version import site_version
from ..utils.locks import acquire_lock, check_rate_limit, client_ip, release_lock
from ..utils.push_notifications import get_vapid_public_key, subscribe as push_subscribe, unsubscribe as push_unsubscribe
from ..utils.site_notifications import build_feed

main_bp = Blueprint('main_bp', __name__)

NOTIF_FEED_PER_MINUTE = 30
RANKING_LOOKUPS_PER_MINUTE = 20


@main_bp.route('/api/notificaciones', methods=['POST'])
def notifications_feed():
    """Avisos de la campana del header: las novedades del admin para todos y,
    para las órdenes que ese navegador dice haber hecho, el estado de cada una
    y los mensajes del admin. Solo lectura y con la entrada acotada (hasta 15
    números de orden); responde siempre sin caché."""
    if request.content_length and request.content_length > 4096:
        return jsonify({'ok': False, 'items': []}), 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        payload = {}
    # El número de orden es la "llave" de la orden: quien lo manda consulta su estado. Sin tope, alguien podría
    # probar números en cadena (15 por petición). Un navegador normal pregunta una vez por minuto.
    # El chat de soporte de este navegador (aviso "Soporte te respondió"): llega en el cuerpo, en la cabecera o en la cookie del chat.
    support_token = str(payload.get('support_token') or request.headers.get('X-Support-Token') or request.cookies.get('support_token') or '').strip()[:64]
    if (payload.get('orders') or support_token) and not check_rate_limit(f'notif_feed:{client_ip()}', NOTIF_FEED_PER_MINUTE, 60):
        return jsonify({'ok': False, 'items': []}), 429
    response = jsonify({'ok': True, 'items': build_feed(payload.get('orders'), support_token)})
    response.headers['Cache-Control'] = 'no-store'
    return response


def _get_setting_val(key, default=''):
    row = Setting.query.filter_by(key=key).first()
    return row.value if row else default


# Los rankings mensuales (Free Fire, Blood Strike, Cuentas, Niveles y los que cree el dueño) viven en la base de datos (RankingBoard) y se editan
# en Admin → Dinámicas → Ranking. La lógica (premios, cierre de mes) está en utils/ranking_boards.py; aquí solo se arma lo que ve la tienda.
_month_range_for_ve = RB.month_range_for_ve
_mask_player_id = RB.mask_player_id
_mask_nickname = RB.mask_nickname
_extract_order_units = extract_order_units
_get_ranking_entries = RB.game_entries
_get_ranking_entries_cached = RB.game_entries_cached
_ranking_entries_cache = RB._entries_cache
_find_current_position = RB.find_current_position


def _is_admin_ranking_view():
    if not has_request_context():
        return False
    return current_user.is_authenticated and current_user.__class__.__name__ == 'AdminUser'


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


def _serialize_archive_entry(row, include_private=False, emails=None):
    payload = {
        'position': row.position,
        'total_units': row.total_units,
        'prize_label': row.prize_label,
    }

    if include_private:
        payload['player_id'] = (row.player_id or row.masked_player_id or '').strip() or '----'
        payload['nickname'] = (row.nickname or row.masked_nickname or '').strip() or 'Jugador***'
        payload['prize_detail'] = row.prize_detail or ''
        if row.user_id:
            payload['email'] = (emails or {}).get(row.user_id, '')
    else:
        payload['masked_player_id'] = row.masked_player_id
        payload['masked_nickname'] = row.masked_nickname

    return payload


def _get_archive_history_payload(ranking_key, include_private=False, limit=None):
    archive_rows = (
        RankingArchive.query
        .filter_by(ranking_key=ranking_key)
        .order_by(RankingArchive.year.desc(), RankingArchive.month.desc(), RankingArchive.position.asc())
        .all()
    )
    if not archive_rows:
        return []

    emails = {}
    if include_private:
        user_ids = {row.user_id for row in archive_rows if row.user_id}
        if user_ids:
            emails = {user.id: (user.email or user.account_identifier or '') for user in User.query.filter(User.id.in_(user_ids)).all()}

    periods = []
    current_group = None
    current_group_key = None

    for row in archive_rows:
        group_key = (row.year, row.month)
        if current_group_key != group_key:
            if limit and len(periods) >= limit:
                break
            current_group_key = group_key
            current_group = {
                'label': f'{row.month:02d}/{row.year}',
                'year': row.year,
                'month': row.month,
                'game_name': row.game_name,
                'entries': [],
            }
            periods.append(current_group)

        current_group['entries'].append(_serialize_archive_entry(row, include_private=include_private, emails=emails))

    return periods


def backfill_ranking_archives_if_needed():
    """Archivos viejos de juegos que se guardaron sin el ID ni el nombre: se completan con el ranking de ese mes (una sola vez)."""
    incomplete_rows = (
        RankingArchive.query
        .filter(RankingArchive.user_id.is_(None))
        .filter(
            or_(
                RankingArchive.player_id.is_(None),
                RankingArchive.player_id == '',
                RankingArchive.nickname.is_(None),
                RankingArchive.nickname == '',
            )
        )
        .order_by(RankingArchive.year.desc(), RankingArchive.month.desc())
        .all()
    )
    if not incomplete_rows:
        return

    grouped = {}
    for row in incomplete_rows:
        grouped.setdefault((row.ranking_key, row.year, row.month, row.game_name), []).append(row)

    updated = False
    for (ranking_key, year, month, game_name), rows in grouped.items():
        board = RankingBoard.query.filter_by(key=ranking_key, kind='game').first()
        if board is None:
            continue

        game = Game.query.filter_by(name=game_name).first() or board.game
        if not game:
            continue

        entries = _get_ranking_entries(game.id, target_date=date(year, month, 1))
        if not entries:
            continue

        for row in rows:
            index = int(row.position or 0) - 1
            if index < 0 or index >= len(entries):
                continue
            entry = entries[index]
            player_id = (entry.get('player_id') or '').strip()
            nickname = (entry.get('nickname') or '').strip()
            row.player_id = player_id or row.player_id
            row.nickname = nickname or row.nickname
            row.masked_player_id = _mask_player_id(player_id)
            row.masked_nickname = _mask_nickname(nickname)
            updated = True

    if updated:
        db.session.commit()


def _get_previous_archive_payload(ranking_key):
    if not _is_admin_ranking_view():
        return None

    history = _get_archive_history_payload(ranking_key, include_private=True, limit=1)
    return history[0] if history else None


def _build_ranking_payload(board, target_date=None):
    """Ranking de IDs de un juego (el de siempre) tal como lo ve la tienda."""
    game = board.game
    places = RB.places(board)

    payload = {
        'key': board.key,
        'kind': 'game',
        'label': board.name,
        'units_label': RB.units_label(board),
        'enabled': game is not None,
        'game_id': game.id if game else None,
        'game_name': game.name if game else board.name,
        'entries': [],
        'reward_ladder': [],
        'current_position': None,
        'previous_winners': None,
        'prize_note': f'Se muestran {RB.PUBLIC_SIZE} posiciones, pero solo el Top {places} recibe premio al cerrar el mes.',
    }

    if game is None:
        return payload

    entries = _get_ranking_entries_cached(game.id, target_date=target_date)
    payload['reward_ladder'] = RB.ladder(board)
    prizes = RB.prize_map(board)

    # El ranking mensual de juegos es solo por ID: no muestra fotos de perfil ni enlaces a perfiles (las cuentas son independientes de los IDs).
    for index, entry in enumerate(entries[:RB.PUBLIC_SIZE], start=1):
        label = RB.prize_label(prizes.get(index)) if index <= places else ''
        payload['entries'].append({
            'position': index,
            'masked_player_id': _mask_player_id(entry['player_id']),
            'masked_nickname': _mask_nickname(entry['nickname']),
            'total_units': entry['total_units'],
            'prize_label': label or 'Sin premio',
            'reward_value': label or None,
            'is_prize_eligible': index <= places,
        })

    lookup_identifier = _resolve_ranking_lookup_identifier(game)
    payload['current_position'] = _find_current_position(entries, lookup_identifier)
    payload['previous_winners'] = _get_previous_archive_payload(board.key)

    return payload


def _public_account_winners(board, limit=3):
    """Ganadores del mes pasado de un ranking de cuentas o de niveles: {'label', 'entries': [{'position', 'name', 'handle', 'prize_label'}]} o None.
    Solo cuentas con perfil visible (las que se pueden abrir)."""
    from ..utils import profile_social as PS

    key = PS.prev_month_key(PS.month_key_now())
    rows = (
        RankingArchive.query
        .filter_by(ranking_key=board.key, year=int(key[:4]), month=int(key[5:7]))
        .filter(RankingArchive.user_id.isnot(None))
        .order_by(RankingArchive.position.asc())
        .all()
    )
    entries = []
    for row in rows:
        user = db.session.get(User, row.user_id)
        profile = UserProfile.query.filter_by(user_id=user.id).first() if user is not None else None
        if user is None or not PS._eligible(profile, user) or not user.has_chosen_username:
            continue
        label = row.prize_label if (row.prize_label and row.prize_label != 'Sin premio') else ''
        entries.append({'position': row.position, 'name': PS._shown_name(user, profile), 'handle': user.username, 'prize_label': label})
        if len(entries) >= limit:
            break
    return {'label': PS.month_label(key), 'entries': entries} if entries else None


def _build_account_board_payload(board):
    """Ranking de CUENTAS (las que más recargan) o de NIVELES, tal como lo ve la tienda. Los premios se entregan al cerrar el mes."""
    from ..utils import profile_social as PS
    from ..utils.auth_accounts import is_account_user

    levels = board.kind == 'levels'
    payload = {
        'key': board.key, 'kind': 'levels' if levels else 'accounts', 'label': board.name, 'units_label': RB.units_label(board), 'enabled': True,
        'entries': [], 'reward_ladder': RB.ladder(board), 'current_position': None, 'previous_winners': None,
        'viewer': {'logged_in': False, 'is_account': False},
    }
    try:
        PS.ensure_fresh(PS.STATS_FALLBACK_MINUTES)   # el recálculo normal lo hace el proceso de fondo; aquí solo la red de seguridad
        rows = PS.level_ranking() if levels else PS.account_month_ranking()
        viewer = current_user if (has_request_context() and current_user.is_authenticated) else None
        for r in rows:
            entry = {'position': r['position'], 'name': r['name'], 'handle': r['handle'], 'avatar': r['avatar'], 'total_units': r['total_units']}
            if levels:
                entry['level'] = r['level']
            payload['entries'].append(entry)
        payload['viewer'] = {'logged_in': viewer is not None, 'is_account': is_account_user(viewer)}
        if viewer is not None:
            payload['current_position'] = PS.level_my_position(viewer) if levels else PS.account_my_position(viewer)
        payload['previous_winners'] = _public_account_winners(board)
    except Exception:
        db.session.rollback()
    return payload


def get_public_rankings_payload(target_date=None):
    payloads = []
    for board in RB.enabled_boards():
        if board.kind == 'game':
            payload = _build_ranking_payload(board, target_date=target_date)
            if not payload['enabled']:
                continue
        else:
            payload = _build_account_board_payload(board)
        payloads.append(payload)
    return payloads


def has_visible_public_rankings(target_date=None):
    """Solo decide si se muestra el botón de Ranking en el header.

    Corre en cada página vía context processor, así que no puede pagar el costo de `get_public_rankings_payload` (que carga todas las órdenes del
    mes): basta con saber si hay algún ranking encendido (los de juego, con su juego elegido)."""
    return RB.visible_boards_exist()


def archive_previous_month_rankings_if_needed():
    """Al empezar un mes cierra el anterior de cada ranking encendido: archiva a los ganadores y entrega los premios automáticos."""
    backfill_ranking_archives_if_needed()
    RB.close_previous_month_if_needed()


@main_bp.route('/')
def index():
    cat_slug = request.args.get('cat', 'juegos')
    category = Category.query.filter_by(slug=cat_slug).first()
    if not category:
        category = Category.query.filter_by(slug='juegos').first()

    # Enlace directo a un producto: /?juego=free-fire abre la tienda con ese
    # juego ya seleccionado. La categoría se toma del juego (no del ?cat=)
    # para que la tarjeta esté sí o sí en la grilla que se pinta; si el slug
    # no existe o el juego está apagado, se entra a la tienda normal.
    game_slug = (request.args.get('juego') or '').strip().lower()
    preselect_game = None
    if game_slug:
        preselect_game = Game.query.filter_by(slug=game_slug, is_active=True).first()
        if preselect_game and preselect_game.category:
            category = preselect_game.category

    # Enlace de compra desde el chat de soporte: /?juego=<slug>&paquete=<id>
    # &jugador=<id>&zona=<zona> deja el paquete elegido y el ID escrito. Todo
    # llega por la URL, así que se valida: el paquete debe ser activo y de ese
    # juego, y los IDs solo pueden llevar caracteres de un ID normal.
    preselect_package_id = None
    preselect_player_id = ''
    preselect_zone_id = ''
    if preselect_game:
        package_id = request.args.get('paquete', type=int)
        if package_id and Package.query.filter_by(id=package_id, game_id=preselect_game.id, is_active=True).first():
            preselect_package_id = package_id
            preselect_player_id = _clean_link_id(request.args.get('jugador'))
            preselect_zone_id = _clean_link_id(request.args.get('zona'))

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

    from ..utils.binance_pay import is_binance_auto_enabled
    binance_auto_enabled = is_binance_auto_enabled(current_app._get_current_object())
    binance_wallet = _get_setting_val('binance_wallet_address', '')

    video_method = (_get_setting_val('checkout_payment_video_method', '') or '').strip().lower()
    video_file = (_get_setting_val('checkout_payment_video_file', '') or '').strip()
    checkout_payment_video = {
        'enabled': bool(video_method and video_file),
        'method': video_method,
        'file': video_file,
        'title': _get_setting_val('checkout_payment_video_title', '') or 'Mira cómo pagar con este método',
        'message': _get_setting_val('checkout_payment_video_message', '') or '',
        'cta': _get_setting_val('checkout_payment_video_cta', '') or 'Entendido, continuar',
    }

    promo_banner = (_get_setting_val('promo_banner_image', '') or '').strip()
    promo_banner_link = (_get_setting_val('promo_banner_link', '') or '').strip()
    # Copia liviana del banner (si se mueve y ya se hizo): el modo liviano usa esa, más chica, en vez del original
    from ..utils import banner_lite
    promo_banner_lite = banner_lite.existing_lite(promo_banner) if promo_banner else ''

    community_popup_enabled = (_get_setting_val('community_popup_enabled', '') or '') == '1'
    try:
        community_popup_interval_hours = max(1, int(float(_get_setting_val('community_popup_interval_hours', '3') or '3')))
    except ValueError:
        community_popup_interval_hours = 3
    community_popup_whatsapp_url = (
        (_get_setting_val('community_popup_whatsapp_url', '') or '').strip()
        or (_get_setting_val('support_whatsapp', '') or '').strip()
        or 'https://wa.me/19543789224'
    )

    # El aviso de Wallet anuncia este horario en texto plano: se calcula acá
    # en vez de hardcodearlo en la plantilla para que nunca vuelva a
    # desincronizarse del horario real que configura el admin en Ajustes
    # (eso pasó antes: el popup decía 10am-10pm mientras el horario real era
    # otro).
    manual_schedule = get_manual_schedule()
    manual_open_label = format_hour(manual_schedule['open_hour'])
    manual_close_label = format_hour(manual_schedule['close_hour'])

    return render_template(
        'index.html',
        games=games,
        categories=categories,
        active_category=category,
        payment_methods=payment_methods,
        usd_rate=usd_rate,
        default_package_id=default_package_id,
        binance_auto_enabled=binance_auto_enabled,
        binance_wallet=binance_wallet,
        checkout_payment_video=checkout_payment_video,
        promo_banner=promo_banner,
        promo_banner_link=promo_banner_link,
        promo_banner_lite=promo_banner_lite,
        manual_open_label=manual_open_label,
        manual_close_label=manual_close_label,
        community_popup_enabled=community_popup_enabled,
        community_popup_interval_hours=community_popup_interval_hours,
        community_popup_whatsapp_url=community_popup_whatsapp_url,
        preselect_game_slug=(preselect_game.slug if preselect_game else ''),
        preselect_package_id=preselect_package_id,
        preselect_player_id=preselect_player_id,
        preselect_zone_id=preselect_zone_id,
    )


_LINK_ID_RE = re.compile(r'^[A-Za-z0-9_.@+-]{1,100}$')


def _clean_link_id(raw):
    value = (raw or '').strip()
    return value if _LINK_ID_RE.match(value) else ''


def page_day_expired():
    """True si la página que llama se abrió otro día (hora de Venezuela).

    La tienda vive en una sola página y la gente la deja abierta días. En
    vez de perseguir cada caso, la regla es simple: la web solo vale el día
    en que se abrió. main.js manda el día en `X-Page-Day`; si no coincide
    con hoy (o no viene, que es lo que pasa con las páginas anteriores a
    este cambio) el servidor no le da paquetes y le pide recargar.
    """
    day = request.headers.get('X-Page-Day')
    if day is None:
        return True            # no la manda: página anterior a este cambio
    day = day.strip()
    if not day:
        return False           # la manda vacía: no se sabe, mejor no caducar
    return day != today_ve_str()


def _expired_page_payload(game, usd_rate):
    """Respuesta para una página de otro día: en vez de paquetes, una sola
    tarjeta apagada que dice que recargue. Las páginas nuevas ni la pintan
    (ven `page_expired` y recargan solas); las viejas la muestran tal cual."""
    game_dict = game.to_dict()
    game_dict.update({
        'scrape_enabled': False,
        'is_ff_verify': False,
        'is_bs_verify': False,
        'requires_manual_login_popup': False,
        'requires_wallet_notice': False,
        'manual_schedule': None,
    })
    return {
        'game': game_dict,
        'packages': [{
            'id': 0,
            'name': '⚠️ Esta página lleva abierta desde otro día. Recárgala para ver los paquetes y precios de hoy.',
            'description': '',
            'price': '0',
            'usd_price': None,
            'bs_price': None,
            'image': None,
            'is_auto': True,
            'out_of_stock': True,
            'closed_now': False,
            'points': 0,
        }],
        'usd_rate_bs': usd_rate,
        'page_expired': True,
        'page_day': today_ve_str(),
    }


def _no_store(response):
    """Las respuestas de la tienda nunca se guardan en caché.

    Sin esto, una pestaña restaurada desde caché podía volver a pintar
    paquetes y precios de días atrás.
    """
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@main_bp.route('/api/games')
def api_games():
    cat_slug = request.args.get('category', 'juegos')
    category = Category.query.filter_by(slug=cat_slug).first()
    if not category:
        return jsonify({'games': []})
    games = _get_games_for_category(category)
    return _no_store(jsonify({'games': [g.to_dict() for g in games]}))


@main_bp.route('/api/packages/<int:game_id>')
def api_packages(game_id):
    game = Game.query.filter_by(id=game_id, is_active=True).first_or_404()
    if page_day_expired():
        usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
        try:
            expired_rate = float(usd_rate_setting.value) if usd_rate_setting else 0.0
        except (TypeError, ValueError):
            expired_rate = 0.0
        return _no_store(jsonify(_expired_page_payload(game, expired_rate)))
    packages = game.packages.filter_by(is_active=True).all()

    # Include verification config for this game
    active_login_game_id = _get_setting_val('active_login_game_id', '')
    bs_package_id = _get_setting_val('bs_package_id', '')
    scrape_enabled = current_app.config.get('SCRAPE_ENABLED', True)

    game_dict = game.to_dict()
    game_dict['scrape_enabled'] = scrape_enabled
    from .verify import free_fire_verify_enabled
    game_dict['is_ff_verify'] = (active_login_game_id and str(game.id) == str(active_login_game_id) and free_fire_verify_enabled())
    game_dict['is_bs_verify'] = (bs_package_id and str(game.id) == str(bs_package_id))

    # Determine which packages have an active auto-mapping (revendedores)
    pkg_ids = [p.id for p in packages]
    auto_mapped_ids = package_ids_with_auto_mapping(pkg_ids)

    category_slug = (game.category.slug if game.category else '').lower()
    is_tarjetas = (category_slug == 'tarjetas')
    is_wallet = (category_slug == 'wallet')
    has_stock_delivery = any(bool(p.is_automated and int(p.pin_count or 0) > 0) for p in packages)

    manual_schedule = get_manual_schedule()
    manual_open_now = manual_service_is_open(schedule=manual_schedule)

    # Las tarjetas y las wallet no otorgan puntos (award_points_for_order las
    # descarta), así que tampoco deben prometerlos en la tarjeta del paquete.
    points_eligible = not is_tarjetas and not is_wallet

    pkg_list = []
    for p in packages:
        d = p.to_dict()
        d['is_auto'] = bool(p.is_automated or is_tarjetas or (p.id in auto_mapped_ids))
        # Sin stock de verdad = paquete automatizado por PINs, sin PINs
        # libres, y sin un mapeo de Revendedores que sirva de respaldo. Si
        # tiene mapeo, igual se puede completar aunque el stock esté en 0.
        # La misma regla la aplica el checkout antes de aceptar la compra.
        d['out_of_stock'] = package_is_out_of_stock(
            p, auto_mapped_ids=auto_mapped_ids, is_tarjetas=is_tarjetas
        )
        # Los paquetes que recarga un admin a mano se apagan fuera del
        # horario de atención: si nadie los puede procesar, es mejor no
        # venderlos que dejar al cliente reclamando de madrugada.
        d['closed_now'] = package_is_closed_now(
            p, auto_mapped_ids=auto_mapped_ids, is_tarjetas=is_tarjetas,
            schedule=manual_schedule,
        )
        d['points'] = package_points_preview(p) if points_eligible else 0
        pkg_list.append(d)

    game_dict['requires_manual_login_popup'] = bool(
        pkg_list and not is_tarjetas and not is_wallet and not has_stock_delivery and not any(pkg.get('is_auto') for pkg in pkg_list)
    )

    # Zinli, TikTok, Binance y todo lo de Wallet se gestiona a mano: el
    # cliente tiene que saber el plazo y el horario antes de pagar. Es un
    # aviso distinto al de recarga manual (ese pide datos de inicio de
    # sesión), por eso va en su propio flag y no reutiliza el anterior.
    # El dueño puede apagarlo juego por juego (Admin → Juegos → "Mostrar aviso de plazo y horario"); sin dato = encendido.
    game_dict['requires_wallet_notice'] = bool(pkg_list and is_wallet and game.show_wallet_notice is not False)

    # Apartados (Moneda / Pases / Tarjetas y los que el dueño creó): nombre y orden para dibujar las pestañitas.
    from ..utils.package_sections import section_meta
    game_dict['package_sections'] = section_meta()

    # El front lo usa para el badge "CERRADO" y para el texto del aviso.
    game_dict['manual_schedule'] = {
        'open_hour': manual_schedule['open_hour'],
        'close_hour': manual_schedule['close_hour'],
        'open_label': format_hour(manual_schedule['open_hour']),
        'close_label': format_hour(manual_schedule['close_hour']),
        'is_open_now': manual_open_now,
        'force_closed': bool(manual_schedule.get('force_closed')),
    }

    # La tasa viaja con los paquetes para que una pestaña vieja pueda
    # refrescar precios Y tasa con una sola llamada (ver refreshCatalog en
    # main.js): la que trae el HTML es la de cuando se abrió la página.
    usd_rate_setting = Setting.query.filter_by(key='usd_rate_bs').first()
    try:
        usd_rate = float(usd_rate_setting.value) if usd_rate_setting else 0.0
    except (TypeError, ValueError):
        usd_rate = 0.0

    return _no_store(jsonify({
        'game': game_dict,
        'packages': pkg_list,
        'usd_rate_bs': usd_rate,
    }))


# Consultas de código de descuento por IP y por minuto: un cliente real
# prueba uno o dos; sin tope se podrían adivinar códigos por diccionario.
DISCOUNT_LOOKUPS_PER_MINUTE = 20


@main_bp.route('/api/discounts')
def get_discounts():
    """Datos de UN código (de descuento o de afiliado) para que la tienda
    calcule el precio con descuento: /api/discounts?code=XYZ.

    Antes devolvía la lista de TODOS los códigos activos, así que cualquiera
    veía los privados (los de un influencer, los de un solo uso para
    clientes nuevos) y se aplicaba el mejor."""
    from ..models import Discount, Affiliate

    code = (request.args.get('code') or '').strip().upper()
    payload = {}
    if code:
        if not check_rate_limit(f'discount_lookup:{client_ip()}', DISCOUNT_LOOKUPS_PER_MINUTE, 60):
            return _no_store(jsonify({
                'discounts': {}, 'error': 'Demasiados intentos seguidos. Espera un momento.',
            })), 429

        discount = Discount.query.filter_by(code=code, is_active=True).first()
        affiliate = None if discount else Affiliate.query.filter_by(code=code, is_active=True).first()
        if discount and discount.is_valid_for_amount(float(discount.min_amount or 1)):
            payload[code] = {
                'discount_type': discount.discount_type,
                'discount_value': str(discount.discount_value),
                'min_amount': str(discount.min_amount) if discount.min_amount else None,
                'max_discount': str(discount.max_discount) if discount.max_discount else None,
                'source': 'discount',
            }
        elif affiliate:
            rate = float(affiliate.client_discount_rate or 0)
            if rate <= 0:
                rate = float(affiliate.commission_rate or 0)
            if rate > 0:
                payload[code] = {
                    'discount_type': 'percentage',
                    'discount_value': str(rate),
                    'min_amount': None,
                    'max_discount': None,
                    'source': 'affiliate',
                }

    response = jsonify({
        'discounts': payload
    })
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


@main_bp.route('/api/rankings')
def api_rankings():
    # El cierre del mes anterior (con la entrega de premios, que puede tardar minutos) ya NO corre dentro de esta visita: lo hace el
    # proceso de fondo (utils/order_scheduler._run_promos_tick), igual que el sorteo diario.
    # Buscar "mi puesto" por ID dice si ese ID entró al ranking del mes: se limita por IP para que nadie recorra IDs en cadena.
    if (request.args.get('lookup_identifier') or '').strip() and not check_rate_limit(f'rank_lookup:{client_ip()}', RANKING_LOOKUPS_PER_MINUTE, 60):
        return jsonify({'rankings': [], 'error': 'Demasiadas consultas seguidas. Espera un momento.'}), 429
    rankings = get_public_rankings_payload()
    return jsonify({'rankings': rankings})


@main_bp.route('/api/version')
def api_version():
    """Versión desplegada del sitio.

    La consulta el navegador cuando vuelve a una pestaña que llevaba horas
    abierta: si no coincide con la que cargó, esa pestaña está vieja y se
    recarga sola (solo si no hay nada a medio llenar).
    """
    return _no_store(jsonify({
        'version': site_version(),
        'day': today_ve_str(),
        # La página que pregunta ya no es de hoy: que se recargue.
        'reload': page_day_expired(),
    }))


@main_bp.route('/sw.js')
def service_worker():
    # Se sirve desde la raíz (no /static/sw.js) para que el scope por
    # defecto cubra todo el sitio, no solo /static/.
    response = current_app.send_static_file('sw.js')
    response.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@main_bp.route('/manifest.webmanifest')
def web_manifest():
    """Datos de la web instalable como app (nombre, íconos, colores, cómo se abre).
    Se sirve desde la raíz para que el alcance sea todo el sitio."""
    def icon(name, size, purpose='any'):
        return {'src': url_for('static', filename=f'pwa/{name}'), 'sizes': f'{size}x{size}',
                'type': 'image/png', 'purpose': purpose}

    response = jsonify({
        'id': '/',
        'name': '3S Recargas',
        'short_name': '3S Recargas',
        'description': 'Recargas de juegos, tarjetas y wallets en segundos.',
        'lang': 'es',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#000000',
        'theme_color': '#000000',
        'categories': ['shopping', 'games'],
        'icons': [
            icon('icon-192.png', 192),
            icon('icon-512.png', 512),
            icon('icon-maskable-512.png', 512, 'maskable'),
        ],
    })
    response.mimetype = 'application/manifest+json'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@main_bp.route('/push/vapid-public-key')
def push_vapid_public_key():
    return jsonify({'ok': True, 'publicKey': get_vapid_public_key()})


@main_bp.route('/push/subscribe', methods=['POST'])
def push_subscribe_route():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get('endpoint')
    keys = payload.get('keys') or {}
    order_number = (payload.get('order_number') or '').strip()

    order_id = None
    if order_number:
        order = Order.query.filter_by(order_number=order_number).first()
        if order:
            order_id = order.id

    # Token del chat de soporte, si el cliente tiene uno abierto: así el
    # aviso de "te respondieron" llega también a quien escribió sin haber
    # comprado nada, que no tiene ninguna orden a la que engancharse.
    chat_id = None
    support_token = (payload.get('support_token') or '').strip()
    if support_token:
        from ..utils.support import get_client_chat
        chat, _expired = get_client_chat(support_token)
        if chat:
            chat_id = chat.id

    try:
        push_subscribe(endpoint, keys.get('p256dh'), keys.get('auth'),
                       order_id=order_id, chat_id=chat_id)
    except ValueError as exc:
        return jsonify({'ok': False, 'message': str(exc)}), 400

    return jsonify({'ok': True})


@main_bp.route('/push/unsubscribe', methods=['POST'])
def push_unsubscribe_route():
    payload = request.get_json(silent=True) or {}
    push_unsubscribe(payload.get('endpoint'))
    return jsonify({'ok': True})


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
