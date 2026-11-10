"""주문·정산 흐름 검증. 증권사 호출과 상태 파일을 가짜로 바꿔 실제 계좌·storage/ 를 건드리지 않는다.

의존 패키지가 없는 환경에서는 건너뛴다. 실행: python3 -m unittest discover tests
"""
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import requests

    import kis_client
    import state_manager
    import strategy
    import utils
    import v4_state
    import scheduler
    from trade_service import RetryLater, TradeService
except ImportError:  # 로컬에 의존 패키지가 없을 때
    kis_client = None

OK = {"rt_cd": "0", "output": {"ODNO": "99"}}
REJECT = {"rt_cd": "1", "msg1": "주문가능금액 부족"}


def _entry(qty=0.0, avg=0.0, seed=8000.0, n=20.0):
    return {"seed": seed, "total_a": n, "target_profit": 15.0, "liquidating": False,
            "data": {"avg": avg, "qty": qty, "cumul": avg * qty}}


def _balance(qty, avg, ticker="TQQQ"):
    return {"rt_cd": "0", "output1": [{"ovrs_pdno": ticker, "ovrs_cblc_qty": str(qty), "pchs_avg_pric": str(avg)}]}


class _Resp:
    def __init__(self, body, tr_cont=""):
        self._body, self.headers, self.text = body, {"tr_cont": tr_cont}, ""

    def json(self):
        return self._body


