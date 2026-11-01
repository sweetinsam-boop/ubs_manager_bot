"""HTML 리포트 스모크 테스트 (합성 데이터). REPORT_OUT=/경로 를 주면 샘플 HTML을 저장해 눈으로 확인할 수 있다."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_html as rh  # noqa: E402
import v4_rules as r  # noqa: E402


def _ticker(name, avg, qty, price, t, n, pool, seed, tp, reverse=False, warn=False):
    plan = r.plan_reverse(name, n, qty, pool, price * 0.98, False) if reverse else r.plan_normal(name, tp, n, t, pool, avg, qty)
    return dict(
        ticker=name, cycle_count=2, start_date="2026-08-11", seed=seed, n=n, target_p=r.limit_pct(name, tp),
        qty=qty, avg=avg, now_p=price, pnl_amt=(price - avg) * qty, pnl_rt=(price / avg - 1) * 100,
        mode=plan["mode"], reverse=reverse, t=t, pool=pool, one_time=plan.get("one_time", plan.get("quarter_amt", 0)),
        first_half=plan.get("first_half"), star_point=plan.get("star_point") or plan.get("star_ref"), star_pct=plan.get("star_pct"),
        star_buy=plan.get("star_buy"), target=plan.get("target"), orders=plan["orders"], implied_t=t + 3.1,
        t_warn=warn, locked=False, unfilled_cnt=0, unfilled_qty=0, active_cnt=0,
    )


SUMMARY = dict(
    tickers=[_ticker("TQQQ", 52.3, 120, 55.1, 6.5, 20, 5200, 8000, 15),
             _ticker("SOXL", 31.0, 80, 26.2, 18.5, 20, 150, 6000, 20, reverse=True, warn=True)],
    recent_orders=[dict(ticker="TQQQ", ord_dt="20261030", side_label="매수", ccld_qty=4, avg_fill_pr=54.2, tags="LOC", ord_prs=[54.3])],
    current_cash=1550.74, total_cost=8756.0, total_value=8708.0, market_session="장 마감", us_date_display="10/31",
)
PERF = [dict(date=f"2026-10-{d:02d}", equity=10000 + d * 37, benchmark=500 + d, nasdaq=400 + d * 1.2) for d in range(1, 29)]
CUMUL = {"TQQQ": 412.5, "history": [dict(date="2026-08-11 08:00", ticker="TQQQ", profit=412.5, sell_qty=60, sell_price=61.2, avg_price=54.3)], "processed_orders": []}
CYCLES = {"TQQQ": {"current_cycle": 2, "active": {"cycle_num": 2, "start_date": "2026-08-11"},
                   "history": [{"cycle_num": 1, "start_date": "2026-06-25", "end_date": "2026-08-11"}]}}


class ReportSmoke(unittest.TestCase):
    def test_full_report(self):
        text = rh.build_report_html(SUMMARY, PERF, CUMUL, CYCLES, "2026-11-01 09:00")
        for needle in ("무한매수 운용 리포트", "TQQQ", "소진 후 리버스모드", "가격 사다리", "/set_t SOXL", "<polyline"):
            self.assertIn(needle, text)
        out = os.environ.get("REPORT_OUT")
        if out:
            with open(os.path.join(out, "report.html"), "w", encoding="utf-8") as f:
                f.write(text)

    def test_empty_and_error_states(self):
        empty = dict(tickers=[], recent_orders=[], current_cash=0, total_cost=0, total_value=0, market_session="", us_date_display="")
        text = rh.build_report_html(empty, [], {}, {}, "2026-11-01 09:00", "API 오류 <예시>")
        self.assertIn("등록된 종목이 없습니다", text)
        self.assertIn("API 오류 &lt;예시&gt;", text)  # 사용자 문자열은 이스케이프
        out = os.environ.get("REPORT_OUT")
        if out:
            with open(os.path.join(out, "report_empty.html"), "w", encoding="utf-8") as f:
                f.write(text)

    def test_no_literal_colors_outside_token_block(self):
        body = rh._CSS.split("}", 1)[1]  # :root 토큰 블록 뒤
        self.assertNotIn("#", body)
        self.assertNotIn("rgb(", body)


if __name__ == "__main__":
    unittest.main()
