"""Perfil PÚBLICO de una cuenta (estilo Discord): página pública, editor, me gusta, seguir y top de perfiles.

`/u/<usuario>` lo ve cualquiera (si el dueño lo dejó público y el administrador no lo ocultó). Solo el dueño lo edita
(`/profile/edit`). La seguridad de la cuenta (correo, contraseña, historial de compras) vive aparte, en `/account`.

Reglas de la tienda: el perfil NO muestra IDs, redes sociales ni comentarios, y los números (diamantes, nivel, puestos)
salen de las compras que hizo la cuenta (utils/profile_social.py). Las cuentas no dependen de ningún ID.
"""
import json

from flask import Blueprint, abort, flash, jsonify, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user
from sqlalchemy import func

from ..models import ProfileAward, ProfileFollow, ProfileLike, ProfileStat, User, db
from ..utils.auth_accounts import ACCOUNT_KINDS, is_account_user
from ..utils.contact_filter import MASK_MESSAGE, mask_contact_info, public_name
from ..utils.locks import check_rate_limit, client_ip
from ..utils.official_profile import admin_return_user, clear_admin_return, is_official_user, visible_official_user
from ..utils import profile_shop as PSHOP
from ..utils import profile_xp as XP
from ..utils.profile_social import (
    BADGES, SEARCH_MAX, SEARCH_MIN, STATS_FALLBACK_MINUTES, SocialError, badge_cards, ensure_fresh, get_thresholds, level_info, network_users, recompute_ranks,
    search_accounts,
    refresh_user, social_counts, target_visible, toggle_follow, toggle_like, top_rows, viewer_gate,
    xp_details_for, top_points_per_dollar,
)
from ..utils.profile_frames import selected_frame_url, shop_frames, usable_frame
from ..utils.profiles import (
    ACCENT_SWATCHES, BACKGROUND_PRESETS, BACKGROUND_VIDEO_MAX_BYTES, BANNER_COLOR_PRESETS,
    BIO_MAX, DISPLAY_NAME_MAX, MAX_PROFILE_IMAGES, MEDIA_KINDS, MEDIA_LABELS, NAME_FONTS, STATUS_MAX, clean_text, delete_media,
    get_or_create_profile, get_profile, is_video_path, load_theme, media_url_path, profile_image_count, reset_profile, save_media, theme_attrs,
    theme_from_form, theme_vars,
)

profiles_bp = Blueprint('profiles_bp', __name__)

EDIT_MAX_ATTEMPTS = 20
EDIT_WINDOW_SECONDS = 10 * 60
MEDIA_FIELDS = tuple(MEDIA_KINDS)  # los 5 tipos de imagen; el campo de archivo y el de "quitar" se llaman igual
TRUTHY = ('1', 'on', 'true')


def _is_admin():
    return current_user.is_authenticated and current_user.__class__.__name__ == 'AdminUser'


def find_public_account(username):
    value = (username or '').strip().lower()
    if not value or len(value) > 40:
        return None
    user = User.query.filter(
        User.account_kind.in_(ACCOUNT_KINDS), func.lower(User.username) == value,
    ).first()
    return user if user is not None and user.has_chosen_username else None


def profile_status(profile):
    """('publico' | 'privado' | 'oculto', texto) — 'oculto' = lo ocultó el administrador."""
    if profile is not None and profile.moderation_hidden:
        return 'oculto', 'Oculto por el administrador'
    if profile is not None and not profile.is_public:
        return 'privado', 'Privado (solo tú lo ves)'
    return 'publico', 'Público'


def _viewer_account():
    return current_user if (current_user.is_authenticated and is_account_user(current_user)) else None


