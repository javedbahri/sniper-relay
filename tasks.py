# tasks.py
from __future__ import annotations
from dotenv import load_dotenv; load_dotenv()

import os, math, json
from typing import Optional, Dict, Any
from datetime import datetime, time as dtime
import zoneinfo
import redis

from ibkr_client import IBKRClient

# --- env / redis ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
IDEMP_TTL_SECONDS = int(os.getenv("IDEMP_TTL_SECONDS", "600"))   # 10 min window
MAX_QTY = int(os.getenv("MAX_QTY", "100"))
MAX_NOTIONAL_USD = float(os.getenv("MAX_NOTIONAL_USD", "10000"))
ENFORCE_RTH_AT_API = int(os.getenv("ENFORCE_RTH_AT_API", "1"))   # 1 = gate orders to RTH
ALLOW_TEST_OUTSIDE_RTH = int(os.getenv("ALLOW_TEST_OUTSIDE_RTH", "0"))
QUOTES_ENABLED = int(os.getenv("QUOTES_ENABLED", "1"))           # 0 = never fetch quotes

# IBKR env (kept for clarity/logging; IBKRClient reads env itself)
IB_HOST = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT = int(os.getenv("IB_PORT", "7496"))
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "201"))

# --- helpers ---
_TZ = zoneinfo.ZoneInfo(os.getenv("MARKET_TZ", "America/New_York"))

def _log(event: str, data: Optional[Dict[str, Any]] = None) -> None:
    payload = {"ts": datetime.now(tz=_TZ).isoformat(), "event": event, "data": data or {}}
    print(json.dumps(payload, ensure_ascii=False))

def _is_rth(now: Optional[datetime] = None) -> bool:
    """Simple US equities RTH: 09:30–16:00 ET, Mon–Fri (no holiday calendar)."""
    n = now.astimezone(_TZ) if now else datetime.now(tz=_TZ)
    if n.weekday() >= 5:
        return False
    start = dtime(9, 30)
    end = dtime(16, 0)
    return start <= n.time() <= end

def _qty_from_config(requested: Optional[int]) -> int:
    try:
        q = int(requested or 0)
    except Exception:
        q = 0
    if q < 1:
        q = 1
    if q > MAX_QTY:
        q = MAX_QTY
    return q

def _limit_from(ref_price: float, side: str, bps: int) -> float:
    """
    MarketableLimit:
      BUY  -> ref * (1 + bps/10000)
      SELL -> ref * (1 - bps/10000)
    """
    if ref_price is None or not math.isfinite(ref_price):
        raise ValueError("ref_price required for MarketableLimit")
    sign = +1 if side.upper() == "BUY" else -1
    return float(ref_price) * (1.0 + sign * (float(bps) / 10_000.0))

def _idempotency_ok(r: redis.Redis, key: Optional[str]) -> bool:
    """
    Returns True if we stored the key (first time), else False if duplicate inside TTL.
    If key is falsy, we skip idempotency and return True.
    """
    if not key:
        return True
    pipe = r.pipeline(True)
    pipe.setnx(key, "1")
    pipe.expire(key, IDEMP_TTL_SECONDS)
    created, _ = pipe.execute()
    return bool(created)

