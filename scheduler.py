from __future__ import annotations

import asyncio
import datetime
import pytz
import math
import os
import time
import json
import requests
from collections import defaultdict
from telegram.ext import ContextTypes

from bot_logging import get_logger
import utils
import kis_client
import strategy
import state_manager
import v4_state
from state_manager import STRATEGY, CURRENT_CASH
import config_manager
from notification_service import notifier
from ui_formatter import UIFormatter
from trade_service import TradeService

log = get_logger(__name__)

_daily_trade_running = False

async def premarket_target_check(context: ContextTypes.DEFAULT_TYPE):
    if not config_manager.ENABLE_PREMARKET: return
    chat_id = context.job.chat_id
    tz_ny = pytz.timezone('America/New_York')
    now_ny = datetime.datetime.now(tz_ny)

    # 뉴욕 시간 기준 프리마켓 시간 (04:00~09:30)
    if not (4 <= now_ny.hour < 9 or (now_ny.hour == 9 and now_ny.minute < 30)): return
    if not utils.is_market_open_today(): return

    for ticker in STRATEGY:
        if utils.check_trade_lock(ticker): continue
        
        curr_p = await asyncio.to_thread(kis_client.get_current_price, ticker)
        s = STRATEGY[ticker]
        avg_p = s["data"].get("avg", 0)

        tgt_pct = strategy.profit_target_pct(ticker)
        if avg_p > 0 and curr_p >= (avg_p * (1 + tgt_pct / 100)):
            await asyncio.to_thread(state_manager.sync_account)
            
            s = STRATEGY[ticker]
            d = s["data"]
            avg_p = d.get("avg", 0)
            qty_t = int(d.get("qty", 0))

            if avg_p <= 0 or qty_t <= 0: continue

            target_p = avg_p * (1 + tgt_pct / 100)
            if curr_p >= target_p:
                profit_rt = (curr_p - avg_p) / avg_p * 100
                one_time = strategy.one_time_for_strategy(s)
                entry_p = round(curr_p * 1.15, 2)
                entry_qty = math.floor(one_time / curr_p)

                now_kst = datetime.datetime.now(pytz.timezone('Asia/Seoul'))
                now_ny = datetime.datetime.now(pytz.timezone('America/New_York'))
                alert_msg = f"[프리마켓 목표가 돌파] {now_kst.strftime('%m-%d %H:%M')} KST / {now_ny.strftime('%H:%M')} NY\n\n"
                alert_msg += f"[{ticker}] 현재가: ${curr_p:.2f} >= 목표가: ${target_p:.2f} (+{tgt_pct:.1f}%)\n"
                alert_msg += f"  평단: ${avg_p:.2f} / 수익률: {profit_rt:+.1f}%\n"
                alert_msg += f"  전량 매도({qty_t}주) 주문 실행!\n"
                await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(alert_msg))

                res_sell = await asyncio.to_thread(
                    kis_client.send_order, ticker, qty_t, curr_p, "SELL", "00"
                )
                sell_success = res_sell.get('rt_cd') == '0'

                result_msg = f"[{ticker} 프리마켓 매도 주문]\n"
                result_msg += f"지정가 매도 {qty_t}주 ${curr_p:.2f}: {'주문 접수' if sell_success else '실패('+res_sell.get('msg1','')+')'}\n"

                if sell_success:
                    order_no = res_sell.get("output", {}).get("ODNO", "")
                    result_msg += f"주문번호: {order_no}\n"
                    await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(result_msg))
                    await _complete_target_profit_sell_after_accept(
                        context, ticker, float(avg_p), qty_t, order_no, entry_qty, entry_p
                    )
                else:
                    # [수정] 매도 주문 자체가 실패한 경우 lock을 걸지 않음
                    # → 정규장에서 다시 매매 기회를 가질 수 있도록 함
                    await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(result_msg))
                    log.warning("premarket sell order failed for %s, NOT locking", ticker)

