from __future__ import annotations

import datetime
import logging
import pytz
import yfinance as yf
import kis_client
from storage.file_store import load_json, save_json
from storage.paths import get_path, V3_STATE_FILE

log = logging.getLogger(__name__)

def load_v3_state():
    return load_json(get_path(V3_STATE_FILE), default={})

def save_v3_state(state):
    save_json(get_path(V3_STATE_FILE), state)

def seed_if_needed(ticker: str, avg: float, qty: float):
    """V3.0 종목의 초기 cumul(누적매수금) 설정"""
    state = load_v3_state()
    if ticker not in state or state[ticker].get("cumul", 0) <= 0:
        if qty > 0:
            state[ticker] = {"cumul": avg * qty, "last_sync": datetime.datetime.now().isoformat()}
            save_v3_state(state)

def get_buy_cumul(ticker: str) -> float:
    state = load_v3_state()
    return state.get(ticker, {}).get("cumul", 0.0)

def reconcile_recent_buys(ticker: str):
    """최근 14일간의 실제 매수 체결 내역을 합산하여 V3.0 누적 매수금 보정"""
    try:
        odnos = kis_client.collect_recent_buy_odnos_with_fill(ticker)
        total = 0.0
        seen = set()
        for od in odnos:
            if od in seen: continue
            total += kis_client.sum_buy_notional_for_order(od, ticker)
            seen.add(od)
        
        if total > 0:
            state = load_v3_state()
            state[ticker] = {"cumul": total, "last_reconcile": datetime.datetime.now().isoformat()}
            save_v3_state(state)
            log.info(f"{ticker} V3.0 Reconciled: ${total:,.2f}")
    except Exception as e:
        log.error(f"{ticker} V3.0 Reconcile Error: {e}")
