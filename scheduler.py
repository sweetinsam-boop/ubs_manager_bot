from __future__ import annotations

import asyncio
import datetime

import pytz
from telegram.ext import ContextTypes

import config_manager
import kis_client
import report_html
import state_manager
import utils
from bot_logging import get_logger
from notification_service import notifier
from state_manager import STRATEGY
from trade_service import RetryLater, TradeService
from ui_formatter import UIFormatter, pre

log = get_logger(__name__)

RETRY_EVERY = 300  # 조회 실패로 못 보낸 주문을 다시 시도하는 간격(초)
RETRY_MAX = 6      # 재시도 횟수 (5분 x 6 = 30분)

_daily_trade_running = False  # 같은 스케줄이 겹쳐 중복 주문되는 것을 막는 플래그


async def _html(chat_id, text: str) -> None:
    await notifier.broadcast_alert(chat_id, text, parse_mode="HTML")


async def daily_market_check(context: ContextTypes.DEFAULT_TYPE):
    """뉴욕 09:35: 동기화·지난 체결 정산 후 종목별 오늘 주문 전송."""
    global _daily_trade_running
    chat_id = context.job.chat_id

    if _daily_trade_running:
        log.warning("daily_market_check skipped: already running")
        await _html(chat_id, "자동매매 작업이 이미 실행 중이라 이번 스케줄은 건너뜁니다. (중복 실행 방지)")
        return
    if not utils.is_market_open_today():
        await _html(chat_id, "오늘은 미국 증시 휴장일입니다. 자동매매를 건너뜁니다.")
        return

    _daily_trade_running = True
    try:
        await _run_daily_market(chat_id)
    except Exception as e:
        log.exception("daily_market_check failed: %s", e)
        await _html(chat_id, f"자동매매 실행 중 오류로 중단되었습니다.\n{pre(str(e))}로그를 확인하세요 (/logs).")
        raise
    finally:
        _daily_trade_running = False


async def _order_round(chat_id, tickers: list[str]) -> tuple[list[str], str]:
    """동기화·정산 후 tickers의 오늘 주문을 보낸다. (조회 실패로 못 보낸 종목, 그 이유)를 돌려준다."""
    # 잔고를 모르는 채로 주문하면 보유 중에 첫 매수가 나갈 수 있다. 동기화가 안 되면 보내지 않는다.
    for attempt in range(3):
        ok, msg = await asyncio.to_thread(state_manager.sync_account)
        if ok:
            break
        log.warning("daily sync failed (attempt %d/3): %s", attempt + 1, msg)
        await asyncio.sleep(20)
    else:
        return tickers, f"계좌 동기화 실패: {msg}"
    log.info("모든 종목 및 자산 상태 동기화 완료")
    try:
        # 마감 후 정산이 빠졌다면 주문 계산 전에 T와 잔금을 먼저 맞춘다
        settled = await asyncio.to_thread(TradeService.settle_fills)
        if settled:
            await _html(chat_id, "<b>지난 체결 정산</b>\n" + pre("\n".join(settled)))
    except Exception as e:
        log.warning(f"지난 체결 정산 중 오류 (주문은 진행): {e}")

    pending, reason = [], ""
    for ticker in tickers:
        try:
            res = await asyncio.to_thread(TradeService.process_ticker_daily, ticker)
        except RetryLater as e:
            log.warning("%s 주문 보류: %s", ticker, e)
            pending.append(ticker)
            reason = str(e)
            continue
        await _html(chat_id, UIFormatter.order_result(res))
    return pending, reason


async def _run_daily_market(chat_id):
    now_kst = datetime.datetime.now(pytz.timezone('Asia/Seoul'))
    now_ny = datetime.datetime.now(pytz.timezone('America/New_York'))
    await _html(chat_id, f"<b>미국 정규장 오픈</b> ({now_kst.strftime('%m-%d %H:%M')} KST / {now_ny.strftime('%H:%M')} NY)\n"
                         "오늘의 주문 전송을 시작합니다.")

    # 조회 실패로 못 보낸 종목만 다시 시도한다. LOC·MOC는 장 마감 전에만 접수되면 결과가 같다.
    pending, reason = await _order_round(chat_id, list(STRATEGY))
    for attempt in range(RETRY_MAX):
        if not pending:
            break
        if attempt == 0:
            await _html(chat_id, f"<b>주문 보류</b> {', '.join(pending)}\n{pre(reason)}"
                                 f"{RETRY_EVERY // 60}분 간격으로 최대 {RETRY_MAX}번 다시 시도합니다.")
        await asyncio.sleep(RETRY_EVERY)
        pending, reason = await _order_round(chat_id, pending)
    if pending:
        await _html(chat_id, f"<b>자동 주문 실패</b> {', '.join(pending)}\n{pre(reason)}"
                             "다시 시도해도 조회가 되지 않아 오늘 주문을 보내지 못했습니다. "
                             "증권사 상태를 확인한 뒤 /manual_order 로 직접 전송하세요.")

    await asyncio.sleep(10)  # 접수 반영 대기
    lines = ["<b>주문 등록 확인</b> (미국 동부 당일 미체결만 집계)"]
    for ticker in STRATEGY:
        try:
            unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        except Exception as e:
            lines.append(f"{ticker}: 미체결 조회 실패 ({e})")
            continue
        lines.append(f"{ticker}: 미체결 {len(unfilled)}건 등록 확인" if unfilled
                     else f"{ticker}: 당일 미체결 없음 (전량 체결, 주문 없음 또는 조회 지연일 수 있음)")
    await _html(chat_id, "\n".join(lines))


