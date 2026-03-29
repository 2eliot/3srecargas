"""Binance Pay automatic payment verification — per-order thread.

Logic:
  1. Customer selects Binance Pay at checkout.
    2. System generates a unique 6-digit numeric code (e.g. "483921").
  3. Customer transfers the exact USD amount (as USDT) to the configured
         Binance Pay address and writes the 6-digit code in the payment Nota/Memo.
  4. When the order is saved (POST confirm), a dedicated daemon thread is
     launched ONLY for that specific order.  No global polling at startup.
  5. The thread polls /sapi/v1/pay/transactions via proxy every 30 s until:
       – payment found  → auto-approve
       – order no longer pending (cancelled/approved externally)
       – 35-minute timeout
  6. All Binance API requests go through BINANCE_PROXY if set.

Environment variables required:
  BINANCE_API_KEY                  – Binance API key (pay-history permissions)
  BINANCE_API_SECRET               – Binance API secret
  BINANCE_PROXY                    – (optional) e.g. http://user:pass@host:port
  BINANCE_REQUEST_TIMEOUT_SECONDS  – per-endpoint TCP timeout (default 4)

DB settings (Setting model):
  binance_auto_enabled     – '1' to activate, '0' / missing to disable
  binance_wallet_address   – address/email shown to the customer at checkout
"""

import hashlib
import hmac
import random
import threading
import time
from datetime import timedelta

import requests

_BINANCE_ENDPOINTS = [
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api.binance.com",
]
_BINANCE_PAY_PATH = "/sapi/v1/pay/transactions"

# Maximum time (seconds) a per-order thread keeps polling (35 min > 30 min checkout timer)
_ORDER_POLL_MAX_SECONDS = 35 * 60
# Interval between Binance API polls
_ORDER_POLL_INTERVAL = 30


# ── Signature ─────────────────────────────────────────────────────────────────

