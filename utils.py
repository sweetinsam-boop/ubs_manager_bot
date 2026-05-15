from __future__ import annotations

import json
import logging
import datetime
import pytz
from pathlib import Path
from storage.file_store import load_json, save_json
from storage.paths import get_path, CYCLE_STATE_FILE, TRADE_LOCK_FILE, CUMUL_PROFIT_FILE, CHAT_ID_FILE, PERFORMANCE_LOG_FILE, LAST_SETTLEMENT_FILE, LAST_KNOWN_HOLD_AVGS_FILE

log = logging.getLogger(__name__)

# --- 시간 유틸 ---

def get_us_today() -> str:
    """미국 동부 시간 기준 오늘 날짜 (YYYYMMDD)"""
    tz = pytz.timezone("America/New_York")
    return datetime.datetime.now(tz).strftime("%Y%m%d")

def get_market_session() -> str:
    """현재 미국 시장 세션 (pre, regular, post, closed)"""
    est = pytz.timezone("US/Eastern")
    now = datetime.datetime.now(est)
    
    if now.weekday() >= 5: return "closed"
    
    # 정규장: 09:30 ~ 16:00
    # 프리마켓: 04:00 ~ 09:30
    # 애프터마켓: 16:00 ~ 20:00
    hm = now.hour * 100 + now.minute
    if 400 <= hm < 930: return "pre"
    if 930 <= hm < 1600: return "regular"
    if 1600 <= hm < 2000: return "post"
    return "closed"

def get_market_session_kr() -> str:
    s = get_market_session()
    mapping = {"pre": "프리마켓", "regular": "정규장", "post": "애프터마켓", "closed": "장종료"}
    return mapping.get(s, "알수없음")

def is_market_open_today() -> bool:
    """오늘 미국 시장이 열리는 날인지 체크 (주말 제외)"""
    est = pytz.timezone("US/Eastern")
    return datetime.datetime.now(est).weekday() < 5


# --- 사이클/상태 관리 유틸 ---

def set_trade_lock(ticker: str):
    locks = load_json(get_path(TRADE_LOCK_FILE), default={})
    locks[ticker] = get_us_today()
    save_json(get_path(TRADE_LOCK_FILE), locks)

def check_trade_lock(ticker: str) -> bool:
    locks = load_json(get_path(TRADE_LOCK_FILE), default={})
    return locks.get(ticker) == get_us_today()

def load_cycle_state():
    return load_json(get_path(CYCLE_STATE_FILE), default={})

def update_cycle_info(ticker: str, start_date: str = None):
    state = load_cycle_state()
    if ticker not in state:
        state[ticker] = {"current_cycle": 1, "active": {"start_date": start_date or get_us_today()}}
    save_json(get_path(CYCLE_STATE_FILE), state)

def get_cycle_timeline(ticker: str) -> dict:
    state = load_cycle_state()
    info = state.get(ticker, {})
    return {
        "current_cycle": info.get("current_cycle", 1),
        "current_start": info.get("active", {}).get("start_date", "-"),
        "previous_start": info.get("last_finished", {}).get("start_date", "-"),
        "previous_end": info.get("last_finished", {}).get("end_date", "-")
    }


# --- 수익 기록/성과 유틸 ---

def load_cumul_profit():
    return load_json(get_path(CUMUL_PROFIT_FILE), default={"history": [], "processed_orders": []})

def save_cumul_profit(data):
    save_json(get_path(CUMUL_PROFIT_FILE), data)

def aggregate_cumul_profit_by_ticker(cp_data: dict) -> dict[str, float]:
    res = {}
    for h in cp_data.get("history", []):
        tkr = h.get("ticker")
        if tkr:
            res[tkr] = res.get(tkr, 0.0) + h.get("profit", 0.0)
    return res

def log_daily_performance(equity: float, spy_p: float, qqq_p: float):
    path = get_path(PERFORMANCE_LOG_FILE)
    date_str = get_us_today()
    row = f"{date_str},{equity:.2f},{spy_p:.2f},{qqq_p:.2f}\n"
    
    if not path.exists():
        with open(path, "w") as f:
            f.write("date,total_equity,spy_price,qqq_price\n")
            
    with open(path, "a") as f:
        f.write(row)

# --- 기타 영속성 ---

def save_chat_id(chat_id: int):
    save_json(get_path(CHAT_ID_FILE), {"chat_id": chat_id})

def load_chat_id() -> int | None:
    data = load_json(get_path(CHAT_ID_FILE), default={})
    return data.get("chat_id")

def get_last_settlement_date() -> str:
    data = load_json(get_path(LAST_SETTLEMENT_FILE), default={})
    return data.get("date", "")

def set_last_settlement_date(ymd: str):
    save_json(get_path(LAST_SETTLEMENT_FILE), {"date": ymd})

def load_last_known_hold_avgs():
    return load_json(get_path(LAST_KNOWN_HOLD_AVGS_FILE), default={})

def save_last_known_hold_avgs(data):
    save_json(get_path(LAST_KNOWN_HOLD_AVGS_FILE), data)