async def send_html_report(bot, chat_id, caption: str) -> None:
    """HTML 리포트를 만들어 문서로 전송."""
    path = await asyncio.to_thread(report_html.render_current)
    with open(path, "rb") as f:
        await bot.send_document(chat_id=chat_id, document=f, filename=path.name, caption=caption)


async def settlement_check(context: ContextTypes.DEFAULT_TYPE):
    """한국 08:00: 전일 매매 보정과 정산 후 HTML 리포트 전송."""
    chat_id = context.job.chat_id
    try:
        pre_avgs = await asyncio.to_thread(TradeService.capture_pre_sync_avgs)
        msgs = await asyncio.to_thread(TradeService.settle_fills)
        if pre_avgs:
            msgs += await asyncio.to_thread(TradeService.reconcile_daily_sell_profits, pre_avgs)
        if msgs:
            await _html(chat_id, "<b>정산 전 보정</b>\n" + pre("\n".join(msgs)))
    except Exception as e:
        log.warning("settlement reconcile failed (continuing): %s", e)

    await send_html_report(context.bot, chat_id, "굿모닝. 전일 매매를 반영한 운용 리포트입니다.")
    utils.set_last_settlement_date(datetime.datetime.now(pytz.timezone("Asia/Seoul")).strftime("%Y%m%d"))
    log.info("settlement report sent")


def _do_performance_log_update() -> bool:
    """계좌 동기화 후 오늘(미국 기준) 자산·벤치마크를 성과 로그에 기록. 성공 시 True."""
    if not utils.is_market_open_today():
        return False
    try:
        state_manager.sync_account()
        total_equity = float(state_manager.CURRENT_CASH or 0)  # 모듈 변수를 직접 읽어야 최신 값이다
        for ticker, s in STRATEGY.items():
            qty = float((s.get("data") or {}).get("qty", 0) or 0)
            total_equity += qty * float(kis_client.get_current_price(ticker) or 0)
        utils.log_daily_performance(total_equity, kis_client.get_current_price("VOO"), kis_client.get_current_price("QQQ"))
        return True
    except Exception as e:
        log.warning("performance_log update failed: %s", e)
        return False


def _backfill_performance_log_from_kis() -> int:
    """성과 로그에 없는 거래일을 KIS 기간손익으로 보강. 채운 일수 반환."""
    from storage.file_store import save_json
    from storage.paths import get_path, PERFORMANCE_LOG_FILE

    data = utils.load_performance_log()
    dates_sorted = sorted({e.get("date") for e in data if e.get("date")})
    if not dates_sorted:
        return 0
    last_ymd = dates_sorted[-1].replace("-", "")
    end_ymd = utils.get_broker_calendar_end_ymd()
    if last_ymd >= end_ymd:
        return 0
    start_next = (datetime.datetime.strptime(last_ymd, "%Y%m%d").date() + datetime.timedelta(days=1)).strftime("%Y%m%d")
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
        new_equity = (by_date[prev_dates[-1]] if prev_dates else 0) + float(row.get("daily_pnl", 0) or 0)
        data.append({"date": d_str, "equity": new_equity, "benchmark": None, "nasdaq": None})
        by_date[d_str] = new_equity
        added += 1
    if added:
        data.sort(key=lambda e: (e.get("date") or ""))
        save_json(get_path(PERFORMANCE_LOG_FILE), data[-365:], ensure_ascii=False, indent=2)
    return added


async def record_daily_performance(context: ContextTypes.DEFAULT_TYPE):
    """장 마감 후 자산 현황을 기록."""
    await asyncio.to_thread(_do_performance_log_update)


async def periodic_performance_log_update(context: ContextTypes.DEFAULT_TYPE):
    """성과 로그를 주기적으로 갱신 (오늘 스냅샷 + 누락된 거래일 보강)."""
    updated = await asyncio.to_thread(_do_performance_log_update)
    filled = await asyncio.to_thread(_backfill_performance_log_from_kis)
    if updated or filled > 0:
        log.info("performance_log periodic: updated=%s backfilled=%d", updated, filled)


async def check_missed_orders(chat_id) -> None:
    """봇이 09:35에 꺼져 있었다면 오늘 주문이 안 나갔다고 알린다 (자동으로 보내지는 않는다)."""
    now = datetime.datetime.now(pytz.timezone("America/New_York"))
    if not utils.is_market_open_today() or not (9, 40) <= (now.hour, now.minute) < (15, 50):
        return
    missed = [tk for tk, s in STRATEGY.items() if not s.get("liquidating") and not utils.check_trade_lock(tk)]
    if missed:
        await _html(chat_id, f"봇이 다시 시작됐습니다. 오늘 자동 주문 기록이 없는 종목: {', '.join(missed)}\n"
                             "필요하면 /manual_order 로 확인한 뒤 전송하세요.")


