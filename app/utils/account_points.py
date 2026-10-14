"""Puntos de la tienda del PERFIL (la cuenta).

Regla del dueño (2026-10-14): los puntos de la tienda viven en los IDs (cada recarga por ID los suma a ese ID). Un perfil NO los gana recargando.
Solo le llegan por dos caminos: el premio de un ranking o porque el administrador se los pone a mano (una dinámica de perfil). Como el canje y la
ruleta funcionan por ID, para usarlos el dueño del perfil los PASA a un ID (juego + ID) y desde ahí se gastan como cualquier otro punto.

Todo es atómico: los puntos solo se descuentan si alcanzan en ese instante (una sola operación en la base) y se suman al ID en la misma
transacción, así nadie los duplica ni los pierde a medias.
"""
import re
from datetime import datetime

from sqlalchemy import select, update

from ..models import AccountPoints, AccountPointsLog, Game, db
from .locks import acquire_lock, release_lock

MAX_POINTS_PER_MOVE = 1_000_000
NOTE_MAX = 200
PLAYER_ID_RE = re.compile(r'^[A-Za-z0-9_.@+-]{1,100}$')
KINDS = ('ranking', 'admin', 'id')


def insert_ignore(model, values):
    """INSERT que no falla si la fila ya existe (dos peticiones a la vez): sirve igual en SQLite y en Postgres."""
    if db.engine.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    db.session.execute(insert(model).values(**values).on_conflict_do_nothing())


def balance(user_id):
    """Puntos que tiene hoy el perfil (0 si nunca tuvo)."""
    if not user_id:
        return 0
    value = db.session.execute(select(AccountPoints.balance).where(AccountPoints.user_id == int(user_id))).scalar()
    return int(value or 0)


def _log(user_id, delta, kind, note, game_id=None, player_id=None):
    db.session.add(AccountPointsLog(
        user_id=user_id, delta=int(delta), balance_after=balance(user_id), kind=kind if kind in KINDS else 'admin',
        note=str(note or '')[:NOTE_MAX], game_id=game_id, player_id=(str(player_id)[:100] if player_id else None),
    ))


def credit(user_id, amount, kind='admin', note=''):
    """Suma puntos a la bolsa del perfil. NO hace commit (quien llama decide cuándo). Devuelve el saldo nuevo."""
    amount = int(amount)
    if amount <= 0 or amount > MAX_POINTS_PER_MOVE:
        raise ValueError('La cantidad de puntos no es válida.')
    insert_ignore(AccountPoints, {'user_id': int(user_id), 'balance': 0, 'updated_at': datetime.utcnow()})
    db.session.execute(
        update(AccountPoints).where(AccountPoints.user_id == int(user_id))
        .values(balance=AccountPoints.balance + amount, updated_at=datetime.utcnow())
    )
    _log(int(user_id), amount, kind, note)
    return balance(user_id)


def debit(user_id, amount, kind='admin', note='', game_id=None, player_id=None):
    """Descuenta puntos SOLO si alcanzan en ese instante. NO hace commit. Devuelve True si se descontó."""
    amount = int(amount)
    if amount <= 0 or amount > MAX_POINTS_PER_MOVE:
        raise ValueError('La cantidad de puntos no es válida.')
    done = db.session.execute(
        update(AccountPoints).where(AccountPoints.user_id == int(user_id), AccountPoints.balance >= amount)
        .values(balance=AccountPoints.balance - amount, updated_at=datetime.utcnow())
    ).rowcount
    if done != 1:
        return False
    _log(int(user_id), -amount, kind, note, game_id=game_id, player_id=player_id)
    return True


def recent_log(user_id, limit=10):
    return AccountPointsLog.query.filter_by(user_id=user_id).order_by(AccountPointsLog.id.desc()).limit(limit).all()


def usable_games():
    """Juegos donde los puntos de un ID sirven de verdad (tienen ruleta o canje directo): [{'game_id', 'game_name'}]. Solo ahí se pueden pasar."""
    from .points import get_points_enabled_games, get_points_redeem_enabled_games
    found = {}
    for item in list(get_points_enabled_games()) + list(get_points_redeem_enabled_games()):
        found.setdefault(item['game_id'], item['game_name'])
    return [{'game_id': game_id, 'game_name': name} for game_id, name in found.items()]


def transfer_to_id(user_id, game_id, player_id, amount):
    """Pasa `amount` puntos de la bolsa del perfil al ID (juego + ID). Todo o nada, con commit. Lanza ValueError con un mensaje listo para
    mostrar si algo no procede. Devuelve {'wallet_balance', 'id_balance'}."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        raise ValueError('Escribe cuántos puntos quieres pasar.')
    if amount <= 0:
        raise ValueError('Escribe cuántos puntos quieres pasar.')
    if amount > MAX_POINTS_PER_MOVE:
        raise ValueError('Esa cantidad es demasiado grande.')
    player_id = str(player_id or '').strip()
    if not PLAYER_ID_RE.match(player_id):
        raise ValueError('Ingresa un ID válido.')
    try:
        game_id = int(game_id)
    except (TypeError, ValueError):
        raise ValueError('Elige un juego válido.')
    if game_id not in {item['game_id'] for item in usable_games()}:
        raise ValueError('En este juego todavía no se pueden usar puntos.')
    game = db.session.get(Game, game_id)
    if game is None or not game.is_active:
        raise ValueError('Juego no encontrado.')

    holder = f'wallet-{datetime.utcnow().timestamp()}'
    lock_key = f'account_points:{int(user_id)}'
    if not acquire_lock(lock_key, 15, holder):
        raise ValueError('Tu movimiento anterior todavía se está procesando. Espera un momento.')
    try:
        if not debit(user_id, amount, kind='id', note=f'Pasados al ID {player_id} de {game.name}', game_id=game_id, player_id=player_id):
            db.session.rollback()
            raise ValueError(f'No tienes tantos puntos en tu perfil. Tienes {balance(user_id)}.')
        from .points import credit_player_points
        id_balance = credit_player_points(game_id, player_id, amount)
        db.session.commit()
    except ValueError:
        raise
    except Exception:
        db.session.rollback()
        raise
    finally:
        release_lock(lock_key, holder)
    return {'wallet_balance': balance(user_id), 'id_balance': id_balance}