# --- CORE ENTRYPOINT (called by RQ worker) ---
def execute_signal(payload: Dict[str, Any], live: bool = False) -> Dict[str, Any]:
    """
    Lenient payload (TV/body or internal):
      {
        "event": "BUY"|"SELL",
        "symbol": "AAPL",
        "qty": 10,
        "orderType"/"order_type": "MKT"|"LMT"|"LIMIT"|"MarketableLimit",
        "limitBps"/"limit_offset_bps": 15,
        "limitPx"/"limit_price": 200.12,
        "tif"/"time_in_force": "DAY",
        "exchange": "SMART",
        "currency": "USD",
        "idempotencyKey"/"idempotency_key"/"nonce": "abc123",
      }
    """
    r = redis.from_url(REDIS_URL, decode_responses=True)

    # --- normalize fields (camelCase & snake_case) ---
    event      = (payload.get("event") or payload.get("side") or "").upper().strip()
    symbol     = (payload.get("symbol") or payload.get("ticker") or "").upper().strip()
    order_type = (payload.get("orderType") or payload.get("order_type") or "MKT").strip()
    limit_bps  = payload.get("limitBps", payload.get("limit_offset_bps"))
    limit_px   = payload.get("limitPx",  payload.get("limit_price"))
    tif        = (payload.get("tif") or payload.get("time_in_force") or "DAY").strip()
    idkey      = payload.get("idempotencyKey") or payload.get("idempotency_key") or payload.get("nonce")
    exchange   = payload.get("exchange")
    currency   = payload.get("currency")
    req_qty    = payload.get("qty") or payload.get("quantity")

    if not event or event not in {"BUY", "SELL"}:
        return {"ok": False, "error": f"invalid_event:{event}"}
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}

    # idempotency (skip if duplicate within TTL)
    idemp_key = f"idemp:{symbol}:{event}:{idkey}" if idkey else None
    if not _idempotency_ok(r, idemp_key):
        _log("SKIP_IDEMPOTENT", {"symbol": symbol, "event": event, "id": idkey})
        return {"ok": True, "skipped": "duplicate"}

    qty = _qty_from_config(req_qty)

    # RTH enforcement
    if ENFORCE_RTH_AT_API and not _is_rth() and not ALLOW_TEST_OUTSIDE_RTH:
        _log("SKIP_OUTSIDE_RTH", {"symbol": symbol, "event": event, "qty": qty})
        return {"ok": True, "skipped": "outside_rth"}

    with IBKRClient() as c:
        # --- SELL guard (strict: block oversell) ---
        if event == "SELL":
            held = float(c.get_position_qty(symbol, exchange, currency))
            if held <= 0:
                _log("SELL_SKIPPED_NO_POSITION", {"symbol": symbol, "requested_qty": qty, "held": held})
                return {"ok": True, "skipped": "no_position", "symbol": symbol, "held": held}
            if qty > held:
                _log("SELL_BLOCKED_OVERHOLD", {"symbol": symbol, "requested_qty": qty, "held": held})
                return {"ok": False, "error": "sell_qty_exceeds_holdings", "symbol": symbol, "held": held}

        # --- order type resolution (zero-quote friendly) ---
        eff_type: str = order_type.upper()
        eff_limit: Optional[float] = None

        if eff_type in {"MKT", "MARKET"}:
            eff_type = "MKT"

        elif eff_type in {"LMT", "LIMIT"}:
            eff_type = "LMT"
            if limit_px is not None and math.isfinite(float(limit_px)):
                eff_limit = float(limit_px)
            elif QUOTES_ENABLED and limit_bps is not None:
                ref = c.get_quote(symbol, exchange, currency)
                if ref is None:
                    return {"ok": False, "error": "no_quote_for_limit"}
                eff_limit = _limit_from(ref, event, int(limit_bps))
            else:
                return {"ok": False, "error": "limit_price_required"}

        elif eff_type in {"MARKETABLELIMIT", "MARKETABLE_LIMIT", "MLMT"}:
            if not QUOTES_ENABLED:
                # zero-quote mode: treat MLMT as market
                eff_type = "MKT"
            else:
                if limit_bps is None:
                    return {"ok": False, "error": "limitBps_required_for_MarketableLimit"}
                ref = c.get_quote(symbol, exchange, currency)
                if ref is None:
                    _log("NO_QUOTE_FALLBACK", {"symbol": symbol, "side": event, "limit_bps": limit_bps})
                    eff_type = "MKT"
                else:
                    eff_type = "LMT"
                    eff_limit = _limit_from(ref, event, int(limit_bps))
        else:
            return {"ok": False, "error": f"unsupported_order_type:{order_type}"}

        # --- notional guard (best-effort; skip check if we have no price) ---
        ref = eff_limit if eff_limit is not None else (c.get_quote(symbol, exchange, currency) if QUOTES_ENABLED else None)
        if ref is None and MAX_NOTIONAL_USD > 0:
            _log("NOTIONAL_CAP_UNCHECKED", {"symbol": symbol, "qty": qty})
        elif ref is not None and MAX_NOTIONAL_USD > 0:
            notional = float(qty) * float(ref)
            if notional > MAX_NOTIONAL_USD:
                scaled = int(MAX_NOTIONAL_USD // max(ref, 0.01))
                if scaled < 1:
                    _log("SKIP_NOTIONAL_CAP", {"symbol": symbol, "requested_qty": qty, "ref": ref})
                    return {"ok": True, "skipped": "exceeds_notional_cap"}
                _log("QTY_SCALED_BY_NOTIONAL_CAP", {"symbol": symbol, "from": qty, "to": scaled, "ref": ref})
                qty = scaled

        # --- log before placing so we always see the attempt ---
        _log("PLACING", {"symbol": symbol, "side": event, "qty": qty, "type": eff_type, "limit": eff_limit})

        # --- place order ---
        try:
            resp = c.place_order(
                symbol=symbol,
                side=event,
                quantity=qty,
                order_type=eff_type,
                limit_price=eff_limit,
                tif=tif,
                exchange=exchange,
                currency=currency,
                outsideRth=bool(ALLOW_TEST_OUTSIDE_RTH or not ENFORCE_RTH_AT_API),
                transmit=True,
            )
        except Exception as e:
            _log("ORDER_ERROR", {
                "symbol": symbol, "side": event, "qty": qty,
                "type": eff_type, "limit": eff_limit, "tif": tif, "error": str(e)
            })
            return {"ok": False, "error": "place_order_failed", "detail": str(e)}

        _log("ORDER_PLACED", {
            "symbol": symbol, "side": event, "qty": qty,
            "type": eff_type, "limit": eff_limit, "tif": tif, "resp": resp
        })

        return {
            "ok": True,
            "symbol": symbol,
            "side": event,
            "qty": qty,
            "type": eff_type,
            "limit": eff_limit,
            "tif": tif,
            "broker": "IBKR",
            "resp": resp,
        }
