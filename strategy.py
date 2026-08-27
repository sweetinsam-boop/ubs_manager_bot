from __future__ import annotations

import math
import datetime
import pytz
import utils
import v4_state
from state_manager import STRATEGY, normalize_strategy_method
from kis_client import get_current_price, get_prev_close, get_avg_close_last_n_days
from utils import load_cumul_profit, save_cumul_profit

# 텔레그램 Markdown: 회차/T가 표시되는 지시서·요약에 붙이는 안내
T_VALUE_NOTE_FOR_TELEGRAM = (
    "ℹ️ **T·회차 안내:** (평단×보유수량)÷1회매수는 증권사 동기화 잔고 기준 **근사치**입니다. "
    "실제 누적 매수금과 다를 수 있으니 `/sync` 후 수치를 대조하세요.\n"
)
T_VALUE_NOTE_V40_FOR_TELEGRAM = (
    "ℹ️ **V4.0 T:** `v4_strategy_state.json`에 저장됩니다. 쿼터 LOC 매도·리버스 MOC 등은 **체결 누적 수량** 기준으로 T를 갱신합니다(미체결이면 `/sync` 시 재확인). "
    "리버스·일반 전환은 종가·평단 기준으로 자동 해제를 시도합니다(근사).\n"
)
T_VALUE_NOTE_V30_FOR_TELEGRAM = (
    "ℹ️ **V3.0 T:** `v3_buy_state.json` **실매수 누적 USD** ÷ 1회 매수액이며, **소수 둘째 자리까지 올림**합니다. "
    "`/sync` 시 증권사 잔고의 평단×수량으로 cumul을 **덮어쓰지 않습니다**.\n"
)


def _is_v40(s: dict) -> bool:
    return str(s.get("method", "2.2")).strip().lower() in ("4.0", "4", "v4", "v4.0")


def _is_v30(s: dict) -> bool:
    return str(s.get("method", "2.2")).strip().lower() in ("3.0", "3", "v3", "v3.0")


def format_method_display(method_raw) -> str:
    """`trading_settings` method → 표시용 라벨. None/빈값은 이전 기록용으로 '미기록'."""
    if method_raw is None or str(method_raw).strip() == "":
        return "미기록"
    m = normalize_strategy_method(method_raw)
    if m == "4.0":
        return "V4.0"
    if m == "3.0":
        return "V3.0"
    return "V2.2"


def one_time_for_strategy(s: dict) -> float:
    v = s.get("one_time_usd")
    if v is not None and float(v) > 0:
        return float(v)
    return s["seed"] / s["total_a"]


def _first_buy_order_info(ticker: str, method: str) -> dict | None:
    """
    새 사이클 첫 매수: 보유 0주일 때 1회분(seed/N) LOC 진입 주문 정보 생성.
    LOC 가격은 현재가 +10%로 설정하여 장 마감 시 종가 체결을 유도합니다.
    Returns: order_info dict 또는 None (현재가 조회 실패 시)
    """
    s = STRATEGY[ticker]
    one_time_amt = one_time_for_strategy(s)
    try:
        curr_price = get_current_price(ticker)
    except Exception:
        curr_price = 0.0
    # 현재가 조회 실패 시 직전 종가(prev_close)로 fallback
    if curr_price <= 0:
        try:
            curr_price = get_prev_close(ticker)
        except Exception:
            curr_price = 0.0
    if curr_price <= 0 or one_time_amt <= 0:
        return None
    first_qty = math.floor(one_time_amt / curr_price)
    if first_qty <= 0:
        return None
    # LOC 가격: 현재가 +10% (장 마감 시 종가로 체결, 상한을 넉넉히)
    first_price = round(curr_price * 1.10, 2)
    return {
        "avg_price": first_price,
        "star_price": 0.0,
        "star_buy_price": 0.0,
        "target_price": 0.0,
        "sell_loc_price": 0.0,
        "b1": first_qty,
        "b2": 0,
        "qty_total": 0,
        "qty_3_4": 0,
        "qty_1_4": 0,
        "t_val": 0.0,
        "is_first_half": True,
        "turbo_price": 0.0,
        "turbo_qty": 0,
        "no_turbo": True,
        "force_quarter_sell": False,
        "grid_orders": [],
        "one_time_amt": one_time_amt,
        "method": method,
        "is_first_buy": True,
        "first_buy_curr_price": curr_price,
    }


def t_value_v30(cumul: float, one_time: float) -> float:
    """라오어 V3.0: T = 매수누적액/1회매수액, 소수 둘째 자리 올림."""
    if one_time <= 0:
        return 0.0
    if cumul <= 0:
        return 0.0
    x = cumul / one_time
    return math.ceil(x * 100) / 100


def v30_star_base_coef(ticker: str, s: dict) -> tuple[float, float]:
    """별% = base - coef*T (TQQQ 15-1.5T, SOXL 20-2T, 그 외는 target_profit 기반)."""
    if ticker == "SOXL":
        return 20.0, 2.0
    if ticker == "TQQQ":
        return 15.0, 1.5
    tp = float(s.get("target_profit", 10.0))
    return tp, tp / 10.0


def v30_star_pct(ticker: str, t_val: float, s: dict) -> float:
    b, c = v30_star_base_coef(ticker, s)
    return b - c * t_val


def v30_limit_pct_3q(ticker: str, s: dict) -> float:
    """3/4 지정가 익절: SOXL +20%, TQQQ +15%, 그 외 target_profit."""
    if ticker == "SOXL":
        return 20.0
    if ticker == "TQQQ":
        return 15.0
    return float(s.get("target_profit", 10.0))


def profit_target_pct(ticker: str) -> float:
    """전량익절 판단용 목표 수익률(%)."""
    s = STRATEGY[ticker]
    if _is_v40(s) or _is_v30(s):
        return v30_limit_pct_3q(ticker, s)
    return float(s.get("target_profit", 10.0))


def v40_use_wide_split(total_a: float) -> bool:
    """분할≥30이면 40분할형 별% 계수(0.75T / T)."""
    return float(total_a) >= 30.0


