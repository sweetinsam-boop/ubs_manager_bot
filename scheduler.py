from __future__ import annotations

import asyncio
import datetime
import logging
import pytz
from telegram.ext import JobQueue
from bot_logging import get_logger
from notification_service import notifier
from ui_formatter import UIFormatter
from trade_service import TradeService
import utils

log = get_logger(__name__)

# --- 정기 스케줄 등록 ---

def register_schedules(job_queue: JobQueue, chat_id: int):
    """모든 자동화 스케줄을 등록합니다 (KST/EST 혼합)."""
    
    # 1. 미국 정규장 오픈 직후 매매 지시 (월~금 22:31 또는 23:31 KST)
    # 실제로는 미국 동부 시간 기준 09:31로 설정하는 것이 서머타임 대응에 유리합니다.
    job_queue.run_daily(
        _market_open_trade_job,
        time=datetime.time(hour=9, minute=31, tzinfo=pytz.timezone("US/Eastern")),
        days=(0, 1, 2, 3, 4), # 월~금
        data={"chat_id": chat_id},
        name="market_open_trade"
    )

    # 2. 한국 시간 오전 08:00 데일리 정산 리포트 (화~토)
    job_queue.run_daily(
        _morning_settlement_job,
        time=datetime.time(hour=8, minute=0, tzinfo=pytz.timezone("Asia/Seoul")),
        days=(1, 2, 3, 4, 5), # 화~토
        data={"chat_id": chat_id},
        name="morning_settlement"
    )

    # 3. 장 마감 직전/직후 자동 동기화 및 V4.0 MOC 해소 (미국 16:05 / 16:40)
    job_queue.run_daily(
        _post_close_sync_job,
        time=datetime.time(hour=16, minute=5, tzinfo=pytz.timezone("US/Eastern")),
        days=(0, 1, 2, 3, 4),
        data={"chat_id": chat_id},
        name="post_close_sync_early"
    )
    
    from config_manager import get_post_close_sync_ny_time
    h, m = get_post_close_sync_ny_time()
    job_queue.run_daily(
        _post_close_sync_job,
        time=datetime.time(hour=h, minute=m, tzinfo=pytz.timezone("US/Eastern")),
        days=(0, 1, 2, 3, 4),
        data={"chat_id": chat_id},
        name="post_close_sync_main"
    )

    # 4. 프리마켓 목표가 감시 (장 시작 전 1시간 동안 5분 간격)
    # 서머타임 기준 KST 21:30 ~ 22:30 (미국 08:30 ~ 09:30)
    job_queue.run_repeating(
        _premarket_monitor_job,
        interval=300, # 5분
        first=datetime.time(hour=8, minute=30, tzinfo=pytz.timezone("US/Eastern")),
        last=datetime.time(hour=9, minute=30, tzinfo=pytz.timezone("US/Eastern")),
        data={"chat_id": chat_id},
        name="premarket_monitor"
    )
    
    log.info("모든 자동 스케줄 등록 완료 (ChatID: %s)", chat_id)


# --- 개별 작업 로직 ---

async def _market_open_trade_job(context):
    """장 오픈 직후 매매 실행"""
    chat_id = context.job.data["chat_id"]
    if not utils.is_market_open_today():
        log.info("오늘은 휴장일입니다. 매매를 건너뜁니다.")
        return

    import state_manager
    await asyncio.to_thread(state_manager.sync_account)
    
    from state_manager import STRATEGY
    for ticker in STRATEGY:
        await TradeService.process_ticker_daily(ticker, chat_id)


async def _morning_settlement_job(context):
    """오전 8시 성과 보고"""
    chat_id = context.job.data["chat_id"]
    if not utils.should_run_kst_morning_settlement():
        return

    report = await asyncio.to_thread(TradeService.generate_settlement_report)
    await notifier.broadcast_alert(chat_id, UIFormatter.format_alert("일일 성과 정산", report))
    
    # 성과 로그 기록 (차트용)
    from state_manager import CURRENT_CASH, STRATEGY
    import kis_client
    
    total_equity = CURRENT_CASH
    for ticker, s in STRATEGY.items():
        p = kis_client.get_current_price(ticker)
        if p <= 0: p = kis_client.get_prev_close(ticker)
        total_equity += p * s["data"]["qty"]
        
    spy_p = kis_client.get_current_price("VOO") # 벤치마크
    qqq_p = kis_client.get_current_price("QQQ") # 나스닥
    
    utils.log_daily_performance(total_equity, spy_p, qqq_p)
    utils.set_last_settlement_date(utils.get_us_today())


