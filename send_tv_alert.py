# send_tv_alert.py — mimic a TradingView webhook alert (secret in BODY)
import uuid, json, requests
from datetime import datetime, timezone

# ---- Fill these 3 and run: python send_tv_alert.py ----
BASE          = "https://noble-seriously-koala.ngrok-free.app"
PATH_TOKEN    = "7e6d7e6d7e6d7e6d"
SHARED_SECRET = "22708371ea6e3a956f5005adf5c9e49bac5955c2dd66e49daffbdaf57caf323a"
# -------------------------------------------------------

# Default alert fields (edit as you like)
VERSION         = "1"
STRATEGY_ID     = "sniper_v13_2"
EVENT           = "BUY"                 # BUY | SELL | EXIT | CANCEL
SYMBOL          = "TSLA"
EXCHANGE        = "SMART"
CURRENCY        = "USD"
INTERVAL        = "5"
QTY             = 5
ORDER_TYPE      = "MarketableLimit"     # MarketableLimit | Market | Limit
LIMIT_OFFSET_BPS= 30
LIMIT_PRICE     = None                  # use when ORDER_TYPE="Limit"
TIME_IN_FORCE   = "DAY"
PAPER           = True                  # FYI only unless you wire it through server-side

def iso_now_z():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def main():
    now = iso_now_z()
    bar_index = 0  # just a placeholder for parity with TV's {{bar_index}}

    body = {
        "version": VERSION,
        "strategy_id": STRATEGY_ID,
        "event": EVENT,
        "symbol": SYMBOL,
        "exchange": EXCHANGE,
        "currency": CURRENCY,
        "interval": INTERVAL,
        "qty": QTY,
        "order_type": ORDER_TYPE,             # snake_case like TV
        "limit_offset_bps": LIMIT_OFFSET_BPS,
        "limit_price": LIMIT_PRICE,
        "time_in_force": TIME_IN_FORCE,
        "paper": PAPER,
        "time": now,                           # {{timenow}}
        "nonce": f"{now}-{bar_index}",         # {{timenow}}-{{bar_index}}
        "idempotency_key": f"v1-{SYMBOL}-{INTERVAL}-{now}-{bar_index}",
        "secret": SHARED_SECRET,               # <<< secret in BODY (TV style)
    }

    url = f"{BASE}/webhook/{PATH_TOKEN}"
    headers = {"Content-Type": "application/json"}

    print("POST", url)
    print("BODY", json.dumps(body))
    r = requests.post(url, data=json.dumps(body), headers=headers, timeout=12)
    print("STATUS", r.status_code)
    print("RESP  ", r.text)

if __name__ == "__main__":
    main()
