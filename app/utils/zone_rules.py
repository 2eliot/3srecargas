"""Formato de la zona (Zone ID) de los juegos que la piden.

En Mobile Legends la Zone ID son los 4 números que salen al lado del ID del jugador, entre paréntesis: en el perfil se ve
`12345678 (1234)` y la zona es `1234`. Allí la zona lleva SOLO números y como máximo 4 (pedido del dueño, 2026-11-12).
Los demás juegos con zona siguen como siempre (solo se exige que no esté vacía).

La tienda, el chat de soporte, la tienda de puntos y el canje de códigos usan estas mismas funciones, y la pantalla recibe el
máximo en `zone_digits` (Game.to_dict) para no dejar escribir otra cosa.
"""
import re

# Juegos cuya zona son solo números: nombre del juego en minúsculas -> máximo de dígitos.
ZONE_DIGIT_GAMES = {'mobile legends': 4}

_IN_PARENS = re.compile(r'\((\d+)\)')
_DIGITS = re.compile(r'[0-9]+')


def zone_label(game):
    return ((getattr(game, 'zone_id_label', '') or '').strip()) or 'Zone ID'


def zone_digits(game):
    """Máximo de dígitos de la zona de este juego, o None si su zona no tiene esa regla."""
    if not game or not getattr(game, 'requires_zone_id', False):
        return None
    name = (getattr(game, 'name', '') or '').strip().lower()
    for game_name, digits in ZONE_DIGIT_GAMES.items():
        if name == game_name or name.startswith(game_name + ' ') or name.startswith(game_name + ':'):
            return digits
    return None


def clean_zone(game, value):
    """La zona tal como se guarda: recortada; en los juegos de solo números, si pegó `12345678 (1234)` se toma lo de los
    paréntesis y se quitan espacios y paréntesis sueltos. Las letras NO se quitan: así zone_problem las rechaza."""
    text = str(value or '').strip()
    if zone_digits(game):
        inside = _IN_PARENS.search(text)
        if inside:
            text = inside.group(1)
        text = re.sub(r'[\s()]', '', text)
    return text


def zone_problem(game, zone):
    """Mensaje para el cliente si la zona no tiene el formato del juego; None si está bien (o si está vacía: eso lo avisa
    cada pantalla con su propio mensaje de "es obligatoria")."""
    digits = zone_digits(game)
    if not digits or not zone:
        return None
    if _DIGITS.fullmatch(zone) and len(zone) <= digits:
        return None
    label = zone_label(game)
    return (f'Tu {label} lleva solo números (máximo {digits}): son los {digits} números que salen al lado de tu ID, '
            f'entre paréntesis. Ej: 12345678 (1234) → tu {label} es 1234.')