async def daily_market_check(context: ContextTypes.DEFAULT_TYPE):
    global _daily_trade_running
    chat_id = context.job.chat_id

    if _daily_trade_running:
        log.warning("daily_market_check skipped: already running")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ **자동매매 작업이 이미 실행 중**이라 이번 스케줄은 건너뜁니다. (중복 실행 방지)",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return
    if not utils.is_market_open_today():
        await context.bot.send_message(chat_id=chat_id, text="오늘은 미국 증시 휴장일입니다. 자동매매를 건너뜁니다.")
        return

    _daily_trade_running = True
    try:
        await _run_daily_market(context, chat_id)
    except Exception as e:
        log.exception("daily_market_check failed: %s", e)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ **자동매매 실행 중 오류**로 중단되었습니다.\n```\n{e}\n```\n로그를 확인하세요.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        raise
    finally:
        _daily_trade_running = False

async def _run_daily_market(context, chat_id):
    # 0. 계좌 및 모든 종목 상태 동기화
    try:
        await asyncio.to_thread(TradeService.sync_all_states)
    except Exception as e:
        log.warning(f"초기 동기화 중 일부 오류 발생 (무시하고 진행): {e}")

    now_kst = datetime.datetime.now(pytz.timezone('Asia/Seoul'))
    now_ny = datetime.datetime.now(pytz.timezone('America/New_York'))
    time_str = f"{now_kst.strftime('%m-%d %H:%M')} KST / {now_ny.strftime('%H:%M')} NY"
    
    # 1. 정규장 오픈 메시지 (UIFormatter 적용)
    msg = f"[자동매매 알림] 미국 정규장 오픈! ({time_str})\n\n"
    msg += "오늘의 매수/매도 시나리오에 따라 주문 전송을 시작합니다."
    await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(msg))
    
    # 2. 모든 종목 일괄 처리 (Service 계층 위임)
    tickers = list(state_manager.STRATEGY.keys())
    for ticker in tickers:
        res_str = await asyncio.to_thread(TradeService.process_ticker_daily, ticker)
        await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(res_str))
    
    # 3. 주문 등록 상태 확인 (10초 대기 후 미체결 체크)
    await notifier.broadcast_alert(chat_id, "주문 등록 상태를 최종 확인 중입니다 (10초 대기)...")
    await asyncio.sleep(10)
    
    verify_msg = (
        "[주문 등록 확인 결과]\n"
        "(미국 동부 당일 주문일·미체결만 집계 — 전일 이전 미체결은 제외)\n"
    )
    for ticker in state_manager.STRATEGY:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        if not unfilled:
            verify_msg += f"  - {ticker}: 당일 미체결 없음 (전량 체결·당일 미접수·API 지연일 수 있음)\n"
        else:
            verify_msg += f"  - {ticker}: 당일 미체결 {len(unfilled)}건 등록 확인\n"
    
    await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(verify_msg))

async def generate_combined_report(is_full: bool = False, title: str = None) -> str:
    """
    TradeService와 UIFormatter를 사용하여 통합 보고서를 생성합니다.
    """
    data = await asyncio.to_thread(TradeService.get_portfolio_summary, is_full)
    if title:
        data["title"] = title
    
    # 텍스트 포맷팅 및 특수기호/이모지 제거
    report = UIFormatter.format_daily_report(data)
    return UIFormatter.clean_text(report)


async def settlement_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    # ── 안전망: 전일 매도 체결 수익 중 미반영분 사후 보정 ──
    # (post_close_sync_job에서 이미 반영되었으면 processed_orders 중복으로 건너뜀)
    try:
        pre_avgs = await asyncio.to_thread(TradeService.capture_pre_sync_avgs)
        if pre_avgs:
            profit_msgs = await asyncio.to_thread(
                TradeService.reconcile_daily_sell_profits, pre_avgs
            )
            if profit_msgs:
                reconcile_text = "[정산 전 매도수익 사후보정]\n\n" + "\n".join(profit_msgs)
                await notifier.broadcast_alert(chat_id, reconcile_text)
                log.info("settlement reconcile: %d sell profit(s) recorded", len(profit_msgs))
    except Exception as e:
        log.warning("settlement reconcile failed (continuing): %s", e)

    report = await generate_combined_report(is_full=True, title="[굿모닝! 전일 매매 종합 리포트]")
    
    utils.save_last_report(report)
    await notifier.broadcast_alert(chat_id, report)
    kst = pytz.timezone("Asia/Seoul")
    utils.set_last_settlement_date(datetime.datetime.now(kst).strftime("%Y%m%d"))
    log.info("settlement report generated and sent at %s", datetime.datetime.now())