def v40_star_pct(ticker: str, t_val: float, s: dict) -> float:
    n = float(s["total_a"])
    wide = v40_use_wide_split(n)
    if ticker == "SOXL":
        c = 1.0 if wide else 2.0
        return 20.0 - c * t_val
    if ticker == "TQQQ":
        c = 0.75 if wide else 1.5
        return 15.0 - c * t_val
    tp = float(s.get("target_profit", 10.0))
    return tp - (2.0 * tp / max(n, 1e-6)) * t_val


def v40_loc_divisor(total_a: float) -> int:
    return 20 if float(total_a) >= 30 else 10


def v40_implied_entry_t(cumul: float, seed: float, total_a: float) -> float:
    if seed <= 0 or total_a <= 0:
        return 0.0
    return t_value_v30(cumul, seed / total_a)


def _v4_try_exit_reverse(ticker: str, s: dict, d: dict) -> None:
    if not v4_state.is_reverse(ticker):
        return
    avg_p = float(d.get("avg", 0) or 0)
    if avg_p <= 0:
        return
    if ticker == "TQQQ":
        thr = 0.15
    elif ticker == "SOXL":
        thr = 0.20
    else:
        thr = min(float(s.get("target_profit", 10.0)) / 100.0, 0.5)
    try:
        px = float(get_current_price(ticker) or 0)
        if px > avg_p * (1 - thr):
            v4_state.exit_reverse(ticker)
    except Exception as e:
        import logging
        logging.getLogger("ubs.strategy").warning(
            "v4_try_exit_reverse price check failed for %s: %s", ticker, e
        )


def _v4_t_after_reverse_buy(ticker: str, N: float) -> None:
    rec = v4_state.get_record(ticker)
    t0 = float(rec.get("t") or 0.0)
    nt = t0 + (float(N) - t0) * 0.25
    v4_state.set_t(ticker, nt)


def _v30_quarter_or_endgame(t_val: float, total_a: float, qty: int) -> bool:
    if qty <= 0:
        return False
    if total_a > 0 and t_val > (total_a - 1):
        return True
    return False


def _generate_ticker_message_v30(ticker: str) -> str:
    s = STRATEGY[ticker]
    d = s["data"]
    one_time_amt = one_time_for_strategy(s)
    half_amt = one_time_amt / 2.0

    try:
        curr_price = get_current_price(ticker)
        avg_price = d["avg"]
        qty_total = int(d["qty"])
        profit_rate = (curr_price - avg_price) / avg_price * 100 if avg_price > 0 else 0.0

        cumul = float(d.get("cumul", 0) or 0)
        t_val = t_value_v30(cumul, one_time_amt)
        star_pct = round(v30_star_pct(ticker, t_val, s), 2) if t_val > 0 else round(v30_star_pct(ticker, 0.0, s), 2)
        star_price = avg_price * (1 + star_pct / 100) if avg_price > 0 else curr_price * 1.05
        lim_pct = v30_limit_pct_3q(ticker, s)
        target_price = avg_price * (1 + lim_pct / 100) if avg_price > 0 else curr_price * (1 + lim_pct / 100)
        qty_3_4 = math.floor(qty_total * 0.75)
        qty_1_4 = qty_total - qty_3_4

        msg = f"[{ticker} 지시서 - 무한매수법 3.0]\n"
        msg += f"현재가: ${curr_price:.2f} / 평단: ${avg_price:.2f} ({profit_rate:.2f}%)\n"
        msg += f"수량: {qty_total}주 / 회차 T: {t_val} / 별%: {star_pct:.2f}%\n"
        ot_note = " (one_time_usd 지정)" if s.get("one_time_usd") else ""
        msg += f"시드: ${s['seed']:.0f} (1회분: ${one_time_amt:.0f}{ot_note})\n"
        msg += f"--------------------------\n"

        if qty_total == 0:
            msg += f"[첫 매수] 1회분(${one_time_amt:.0f}) 약 {math.floor(one_time_amt / curr_price)}주 매수\n"
        elif _v30_quarter_or_endgame(t_val, s["total_a"], qty_total):
            msg += f"[쿼터·잔금소진] 최종회차: 매수 없음\n"
            msg += f"1. [MOC 매도] 1/4 물량 ({qty_1_4}주)\n"
            msg += f"2. [지정가 매도] 잔여 3/4 ({qty_3_4}주) 목표가 ${target_price:.2f} (+{lim_pct}%)\n"
        else:
            midpoint = s["total_a"] / 2
            is_first_half = t_val <= midpoint

            if is_first_half:
                b2 = math.floor(half_amt / star_price) if star_price > 0 else 0
                b1 = math.floor(half_amt / (avg_price if avg_price > 0 else curr_price)) + 1
                msg += f"[매수 - 전반전 (T≤{midpoint:.0f})]\n"
                msg += f"1. [LOC] 별%가격 ${star_price:.2f} 에 -> {b2}주 (0.5회분)\n"
                msg += f"2. [LOC] 평단가 ${avg_price:.2f} 에 -> {b1}주 (0.5회분+1주)\n\n"
            else:
                b2 = math.floor(one_time_amt / star_price) if star_price > 0 else 0
                msg += f"[매수 - 후반전 (T>{midpoint:.0f})]\n"
                msg += f"1. [LOC] 별%가격 ${star_price:.2f} 에 -> {b2}주 (1회분)\n\n"

            if avg_price > 0:
                base_qty = math.floor(one_time_amt / avg_price)
                msg += f"[줍줍 - 하락 대비 추가 LOC]\n"
                for i in range(1, 7):
                    grid_p = round(one_time_amt / (base_qty + i), 2)
                    if grid_p <= 0: break
                    msg += f"  - LOC ${grid_p:.2f} x 1주\n"
                msg += "\n"

            msg += f"[매도]\n"
            msg += f"1. [지정가] 3/4물량({qty_3_4}주) -> ${target_price:.2f} (+{lim_pct}%)\n"
            msg += f"2. [LOC] 1/4물량({qty_1_4}주) -> ${star_price:.2f} (별% 가격)\n"

        return msg + "\n" + T_VALUE_NOTE_FOR_TELEGRAM
    except Exception as e:
        return f"{ticker} 데이터 오류: {str(e)}\n"


