"""
공통 유틸: 거래 잠금, 채팅 ID, 누적 수익, 성과 로그, 사이클 상태, 리포트/정산일, 시장 세션.
파일 경로 및 JSON/텍스트 I/O는 storage 모듈을 사용합니다.
"""
from __future__ import annotations

import datetime
import os
import pytz
import pandas_market_calendars as mcal

from storage.paths import (
    get_path,
    TRADE_LOCK_FILE,
    CHAT_ID_FILE,
    CUMUL_PROFIT_FILE,
    PERFORMANCE_LOG_FILE,
    CYCLE_STATE_FILE,
    LAST_SETTLEMENT_DATE_FILE,
)
from storage.file_store import load_json, save_json, load_text, save_text
from bot_logging import get_logger

log = get_logger(__name__)

# NAS 등에서 호스트가 ./storage 전체를 마운트하면 구버전 paths.py 가 이미지를 덮어
# LAST_KNOWN_HOLD_AVG_FILE 상수 import 가 실패합니다. 파일명은 여기서만 고정합니다.
# (storage.paths.LAST_KNOWN_HOLD_AVG_FILE 과 동일 문자열 유지 권장)
_LAST_KNOWN_HOLD_AVG_FILE = "last_known_hold_avg.json"

# 하위 호환: telegram_bot 등이 open(utils.PERFORMANCE_LOG_FILE), os.path.exists(utils.TRADE_LOCK_FILE) 사용
PERFORMANCE_LOG_FILE = str(get_path(PERFORMANCE_LOG_FILE))
TRADE_LOCK_FILE = str(get_path(TRADE_LOCK_FILE))


def load_performance_log() -> list:
    """일별 계좌 자산·벤치마크 기록 [{date, equity, benchmark, nasdaq}]. 파일명은 .csv지만 내용은 JSON이다."""
    data = load_json(get_path(PERFORMANCE_LOG_FILE), default=[])
    return data if isinstance(data, list) else []


def get_us_today() -> str:
    """미국 동부 기준 오늘 날짜 문자열 (YYYY-MM-DD)을 반환합니다."""
    est = pytz.timezone("US/Eastern")
    return datetime.datetime.now(est).strftime("%Y-%m-%d")


def get_broker_calendar_end_ymd() -> str:
    """
    KIS 계좌/기간손익 조회 종료일(YYYYMMDD).
    미국 동부·한국 달력 중 더 늦은 '오늘'을 씁니다. (한국이 하루 앞선 날짜일 때 조회가 잘리는 것을 줄임)
    """
    est = pytz.timezone("US/Eastern")
    kst = pytz.timezone("Asia/Seoul")
    d_us = datetime.datetime.now(est).date()
    d_kr = datetime.datetime.now(kst).date()
    return max(d_us, d_kr).strftime("%Y%m%d")


def normalize_ord_yyyymmdd(ord_dt_raw) -> str:
    """
    KIS 해외주식 체결/주문 API의 ord_dt를 YYYYMMDD 8자리로 통일합니다.
    (하이픈 포함·공백·빈 값 등으로 locked_by_broker 비교가 실패하는 경우 방지)
    """
    if ord_dt_raw is None:
        return ""
    s = "".join(ch for ch in str(ord_dt_raw).strip() if ch.isdigit())
    if len(s) >= 8:
        return s[:8]
    return ""


def should_run_kst_morning_settlement(now_kst: datetime.datetime | None = None) -> bool:
    """
    KST 08:00 정산 리포트를 보낼 요일인지 (화~토). 일·월은 주말 직후로 의미가 적어 생략.
    weekday: 월=0 … 일=6
    """
    kst = pytz.timezone("Asia/Seoul")
    dt = now_kst or datetime.datetime.now(kst)
    if dt.tzinfo is None:
        dt = kst.localize(dt)
    else:
        dt = dt.astimezone(kst)
    # [중요] Python 표준 datetime.weekday() 요일 매핑 안내: 
    # 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday
    # (PTB v20+의 0=Sunday와 다르니 주의 필요)
    return dt.weekday() in (1, 2, 3, 4, 5)


def log_daily_performance(total_equity: float, benchmark_price: float, nasdaq_price: float) -> None:
    """매일의 총 자산과 벤치마크(S&P500), 나스닥 가격을 기록합니다."""
    today = get_us_today()
    path = get_path(PERFORMANCE_LOG_FILE)
    data = load_json(path, default=[])

    found = False
    for entry in data:
        if entry.get("date") == today:
            entry["equity"] = total_equity
            entry["benchmark"] = benchmark_price
            entry["nasdaq"] = nasdaq_price
            found = True
            break
    if not found:
        data.append({
            "date": today,
            "equity": total_equity,
            "benchmark": benchmark_price,
            "nasdaq": nasdaq_price,
        })

    if len(data) > 365:
        data = data[-365:]
    save_json(path, data, ensure_ascii=False, indent=2)