def _do_performance_log_update() -> bool:
    """계좌 동기화 후 오늘(미국 기준) 자산·벤치마크를 performance_log에 기록. 성공 시 True."""
    if not utils.is_market_open_today():
        return False
    try:
        state_manager.sync_account()
        total_equity = float(CURRENT_CASH or 0)
        for ticker in STRATEGY:
            s = STRATEGY[ticker]
            qty = float((s.get("data") or {}).get("qty", 0) or 0)
            curr_p = float(kis_client.get_current_price(ticker) or 0)
            total_equity += qty * curr_p
        benchmark_p = kis_client.get_current_price("VOO")
        nasdaq_p = kis_client.get_current_price("QQQ")
        utils.log_daily_performance(total_equity, benchmark_p, nasdaq_p)
        return True
    except Exception as e:
        log.warning("performance_log update failed: %s", e)
        return False


def _backfill_performance_log_from_kis() -> int:
    """performance_log에 없는 거래일을 KIS 기간손익으로 보강. 채운 일수 반환."""
    from storage.file_store import load_json, save_json
    from storage.paths import get_path, PERFORMANCE_LOG_FILE as _LOG_FILE

    path = get_path(_LOG_FILE)
    data = load_json(path, default=[])
    if not data:
        return 0
    dates_sorted = sorted({e.get("date") for e in data if e.get("date")})
    if not dates_sorted:
        return 0
    last_ymd = dates_sorted[-1].replace("-", "")
    end_ymd = utils.get_broker_calendar_end_ymd()
    if last_ymd >= end_ymd:
        return 0
    from datetime import timedelta
    last_d = datetime.datetime.strptime(last_ymd, "%Y%m%d").date()
    start_next = (last_d + timedelta(days=1)).strftime("%Y%m%d")
    df, _ = kis_client.account_daily_pnl_dataframe(start_next, end_ymd)
    if df is None or df.empty:
        return 0
    by_date = {e["date"]: float(e.get("equity", 0) or 0) for e in data}
    added = 0
    for _, row in df.sort_values("date").iterrows():
        d = row["date"]
        d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        if d_str in by_date:
            continue
        prev_dates = sorted(k for k in by_date if k < d_str)
        prev_equity = by_date[prev_dates[-1]] if prev_dates else 0
        new_equity = prev_equity + float(row.get("daily_pnl", 0) or 0)
        data.append({"date": d_str, "equity": new_equity, "benchmark": None, "nasdaq": None})
        by_date[d_str] = new_equity
        added += 1
    if added > 0:
        data.sort(key=lambda e: (e.get("date") or ""))
        if len(data) > 365:
            data = data[-365:]
        save_json(path, data, ensure_ascii=False, indent=2)
    return added


async def record_daily_performance(context: ContextTypes.DEFAULT_TYPE):
    """장 마감 시점의 자산 현황을 기록합니다."""
    await asyncio.to_thread(_do_performance_log_update)