def _generate_ticker_message_v40(ticker: str) -> str:
    s = STRATEGY[ticker]
    d = s["data"]
    try:
        curr_price = get_current_price(ticker)
        avg_price = float(d.get("avg", 0) or 0)
        qty_total = int(d.get("qty", 0))
        cumul = float(d.get("cumul", 0) or 0)
        seed = float(s["seed"])
        N = float(s["total_a"])
        profit_rate = (curr_price - avg_price) / avg_price * 100 if avg_price > 0 else 0.0

        _v4_try_exit_reverse(ticker, s, d)
        if v4_state.is_reverse(ticker) and qty_total <= 0:
            v4_state.exit_reverse(ticker)
        T = v4_state.ensure_t(ticker, s, d, v40_implied_entry_t(cumul, seed, N))
        if qty_total > 0 and T > N - 1 + 1e-9 and not v4_state.is_reverse(ticker):
            frozen = max(seed - cumul, 0.0)
            v4_state.enter_reverse(ticker, utils.get_us_today(), frozen)

        div = v40_loc_divisor(N)
        lim_pct = v30_limit_pct_3q(ticker, s)
        star_pct = round(v40_star_pct(ticker, T, s), 4)
        raw_star = avg_price * (1 + star_pct / 100) if avg_price > 0 else 0.0
        star_buy = max(raw_star - 0.01, 0.01) if raw_star > 0 else 0.0

        msg = f"[{ticker} 지시서 - 무한매수법 4.0]\n"
        msg += f"현재가: ${curr_price:.2f} / 평단: ${avg_price:.2f} ({profit_rate:.2f}%)\n"
        msg += f"수량: {qty_total}주 / 저장 T: {T:.4f} / 별%: {star_pct:.4f}%\n"
        msg += f"시드: ${seed:.0f} / 분할: {int(N)} / 리버스: {'예' if v4_state.is_reverse(ticker) else '아니오'}\n"
        msg += f"--------------------------\n"

        if v4_state.is_reverse(ticker):
            rday = v4_state.reverse_start_ymd(ticker)
            usd = utils.get_us_today()
            msg += f"[리버스모드] 시작일 기록: {rday}\n"
            if rday == usd:
                mq = qty_total // div if div else 0
                msg += f"· 첫날: MOC 매도만 {mq}주 (전체÷{div} 내림)\n"
            else:
                ma5 = get_avg_close_last_n_days(ticker, 5)
                if ma5 <= 0:
                    ma5 = float(get_prev_close(ticker) or 0)
                sq = qty_total // div if div else 0
                proceeds = v4_state.get_reverse_sell_proceeds(ticker)
                rem = max(v4_state.get_frozen_rem(ticker) + proceeds, 0.0)
                msg += f"· 5일종가평균 별지점: ${ma5:.2f}\n"
                msg += f"· LOC 매도 시도: {sq}주 @ 별지점\n"
                msg += f"· 쿼터매수: 잔금(rem={rem:.0f}$, 매도회수금 {proceeds:.0f}$ 포함)/4 를 별지점-0.01 아래 LOC\n"
            msg += f"· 리버스 종료: 종가가 평단 대비 TQQQ -15%·SOXL -20% 이내로 회복 시(현재가로 근사 확인)\n"
            msg += f"· 매도 후 T×{0.9 if div==10 else 0.95}, 쿼터매수 체결 시 T+=(N-T)×0.25\n"
            return msg + "\n" + T_VALUE_NOTE_V40_FOR_TELEGRAM

        rem = max(seed - cumul, 0.0)
        ot = rem / max(N - T, 1e-6) if qty_total > 0 else seed / max(N, 1e-6)
        half_amt = ot / 2.0
        target_price = avg_price * (1 + lim_pct / 100) if avg_price > 0 else 0.0
        qty_3_4 = math.floor(qty_total * 0.75)
        qty_1_4 = qty_total - qty_3_4

        if qty_total == 0:
            o0 = seed / N
            msg += f"[첫 매수] 1회분(시드÷분할 ${o0:.0f}) 약 {math.floor(o0 / curr_price)}주 LOC (10~15% 위 권장)\n"
        else:
            mid = N / 2.0
            is_first_half = T < mid
            if is_first_half:
                b2 = math.floor(half_amt / star_buy) if star_buy > 0 else 0
                b1 = math.floor(half_amt / avg_price) + 1 if avg_price > 0 else 0
                msg += f"[전반전 T<{mid:.0f}] 1회 시도액 ${ot:.2f} (잔금÷(N-T))\n"
                msg += f"· 별지점-0.01 LOC ${star_buy:.2f} → {b2}주 / 평단 LOC ${avg_price:.2f} → {b1}주\n"
            else:
                b2 = math.floor(ot / star_buy) if star_buy > 0 else 0
                msg += f"[후반전 T≥{mid:.0f}] 1회 시도액 ${ot:.2f}\n"
                msg += f"· 별지점-0.01 LOC ${star_buy:.2f} → {b2}주 (전액)\n"
            msg += "· 하락 대비 추가 LOC(줍줍)는 주문표와 동일하게 격자 사용\n"

        if qty_total > 0:
            msg += f"\n[매도·공통]\n"
            msg += f"· 지정가 3/4 ({qty_3_4}주) ${target_price:.2f} (+{lim_pct}%)\n"
            msg += f"· LOC 쿼터 1/4 ({qty_1_4}주) 별지점 ${raw_star:.2f}\n"
            msg += f"· 쿼터매도 시 T←T×0.75 (체결 누적 기준, 자동 주문 시 대기 후 미체결이면 동기화 시 재확인)\n"

        return msg + "\n" + T_VALUE_NOTE_FOR_TELEGRAM + T_VALUE_NOTE_V40_FOR_TELEGRAM
    except Exception as e:
        return f"{ticker} 데이터 오류: {str(e)}\n"