def check_trade_lock(ticker: str) -> bool:
    """해당 티커가 오늘(미국 기준) 이미 매매 완료로 잠금인지 여부."""
    locks = load_json(get_path(TRADE_LOCK_FILE), default={})
    return locks.get(ticker) == get_us_today()


def set_trade_lock(ticker: str) -> None:
    """해당 티커를 오늘(미국 기준) 매매 완료로 잠금 처리합니다."""
    locks = load_json(get_path(TRADE_LOCK_FILE), default={})
    locks[ticker] = get_us_today()
    save_json(get_path(TRADE_LOCK_FILE), locks)


def save_chat_id(chat_id: int) -> None:
    """봇이 사용할 채팅 ID를 저장합니다."""
    save_text(get_path(CHAT_ID_FILE), str(chat_id))


def load_chat_id():
    """명령을 받을 채팅 ID. .env의 TELEGRAM_CHAT_ID가 우선이고, 없으면 처음 /start 한 채팅. 없으면 None."""
    env = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if env.lstrip("-").isdigit():
        return int(env)
    raw = load_text(get_path(CHAT_ID_FILE))
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (ValueError, TypeError):
        return None


def is_market_open_today() -> bool:
    """오늘(미국 동부) NYSE 개장일 여부."""
    est = pytz.timezone("US/Eastern")
    today_est = datetime.datetime.now(est).date()
    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.schedule(start_date=today_est, end_date=today_est)
    return not schedule.empty


def get_market_session() -> str:
    """현재 미국 시장 세션: 'regular' | 'aftermarket' | 'premarket' | 'closed'."""
    est = pytz.timezone("US/Eastern")
    now = datetime.datetime.now(est)
    hour, minute, weekday = now.hour, now.minute, now.weekday()

    if weekday >= 5:
        return "closed"
    if (hour == 9 and minute >= 30) or (hour >= 10 and hour < 16):
        return "regular"
    if hour >= 16 and hour < 20:
        return "aftermarket"
    if hour >= 4 and (hour < 9 or (hour == 9 and minute < 30)):
        return "premarket"
    return "closed"


def get_market_session_kr() -> str:
    """현재 시장 세션의 한글 설명."""
    session = get_market_session()
    return {"regular": "정규장 거래 중", "aftermarket": "애프터마켓", "premarket": "프리마켓", "closed": "장 마감"}.get(session, "장 마감")


_CUMUL_META_KEYS = frozenset({"history", "processed_orders"})


def load_cumul_profit() -> dict:
    """누적 수익 데이터. history, processed_orders 및 티커별 float 수익."""
    data = load_json(get_path(CUMUL_PROFIT_FILE), default={})
    hist_in = data.get("history", []) or []
    hist_out = []
    for h in hist_in:
        if not isinstance(h, dict):
            continue
        hc = dict(h)
        if hc.get("ticker") is not None:
            hc["ticker"] = str(hc["ticker"]).strip().upper()
        hist_out.append(hc)
    refined = {
        "history": hist_out,
        "processed_orders": [str(x).strip() for x in (data.get("processed_orders") or [])],
    }
    for k, v in data.items():
        if k not in _CUMUL_META_KEYS:
            t = str(k).strip().upper()
            if not t:
                continue
            try:
                fv = float(v)
            except (ValueError, TypeError):
                continue
            refined[t] = float(refined.get(t, 0.0)) + fv
    return refined


def aggregate_cumul_profit_by_ticker(cp: dict | None) -> dict[str, float]:
    """
    /report 등 표시용: history 건별 합과 cumul_profit.json 티커 키 누적을 모두 구한 뒤 max를 씁니다.
    과거 일부 경로에서 history만 갱신·또는 키만 갱신되어 둘이 어긋날 수 있어 누수·과소표시를 막습니다.
    """
    cp = cp or {}
    from collections import defaultdict

    hist_sum: dict[str, float] = defaultdict(float)
    for h in cp.get("history") or []:
        t = str(h.get("ticker") or "").strip().upper()
        if not t:
            continue
        try:
            hist_sum[t] += float(h.get("profit") or 0)
        except (TypeError, ValueError):
            pass

    file_sum: dict[str, float] = {}
    for k, v in cp.items():
        if k in _CUMUL_META_KEYS:
            continue
        t = str(k).strip().upper()
        if not t:
            continue
        try:
            file_sum[t] = float(v)
        except (TypeError, ValueError):
            pass

    out: dict[str, float] = {}
    for t in sorted(set(hist_sum) | set(file_sum)):
        h = hist_sum.get(t, 0.0)
        f = file_sum.get(t, 0.0)
        out[t] = max(h, f)
    return out