async def check_missed_settlement(application):
    """봇 시작 시 오늘자 정산 리포트가 누락되었는지 확인하고 필요시 생성합니다."""
    chat_id = utils.load_chat_id()
    if not chat_id:
        return

    kst = pytz.timezone("Asia/Seoul")
    now_kst = datetime.datetime.now(kst)
    today_str = now_kst.strftime("%Y%m%d")

    last_date = utils.get_last_settlement_date()

    if not utils.should_run_kst_morning_settlement(now_kst):
        log.info(
            "missed-report skip (일·월 아침 정산 생략 요일). Last: %s, Now: %s %sh",
            last_date,
            today_str,
            now_kst.hour,
        )
        return

    class _Job:
        def __init__(self, cid):
            self.chat_id = cid

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.bot = application.bot
    ctx.job = _Job(chat_id)

    if now_kst.hour >= 8 and last_date != today_str:
        log.info("missed-report: today's report (%s) missing, starting catch-up", today_str)
        await settlement_check(ctx)
    else:
        log.info(
            "missed-report: no catch-up. Last: %s, Now: %s %sh",
            last_date,
            today_str,
            now_kst.hour,
        )

async def send_cycle_report(context, ticker, avg, sell_price, qty, profit):
    """투자 사이클 종료 시 종합 보고서를 전송합니다."""
    chat_id = context.job.chat_id
    yield_rt = (sell_price - avg) / avg * 100
    
    benchmark_msg = ""
    try:
        voo_curr = await asyncio.to_thread(kis_client.get_current_price, "VOO")
        qqq_curr = await asyncio.to_thread(kis_client.get_current_price, "QQQ")
        benchmark_msg = (
            f"\n[시장 지수 참고]\n"
            f"- S&P 500 (VOO): ${voo_curr:.2f}\n"
            f"- Nasdaq 100 (QQQ): ${qqq_curr:.2f}\n\n"
            f"(시장 마감 후 performance_log 스냅샷으로 계좌/지수 추이를 참고할 수 있습니다.)"
        )
    except Exception: pass

    report = (
        f"[{ticker}] 투자 사이클 종료 보고서\n\n"
        f"[최종 정산 결과]\n"
        f"- 매수평단: ${avg:.2f}\n"
        f"- 매도단가: ${sell_price:.2f}\n"
        f"- 최종수익: ${profit:,.2f}\n"
        f"- 확정수익률: {yield_rt:+.2f}%\n"
        f"{benchmark_msg}\n\n"
        f"투자 사이클이 종료되었습니다. 새로운 사이클을 응원합니다."
    )
    await context.bot.send_message(chat_id=chat_id, text=report, parse_mode='Markdown')
    # 전량 매도 체결 시점에서는 active만 종료하고, 회차 증가는 다음 재진입 주문 시점에 1회 반영.
    utils.close_active_cycle_on_zero(ticker)