def build_social(user, profile, viewer, owner_view=False, preview=False):
    """Números y partes sociales del perfil. Con "mostrar mis estadísticas" apagado, NADA de eso sale en la página pública
    (ni escondido); la vista previa del editor sí lo trae todo para que el interruptor lo muestre al instante."""
    show_stats = profile is None or profile.show_stats is not False
    include = show_stats or preview
    stat = ProfileStat.query.filter_by(user_id=user.id).first()
    level = level_info(stat, get_thresholds()) if (include and stat) else None
    awards, title = [], ''
    if include:
        rows = ProfileAward.query.filter_by(user_id=user.id).order_by(ProfileAward.awarded_at.desc(), ProfileAward.id.desc()).all()
        awards = [
            {'title': a.title, 'description': a.description or '', 'image': media_url_path(a.image_path), 'date': a.awarded_at}
            for a in rows
        ]
        titled = next((a for a in rows if a.is_title), None)
        title = titled.title if titled else ''
    xp_cfg = XP.get_config()
    xp = XP.level_progress(stat.xp_total or 0, xp_cfg) if (include and stat) else None
    xp_details = {'parts': [], 'missions': []}
    if xp is not None and owner_view and not preview:
        xp_details = xp_details_for(user, xp_cfg)   # solo el dueño ve de dónde salen sus puntos y sus misiones
    social = {
        'counts': social_counts(user.id),
        'show_stats': show_stats,
        'stat': stat if include else None,
        'level': level,
        'xp': xp,
        'xp_rank': stat.xp_rank if (include and stat) else None,
        'xp_parts': xp_details['parts'],
        'missions': xp_details['missions'],
        'badges': badge_cards(user.id) if include else [],
        'awards': awards,
        'title': title,
        # "Cliente desde" = primera compra verificada; si las estadísticas están apagadas (o no hay compras verificadas), la fecha de registro.
        'since': (stat.first_order_at if (include and stat and stat.first_order_at) else user.created_at),
        'since_label': ('Cliente desde' if (include and stat and stat.first_order_at) else 'Miembro desde'),
        'verified': bool(stat and (stat.orders_count or 0) > 0),
        'viewer': None,
    }
    if viewer is not None and not owner_view and not preview:
        try:
            viewer_gate(viewer)
            can, message = True, ''
        except SocialError as exc:
            can, message = False, str(exc)
        social['viewer'] = {
            'can': can, 'message': message,
            'liked': ProfileLike.query.filter_by(from_user_id=viewer.id, to_user_id=user.id).first() is not None,
            'following': ProfileFollow.query.filter_by(follower_id=viewer.id, following_id=user.id).first() is not None,
        }
    return social


def frame_for_view(profile, user, kind):
    """`uploads/...` del marco que se dibuja encima de la foto (kind='avatar') o del banner (kind='banner'): el de la tienda que la cuenta
    eligió (y todavía puede usar) o, si no eligió ninguno, el propio que subió. `*_frame_id == -1` = "sin marco" (aunque tenga uno subido)."""
    if profile is None:
        return ''
    frame_id = profile.avatar_frame_id if kind == 'avatar' else profile.banner_frame_id
    premium = PSHOP.frame_path(profile, kind)       # marco premium de la tienda de puntos que la cuenta tiene puesto
    if premium:
        return media_url_path(premium)
    if frame_id == -1:
        return ''
    shop_url = selected_frame_url(profile, user, kind) if frame_id else ''
    if shop_url:
        return shop_url
    return media_url_path(profile.avatar_frame_path if kind == 'avatar' else profile.banner_frame_path)


def build_view(user, profile, owner_view=False, preview=False, viewer=None):
    """Todo lo que necesita la tarjeta del perfil (la usan la página pública y la vista previa del editor)."""
    theme = load_theme(profile)
    # La regla (sin redes, enlaces, correos, teléfonos ni IDs) también rige al MOSTRAR: un texto guardado antes de la regla, o
    # metido a mano en la base, que la incumpla sale TACHADO con asteriscos (y si del nombre no queda nada, se usa el usuario).
    shown_name = public_name(profile.display_name, user.username) if profile and profile.display_name else user.username
    shown_bio = mask_contact_info(profile.bio)[0] if profile and profile.bio else ''
    shown_status = mask_contact_info(profile.status_text)[0] if profile and profile.status_text else ''
    social = build_social(user, profile, viewer, owner_view=owner_view, preview=preview)
    level_key = social['level']['key'] if social['level'] else None
    banner_frame_url = frame_for_view(profile, user, 'banner')
    # Foto, portada y fondo: lo premium de la tienda de puntos si la cuenta lo tiene puesto; si no, lo propio.
    avatar_path = PSHOP.effective_avatar_path(profile)
    banner_path = PSHOP.effective_banner_path(profile)
    background_path = PSHOP.effective_background_path(profile)
    media = {'background': background_path, 'banner': banner_path,
             'banner_frame': banner_frame_url}   # el marco solo se usa para saber si "Cuánto sobresale" aplica (hay marco puesto)
    return {
        'user': user,
        'profile': profile,
        'theme': theme,
        'style': theme_vars(theme, media, level_key),
        'attrs': theme_attrs(theme, level_key, bool(banner_frame_url)),
        'level_key': level_key,
        'name': shown_name,
        'handle': user.username,
        'bio': shown_bio,
        'status_text': shown_status,
        'avatar': media_url_path(avatar_path) if profile else '',
        'banner': media_url_path(banner_path) if profile else '',
        # el fondo puede ser una imagen o un VIDEO corto: `background` es la imagen (vacía si es video) y `background_video` el video
        'background': media_url_path(background_path) if profile and not is_video_path(background_path) else '',
        'background_video': media_url_path(background_path) if profile and is_video_path(background_path) else '',
        'avatar_frame': frame_for_view(profile, user, 'avatar'),
        'banner_frame': frame_for_view(profile, user, 'banner'),
        'owner_view': owner_view,
        'letter': (shown_name or '?')[:1].upper(),
        'social': social,
        # La cuenta oficial del administrador lleva el verificado y la etiqueta ADMIN junto al nombre.
        'official': is_official_user(user),
    }