async def _post_close_sync_job(context):
    """장 마감 후 잔고 동기화 및 V4.0 MOC/Liquidate 처리"""
    chat_id = context.job.data["chat_id"]
    import state_manager
    success, msg = await asyncio.to_thread(state_manager.sync_account)
    
    # Liquidate(전량매도) 요청된 종목들 정리
    from state_manager import STRATEGY
    liquidated_tickers = []
    for ticker, s in list(STRATEGY.items()):
        if s.get("liquidating") and s["data"]["qty"] == 0:
            # 1. 수익 기록 (마지막 평단 기준)
            # sync_account가 내부적으로 last_known_hold_avgs를 갱신하므로 utils에서 로드
            lk = utils.load_last_known_hold_avgs()
            last_avg = lk.get(ticker, 0)
            
            # 최근 매도 체결가 조회 (대략)
            import kis_client
            recent = kis_client.query_recent_orders(ticker, days_back=1, ccld_dvsn="01")
            sell_px = 0.0
            sell_qty = 0
            for o in recent:
                if o.get("sll_buy_dvsn_cd") == "01": # 매도
                    sell_px = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", 0)))
                    sell_qty += int(float(o.get("ft_ccld_qty", 0)))
            
            if sell_qty > 0 and last_avg > 0:
                profit = (sell_px - last_avg) * sell_qty
                from strategy import add_cumul_profit
                add_cumul_profit(ticker, profit, sell_qty, sell_px, last_avg, method=s["method"])
            
            # 2. 설정에서 삭제
            from config_manager import STRATEGY_SETTINGS, save_config
            new_settings = dict(STRATEGY_SETTINGS)
            if ticker in new_settings:
                del new_settings[ticker]
                save_config({"STRATEGY_SETTINGS": new_settings})
            
            if ticker in STRATEGY: del STRATEGY[ticker]
            utils.delete_ticker_from_all_states(ticker)
            liquidated_tickers.append(ticker)

    if liquidated_tickers:
        await notifier.broadcast_alert(chat_id, f"✅ **사이클 종료 알림**\n\n다음 종목의 전량 매도가 확인되어 포트폴리오에서 자동 삭제되었습니다: {', '.join(liquidated_tickers)}")


async def _premarket_monitor_job(context):
    """프리마켓 감시: 목표가 도달 시 알림 및 자동 익절 매도"""
    from config_manager import ENABLE_PREMARKET
    if not ENABLE_PREMARKET:
        return

    chat_id = context.job.data["chat_id"]
    from state_manager import STRATEGY
    import kis_client
    
    for ticker, s in STRATEGY.items():
        qty = s["data"]["qty"]
        if qty <= 0: continue
        
        curr_p = kis_client.get_current_price(ticker)
        target_p = s["data"]["avg"] * (1 + s["target_profit"] / 100)
        
        if curr_p >= target_p:
            msg = f"🚀 **{ticker} 프리마켓 목표가 도달!**\n\n현재가 ${curr_p:.2f} >= 목표가 ${target_p:.2f}\n즉시 전량 지정가 매도 주문을 전송합니다."
            await notifier.broadcast_alert(chat_id, msg)
            
            # 자동 매도 전송
            res = kis_client.send_order(ticker, int(qty), curr_p, "SELL", "00")
            if res.get("rt_cd") == "0":
                utils.set_trade_lock(ticker)
                await notifier.broadcast_alert(chat_id, f"✅ {ticker} 프리마켓 익절 주문 전송 완료.")
            else:
                await notifier.broadcast_alert(chat_id, f"❌ {ticker} 프리마켓 매도 실패: {res.get('msg1')}")


async def check_missed_settlement(application):
    """봇 재시작 시 오늘 정산 리포트를 보냈는지 체크하고 누락 시 발송"""
    chat_id = utils.load_chat_id()
    if not chat_id: return

    today = utils.get_us_today()
    last_date = utils.get_last_settlement_date()
    
    # KST 08:00 이후이고 오늘 날짜(US) 정산이 아직 안 나갔다면
    now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    if now_kst.hour >= 8 and last_date != today and utils.should_run_kst_morning_settlement():
        log.info("누락된 정산 리포트 발송을 시작합니다.")
        # 가짜 컨텍스트 생성하여 호출
        class MockJob:
            def __init__(self, cid): self.data = {"chat_id": cid}
        class MockContext:
            def __init__(self, cid): self.job = MockJob(cid)
        
        await _morning_settlement_job(MockContext(chat_id))
