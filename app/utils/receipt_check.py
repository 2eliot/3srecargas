"""Comprobantes que llegan al chat de soporte: ¿el pago ya está en la tienda? ¿se
pagó a NUESTROS datos?

El asistente de IA no decide esto "de memoria": el servidor lee el comprobante,
busca la referencia en las órdenes y compara a quién se pagó (teléfono, cédula,
correo) con los datos de la tienda, que salen de dos lugares:

- los métodos de pago activos (teléfono, cédula/RIF, correo y Pay ID), y
- las instrucciones que el dueño escribe en Admin → Soporte → Asistente IA
  (cualquier teléfono, cédula, correo o cuenta que ponga ahí cuenta como "nuestro").

El resultado se guarda en el mensaje (`ai_note`) y es lo único que se le cuenta al
modelo: referencia, monto y los veredictos. Nunca el texto libre de la imagen
(nombres, bancos...), porque una foto podría traer frases pensadas para engañar a la IA.
"""

import json
import re
from datetime import datetime, timedelta

from sqlalchemy import or_

from ..models import Order, PaymentMethod
from .payment_verification import counted_remainder_references, same_bank_reference

REFERENCE_MIN_DIGITS = 6
NOTE_VERSION = 1
KINDS = ('pago_movil', 'transferencia', 'zelle', 'binance', 'otro')
KIND_NAMES = {
    'pago_movil': 'Pago Móvil', 'transferencia': 'transferencia', 'zelle': 'Zelle',
    'binance': 'Binance', 'otro': 'otro método',
}
# Cuánto vale un comprobante para invitar a crear la orden: una foto vieja ya no sirve.
INVITE_MAX_AGE_MINUTES = 90
# El asistente solo consulta pedidos de los últimos 60 días (support_ai.search_orders_for_chat).
SEARCHABLE_DAYS = 60

_NUMBER_RUN = re.compile(r'\d[\d .\-/]*\d')
_EMAIL = re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+')


def _digits(value):
    return re.sub(r'\D', '', str(value or ''))


# ─── ¿Ya está en el sistema? ────────────────────────────────────────────────

def reference_orders(reference, limit=3, since=None):
    """Órdenes que ya tienen esa referencia (de cualquier fecha y estado, incluida
    la del pago restante): si hay una, ese pago ya está registrado. Se usa la misma
    regla de la tienda para decir que dos referencias son la misma operación
    (iguales, o una es el final de la otra), porque el cliente a veces escribe solo
    los últimos dígitos y su comprobante trae el número completo. Con `since` solo
    cuentan las órdenes creadas desde esa fecha."""
    digits = _digits(reference)
    if len(digits) < REFERENCE_MIN_DIGITS:
        return []
    tail = digits[-REFERENCE_MIN_DIGITS:]
    query = Order.query.filter(or_(Order.payment_reference_last5 == tail,
                                   Order.payment_reference.contains(tail),
                                   Order.ai_extracted_reference.contains(tail),
                                   Order.remainder_reference.contains(tail),
                                   Order.remainder_references.contains(tail)))
    if since:
        query = query.filter(Order.created_at >= since)
    candidates = query.order_by(Order.id.desc()).limit(50).all()
    found = []
    for order in candidates:
        refs = [order.payment_reference, order.ai_extracted_reference, order.remainder_reference]
        refs += counted_remainder_references(order)
        if any(same_bank_reference(digits, ref) for ref in refs if ref):
            found.append(order)
            if len(found) >= limit:
                break
    return found


# ─── ¿Es a nuestros datos? ──────────────────────────────────────────────────

def _number_candidates(text):
    """Números de 6 a 25 dígitos que aparecen en un texto, sin importar cómo estén
    escritos ("0414-123 4567", "J-12345678-9"...). Se prueba cada tramo completo y
    también cada pedazo separado por espacios, para no pegar números vecinos."""
    found = set()
    for run in _NUMBER_RUN.findall(str(text or '')):
        for piece in [run] + run.split():
            digits = _digits(piece)
            if 6 <= len(digits) <= 25:
                found.add(digits)
    return found


