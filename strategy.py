from __future__ import annotations

import datetime
import math
import pytz
import v3_state
import v4_state
from bot_logging import get_logger
from storage.file_store import load_json, save_json
from storage.paths import get_path, CUMUL_PROFIT_FILE
import utils

log = get_logger(__name__)


def normalize_strategy_method(raw) -> str:
    m = str(raw or "2.2").strip().lower()
    if m in ("4.0", "4", "v4", "v4.0"):
        return "4.0"
    if m in ("3.0", "3", "v3", "v3.0"):
        return "3.0"
    return "2.2"


def format_method_display(method_raw) -> str:
    m = normalize_strategy_method(method_raw)
    return f"V{m}"


def one_time_for_strategy(s: dict) -> float:
    """1회 매수금액 산출 (고정금액 one_time_usd 우선, 없으면 seed/total_a)"""
    ot = s.get("one_time_usd")
    if ot is not None and float(ot) > 0:
        return float(ot)
    seed = float(s.get("seed", 0))
    total_a = float(s.get("total_a", 1))
    return seed / total_a if total_a > 0 else 0.0


def t_value_v30(ticker: str, one_time: float) -> float:
    """V3.0 회차 T: (실매수 누적 USD) / (1회 매수금)"""
    if one_time <= 0:
        return 0.0
    cumul = v3_state.get_buy_cumul(ticker)
    return cumul / one_time


def star_percent_v30(ticker: str, t: float) -> float:
    """V3.0 별%: 종목별(TQQQ/SOXL) 및 회차(T)에 따른 고정 수식"""
    t_ceil = math.ceil(round(t, 2))
    t_int = int(t_ceil)
    
    if ticker.upper() == "TQQQ":
        if t_int <= 10: return 10.0
        if t_int <= 15: return 5.0
        return 3.0
    elif ticker.upper() == "SOXL":
        if t_int <= 10: return 12.0
        if t_int <= 15: return 7.0
        return 5.0
    return 10.0


def _v4_calc_target_p_and_loc(ticker: str) -> tuple[float, float]:
    """V4.0 종목별 지정가 익절%와 LOC 매도% (TQQQ/SOXL 전용)"""
    tk = ticker.upper()
    if tk == "TQQQ":
        return 10.0, 5.0
    if tk == "SOXL":
        return 12.0, 7.0
    return 10.0, 5.0