def generate_ticker_message(ticker):
    s = STRATEGY[ticker]
    if _is_v40(s):
        return _generate_ticker_message_v40(ticker)
    if _is_v30(s):
        return _generate_ticker_message_v30(ticker)
    d = s["data"]

    one_time_amt = s["seed"] / s["total_a"]
    half_amt = one_time_amt / 2.0

    try:
        curr_price = get_current_price(ticker)
        avg_price = d["avg"]
        qty_total = int(d["qty"])
        profit_rate = (curr_price - avg_price) / avg_price * 100 if avg_price > 0 else 0.0

        t_val = round(d["cumul"] / one_time_amt, 2) if one_time_amt > 0 and d["cumul"] > 0 else 0.0
        star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]
        star_price = avg_price * (1 + star_pct / 100) if avg_price > 0 else curr_price * 1.05

        target_price = avg_price * (1 + s["target_profit"] / 100) if avg_price > 0 else curr_price * (1 + s["target_profit"] / 100)
        qty_3_4 = math.floor(qty_total * 0.75)
        qty_1_4 = qty_total - qty_3_4

        msg = f"[{ticker} 지시서 - 목표 {s['target_profit']}%]\n"
        msg += f"현재가: ${curr_price:.2f} / 평단: ${avg_price:.2f} ({profit_rate:.2f}%)\n"
        msg += f"수량: {qty_total}주 / 회차: {t_val}T / 별%: {star_pct:.2f}%\n"
        msg += f"시드: ${s['seed']:.0f} (1회분: ${one_time_amt:.0f})\n"
        msg += f"--------------------------\n"

        if qty_total == 0:
            msg += f"[첫 매수] 1회분(${one_time_amt:.0f}) 약 {math.floor(one_time_amt / curr_price)}주 매수\n"
        elif t_val > (s["total_a"] - 1):
            msg += f"[매도] (쿼터손절 진행)\n"
            msg += f"1. [MOC 매도] 1/4 물량 ({qty_1_4}주)을 무조건 매도(MOC)\n"
            msg += f"2. [지정가 매도] 잔여 3/4 물량 ({qty_3_4}주)을 목표가 ${target_price:.2f} (+{s['target_profit']}%)에 예약\n"
        else:
            midpoint = s["total_a"] / 2
            is_first_half = (t_val <= midpoint)

            if is_first_half:
                b2 = math.floor(half_amt / star_price) if star_price > 0 else 0
                b1 = math.floor(half_amt / (avg_price if avg_price > 0 else curr_price)) + 1
                msg += f"[매수 - 전반전 (T≤{int(midpoint)})]\n"
                msg += f"1. [LOC] 별%가격 ${star_price:.2f} 에 -> {b2}주 (0.5회분)\n"
                msg += f"2. [LOC] 평단가 ${avg_price:.2f} 에 -> {b1}주 (0.5회분+1주)\n\n"
            else:
                b2 = math.floor(one_time_amt / star_price) if star_price > 0 else 0
                msg += f"[매수 - 후반전 (T>{int(midpoint)})]\n"
                msg += f"1. [LOC] 별%가격 ${star_price:.2f} 에 -> {b2}주 (1회분 전체)\n\n"

            try:
                prev_close = get_prev_close(ticker)
                base_price = min(avg_price, prev_close)
                turbo_price = base_price * 0.95
                turbo_qty = math.floor(one_time_amt / turbo_price)
                msg += f"[가속매수 - 1회분 추가 LOC]\n"
                msg += f"-> min(평단${avg_price:.2f}, 전일종가${prev_close:.2f})x0.95 = ${turbo_price:.2f} 에 {turbo_qty}주\n\n"
            except Exception:
                msg += f"[가속매수] 전일종가 조회 실패\n\n"

            base_qty = math.floor(one_time_amt / avg_price) if avg_price > 0 else 0
            msg += f"[줍줍 - 폭락장 대비 추가 매수]\n"
            for i in range(1, 7):
                grid_p = round(one_time_amt / (base_qty + i), 2)
                if grid_p <= 0: break
                msg += f"  - LOC ${grid_p:.2f} x 1주\n"

            msg += f"\n[매도]\n"
            msg += f"1. [지정가] 3/4물량({qty_3_4}주) -> ${target_price:.2f} (+{s['target_profit']}%)\n"
            msg += f"2. [LOC] 1/4물량({qty_1_4}주) -> ${star_price + 0.01:.2f} (별%+0.01)\n"

        return msg + "\n" + T_VALUE_NOTE_FOR_TELEGRAM
    except Exception as e:
        return f"{ticker} 데이터 오류: {str(e)}\n"