def _sign(secret: str, query_string: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ── Fetch Pay transactions via proxy ─────────────────────────────────────────

def _fetch_pay_transactions(api_key: str, api_secret: str, proxy: str, timeout: float,
                             start_time_ms: int, limit: int = 100):
    """Query Binance Pay transaction history through the configured proxy.

    Returns list of transactions, or None if all endpoints fail.
    """
    ts = int(time.time() * 1000)
    params = f"startTime={start_time_ms}&limit={limit}&timestamp={ts}"
    sig = _sign(api_secret, params)
    qs = f"{params}&signature={sig}"
    headers = {"X-MBX-APIKEY": api_key}
    # Always route through proxy when configured
    proxies = {"https": proxy, "http": proxy} if proxy else None

    for base in _BINANCE_ENDPOINTS:
        try:
            resp = requests.get(
                f"{base}{_BINANCE_PAY_PATH}?{qs}",
                headers=headers,
                proxies=proxies,
                timeout=timeout,
            )
            if resp.ok:
                data = resp.json()
                return data.get("data") or data.get("rows") or []
        except Exception:
            continue
    return None  # all endpoints failed


# ── Payment verification ──────────────────────────────────────────────────────

def verify_binance_payment(api_key: str, api_secret: str, proxy: str, timeout: float,
                            order_reference: str, expected_usdt: float, since_ms: int):
    """Check if any Binance Pay transaction matches the memo code and USDT amount.

    Returns:
        True  – payment verified
        False – not found yet (keep polling)
        None  – Binance API unreachable (keep polling)
    """
    txs = _fetch_pay_transactions(api_key, api_secret, proxy, timeout, since_ms)
    if txs is None:
        return None
    if not txs:
        return False

    ref_upper = str(order_reference).upper().strip()
    for tx in txs:
        note = (
            tx.get("orderMemo")
            or tx.get("remark")
            or tx.get("note")
            or ""
        )
        if ref_upper not in str(note).upper().strip():
            continue

        # Verify currency is USDT
        tx_currency = ""
        funds = tx.get("fundsDetail") or []
        if isinstance(funds, list) and funds:
            tx_currency = str(funds[0].get("currency") or "").upper()
        if not tx_currency:
            tx_currency = str(tx.get("transactedCurrency") or tx.get("currency") or "").upper()
        if tx_currency and tx_currency != "USDT":
            continue

        # Verify amount ±0.01 USDT
        tx_amount = 0.0
        if isinstance(funds, list) and funds:
            try:
                tx_amount = float(funds[0].get("amount") or 0)
            except Exception:
                pass
        if tx_amount == 0.0:
            try:
                tx_amount = float(tx.get("transactedAmount") or tx.get("amount") or 0)
            except Exception:
                pass
        if abs(tx_amount - expected_usdt) <= 0.01:
            return True
    return False


# ── Code helpers ──────────────────────────────────────────────────────────────

def is_binance_auto_reference(reference: str) -> bool:
    """True if reference is a valid Binance auto-code: 6 numeric digits."""
    r = str(reference or "").strip()
    return len(r) == 6 and r.isdigit()


def generate_binance_auto_code(app) -> str:
    """Generate a unique 6-digit numeric code not already in use by a pending order."""
    chars = "0123456789"
    with app.app_context():
        from ..models import Order
        for _ in range(50):
            code = "".join(random.choices(chars, k=6))
            if not Order.query.filter_by(payment_reference=code, status="pending").first():
                return code
    return f"{random.randint(0, 999999):06d}"


# ── Settings helpers ──────────────────────────────────────────────────────────

def is_binance_auto_enabled(app) -> bool:
    """Return True only when binance_auto_enabled == '1' in DB settings."""
    with app.app_context():
        from ..models import Setting
        s = Setting.query.filter_by(key="binance_auto_enabled").first()
        return bool(s and s.value == "1")


# ── Auto-approve ──────────────────────────────────────────────────────────────

def _auto_approve_order(order, app):
    """Call approve_order() for a verified Binance payment."""
    from ..models import db
    from ..utils.order_processing import approve_order

    if (order.status or "").lower() != "pending":
        print(f"[BinanceAuto] #{order.order_number} already '{order.status}', skipping.")
        return
    try:
        result = approve_order(order)
        print(f"[BinanceAuto] #{order.order_number} auto-approved: {result.get('message', '')}")
    except Exception as exc:
        print(f"[BinanceAuto] Error approving #{order.order_number}: {exc}")
        try:
            db.session.rollback()
        except Exception:
            pass


# ── Per-order verification thread ─────────────────────────────────────────────

def _verify_order_thread(order_id: int, order_number: str, app):
    """Daemon thread for ONE order.

    Polls Binance Pay via proxy every 30 s.
    Exits when: payment found, order leaves 'pending', or 35-min timeout.
    The Binance API is never called unless an actual order triggered this thread.
    """
    log = f"[BinanceAuto #{order_number}]"
    deadline = time.time() + _ORDER_POLL_MAX_SECONDS

    # Short initial delay so DB commit is fully visible before first query
    time.sleep(12)

    while time.time() < deadline:
        try:
            if not is_binance_auto_enabled(app):
                print(f"{log} Feature disabled — stopping thread.")
                return

            api_key = app.config.get("BINANCE_API_KEY", "").strip()
            api_secret = app.config.get("BINANCE_API_SECRET", "").strip()
            if not api_key or not api_secret:
                print(f"{log} API credentials missing — stopping thread.")
                return

            proxy = app.config.get("BINANCE_PROXY", "").strip()
            timeout = float(app.config.get("BINANCE_REQUEST_TIMEOUT", 4))

            with app.app_context():
                from ..models import db, Order
                order = Order.query.get(order_id)

                if order is None:
                    print(f"{log} Order not found in DB — stopping thread.")
                    return

                if (order.status or "").lower() != "pending":
                    print(f"{log} Status is '{order.status}' — stopping thread.")
                    return

                if not is_binance_auto_reference(order.payment_reference):
                    print(f"{log} Invalid reference format — stopping thread.")
                    return

                since_ms = int(
                    (order.created_at - timedelta(minutes=2)).timestamp() * 1000
                )
                expected_usdt = float(order.amount or 0.0)
                if expected_usdt <= 0:
                    print(f"{log} Amount is 0 — stopping thread.")
                    return

                result = verify_binance_payment(
                    api_key, api_secret, proxy, timeout,
                    order_reference=str(order.payment_reference).upper(),
                    expected_usdt=expected_usdt,
                    since_ms=since_ms,
                )

                if result is True:
                    db.session.refresh(order)
                    if (order.status or "").lower() == "pending":
                        print(f"{log} Payment verified — auto-approving.")
                        _auto_approve_order(order, app)
                    return  # done regardless

                # result False → not found yet; None → API unreachable → keep polling
                print(f"{log} Not found yet (result={result}), next check in {_ORDER_POLL_INTERVAL}s.")

        except Exception as exc:
            print(f"{log} Thread error: {exc}")

        time.sleep(_ORDER_POLL_INTERVAL)

    print(f"{log} Verification timeout after {_ORDER_POLL_MAX_SECONDS // 60} min.")


def start_order_verification(order, app):
    """Launch a dedicated verification thread for a single Binance Pay order.

    Called immediately after the order is committed to the DB.
    No thread is ever started at app startup — only when a real order is created.
    """
    t = threading.Thread(
        target=_verify_order_thread,
        args=(order.id, order.order_number, app),
        daemon=True,
        name=f"binance-{order.order_number}",
    )
    t.start()
    print(f"[BinanceAuto] Verification thread started for order #{order.order_number}.")