async def _complete_target_profit_sell_after_accept(
    context: ContextTypes.DEFAULT_TYPE,
    ticker: str,
    avg_p: float,
    qty_ordered: int,
    order_no,
    entry_qty: int,
    entry_p: float,
) -> None:
    """
    목표가 도달 후 익절 매도가 증권사에 접수된 뒤:
    체결 확인 → cumul_profit 반영 → 투자 사이클 종료 보고 → LOC 재진입 → 일일 잠금.
    """
    chat_id = context.job.chat_id
    on = str(order_no or "").strip()
    if not on:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"[{ticker}] 매도는 접수됐으나 주문번호를 읽지 못해 누적실현·사이클 종료 보고를 건너뜁니다.\n"
                "체결 내역을 확인한 뒤 `/sync` 하세요."
            ),
        )
        if entry_qty > 0:
            res_buy = await asyncio.to_thread(
                kis_client.send_order, ticker, entry_qty, entry_p, "BUY", "LOC"
            )
            bm = (
                f"[{ticker} LOC 재진입] {entry_qty}주 ${entry_p:.2f}: "
                f"{'성공' if res_buy.get('rt_cd') == '0' else '실패(' + str(res_buy.get('msg1', '')) + ')'}"
            )
            await context.bot.send_message(chat_id=chat_id, text=bm)
        utils.set_trade_lock(ticker)
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"[{ticker}] 익절 매도 체결 확인 중... (최대 약 30초)",
    )
    filled, filled_qty, filled_price = await asyncio.to_thread(
        kis_client.check_order_filled, on, ticker, "SELL", 10, 3
    )
    if not filled:
        # [수정] 프리마켓 미체결 시: 주문 취소 시도 후 lock을 걸지 않음
        # → 정규장에서 DCA(격자 매수) 기회를 놓치지 않도록 함
        cancel_msg = ""
        try:
            cancel_res = await asyncio.to_thread(
                kis_client.cancel_order, ticker, on, qty_ordered
            )
            if cancel_res.get("rt_cd") == "0":
                cancel_msg = "미체결 주문 취소 완료. 정규장 매매를 정상 진행합니다."
                log.info("premarket unfilled sell cancelled: %s, odno=%s", ticker, on)
            else:
                cancel_msg = f"주문 취소 실패({cancel_res.get('msg1', '')}). 수동 확인 필요."
                utils.set_trade_lock(ticker)  # 취소 실패 시에만 안전하게 잠금
                log.warning("premarket cancel failed: %s, odno=%s, result=%s", ticker, on, cancel_res)
        except Exception as e:
            cancel_msg = f"주문 취소 중 오류({e}). 수동 확인 필요."
            utils.set_trade_lock(ticker)
            log.warning("premarket cancel error: %s, %s", ticker, e)

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"[{ticker}] 프리마켓 매도 체결이 확인되지 않았습니다.\n"
                f"{cancel_msg}\n"
                "필요 시 `/sync` 로 상태를 확인하세요."
            ),
        )
        return

    realized = (filled_price - avg_p) * filled_qty
    cumul_total, recorded = strategy.add_cumul_profit(
        ticker, realized, filled_qty, filled_price, avg_p, order_no=on
    )
    fill_msg = (
        f"[{ticker} 매도 체결 확인]\n"
        f"체결: {filled_qty}주 @ ${filled_price:.2f}\n"
        f"실현수익: ${realized:+,.2f} (누적: ${cumul_total:,.2f})\n"
    )
    if not recorded:
        fill_msg += "(이 주문번호는 이미 누적에 반영된 적이 있습니다.)\n"
    fill_msg += "LOC 종가 재진입 매수 주문 실행...\n"
    await context.bot.send_message(chat_id=chat_id, text=fill_msg)

    if recorded:
        try:
            await send_cycle_report(context, ticker, avg_p, filled_price, filled_qty, realized)
        except Exception as e:
            log.warning("send_cycle_report failed for %s: %s", ticker, e, exc_info=True)

    if entry_qty > 0:
        res_buy = await asyncio.to_thread(
            kis_client.send_order, ticker, entry_qty, entry_p, "BUY", "LOC"
        )
        buy_msg = (
            f"[{ticker} LOC 재진입 결과]\n"
            f"LOC 매수 {entry_qty}주 ${entry_p:.2f}: "
            f"{'성공' if res_buy.get('rt_cd') == '0' else '실패(' + str(res_buy.get('msg1', '')) + ')'}\n"
        )
        await context.bot.send_message(chat_id=chat_id, text=buy_msg)
    utils.set_trade_lock(ticker)


async def market_close_alert(context: ContextTypes.DEFAULT_TYPE):
    """정규장 마감 알림 (KST 05:00/06:00)"""
    chat_id = context.job.chat_id
    if not utils.is_market_open_today(): return
    
    msg = "[시장 마감] 미국 증시가 마감되었습니다.\n"
    msg += "매매 결과는 잠시 후 아침 8시 리포트에서 확인하실 수 있습니다."
    await notifier.broadcast_alert(chat_id, UIFormatter.clean_text(msg))

async def market_close_report(context: ContextTypes.DEFAULT_TYPE):
    """장 마감 직후 체결 내역과 잔고를 종합하여 보고합니다. (NY 16:05)"""
    chat_id = context.job.chat_id
    if not utils.is_market_open_today(): return

    report = await generate_combined_report(is_full=False, title="[장 마감 종합 리포트]")
    await notifier.broadcast_alert(chat_id, report)

