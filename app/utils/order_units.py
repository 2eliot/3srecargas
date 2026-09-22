"""Cantidad de unidades (diamantes, oro, etc.) que entregó una orden.

Se extrae del nombre/descripción del paquete (ej. "550 Diamantes" → 550);
si el paquete no trae ningún número, se usa el monto pagado como respaldo.
Es el mismo criterio que ya usaba el Ranking mensual — vive aquí para que
la barra de Recarga Acumulada mida "cuánto recargó" exactamente igual, sin
que cada función mantenga su propia copia de la regex.
"""
import re


def extract_order_units(order):
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
