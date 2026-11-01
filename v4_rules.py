"""무한매수법 V4.0 순수 규칙 (표준 라이브러리만 사용, 파일·네트워크 접근 없음).

기준: 라오어 카페 'V4.0 방법론 1.일반모드 / 2.소진후 리버스모드'.
- 별% = 기준% - (2*기준%/N)*T   (기준% = TQQQ 15, SOXL 20 → 20분할 15-1.5T, 40분할 15-0.75T)
- 1회매수금 = 잔금 / (N - T)
- T: 1회 매수 +1, 절반 +0.5, 쿼터매도 x0.75, 지정가 3/4 매도 x0.25
- 소진(T > N-1) 후 리버스: 첫날 MOC 분할매도, 이후 5일 종가평균 별지점, 쿼터매수
"""
from __future__ import annotations

import math

DEFAULT_LIMIT_PCT = {"TQQQ": 15.0, "SOXL": 20.0}
GRID_LEVELS = 6  # 줍줍(하락 대비 1주 LOC) 단계 수

KIND_LABEL = {
    "first": "첫 매수 LOC", "avg": "평단 LOC 매수", "star": "별지점 LOC 매수", "grid": "줍줍 LOC 매수",
    "sell_limit": "지정가 매도 3/4", "sell_quarter": "별지점 LOC 매도 1/4",
    "rev_moc": "리버스 MOC 매도", "rev_sell": "리버스 LOC 매도",
    "rev_buy": "리버스 쿼터매수", "rev_grid": "리버스 줍줍 매수",
}

SELL_KINDS = ("sell_limit", "sell_quarter", "rev_moc", "rev_sell")
BUY_KINDS = ("first", "avg", "star", "grid", "rev_buy", "rev_grid")


def limit_pct(ticker: str, target_profit: float = 10.0) -> float:
    """3/4 지정가 익절 목표%. TQQQ 15, SOXL 20, 그 외는 사용자 설정."""
    return DEFAULT_LIMIT_PCT.get(str(ticker).upper(), float(target_profit))


def star_pct(ticker: str, t: float, n: float, target_profit: float = 10.0) -> float:
    base = limit_pct(ticker, target_profit)
    return base - (2.0 * base / n) * t


def star_point(avg: float, pct: float) -> float:
    return round(avg * (1 + pct / 100.0), 2)


def loc_divisor(n: float) -> int:
    """리버스 매도 분할: 20분할 10등분, 40분할 20등분."""
    return 20 if n >= 30 else 10


def reverse_mult(n: float) -> float:
    """리버스 매도 시 T 배수: 20분할 x0.9, 40분할 x0.95."""
    return 0.95 if n >= 30 else 0.9


def reverse_exit_price(ticker: str, avg: float, target_profit: float = 10.0) -> float:
    """종가가 이 값보다 커지면 리버스 종료 (TQQQ 평단-15%, SOXL 평단-20%)."""
    return avg * (1 - limit_pct(ticker, target_profit) / 100.0)


def is_exhausted(t: float, n: float) -> bool:
    return t > n - 1 + 1e-9


def implied_t(cost_basis: float, seed: float, n: float) -> float:
    """상태 없이 중간에 합류했을 때 보유 원가로 추정하는 T (소수 둘째 자리 올림)."""
    if seed <= 0 or n <= 0 or cost_basis <= 0:
        return 0.0
    return math.ceil(cost_basis / (seed / n) * 100) / 100


def _order(kind: str, side: str, otype: str, qty: int, price: float) -> dict:
    return {"kind": kind, "side": side, "type": otype, "qty": int(qty), "price": round(float(price), 2)}


def grid_orders(one_time: float, base_qty: int, ceiling: float | None, kind: str = "grid") -> list[dict]:
    """줍줍: 1회분/(기본수량+i) 가격에 1주씩. 기본수량은 본 매수 주문 수량의 합."""
    out = []
    base = max(int(base_qty), 1)
    for i in range(1, GRID_LEVELS + 1):
        px = round(one_time / (base + i), 2)
        if px <= 0:
            break
        if ceiling is not None and px >= ceiling:
            continue
        out.append(_order(kind, "BUY", "LOC", 1, px))
    return out


def plan_first(one_time: float, price: float) -> dict | None:
    """새 사이클 첫 매수: 1회분을 현재가 +10% LOC로 (종가 체결 유도)."""
    if price <= 0 or one_time <= 0:
        return None
    qty = math.floor(one_time / price)
    if qty <= 0:
        return None
    return {
        "mode": "first",
        "one_time": one_time,
        "orders": [_order("first", "BUY", "LOC", qty, price * 1.10)],
    }


