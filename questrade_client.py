# questrade_client.py
# Robust client with:
# - POST token refresh + rotation persisted to a cache file
# - Shared cache across processes (API, worker, REPL)
# - Auto /v1 prefixing so callers can pass "/symbols" or "/v1/symbols"
# - Auto-refresh on expiry/401

from __future__ import annotations
import os, json, time, tempfile, httpx
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _now() -> float:
    return time.time()


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".qt_tmp_", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


class QuestradeClient:
    def __init__(self, live: Optional[bool] = None, timeout: float = 20.0):
        self.live = bool(int(os.getenv("QT_LIVE", "0"))) if live is None else live
        self.login_host = "https://login.questrade.com" if self.live else "https://practicelogin.questrade.com"
        self.cache_path = ".qt_auth_live.json" if self.live else ".qt_auth_practice.json"

        cached = _read_json(self.cache_path)
        seed_env = (os.getenv("QT_REFRESH_TOKEN_LIVE") if self.live else os.getenv("QT_REFRESH_TOKEN")) or ""
        self.refresh_token = (cached.get("refresh_token") or seed_env).strip()

        if not self.refresh_token:
            raise RuntimeError(
                "Missing refresh token. Put a seed token in .env "
                "(QT_REFRESH_TOKEN_LIVE for live, QT_REFRESH_TOKEN for practice), "
                "or ensure the cache file exists."
            )

        self.access_token = cached.get("access_token")
        self.api_server = (cached.get("api_server") or "").rstrip("/")  # e.g. https://api02.iq.questrade.com
        self.expires_at = float(cached.get("expires_at") or 0.0)

        self.session = httpx.Client(timeout=timeout, headers={
            "User-Agent": os.getenv("QT_USER_AGENT", "sniper-relay/1.0"),
        })

        if not self._is_token_valid() or not self.api_server:
            self._refresh_tokens()

        # set auth header
        self.session.headers["Authorization"] = f"Bearer {self.access_token}"

    # ---------- public HTTP ----------

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._ensure_fresh()
        url = self._url(path)
        r = self.session.get(url, params=params)
        if r.status_code == 401:
            self._refresh_tokens()
            r = self.session.get(self._url(path), params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._ensure_fresh()
        url = self._url(path)
        r = self.session.post(url, json=json)
        if r.status_code == 401:
            self._refresh_tokens()
            r = self.session.post(self._url(path), json=json)
        r.raise_for_status()
        return r.json()

   
  
        # ---------- convenience ----------

    def resolve_symbol_id(self, symbol: str) -> int | None:
        """
        Resolve ticker (e.g., 'AAPL', 'RY.TO') to Questrade symbolId.
        """
        out = self.get("/symbols", params={"names": symbol})  # auto-prefixed to /v1
        syms = out.get("symbols") or []
        return syms[0].get("symbolId") if syms else None

    def get_quote(self, symbol: str) -> dict:
        """
        Fetch quote using ids=... (required by Questrade).
        """
        sid = self.resolve_symbol_id(symbol)
        if not sid:
            return {}
        out = self.get("/markets/quotes", params={"ids": str(sid)})  # auto /v1 prefix
        quotes = out.get("quotes") or []
        return quotes[0] if quotes else {}



    # ---------- internals ----------

    def _url(self, path: str) -> str:
        """
        Build a full URL:
          - If path starts with 'http', return as-is.
          - Else, ensure it starts with '/v1/' (auto-prefix if needed).
        """
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/v1/"):
            path = "/v1" + path  # auto-prefix
        base = self.api_server.rstrip("/")
        if not base:
            raise RuntimeError("API server not initialized.")
        return base + path

    def _is_token_valid(self) -> bool:
        # valid if >60s remaining
        return bool(self.access_token) and _now() < (self.expires_at - 60)

    def _ensure_fresh(self) -> None:
        if not self._is_token_valid():
            self._refresh_tokens()

    def _save_cache(self) -> None:
        data = {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "api_server": self.api_server,
            "expires_at": self.expires_at,
            "live": self.live,
            "saved_at": _now(),
        }
        _atomic_write(self.cache_path, data)

    def _refresh_tokens(self) -> None:
        def _do_refresh(rt: str) -> Dict[str, Any]:
            resp = self.session.post(
                f"{self.login_host}/oauth2/token",
                data={"grant_type": "refresh_token", "refresh_token": rt},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()

        try:
            data = _do_refresh(self.refresh_token)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 400:
                # try newer token from cache (another process may have rotated it)
                latest = _read_json(self.cache_path).get("refresh_token")
                if latest and latest != self.refresh_token:
                    self.refresh_token = latest.strip()
                    data = _do_refresh(self.refresh_token)
                else:
                    raise
            else:
                raise

        self.access_token = data["access_token"]
        self.api_server = data["api_server"].rstrip("/")
        expires_in = int(data.get("expires_in", 1800))
        self.expires_at = _now() + max(60, expires_in)

        new_rt = (data.get("refresh_token") or "").strip()
        if new_rt:
            self.refresh_token = new_rt

        self.session.headers["Authorization"] = f"Bearer {self.access_token}"
        self._save_cache()
