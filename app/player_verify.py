"""
Player ID verification via scraping.
Free Fire: usa el endpoint JSON perfil-free-fire-check-id.php de FFMania
(el patrón viejo /cuenta/{uid}.html quedó como fallback: solo responde para
perfiles ya cacheados por el sitio; IDs nuevos dan 404 ahí).
"""
import re
import time
import urllib.request
import urllib.error
import html as _html
import requests as _requests_lib

# ── In-memory cache ──────────────────────────────────────────────────────────

_PLAYER_SCRAPE_CACHE = {}


def _player_cache_get(key: str):
    try:
        ent = _PLAYER_SCRAPE_CACHE.get(key)
        if not ent:
            return None
        exp = float(ent.get("exp") or 0)
        if exp and time.time() > exp:
            _PLAYER_SCRAPE_CACHE.pop(key, None)
            return None
        return ent.get("val")
    except Exception:
        return None


def _player_cache_set(key: str, val, ttl_seconds: int = 600):
    try:
        _PLAYER_SCRAPE_CACHE[key] = {"val": val, "exp": time.time() + int(ttl_seconds or 0)}
    except Exception:
        pass


# ── Free Fire (FFMania) scraper ──────────────────────────────────────────────

def _ffmania_check_id(uid: str):
    """Consulta el endpoint JSON de FFMania (flujo vigente desde ~2026-08).

    Devuelve (found, nick):
      - (True, nick)  → jugador encontrado
      - (False, "")   → la API confirmó que el ID no existe
      - (None, "")    → el endpoint falló; intentar el fallback HTML
    """
    try:
        resp = _requests_lib.post(
            "https://www.freefiremania.com.br/paginas/perfil-free-fire-check-id.php",
            data={"id": uid},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://www.freefiremania.com.br",
                "Referer": "https://www.freefiremania.com.br/perfil-free-fire-id.html",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None, ""
        data = resp.json()
        if not data.get("success"):
            return None, ""
        if not data.get("found"):
            return False, ""
        nick = str((data.get("player") or {}).get("nickname") or "").strip()
        if nick:
            return True, nick
        return None, ""
    except Exception:
        return None, ""


def scrape_ffmania_nick(uid: str) -> str:
    """Resuelve el nick de un ID de Free Fire.

    Prueba primero el scraping HTML de /cuenta/{uid}.html: no dispara el
    bloqueo por captcha que a veces cae sobre el endpoint JSON (visto
    2026-08-24, probablemente por el volumen acumulado de las 4 webs que
    comparten la IP del VPS — perfil-free-fire-check-id.php empezó a
    responder 422 {"error":"captcha_required"} para toda consulta) y ya
    cubre la gran mayoría de los casos reales: casi todo jugador que
    alguien va a verificar ya tiene su perfil cacheado en FFMania de una
    consulta anterior de cualquiera, no solo nuestra.

    Si el HTML no lo tiene (ID nuevo o muy poco consultado, /cuenta/ da
    404), se intenta el endpoint JSON como último recurso — es el único
    camino con lookup en vivo, aunque en ese momento puede estar bloqueado
    por el mismo captcha.
    """
    nick = _scrape_html_profile(uid)
    if nick:
        return nick

    found, nick = _ffmania_check_id(uid)
    if found is True:
        return nick
    return ""


def _scrape_html_profile(uid: str) -> str:
    """Scraping de /cuenta/{uid}.html — solo sirve para perfiles que FFMania
    ya tiene cacheados; los IDs nuevos dan 404 aquí (ver comentario en
    scrape_ffmania_nick)."""
    url = f"https://www.freefiremania.com.br/cuenta/{uid}.html"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read() or b""
    except Exception:
        # 404 (ID nuevo, sin cachear), bloqueo temporal, timeout, lo que sea:
        # no es motivo para reventar la request de verificación completa.
        return ""
    html_txt = raw.decode("utf-8", errors="ignore")

    # Convert HTML to plain-ish text to make extraction resilient to markup changes/ads.
    txt = html_txt
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", txt)
    txt = re.sub(r"(?i)<br\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</(p|div|tr|li|h1|h2|h3|table|section|article)>", "\n", txt)
    txt = re.sub(r"(?is)<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    txt = re.sub(r"[\t\r]+", " ", txt)
    txt = re.sub(r"[ ]{2,}", " ", txt)
    txt = re.sub(r"\n{2,}", "\n", txt)

    # Rediseño FFMania ~2026-08: la etiqueta "Nombre" ya no lleva dos puntos
    # (tabla <th>Nombre</th><td>NICK</td>) y el nick también está en <h1>/<title>.
    # Estos patrones van sobre el HTML crudo; el primero es el mismo que usa
    # Inefablestore (que sigue funcionando).
    html_patterns = [
        r"(?is)<[^>]*>\s*(?:Nombre|Nome|Nick)\s*:?\s*</[^>]*>\s*<[^>]*>\s*([^<]+?)\s*</",
        r"(?i)<h1[^>]*>\s*Perfil\s+d[eo]l?\s+J[ou]gador\s+(.+?)\s+[ne][nm]\s+Free\s+Fire\s*</h1>",
        r"(?i)<title>\s*(.+?)\s*\(ID\s*\d+\)",
    ]
    patterns = [
        r"(?im)^\s*Nombre\s*:\s*(.+?)\s*$",
        r"(?im)^\s*Nome\s*:\s*(.+?)\s*$",
        r"(?im)^\s*Nick\s*:\s*(.+?)\s*$",
        r"\"nick\"\s*:\s*\"([^\"]+)\"",
    ]
    nick = ""
    for source, pats in ((html_txt, html_patterns), (txt, patterns)):
        for pat in pats:
            m = re.search(pat, source, flags=re.IGNORECASE)
            if not m:
                continue
            cand = _html.unescape((m.group(1) or "")).strip()
            cand = re.sub(r"\s+", " ", cand).strip()
            # Placeholder de página sin datos: "ID 123456" no es un nick real
            if cand and not re.fullmatch(r"(?i)ID\s*\d+", cand):
                nick = cand
                break
        if nick:
            break
    return nick


# ── Blood Strike (Smile.One) scraper ────────────────────────────────────────

def scrape_smileone_bloodstrike_nick(role_id: str, bs_package_id: str = "", bs_server_id: str = "-1") -> str:
    """Consulta la API interna de Smile.One Brasil para obtener el nickname de Blood Strike."""
    try:
        sess = _requests_lib.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        })
        # Step 1: GET the Blood Strike page to obtain session cookies + CSRF token
        page_url = "https://www.smile.one/br/merchant/game/bloodstrike?source=other"
        page = sess.get(page_url, timeout=8)
        print(f"[BS] page status={page.status_code} cookies={dict(sess.cookies)}")
        # Extract CSRF token from _csrf cookie (Yii2 PHP serialized format)
        csrf = ""
        raw_csrf_cookie = sess.cookies.get("_csrf", "")
        try:
            import urllib.parse as _urlparse
            decoded = _urlparse.unquote(raw_csrf_cookie)
            # PHP serialized: i:1;s:32:"TOKEN_HERE";}
            m = re.search(r'i:1;s:\d+:"([^"]+)"', decoded)
            if m:
                csrf = m.group(1)
        except Exception:
            pass
        # Fallback: search in HTML
        if not csrf:
            for pat in [r'name="_csrf"\s+value="([^"]+)"', r'"csrf"\s*:\s*"([^"]+)"']:
                m = re.search(pat, page.text)
                if m:
                    csrf = m.group(1)
                    break
        print(f"[BS] csrf={csrf!r}")
        # Step 2: POST checkrole with session cookies + CSRF header
        post_headers = {
            "Referer": page_url,
            "Origin": "https://www.smile.one",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        if csrf:
            post_headers["X-CSRF-Token"] = csrf
        post_data = {
            "uid": role_id,
            "sid": bs_server_id or "-1",
            "pid": bs_package_id or "",
            "product": "bloodstrike",
            "checkrole": "1",
        }
        if csrf:
            post_data["_csrf"] = csrf
        # Try known endpoint variants
        resp = None
        for _endpoint in [
            "https://www.smile.one/br/merchant/game/checkrole?product=bloodstrike",
            "https://www.smile.one/merchant/bloodstrike/checkrole",
            "https://www.smile.one/merchant/checkrole",
        ]:
            resp = sess.post(_endpoint, data=post_data, headers=post_headers, timeout=8)
            print(f"[BS] {_endpoint} -> {resp.status_code} {resp.text[:150]}")
            if resp.status_code == 200:
                break
        if not resp or resp.status_code != 200:
            return ""
        try:
            import json
            data = resp.json()
        except Exception:
            # Some responses may be plain text
            import json
            txt = resp.text.strip()
            if txt.startswith('{"code":'):
                data = json.loads(txt)
            else:
                return ""
        # Handle error codes
        if int(data.get("code") or 0) != 200:
            # 201 = USER ID no existe, 404 = not found, etc.
            print(f"[BS] API error: {data.get('info', '')}")
            return ""
        # Extract username from various possible structures
        username = (
            (data.get("data") or {}).get("username")
            or (data.get("data") or {}).get("nickname")
            or (data.get("data") or {}).get("name")
            or data.get("username")
            or data.get("nickname")
            or data.get("name")
            or data.get("info")  # some APIs return username in info field
            or ""
        )
        if username:
            return username.strip()
        print(f"[BS] JSON completo: {data}")
        return ""
    except Exception as e:
        print(f"[BS] Error: {e}")
        return ""