def build_order_info(ticker: str) -> dict:
    """종목별 현재 상태와 지시서 정보를 구성합니다."""
    from state_manager import STRATEGY
    import kis_client

    s = STRATEGY.get(ticker)
    if not s:
        return {}

    method = s.get("method", "2.2")
    d = s.get("data", {})
    avg = float(d.get("avg", 0) or 0)
    qty = float(d.get("qty", 0) or 0)
    cumul = float(d.get("cumul", 0) or 0)
    seed = float(s.get("seed", 0) or 0)
    total_a = float(s.get("total_a", 0) or 0)
    target_p_orig = float(s.get("target_profit", 10.0) or 10.0)
    one_time = one_time_for_strategy(s)

    info = {
        "ticker": ticker,
        "method": method,
        "avg_price": avg,
        "qty_total": qty,
        "seed": seed,
        "total_a": total_a,
        "target_profit_pct": target_p_orig,
        "one_time_usd": one_time,
        "b1": 0, "b2": 0, "star_price": 0.0, "target_price": 0.0,
        "qty_3_4": 0, "qty_1_4": 0,
        "turbo_qty": 0, "turbo_price": 0.0,
        "grid_orders": [],
    }

    # 현재가 조회 (지시서 기준가)
    curr_p = kis_client.get_current_price(ticker)
    if curr_p <= 0:
        curr_p = kis_client.get_prev_close(ticker)
    info["first_buy_curr_price"] = curr_p

    # --- V4.0 로직 ---
    if method == "4.0":
        t_seed = (avg * qty / one_time) if one_time > 0 else 0.0
        t = v4_state.ensure_t(ticker, s, d, t_seed)
        info["t_value"] = t
        
        # 리버스 모드 체크
        is_rev = v4_state.is_reverse(ticker)
        if not is_rev and t >= total_a:
            v4_state.enter_reverse(ticker, utils.get_us_today(), frozen_rem=(seed - cumul))
            is_rev = True
            log.info(f"{ticker} V4.0: T={t:.2f} >= N={total_a} -> 리버스 모드 진입")

        # 1. 리버스 모드 지시서
        if is_rev:
            info["is_reverse"] = True
            info["reverse_start_ymd"] = v4_state.reverse_start_ymd(ticker)
            
            # 리버스 1일차: MOC 매도만
            if info["reverse_start_ymd"] == utils.get_us_today():
                info["v40_reverse_moc_qty"] = math.floor(qty * 0.1)
                info["v40_rev_mult"] = 0.95 # T -> T*0.95
            else:
                # 리버스 2일차~: 5일 이평 기반 LOC 매도 + 쿼터 매수
                info["v40_reverse_day2"] = True
                hist = kis_client.get_history_prices(ticker, limit=5)
                if hist:
                    ma5 = sum(hist) / len(hist)
                    info["star_price"] = round(ma5, 2)
                    info["v40_reverse_sell_qty"] = math.floor(qty * 0.1)
                    
                    # 쿼터 매수 (잔금 기준)
                    rem_cash = seed - cumul + v4_state.get_reverse_sell_proceeds(ticker)
                    info["b1"] = 0 # 리버스는 평단매수 없음
                    if total_a > t:
                        b2_usd = rem_cash / (total_a - t)
                        info["b2"] = math.floor(b2_usd / ma5) if ma5 > 0 else 0
                    else:
                        info["b2"] = 0
            return info

        # 2. 일반 모드 지시서
        target_p, loc_p = _v4_calc_target_p_and_loc(ticker)
        info["target_profit_pct"] = target_p
        info["target_price"] = round(avg * (1 + target_p / 100), 2)
        info["star_price"] = round(avg * (1 + loc_p / 100), 2)
        
        if qty > 0:
            info["qty_3_4"] = math.ceil(qty * 0.75)
            info["qty_1_4"] = int(qty - info["qty_3_4"])
        
        # 매수 (잔금 기반)
        rem_cash = seed - cumul
        if total_a > t:
            one_try = rem_cash / (total_a - t)
            if t < (total_a / 2): # 전반전
                info["b1"] = math.floor((one_try * 0.5) / avg) if avg > 0 else 0
                info["b2"] = math.floor((one_try * 0.5) / info["star_price"]) if info["star_price"] > 0 else 0
            else: # 후반전
                info["b1"] = 0
                info["b2"] = math.floor(one_try / info["star_price"]) if info["star_price"] > 0 else 0
        
        # 줍줍 (격자)
        if curr_p > 0:
            for i in range(1, 7):
                p = round(curr_p * (1 - 0.05 * i), 2)
                q = math.floor((one_time * 0.2) / p) if p > 0 else 0
                if q > 0: info["grid_orders"].append({"price": p, "qty": q})
        return info

    # --- V3.0 로직 ---
    if method == "3.0":
        t = t_value_v30(ticker, one_time)
        info["t_value"] = t
        star_p = star_percent_v30(ticker, t)
        info["star_percent"] = star_p
        
        # 별지점 및 목표가
        sp = round(avg * (1 + star_p / 100), 2) if avg > 0 else round(curr_p, 2)
        info["star_price"] = sp
        tp = round(avg * (1 + target_p_orig / 100), 2) if avg > 0 else 0.0
        info["target_price"] = tp
        
        if qty > 0:
            info["qty_3_4"] = math.ceil(qty * 0.75)
            info["qty_1_4"] = int(qty - info["qty_3_4"])

        # 매수 (T 기준)
        if t < (total_a / 2): # 전반전
            info["b1"] = math.floor((one_time * 0.5) / avg) if avg > 0 else 0
            info["b2"] = math.floor((one_time * 0.5) / sp) if sp > 0 else 0
        else: # 후반전
            info["b1"] = 0
            info["b2"] = math.floor(one_time / sp) if sp > 0 else 0
            
        # 줍줍 (6단 격자)
        if curr_p > 0:
            for i in range(1, 7):
                p = round(curr_p * (1 - 0.05 * i), 2)
                q = math.floor((one_time * 0.2) / p) if p > 0 else 0
                if q > 0: info["grid_orders"].append({"price": p, "qty": q})
        return info

    # --- V2.2 로직 (기본) ---
    t = (avg * qty / one_time) if one_time > 0 else 0.0
    info["t_value"] = t
    
    # 별% (간략화된 2.2 수식)
    star_p = max(3.0, target_p_orig - (t * 0.2))
    info["star_percent"] = star_p
    sp = round(avg * (1 + star_p / 100), 2) if avg > 0 else round(curr_p, 2)
    info["star_price"] = sp
    tp = round(avg * (1 + target_p_orig / 100), 2) if avg > 0 else 0.0
    info["target_price"] = tp

    if qty > 0:
        info["qty_3_4"] = math.ceil(qty * 0.75)
        info["qty_1_4"] = int(qty - info["qty_3_4"])

    # 가속매수
    if avg > 0 and curr_p > 0:
        info["turbo_price"] = round(min(avg, curr_p) * 0.95, 2)
        info["turbo_qty"] = math.floor(one_time / info["turbo_price"]) if info["turbo_price"] > 0 else 0

    # 매수
    if t < (total_a / 2):
        info["b1"] = math.floor((one_time * 0.5) / avg) if avg > 0 else 0
        info["b2"] = math.floor((one_time * 0.5) / sp) if sp > 0 else 0
    else:
        info["b1"] = 0
        info["b2"] = math.floor(one_time / sp) if sp > 0 else 0

    return info