def _build_order_info_v40(ticker, no_turbo=False):
    """V4.0 일반모드 + 소진 후 리버스(첫날 MOC, 이후 5일평균 별지점)."""
    s = STRATEGY[ticker]
    d = s["data"]
    avg_price = float(d.get("avg", 0) or 0)
    qty_total = int(d.get("qty", 0))
    cumul = float(d.get("cumul", 0) or 0)
    seed = float(s["seed"])
    N = float(s["total_a"])

    _v4_try_exit_reverse(ticker, s, d)
    if v4_state.is_reverse(ticker) and qty_total <= 0:
        v4_state.exit_reverse(ticker)

    # 새 사이클 첫 매수: 보유 0주일 때 1회분 LOC 진입
    if qty_total == 0:
        fb = _first_buy_order_info(ticker, "4.0")
        if fb:
            return fb

    T = v4_state.ensure_t(ticker, s, d, v40_implied_entry_t(cumul, seed, N))

    if qty_total > 0 and T > N - 1 + 1e-9 and not v4_state.is_reverse(ticker):
        frozen = max(seed - cumul, 0.0)
        v4_state.enter_reverse(ticker, utils.get_us_today(), frozen)

    div = v40_loc_divisor(N)
    rev_mult = 0.9 if div == 10 else 0.95

    if v4_state.is_reverse(ticker):
        usd = utils.get_us_today()
        rday = v4_state.reverse_start_ymd(ticker)
        if rday == usd:
            moc_q = qty_total // div if div else 0
            return {
                "avg_price": avg_price,
                "star_price": 0.0,
                "star_buy_price": 0.0,
                "target_price": 0.0,
                "sell_loc_price": 0.0,
                "b1": 0,
                "b2": 0,
                "qty_total": qty_total,
                "qty_3_4": 0,
                "qty_1_4": 0,
                "t_val": T,
                "is_first_half": False,
                "turbo_price": 0.0,
                "turbo_qty": 0,
                "no_turbo": True,
                "force_quarter_sell": False,
                "grid_orders": [],
                "one_time_amt": 0.0,
                "method": "4.0",
                "v40_reverse": True,
                "v40_reverse_moc_qty": moc_q,
                "v40_rev_mult": rev_mult,
                "v40_total_a": N,
            }
        star_r = get_avg_close_last_n_days(ticker, 5)
        if star_r <= 0:
            try:
                star_r = float(get_prev_close(ticker) or 0)
            except Exception:
                star_r = 0.0
        if star_r <= 0:
            star_r = avg_price
        sell_q = qty_total // div if div else 0
        proceeds = v4_state.get_reverse_sell_proceeds(ticker)
        rem = max(v4_state.get_frozen_rem(ticker) + proceeds, 0.0)
        q_buy = rem / 4.0
        buy_px = max(star_r - 0.01, 0.01)
        b2 = math.floor(q_buy / buy_px) if buy_px > 0 else 0
        return {
            "avg_price": avg_price,
            "star_price": star_r,
            "star_buy_price": buy_px,
            "target_price": 0.0,
            "sell_loc_price": star_r,
            "b1": 0,
            "b2": b2,
            "qty_total": qty_total,
            "qty_3_4": 0,
            "qty_1_4": 0,
            "t_val": T,
            "is_first_half": False,
            "turbo_price": 0.0,
            "turbo_qty": 0,
            "no_turbo": True,
            "force_quarter_sell": False,
            "grid_orders": [],
            "one_time_amt": q_buy,
            "method": "4.0",
            "v40_reverse": True,
            "v40_reverse_day2": True,
            "v40_reverse_sell_qty": sell_q,
            "v40_rev_mult": rev_mult,
            "v40_total_a": N,
        }

    star_pct = v40_star_pct(ticker, T, s)
    raw_star = avg_price * (1 + star_pct / 100) if avg_price > 0 else 0.0
    star_buy = max(raw_star - 0.01, 0.01) if raw_star > 0 else 0.0
    target_price = avg_price * (1 + v30_limit_pct_3q(ticker, s) / 100) if avg_price > 0 else 0.0
    qty_3_4 = math.floor(qty_total * 0.75)
    qty_1_4 = qty_total - qty_3_4

    rem = max(seed - cumul, 0.0)
    ot = rem / max(N - T, 1e-6) if qty_total > 0 else seed / max(N, 1e-6)
    half_amt = ot / 2.0

    mid = N / 2.0
    is_first_half = T < mid
    b1, b2 = 0, 0
    if qty_total > 0 and T <= N - 1 + 1e-9:
        if is_first_half:
            b2 = math.floor(half_amt / star_buy) if star_buy > 0 else 0
            b1 = (math.floor(half_amt / avg_price) + 1) if avg_price > 0 else 0
        else:
            b2 = math.floor(ot / star_buy) if star_buy > 0 else 0
            b1 = 0

    grid_orders = []
    if avg_price > 0 and qty_total > 0 and T <= N - 1 + 1e-9:
        base_qty = max(math.floor(ot / avg_price), 1)
        for i in range(1, 7):
            gp = round(ot / (base_qty + i), 2)
            if gp > 0:
                grid_orders.append({"price": gp, "qty": 1})

    return {
        "avg_price": avg_price,
        "star_price": raw_star,
        "star_buy_price": star_buy,
        "target_price": target_price,
        "sell_loc_price": raw_star,
        "b1": b1,
        "b2": b2,
        "qty_total": qty_total,
        "qty_3_4": qty_3_4,
        "qty_1_4": qty_1_4,
        "t_val": T,
        "is_first_half": is_first_half,
        "turbo_price": 0.0,
        "turbo_qty": 0,
        "no_turbo": True,
        "force_quarter_sell": False,
        "grid_orders": grid_orders,
        "one_time_amt": ot,
        "method": "4.0",
        "v40_total_a": N,
    }


def _build_order_info_v30(ticker, no_turbo: bool = True):
    """무한매수법 3.0 (라오어 카페 V3.0): T 올림, 별%/쿼터 규칙, 격자 포함, 가속 없음."""
    s = STRATEGY[ticker]
    d = s["data"]
    one_time_amt = one_time_for_strategy(s)
    half_amt = one_time_amt / 2.0
    avg_price = d.get("avg", 0)
    qty_total = int(d.get("qty", 0))

    # 새 사이클 첫 매수: 보유 0주일 때 1회분 LOC 진입
    if qty_total == 0:
        fb = _first_buy_order_info(ticker, "3.0")
        if fb:
            return fb

    cumul = float(d.get("cumul", 0) or 0)
    t_val = t_value_v30(cumul, one_time_amt)

    star_pct = v30_star_pct(ticker, t_val, s) if t_val > 0 else v30_star_pct(ticker, 0.0, s)
    star_pct = round(star_pct, 6)
    star_price = avg_price * (1 + star_pct / 100) if avg_price > 0 else 0
    lim_pct = v30_limit_pct_3q(ticker, s)
    target_price = avg_price * (1 + lim_pct / 100) if avg_price > 0 else 0
    qty_3_4 = math.floor(qty_total * 0.75)
    qty_1_4 = qty_total - qty_3_4
    sell_loc_price = star_price

    force_quarter_sell = _v30_quarter_or_endgame(t_val, s["total_a"], qty_total)

    midpoint = s["total_a"] / 2
    is_first_half = t_val <= midpoint
    b1, b2 = 0, 0
    if not force_quarter_sell and qty_total > 0:
        if is_first_half:
            b2 = math.floor(half_amt / star_price) if star_price > 0 else 0
            b1 = (math.floor(half_amt / avg_price) + 1) if avg_price > 0 else 0
        else:
            b2 = math.floor(one_time_amt / star_price) if star_price > 0 else 0
            b1 = 0

    # V3.0 격자(줍줍): 하락 대비 1회 정액 매수를 맞추기 위한 추가 LOC
    grid_orders = []
    if avg_price > 0 and qty_total > 0 and not force_quarter_sell:
        base_qty = math.floor(one_time_amt / avg_price)
        for i in range(1, 7):
            gp = round(one_time_amt / (base_qty + i), 2)
            if gp > 0:
                grid_orders.append({"price": gp, "qty": 1})

    return {
        "avg_price": avg_price,
        "star_price": star_price,
        "star_buy_price": star_price,
        "target_price": target_price,
        "sell_loc_price": sell_loc_price,
        "b1": b1,
        "b2": b2,
        "qty_total": qty_total,
        "qty_3_4": qty_3_4,
        "qty_1_4": qty_1_4,
        "t_val": t_val,
        "is_first_half": is_first_half,
        "turbo_price": 0.0,
        "turbo_qty": 0,
        "no_turbo": True,
        "force_quarter_sell": force_quarter_sell,
        "grid_orders": grid_orders,
        "one_time_amt": one_time_amt,
        "method": "3.0",
    }


