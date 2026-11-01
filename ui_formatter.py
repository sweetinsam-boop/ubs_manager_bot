"""텔레그램 메시지 포맷터 (parse_mode=HTML). 이모지 없이 굵은 제목과 고정폭 표로 정리한다.

모든 동적 문자열은 html.escape를 거친다. 텔레그램 HTML은 b, code, pre만 쓴다.
"""
from __future__ import annotations

import html

from v4_rules import KIND_LABEL

_e = html.escape

MODE_LABEL = {"first": "새 사이클 첫 매수", "reverse": "소진 후 리버스모드"}


def mode_label(mode: str, first_half) -> str:
    if mode in MODE_LABEL:
        return MODE_LABEL[mode]
    return "일반모드 전반전" if first_half else "일반모드 후반전"


def _bar(t: float, n: float, width: int = 10) -> str:
    filled = round(max(0.0, min(t / n if n else 0.0, 1.0)) * width)
    return "█" * filled + "░" * (width - filled)


def pre(text: str) -> str:
    return f"<pre>{_e(text)}</pre>"


def _order_rows(orders: list) -> str:
    """주문 목록을 고정폭 표로. 줍줍은 한 줄로 묶는다."""
    rows, grid = [], []
    for o in orders:
        if o["kind"] in ("grid", "rev_grid"):
            grid.append(o)
            continue
        px = "시장가" if o["type"] == "MOC" else f"@ ${o['price']:.2f}"
        rows.append(f"{KIND_LABEL[o['kind']]} {o['qty']}주 {px}")
    if grid:
        lo, hi = min(g["price"] for g in grid), max(g["price"] for g in grid)
        rows.append(f"{KIND_LABEL[grid[0]['kind']]} {len(grid)}건 ${lo:.2f}~${hi:.2f}")
    return "\n".join(rows)


class UIFormatter:
    @staticmethod
    def daily_report(data: dict) -> str:
        cost, value, cash = data["total_cost"], data["total_value"], data["current_cash"]
        market = f"미국 {data['us_date_display']} 정규장 종료" if data["is_closed"] else data["market_session"]
        out = [f"<b>{_e(data.get('title', '무한매수 운용 현황'))}</b>",
               f"기준 {_e(data['recorded_at'])} KST, {_e(market)}", ""]

        pnl = value - cost
        out.append("<b>계좌</b>")
        out.append(f"총 평가금액 <b>${value + cash:,.2f}</b> (주식 ${value:,.2f}, 예수금 ${cash:,.2f})")
        out.append(f"평가손익 {pnl:+,.2f} ({pnl / cost * 100:+.2f}%)" if cost > 0 else "평가손익 보유 없음")

        for t in data["tickers"]:
            out += ["", f"<b>{_e(t['ticker'])}</b>  {_e(mode_label(t['mode'], t.get('first_half')))}, {t['cycle_count']}회차"]
            out.append(f"T {t['t']:.2f} / {t['n']:g}  <code>{_bar(t['t'], t['n'])}</code>")
            if t["qty"] > 0:
                out.append(f"보유 {t['qty']}주, 평단 ${t['avg']:.2f}, 현재 ${t['now_p']:.2f} ({t['pnl_rt']:+.2f}%)")
                out.append(f"손익 {t['pnl_amt']:+,.2f}")
                if t.get("star_point"):
                    tgt = f", 목표 ${t['target']:.2f}" if t.get("target") else ""
                    out.append(f"별지점 ${t['star_point']:.2f}{tgt}")
            else:
                out.append(f"미보유, 현재 ${t['now_p']:.2f}")
            out.append(f"잔금 ${t['pool']:,.0f} / 원금 ${t['seed']:,.0f}")
            state = "오늘 주문 전송됨" if t["locked"] else "오늘 주문 전"
            if t["unfilled_cnt"]:
                state += f", 미체결 {t['unfilled_qty']}주 ({t['active_cnt']}건)"
            out.append(state)
            if t.get("t_warn"):
                out.append(f"주의: 저장 T {t['t']:.2f}, 보유 원가 추정 T {t['implied_t']:.2f}. 실제와 다르면 /set_t {_e(t['ticker'])}")

        out += ["", "<b>최근 체결</b>"]
        if not data.get("recent_orders"):
            out.append("최근 3일 체결 없음")
        else:
            lines = []
            for o in data["recent_orders"][:10]:
                d = o["ord_dt"]
                dt = f"{d[4:6]}-{d[6:8]}" if len(d) >= 8 else d
                lines.append(f"{dt} {o['ticker']:<5}{o['side_label']} {o['ccld_qty']:>4}주 ${o['avg_fill_pr']:>7.2f} {o['tags']}")
            out.append(pre("\n".join(lines)))
        return "\n".join(out)

    @staticmethod
    def instruction(info: dict) -> str:
        """종목 하나의 오늘 지시서 (읽기 전용 계획)."""
        tk, mode = info["ticker"], info["mode"]
        out = [f"<b>{_e(tk)} 오늘 지시서</b>  {_e(mode_label(mode, info.get('first_half')))}"]
        if info["qty"] > 0:
            pr = (info["price"] / info["avg"] - 1) * 100 if info["avg"] > 0 else 0.0
            out.append(f"현재가 ${info['price']:.2f}, 평단 ${info['avg']:.2f} ({pr:+.2f}%), {info['qty']}주")
        else:
            out.append(f"현재가 ${info['price']:.2f}, 미보유")
        out.append(f"T {info['t']:.2f} / {info['n']:g}  <code>{_bar(info['t'], info['n'])}</code>, 잔금 ${info['pool']:,.0f}")

        if mode == "normal":
            out.append(f"별% {info['star_pct']:.2f} → 별지점 ${info['star_point']:.2f}, 목표 ${info['target']:.2f} (+{info['limit_pct']:g}%)")
            out.append(f"1회 매수금 ${info['one_time']:,.2f} = 잔금 ÷ (N-T)")
        elif mode == "reverse":
            if info.get("day1"):
                out.append(f"리버스 첫날: 보유 수량의 1/{info['divisor']}을 MOC로 매도합니다. 매수는 없습니다.")
            else:
                out.append(f"리버스 별지점(5일 종가 평균) ${info['star_ref']:.2f}, 쿼터매수 금액 ${info['quarter_amt']:,.2f}")
        else:
            out.append(f"1회분 ${info['one_time']:,.2f} = 원금 ÷ N")

        if info.get("reverse_enter"):
            out.append("T가 소진 기준을 넘어 오늘 리버스모드로 전환됩니다.")
        if info.get("reverse_exit"):
            out.append("종가가 회복 기준을 넘어 일반모드로 복귀합니다.")
        orders = info.get("orders") or []
        out.append(pre(_order_rows(orders)) if orders else "오늘 낼 주문이 없습니다.")
        if mode == "first" and not orders:
            out.append("현재가에서 1회분으로 1주도 살 수 없습니다. 시드나 분할 수를 조정하세요.")
        return "\n".join(out)

    @staticmethod
    def order_result(text: str) -> str:
        return pre(text)