@profiles_bp.route('/u/<username>')
def public_profile(username):
    user = find_public_account(username)
    if user is None or not user.is_active:
        abort(404)
    profile = get_profile(user)
    is_owner = current_user.is_authenticated and current_user.__class__.__name__ == 'User' and current_user.id == user.id
    status, status_text = profile_status(profile)
    if status != 'publico' and not (is_owner or _is_admin()):
        abort(404)
    try:
        ensure_fresh(STATS_FALLBACK_MINUTES)   # el recálculo normal lo hace el proceso de fondo; aquí solo la red de seguridad
    except Exception:
        db.session.rollback()
    view = build_view(user, profile, owner_view=is_owner, viewer=_viewer_account())
    return render_template(
        'profiles/public.html', v=view, is_owner=is_owner, is_admin=_is_admin(), status=status, status_text=status_text,
        name_fonts=NAME_FONTS,
    )


@profiles_bp.route('/volver-al-panel', methods=['POST'])
def back_to_admin():
    """"Volver al panel de admin": el administrador que entró al perfil oficial desde su menú regresa a su sesión del panel sin escribir la
    clave. Solo sirve si esta sesión lo anotó al entrar (marca firmada, de hasta 12 h) y quien está dentro ahora es la cuenta oficial."""
    admin = admin_return_user(current_user) if current_user.is_authenticated else None
    if admin is None:
        clear_admin_return()
        return redirect(url_for('main_bp.index'))
    clear_admin_return()
    login_user(admin)
    return redirect(url_for('admin_bp.dashboard'))


# ───────────────────────── Me gusta y seguir ─────────────────────────
def _wants_json():
    return request.headers.get('X-Requested-With') == 'fetch' or request.accept_mimetypes.best == 'application/json'


def _social_action(username, action, key):
    target = find_public_account(username)
    if target is None or not target.is_active:
        abort(404)
    if not target_visible(target, get_profile(target)):
        abort(404)   # perfil privado u oculto: igual que la página, no se reconoce que existe
    try:
        active = action(_viewer_account(), target)
    except SocialError as exc:
        if _wants_json():
            code = 401 if not current_user.is_authenticated else 403
            return jsonify({'ok': False, 'message': str(exc)}), code
        flash(str(exc), 'warning')
        if not current_user.is_authenticated:
            return redirect(url_for('auth_bp.login', next=url_for('profiles_bp.public_profile', username=target.username)))
        return redirect(url_for('profiles_bp.public_profile', username=target.username))
    counts = social_counts(target.id)
    if _wants_json():
        return jsonify({'ok': True, key: active, 'likes': counts['likes'], 'followers': counts['followers'], 'following': counts['following'], 'reputation': counts['reputation']})
    return redirect(url_for('profiles_bp.public_profile', username=target.username))


@profiles_bp.route('/u/<username>/like', methods=['POST'])
def like_profile(username):
    return _social_action(username, toggle_like, 'liked')


@profiles_bp.route('/u/<username>/follow', methods=['POST'])
def follow_profile(username):
    return _social_action(username, toggle_follow, 'is_following')


@profiles_bp.route('/mi-red')
@login_required
def my_network():
    """Las cuentas que me siguen y las que sigo (solo perfiles visibles)."""
    if not is_account_user(current_user):
        return redirect(url_for('auth_bp.login', tab='registro'))
    tab = 'seguidores' if request.args.get('tab') == 'seguidores' else 'siguiendo'
    counts = social_counts(current_user.id)
    return render_template('profiles/network.html', tab=tab, cards=network_users(current_user, tab), counts=counts)


