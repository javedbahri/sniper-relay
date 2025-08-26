# test_order.py
import os, uuid, argparse
import os; os.environ["IB_CLIENT_ID"] = "301"
from tasks import execute_signal
# --- Host env (override BEFORE imports) ---
os.environ.setdefault("REDIS_URL", "redis://default:Lenovo578Q@localhost:6379/0")
os.environ.setdefault("IB_HOST", "127.0.0.1")  # host talks directly to local TWS
os.environ.setdefault("IB_PORT", "7496")       # 7496=LIVE, 7497=PAPER
os.environ.setdefault("IB_CLIENT_ID", "301")   # unique clientId for this host test

from tasks import execute_signal  # imports ibkr_client with the env above

def main():
    p = argparse.ArgumentParser(description="Manual order test (bypasses webhook/TV)")
    p.add_argument("--event", choices=["BUY", "SELL"], default="BUY")
    p.add_argument("--symbol", default="NIO")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--type", choices=["MKT", "LMT", "MarketableLimit"], default="MKT")
    p.add_argument("--limitPx", type=float, default=None, help="Use with --type LMT")
    p.add_argument("--tif", choices=["DAY", "GTC"], default="DAY")
    args = p.parse_args()

    payload = {
        "event": args.event,
        "symbol": args.symbol.upper(),
        "qty": args.qty,
        "orderType": args.type,        # tasks.py accepts camelCase
        "limitPx": args.limitPx,       # only used for LMT
        "tif": args.tif,
        "idempotencyKey": f"host-test-{uuid.uuid4()}",
        "nonce": str(uuid.uuid4()),
    }

    print("Sending:", payload)
    resp = execute_signal(payload, live=True)
    print("Response:", resp)

if __name__ == "__main__":
    main()
