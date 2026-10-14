"""Chequeo barato para los archivos que suben los CLIENTES (comprobantes de pago y adjuntos del chat).

Esas subidas solo se validaban por la extensión (.png, .jpg...). Un archivo llamado "foto.png" que por dentro es HTML/SVG/XML con
código no es una foto: se rechaza. Una imagen o un video de verdad JAMÁS empieza con "<", así que esta prueba no deja fuera a nadie
legítimo (no abre ni re-guarda la imagen: el comprobante se conserva tal cual lo mandó el cliente).
"""


def looks_like_markup(file_storage):
    """True si el contenido del archivo empieza como HTML, SVG, XML o <script>."""
    stream = getattr(file_storage, 'stream', None)
    if stream is None:
        return False
    try:
        position = stream.tell()
    except (OSError, ValueError):
        position = 0
    try:
        head = stream.read(1024)
    except (OSError, ValueError):
        return False
    finally:
        try:
            stream.seek(position)
        except (OSError, ValueError):
            pass
    head = head.lstrip(b'\xef\xbb\xbf\xff\xfe\x00 \t\r\n\x0b\x0c')
    return head[:1] == b'<'
