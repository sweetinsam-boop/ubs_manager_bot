from __future__ import annotations

import datetime
from storage.file_store import load_json, save_json
from storage.paths import get_path, V4_STATE_FILE
import utils

def load_v4_state():
    return load_json(get_path(V4_STATE_FILE), default={})

def save_v4_state(state):
    save_json(get_path(V4_STATE_FILE), state)

def ensure_t(ticker, s_cfg, d_kis, t_seed):
    """V4.0 회차 T값 관리 (JSON 저장소 우선, 없으면 시드 기반 역산)"""
    state = load_v4_state()
    if ticker in state and "t" in state[ticker]:
        return state[ticker]["t"]
    return t_seed

def is_reverse(ticker):
    state = load_v4_state()
    return state.get(ticker, {}).get("mode") == "REVERSE"

def enter_reverse(ticker, ymd, frozen_rem=0.0):
    state = load_v4_state()
    state[ticker] = {
        "mode": "REVERSE",
        "start_ymd": ymd,
        "frozen_rem": frozen_rem,
        "sell_proceeds": 0.0,
        "t": 1.0 # 리버스 1회차부터 시작
    }
    save_v4_state(state)

def reverse_start_ymd(ticker):
    state = load_v4_state()
    return state.get(ticker, {}).get("start_ymd")

def get_reverse_sell_proceeds(ticker):
    state = load_v4_state()
    return state.get(ticker, {}).get("sell_proceeds", 0.0)
