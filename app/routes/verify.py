"""
Player ID verification routes.
Replicated from Inefablestore – identical endpoint paths, parameters, and error responses.
"""
import os
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required
from ..models import db, Game, Setting
from ..player_verify import (
    VERIFY_NAME_TIMEOUT_S,
    player_lookup_singleflight,
    revendedores_verify_name_nick,
    scrape_ffmania_nick,
    scrape_smileone_bloodstrike_nick,
    _player_cache_get,
    _player_cache_set,
)

verify_bp = Blueprint('verify_bp', __name__)


# ── Helpers to read/write Setting table (mirrors Inefable's get_config_value) ─

def _get_setting(key, default=""):
    row = Setting.query.filter_by(key=key).first()
    return row.value if row else default


def _set_setting(key, value, description=""):
    row = Setting.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        row = Setting(key=key, value=str(value), description=description)
        db.session.add(row)
    db.session.commit()


# ── Verificación compartida ──────────────────────────────────────────────────

def _verify_free_fire(uid, gid_raw):
    """Free Fire contra la API verify-name de Revendedores. (payload, status)."""
    game = Game.query.get(int(gid_raw))
    if not game or not game.is_active:
        return {"ok": False, "error": "Juego no encontrado"}, 404

    cache_key = f"ffid_verify:{uid}"
    cached = _player_cache_get(cache_key)
    if cached is not None:
        if not cached:
            return {"ok": False, "error": "ID no encontrado"}, 404
        return {"ok": True, "uid": uid, "nick": cached, "cached": True}, 200

    # Fuente principal: API verify-name de Revendedores (verificación real
    # contra Hype, mismo sistema que Inefablestore). FFMania quedó solo como
    # red de seguridad si Revendedores/el bot están caídos.
    base_url = current_app.config.get('REVENDEDORES_BASE_URL', '')
    api_key = current_app.config.get('REVENDEDORES_API_KEY', '')
    try:
        # wait_timeout alto: el primer lookup de un ID puede tardar ~40s
        # (Playwright en el bot); los seguidores concurrentes deben esperar
        # el resultado real en vez de recibir None y cachear un 404 falso.
        nick = player_lookup_singleflight(
            cache_key,
            lambda: revendedores_verify_name_nick(uid, base_url, api_key),
            wait_timeout=VERIFY_NAME_TIMEOUT_S + 10,
        )
    except Exception:
        try:
            nick = scrape_ffmania_nick(uid)
        except Exception:
            nick = ""
        if not nick:
            # Con Revendedores caído, el "no encontrado" de FFMania no es
            # confiable (captcha/índice incompleto): no cachear negativo.
            return {"ok": False, "error": "No se pudo verificar el ID"}, 502
        _player_cache_set(cache_key, nick, ttl_seconds=600)
        return {"ok": True, "uid": uid, "nick": nick, "cached": False}, 200

    if not nick:
        # El 404 de Hype es autoritativo (a diferencia de FFMania), pero se
        # cachea corto por si fue una rareza transitoria del bot.
        _player_cache_set(cache_key, nick, ttl_seconds=45)
        return {"ok": False, "error": "ID no encontrado"}, 404
    _player_cache_set(cache_key, nick, ttl_seconds=600)
    return {"ok": True, "uid": uid, "nick": nick, "cached": False}, 200


def _verify_bloodstrike(uid, gid_raw):
    """Blood Strike contra Smile.One. (payload, status)."""
    cache_key = f"bs_smileone:{uid}"
    cached = _player_cache_get(cache_key)
    if cached is not None:
        if not cached:
            return {"ok": False, "error": "ID no encontrado"}, 404
        return {"ok": True, "uid": uid, "nick": cached, "cached": True}, 200

    bs_server_id = (_get_setting("bs_server_id", "-1") or "-1").strip()
    nick = scrape_smileone_bloodstrike_nick(uid, gid_raw, bs_server_id)

    _player_cache_set(cache_key, nick, ttl_seconds=600)
    if not nick:
        return {"ok": False, "error": "ID no encontrado"}, 404
    return {"ok": True, "uid": uid, "nick": nick, "cached": False}, 200