def build_order_info(ticker, no_turbo=False):
    s = STRATEGY[ticker]
    if _is_v40(s):
        return _build_order_info_v40(ticker, no_turbo=True)
    if _is_v30(s):
        return _build_order_info_v30(ticker, no_turbo=True)

    d = s["data"]
    one_time_amt = s["seed"] / s["total_a"]
    half_amt = one_time_amt / 2.0
    avg_price = d.get("avg", 0)
    qty_total = int(d.get("qty", 0))

    # 새 사이클 첫 매수: 보유 0주일 때 1회분 LOC 진입
    if qty_total == 0:
        fb = _first_buy_order_info(ticker, "2.2")
        if fb:
            return fb

    t_val = round(d.get("cumul", 0) / one_time_amt, 2) if one_time_amt > 0 and d.get("cumul", 0) > 0 else 0.0
    star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]
    star_price = avg_price * (1 + star_pct / 100) if avg_price > 0 else 0
    target_price = avg_price * (1 + s["target_profit"] / 100) if avg_price > 0 else 0
    sell_loc_price = star_price + 0.01 if star_price > 0 else 0.0
    qty_3_4 = math.floor(qty_total * 0.75)
    qty_1_4 = qty_total - qty_3_4

    midpoint = s["total_a"] / 2
    is_first_half = (t_val <= midpoint)

    if is_first_half:
        b2 = math.floor(half_amt / star_price) if star_price > 0 else 0
        b1 = (math.floor(half_amt / avg_price) + 1) if avg_price > 0 else 0
    else:
        b2 = math.floor(one_time_amt / star_price) if star_price > 0 else 0
        b1 = 0

    grid_orders = []
    if avg_price > 0:
        base_qty = math.floor(one_time_amt / avg_price)
        for i in range(1, 7):
            grid_p = round(one_time_amt / (base_qty + i), 2)
            if grid_p > 0:
                grid_orders.append({"price": grid_p, "qty": 1})

    turbo_price = 0.0
    turbo_qty = 0
    if not no_turbo and avg_price > 0 and qty_total > 0:
        try:
            prev_close = get_prev_close(ticker)
            base_price = min(avg_price, prev_close)
            turbo_price = base_price * 0.95
            turbo_qty = math.floor(one_time_amt / turbo_price)
        except Exception:
            pass

    force_quarter_sell = (t_val > s["total_a"] - 1)

    return {
        "avg_price": avg_price, "star_price": star_price, "target_price": target_price,
        "sell_loc_price": sell_loc_price,
        "b1": b1, "b2": b2, "qty_total": qty_total, "qty_3_4": qty_3_4, "qty_1_4": qty_1_4,
        "t_val": t_val, "is_first_half": is_first_half, "turbo_price": turbo_price,
        "turbo_qty": turbo_qty, "no_turbo": no_turbo, "force_quarter_sell": force_quarter_sell,
        "grid_orders": grid_orders,
        "one_time_amt": one_time_amt,
        "method": "2.2",
    }

def estimate_required_amount(info, seed, total_a):
    if info.get("force_quarter_sell"):
        return 0.0
    total = 0.0
    one_time = float(info.get("one_time_amt") or (seed / total_a if total_a else 0))
    if info["b1"] > 0:
        total += info["b1"] * info["avg_price"]
    if info["b2"] > 0:
        b2px = info.get("star_buy_price")
        px = float(b2px) if b2px is not None else float(info.get("star_price") or 0)
        total += info["b2"] * px
    if not info.get("no_turbo", False) and info.get("turbo_qty", 0) > 0:
        total += info["turbo_qty"] * info["turbo_price"]
    if info.get("v40_reverse_moc_qty"):
        return 0.0

    if info["avg_price"] > 0:
        base_qty = math.floor(one_time / info["avg_price"])
        for i in range(1, 7):
            gp = one_time / (base_qty + i)
            if gp > 0:
                total += gp
    return total

def _order_no_from_res(res) -> str:
    if not res or str(res.get("rt_cd", "")) != "0":
        return ""
    out = res.get("output") or {}
    if not isinstance(out, dict):
        out = {}
    return str(out.get("ODNO") or out.get("odno") or "").strip()


def v4_try_resolve_pending_moc(ticker: str) -> str | None:
    """저장된 리버스 MOC pending이 체결되었으면 T 반영 후 제거. 메시지 또는 None."""
    from kis_client import sum_ccld_qty_for_order, sum_sell_notional_for_order

    pm = v4_state.get_pending_moc(ticker)
    if not pm:
        return None
    od = str(pm.get("odno") or "").strip()
    mq = int(pm.get("min_qty") or 0)
    mult = float(pm.get("mult") or 0.95)
    if not od or mq <= 0:
        return None
    total = sum_ccld_qty_for_order(od, ticker)
    if total >= mq:
        v4_state.multiply_t(ticker, mult)
        sell_amt = sum_sell_notional_for_order(od, ticker)
        if sell_amt > 0:
            v4_state.add_reverse_sell_proceeds(ticker, sell_amt)
        v4_state.clear_pending_moc(ticker)
        return f"{ticker} V4.0: pending MOC 체결 반영 T×{mult} ({total}주, 매도회수금 ${sell_amt:.2f})"
    return None