async def premarket_start_alert(context: ContextTypes.DEFAULT_TYPE):
    """프리마켓 시작 알림 (KST 17:00/18:00)"""
    if not config_manager.ENABLE_PREMARKET: return
    chat_id = context.job.chat_id
    if not utils.is_market_open_today(): return
    
    msg = "🌅 **[감시 시작] 프리마켓이 시작되었습니다.**\n"
    msg += "봇이 목표가 도달 여부를 실시간으로 감시합니다. (NY 04:00)"
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')

async def token_renewal_check(context: ContextTypes.DEFAULT_TYPE):
    """
    [Deprecated] 주기적 강제 재발급은 KIS의 24시간/1분 발급 정책상 의미가 없습니다.
    24시간 안에는 동일 키만 돌려주며, 분당 1회 재발급 제한도 있어 호출 자체가 낭비입니다.
    토큰 갱신은 만료 시각 도래 또는 API 응답의 만료 시그널을 받았을 때 자동으로 이뤄집니다
    (kis_client.get_kis_access_token / kis_api_call 참조).
    하위 호환을 위해 함수는 남겨두되 캐시 검사만 수행합니다(필요 시 자연스럽게 신규 발급).
    """
    await asyncio.to_thread(kis_client.get_kis_access_token)


async def periodic_performance_log_update(context: ContextTypes.DEFAULT_TYPE):
    """performance_log를 주기적으로 갱신. 오늘 스냅샷 + 누락된 거래일 KIS 보강."""
    updated = await asyncio.to_thread(_do_performance_log_update)
    filled = await asyncio.to_thread(_backfill_performance_log_from_kis)
    if updated or filled > 0:
        log.info("performance_log periodic: updated=%s backfilled=%d", updated, filled)


async def post_close_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """장 마감 후 일정 시각에 계좌 동기화(V4 pending MOC 등 포함). trading_settings POST_CLOSE_SYNC_NY 로 시각 조정."""
    if not utils.is_market_open_today():
        return
    chat_id = context.job.chat_id

    # ── 1) 동기화 전: 매도 수익 계산을 위해 현재 평단가 캡처 ──
    pre_sync_avgs = await asyncio.to_thread(TradeService.capture_pre_sync_avgs)

    # ── 2) 계좌 동기화 ──
    success, msg = await asyncio.to_thread(state_manager.sync_account)
    body = msg if msg else "(메시지 없음)"
    if success:
        text = f"[장 마감 후 자동 동기화]\n\n{body}"
    else:
        text = f"[장 마감 후 자동 동기화 실패]\n\n{body}"
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=text.replace("**", ""))

    # ── 3) 당일 매도 체결 수익 반영 (cumul_profit 누락 방지) ──
    if pre_sync_avgs:
        try:
            profit_msgs = await asyncio.to_thread(
                TradeService.reconcile_daily_sell_profits, pre_sync_avgs
            )
            if profit_msgs:
                reconcile_text = "[당일 매도 실현수익 반영]\n\n" + "\n".join(profit_msgs)
                await notifier.broadcast_alert(chat_id, reconcile_text)
                log.info("post_close reconcile: %d sell profit(s) recorded", len(profit_msgs))
            else:
                log.info("post_close reconcile: no new sell profits to record")
        except Exception as e:
            log.warning("post_close sell profit reconcile failed: %s", e, exc_info=True)

    # ── 4) liquidating 플래그가 설정된 종목 정리 ──
    # /liquidate로 MOC 매도 주문 접수 후 체결·수익 기록이 완료된 시점에 설정 삭제
    try:
        tickers_to_remove = [
            tk for tk in list(state_manager.STRATEGY.keys())
            if state_manager.STRATEGY[tk].get("liquidating")
            and float((state_manager.STRATEGY[tk].get("data") or {}).get("qty", 0) or 0) <= 0
        ]
        if tickers_to_remove:
            from config_manager import STRATEGY_SETTINGS, save_config
            new_settings = dict(STRATEGY_SETTINGS)
            remove_msgs = []
            for tk in tickers_to_remove:
                if tk in new_settings:
                    del new_settings[tk]
                if tk in state_manager.STRATEGY:
                    del state_manager.STRATEGY[tk]
                
                # 모든 상태 파일 정리 (cumul_profit history 보존 포함)
                utils.delete_ticker_from_all_states(tk)
                
                remove_msgs.append(f"🗑️ {tk} 청산 완료 → 포트폴리오에서 삭제됨")
                log.info("liquidating cleanup: %s removed from STRATEGY", tk)
            save_config({"STRATEGY_SETTINGS": new_settings})
            if remove_msgs:
                await notifier.broadcast_alert(
                    chat_id,
                    "[청산 종목 정리]\n\n" + "\n".join(remove_msgs)
                )
    except Exception as e:
        log.warning("liquidating cleanup failed: %s", e, exc_info=True)


