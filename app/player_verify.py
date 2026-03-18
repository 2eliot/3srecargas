"""
Player ID verification via scraping.
Replicated from Inefablestore – do NOT modify request headers, selectors, or URL patterns.
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

def scrape_ffmania_nick(uid: str) -> str:
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read() or b""
    except urllib.error.HTTPError as e:
        if int(getattr(e, "code", 0) or 0) == 404:
            return ""
        raise
    html_txt = raw.decode("utf-8", errors="ignore")

    # Convert HTML to plain-ish text to make extraction resilient to markup changes/ads.
    txt = html_txt
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\\1>", " ", txt)
    txt = re.sub(r"(?i)<br\\s*/?>", "\n", txt)
    txt = re.sub(r"(?i)</(p|div|tr|li|h1|h2|h3|table|section|article)>", "\n", txt)
    txt = re.sub(r"(?is)<[^>]+>", " ", txt)
    txt = _html.unescape(txt)
    txt = re.sub(r"[\t\r]+", " ", txt)
    txt = re.sub(r"[ ]{2,}", " ", txt)
    txt = re.sub(r"\n{2,}", "\n", txt)

    patterns = [
        r"(?im)^\s*Nombre\s*:\s*(.+?)\s*$",
        r"(?im)^\s*Nome\s*:\s*(.+?)\s*$",
        r"(?im)^\s*Nick\s*:\s*(.+?)\s*$",
        r"\"nick\"\s*:\s*\"([^\"]+)\"",
    ]
    nick = ""
    for pat in patterns:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if m:
            nick = (m.group(1) or "").strip()
            break
    nick = re.sub(r"\s+", " ", nick).strip()
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
