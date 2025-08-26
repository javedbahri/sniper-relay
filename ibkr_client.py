from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any, List

from ib_insync import IB, Stock, MarketOrder, LimitOrder, Contract, Ticker, Order, Trade  # type: ignore

# ---- env / defaults ----
IB_HOST          = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT_ENV      = int(os.getenv("IB_PORT", "7496"))  # TWS live=7496, paper=7497; IBGW live=4001, paper=4002
IB_CLIENT_ID_DEF = int(os.getenv("IB_CLIENT_ID", "201"))
DEFAULT_EXCHANGE = os.getenv("IB_EXCHANGE", "SMART")
DEFAULT_CCY      = os.getenv("IB_CURRENCY", "USD")


class IBKRClient:
    """
    Thin wrapper around ib_insync focused on:
      - connecting/closing
      - building simple stock contracts
      - quotes
      - positions
      - simple market/limit order placement
    """

    def __init__(
        self,
        host: str = IB_HOST,
        port: int = IB_PORT_ENV,
        client_id: int = IB_CLIENT_ID_DEF,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.connect_timeout = connect_timeout
        self.ib: IB = IB()
        self._connected = False

    # ---- connection ----
    def connect(self) -> None:
        if self._connected:
            return
        self.ib.connectedEvent.clear()
        self.ib.errorEvent.clear()
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=self.connect_timeout)
        # small wait to ensure connection state settles
        self.ib.sleep(0.1)
        self._connected = True

    def _ensure_conn(self) -> None:
        if not self._connected or not self.ib.isConnected():
            self.connect()

    def close(self) -> None:
        try:
            if self.ib and self.ib.isConnected():
                self.ib.disconnect()
        finally:
            self._connected = False

    def __enter__(self) -> "IBKRClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- contracts ----
    def stock(self, symbol: str, exchange: Optional[str] = None, currency: Optional[str] = None) -> Contract:
        return Stock(symbol.upper().strip(), exchange or DEFAULT_EXCHANGE, currency or DEFAULT_CCY)

    # ---- quotes ----
    def get_quote(self, symbol: str, exchange: Optional[str] = None, currency: Optional[str] = None) -> Optional[float]:
        """
        Return a representative marketable price:
        - prefer last if recent
        - else midpoint of bid/ask
        - else None
        """
        self._ensure_conn()
        c = self.stock(symbol, exchange, currency)
        [ticker] = self.ib.reqTickers(c)  # synchronous in ib_insync
        if not isinstance(ticker, Ticker):
            return None

        # Try last price first
        px = None
        if getattr(ticker, "last", None):
            px = float(ticker.last)
        elif getattr(ticker, "close", None):
            px = float(ticker.close)

        # Midpoint fallback
        bid = getattr(ticker, "bid", None)
        ask = getattr(ticker, "ask", None)
        if (px is None) and (bid is not None) and (ask is not None) and (bid > 0) and (ask > 0):
            px = (float(bid) + float(ask)) / 2.0
        return px

    # ---- positions ----
    def get_position_qty(
        self, symbol: str, exchange: Optional[str] = None, currency: Optional[str] = None
    ) -> float:
        """
        Return net position quantity for the given symbol (long > 0, short < 0, flat == 0).
        Filters by exchange/currency if provided.
        """
        self._ensure_conn()
        sym = (symbol or "").upper().strip()
        qty = 0.0
        try:
            positions = self.ib.positions()
            for p in positions:
                c = getattr(p, "contract", None)
                if not c or (getattr(c, "symbol", "") or "").upper() != sym:
                    continue
                if exchange and getattr(c, "exchange", None) and c.exchange != exchange:
                    continue
                if currency and getattr(c, "currency", None) and c.currency != currency:
                    continue
                try:
                    qty += float(getattr(p, "position", 0) or 0)
                except Exception:
                    pass
        except Exception:
            # If the positions call fails for any reason, fail-closed for SELL logic
            return 0.0
        return qty

    # ---- orders ----
    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
        tif: str = "DAY",
        exchange: Optional[str] = None,
        currency: Optional[str] = None,
        outsideRth: Optional[bool] = None,
        account: Optional[str] = None,
        transmit: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Place a simple stock order (BUY/SELL). Returns a dict with basic info.
        """
        self._ensure_conn()
        side_u = side.upper().strip()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"Unsupported side: {side}")

        qty = int(quantity)
        if qty <= 0:
            raise ValueError("quantity must be > 0")

        c = self.stock(symbol, exchange, currency)

        ot = order_type.upper().strip()
        if ot == "MKT":
            o: Order = MarketOrder(side_u, qty, tif=tif, outsideRth=outsideRth, account=account, transmit=transmit)
        elif ot in {"LMT", "LIMIT"}:
            if limit_price is None:
                raise ValueError("limit_price is required for LIMIT orders")
            o = LimitOrder(side_u, qty, limit_price, tif=tif, outsideRth=outsideRth, account=account, transmit=transmit)
        else:
            raise ValueError(f"Unsupported order_type: {order_type}")

        trade: Trade = self.ib.placeOrder(c, o)  # synchronous wrapper
        # give IB a tick to populate
        self.ib.sleep(0.05)

        return {
            "orderId": getattr(trade, "order", None) and getattr(trade.order, "orderId", None),
            "permId": getattr(trade, "order", None) and getattr(trade.order, "permId", None),
            "status": getattr(trade, "orderStatus", None) and getattr(trade.orderStatus, "status", None),
            "side": side_u,
            "symbol": symbol.upper(),
            "qty": qty,
            "type": ot,
            "limit": limit_price,
            "tif": tif,
        }

    def cancel_open_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Cancel open orders, optionally filtered by symbol.
        """
        self._ensure_conn()
        results: List[Dict[str, Any]] = []
        open_trades: List[Trade] = list(self.ib.openTrades())
        for t in open_trades:
            try:
                c = getattr(t, "contract", None)
                if symbol and c and getattr(c, "symbol", "").upper() != symbol.upper():
                    continue
                self.ib.cancelOrder(t.order)
                results.append({"orderId": getattr(t.order, "orderId", None), "canceled": True})
            except Exception as e:
                results.append({"orderId": getattr(t.order, "orderId", None), "canceled": False, "error": str(e)})
        return {"results": results}
