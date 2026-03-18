"""
Player ID verification routes.
Replicated from Inefablestore – identical endpoint paths, parameters, and error responses.
"""
import os
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required
from ..models import db, Game, Setting
from ..player_verify import (
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


# ── Free Fire verification (same path as Inefable: /store/player/verify) ─────

@verify_bp.route('/store/player/verify')
def store_player_verify():
    if not current_app.config.get('SCRAPE_ENABLED', True):
        return jsonify({"ok": False, "error": "Verificación deshabilitada"}), 403

    uid = (request.args.get("uid") or "").strip()
    gid_raw = (request.args.get("gid") or "").strip()
    if not uid or not uid.isdigit():
        return jsonify({"ok": False, "error": "ID inválido"}), 400
    if not gid_raw or not gid_raw.isdigit():
        return jsonify({"ok": False, "error": "Juego inválido"}), 400

    active_login_game_id = (_get_setting("active_login_game_id", "") or "").strip()
    if not active_login_game_id or active_login_game_id != gid_raw:
        return jsonify({"ok": False, "error": "Verificación no disponible para este juego"}), 403

    game = Game.query.get(int(gid_raw))
    if not game or not game.is_active:
        return jsonify({"ok": False, "error": "Juego no encontrado"}), 404

    cache_key = f"ffmania:{uid}"
    cached = _player_cache_get(cache_key)
    if cached is not None:
        if not cached:
            return jsonify({"ok": False, "error": "ID no encontrado"}), 404
        return jsonify({"ok": True, "uid": uid, "nick": cached, "cached": True})

    try:
        nick = scrape_ffmania_nick(uid)
    except Exception:
        return jsonify({"ok": False, "error": "No se pudo verificar el ID"}), 502

    # Cache both hits and misses for short time to reduce external traffic
    _player_cache_set(cache_key, nick, ttl_seconds=600)
    if not nick:
        return jsonify({"ok": False, "error": "ID no encontrado"}), 404
    return jsonify({"ok": True, "uid": uid, "nick": nick, "cached": False})


# ── Blood Strike verification (same path: /store/player/verify/bloodstrike) ──

@verify_bp.route('/store/player/verify/bloodstrike')
def store_player_verify_bloodstrike():
    if not current_app.config.get('SCRAPE_ENABLED', True):
        return jsonify({"ok": False, "error": "Verificación deshabilitada"}), 403

    uid = (request.args.get("uid") or "").strip()
    gid_raw = (request.args.get("gid") or "").strip()
    if not uid or not uid.isdigit():
        return jsonify({"ok": False, "error": "ID inválido"}), 400

    bs_package_id = (_get_setting("bs_package_id", "") or "").strip()
    if not bs_package_id or bs_package_id != gid_raw:
        return jsonify({"ok": False, "error": "Verificación no disponible para este juego"}), 403

    cache_key = f"bs_smileone:{uid}"
    cached = _player_cache_get(cache_key)
    if cached is not None:
        if not cached:
            return jsonify({"ok": False, "error": "ID no encontrado"}), 404
        return jsonify({"ok": True, "uid": uid, "nick": cached, "cached": True})

    bs_server_id = (_get_setting("bs_server_id", "-1") or "-1").strip()
    nick = scrape_smileone_bloodstrike_nick(uid, bs_package_id, bs_server_id)

    _player_cache_set(cache_key, nick, ttl_seconds=600)
    if not nick:
        return jsonify({"ok": False, "error": "ID no encontrado"}), 404
    return jsonify({"ok": True, "uid": uid, "nick": nick, "cached": False})


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
