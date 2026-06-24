from __future__ import annotations

import math
import datetime
import pytz
from collections import defaultdict

import kis_client
import strategy
import state_manager
import utils
import v4_state
from config_manager import STRATEGY_SETTINGS, ENABLE_PREMARKET
from bot_logging import get_logger

log = get_logger(__name__)

class TradeService:
    """
    비즈니스/통신 파사드 레이어.
    증권사 API와 전략/상태 모듈들을 엮은 결과를 View 레이어로 넘깁니다.
    """

    @staticmethod
    def get_portfolio_summary(is_full: bool = False) -> dict:
        """
        잔고 및 포트폴리오 요약 정보와 최근 주문 내역, 시장 상태 등을 딕셔너리로 반환합니다.
        is_full: 스케줄러·/status 호환용 인자(과거 뉴스 섹션용). 현재는 True/False 모두 동일 구성.
        """
        _ = is_full  # API 호환 유지
        try:
            state_manager.sync_account()
        except Exception as e:
            log.warning("sync_account failed in get_portfolio_summary: %s", e)

        data = {
            "title": "[무한매수 실시간 운용 현황]",
            "recorded_at": datetime.datetime.now(pytz.timezone('Asia/Seoul')).strftime('%m-%d %H:%M:%S'),
            "market_session": utils.get_market_session_kr(),
            "is_closed": utils.get_market_session() == 'closed',
            "premarket_on": ENABLE_PREMARKET,
            "tickers": [],
            "recent_orders": [],
            "current_cash": state_manager.CURRENT_CASH,
            "total_cost": 0.0,
            "total_value": 0.0,
            "methods_in_use": set()
        }

        # 시장 세부 시간 (NY 기준)
        ny_tz = pytz.timezone('America/New_York')
        now_ny = datetime.datetime.now(ny_tz)
        data["us_date_display"] = now_ny.strftime('%m/%d')

        # 최근 체결 내역 요약
        try:
            orders = kis_client.query_ccnl_for_report(excg_list=["NASD", "NYSE", "AMEX"], days_back=3, max_pages=3)
            data["recent_orders"] = TradeService._parse_recent_orders(orders)
        except Exception:
            data["recent_orders"] = []

        # 개별 종목 요약 (미체결은 미국 동부 당일만 — /status·리포트와 스케줄 확인과 동일 기준)
        for ticker in state_manager.STRATEGY:
            s_dict = state_manager.STRATEGY[ticker]
            d_dict = s_dict["data"]
            method = s_dict.get("method") or "2.2"
            data["methods_in_use"].add(method)
            
            qty = int(d_dict.get("qty", 0))
            avg = float(d_dict.get("avg", 0.0))
            cumul_f = float(d_dict.get("cumul", 0) or 0)
            seed = float(s_dict["seed"])
            total_a = float(s_dict["total_a"])
            target_p = strategy.profit_target_pct(ticker)
            one_split = strategy.one_time_for_strategy(s_dict)
            
            t_disp = ""
            if method == "4.0":
                tr = v4_state.get_record(ticker).get("t")
                t_val = float(tr) if tr is not None else 0.0
                t_disp = f"T={t_val:.4f} / 분할 {int(total_a)} (V4.0)"
            elif method == "3.0":
                if one_split > 0:
                    t_val = strategy.t_value_v30(cumul_f, one_split)
                    t_disp = f"T={t_val:.2f} / 분할 {int(total_a)} (V3.0 올림)"
                else:
                    t_disp = "T - (1회분 없음)"
            else:
                if one_split > 0:
                    t_dec = round(cumul_f / one_split, 2) if cumul_f > 0 else 0.0
                    progress = (avg * qty) / one_split if qty > 0 and avg > 0 else 0.0
                    progress_int = int(math.ceil(progress)) if progress > 0 else 0
                    t_disp = f"T={t_dec:.2f} / 정수분할 {progress_int}/{int(total_a)} (V2.2 근사)"
                else:
                    t_disp = "T - (1회분 없음)"

            c_state = utils.load_cycle_state().get(ticker, {})
            current_cycle = c_state.get("current_cycle", 1)
            active_info = c_state.get("active")
            start_date = active_info.get("start_date", "대기") if active_info else "대기"
            now_p = 0.0
            pnl_amt = 0.0
            pnl_rt = 0.0
            if qty > 0 and avg > 0:
                try:
                    now_p = float(kis_client.get_current_price(ticker) or 0)
                    pnl_amt = (now_p - avg) * qty
                    pnl_rt = ((now_p - avg) / avg * 100) if avg > 0 else 0.0
                    data["total_cost"] += (avg * qty)
                    data["total_value"] += (now_p * qty)
                except Exception:
                    pass

            # 잠금/미체결 상태
            locked = utils.check_trade_lock(ticker)
            try:
                unfilled = kis_client.query_unfilled_orders_today_us(ticker) or []
                locked_by_broker = bool(unfilled)
                if locked_by_broker and not locked:
                    locked = True
                    try:
                        utils.set_trade_lock(ticker)
                    except Exception:
                        pass
            except Exception:
                unfilled = []

            data["tickers"].append({
                "ticker": ticker,
                "method_disp": strategy.format_method_display(method),
                "cycle_count": current_cycle,
                "start_date": start_date,
                "seed": seed,
                "target_p": target_p,
                "qty": qty,
                "avg": avg,
                "t_disp": t_disp,
                "now_p": now_p,
                "pnl_amt": pnl_amt,
                "pnl_rt": pnl_rt,
                "locked": locked,
                "unfilled_cnt": len(unfilled),
                "unfilled_qty": sum(int(u.get("nccs_qty", u.get("qty", 0)) or 0) for u in unfilled),
                "active_cnt": sum(1 for u in unfilled if u.get("status") == "대기")
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
            if ord_no_s and ord_no_s in seen_ord_nos: continue
            if ord_no_s: seen_ord_nos.add(ord_no_s)

            ccld_qty = int(float(o.get("ft_ccld_qty", "0")))
            if ccld_qty <= 0: continue

            ticker = (o.get("pdno") or "?").strip()
            side_label = "매수" if str(o.get("sll_buy_dvsn_cd", "") or "").strip() == "02" else "매도"
            
            type_hint = " ".join(x for x in [(o.get("ord_dvsn_cd_name") or "").strip(), (o.get("sll_buy_dvsn_cd_name") or "").strip()] if x).strip()
            if not type_hint: type_hint = side_label

            ord_pr = float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", "0")))
            ccld_pr = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0")))
            ord_dt = (o.get("ord_dt") or "").strip()
            
            rows.append({
                "ticker": ticker, "ord_dt": ord_dt, "side_label": side_label, 
                "type_hint": type_hint, "ord_pr": ord_pr, "ccld_qty": ccld_qty, "ccld_pr": ccld_pr
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
            if all("LOC" in t for t in types): tags = "LOC"
            elif all("MOC" in t for t in types): tags = "MOC"
            elif all("시장가" in t or "Market" in t for t in types): tags = "시장가"
            
            result.append({
                "ticker": tkr, "ord_dt": dt, "side_label": side,
                "ccld_qty": tot_ccld, "avg_fill_pr": avg_fill_pr, "tags": tags,
                "ord_prs": sorted({round(x["ord_pr"], 2) for x in grp if x["ord_pr"] > 0})
            })
        return result

    @staticmethod
    def execute_daily_order(ticker: str) -> str:
        """
        특정 종목의 오늘 치 전략 주문(LOC/MOC 등)을 생성하고 증권사에 전송합니다.
        전송 성공 시 해당 종목의 '오늘 주문 잠금(trade_lock)'을 활성화합니다.
        """
        try:
            qty_before = int(float((state_manager.STRATEGY.get(ticker, {}).get("data", {}) or {}).get("qty", 0) or 0))
            cycle_note = ""

            # 1. 주문 정보 빌드 (전략 버전별 분기 로직은 strategy.py 내부에 캡슐화됨)
            order_info = strategy.build_order_info(ticker)
            
            # 2. 주문 실행
            res_str = strategy.execute_all_order(ticker, order_info)
            
            # 3. 성공 여부 판단 및 잠금 처리
            if "성공" in res_str:
                utils.set_trade_lock(ticker)
                log.info(f"{ticker} 일일 주문 집행 성공 및 잠금 설정")

                # 자동주문에서 잔고 0주였던 경우는 "새 사이클 진입"으로 간주하여 회차를 1 증가시킵니다.
                if qty_before <= 0 and int(order_info.get("qty_total", 0) or 0) == 0:
                    new_cycle, inc = utils.start_cycle_for_new_entry(
                        ticker, increment_on_reentry=True
                    )
                    tl = utils.get_cycle_timeline(ticker)
                    timeline_note = (
                        f"\n- 이전 회차: 시작 {tl.get('previous_start', '-')} / 종료 {tl.get('previous_end', '-')}"
                        f"\n- 현재 회차 시작일: {tl.get('current_start', '-')}"
                    )
                    if inc:
                        log.info("%s auto zero-qty entry: cycle advanced to %d", ticker, new_cycle)
                        cycle_note = (
                            f"\n🔁 사이클 회차 반영: {new_cycle}회차로 증가(0주→재진입)."
                            + timeline_note
                        )
                    else:
                        log.info("%s auto zero-qty entry: cycle kept at %d (duplicate prevented)", ticker, new_cycle)
                        cycle_note = (
                            f"\nℹ️ 사이클 회차 유지: {new_cycle}회차(중복 증가 방지)."
                            + timeline_note
                        )
            else:
                log.warning(f"{ticker} 일일 주문 집행 결과: {res_str}")
            
            # UI용 메시지 반환
            return str(res_str) + cycle_note
        except Exception as e:
            error_msg = f"{ticker} 주문 집행 중 서버 오류: {str(e)}"
            log.error(error_msg)
            return error_msg

    @staticmethod
    def _verify_broker_qty(ticker: str):
        """증권사 실잔고에서 해당 종목의 실제 보유수량을 직접 조회합니다.
        전량 매도 후 동기화 누락으로 존재하지 않는 수량을 매도 시도하는 것을 방지합니다.
        Returns: 실제 보유수량 (종목이 잔고에 없으면 0). 조회 실패 시 None.
        """
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
        """
        특정 종목의 전일 수익실현 체크, 신규 진입, 쿼터 손절, 일반 주문 집행을 
        순차적으로 처리하고 결과 메시지를 반환합니다.
        """
        try:
            if utils.check_trade_lock(ticker):
                return f"[안전장치] {ticker}: 오늘 이미 매매 기록 있음. 건너뜀."

            s = state_manager.STRATEGY[ticker]

            # [방어] /liquidate로 청산 대기 중인 종목은 추가 매매를 하지 않음
            if s.get("liquidating"):
                return f"[안전장치] {ticker}: 청산 대기 중(liquidating). 매매 건너뜀."

            d = s["data"]
            avg_p = d.get("avg", 0)
            qty_t = int(d.get("qty", 0))
            
            results_list = []
            profit_take_sent = False
            target_reached = False  # [방어] 목표가 도달 여부 (매도 실패해도 추가 매수 방지)

            # [방어] 보유수량이 있는 것으로 보이면 증권사 실잔고를 한 번 더 확인합니다.
            # 전량 매도 후 동기화 누락으로 존재하지 않는 수량을 매도하는 것을 방지합니다.
            if qty_t > 0:
                actual_qty = TradeService._verify_broker_qty(ticker)
                if actual_qty is not None and actual_qty != qty_t:
                    log.warning(
                        "%s pre-order verify: memory qty=%d → broker qty=%d, correcting",
                        ticker, qty_t, actual_qty,
                    )
                    d["qty"] = float(actual_qty)
                    if actual_qty <= 0:
                        d["avg"] = 0.0
                        d["cumul"] = 0.0
                    else:
                        d["cumul"] = float(d.get("avg", 0.0)) * actual_qty
                    results_list.append(
                        f"[{ticker}] ⚠️ 잔고 불일치 보정: 메모리 {qty_t}주 → 실잔고 {actual_qty}주"
                    )
                    qty_t = actual_qty
                    avg_p = float(d.get("avg", 0))

            # 1. 자동 수익실현(전량 익절) 체크
            if avg_p > 0 and qty_t > 0:
                tgt_pct = strategy.profit_target_pct(ticker)
                target_p = avg_p * (1 + tgt_pct / 100)
                curr_p = kis_client.get_current_price(ticker)
                if curr_p >= target_p:
                    target_reached = True
                    # [수정] MOC(시장가 장마감)로 전환: LOC는 현재가를 하한으로 잡아
                    # 장마감 시 가격이 내려가면 미체결 위험이 있음. MOC는 무조건 체결됨.
                    res_sell = kis_client.send_order(ticker, qty_t, 0, "SELL", "MOC")
                    if res_sell.get("rt_cd") == "0":
                        results_list.append(
                            f"[{ticker}] 목표가 달성(+{tgt_pct:.1f}%)! "
                            f"전량 익절 MOC 매도 {qty_t}주 주문 전송 성공.\n"
                            f"  (실현수익은 장 마감 후 자동 동기화 시 cumul_profit에 반영됩니다.)"
                        )
                        profit_take_sent = True
                    else:
                        results_list.append(f"[{ticker}] 수익실현 MOC 매도 주문 실패: {res_sell.get('msg1','')}")

            # 2. 일반 일일 전략 주문 (신규 진입/쿼터 손절/일반 매수 포함)
            # [방어] 목표가에 도달한 상태에서는 매도 성공/실패와 무관하게 추가 매수를 하지 않음.
            # → 매도 실패 시 추가 매수가 들어가면 평단가가 왜곡되고 전략 의도에 어긋남.
            if not profit_take_sent and not target_reached:
                strat_res = TradeService.execute_daily_order(ticker)
                results_list.append(strat_res)
            else:
                if target_reached and not profit_take_sent:
                    results_list.append(
                        f"[{ticker}] 목표가 도달 상태이나 매도 실패. 추가 매수를 건너뜁니다."
                    )
                utils.set_trade_lock(ticker)
            
            return "\n".join(results_list)

        except Exception as e:
            msg = f"{ticker} 처리 중 오류: {str(e)}"
            log.error(msg)
            return msg

    @staticmethod
    def capture_pre_sync_avgs() -> dict[str, float]:
        """
        동기화 전 각 종목의 평단가를 캡처합니다.
        장 마감 후 매도 체결 수익을 계산하려면 매도 전의 평단가가 필요하기 때문입니다.

        Returns:
            {ticker: avg_price} — (1) 메모리상 보유 중인 종목,
            (2) 전량 매도 직후 등으로 last_known에만 평단이 남은 종목,
            (3) 사이클 active는 있는데 동기화 후 수량이 0인 종목(타 종목만 보유할 때
            루프에서 빠지던 케이스)을 합집합으로 포함합니다.
        """
        result: dict[str, float] = {}
        for ticker in state_manager.STRATEGY:
            d = state_manager.STRATEGY[ticker].get("data", {})
            qty = float(d.get("qty", 0) or 0)
            avg = float(d.get("avg", 0) or 0)
            if qty > 0 and avg > 0:
                result[ticker] = avg

        lk = utils.load_last_known_hold_avgs()
        for ticker in state_manager.STRATEGY:
            if ticker not in lk:
                continue
            try:
                lk_avg = float(lk[ticker] or 0)
            except (TypeError, ValueError):
                continue
            if lk_avg > 0 and ticker not in result:
                result[ticker] = lk_avg

        cycle_state = utils.load_cycle_state()
        for ticker in state_manager.STRATEGY:
            d = state_manager.STRATEGY[ticker].get("data", {}) or {}
            qty = float(d.get("qty", 0) or 0)
            info = cycle_state.get(ticker) or {}
            if info.get("active") is None or qty > 0 or ticker in result:
                continue
            mem_avg = float(d.get("avg", 0) or 0)
            lk_avg = float(lk.get(ticker, 0) or 0) if ticker in lk else 0.0
            use_avg = lk_avg if lk_avg > 0 else mem_avg
            if use_avg > 0:
                result[ticker] = use_avg

        if result:
            return result
        # record_daily_performance 등이 먼저 sync하여 메모리가 0인 경우: 직전 보유 평단 파일 사용
        if not lk:
            return {}
        return {
            t: float(lk[t])
            for t in state_manager.STRATEGY
            if t in lk and float(lk[t] or 0) > 0
        }

    @staticmethod
    def _reconcile_maybe_close_cycle_on_zero(ticker: str, messages: list[str]) -> None:
        """
        동기화 후 수량 0이고 cycle_state에 active가 남아 있으면 사이클을 종료하고
        V4/V3 상태를 리셋합니다. (체결 조회·실현손익 반영과 독립)
        """
        post_qty = float(
            (state_manager.STRATEGY.get(ticker, {}).get("data") or {}).get("qty", 0) or 0
        )
        current_cycle_state = utils.load_cycle_state().get(ticker, {})
        has_active_cycle = current_cycle_state.get("active") is not None

        if post_qty <= 0 and has_active_cycle:
            utils.close_active_cycle_on_zero(ticker)

            new_cs = utils.load_cycle_state().get(ticker, {})
            new_count = new_cs.get("current_cycle", 1)

            messages.append(
                f"🔄 {ticker} 투자 사이클 종료! "
                f"(전량 매도 확인 → 회차 증가는 다음 재진입 주문 시 1회만 반영, 현재 {new_count}회차)"
            )
            log.info(
                "active cycle closed on zero qty: %s (current_cycle=%s until next re-entry)",
                ticker,
                new_count,
            )

            lk_rem = utils.load_last_known_hold_avgs()
            lk_rem.pop(ticker, None)
            utils.save_last_known_hold_avgs(lk_rem)

            method = (state_manager.STRATEGY.get(ticker) or {}).get("method", "2.2")
            if method == "4.0":
                v4_state.put_record(
                    ticker,
                    t=None,
                    reverse=False,
                    reverse_started_ymd=None,
                    reverse_sell_proceeds=0.0,
                    frozen_rem=0.0,
                    pending_moc=None,
                )
                log.info("v4 state reset for %s (post full exit)", ticker)
            elif method == "3.0":
                import v3_state

                v3_state.put_record(
                    ticker,
                    buy_cumul_usd=0.0,
                    initialized=False,
                    processed_buy_odnos=[],
                )
                log.info("v3 state reset for %s (post full exit)", ticker)
        elif post_qty <= 0 and not has_active_cycle:
            log.debug(
                "reconcile %s: qty=0, no active cycle (nothing to close).",
                ticker,
            )

    @staticmethod
    def reconcile_daily_sell_profits(pre_sync_avgs: dict[str, float]) -> list[str]:
        """
        장 마감 후 당일 체결된 매도 주문의 실현 수익을 cumul_profit에 반영합니다.
        프리마켓 익절 경로 외에도, 정규장 LOC/지정가 매도 체결의 수익이 누락되지 않도록 합니다.

        Args:
            pre_sync_avgs: 동기화 전 캡처한 {ticker: avg_price}
        Returns:
            반영 메시지 리스트
        """
        messages: list[str] = []

        if not pre_sync_avgs:
            return messages

        ny_tz = pytz.timezone("America/New_York")
        now_ny = datetime.datetime.now(ny_tz)
        end_dt = now_ny.strftime("%Y%m%d")
        # 미국 '당일'만 보면 한국 시각상 다음 날·봇 재시작 후 sync 시 전 거래일 MOC가 누락됨.
        # processed_orders(주문번호)로 중복만 막고 최근 구간을 넓게 조회합니다.
        start_dt = (now_ny - datetime.timedelta(days=7)).strftime("%Y%m%d")

        for ticker, avg_p in pre_sync_avgs.items():
            if avg_p <= 0:
                try:
                    TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)
                except Exception as e:
                    log.warning("reconcile cycle-close for %s failed: %s", ticker, e)
                continue
            try:
                # 체결만(ccld_dvsn="01"). NASD/NYSE/AMEX 전부 조회(단일 거래소 추정 누락 방지).
                orders = kis_client.query_ccnl_collect_all_pages_multi_excg(
                    ticker=ticker,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    ccld_dvsn="01",
                    max_pages=8,
                )
                if not orders:
                    TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)
                    continue

                # 주문번호(odno)별 매도 체결 집계 (중복 행 방지)
                sell_by_odno: dict[str, dict] = {}
                for o in orders:
                    side_cd = str(o.get("sll_buy_dvsn_cd", "") or "").strip()
                    # TTTS3035R: 01=매도, 02=매수
                    if side_cd == "02":
                        continue
                    if side_cd not in ("01", "1"):
                        nm = str(o.get("sll_buy_dvsn_cd_name", "") or "").strip()
                        if "매도" not in nm:
                            continue
                    ccld_qty = int(float(o.get("ft_ccld_qty", "0") or 0))
                    if ccld_qty <= 0:
                        continue
                    ccld_pr = float(
                        o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0") or 0) or 0
                    )
                    if ccld_pr <= 0:
                        continue
                    odno = str(o.get("odno") or "").strip()
                    if not odno:
                        continue

                    # 같은 odno의 여러 행 중 최대 체결수량(누적)을 사용
                    if odno not in sell_by_odno or ccld_qty > sell_by_odno[odno]["qty"]:
                        sell_by_odno[odno] = {"qty": ccld_qty, "price": ccld_pr}

                # 각 매도 주문을 cumul_profit에 반영
                for odno, fill in sell_by_odno.items():
                    realized = (fill["price"] - avg_p) * fill["qty"]
                    cumul_total, recorded = strategy.add_cumul_profit(
                        ticker,
                        realized,
                        fill["qty"],
                        fill["price"],
                        avg_p,
                        order_no=odno,
                    )
                    if recorded:
                        messages.append(
                            f"💰 {ticker} 매도 체결 수익 반영: "
                            f"{fill['qty']}주 @ ${fill['price']:.2f} "
                            f"(평단 ${avg_p:.2f}) → "
                            f"실현수익 ${realized:+,.2f} (누적 ${cumul_total:,.2f})"
                        )
                        log.info(
                            "reconcile sell profit: %s %d shares @ %.2f, avg=%.2f, "
                            "realized=%.2f, cumul=%.2f, odno=%s",
                            ticker,
                            fill["qty"],
                            fill["price"],
                            avg_p,
                            realized,
                            cumul_total,
                            odno,
                        )

                # 사이클 종료: 체결 API가 비었어도 위에서 처리; 여기서는 정상 체결 후에도 반영
                TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)

            except Exception as e:
                log.warning("reconcile sell profit for %s failed: %s", ticker, e)

        # pre_sync에 평단이 없었지만(active만 남은 0주 등) 사이클 종료가 필요한 종목
        seen = set(pre_sync_avgs.keys())
        for ticker in state_manager.STRATEGY:
            if ticker in seen:
                continue
            try:
                TradeService._reconcile_maybe_close_cycle_on_zero(ticker, messages)
            except Exception as e:
                log.warning("reconcile cycle-close (extra) for %s failed: %s", ticker, e)

        return messages

    @staticmethod
    def sync_all_states():
        """
        모든 활성 종목의 계좌 잔고와 각 버전별(V3, V4) 상태 파일을 동기화합니다.
        sync_account()가 V3 cumul 및 V4 ensure_t를 포함하므로 별도 호출 불필요.
        """
        try:
            state_manager.sync_account()
            log.info("모든 종목 및 자산 상태 동기화 완료")
        except Exception as e:
            log.error(f"상태 동기화 중 오류 발생: {e}")
            raise