async def check_missed_settlement(application):
    """봇 시작 시 오늘 아침 정산 리포트가 누락됐는지 확인하고 필요하면 만든다."""
    chat_id = utils.load_chat_id()
    if not chat_id:
        return
    now_kst = datetime.datetime.now(pytz.timezone("Asia/Seoul"))
    today = now_kst.strftime("%Y%m%d")
    if not utils.should_run_kst_morning_settlement(now_kst):
        log.info("missed-report skip (일·월 아침 정산 생략 요일)")
        return

    if now_kst.hour >= 8 and utils.get_last_settlement_date() != today:
        class _Job:
            def __init__(self, cid):
                self.chat_id = cid

        class _Ctx:
            bot = application.bot
            job = _Job(chat_id)

        log.info("missed-report: today's report (%s) missing, starting catch-up", today)
        await settlement_check(_Ctx())
    else:
        log.info("missed-report: no catch-up needed")


async def post_close_sync_job(context: ContextTypes.DEFAULT_TYPE):
    """장 마감 후: 동기화 → 체결 정산(T·잔금) → 매도 수익 반영 → 청산 종목 정리 → 마감 리포트."""
    if not utils.is_market_open_today():
        return
    chat_id = context.job.chat_id

    pre_sync_avgs = await asyncio.to_thread(TradeService.capture_pre_sync_avgs)
    success, msg = await asyncio.to_thread(state_manager.sync_account)
    if not success:
        await _html(chat_id, "<b>장 마감 후 자동 동기화 실패</b>\n" + pre(msg or "(메시지 없음)"))
        return

    notes: list[str] = []
    try:
        notes += await asyncio.to_thread(TradeService.settle_fills)
        if pre_sync_avgs:
            notes += await asyncio.to_thread(TradeService.reconcile_daily_sell_profits, pre_sync_avgs)
    except Exception as e:
        log.warning("post_close settle/reconcile failed: %s", e, exc_info=True)
        notes.append(f"정산 중 오류: {e}")

    # /liquidate로 매도한 종목은 체결·수익 기록이 끝난 지금 설정에서 삭제한다
    try:
        done = [tk for tk, s in STRATEGY.items()
                if s.get("liquidating") and float((s.get("data") or {}).get("qty", 0) or 0) <= 0]
        if done:
            new_settings = dict(config_manager.STRATEGY_SETTINGS)
            for tk in done:
                new_settings.pop(tk, None)
                STRATEGY.pop(tk, None)
                utils.delete_ticker_from_all_states(tk)
                notes.append(f"{tk} 청산 완료. 포트폴리오에서 삭제했습니다.")
            config_manager.save_config({"STRATEGY_SETTINGS": new_settings})
        for tk, s in STRATEGY.items():
            if s.get("liquidating"):
                notes.append(f"{tk} 청산 주문이 아직 체결되지 않았습니다. 자동 매매는 멈춰 있으니 /liquidate 를 다시 실행하세요.")
    except Exception as e:
        log.warning("liquidating cleanup failed: %s", e, exc_info=True)

    data = await asyncio.to_thread(TradeService.get_portfolio_summary)
    data["title"] = "장 마감 리포트"
    text = UIFormatter.daily_report(data)
    if notes:
        text += "\n\n<b>정산</b>\n" + pre("\n".join(notes))
    await _html(chat_id, text)


def register_schedules(job_queue, chat_id):
    if job_queue is None:
        log.error("JobQueue가 초기화되지 않았습니다. python-telegram-bot[job-queue] 설치를 확인하세요.")
        return
    tz_ny = pytz.timezone('America/New_York')
    kst = pytz.timezone('Asia/Seoul')

    for job in job_queue.jobs():
        job.schedule_removal()

    # python-telegram-bot v20+ 요일: 0=일요일 ... 6=토요일 (datetime.weekday()의 0=월요일과 다르다)
    weekdays = (1, 2, 3, 4, 5)
    job_queue.run_daily(daily_market_check, time=datetime.time(9, 35, tzinfo=tz_ny), days=weekdays, chat_id=chat_id)
    pc_h, pc_m = config_manager.get_post_close_sync_ny_time()
    job_queue.run_daily(post_close_sync_job, time=datetime.time(pc_h, pc_m, tzinfo=tz_ny), days=weekdays, chat_id=chat_id)
    # 마감 동기화·정산 이후에 스냅샷을 남기도록 16:50
    job_queue.run_daily(record_daily_performance, time=datetime.time(16, 50, tzinfo=tz_ny), days=weekdays, chat_id=chat_id)
    # 한국 08:00 정산: 화~토 (일·월 아침은 직전 미국 휴장이라 생략)
    job_queue.run_daily(settlement_check, time=datetime.time(8, 0, tzinfo=kst), days=(2, 3, 4, 5, 6), chat_id=chat_id)
    job_queue.run_repeating(periodic_performance_log_update, interval=14400, first=120, chat_id=chat_id,
                            name=f"perf_log_{chat_id}")