def _phone_key(digits):
    """Teléfono comparable: sin 58 ni 0 delante y con los últimos 10 dígitos."""
    digits = _digits(digits)
    if digits.startswith('58') and len(digits) >= 12:
        digits = digits[2:]
    digits = digits.lstrip('0')
    return digits[-10:] if len(digits) > 10 else digits


def _masked_phone_fits(shown, our_key):
    """Algunos bancos tapan el medio del teléfono ("0414***4567" se lee como 04144567).
    Encaja si empieza y termina igual que el nuestro. Con un número tapado que no
    encaja no se acusa a nadie (puede ser un error de lectura): queda sin comprobar."""
    ours = '0' + our_key
    return (ours.startswith(shown[:4]) and ours.endswith(shown[4:])) or ours.endswith(shown)


def _same_id(a, b):
    """Cédula o RIF. El RIF lleva un dígito verificador al final: 12345678 y
    123456789 son la misma persona."""
    if len(a) < 6 or len(b) < 6:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(long_) - len(short) == 1 and long_.startswith(short)


def our_identifiers(instructions=''):
    """Teléfonos, cédulas, cuentas y correos de la tienda."""
    numbers, emails = set(), set()
    for method in PaymentMethod.query.filter_by(is_active=True).all():
        for value in (method.contact_phone, method.id_number, method.pay_id, method.contact_email):
            numbers |= _number_candidates(value)
            emails |= {e.lower() for e in _EMAIL.findall(str(value or ''))}
    numbers |= _number_candidates(instructions)
    emails |= {e.lower() for e in _EMAIL.findall(str(instructions or ''))}
    return {'numbers': numbers, 'emails': emails}


def compare_with_our_data(details, instructions=''):
    """Compara a quién se pagó con los datos de la tienda.

    Devuelve {verdict, matched, mismatched}. `verdict`:
    - 'ours': coincide al menos un dato y ninguno contradice.
    - 'not_ours': el comprobante trae datos del destinatario y ninguno es nuestro.
    - 'unknown': no se leyó a quién se pagó, o los datos se contradicen entre sí,
      o la tienda no tiene datos con los que comparar. Lo revisa una persona.

    Solo teléfono, cédula y correo pueden contradecir: son datos que se leen y
    comparan sin ambigüedad. Un número de cuenta u otro código que no esté en la
    lista simplemente no cuenta (podría ser una cuenta que el dueño no anotó)."""
    ours = our_identifiers(instructions)
    numbers, emails = ours['numbers'], ours['emails']
    matched, mismatched = [], []

    raw_phone = _digits(details.get('payee_phone'))
    phone = _phone_key(raw_phone)
    pool_phones = {_phone_key(n) for n in numbers if len(_phone_key(n)) >= 10}
    if len(phone) >= 10 and pool_phones:
        (matched if phone in pool_phones else mismatched).append('phone')
    elif len(raw_phone) >= 7 and any(_masked_phone_fits(raw_phone, p) for p in pool_phones):
        matched.append('phone')   # número tapado por el banco ("0414***4567") que sí concuerda con el nuestro

    cedula = _digits(details.get('payee_id'))
    pool_ids = {n for n in numbers if 6 <= len(n) <= 10}
    if len(cedula) >= 6 and pool_ids:
        (matched if any(_same_id(cedula, n) for n in pool_ids) else mismatched).append('id')

    account = str(details.get('payee_account') or '').strip().lower()
    if account:
        if '@' in account:
            found = _EMAIL.findall(account)
            if found and emails:
                (matched if found[0] in emails else mismatched).append('email')
        else:
            digits = _digits(account)
            if len(digits) >= 6 and digits in numbers:
                matched.append('account')

    if matched and not mismatched:
        verdict = 'ours'
    elif mismatched and not matched:
        verdict = 'not_ours'
    else:
        verdict = 'unknown'
    return {'verdict': verdict, 'matched': matched, 'mismatched': mismatched}