def verify_player_nick(uid, gid_raw, mode='auto'):
    """Verifica un ID de jugador y devuelve (payload, status) listo para JSON.

    Es el mismo camino que usa la tienda; se comparte para que el panel pueda
    verificar el ID de un canje sin duplicar la lógica ni la caché. `mode`
    acota a quién le responde: las rutas públicas atienden solo a su juego
    ('ff' o 'bs') y 'auto' deja que lo decida la configuración, que es lo que
    necesita el admin cuando solo sabe de qué juego era el premio.
    """
    if not current_app.config.get('SCRAPE_ENABLED', True):
        return {"ok": False, "error": "Verificación deshabilitada"}, 403

    uid = (uid or "").strip()
    gid_raw = (gid_raw or "").strip()
    if not uid or not uid.isdigit():
        return {"ok": False, "error": "ID inválido"}, 400
    if not gid_raw or not gid_raw.isdigit():
        return {"ok": False, "error": "Juego inválido"}, 400

    ff_game_id = (_get_setting("active_login_game_id", "") or "").strip()
    bs_game_id = (_get_setting("bs_package_id", "") or "").strip()
    is_ff = mode in ('ff', 'auto') and bool(ff_game_id) and ff_game_id == gid_raw
    is_bs = mode in ('bs', 'auto') and bool(bs_game_id) and bs_game_id == gid_raw

    if is_ff:
        return _verify_free_fire(uid, gid_raw)
    if is_bs:
        return _verify_bloodstrike(uid, gid_raw)
    return {"ok": False, "error": "Verificación no disponible para este juego"}, 403


def verifiable_game_ids():
    """Juegos con verificación de ID configurada (Free Fire / Blood Strike).

    El panel la usa para pintar el botón "Verificar ID" solo donde hay a
    quién preguntarle el nombre.
    """
    if not current_app.config.get('SCRAPE_ENABLED', True):
        return set()
    ids = set()
    for key in ("active_login_game_id", "bs_package_id"):
        val = (_get_setting(key, "") or "").strip()
        if val.isdigit():
            ids.add(int(val))
    return ids


# ── Free Fire verification (same path as Inefable: /store/player/verify) ─────

@verify_bp.route('/store/player/verify')
def store_player_verify():
    payload, status = verify_player_nick(
        request.args.get("uid"), request.args.get("gid"), mode='ff'
    )
    return jsonify(payload), status


# ── Blood Strike verification (same path: /store/player/verify/bloodstrike) ──

@verify_bp.route('/store/player/verify/bloodstrike')
def store_player_verify_bloodstrike():
    payload, status = verify_player_nick(
        request.args.get("uid"), request.args.get("gid"), mode='bs'
    )
    return jsonify(payload), status


# ── Admin config endpoints (mirror Inefable's admin config routes) ───────────

@verify_bp.route('/admin/config/active_login_game', methods=['GET'])
@login_required
def admin_config_active_login_game_get():
    return jsonify({"ok": True, "active_login_game_id": _get_setting("active_login_game_id", "")})


@verify_bp.route('/admin/config/active_login_game', methods=['POST'])
@login_required
def admin_config_active_login_game_set():
    data = request.get_json(silent=True) or {}
    val = (data.get("active_login_game_id") or "").strip()
    _set_setting("active_login_game_id", val, "ID del juego activo para verificación de ID (Free Fire)")
    return jsonify({"ok": True, "active_login_game_id": val})


@verify_bp.route('/admin/config/bs_package_id', methods=['GET'])
@login_required
def admin_config_bs_package_id_get():
    return jsonify({"ok": True, "bs_package_id": _get_setting("bs_package_id", "")})


@verify_bp.route('/admin/config/bs_package_id', methods=['POST'])
@login_required
def admin_config_bs_package_id_set():
    data = request.get_json(silent=True) or {}
    val = (data.get("bs_package_id") or "").strip()
    _set_setting("bs_package_id", val, "ID del paquete Blood Strike para verificación Smile.One")
    return jsonify({"ok": True, "bs_package_id": val})


@verify_bp.route('/admin/config/bs_server_id', methods=['GET'])
@login_required
def admin_config_bs_server_id_get():
    return jsonify({"ok": True, "bs_server_id": _get_setting("bs_server_id", "-1")})


@verify_bp.route('/admin/config/bs_server_id', methods=['POST'])
@login_required
def admin_config_bs_server_id_set():
    data = request.get_json(silent=True) or {}
    val = (data.get("bs_server_id") or "").strip()
    _set_setting("bs_server_id", val, "ID del servidor Blood Strike (-1 si no requiere)")
    return jsonify({"ok": True, "bs_server_id": val})