def register_schedules(job_queue, chat_id):
    if job_queue is None:
        log.error("JobQueue가 초기화되지 않았습니다. python-telegram-bot[job-queue] 설치를 확인하세요.")
        return
    tz_ny = pytz.timezone('America/New_York')
    kst = pytz.timezone('Asia/Seoul')
    
    # [중요] python-telegram-bot v20+ (APScheduler 기반) 요일 매핑 안내:
    # 0=Sunday, 1=Monday, 2=Tuesday, 3=Wednesday, 4=Thursday, 5=Friday, 6=Saturday
    # (Python 표준 datetime.weekday()의 0=Monday와 다르니 주의 필요)
    
    for job in job_queue.jobs():
        job.schedule_removal()
    
    job_queue.run_daily(daily_market_check, time=datetime.time(9, 35, tzinfo=tz_ny), days=(1, 2, 3, 4, 5), chat_id=chat_id)
    job_queue.run_daily(market_close_report, time=datetime.time(16, 5, tzinfo=tz_ny), days=(1, 2, 3, 4, 5), chat_id=chat_id)
    pc_h, pc_m = config_manager.get_post_close_sync_ny_time()
    job_queue.run_daily(
        post_close_sync_job,
        time=datetime.time(pc_h, pc_m, tzinfo=tz_ny),
        days=(1, 2, 3, 4, 5),
        chat_id=chat_id,
    )
    # KST 08:00 정산: 일·월 아침 제외 (토~일 장 휴무 직후 리포트는 의미가 적음) → 화~토만 (PTB 20+: 2~6)
    job_queue.run_daily(settlement_check, time=datetime.time(8, 0, tzinfo=kst), days=(2, 3, 4, 5, 6), chat_id=chat_id)
    job_queue.run_daily(market_close_alert, time=datetime.time(16, 0, tzinfo=tz_ny), days=(1, 2, 3, 4, 5), chat_id=chat_id)
    job_queue.run_daily(premarket_start_alert, time=datetime.time(4, 0, tzinfo=tz_ny), days=(1, 2, 3, 4, 5), chat_id=chat_id)
    # 토큰은 24시간 발급 정책 + 1분 재발급 제한이 있어 주기 강제 재발급은 무의미하며, 오히려 호출 낭비.
    # 만료 도래·API 만료 시그널 수신 시 kis_client에서 자동으로 재발급하므로 주기 잡은 등록하지 않음.
    job_queue.run_repeating(
        periodic_performance_log_update,
        interval=14400,
        first=120,
        chat_id=chat_id,
        name=f"perf_log_{chat_id}",
    )
    if config_manager.ENABLE_PREMARKET:
        job_queue.run_repeating(premarket_target_check, interval=300, first=30, chat_id=chat_id, name=f"premarket_{chat_id}")
    # post_close_sync_job(기본 16:40)보다 먼저 돌면 sync 후 메모리 0 → 평단 캡처 실패·회차 미반영.
    # 장마감 동기화·매도수익 반영 이후에 스냅샷을 남기도록 16:50으로 둡니다.
    job_queue.run_daily(record_daily_performance, time=datetime.time(16, 50, tzinfo=tz_ny), days=(1, 2, 3, 4, 5), chat_id=chat_id)
