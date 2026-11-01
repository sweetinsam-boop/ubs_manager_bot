"""무한매수법 V4.0 전략 오케스트레이션.

시세·계좌 → v4_rules로 하루치 계획 → 주문 전송 → 장 마감 후 체결을 읽어 T·잔금 정산.
상태 변경은 execute_all_order(주문 시점)와 settle_ticker(정산 시점)에서만 일어난다.
build_order_info는 읽기 전용이라 /status 등에서 몇 번을 불러도 안전하다.
"""
from __future__ import annotations

import datetime

import pytz

import kis_client
import state_manager
import utils
import v4_rules as rules
import v4_state
from bot_logging import get_logger
from state_manager import STRATEGY

log = get_logger(__name__)

KIND_LABEL = rules.KIND_LABEL
_HALF_KINDS = ("avg", "star", "grid")


def profit_target_pct(ticker: str) -> float:
    """3/4 지정가 익절 목표% (TQQQ 15, SOXL 20, 그 외 사용자 설정)."""
    return rules.limit_pct(ticker, STRATEGY[ticker]["target_profit"])


def current_state(ticker: str) -> tuple[float, float]:
    """(T, 잔금) 읽기 전용. 상태가 없으면 보유 원가로 추정한다 (중간 합류·상태 유실 대비)."""
    s = STRATEGY[ticker]
    d = s["data"]
    rec = v4_state.get_record(ticker)
    cost = float(d.get("avg") or 0.0) * float(d.get("qty") or 0.0)
    t, pool = rec["t"], rec["pool"]
    if t is None:
        t = rules.implied_t(cost, s["seed"], s["total_a"])
    if pool is None:
        pool = max(float(s["seed"]) - cost, 0.0)
    return float(t), float(pool)


def implied_t_now(ticker: str) -> float:
    """보유 원가 기준 T 추정치. 저장된 T와 크게 어긋나면 상태 유실·불일치 신호다."""
    s = STRATEGY[ticker]
    d = s["data"]
    cost = float(d.get("avg") or 0.0) * float(d.get("qty") or 0.0)
    return rules.implied_t(cost, s["seed"], s["total_a"])


def _price(fn, ticker: str) -> float:
    try:
        return float(fn(ticker) or 0.0)
    except Exception as e:
        log.warning("%s %s failed: %s", getattr(fn, "__name__", "price"), ticker, e)
        return 0.0


def build_order_info(ticker: str) -> dict:
    """오늘 낼 주문 계획. 상태는 바꾸지 않는다 (reverse_enter/exit는 실행 시 반영)."""
    s = STRATEGY[ticker]
    d = s["data"]
    avg = float(d.get("avg") or 0.0)
    qty = int(d.get("qty") or 0)
    seed, n, tp = float(s["seed"]), float(s["total_a"]), float(s["target_profit"])
    t, pool = current_state(ticker)
    cash = float(state_manager.CURRENT_CASH or 0.0)
    if cash > 0:
        pool = min(pool, cash)  # 계좌 현금보다 많이 주문하지 않는다

    price = _price(kis_client.get_current_price, ticker) or _price(kis_client.get_prev_close, ticker)
    info = {
        "ticker": ticker, "avg": avg, "qty": qty, "seed": seed, "n": n, "target_profit": tp,
        "t": t, "pool": pool, "price": price, "reverse_enter": False, "reverse_exit": False,
    }

    if qty <= 0:
        plan = rules.plan_first(seed / n, price) or {"mode": "first", "one_time": seed / n, "orders": []}
        info.update(plan)
        info["t"], info["pool"] = 0.0, seed
        return info

    rec = v4_state.get_record(ticker)
    reverse = bool(rec["reverse"])
    prev_close = _price(kis_client.get_prev_close, ticker)
    if reverse and prev_close > rules.reverse_exit_price(ticker, avg, tp):
        reverse = False
        info["reverse_exit"] = True
    if not reverse and rules.is_exhausted(t, n):
        reverse = True
        info["reverse_enter"] = True

    if reverse:
        day1 = info["reverse_enter"] or not rec["reverse_day1_done"]
        star_ref = _price(lambda tk: kis_client.get_avg_close_last_n_days(tk, 5), ticker) or prev_close or avg
        info.update(rules.plan_reverse(ticker, n, qty, pool, star_ref, day1))
    else:
        info.update(rules.plan_normal(ticker, tp, n, t, pool, avg, qty))
    return info


