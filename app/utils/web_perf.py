"""Compresión de respuestas y caché larga de archivos estáticos (la web se usa sobre todo en celulares de gama baja con datos lentos).

Antes el sitio no comprimía nada (la portada pesaba 2,3 MB, de los cuales ~0,5 MB era texto sin comprimir) y los archivos CSS/JS se
revalidaban en cada visita aunque su dirección ya lleva `?v=<fecha>`. Aquí:

* Se comprime con gzip el HTML, CSS, JS, JSON y SVG (la portada baja de 120 KB a ~27 KB; todo el CSS+JS propio, de 884 KB a ~197 KB).
  Si nginx ya comprime, no se pierde nada: ve la cabecera Content-Encoding y no vuelve a comprimir.
* En producción (no en modo desarrollo, para que los cambios de CSS se vean al instante) los archivos de /static/ pedidos con `?v=`
  llevan caché de 1 año (cambian de dirección cuando cambian de contenido) y las imágenes subidas, 30 días solo en el navegador.

Se apaga con APP_GZIP=0 en el entorno.
"""
import gzip
import os
import threading

from flask import request

COMPRESSIBLE_TYPES = {
    'text/html', 'text/css', 'text/plain', 'text/xml', 'application/xml', 'text/javascript', 'application/javascript',
    'application/json', 'application/manifest+json', 'image/svg+xml',
}
MIN_BYTES = 1024
MAX_BYTES = 4 * 1024 * 1024
GZIP_LEVEL = 6
STATIC_CACHE_MAX_BYTES = 12 * 1024 * 1024

_static_gzip = {}
_static_gzip_bytes = 0
_static_lock = threading.Lock()


def _weaken_etag(response):
    etag = response.headers.get('ETag')
    if etag and not etag.startswith('W/'):
        response.headers['ETag'] = 'W/' + etag


def _gzip_static(key, data):
    """Los estáticos no cambian entre visitas: se comprimen una sola vez por archivo y versión y se guardan en memoria."""
    global _static_gzip_bytes
    with _static_lock:
        hit = _static_gzip.get(key)
    if hit is not None:
        return hit
    packed = gzip.compress(data, compresslevel=9, mtime=0)
    with _static_lock:
        if _static_gzip_bytes + len(packed) <= STATIC_CACHE_MAX_BYTES:
            _static_gzip[key] = packed
            _static_gzip_bytes += len(packed)
    return packed


def init_web_perf(app):
    gzip_on = (os.environ.get('APP_GZIP') or '1').strip() != '0'

    @app.after_request
    def _compress_and_cache(response):
        path = request.path
        is_static = path.startswith('/static/')

        # ── Caché larga de estáticos (solo producción) ──
        if is_static and not app.debug and response.status_code in (200, 304) and 'Cache-Control' in response.headers:
            if request.args.get('v'):
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            elif path.startswith('/static/uploads/'):
                response.headers['Cache-Control'] = 'private, max-age=2592000'

        # ── Compresión ──
        if not gzip_on or response.status_code != 200 or request.method == 'HEAD':
            return response
        if response.mimetype not in COMPRESSIBLE_TYPES or 'Content-Encoding' in response.headers:
            return response
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return response
        if request.headers.get('Range') or response.headers.get('Content-Range'):
            return response
        try:
            if response.is_streamed and not response.direct_passthrough:
                return response    # respuesta en partes (no es el caso normal): no se toca
            length = response.content_length
            if length is not None and (length < MIN_BYTES or length > MAX_BYTES):
                return response
            response.direct_passthrough = False
            data = response.get_data()
            if len(data) < MIN_BYTES or len(data) > MAX_BYTES:
                response.set_data(data)
                return response
            if is_static:
                packed = _gzip_static((path, response.headers.get('ETag') or len(data)), data)
            else:
                packed = gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)
            response.set_data(packed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = str(len(packed))
            _weaken_etag(response)
            vary = response.headers.get('Vary')
            if not vary:
                response.headers['Vary'] = 'Accept-Encoding'
            elif 'accept-encoding' not in vary.lower():
                response.headers['Vary'] = vary + ', Accept-Encoding'
        except Exception:
            # Si algo sale raro, la respuesta se entrega sin comprimir (nunca debe romper una página por esto).
            pass
        return response