def _execute_all_order_v40_normal(ticker: str, info: dict) -> str:
    """V4.0 일반: 매도(지정가→쿼터 LOC) 선행 후 매수·격자.
    LOC/지정가는 장 마감 시 체결되므로 주문 전송만 수행하고,
    T 갱신은 장 마감 후 post_close_sync_job → ensure_t에서 반영합니다."""
    from kis_client import send_order

    avg_p = float(info["avg_price"])
    star_buy_p = float(info.get("star_buy_price") or info["star_price"])
    target_p = float(info["target_price"])
    b1_qty = int(info["b1"])
    b2_qty = int(info["b2"])
    q_3_4 = int(info["qty_3_4"])
    q_1_4 = int(info["qty_1_4"])
    raw_star = float(info.get("star_price") or 0)
    loc_px = float(info.get("sell_loc_price") or (raw_star + 0.01 if raw_star > 0 else 0.0))
    grid_list = info.get("grid_orders", [])

    def get_status_tag(res):
        if not res:
            return "미실행"
        if res.get("rt_cd") == "0":
            return "성공"
        return f"실패({res.get('msg1', '')})"

    success_count = 0

    def tag_and_count(res):
        nonlocal success_count
        t = get_status_tag(res)
        if res and res.get("rt_cd") == "0":
            success_count += 1
        return t

    res_msg = f"[{ticker} 주문 결과 - V4.0]\n"

    if q_3_4 > 0 and target_p > 0:
        res_s1 = send_order(ticker, q_3_4, target_p, "SELL", "00")
        res_msg += f"  지정가매도: {tag_and_count(res_s1)}\n"

    if q_1_4 > 0 and loc_px > 0:
        res_s2 = send_order(ticker, q_1_4, loc_px, "SELL", "LOC")
        res_msg += f"  LOC매도(쿼터): {tag_and_count(res_s2)}\n"

    for idx, g in enumerate(grid_list):
        res_g = send_order(ticker, g["qty"], g["price"], "BUY", "LOC")
        if res_g.get("rt_cd") != "0":
            res_msg += f"  줍줍{idx+1}({g['price']}): {get_status_tag(res_g)}\n"
        else:
            success_count += 1

    if b1_qty > 0 and avg_p > 0:
        res1 = send_order(ticker, b1_qty, avg_p, "BUY", "LOC")
        res_msg += f"  평단매수: {tag_and_count(res1)}\n"

    if b2_qty > 0 and star_buy_p > 0:
        res2 = send_order(ticker, b2_qty, star_buy_p, "BUY", "LOC")
        res_msg += f"  별가매수: {tag_and_count(res2)}\n"

    if success_count > 0:
        res_msg += f"\n✅ 총 {success_count}건의 주문이 성공적으로 접수되었습니다."
    return res_msg


def _execute_all_order_v30(ticker: str, info: dict) -> str:
    """V3.0: 주문 전송만 수행. 체결 확인 및 V3 cumul 반영은 장 마감 후 동기화 시 처리."""
    from kis_client import send_order

    avg_p = float(info["avg_price"])
    star_p = float(info["star_price"])
    star_buy_p = float(info.get("star_buy_price") or star_p)
    target_p = float(info["target_price"])
    b1_qty = int(info["b1"])
    b2_qty = int(info["b2"])
    q_3_4 = int(info["qty_3_4"])
    q_1_4 = int(info["qty_1_4"])

    def get_status_tag(res):
        if not res:
            return "미실행"
        if res.get("rt_cd") == "0":
            return "성공"
        return f"실패({res.get('msg1', '')})"

    success_count = 0

    def tag_and_count(res):
        nonlocal success_count
        t = get_status_tag(res)
        if res and res.get("rt_cd") == "0":
            success_count += 1
        return t

    res_msg = f"[{ticker} 주문 결과 - V3.0]\n"

    if b1_qty > 0 and avg_p > 0:
        res1 = send_order(ticker, b1_qty, avg_p, "BUY", "LOC")
        res_msg += f"  평단매수: {tag_and_count(res1)}\n"

    if b2_qty > 0 and star_buy_p > 0:
        res2 = send_order(ticker, b2_qty, star_buy_p, "BUY", "LOC")
        res_msg += f"  별가매수: {tag_and_count(res2)}\n"

    # V3.0 격자(줍줍) 주문
    grid_list = info.get("grid_orders", [])
    for idx, g in enumerate(grid_list):
        res_g = send_order(ticker, g["qty"], g["price"], "BUY", "LOC")
        if res_g.get("rt_cd") != "0":
            res_msg += f"  줍줍{idx+1}({g['price']}): {get_status_tag(res_g)}\n"
        else:
            success_count += 1

    if q_3_4 > 0 and target_p > 0:
        res_s1 = send_order(ticker, q_3_4, target_p, "SELL", "00")
        res_msg += f"  지정가매도: {tag_and_count(res_s1)}\n"

    if q_1_4 > 0:
        loc_px = float(info.get("sell_loc_price", star_p + 0.01))
        res_s2 = send_order(ticker, q_1_4, loc_px, "SELL", "LOC")
        res_msg += f"  LOC매도: {tag_and_count(res_s2)}\n"

    if success_count > 0:
        res_msg += f"\n✅ 총 {success_count}건의 주문이 성공적으로 접수되었습니다."
    return res_msg