def _odno(res) -> str:
    out = (res or {}).get("output") or {}
    return str(out.get("ODNO") or out.get("odno") or "").strip() if isinstance(out, dict) else ""


def execute_all_order(ticker: str, info: dict) -> tuple[str, int]:
    """info의 주문을 전송하고 상태를 확정한다. (결과 텍스트, 접수 성공 건수)."""
    ymd = utils.get_us_today()
    with v4_state.locked():
        upd: dict = {}
        if info["mode"] == "first":
            upd.update(t=0.0, pool=info["seed"], reverse=False, reverse_started_ymd=None,
                       reverse_day1_done=False, orders=[])
        else:
            rec = v4_state.get_record(ticker)
            t0, p0 = current_state(ticker)
            if rec["t"] is None:
                upd["t"] = t0
            if rec["pool"] is None:
                upd["pool"] = p0
            if info["reverse_exit"]:
                upd.update(reverse=False, reverse_started_ymd=None, reverse_day1_done=False)
            if info["reverse_enter"]:
                upd.update(reverse=True, reverse_started_ymd=ymd, reverse_day1_done=False)
        if upd:
            v4_state.put_record(ticker, **upd)

    half = "first" if info.get("first_half") else "second"
    label = {"first": "첫 매수", "normal": "일반모드", "reverse": "리버스"}.get(info["mode"], "")
    lines = [f"[{ticker} 주문 결과 - V4.0 {label}]"]
    ok = 0
    grid_ok = grid_fail = 0
    for o in info.get("orders", []):
        res = kis_client.send_order(ticker, o["qty"], o["price"], o["side"], o["type"])
        good = bool(res) and str(res.get("rt_cd")) == "0"
        if good:
            ok += 1
            v4_state.add_order(ticker, {
                **o, "ymd": ymd, "odno": _odno(res), "settled": False,
                "half": half if o["kind"] in _HALF_KINDS else None,
            })
        if o["kind"] in ("grid", "rev_grid"):
            grid_ok, grid_fail = grid_ok + good, grid_fail + (not good)
            continue
        status = "성공" if good else f"실패({(res or {}).get('msg1', '')})"
        lines.append(f"  {KIND_LABEL[o['kind']]} {o['qty']}주 @ ${o['price']:.2f}: {status}")
    if grid_ok or grid_fail:
        lines.append(f"  줍줍 {grid_ok + grid_fail}건: 성공 {grid_ok}, 실패 {grid_fail}")
    if info["mode"] == "reverse" and info.get("day1") and (ok > 0 or not info.get("orders")):
        v4_state.put_record(ticker, reverse_day1_done=True)
    if not info.get("orders"):
        lines.append("  오늘 낼 주문이 없습니다.")
    if ok:
        lines.append(f"\n총 {ok}건의 주문이 접수되었습니다.")
    return "\n".join(lines), ok


def _is_after_close(now_ny: datetime.datetime) -> bool:
    return (now_ny.hour, now_ny.minute) >= (16, 15)


