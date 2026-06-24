"""무한매수법 V4.0 전용 상태(T, 리버스모드). trading_settings와 별도 JSON으로 관리."""
from __future__ import annotations

from typing import Any

from storage.paths import get_path
from storage.file_store import load_json, save_json

# paths.py 구버전(상수 미정의)과도 호환되도록 파일명은 여기서 고정
_V4_STATE_JSON = "v4_strategy_state.json"


def load_v4_state() -> dict[str, Any]:
    return load_json(get_path(_V4_STATE_JSON), default={})


def save_v4_state(data: dict[str, Any]) -> None:
    save_json(get_path(_V4_STATE_JSON), data, ensure_ascii=False, indent=2)


def _default_record() -> dict[str, Any]:
    return {
        "t": None,
        "reverse": False,
        "reverse_started_ymd": None,
        "pending_moc": None,
        "reverse_sell_proceeds": 0.0,
        "frozen_rem": 0.0,
    }


def get_record(ticker: str) -> dict[str, Any]:
    st = load_v4_state()
    r = st.get(ticker)
    if not isinstance(r, dict):
        r = _default_record()
    else:
        r = {**_default_record(), **r}
    return r


def put_record(ticker: str, **kwargs) -> None:
    st = load_v4_state()
    cur = st.get(ticker)
    if not isinstance(cur, dict):
        cur = _default_record()
    else:
        cur = {**_default_record(), **cur}
    cur.update(kwargs)
    st[ticker] = cur
    save_v4_state(st)


def ensure_t(ticker: str, s: dict, d: dict, t_seed: float) -> float:
    """저장된 T가 없으면 t_seed로 초기화 후 저장."""
    st = load_v4_state()
    cur = st.get(ticker)
    if not isinstance(cur, dict):
        cur = _default_record()
    else:
        cur = {**_default_record(), **cur}
    if cur.get("t") is None:
        cur["t"] = float(t_seed)
        st[ticker] = cur
        save_v4_state(st)
        return float(t_seed)
    return float(cur["t"])


def set_t(ticker: str, t_val: float) -> None:
    put_record(ticker, t=t_val)


def multiply_t(ticker: str, factor: float) -> float:
    st = load_v4_state()
    cur = st.get(ticker)
    if not isinstance(cur, dict):
        cur = _default_record()
    else:
        cur = {**_default_record(), **cur}
    nt = float(cur.get("t") or 0.0) * float(factor)
    cur["t"] = nt
    st[ticker] = cur
    save_v4_state(st)
    return nt


def add_t_delta(ticker: str, delta: float) -> float:
    st = load_v4_state()
    cur = st.get(ticker)
    if not isinstance(cur, dict):
        cur = _default_record()
    else:
        cur = {**_default_record(), **cur}
    nt = float(cur.get("t") or 0.0) + float(delta)
    cur["t"] = nt
    st[ticker] = cur
    save_v4_state(st)
    return nt


def enter_reverse(ticker: str, us_ymd: str, frozen_rem: float = 0.0) -> None:
    put_record(
        ticker,
        reverse=True,
        reverse_started_ymd=us_ymd,
        reverse_sell_proceeds=0.0,
        frozen_rem=float(frozen_rem),
    )


def exit_reverse(ticker: str) -> None:
    put_record(
        ticker,
        reverse=False,
        reverse_started_ymd=None,
        reverse_sell_proceeds=0.0,
        frozen_rem=0.0,
    )

def get_frozen_rem(ticker: str) -> float:
    """리버스 진입 시 굳어진 가상의 잔여 시드 현금"""
    return float(get_record(ticker).get("frozen_rem") or 0.0)

def is_reverse(ticker: str) -> bool:
    return bool(get_record(ticker).get("reverse"))


def reverse_start_ymd(ticker: str) -> str | None:
    v = get_record(ticker).get("reverse_started_ymd")
    return str(v) if v else None


def get_pending_moc(ticker: str) -> dict[str, Any] | None:
    pm = get_record(ticker).get("pending_moc")
    return pm if isinstance(pm, dict) else None


def set_pending_moc(ticker: str, odno: str, min_qty: int, mult: float) -> None:
    put_record(
        ticker,
        pending_moc={"odno": str(odno), "min_qty": int(min_qty), "mult": float(mult)},
    )


def clear_pending_moc(ticker: str) -> None:
    put_record(ticker, pending_moc=None)


def get_reverse_sell_proceeds(ticker: str) -> float:
    """리버스모드 중 누적된 매도 회수금(달러)."""
    return float(get_record(ticker).get("reverse_sell_proceeds") or 0.0)


def add_reverse_sell_proceeds(ticker: str, amount: float) -> float:
    """리버스모드 매도 체결 금액을 누적. 반환값: 누적 합계."""
    st = load_v4_state()
    cur = st.get(ticker)
    if not isinstance(cur, dict):
        cur = _default_record()
    else:
        cur = {**_default_record(), **cur}
    prev = float(cur.get("reverse_sell_proceeds") or 0.0)
    cur["reverse_sell_proceeds"] = prev + float(amount)
    st[ticker] = cur
    save_v4_state(st)
    return cur["reverse_sell_proceeds"]


def delete_record(ticker: str) -> None:
    """해당 종목의 V4 상태 기록을 완전히 제거합니다."""
    st = load_v4_state()
    if ticker in st:
        del st[ticker]
        save_v4_state(st)