# ─── La nota del mensaje ────────────────────────────────────────────────────

def analyze(result, instructions=''):
    """`result` es lo que devuelve `extract_receipt_details_from_image_*`.
    Devuelve la nota lista para guardar (dict)."""
    if not result or not result.get('ok'):
        return {'v': NOTE_VERSION, 'read': False}
    details = result.get('details') or {}
    reference = _digits(details.get('reference') or result.get('reference'))
    comparison = compare_with_our_data(details, instructions)
    orders = reference_orders(reference)
    cutoff = datetime.utcnow() - timedelta(days=SEARCHABLE_DAYS)
    kind = details.get('kind') if details.get('kind') in KINDS else 'otro'
    amount = details.get('amount')
    return {
        'v': NOTE_VERSION, 'read': True,
        'reference': reference if len(reference) >= REFERENCE_MIN_DIGITS else '',
        'amount': round(float(amount), 2) if isinstance(amount, (int, float)) else None,
        'currency': details.get('currency') if details.get('currency') in ('bs', 'usd') else '',
        'kind': kind,
        'in_system': bool(orders),
        # Ya hay orden, pero tan vieja que el asistente no la puede consultar: la ve una persona.
        'old_order': bool(orders) and all(o.created_at and o.created_at < cutoff for o in orders),
        'ours': comparison['verdict'],
        'matched': comparison['matched'],
        'mismatched': comparison['mismatched'],
    }


def encode_note(note):
    return json.dumps(note, ensure_ascii=False, separators=(',', ':'))


def decode_note(text):
    """La nota guardada como dict, o None si es un texto simple (notas de antes)."""
    try:
        data = json.loads(text or '')
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get('v') == NOTE_VERSION else None


def note_for_model(text):
    """Cómo se le cuenta la nota al asistente. Solo datos ya validados."""
    note = decode_note(text)
    if note is None:
        return text or ''
    if not note.get('read'):
        return ('(El sistema no pudo leer el comprobante: la imagen no se ve bien o no es un comprobante de '
                'pago. Pídele una foto más clara o que escriba la referencia.)')

    parts = []
    parts.append(f"referencia {note['reference']}" if note.get('reference') else 'la referencia no se alcanzó a leer')
    if note.get('amount') is not None:
        sign = '$' if note.get('currency') == 'usd' else 'Bs ' if note.get('currency') == 'bs' else ''
        parts.append(f"monto {sign}{('%.2f' % note['amount']).rstrip('0').rstrip('.')}")
    parts.append(KIND_NAMES.get(note.get('kind'), 'otro método'))
    if note.get('reference'):
        parts.append('¿ya hay una orden con esa referencia en la tienda? ' + (
            ('SÍ, pero es una orden de hace más de 60 días: no la puedes consultar, pásalo a una persona'
             if note.get('old_order') else 'SÍ') if note.get('in_system') else 'NO'))
    parts.append('¿fue pagado a los datos de la tienda? ' + {
        'ours': 'SÍ',
        'not_ours': 'NO, el destinatario no coincide con nuestros datos',
        'unknown': 'NO SE PUDO COMPROBAR (no se leyó bien el destinatario o los datos se contradicen)',
    }.get(note.get('ours'), 'NO SE PUDO COMPROBAR'))
    return '(Lectura automática del comprobante: ' + '; '.join(parts) + '.)'


def can_invite(note):
    """Solo se invita a crear la orden con un comprobante que se leyó, tiene
    referencia, todavía no está registrado y se pagó a nuestros datos."""
    return bool(note and note.get('read') and note.get('reference')
                and not note.get('in_system') and note.get('ours') == 'ours')
