"""Puntos de la tienda de la CUENTA (el perfil).

Regla del dueño (2026-10-16, cambia la del 14 de octubre): los puntos de una recarga quedan donde se hizo la compra. Sin sesión de cuenta, en el ID
(`PlayerPoints`, como siempre). Con la sesión de una cuenta abierta, en la CUENTA (esta bolsa) y no en el ID. Además llegan por el premio de un
ranking o porque el administrador se los pone a mano. Se gastan en la tienda de la cuenta (`/tiendapuntos`, utils/points_shop.py); los puntos de un ID
se gastan por ID (ruleta y canje directo) y ya no se pasan de una bolsa a la otra.

Todo es atómico: los puntos solo se descuentan si alcanzan en ese instante (una sola operación en la base), así nadie los duplica ni los pierde a medias.
"""
from datetime import datetime

from sqlalchemy import select, update

from ..models import AccountPoints, AccountPointsLog, db

MAX_POINTS_PER_MOVE = 1_000_000
NOTE_MAX = 200
# De dónde llegó (o a dónde se fue) cada movimiento: 'compra' = una recarga con la sesión abierta, 'tienda' = un canje en /tiendapuntos,
# 'hordas' = premio de Hordas de Diamantes o partida extra pagada con puntos.
KINDS = ('ranking', 'admin', 'id', 'compra', 'tienda', 'hordas')


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


def credit(user_id, amount, kind='admin', note='', game_id=None, player_id=None):
    """Suma puntos a la bolsa del perfil. NO hace commit (quien llama decide cuándo). Devuelve el saldo nuevo."""
    amount = int(amount)
    if amount <= 0 or amount > MAX_POINTS_PER_MOVE:
        raise ValueError('La cantidad de puntos no es válida.')
    insert_ignore(AccountPoints, {'user_id': int(user_id), 'balance': 0, 'updated_at': datetime.utcnow()})
    db.session.execute(
        update(AccountPoints).where(AccountPoints.user_id == int(user_id))
        .values(balance=AccountPoints.balance + amount, updated_at=datetime.utcnow())
    )
    _log(int(user_id), amount, kind, note, game_id=game_id, player_id=player_id)
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