def add_cumul_profit(ticker, profit_amount, sell_qty, sell_price, avg_price, order_no="", method=None):
    """
    익절 실현손익을 누적 파일에 반영합니다.
    Returns:
        (ticker별 누적 합계, 이번 호출에서 신규 반영 여부)
    """
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return 0.0, False
    if sell_qty <= 0 or avg_price <= 0:
        return float(load_cumul_profit().get(ticker, 0.0)), False
    data = load_cumul_profit()
    processed = [str(x).strip() for x in (data.get("processed_orders") or [])]
    on = str(order_no or "").strip()
    if on and on in processed:
        return float(data.get(ticker, 0.0)), False

    rec_method = normalize_strategy_method(
        method if method is not None else STRATEGY.get(ticker, {}).get("method", "2.2")
    )

    data[ticker] = float(data.get(ticker, 0.0)) + float(profit_amount)
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.datetime.now(kst).strftime('%Y-%m-%d %H:%M')
    data["history"].append({
        "date": now_kst, "ticker": ticker, "profit": round(profit_amount, 2),
        "sell_qty": sell_qty, "sell_price": round(sell_price, 2),
        "avg_price": round(avg_price, 2), "cumul_total": round(data[ticker], 2),
        "order_no": on,
        "method": rec_method,
    })
    
    if on:
        processed.append(on)
        if len(processed) > 200:
            processed = processed[-200:]
        data["processed_orders"] = processed
    if len(data["history"]) > 100:
        data["history"] = data["history"][-100:]

    save_cumul_profit(data)
    return float(data[ticker]), True

def execute_all_order(ticker, info):
    from kis_client import send_order
    avg_p = info["avg_price"]
    star_p = info["star_price"]
    star_buy_p = float(info.get("star_buy_price") or star_p)
    target_p = info["target_price"]
    b1_qty = info["b1"]
    b2_qty = info["b2"]
    q_3_4 = info["qty_3_4"]
    q_1_4 = info["qty_1_4"]
    turbo_qty = info["turbo_qty"]
    turbo_price = info["turbo_price"]

    def get_status_tag(res):
        if not res: return "미실행"
        rt = res.get('rt_cd')
        if rt == '0': return "성공"
        return f"실패({res.get('msg1','')})"

    res_msg = f"[{ticker} 주문 결과]\n"
    success_count = 0
    
    def get_tag_and_count(res):
        tag = get_status_tag(res)
        nonlocal success_count
        if res and res.get('rt_cd') == '0':
            success_count += 1
        return tag

    # V4.0 리버스 첫날: MOC 매도 → pending 저장 (장 마감 후 sync 시 resolve)
    mq = int(info.get("v40_reverse_moc_qty") or 0)
    if mq > 0:
        rev_mult = float(info.get("v40_rev_mult", 0.95))
        res_msg = f"[{ticker} 주문 결과 - V4.0 리버스 첫날]\n"
        res_m = send_order(ticker, mq, 0, "SELL", "MOC")
        res_msg += f"  MOC분할매도: {get_tag_and_count(res_m)}\n"
        od_m = _order_no_from_res(res_m)
        if od_m and res_m and res_m.get("rt_cd") == "0":
            v4_state.set_pending_moc(ticker, od_m, mq, rev_mult)
            res_msg += f"  → 장 마감 후 체결 예정: pending 저장 완료. `/sync`로 체결 확인\n"
        if success_count > 0:
            res_msg += f"\n✅ 총 {success_count}건의 주문이 성공적으로 접수되었습니다."
        return res_msg

    # V4.0 리버스 2일차~: LOC 매도 + 쿼터매수 (체결 확인은 장 마감 후)
    if info.get("v40_reverse_day2"):
        res_msg = f"[{ticker} 주문 결과 - V4.0 리버스]\n"
        sq = int(info.get("v40_reverse_sell_qty", 0))
        sx = float(info.get("star_price") or 0)
        if sq > 0 and sx > 0:
            res_s = send_order(ticker, sq, sx, "SELL", "LOC")
            res_msg += f"  리버스LOC매도: {get_tag_and_count(res_s)}\n"
        if b1_qty > 0:
            res1 = send_order(ticker, b1_qty, avg_p, "BUY", "LOC")
            res_msg += f"  평단매수: {get_tag_and_count(res1)}\n"
        if b2_qty > 0:
            res2 = send_order(ticker, b2_qty, star_buy_p, "BUY", "LOC")
            res_msg += f"  쿼터매수: {get_tag_and_count(res2)}\n"
        if success_count > 0:
            res_msg += f"\n✅ 총 {success_count}건의 주문이 성공적으로 접수되었습니다."
        return res_msg

    if info.get("method") == "4.0":
        return _execute_all_order_v40_normal(ticker, info)

    if info.get("method") == "3.0":
        return _execute_all_order_v30(ticker, info)

    # 매수 주문들 (2.2)
    if b1_qty > 0:
        res1 = send_order(ticker, b1_qty, avg_p, "BUY", "LOC")
        res_msg += f"  평단매수: {get_tag_and_count(res1)}\n"

    if b2_qty > 0:
        res2 = send_order(ticker, b2_qty, star_buy_p, "BUY", "LOC")
        res_msg += f"  별가매수: {get_tag_and_count(res2)}\n"

    if turbo_qty > 0:
        res_t = send_order(ticker, turbo_qty, turbo_price, "BUY", "LOC")
        res_msg += f"  가속매수: {get_tag_and_count(res_t)}\n"

    # 줍줍(Grid) 주문들
    grid_list = info.get("grid_orders", [])
    for idx, g in enumerate(grid_list):
        res_g = send_order(ticker, g["qty"], g["price"], "BUY", "LOC")
        # 격자 주문은 성공 시 표시하지 않고 실패 시만 표시하거나, 
        # 전체 성공 개수만 보여주는 것이 깔끔함 (사용자 요청에 따라)
        if res_g.get('rt_cd') != '0':
            res_msg += f"  줍줍{idx+1}({g['price']}): {get_status_tag(res_g)}\n"
        else:
            success_count += 1

    # 매도 주문들
    if q_3_4 > 0:
        res_s1 = send_order(ticker, q_3_4, target_p, "SELL", "00")
        res_msg += f"  지정가매도: {get_tag_and_count(res_s1)}\n"

    if q_1_4 > 0:
        loc_px = float(info.get("sell_loc_price", star_p + 0.01))
        res_s2 = send_order(ticker, q_1_4, loc_px, "SELL", "LOC")
        res_msg += f"  LOC매도: {get_tag_and_count(res_s2)}\n"
    
    if success_count > 0:
        res_msg += f"\n✅ 총 {success_count}건의 주문이 성공적으로 접수되었습니다."
    
    return res_msg