SEARCH_PER_MINUTE = 60


@profiles_bp.route('/perfiles/buscar')
def search_profiles():
    """Buscador de amigos por nombre de usuario (la barra de arriba de los perfiles). Devuelve JSON; con tope por conexión para que nadie recorra
    la lista de usuarios en cadena. Solo salen perfiles públicos (los mismos que abre /u/<usuario>)."""
    def reply(data, status=200):
        response = jsonify(data)
        response.status_code = status
        response.headers['Cache-Control'] = 'no-store'
        return response

    text = (request.args.get('q') or '').strip().lstrip('@')[:SEARCH_MAX]
    if len(text) < SEARCH_MIN:
        return reply({'results': []})
    if not check_rate_limit(f'profile_search:{client_ip()}', SEARCH_PER_MINUTE, 60):
        return reply({'results': [], 'error': 'Vas muy rápido. Espera un momento.'}, 429)
    try:
        return reply({'results': search_accounts(text)})
    except Exception:
        db.session.rollback()
        return reply({'results': [], 'error': 'No se pudo buscar ahora. Intenta de nuevo.'}, 500)


TOP_TABS = (
    ('mes', 'Este mes', 'Puntos de compras de este mes'), ('historico', 'Histórico', 'Puntos de compras desde siempre'),
    ('reputacion', 'Reputación', 'Puntos del perfil'), ('nivel', 'Nivel', 'Nivel y puntos de experiencia de la comunidad'),
)
# Lo que ve el cliente: arriba el período de las compras, abajo qué se mide. "mes" y "historico" son los dos períodos de "Más compras".
TOP_PERIOD_TABS = tuple(t for t in TOP_TABS if t[0] in ('mes', 'historico'))
TOP_KIND_TABS = (('compras', 'Más compras', 'Quién más compró (puntos): este mes o desde siempre'),) + tuple(t for t in TOP_TABS if t[0] in ('reputacion', 'nivel'))


@profiles_bp.route('/perfiles/top')
def top_profiles():
    """Top de perfiles: por recarga del mes, histórico o reputación. Solo entran perfiles públicos que lo permiten."""
    tab = request.args.get('tab') if request.args.get('tab') in {t[0] for t in TOP_TABS} else 'mes'
    partial = request.args.get('partial') == '1'
    if not partial:     # la lista sola (cambio de pestaña) ya viene detrás de una página completa que hizo esta comprobación
        try:
            ensure_fresh(STATS_FALLBACK_MINUTES)
        except Exception:
            db.session.rollback()
    rows, total, page, pages = top_rows(tab, request.args.get('page', type=int) or 1)
    page_numbers = list(range(max(1, page - 2), min(pages, page + 2) + 1))
    if partial:
        response = make_response(render_template('profiles/_top_list.html', tab=tab, rows=rows, total=total, page=page, pages=pages, page_numbers=page_numbers))
        response.headers['Cache-Control'] = 'no-store'
        return response
    active_kind = 'compras' if tab in ('mes', 'historico') else tab
    return render_template(
        'profiles/top.html', tab=tab, tabs=TOP_TABS, period_tabs=TOP_PERIOD_TABS, kind_tabs=TOP_KIND_TABS, active_kind=active_kind,
        rows=rows, total=total, page=page, pages=pages, page_numbers=page_numbers, points_per_dollar=top_points_per_dollar(),
        official_user=visible_official_user(),   # botón "Ver perfil del admin" (solo si ese perfil se puede abrir)
    )