def settle_ticker(ticker: str) -> list[str]:
    """장이 끝난 주문의 체결 결과를 T·잔금에 반영한다. 여러 번 불러도 같은 주문은 한 번만 반영(멱등)."""
    if ticker not in STRATEGY:
        return []
    now_ny = datetime.datetime.now(pytz.timezone("America/New_York"))
    today = now_ny.strftime("%Y-%m-%d")
    with v4_state.locked():
        rec = v4_state.get_record(ticker)
        ready = [o for o in rec["orders"]
                 if not o.get("settled") and (o["ymd"] < today or (o["ymd"] == today and _is_after_close(now_ny)))]
        if not ready:
            return []
        fills = kis_client.fills_by_order(ticker)
        if fills is None:  # 조회 실패를 '미체결'로 오해해 정산하지 않도록 다음 기회로 미룬다
            log.warning("settle %s skipped: fills query failed", ticker)
            return []
        n = float(STRATEGY[ticker]["total_a"])
        t, pool = current_state(ticker)
        msgs = []
        for ymd in sorted({o["ymd"] for o in ready}):
            day = []
            for o in (x for x in ready if x["ymd"] == ymd):
                q, amt = fills.get(str(o.get("odno") or ""), (0, 0.0))
                day.append({**o, "filled": q, "notional": amt})
            t_new, pool_new = rules.settle(t, pool, n, day)
            hit = ", ".join(f"{KIND_LABEL[o['kind']]} {o['filled']}주" for o in day if o["filled"] > 0) or "체결 없음"
            msgs.append(f"{ticker} {ymd} 정산: {hit} → T {t:.2f}→{t_new:.2f}, 잔금 ${pool_new:,.0f}")
            t, pool = t_new, pool_new
        for o in ready:  # ready의 항목은 rec["orders"]의 같은 객체다
            o["settled"] = True
        v4_state.put_record(ticker, t=t, pool=pool, orders=rec["orders"])
        return msgs


def force_first_entry(ticker: str) -> tuple[str, int]:
    """1회분으로 1주도 못 사는 새 사이클을 사용자가 선택했을 때 1주만 LOC로 진입한다."""
    info = build_order_info(ticker)
    if info["mode"] != "first" or info["price"] <= 0:
        return "진입할 수 있는 상태가 아닙니다. 시세를 확인하지 못했거나 이미 보유 중입니다.", 0
    info["orders"] = rules.plan_first(info["price"], info["price"])["orders"]
    return execute_all_order(ticker, info)


def recalibrate(ticker: str, t_value: float | None = None) -> tuple[float, float]:
    """T를 직접 지정하거나(예: 카페 엑셀 값), 생략하면 보유 원가로 다시 추정한다. 잔금은 원금-보유원가."""
    s = STRATEGY[ticker]
    d = s["data"]
    cost = float(d.get("avg") or 0.0) * float(d.get("qty") or 0.0)
    t = float(t_value) if t_value is not None else rules.implied_t(cost, s["seed"], s["total_a"])
    pool = max(float(s["seed"]) - cost, 0.0)
    v4_state.put_record(ticker, t=t, pool=pool)
    return t, pool


def add_cumul_profit(ticker, profit_amount, sell_qty, sell_price, avg_price, order_no=""):
    """익절 실현손익을 누적 파일에 반영. Returns: (종목별 누적 합계, 이번 호출에서 신규 반영 여부)."""
    ticker = str(ticker or "").strip().upper()
    if not ticker:
        return 0.0, False
    if sell_qty <= 0 or avg_price <= 0:
        return float(utils.load_cumul_profit().get(ticker, 0.0)), False
    data = utils.load_cumul_profit()
    processed = [str(x).strip() for x in (data.get("processed_orders") or [])]
    on = str(order_no or "").strip()
    if on and on in processed:
        return float(data.get(ticker, 0.0)), False

    data[ticker] = float(data.get(ticker, 0.0)) + float(profit_amount)
    now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    data["history"].append({
        "date": now_kst, "ticker": ticker, "profit": round(profit_amount, 2),
        "sell_qty": sell_qty, "sell_price": round(sell_price, 2),
        "avg_price": round(avg_price, 2), "cumul_total": round(data[ticker], 2),
        "order_no": on, "method": "4.0",
    })
    if on:
        processed.append(on)
        data["processed_orders"] = processed[-200:]
    data["history"] = data["history"][-100:]
    utils.save_cumul_profit(data)
    return float(data[ticker]), True
