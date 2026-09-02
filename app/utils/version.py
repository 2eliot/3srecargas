"""Sello de la versión desplegada del sitio.

Sirve para lo único que importa aquí: que un navegador que dejó la pestaña
abierta ayer pueda darse cuenta de que la tienda ya cambió. Es el mtime más
nuevo de las plantillas, el CSS y el JS, así que:

- es idéntico en los 3 workers de gunicorn (no es un número por proceso),
- solo cambia cuando se despliega de verdad,
- no obliga a acordarse de subir ningún número a mano.

Se calcula una sola vez por proceso: un deploy reinicia gunicorn, y ese
reinicio es justamente lo que lo recalcula.
"""

import os

from flask import current_app

_cache = {}


def build_version():
    cached = _cache.get('v')
    if cached:
        return cached

    app = current_app
    carpetas = [
        (os.path.join(app.root_path, app.template_folder or 'templates'), ('.html',)),
        (os.path.join(app.static_folder or '', 'css'), ('.css',)),
        (os.path.join(app.static_folder or '', 'js'), ('.js',)),
    ]

    reciente = 0
    for raiz, extensiones in carpetas:
        if not raiz or not os.path.isdir(raiz):
            continue
        for carpeta, _subcarpetas, archivos in os.walk(raiz):
            for nombre in archivos:
                if not nombre.endswith(extensiones):
                    continue
                try:
                    marca = int(os.path.getmtime(os.path.join(carpeta, nombre)))
                except OSError:
                    continue
                if marca > reciente:
                    reciente = marca

    _cache['v'] = str(reciente)
    return _cache['v']
