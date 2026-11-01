from __future__ import annotations

import datetime
from collections import defaultdict

import pytz

import kis_client
import state_manager
import strategy
import utils
import v4_state
from bot_logging import get_logger

log = get_logger(__name__)


class TradeService:
    """
    비즈니스/통신 파사드 레이어 (V4.0 전용).
    증권사 API와 전략/상태 모듈들을 엮은 결과를 View 레이어로 넘깁니다.
    """

    @staticmethod
    def get_portfolio_summary() -> dict:
        """잔고·종목별 T/잔금/오늘 계획·최근 체결을 한 덩어리로 반환 (텔레그램 현황·HTML 리포트 공용)."""
        try:
            state_manager.sync_account()
        except Exception as e:
            log.warning("sync_account failed in get_portfolio_summary: %s", e)

        now_ny = datetime.datetime.now(pytz.timezone('America/New_York'))
        data = {
            "title": "무한매수 운용 현황",
            "recorded_at": datetime.datetime.now(pytz.timezone('Asia/Seoul')).strftime('%m-%d %H:%M:%S'),
            "market_session": utils.get_market_session_kr(),
            "is_closed": utils.get_market_session() == 'closed',
            "us_date_display": now_ny.strftime('%m/%d'),
            "tickers": [],
            "recent_orders": [],
            "current_cash": state_manager.CURRENT_CASH,
            "total_cost": 0.0,
            "total_value": 0.0,
        }

        try:
            orders = kis_client.query_ccnl_for_report(days_back=3, max_pages=3)
            data["recent_orders"] = TradeService._parse_recent_orders(orders)
        except Exception:
            data["recent_orders"] = []

        cycles = utils.load_cycle_state()
        for ticker, s in state_manager.STRATEGY.items():
            d = s["data"]
            qty = int(d.get("qty", 0))
            avg = float(d.get("avg", 0.0))
            info = strategy.build_order_info(ticker)
            now_p = float(info.get("price") or 0.0)
            pnl_amt = pnl_rt = 0.0
            if qty > 0 and avg > 0 and now_p > 0:
                pnl_amt = (now_p - avg) * qty
                pnl_rt = (now_p - avg) / avg * 100
                data["total_cost"] += avg * qty
                data["total_value"] += now_p * qty

            locked = utils.check_trade_lock(ticker)
            try:
                unfilled = kis_client.query_unfilled_orders_today_us(ticker) or []
                if unfilled and not locked:
                    locked = True
                    utils.set_trade_lock(ticker)
            except Exception:
                unfilled = []

            c_state = cycles.get(ticker, {})
            active = c_state.get("active")
            implied = strategy.implied_t_now(ticker) if qty > 0 else 0.0
            data["tickers"].append({
                "ticker": ticker,
                "cycle_count": c_state.get("current_cycle", 1),
                "start_date": active.get("start_date", "대기") if active else "대기",
                "seed": float(s["seed"]),
                "n": float(s["total_a"]),
                "target_p": strategy.profit_target_pct(ticker),
                "qty": qty, "avg": avg, "now_p": now_p,
                "pnl_amt": pnl_amt, "pnl_rt": pnl_rt,
                "mode": info["mode"],
                "reverse": info["mode"] == "reverse",
                "t": info.get("t", 0.0), "pool": info.get("pool", 0.0),
                "one_time": info.get("one_time", info.get("quarter_amt", 0.0)),
                "first_half": info.get("first_half"),
                "star_point": info.get("star_point") or info.get("star_ref"), "star_pct": info.get("star_pct"),
                "star_buy": info.get("star_buy"), "target": info.get("target"),
                "orders": info.get("orders", []),
                "implied_t": implied,
                "t_warn": qty > 0 and abs(info.get("t", 0.0) - implied) > 2.0,
                "locked": locked,
                "unfilled_cnt": len(unfilled),
                "unfilled_qty": sum(int(u.get("nccs_qty", u.get("qty", 0)) or 0) for u in unfilled),
                "active_cnt": sum(1 for u in unfilled if u.get("status") == "대기"),
            })
        return data

    @staticmethod
    def _parse_recent_orders(orders: list) -> list:
        if not orders:
            return []

        seen_ord_nos = set()
        rows = []
        for o in sorted(orders, key=lambda x: (x.get("ord_dt") or "", x.get("ord_tmd") or "", x.get("odno") or ""), reverse=True):
            ord_no_s = str(o.get("odno")).strip() if o.get("odno") is not None else ""
            if ord_no_s and ord_no_s in seen_ord_nos:
                continue
            if ord_no_s:
                seen_ord_nos.add(ord_no_s)

            ccld_qty = int(float(o.get("ft_ccld_qty", "0")))
            if ccld_qty <= 0:
                continue

            ticker = (o.get("pdno") or "?").strip()
            side_label = "매수" if str(o.get("sll_buy_dvsn_cd", "") or "").strip() == "02" else "매도"
            type_hint = " ".join(x for x in [(o.get("ord_dvsn_cd_name") or "").strip(), (o.get("sll_buy_dvsn_cd_name") or "").strip()] if x).strip()
            if not type_hint:
                type_hint = side_label

            rows.append({
                "ticker": ticker, "ord_dt": (o.get("ord_dt") or "").strip(), "side_label": side_label,
                "type_hint": type_hint,
                "ord_pr": float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", "0"))),
                "ccld_qty": ccld_qty,
                "ccld_pr": float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0"))),
            })

        by_grp = defaultdict(list)
        for r in rows:
            by_grp[(r["ticker"], r["ord_dt"], r["side_label"])].append(r)

        result = []
        for (tkr, dt, side), grp in by_grp.items():
            tot_ccld = sum(x["ccld_qty"] for x in grp)
            avg_fill_pr = sum(x["ccld_pr"] * x["ccld_qty"] for x in grp) / tot_ccld if tot_ccld > 0 else 0
            types = [x["type_hint"] for x in grp]

            tags = "주문"
            if all("LOC" in t for t in types):
                tags = "LOC"
            elif all("MOC" in t for t in types):
                tags = "MOC"
            elif all("시장가" in t or "Market" in t for t in types):
                tags = "시장가"

            result.append({
                "ticker": tkr, "ord_dt": dt, "side_label": side,
                "ccld_qty": tot_ccld, "avg_fill_pr": avg_fill_pr, "tags": tags,
                "ord_prs": sorted({round(x["ord_pr"], 2) for x in grp if x["ord_pr"] > 0}),
            })
        return result

    @staticmethod
    def execute_daily_order(ticker: str) -> str:
        """오늘 치 전략 주문을 만들어 전송하고, 접수된 주문이 있으면 오늘 잠금을 건다."""
        try:
            info = strategy.build_order_info(ticker)
            text, ok = strategy.execute_all_order(ticker, info)
            if ok <= 0:
                log.warning("%s 일일 주문 집행 결과: %s", ticker, text)
                return text

            utils.set_trade_lock(ticker)
            log.info("%s 일일 주문 집행 성공 및 잠금 설정", ticker)
            if info["mode"] != "first":
                return text

            # 0주에서 시작한 첫 매수는 '새 사이클 진입'으로 보고 회차를 반영한다.
            new_cycle, inc = utils.start_cycle_for_new_entry(ticker, increment_on_reentry=True)
            tl = utils.get_cycle_timeline(ticker)
            head = f"사이클 회차 {'증가' if inc else '유지'}: {new_cycle}회차"
            return (
                f"{text}\n\n{head}\n"
                f"- 이전 회차: 시작 {tl.get('previous_start', '-')} / 종료 {tl.get('previous_end', '-')}\n"
                f"- 현재 회차 시작일: {tl.get('current_start', '-')}"
            )
        except Exception as e:
            error_msg = f"{ticker} 주문 집행 중 서버 오류: {e}"
            log.error(error_msg)
            return error_msg

    @staticmethod
    def _verify_broker_qty(ticker: str):
        """증권사 실잔고에서 실제 보유수량을 직접 조회. 조회 실패 시 None."""
        try:
            res = kis_client.query_account_raw()
            if not res or res.get('rt_cd') != '0':
                return None
            for item in res.get('output1', []):
                if item.get('ovrs_pdno', '').strip() == ticker:
                    return int(float(item.get('ovrs_cblc_qty', '0')))
            return 0  # 종목이 output1에 없음 = 보유 0주
        except Exception as e:
            log.warning("_verify_broker_qty(%s) failed: %s", ticker, e)
            return None

    @staticmethod
    def process_ticker_daily(ticker: str) -> str:
        """전량 익절 체크 → 일반 주문 집행 순으로 처리하고 결과 메시지를 반환."""
        try:
            if utils.check_trade_lock(ticker):
                return f"[안전장치] {ticker}: 오늘 이미 매매 기록 있음. 건너뜀."

            s = state_manager.STRATEGY[ticker]
            if s.get("liquidating"):
                return f"[안전장치] {ticker}: 청산 대기 중. 매매 건너뜀."

            d = s["data"]
            avg_p = d.get("avg", 0)
            qty_t = int(d.get("qty", 0))
            results_list = []

            # 메모리상 수량이 있으면 증권사 실잔고로 한 번 더 확인 (동기화 누락으로 없는 수량을 파는 사고 방지)
            if qty_t > 0:
                actual_qty = TradeService._verify_broker_qty(ticker)
                if actual_qty is not None and actual_qty != qty_t:
                    log.warning("%s pre-order verify: memory qty=%d → broker qty=%d", ticker, qty_t, actual_qty)
                    d["qty"] = float(actual_qty)
                    d["cumul"] = float(d.get("avg", 0.0)) * actual_qty if actual_qty > 0 else 0.0
                    if actual_qty <= 0:
                        d["avg"] = 0.0
                    results_list.append(f"[{ticker}] 잔고 불일치 보정: 메모리 {qty_t}주 → 실잔고 {actual_qty}주")
                    qty_t = actual_qty
                    avg_p = float(d.get("avg", 0))

            # 현재가가 이미 3/4 지정가 목표 이상이면 전량 MOC 익절 (공식 규칙은 지정가 3/4 + 쿼터 LOC이므로 아래 일반 주문으로도 정리됨)
            profit_take_sent = target_reached = False
            if avg_p > 0 and qty_t > 0:
                tgt_pct = strategy.profit_target_pct(ticker)
                if kis_client.get_current_price(ticker) >= avg_p * (1 + tgt_pct / 100):
                    target_reached = True
                    res_sell = kis_client.send_order(ticker, qty_t, 0, "SELL", "MOC")
                    if res_sell.get("rt_cd") == "0":
                        results_list.append(
                            f"[{ticker}] 목표가 달성(+{tgt_pct:.1f}%). 전량 익절 MOC 매도 {qty_t}주 전송 성공.\n"
                            f"  (실현수익은 장 마감 후 자동 동기화 시 기록됩니다.)"
                        )
                        profit_take_sent = True
                    else:
                        results_list.append(f"[{ticker}] 수익실현 MOC 매도 주문 실패: {res_sell.get('msg1', '')}")

            # 목표가에 도달한 날은 매도 성공/실패와 무관하게 추가 매수를 하지 않는다 (평단 왜곡 방지)
            if not target_reached:
                results_list.append(TradeService.execute_daily_order(ticker))
            else:
                if not profit_take_sent:
                    results_list.append(f"[{ticker}] 목표가 도달 상태이나 매도 실패. 추가 매수를 건너뜁니다.")
                utils.set_trade_lock(ticker)
            return "\n".join(results_list)

        except Exception as e:
            msg = f"{ticker} 처리 중 오류: {e}"
            log.error(msg)
            return msg

    @staticmethod
    def settle_fills() -> list[str]:
        """모든 종목의 지난 주문 체결을 T·잔금에 반영 (멱등)."""
        msgs: list[str] = []
        for tk in list(state_manager.STRATEGY):
            try:
                msgs += strategy.settle_ticker(tk)
            except Exception as e:
                log.warning("settle_fills %s failed: %s", tk, e, exc_info=True)
        return msgs

    @staticmethod
    def capture_pre_sync_avgs() -> dict[str, float]:
        """
        동기화 전 각 종목의 평단가를 캡처 (장 마감 후 매도 체결 수익 계산에는 매도 전 평단이 필요).
        (1) 메모리상 보유 종목, (2) last_known에만 평단이 남은 종목,
        (3) 사이클 active인데 수량 0인 종목의 합집합.
        """
        result: dict[str, float] = {}
        for ticker in state_manager.STRATEGY:
            d = state_manager.STRATEGY[ticker].get("data", {})
            if float(d.get("qty", 0) or 0) > 0 and float(d.get("avg", 0) or 0) > 0:
                result[ticker] = float(d["avg"])

        lk = utils.load_last_known_hold_avgs()
        for ticker in state_manager.STRATEGY:
            if lk.get(ticker, 0) > 0 and ticker not in result:
                result[ticker] = float(lk[ticker])

        cycle_state = utils.load_cycle_state()
        for ticker in state_manager.STRATEGY:
            d = state_manager.STRATEGY[ticker].get("data", {}) or {}
            info = cycle_state.get(ticker) or {}
            if info.get("active") is None or float(d.get("qty", 0) or 0) > 0 or ticker in result:
                continue
            use_avg = lk.get(ticker, 0) or float(d.get("avg", 0) or 0)
            if use_avg > 0:
                result[ticker] = float(use_avg)
        return result

    @staticmethod
    def _reconcile_maybe_close_cycle_on_zero(ticker: str, messages: list[str]) -> None:
        """동기화 후 수량 0이고 cycle_state에 active가 남아 있으면 사이클 종료 + V4 상태 초기화."""
        post_qty = float((state_manager.STRATEGY.get(ticker, {}).get("data") or {}).get("qty", 0) or 0)
        has_active_cycle = utils.load_cycle_state().get(ticker, {}).get("active") is not None
        if post_qty > 0 or not has_active_cycle:
            return

        utils.close_active_cycle_on_zero(ticker)
        new_count = utils.load_cycle_state().get(ticker, {}).get("current_cycle", 1)
        messages.append(
            f"{ticker} 투자 사이클 종료. (회차는 다음 재진입 때 +1, 현재 {new_count}회차)"
        )
        log.info("active cycle closed on zero qty: %s (current_cycle=%s)", ticker, new_count)

        lk_rem = utils.load_last_known_hold_avgs()
        lk_rem.pop(ticker, None)
        utils.save_last_known_hold_avgs(lk_rem)
        v4_state.reset_record(ticker)

    @staticmethod
    def reconcile_daily_sell_profits(pre_sync_avgs: dict[str, float]) -> list[str]:
        """장 마감 후 최근 체결된 매도 주문의 실현 수익을 cumul_profit에 반영하고, 전량 매도된 사이클을 닫는다."""
        messages: list[str] = []
        if not pre_sync_avgs:
            return messages

        now_ny = datetime.datetime.now(pytz.timezone("America/New_York"))
        end_dt = now_ny.strftime("%Y%m%d")
        # 한국 시각으로 다음 날·봇 재시작 후에도 전 거래일 MOC가 누락되지 않게 넉넉히 조회 (중복은 주문번호로 방지)
        start_dt = (now_ny - datetime.timedelta(days=7)).strftime("%Y%m%d")

        for ticker, avg_p in pre_sync_avgs.items():
            if avg_p <= 0:
                TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)
                continue
            try:
                orders = kis_client.query_ccnl_collect_all_pages_multi_excg(
                    ticker=ticker, start_dt=start_dt, end_dt=end_dt, ccld_dvsn="01", max_pages=8,
                )
                sell_by_odno: dict[str, dict] = {}
                for o in orders or []:
                    side_cd = str(o.get("sll_buy_dvsn_cd", "") or "").strip()
                    if side_cd == "02":  # TTTS3035R: 01=매도, 02=매수
                        continue
                    if side_cd not in ("01", "1") and "매도" not in str(o.get("sll_buy_dvsn_cd_name", "") or ""):
                        continue
                    ccld_qty = int(float(o.get("ft_ccld_qty", "0") or 0))
                    ccld_pr = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0") or 0) or 0)
                    odno = str(o.get("odno") or "").strip()
                    if ccld_qty <= 0 or ccld_pr <= 0 or not odno:
                        continue
                    if odno not in sell_by_odno or ccld_qty > sell_by_odno[odno]["qty"]:
                        sell_by_odno[odno] = {"qty": ccld_qty, "price": ccld_pr}

                for odno, fill in sell_by_odno.items():
                    realized = (fill["price"] - avg_p) * fill["qty"]
                    cumul_total, recorded = strategy.add_cumul_profit(
                        ticker, realized, fill["qty"], fill["price"], avg_p, order_no=odno,
                    )
                    if recorded:
                        messages.append(
                            f"{ticker} 매도 체결 수익 반영: {fill['qty']}주 @ ${fill['price']:.2f} "
                            f"(평단 ${avg_p:.2f}) → 실현수익 ${realized:+,.2f} (누적 ${cumul_total:,.2f})"
                        )
                        log.info("reconcile sell profit: %s %d @ %.2f avg=%.2f realized=%.2f odno=%s",
                                 ticker, fill["qty"], fill["price"], avg_p, realized, odno)
                TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)
            except Exception as e:
                log.warning("reconcile sell profit for %s failed: %s", ticker, e)

        # pre_sync에 평단이 없었지만 사이클 종료가 필요한 종목
        for ticker in state_manager.STRATEGY:
            if ticker not in pre_sync_avgs:
                TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)
        return messages

    @staticmethod
    def sync_all_states():
        """계좌 잔고·예수금 동기화 (T와 잔금은 체결 정산에서 갱신된다)."""
        try:
            state_manager.sync_account()
            log.info("모든 종목 및 자산 상태 동기화 완료")
        except Exception as e:
            log.error(f"상태 동기화 중 오류 발생: {e}")
            raise