def plan_normal(ticker: str, target_profit: float, n: float, t: float, pool: float, avg: float, qty: int) -> dict:
    """일반모드 하루치 주문. 전반전(T<N/2)은 별지점·평단 반반, 후반전은 별지점 전액."""
    lp = limit_pct(ticker, target_profit)
    sp = star_pct(ticker, t, n, target_profit)
    pt = star_point(avg, sp)
    star_buy = max(round(pt - 0.01, 2), 0.01)  # 매수점은 별지점-0.01 (매도와 겹침 방지)
    target = round(avg * (1 + lp / 100.0), 2)
    one_time = max(pool, 0.0) / max(n - t, 1e-6)
    first_half = t < n / 2.0

    if first_half:
        half = one_time / 2.0
        b2 = math.floor(half / star_buy)
        b1 = math.floor(half / avg) + 1
    else:
        b2 = math.floor(one_time / star_buy)
        b1 = 0

    q_quarter = qty // 4
    q_limit = qty - q_quarter

    orders: list[dict] = []
    if q_limit > 0:
        orders.append(_order("sell_limit", "SELL", "00", q_limit, target))
    if q_quarter > 0:
        orders.append(_order("sell_quarter", "SELL", "LOC", q_quarter, pt))
    if b1 > 0:
        orders.append(_order("avg", "BUY", "LOC", b1, avg))
    if b2 > 0:
        orders.append(_order("star", "BUY", "LOC", b2, star_buy))
    main_prices = [o["price"] for o in orders if o["side"] == "BUY"]
    ceiling = min(main_prices) if main_prices else None
    orders += grid_orders(one_time, b1 + b2, ceiling)

    return {
        "mode": "normal",
        "t": t, "pool": pool, "one_time": one_time, "first_half": first_half,
        "limit_pct": lp, "star_pct": sp, "star_point": pt, "star_buy": star_buy,
        "target": target, "qty_limit": q_limit, "qty_quarter": q_quarter,
        "orders": orders,
    }


def plan_reverse(ticker: str, n: float, qty: int, pool: float, star_ref: float, day1: bool) -> dict:
    """소진 후 리버스. 첫날은 MOC 분할매도만, 이후는 별지점(5일 종가평균) LOC 매도 + 쿼터매수."""
    div = loc_divisor(n)
    sell_q = qty // div
    base = {"mode": "reverse", "day1": day1, "divisor": div, "mult": reverse_mult(n), "pool": pool}
    if day1:
        orders = [_order("rev_moc", "SELL", "MOC", sell_q, 0)] if sell_q > 0 else []
        return {**base, "orders": orders, "sell_qty": sell_q}

    pt = round(star_ref, 2)
    buy_px = max(round(pt - 0.01, 2), 0.01)
    quarter_amt = max(pool, 0.0) / 4.0
    buy_q = math.floor(quarter_amt / buy_px)
    orders = []
    if sell_q > 0:
        orders.append(_order("rev_sell", "SELL", "LOC", sell_q, pt))
    if buy_q > 0:
        orders.append(_order("rev_buy", "BUY", "LOC", buy_q, buy_px))
        orders += grid_orders(quarter_amt, buy_q, buy_px, kind="rev_grid")
    return {**base, "orders": orders, "sell_qty": sell_q, "star_ref": pt, "buy_px": buy_px,
            "quarter_amt": quarter_amt}


def settle(t: float, pool: float, n: float, orders: list[dict]) -> tuple[float, float]:
    """하루치 주문의 체결 결과로 T와 잔금을 갱신.

    orders: {kind, filled(체결수량), notional(체결금액 $), half('first'|'second'|None)}
    갱신 순서는 공식 표기 그대로 곱셈 먼저, 덧셈 나중: (T*0.25 + 1).
    """
    def filled(*kinds) -> bool:
        return any(o["kind"] in kinds and o.get("filled", 0) > 0 for o in orders)

    if filled("sell_limit"):
        t *= 0.25
    elif filled("sell_quarter"):
        t *= 0.75
    if filled("rev_moc", "rev_sell"):
        t *= reverse_mult(n)

    if filled("first"):
        t += 1.0
    star_hit = filled("star", "grid")  # 줍줍이 체결됐다면 그보다 위의 LOC도 체결된 것
    avg_hit = filled("avg", "grid")
    half = next((o.get("half") for o in orders if o["kind"] in ("star", "avg", "grid") and o.get("half")), None)
    if half == "first":
        t += 0.5 * star_hit + 0.5 * avg_hit
    elif half == "second":
        t += 1.0 if star_hit else 0.0
    if filled("rev_buy", "rev_grid"):
        t += (n - t) * 0.25

    for o in orders:
        amt = float(o.get("notional") or 0.0)
        if o["kind"] in BUY_KINDS:
            pool -= amt
        elif o["kind"] in SELL_KINDS:
            pool += amt
    return t, max(pool, 0.0)
