# audit.py
from __future__ import annotations
from datetime import datetime
import json, os
from typing import Any, Dict, Optional

from sqlalchemy import (create_engine, Column, Integer, Float, String, Boolean,
                        DateTime, Text, JSON)
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL = os.getenv("AUDIT_DB_URL", "sqlite:///./sniper_audit.db")
engine = create_engine(DB_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class ApiEvent(Base):
    __tablename__ = "api_events"
    id = Column(Integer, primary_key=True)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ip = Column(String)
    user_agent = Column(String)
    event = Column(String)
    symbol = Column(String)
    qty = Column(Integer)
    order_type = Column(String)
    tif = Column(String)
    idempotency_key = Column(String)
    nonce = Column(String)
    accepted = Column(Boolean, default=False)
    reason = Column(String)
    raw = Column(JSON)

class OrderRow(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    event = Column(String)
    symbol = Column(String)
    qty = Column(Integer)
    order_type = Column(String)
    limit_price = Column(Float)
    tif = Column(String)
    exchange = Column(String)
    currency = Column(String)
    live = Column(Boolean, default=False)

    ib_order_id = Column(Integer)
    ib_perm_id = Column(Integer)
    client_id = Column(Integer)
    status = Column(String)
    filled = Column(Float)
    remaining = Column(Float)
    avg_fill_price = Column(Float)
    warning_text = Column(Text)

    request = Column(JSON)   # what we sent (contract+order)
    response = Column(JSON)  # what we got back

def init_db():
    Base.metadata.create_all(bind=engine)

def log_api_event(payload: Dict[str, Any], ip: Optional[str], ua: Optional[str],
                  accepted: bool, reason: Optional[str]) -> int:
    sess = SessionLocal()
    try:
        row = ApiEvent(
            ip=ip, user_agent=ua,
            event=payload.get("event"), symbol=payload.get("symbol"),
            qty=payload.get("qty"), order_type=payload.get("order_type"),
            tif=payload.get("time_in_force"),
            idempotency_key=payload.get("idempotency_key"),
            nonce=payload.get("nonce"),
            accepted=accepted, reason=reason,
            raw=payload
        )
        sess.add(row); sess.commit(); sess.refresh(row)
        return row.id
    finally:
        sess.close()

def insert_order(event: str, symbol: str, qty: int, order_type: str, limit_price: float,
                 tif: str, exchange: str, currency: str, live: bool,
                 request_obj: Dict[str, Any], response_obj: Dict[str, Any]) -> int:
    sess = SessionLocal()
    try:
        row = OrderRow(
            event=event, symbol=symbol, qty=qty, order_type=order_type, limit_price=limit_price,
            tif=tif, exchange=exchange, currency=currency, live=live,
            request=request_obj, response=response_obj,
            ib_order_id=response_obj.get("orderId"),
            ib_perm_id=response_obj.get("permId"),
            status=response_obj.get("status"),
            filled=response_obj.get("filled"),
            remaining=response_obj.get("remaining"),
            avg_fill_price=response_obj.get("avgFillPrice"),
            warning_text=response_obj.get("warningText"),
        )
        sess.add(row); sess.commit(); sess.refresh(row)
        return row.id
    finally:
        sess.close()
