from flask import Blueprint, render_template, jsonify, request, session, redirect, url_for, current_app
from ..models import db, Game, Package, Category, PaymentMethod, Setting

main_bp = Blueprint('main_bp', __name__)


def _get_setting_val(key, default=''):
    row = Setting.query.filter_by(key=key).first()
    return row.value if row else default


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

    return jsonify({
        'game': game_dict,
        'packages': [p.to_dict() for p in packages],
    })


@main_bp.route('/api/discounts')
def get_discounts():
    """API para obtener códigos de descuento válidos"""
    from ..models import Discount
    discounts = Discount.query.filter_by(is_active=True).all()
    return jsonify({
        'discounts': {
            d.code.upper(): {
                'discount_type': d.discount_type,
                'discount_value': str(d.discount_value),
                'min_amount': str(d.min_amount) if d.min_amount else None,
                'max_discount': str(d.max_discount) if d.max_discount else None,
            } for d in discounts
        }
    })


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
