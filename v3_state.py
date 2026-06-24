"""무한매수법 3.0 전용: T 분자 = 실매수 누적 USD (증권사 평단×수량과 별도)."""
from __future__ import annotations

from typing import Any

from storage.paths import get_path
from storage.file_store import load_json, save_json

_V3_BUY_STATE_JSON = "v3_buy_state.json"
_MAX_ODNOS = 300


def load_v3_state() -> dict[str, Any]:
    return load_json(get_path(_V3_BUY_STATE_JSON), default={})


def save_v3_state(data: dict[str, Any]) -> None:
    save_json(get_path(_V3_BUY_STATE_JSON), data, ensure_ascii=False, indent=2)


def _default_record() -> dict[str, Any]:
    return {"buy_cumul_usd": 0.0, "initialized": False, "processed_buy_odnos": []}


def get_record(ticker: str) -> dict[str, Any]:
    st = load_v3_state()
    r = st.get(ticker)
    if not isinstance(r, dict):
        r = _default_record()
    else:
        r = {**_default_record(), **r}
    po = r.get("processed_buy_odnos")
    if not isinstance(po, list):
        r["processed_buy_odnos"] = []
    return r


def put_record(ticker: str, **kwargs: Any) -> None:
    st = load_v3_state()
    cur = st.get(ticker)
    if not isinstance(cur, dict):
        cur = _default_record()
    else:
        cur = {**_default_record(), **cur}
    cur.update(kwargs)
    st[ticker] = cur
    save_v3_state(st)


def get_buy_cumul(ticker: str) -> float:
    return float(get_record(ticker).get("buy_cumul_usd") or 0.0)


def _cap_odnos(odnos: list[str]) -> list[str]:
    if len(odnos) <= _MAX_ODNOS:
        return odnos
    return odnos[-_MAX_ODNOS:]


def seed_if_needed(ticker: str, avg: float, qty: float) -> None:
    """최초 1회: 누적 매수금을 평단×수량으로 두고, 최근 매수 odno는 processed에 넣어 이중 가산 방지."""
    r = get_record(ticker)
    if r.get("initialized"):
        return
    from kis_client import collect_recent_buy_odnos_with_fill

    cum = float(avg or 0.0) * float(qty or 0.0)
    proc = collect_recent_buy_odnos_with_fill(ticker)
    put_record(
        ticker,
        buy_cumul_usd=cum,
        initialized=True,
        processed_buy_odnos=_cap_odnos(proc),
    )


def apply_buy_order_notional(ticker: str, odno: str) -> tuple[float, bool]:
    """KIS에서 해당 매수 주문 체결금액을 조회해 누적에 반영. (가산액, 반영 여부)."""
    from kis_client import sum_buy_notional_for_order

    on = str(odno or "").strip()
    if not on:
        return 0.0, False
    r = get_record(ticker)
    proc = list(r.get("processed_buy_odnos") or [])
    if on in proc:
        return 0.0, False
    n = float(sum_buy_notional_for_order(on, ticker))
    if n <= 0:
        return 0.0, False
    bc = float(r.get("buy_cumul_usd") or 0.0) + n
    proc.append(on)
    put_record(
        ticker,
        buy_cumul_usd=bc,
        initialized=True,
        processed_buy_odnos=_cap_odnos(proc),
    )
    return n, True


def reconcile_recent_buys(ticker: str) -> int:
    """동기화 시: 최근 매수 체결 odno 중 미처리분만 반영. 건수 반환."""
    from kis_client import collect_recent_buy_odnos_with_fill

    if not get_record(ticker).get("initialized"):
        return 0
    added = 0
    for od in collect_recent_buy_odnos_with_fill(ticker):
        _, did = apply_buy_order_notional(ticker, od)
        if did:
            added += 1
    return added


def delete_record(ticker: str) -> None:
    """해당 종목의 V3 상태 기록을 완전히 제거합니다."""
    st = load_v3_state()
    if ticker in st:
        del st[ticker]
        save_v3_state(st)
