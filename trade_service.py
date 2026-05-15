from __future__ import annotations

import asyncio
import datetime
import logging
import pytz
import kis_client
import strategy
import utils
from notification_service import notifier
from ui_formatter import UIFormatter
from state_manager import STRATEGY, CURRENT_CASH, sync_account

log = logging.getLogger(__name__)


class TradeService:
    """거래 관리 및 보고 서비스 (정산, 리포트 생성, 장중 감시 등)"""

    @staticmethod
    async def process_ticker_daily(ticker: str, chat_id: int):
        """종목별 일일 거래 프로세스 수행 (지시서 생성 및 주문 전송)"""
        if utils.check_trade_lock(ticker):
            log.info("%s: 이미 금일 거래가 완료되었습니다(Lock).", ticker)
            return

        # 1. 지시서 생성
        info = await asyncio.to_thread(strategy.build_order_info, ticker)
        if not info:
            log.error("%s: 주문 정보 생성 실패", ticker)
            return

        # 2. 주문 실행
        res_msg = await asyncio.to_thread(strategy.execute_all_order, ticker, info)
        
        # 3. 결과 알림
        await notifier.broadcast_alert(chat_id, UIFormatter.format_alert(f"{ticker} 자동 매매 결과", res_msg))
        
        # 4. 잠금 설정
        utils.set_trade_lock(ticker)
        log.info("%s: 일일 매매 프로세스 완료.", ticker)


    @staticmethod
    def prepare_daily_report_data() -> dict:
        """/status 등에 사용할 종합 현황 데이터 구성"""
        import state_manager
        
        kst = pytz.timezone("Asia/Seoul")
        est = pytz.timezone("US/Eastern")
        now_kst = datetime.datetime.now(kst)
        now_est = datetime.datetime.now(est)
        
        # 최근 체결 내역 수집 (모든 종목 합산)
        recent_orders = []
        for ticker in STRATEGY:
            orders = kis_client.query_recent_orders(ticker, days_back=1, max_pages=2)
            # 체결된 매수/매도만 필터링
            for o in orders:
                try:
                    ccld_qty = int(float(o.get("ft_ccld_qty", 0)))
                    if ccld_qty > 0:
                        side_cd = o.get("sll_buy_dvsn_cd")
                        recent_orders.append({
                            "ticker": ticker,
                            "ord_dt": o.get("ord_dt"),
                            "side_label": "매수" if side_cd == "02" else "매도",
                            "ccld_qty": ccld_qty,
                            "avg_fill_pr": float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", 0))),
                            "tags": o.get("sll_buy_dvsn_cd_name", ""),
                            "ord_prs": [] # 상세 단가 등 추가 정보 필요 시
                        })
                except: continue

        # 종목별 상세 데이터
        ticker_list = []
        total_cost = 0.0
        total_value = 0.0
        methods_in_use = set()

        for ticker, s in STRATEGY.items():
            d = s["data"]
            avg = d["avg"]
            qty = d["qty"]
            method = s["method"]
            methods_in_use.add(method)
            
            now_p = kis_client.get_current_price(ticker)
            if now_p <= 0: now_p = kis_client.get_prev_close(ticker)
            
            cost = avg * qty
            val = now_p * qty
            total_cost += cost
            total_value += val
            
            pnl_amt = val - cost
            pnl_rt = (pnl_amt / cost * 100) if cost > 0 else 0.0
            
            info = strategy.build_order_info(ticker)
            t_val = info.get("t_value", 0.0)
            tl = utils.get_cycle_timeline(ticker)

            # 미체결 정보
            unfilled = kis_client.query_unfilled_orders_today_us(ticker)
            unfilled_qty = sum(o["qty"] for o in unfilled)
            active_cnt = sum(1 for o in unfilled if o["status"] == "대기")

            ticker_list.append({
                "ticker": ticker,
                "method_disp": strategy.format_method_display(method),
                "cycle_count": tl["current_cycle"],
                "qty": qty,
                "avg": avg,
                "now_p": now_p,
                "pnl_amt": pnl_amt,
                "pnl_rt": pnl_rt,
                "seed": s["seed"],
                "target_p": s["target_profit"],
                "t_disp": f"회차: {t_val:.2f} / {int(s['total_a'])}회",
                "start_date": tl["current_start"],
                "locked": utils.check_trade_lock(ticker),
                "unfilled_qty": unfilled_qty,
                "active_cnt": active_cnt,
                "unfilled_cnt": len(unfilled)
            })

        return {
            "title": "[포트폴리오 현황]",
            "recorded_at": now_kst.strftime("%Y-%m-%d %H:%M:%S"),
            "us_date_display": now_est.strftime("%Y-%m-%d"),
            "is_closed": utils.get_market_session() == "closed",
            "market_session": utils.get_market_session_kr(),
            "premarket_on": getattr(state_manager, "ENABLE_PREMARKET", False),
            "recent_orders": sorted(recent_orders, key=lambda x: x["ord_dt"], reverse=True)[:10],
            "tickers": ticker_list,
            "total_cost": total_cost,
            "total_value": total_value,
            "current_cash": CURRENT_CASH,
            "methods_in_use": methods_in_use
        }


    @staticmethod
    def generate_settlement_report() -> str:
        """성과 정산 텍스트 리포트 생성"""
        import state_manager
        state_manager.sync_account()
        
        cp = utils.load_cumul_profit()
        summary = utils.aggregate_cumul_profit_by_ticker(cp)
        total_pnl = sum(summary.values())
        
        report = "📊 **무한매수 성과 정산 리포트**\n\n"
        report += f"기준일: {utils.get_us_today()} (US)\n"
        report += "--------------------------\n"
        
        if not summary:
            report += "기록된 실현 수익이 없습니다.\n"
        else:
            for tkr, val in sorted(summary.items()):
                report += f"- {tkr}: ${val:,.2f}\n"
            report += f"\n💰 **누적 합계: ${total_pnl:,.2f}**\n"
        
        report += "--------------------------\n"
        report += f"현재 달러 예수금: ${CURRENT_CASH:,.2f}\n"
        
        # 현재 평가 손익 추가
        total_eval_pnl = 0.0
        for tkr, s in STRATEGY.items():
            d = s["data"]
            now_p = kis_client.get_current_price(tkr)
            if now_p <= 0: now_p = kis_client.get_prev_close(tkr)
            eval_pnl = (now_p - d["avg"]) * d["qty"]
            total_eval_pnl += eval_pnl
            
        report += f"현재 보유종목 평가손익: ${total_eval_pnl:,.2f}\n"
        report += f"합계 (실현+평가): **${(total_pnl + total_eval_pnl):,.2f}**\n"
        
        return report
