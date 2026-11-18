"""Ubicación aproximada de una IP: país, ciudad y proveedor.

Se usa en el detalle de la orden y en las IPs bloqueadas del admin. La
consulta se hace una vez y se guarda en `ip_geolocations` para no llamar a la
API en cada página. La ciudad de un VPS/VPN es la del servidor, no la de la
persona; se muestra como referencia, no como prueba.
"""
import ipaddress
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from ..models import IpGeoCache, db

API_SINGLE = 'http://ip-api.com/json/'
API_BATCH = 'http://ip-api.com/batch'
FIELDS = 'status,message,country,countryCode,regionName,city,isp,org,as,query'
CACHE_DAYS = 30
FAIL_CACHE_HOURS = 6
REQUEST_TIMEOUT = 2.5
BATCH_LIMIT = 100


def _now():
    return datetime.utcnow()


def _flag(country_code):
    code = (country_code or '').strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ''
    return ''.join(chr(0x1F1E6 + (ord(ch) - ord('A'))) for ch in code)


def _label(country='', region='', city='', isp='', org='', status=''):
    if status == 'private':
        return 'Red local / IP privada'
    if status == 'fail' or status == 'error':
        return 'No se pudo ubicar'
    place = ', '.join(part for part in (city, region, country) if part)
    provider = isp or org
    if place and provider:
        return f'{place} — {provider}'
    return place or provider or ''


def _private_label(ip):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ''
    if address.is_loopback:
        return 'Red local'
    if address.is_private or address.is_link_local or address.is_reserved or address.is_unspecified:
        return 'IP privada'
    return ''


def _row_to_dict(row):
    data = {
        'ip': row.ip,
        'status': row.status or '',
        'country': row.country or '',
        'country_code': row.country_code or '',
        'region': row.region or '',
        'city': row.city or '',
        'isp': row.isp or '',
        'org': row.org or '',
        'asn': row.asn or '',
    }
    data['flag'] = _flag(data['country_code'])
    data['label'] = _label(
        data['country'], data['region'], data['city'], data['isp'], data['org'], data['status']
    )
    return data


def _fresh(row):
    if not row or not row.updated_at:
        return False
    hours = FAIL_CACHE_HOURS if (row.status or '') in {'fail', 'error'} else CACHE_DAYS * 24
    return row.updated_at > _now() - timedelta(hours=hours)


def _save(ip, data, commit=True):
    status = (data.get('status') or '').strip()
    try:
        row = IpGeoCache.query.filter_by(ip=ip).first()
        if not row:
            row = IpGeoCache(ip=ip)
            db.session.add(row)
        row.status = status[:20]
        row.country = (data.get('country') or '')[:100] or None
        row.country_code = (data.get('countryCode') or '')[:5] or None
        row.region = (data.get('regionName') or '')[:100] or None
        row.city = (data.get('city') or '')[:100] or None
        row.isp = (data.get('isp') or '')[:200] or None
        row.org = (data.get('org') or '')[:200] or None
        row.asn = (data.get('as') or '')[:100] or None
        row.updated_at = _now()
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return row
    except Exception:
        db.session.rollback()
        return None


def _error_dict(ip):
    return {
        'ip': ip, 'status': 'error', 'country': '', 'country_code': '',
        'region': '', 'city': '', 'isp': '', 'org': '', 'asn': '',
        'flag': '', 'label': 'No se pudo ubicar',
    }


def get_geo(ip, allow_remote=True):
    """Diccionario con país/ciudad/proveedor, o None si la IP está vacía."""
    ip = (ip or '').strip()
    if not ip:
        return None
    private = _private_label(ip)
    if private:
        return {
            'ip': ip, 'status': 'private', 'country': '', 'country_code': '',
            'region': '', 'city': '', 'isp': '', 'org': '', 'asn': '',
            'flag': '', 'label': private,
        }
    try:
        row = IpGeoCache.query.filter_by(ip=ip).first()
    except Exception:
        db.session.rollback()
        row = None
    if row and _fresh(row):
        return _row_to_dict(row)
    if not allow_remote:
        return _row_to_dict(row) if row else None
    try:
        resp = requests.get(
            API_SINGLE + quote(ip, safe=':.'),
            params={'fields': FIELDS},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json() if resp.status_code == 200 else {}
    except Exception:
        data = {}
    if not isinstance(data, dict) or data.get('status') != 'success':
        _save(ip, {'status': 'fail'})
        return _error_dict(ip)
    saved = _save(ip, data)
    return _row_to_dict(saved) if saved else _error_dict(ip)


def get_geo_many(ips, allow_remote=True, budget=4.0):
    """{ip: diccionario} para varias IPs, con una sola consulta en lote.

    `allow_remote=False` solo devuelve lo que ya está en la caché (para listas
    largas, sin frenar la página).
    """
    wanted = []
    for raw in ips or []:
        ip = (raw or '').strip()
        if ip and ip not in wanted:
            wanted.append(ip)
    result = {}
    missing = []
    if not wanted:
        return result
    try:
        rows = IpGeoCache.query.filter(IpGeoCache.ip.in_(wanted)).all()
        by_ip = {row.ip: row for row in rows}
    except Exception:
        db.session.rollback()
        by_ip = {}
    for ip in wanted:
        private = _private_label(ip)
        if private:
            result[ip] = {
                'ip': ip, 'status': 'private', 'country': '', 'country_code': '',
                'region': '', 'city': '', 'isp': '', 'org': '', 'asn': '',
                'flag': '', 'label': private,
            }
            continue
        row = by_ip.get(ip)
        if row and _fresh(row):
            result[ip] = _row_to_dict(row)
        elif row and not allow_remote:
            result[ip] = _row_to_dict(row)
        else:
            missing.append(ip)
    if not allow_remote or not missing:
        return result
    pending = missing[:BATCH_LIMIT]
    try:
        resp = requests.post(
            API_BATCH,
            params={'fields': FIELDS},
            json=pending,
            timeout=budget,
        )
        items = resp.json() if resp.status_code == 200 else []
    except Exception:
        items = []
    found = {}
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            query_ip = (item.get('query') or '').strip()
            if not query_ip:
                continue
            found[query_ip] = item
    for ip in pending:
        item = found.get(ip)
        if item and item.get('status') == 'success':
            saved = _save(ip, item, commit=False)
            result[ip] = _row_to_dict(saved) if saved else _row_to_dict_from_item(ip, item)
        else:
            _save(ip, {'status': 'fail'}, commit=False)
            result[ip] = _error_dict(ip)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return result


def _row_to_dict_from_item(ip, item):
    data = {
        'ip': ip,
        'status': item.get('status') or '',
        'country': item.get('country') or '',
        'country_code': item.get('countryCode') or '',
        'region': item.get('regionName') or '',
        'city': item.get('city') or '',
        'isp': item.get('isp') or '',
        'org': item.get('org') or '',
        'asn': item.get('as') or '',
    }
    data['flag'] = _flag(data['country_code'])
    data['label'] = _label(
        data['country'], data['region'], data['city'], data['isp'], data['org'], data['status']
    )
    return data


def get_cached_geo_many(ips):
    return get_geo_many(ips, allow_remote=False)
