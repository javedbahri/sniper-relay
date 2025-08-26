# main.py
from dotenv import load_dotenv; load_dotenv()
import os
from typing import Optional, Literal
from datetime import datetime, timezone, time
import zoneinfo

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
import redis
from rq import Queue

# in main.py startup()


APP_NAME = os.getenv("APP_NAME", "sniper-relay")
PATH_TOKEN = os.getenv("PATH_TOKEN", "7e6d7e6d7e6d7e6d")
SHARED_SECRET = os.getenv("SHARED_SECRET", "")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RQ_QUEUE = os.getenv("RQ_QUEUE", "sniper")
IB_LIVE = os.getenv("IB_LIVE", "0") == "1"

# security knobs (override via .env)
MAX_SKEW_SECONDS  = int(os.getenv("MAX_SKEW_SECONDS", "60"))      # +/- allowed drift
NONCE_TTL_SECONDS = int(os.getenv("NONCE_TTL_SECONDS", "300"))    # block replays within TTL
MAX_QTY           = int(os.getenv("MAX_QTY", "100"))              # hard share cap
MAX_NOTIONAL_USD  = float(os.getenv("MAX_NOTIONAL_USD", "0"))     # 0 disables notional cap
MAX_BODY_BYTES    = int(os.getenv("MAX_BODY_BYTES", "10000"))     # 10 KB default
ENFORCE_RTH_AT_API = os.getenv("ENFORCE_RTH_AT_API", "0") == "1"  # prefilter at API

# ---------- Redis (singleton) ----------
try:
    r = redis.from_url(REDIS_URL)
except Exception as e:
    raise RuntimeError(f"Redis init failed: {e}")

q = Queue(RQ_QUEUE, connection=r)
app = FastAPI(title=APP_NAME)

@app.get("/healthz")
def healthz():
    return {"ok": True}
# ---------- helpers ----------
def _parse_iso8601_z(ts: str) -> datetime:
    """Parse ISO8601; accept trailing 'Z'. Return timezone-aware UTC dt."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _is_rth_now_api(tz="America/New_York") -> bool:
    now_dt = datetime.now(zoneinfo.ZoneInfo(tz))
    if now_dt.weekday() > 4:
        return False
    now_t = now_dt.time()
    return time(9, 30) <= now_t <= time(16, 0)

# ---------- schema ----------
class TVPayload(BaseModel):
    version: Optional[str] = None
    strategy_id: Optional[str] = None

    event: Literal["BUY", "SELL", "EXIT", "CANCEL"]
    symbol: str
    exchange: Optional[str] = None
    currency: Optional[str] = "USD"
    interval: Optional[str] = None
    price: Optional[float] = None

    qty: Optional[int] = Field(default=None, ge=1)
    order_type: Optional[Literal["Market", "Limit", "MarketableLimit"]] = "MarketableLimit"
    limit_offset_bps: Optional[int] = Field(default=30, ge=0, le=500)
    limit_price: Optional[float] = Field(default=None, gt=0)
    time_in_force: Optional[Literal["DAY", "GTC"]] = "DAY"

    paper: Optional[bool] = None
    time: str
    nonce: str
    idempotency_key: Optional[str] = None

    secret: Optional[str] = None  # TV body secret

    @validator("order_type", pre=True)
    def _normalize_otype(cls, v):
        if not v:
            return "MarketableLimit"
        s = str(v).strip().lower()
        if s in ("marketablelimit", "marketable_limit"):
            return "MarketableLimit"
        if s in ("market", "mkt"):
            return "Market"
        if s in ("limit", "lmt"):
            return "Limit"
        return "MarketableLimit"

# ---------- startup ----------
@app.on_event("startup")
def startup_check():
    try:
        if not r.ping():
            raise RuntimeError("Redis ping returned False")
    except Exception as e:
        raise RuntimeError(f"Redis not available: {e}")

# ---------- misc ----------
@app.get("/")
def root():
    return {"ok": True, "route": f"/webhook/{PATH_TOKEN}", "queue": RQ_QUEUE, "live": IB_LIVE}

@app.get("/health")
def health():
    try:
        ok = r.ping()
        return {"ok": bool(ok), "redis": bool(ok), "live": IB_LIVE}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": f"redis: {e}", "live": IB_LIVE})

# ---------- webhook ----------
@app.post("/webhook/{token}")
async def webhook(token: str, request: Request):
    # 0) Path token
    if token != PATH_TOKEN:
        raise HTTPException(status_code=404, detail="Not Found")

    # 0.1) Content-type & size guards
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="application/json required")
    clen = request.headers.get("content-length")
    if clen and int(clen) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    # 1) Parse JSON
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 2) Validate schema
    try:
        payload = TVPayload(**raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Validation error: {e}")

    # 3) Auth: header or body secret
    header_secret = request.headers.get("X-Shared-Secret")
    if SHARED_SECRET and not (header_secret == SHARED_SECRET or payload.secret == SHARED_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 4) Timestamp skew
    try:
        ts = _parse_iso8601_z(payload.time)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid time format; expected ISO8601")
    now = datetime.now(timezone.utc)
    skew = abs((now - ts).total_seconds())
    if skew > MAX_SKEW_SECONDS:
        raise HTTPException(status_code=400, detail=f"Stale/early alert (skew {int(skew)}s > {MAX_SKEW_SECONDS}s)")

    # 5) Anti-replay via nonce
    nonce_key = f"nonce:{payload.nonce}"
    try:
        created = r.set(nonce_key, "1", ex=NONCE_TTL_SECONDS, nx=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"redis_error: {e}")
    if not created:
        raise HTTPException(status_code=409, detail="Duplicate nonce (replay detected)")

    # 6) Optional API-layer RTH block (prevents even enqueuing outside RTH)
    if ENFORCE_RTH_AT_API and not _is_rth_now_api():
        return JSONResponse(status_code=202, content={
            "queued": False, "skipped": "outside_rth", "live": IB_LIVE
        })

    # 7) Qty / notional caps (defense-in-depth)
    if payload.qty is None or payload.qty < 1:
        raise HTTPException(status_code=400, detail="qty must be >= 1")
    if payload.qty > MAX_QTY:
        raise HTTPException(status_code=400, detail=f"qty exceeds MAX_QTY ({MAX_QTY})")
    if MAX_NOTIONAL_USD > 0 and payload.price:
        notional = float(payload.qty) * float(payload.price)
        if notional > MAX_NOTIONAL_USD:
            raise HTTPException(status_code=400, detail=f"notional {notional:.2f} > cap {MAX_NOTIONAL_USD:.2f}")

    # 8) Build worker payload
    job_payload = payload.dict()
    job_payload["meta"] = {
        "exchange": payload.exchange,
        "currency": payload.currency,
        "interval": payload.interval,
        "time": payload.time,
        "ip": request.client.host if request.client else None,
        "ua": request.headers.get("User-Agent"),
    }

    # 9) Enqueue
    try:
        job = q.enqueue(
            "tasks.execute_signal",
            kwargs={"payload": job_payload, "live": IB_LIVE},
            retry=None,
            result_ttl=3600,
            failure_ttl=86400,
        )
    except AssertionError as e:
        raise HTTPException(status_code=500, detail=f"enqueue_failed: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"enqueue_error: {e}")

    return JSONResponse({"queued": True, "job_id": job.id, "route": f"/webhook/{PATH_TOKEN}", "live": IB_LIVE})