def save_cumul_profit(data: dict) -> None:
    """누적 수익 데이터를 저장합니다."""
    save_json(get_path(CUMUL_PROFIT_FILE), data, ensure_ascii=False, indent=2)


def load_cycle_state() -> dict:
    """종합적인 사이클 데이터(현재 회차, 진행 중 정보, 이력)를 반환합니다."""
    return load_json(get_path(CYCLE_STATE_FILE), default={})


def save_cycle_state(data: dict) -> None:
    """사이클 상태를 저장합니다."""
    save_json(get_path(CYCLE_STATE_FILE), data, ensure_ascii=False, indent=2)


def delete_ticker_from_all_states(ticker: str) -> None:
    """해당 종목의 모든 상태 파일(사이클, 잠금, 평단, V3/V4) 정보를 보존 로직에 맞춰 정리합니다."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return

    # 1. cycle_state.json 삭제
    cs = load_cycle_state()
    if ticker in cs:
        del cs[ticker]
        save_cycle_state(cs)

    # 2. trade_lock.json 삭제
    locks = load_json(get_path(TRADE_LOCK_FILE), default={})
    if ticker in locks:
        del locks[ticker]
        save_json(get_path(TRADE_LOCK_FILE), locks)

    # 3. last_known_hold_avg.json 삭제
    lk = load_last_known_hold_avgs()
    if ticker in lk:
        del lk[ticker]
        save_last_known_hold_avgs(lk)

    # 4. cumul_profit.json (종목 합계 키만 삭제, history 리스트는 보존)
    cp = load_cumul_profit()
    if ticker in cp:
        del cp[ticker]
        save_cumul_profit(cp)

    # 5. V3/V4 전용 상태 파일 삭제
    import v3_state
    import v4_state

    v3_state.delete_record(ticker)
    v4_state.delete_record(ticker)

    log.info(f"Cleanup completed for ticker: {ticker} (State files cleaned, Profit history preserved)")


def load_last_known_hold_avgs() -> dict[str, float]:
    """
    sync_account 성공 직후 보유가 있었을 때 기록한 종목별 마지막 평단.
    전량 매도 후 메모리가 0이 되어도 장마감 매도수익 정리(reconcile)에 평단으로 사용합니다.
    """
    raw = load_json(get_path(_LAST_KNOWN_HOLD_AVG_FILE), default={})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        t = str(k or "").strip().upper()
        try:
            a = float(v)
        except (TypeError, ValueError):
            continue
        if t and a > 0:
            out[t] = a
    return out


def save_last_known_hold_avgs(avgs: dict[str, float]) -> None:
    """종목별 마지막 보유 평단 스냅샷 저장."""
    clean: dict[str, float] = {}
    for k, v in (avgs or {}).items():
        t = str(k or "").strip().upper()
        if not t:
            continue
        try:
            a = float(v)
        except (TypeError, ValueError):
            continue
        if a > 0:
            clean[t] = a
    save_json(get_path(_LAST_KNOWN_HOLD_AVG_FILE), clean, ensure_ascii=False, indent=2)


def update_cycle_info(
    ticker: str, start_date: str | None = None, end_date: str | None = None, increment_cycle: bool = False
) -> None:
    """
    사이클 정보를 기록하거나 갱신합니다.
    - 구조: {"current_cycle": N, "active": {...}, "history": [...]}
    """
    ticker = str(ticker or "").strip().upper()
    state = load_cycle_state()

    if ticker not in state:
        state[ticker] = {
            "current_cycle": 1,
            "active": None,
            "history": []
        }

    info = state[ticker]
    # 회차 정보 보정 (구버전 데이터 호환)
    if "cycle_count" in info and "current_cycle" not in info:
        info["current_cycle"] = info.pop("cycle_count")
    if "start_date" in info and info.get("active") is None:
        # 구버전 최상위 start_date가 있으면 active로 승격
        sd = info.pop("start_date")
        if sd:
            info["active"] = {"cycle_num": info["current_cycle"], "start_date": sd}
    if "history" not in info:
        info["history"] = []

    # 1. 사이클 종료 처리 (increment_cycle=True 또는 end_date 명시)
    if increment_cycle:
        active = info.get("active")
        if active:
            # 현재 active를 종료하고 history로 이동
            active["end_date"] = end_date or get_us_today()
            info["history"].append(active)
            # 최근 10개만 유지
            if len(info["history"]) > 10:
                info["history"] = info["history"][-10:]
            info["active"] = None
            info["current_cycle"] += 1
        else:
            # 진행 중인 사이클이 없으면 이미 종료된 상태 → 중복 증가 방지
            log.warning(
                "%s: increment_cycle 요청이나 active 사이클 없음 (이미 종료 처리됨). 건너뜁니다. current_cycle=%d",
                ticker, info["current_cycle"],
            )

    # 2. 사이클 시작 처리 (start_date 명시)
    elif start_date:
        if info.get("active") is None:
            # 마지막 사이클의 종료일 확인 (중복 증가 방지 안전장치)
            last_end = info["history"][-1].get("end_date") if info["history"] else None
            # [선택] 필요 시 실무에서 last_end와 start_date 비교 로직 추가 가능
            info["active"] = {
                "cycle_num": info["current_cycle"],
                "start_date": start_date
            }

    save_cycle_state(state)


def close_active_cycle_on_zero(ticker: str, end_date: str | None = None) -> tuple[bool, int]:
    """
    보유수량 0주가 확인된 시점에 현재 active 사이클만 종료합니다.
    주의: 회차(current_cycle)는 증가시키지 않습니다.
    Returns:
      (closed, current_cycle)
    """
    ticker = str(ticker or "").strip().upper()
    state = load_cycle_state()
    if ticker not in state:
        state[ticker] = {"current_cycle": 1, "active": None, "history": []}

    info = state[ticker]
    if "cycle_count" in info and "current_cycle" not in info:
        info["current_cycle"] = info.pop("cycle_count")
    info.setdefault("current_cycle", 1)
    info.setdefault("active", None)
    info.setdefault("history", [])

    active = info.get("active")
    if not active:
        state[ticker] = info
        save_cycle_state(state)
        return False, int(info["current_cycle"])

    done = dict(active)
    done["end_date"] = end_date or get_us_today()
    hist = list(info.get("history") or [])
    hist.append(done)
    info["history"] = hist[-10:]
    info["active"] = None

    state[ticker] = info
    save_cycle_state(state)
    return True, int(info["current_cycle"])


def start_cycle_for_new_entry(
    ticker: str, *, increment_on_reentry: bool = True, start_date: str | None = None
) -> tuple[int, bool]:
    """
    새 매수 진입 주문 시 사이클 시작을 보장합니다.
    규칙:
    - active가 이미 있으면 중복 증가/재시작하지 않음.
    - active가 없고 history가 있으면(=이전 사이클 종료 이력 존재) 재진입으로 보고 +1(옵션).
    - active가 없고 history가 없으면 최초 시작으로 보고 +1 없이 시작.
    Returns:
      (current_cycle, incremented)
    """
    ticker = str(ticker or "").strip().upper()
    us_today = start_date or get_us_today()
    state = load_cycle_state()
    if ticker not in state:
        state[ticker] = {"current_cycle": 1, "active": None, "history": []}

    info = state[ticker]
    if "cycle_count" in info and "current_cycle" not in info:
        info["current_cycle"] = info.pop("cycle_count")
    info.setdefault("current_cycle", 1)
    info.setdefault("active", None)
    info.setdefault("history", [])

    # 이미 active 사이클이 있으면 중복 증가 금지
    if info.get("active") is not None:
        state[ticker] = info
        save_cycle_state(state)
        return int(info["current_cycle"]), False

    hist = list(info.get("history") or [])
    incremented = False
    if increment_on_reentry and len(hist) > 0:
        info["current_cycle"] = int(info.get("current_cycle", 1) or 1) + 1
        incremented = True

    info["active"] = {"cycle_num": int(info["current_cycle"]), "start_date": us_today}
    state[ticker] = info
    save_cycle_state(state)
    return int(info["current_cycle"]), incremented


def get_cycle_timeline(ticker: str) -> dict:
    """
    사이클 표시용 타임라인 요약.
    Returns:
      {
        "current_cycle": int,
        "previous_start": str,
        "previous_end": str,
        "current_start": str,
      }
    """
    tk = str(ticker or "").strip().upper()
    state = load_cycle_state()
    info = state.get(tk) or {}
    cur = int(info.get("current_cycle", 1) or 1)
    hist = list(info.get("history") or [])
    prev = hist[-1] if hist else {}
    active = info.get("active") or {}
    return {
        "current_cycle": cur,
        "previous_start": str(prev.get("start_date") or "-"),
        "previous_end": str(prev.get("end_date") or "-"),
        "current_start": str(active.get("start_date") or "-"),
    }


def get_last_settlement_date() -> str:
    """마지막으로 정산 리포트가 생성된 날짜 문자열."""
    raw = load_text(get_path(LAST_SETTLEMENT_DATE_FILE))
    return raw.strip() if raw else ""


def set_last_settlement_date(date_str: str) -> None:
    """정산 리포트가 생성된 날짜를 저장합니다."""
    save_text(get_path(LAST_SETTLEMENT_DATE_FILE), date_str)