@unittest.skipIf(kis_client is None, "의존 패키지 없음")
class FlowCase(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.history = []
        self.locks = set()
        patches = [
            mock.patch.dict(state_manager.STRATEGY, {"TQQQ": _entry()}, clear=True),
            mock.patch.object(state_manager, "CURRENT_CASH", 0.0),
            mock.patch.object(v4_state, "load_v4_state", lambda: copy.deepcopy(self.store)),
            mock.patch.object(v4_state, "save_v4_state", lambda d: self.store.update(copy.deepcopy(d))),
            mock.patch.object(utils, "load_cycle_state",
                              lambda: {"TQQQ": {"current_cycle": 1, "active": {"start_date": "2026-10-01"}}}),
            mock.patch.object(utils, "load_cumul_profit", lambda: {"history": self.history, "processed_orders": []}),
            mock.patch.object(utils, "check_trade_lock", lambda tk: tk in self.locks),
            mock.patch.object(utils, "set_trade_lock", self.locks.add),
            mock.patch.object(utils, "start_cycle_for_new_entry", lambda *a, **k: (1, False)),
            mock.patch.object(utils, "get_cycle_timeline", lambda tk: {}),
            mock.patch.object(kis_client, "get_current_price", lambda tk: 50.0),
            mock.patch.object(kis_client, "get_prev_close", lambda tk: 50.0),
            mock.patch.object(kis_client, "query_unfilled_orders_today_us", lambda tk: []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.send = mock.patch.object(kis_client, "send_order", return_value=OK).start()
        self.addCleanup(mock.patch.stopall)

    def hold(self, qty, avg):
        state_manager.STRATEGY["TQQQ"]["data"] = {"avg": avg, "qty": float(qty), "cumul": avg * qty}


class FirstBuy(FlowCase):
    def setUp(self):
        super().setUp()
        self.store["TQQQ"] = {"t": 7.0, "pool": 500.0,
                              "orders": [{"ymd": "2026-11-09", "odno": "1", "kind": "star", "settled": False}]}

    def test_rejected_first_buy_keeps_previous_state(self):
        self.send.return_value = REJECT
        _, ok = strategy.execute_all_order("TQQQ", strategy.build_order_info("TQQQ"))
        self.assertEqual(ok, 0)
        self.assertEqual(self.store["TQQQ"]["t"], 7.0)
        self.assertEqual(len(self.store["TQQQ"]["orders"]), 1)  # 정산 대기 주문도 그대로

    def test_accepted_first_buy_starts_new_cycle(self):
        _, ok = strategy.execute_all_order("TQQQ", strategy.build_order_info("TQQQ"))
        self.assertEqual(ok, 1)
        rec = self.store["TQQQ"]
        self.assertEqual((rec["t"], rec["pool"]), (0.0, 8000.0))
        self.assertEqual([(o["kind"], o["odno"]) for o in rec["orders"]], [("first", "99")])


class DailyOrder(FlowCase):
    def test_no_order_when_balance_unknown(self):
        with mock.patch.object(kis_client, "query_account_raw", return_value=None):
            self.assertRaises(RetryLater, TradeService.process_ticker_daily, "TQQQ")
        self.send.assert_not_called()
        self.assertEqual(self.store, {})
        self.assertEqual(self.locks, set())  # 잠그지 않아야 다시 시도할 수 있다

    def test_memory_zero_but_holding_does_not_first_buy(self):
        # 재시작 직후 동기화가 빠져 메모리는 0주지만 실제로는 100주를 들고 있는 경우
        with mock.patch.object(kis_client, "query_account_raw", return_value=_balance(100, 50.0)):
            TradeService.process_ticker_daily("TQQQ")
        self.assertEqual(state_manager.STRATEGY["TQQQ"]["data"]["qty"], 100.0)
        kinds = {o["kind"] for o in self.store["TQQQ"]["orders"]}
        self.assertNotIn("first", kinds)
        self.assertIn("sell_limit", kinds)

    def test_skips_when_broker_already_has_orders(self):
        with mock.patch.object(kis_client, "query_unfilled_orders_today_us", return_value=[{"ord_no": "1"}]):
            msg = TradeService.process_ticker_daily("TQQQ")
        self.send.assert_not_called()
        self.assertIn("TQQQ", self.locks)
        self.assertIn("미체결", msg)

    def test_no_order_when_unfilled_query_fails(self):
        with mock.patch.object(kis_client, "query_unfilled_orders_today_us", side_effect=RuntimeError("조회 실패")):
            self.assertRaises(RetryLater, TradeService.process_ticker_daily, "TQQQ")
        self.send.assert_not_called()


class Settlement(FlowCase):
    def test_fill_query_failure_defers_settlement(self):
        self.hold(100, 50.0)
        self.store["TQQQ"] = {"t": 5.0, "pool": 4000.0, "orders": [
            {"ymd": "2026-11-02", "odno": "1", "kind": "star", "half": "first", "settled": False}]}
        with mock.patch.object(kis_client, "fills_by_order", return_value=None):
            self.assertEqual(strategy.settle_ticker("TQQQ"), [])
        self.assertFalse(self.store["TQQQ"]["orders"][0]["settled"])
        with mock.patch.object(kis_client, "fills_by_order", return_value={"1": (10, 500.0)}):
            strategy.settle_ticker("TQQQ")
        self.assertEqual((self.store["TQQQ"]["t"], self.store["TQQQ"]["pool"]), (5.5, 3500.0))

    def test_cycle_not_closed_on_single_empty_balance(self):
        with mock.patch.object(kis_client, "query_account_raw", return_value=_balance(100, 50.0)), \
                mock.patch.object(utils, "close_active_cycle_on_zero") as close, \
                mock.patch.object(v4_state, "reset_record") as reset:
            TradeService._reconcile_maybe_close_cycle_on_zero("TQQQ", [])
        close.assert_not_called()
        reset.assert_not_called()


class PoolEstimate(FlowCase):
    def test_realized_loss_is_not_in_pool(self):
        self.hold(50, 150.0)  # 보유 원가 7,500
        self.history += [
            {"ticker": "TQQQ", "date": "2026-11-03 06:40", "profit": -414.0},  # 이번 사이클 손절
            {"ticker": "TQQQ", "date": "2026-09-01 06:40", "profit": 100.0},   # 지난 사이클
            {"ticker": "SOXL", "date": "2026-11-03 06:40", "profit": -50.0},
        ]
        _, pool = strategy.recalibrate("TQQQ", 12.0)
        self.assertEqual(pool, 8000.0 - 7500.0 - 414.0)

    def test_tickers_share_cash_by_pool(self):
        state_manager.STRATEGY["SOXL"] = _entry(qty=10, avg=30.0)
        self.hold(10, 50.0)
        self.store.update({"TQQQ": {"t": 3.0, "pool": 3000.0}, "SOXL": {"t": 3.0, "pool": 1000.0}})
        with mock.patch.object(state_manager, "CURRENT_CASH", 2000.0):
            self.assertEqual(strategy.build_order_info("TQQQ")["pool"], 1500.0)
            self.assertEqual(strategy.build_order_info("SOXL")["pool"], 500.0)


@unittest.skipIf(kis_client is None, "의존 패키지 없음")
class DailyRetry(unittest.IsolatedAsyncioTestCase):
    """조회 실패로 못 보낸 종목만 다시 시도한다."""

    def setUp(self):
        self.sent = []

        async def html(chat_id, text):
            self.sent.append(text)

        self.sleep = mock.AsyncMock()
        for target, attr, new in (
            (state_manager.STRATEGY, None, {"TQQQ": _entry(), "SOXL": _entry()}),
            (scheduler, "_html", html),
            (scheduler.asyncio, "sleep", self.sleep),
            (state_manager, "sync_account", lambda: (True, "ok")),
            (TradeService, "settle_fills", lambda: []),
            (kis_client, "query_unfilled_orders_today_us", lambda tk: []),
        ):
            p = mock.patch.dict(target, new, clear=True) if attr is None else mock.patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    async def test_only_failed_ticker_is_retried(self):
        calls = []

        def process(ticker):
            calls.append(ticker)
            if ticker == "SOXL" and calls.count("SOXL") == 1:
                raise RetryLater("증권사 잔고를 확인하지 못했습니다")
            return f"{ticker} 주문 완료"

        with mock.patch.object(TradeService, "process_ticker_daily", process):
            await scheduler._run_daily_market(1)
        self.assertEqual(calls, ["TQQQ", "SOXL", "SOXL"])  # TQQQ는 다시 보내지 않는다
        self.sleep.assert_any_await(scheduler.RETRY_EVERY)
        self.assertTrue(any("주문 보류" in t for t in self.sent))
        self.assertFalse(any("자동 주문 실패" in t for t in self.sent))

    async def test_gives_up_after_max_retries(self):
        process = mock.Mock(side_effect=RetryLater("증권사 잔고를 확인하지 못했습니다"))
        with mock.patch.object(TradeService, "process_ticker_daily", process):
            await scheduler._run_daily_market(1)
        self.assertEqual(process.call_count, 2 * (1 + scheduler.RETRY_MAX))
        self.assertTrue(any("자동 주문 실패" in t and "/manual_order" in t for t in self.sent))

    async def test_sync_failure_is_retried_without_ordering(self):
        results = iter([(False, "점검 중")] * 3 + [(True, "ok")])
        process = mock.Mock(return_value="주문 완료")
        with mock.patch.object(state_manager, "sync_account", lambda: next(results)), \
                mock.patch.object(TradeService, "process_ticker_daily", process):
            await scheduler._run_daily_market(1)
        self.assertEqual(process.call_count, 2)  # 동기화가 된 두 번째 회차에서만 두 종목 주문


@unittest.skipIf(kis_client is None, "의존 패키지 없음")
class KisClient(unittest.TestCase):
    def setUp(self):
        for name, value in (("get_kis_access_token", "tok"), ("get_kis_hashkey", "")):
            p = mock.patch.object(kis_client, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(kis_client.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_timeout_is_not_resent(self):
        with mock.patch.object(kis_client.requests, "post", side_effect=requests.ReadTimeout("느림")) as post:
            res = kis_client.send_order("TQQQ", 1, 50.0, "BUY", "LOC")
        self.assertEqual(post.call_count, 1)
        self.assertIn("접수 여부 미확인", res["msg1"])

    def test_rejection_is_not_resent(self):
        with mock.patch.object(kis_client.requests, "post", return_value=_Resp(REJECT)) as post:
            kis_client.send_order("TQQQ", 1, 50.0, "BUY", "LOC")
        self.assertEqual(post.call_count, 1)

    def test_rate_limit_is_retried(self):
        seq = [_Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}), _Resp(OK)]
        with mock.patch.object(kis_client.requests, "post", side_effect=seq) as post:
            res = kis_client.send_order("TQQQ", 1, 50.0, "BUY", "LOC")
        self.assertEqual((post.call_count, res["rt_cd"]), (2, "0"))

    def test_pagination_follows_response_header(self):
        pages = [_Resp({"rt_cd": "0", "output": [{"odno": "1"}], "ctx_area_nk200": "k"}, tr_cont="M"),
                 _Resp({"rt_cd": "0", "output": [{"odno": "2"}]}, tr_cont="D")]
        with mock.patch.object(kis_client.requests, "get", side_effect=pages) as get:
            rows = kis_client.query_ccnl_collect_all_pages("TQQQ", "NASD", "20261101", "20261110")
        self.assertEqual([r["odno"] for r in rows], ["1", "2"])
        self.assertEqual(get.call_args_list[1].kwargs["headers"]["tr_cont"], "N")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["CTX_AREA_NK200"], "k")

    def test_partial_fill_history_is_not_trusted(self):
        pages = [_Resp({"rt_cd": "0", "output": [{"odno": "1", "ft_ccld_qty": "3", "ft_ccld_unpr3": "50"}]}, tr_cont="M"),
                 _Resp({"rt_cd": "1", "msg1": "오류"})]
        with mock.patch.object(kis_client.requests, "get", side_effect=pages):
            self.assertIsNone(kis_client.fills_by_order("TQQQ"))


if __name__ == "__main__":
    unittest.main()