# ───────────────────────── Editor ─────────────────────────
def _editor_context(user, profile):
    view = build_view(user, profile, owner_view=True, preview=True)
    return {
        'v': view,
        'profile': profile,
        'name_max': DISPLAY_NAME_MAX,
        'bio_max': BIO_MAX,
        'status_max': STATUS_MAX,
        'banner_presets': BANNER_COLOR_PRESETS,
        # Marcos: los de la tienda (todos gratis) y el tope de 5 imágenes propias.
        'shop_avatar_frames': shop_frames(user, 'avatar'),
        'shop_banner_frames': shop_frames(user, 'banner'),
        'avatar_frame_choice': _frame_choice(profile, 'avatar'),
        'banner_frame_choice': _frame_choice(profile, 'banner'),
        'own_avatar_frame': media_url_path(profile.avatar_frame_path) if profile else '',
        'own_banner_frame': media_url_path(profile.banner_frame_path) if profile else '',
        # Lo premium que la cuenta consiguió en la tienda de puntos ("Ver mis marcos / fotos / fondos") y lo que tiene puesto. No cuenta en el tope.
        'owned': {kind: PSHOP.owned_for_editor(user.id, kind) for kind in PSHOP.EQUIP_COLUMN},
        'shop_choice': {kind: PSHOP.current_choice(profile, kind) for kind in PSHOP.EQUIP_COLUMN},
        'max_images': MAX_PROFILE_IMAGES,
        'image_count': profile_image_count(profile),
        'image_slots': {kind: bool(profile and (getattr(profile, spec[0]) or '').strip()) for kind, spec in MEDIA_KINDS.items()},
        'background_presets': BACKGROUND_PRESETS,
        'swatches': ACCENT_SWATCHES,
        'name_fonts': list(NAME_FONTS),
        'limits_mb': {kind: spec[1] // (1024 * 1024) for kind, spec in MEDIA_KINDS.items()},
        'background_video_mb': BACKGROUND_VIDEO_MAX_BYTES // (1024 * 1024),
        'status': profile_status(profile),
    }


def _frame_choice(profile, kind):
    """Qué marco tiene puesto la cuenta en el editor: 'none', 'own' (el que subió) o el id (texto) de uno de la tienda."""
    premium = PSHOP.current_choice(profile, 'frame_avatar' if kind == 'avatar' else 'frame_banner')
    if premium:
        return premium
    frame_id = (profile.avatar_frame_id if kind == 'avatar' else profile.banner_frame_id) if profile else None
    if frame_id == -1:
        return 'none'
    if frame_id:
        return str(frame_id)
    own = (profile.avatar_frame_path if kind == 'avatar' else profile.banner_frame_path) if profile else ''
    return 'own' if (own or '').strip() else 'none'


def _apply_frame_choice(profile, user, kind, raw):
    """Guarda el marco elegido en el editor: 'none', 'own' o el id de uno de la tienda que la cuenta pueda usar. Algo inválido no cambia nada."""
    value = (raw or '').strip().lower()
    column = 'avatar_frame_id' if kind == 'avatar' else 'banner_frame_id'
    premium_kind = 'frame_avatar' if kind == 'avatar' else 'frame_banner'
    if PSHOP.parse_choice(value) is not None:
        # Un marco premium de la tienda de puntos: solo si la cuenta lo tiene. Uno que no es suyo no cambia nada.
        if PSHOP.apply_choice(profile, user, premium_kind, value):
            setattr(profile, column, None)
        return
    PSHOP.apply_choice(profile, user, premium_kind, '')    # cualquier otra elección suelta el marco premium
    if value == 'none':
        setattr(profile, column, -1)
    elif value == 'own':
        setattr(profile, column, None)
    elif value.isdigit():
        frame = usable_frame(user, value, kind)
        if frame is not None:
            setattr(profile, column, frame.id)


@profiles_bp.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    if not is_account_user(current_user):
        if current_user.__class__.__name__ == 'AdminUser':
            return redirect(url_for('admin_bp.dashboard'))
        flash('Para tener un perfil público crea una cuenta.', 'warning')
        return redirect(url_for('auth_bp.login', tab='registro'))
    user = current_user
    if not user.has_chosen_username:
        flash('Elige tu nombre de usuario para tener perfil público: sale en su dirección.', 'warning')
        return redirect(url_for('auth_bp.account'))

    profile = get_profile(user)
    if request.method == 'GET':
        return render_template('profiles/edit.html', **_editor_context(user, profile))

    if not check_rate_limit(f'profile_edit:{user.id}', EDIT_MAX_ATTEMPTS, EDIT_WINDOW_SECONDS):
        flash('Demasiados cambios seguidos. Espera unos minutos e intenta de nuevo.', 'danger')
        return redirect(url_for('profiles_bp.edit_profile'))

    profile = get_or_create_profile(user)
    errors = []
    # Regla de la tienda: nada de enlaces, redes sociales, correos, teléfonos ni números de ID en lo que ve el público. Lo que se
    # detecta se TACHA CON ASTERISCOS (el resto del texto sí se guarda) y se avisa.
    masked_fields = []
    profile.display_name, changed = mask_contact_info(clean_text(request.form.get('display_name'), DISPLAY_NAME_MAX))
    if changed:
        masked_fields.append('el nombre')
    profile.bio, changed = mask_contact_info(clean_text(request.form.get('bio'), BIO_MAX, multiline=True))
    if changed:
        masked_fields.append('"Acerca de mí"')
    if 'status_text' in request.form:
        profile.status_text, changed = mask_contact_info(clean_text(request.form.get('status_text'), STATUS_MAX))
        if changed:
            masked_fields.append('la frase de estado')
    profile.is_public = request.form.get('is_public') in TRUTHY
    if request.form.get('__show_fields') is not None:
        profile.show_stats = request.form.get('show_stats') in TRUTHY
        profile.in_rankings = request.form.get('in_rankings') in TRUTHY
    theme = theme_from_form(request.form, load_theme(profile))
    profile.config_json = json.dumps(theme, ensure_ascii=False, separators=(',', ':'))

    background_uploaded = False
    uploads = {kind: request.files.get(kind) for kind in MEDIA_FIELDS}
    has_upload = {kind: up is not None and bool((up.filename or '').strip()) for kind, up in uploads.items()}
    # Tope de imágenes (pedido del dueño): un perfil tiene como máximo MAX_PROFILE_IMAGES imágenes propias en total. Primero se quitan las que
    # el dueño marcó para quitar (así libera lugar para subir otra en el mismo guardado) y después se suben las nuevas hasta llegar al tope.
    for kind in MEDIA_FIELDS:
        column = MEDIA_KINDS[kind][0]
        if not has_upload[kind] and request.form.get(f'remove_{kind}') in TRUTHY:
            delete_media(getattr(profile, column))
            setattr(profile, column, '')
    image_count = profile_image_count(profile)
    for kind in MEDIA_FIELDS:
        if not has_upload[kind]:
            continue
        column = MEDIA_KINDS[kind][0]
        had_image = bool((getattr(profile, column) or '').strip())
        if not had_image and image_count >= MAX_PROFILE_IMAGES:
            errors.append(f'{MEDIA_LABELS[kind]}: tu perfil ya tiene {MAX_PROFILE_IMAGES} imágenes, que es el máximo. Quita una y vuelve a intentarlo.')
            continue
        rel, error = save_media(uploads[kind], kind, user.id)
        if error:
            errors.append(error)
            continue
        delete_media(getattr(profile, column))
        setattr(profile, column, rel)
        if not had_image:
            image_count += 1
        background_uploaded = background_uploaded or kind == 'background'
    # Marco elegido para la foto y para el banner (tienda, propio o ninguno).
    for kind in ('avatar', 'banner'):
        if f'{kind}_frame_choice' in request.form:
            _apply_frame_choice(profile, user, kind, request.form.get(f'{kind}_frame_choice'))
    # Foto de perfil, portada y fondo de la tienda de puntos (solo los que la cuenta tiene).
    for shop_kind, field_name in (('avatar_photo', 'avatar_photo_choice'), ('banner_photo', 'banner_photo_choice'), ('background', 'background_choice')):
        if field_name in request.form:
            picked = PSHOP.apply_choice(profile, user, shop_kind, request.form.get(field_name))
            background_uploaded = background_uploaded or (shop_kind == 'background' and picked)
    if background_uploaded and theme['background']['mode'] in ('gradient', 'color'):
        # Quien sube una imagen de fondo quiere verla: sin esto quedaba guardada pero el fondo seguía siendo el degradado.
        theme['background']['mode'] = 'image'
        profile.config_json = json.dumps(theme, ensure_ascii=False, separators=(',', ':'))
    db.session.commit()
    try:
        refresh_user(user)       # perfil completo suma reputación
        recompute_ranks()        # y "aparecer en rankings" cambia los puestos
    except Exception:
        db.session.rollback()
    for message in errors:
        flash(message, 'danger')
    if masked_fields:
        flash(f'Tachamos con asteriscos algo de {" y de ".join(masked_fields)}. ' + MASK_MESSAGE, 'warning')
    if not errors:
        flash('Perfil guardado.', 'success')
    else:
        flash('El resto de tus cambios sí se guardó.', 'info')
    return redirect(url_for('profiles_bp.edit_profile'))


@profiles_bp.route('/profile/edit/reset', methods=['POST'])
@login_required
def reset_my_profile():
    if not is_account_user(current_user):
        return redirect(url_for('auth_bp.login', tab='registro'))
    profile = get_profile(current_user)
    if profile is not None:
        reset_profile(profile)
        db.session.commit()
    flash('Tu perfil volvió a como empezó.', 'info')
    return redirect(url_for('profiles_bp.edit_profile'))
