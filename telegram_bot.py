"""
텔레그램 봇: 커맨드/콜백 핸들러 및 메뉴 (무한매수법 V4.0 전용). 응답은 HTML parse_mode.
콜백 액션은 CALLBACK_HANDLERS 매핑으로 디스패치합니다.
"""
from __future__ import annotations

import asyncio
import html
import io
import re
import traceback

import pytz
from dateutil import parser
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import ContextTypes

import guide_theory
import kis_client
import report_html
import state_manager
import strategy
import utils
from bot_logging import get_logger, read_recent_log_text
from notification_service import notifier
from state_manager import STRATEGY
from trade_service import TradeService
from ui_formatter import UIFormatter, pre

log = get_logger(__name__)
_e = html.escape
TG_SAFE = 3900


async def _send(event, text: str, reply_markup=None) -> None:
    """메시지면 새로 답하고, 콜백 쿼리면 그 메시지를 수정한다."""
    kw = {"parse_mode": "HTML", "reply_markup": reply_markup, "disable_web_page_preview": True}
    if hasattr(event, "edit_message_text"):
        await event.edit_message_text(text, **kw)
    else:
        await event.reply_text(text, **kw)


async def _send_long(message, text: str) -> None:
    """긴 HTML 본문을 빈 줄 경계로 나눠 보낸다 (각 문단이 자체 완결된 태그여야 함)."""
    t = text.strip()
    while t:
        if len(t) <= TG_SAFE:
            await message.reply_text(t, parse_mode="HTML")
            return
        cut = t.rfind("\n\n", 0, TG_SAFE)
        cut = cut if cut > TG_SAFE // 3 else TG_SAFE
        await message.reply_text(t[:cut].strip(), parse_mode="HTML")
        t = t[cut:].strip()


def main_reply_keyboard_markup() -> ReplyKeyboardMarkup:
    rows = [
        ["현황 /status", "예수금 /cash"],
        ["리포트 /report", "수익기록 /profit_history"],
        ["동기화 /sync", "수동주문 /manual_order"],
        ["도움말 /help"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=cb) for t, cb in row] for row in rows])


async def _active_unfilled(ticker: str) -> int:
    try:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        return sum(1 for u in unfilled if u.get("status") == "대기") if unfilled else 0
    except Exception as e:
        log.warning("미체결 확인 중 오류: %s", e)
        return 0


# ── 시작·도움말·조회 ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    utils.save_chat_id(chat_id)

    from scheduler import register_schedules
    register_schedules(context.job_queue, chat_id)

    from config_manager import get_post_close_sync_ny_time
    h, m = get_post_close_sync_ny_time()
    msg = (
        "<b>무한매수법 V4.0 봇이 가동 중입니다.</b>\n\n"
        "설정한 종목을 규칙대로 매일 자동 매매합니다. 하는 일은 다음과 같습니다.\n"
        "- 뉴욕 09:35 (한국 22:35 또는 23:35): 정규장 주문 전송\n"
        f"- 뉴욕 {h:02d}:{m:02d}: 계좌 동기화와 체결 정산(T, 잔금 갱신)\n"
        "- 한국 08:00 (화~토): 전일 매매 리포트와 HTML 리포트\n\n"
        "아래 버튼으로 주요 기능을 바로 쓸 수 있고, 자세한 설명은 /help 에 있습니다. "
        "오류가 의심되면 /logs 로 최근 24시간 기록을 볼 수 있습니다."
    )
    if not STRATEGY:
        msg += "\n\n등록된 종목이 없습니다. <code>/add_target TQQQ 8000 20</code> 처럼 추가하세요 (종목, 원금, 분할 수)."
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_reply_keyboard_markup())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "<b>무한매수봇 도움말</b>\n\n"
        "<b>조회</b>\n"
        "/status 현황과 오늘 계획, /report HTML 리포트, /cash 예수금, /profit_history 익절 기록, /logs 최근 24시간 로그\n\n"
        "<b>제어</b>\n"
        "/sync 계좌 동기화와 체결 정산, /manual_order 오늘 주문을 수동으로 전송, /set_t 진행도 T 보정\n\n"
        "<b>설정</b>\n"
        "/add_target 종목 원금 분할수 [수익%] 추가\n"
        "/edit_target 종목 원금 분할수 [수익%] 수정\n"
        "/del_target 감시 중단 (주식은 팔지 않음)\n\n"
        "<b>긴급</b>\n"
        "/liquidate 전량 매도 후 종료, /profit_reset 누적 수익 기록 초기화\n\n"
        "<b>자주 하는 일</b>\n"
        "- 증권사 앱에서 직접 매매했거나 입금했다면 /sync 후 /status 로 확인합니다.\n"
        "- T가 증권사 앱·카페 엑셀의 진행과 다르면 <code>/set_t TQQQ 7.5</code> 처럼 맞춥니다. 값을 생략하면 보유 원가로 다시 추정합니다.\n\n"
        "아래 버튼에서 기능 원리와 V4.0 이론을 볼 수 있습니다."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_kb(
        [("현황 원리", "guide:status"), ("주문과 정산 원리", "guide:orders")],
        [("V4.0 이론", "guide:theory_40")],
    ))


GUIDE_TEXTS = {
    "status": (
        "<b>현황 원리</b>\n\n"
        "증권사에서 잔고와 시세를 읽고, 봇이 저장한 T와 잔금으로 오늘의 별지점, 목표가, 주문 계획을 계산해 보여줍니다. "
        "조회만 하며 주문이나 상태 변경은 일어나지 않습니다. 종목 버튼을 누르면 그 종목의 상세 지시서를 볼 수 있습니다."
    ),
    "orders": (
        "<b>주문과 정산 원리</b>\n\n"
        "1. 뉴욕 09:35에 오늘 주문(LOC, 지정가, MOC)을 전송하고 주문번호를 저장합니다.\n"
        "2. 장 마감 후 그 주문번호의 체결 내역을 읽습니다.\n"
        "3. 체결 결과에 따라 T와 잔금을 갱신합니다 (매수 +1 또는 +0.5, 쿼터매도 ×0.75 등).\n"
        "정산은 같은 주문을 두 번 반영하지 않고, 증권사 조회가 실패하면 다음 기회로 미룹니다."
    ),
    "theory_40": guide_theory.THEORY_40,
}


async def _cb_guide(update, context, query, parts):
    content = GUIDE_TEXTS.get(parts[1], "")
    if content:
        await _send_long(query.message, content)
    else:
        await query.message.reply_text("해당 가이드가 없습니다.")


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("최근 24시간 로그를 불러오는 중입니다.")
    preview, prev_trunc = read_recent_log_text(hours=24, max_total_chars=3500)
    full, full_trunc = read_recent_log_text(hours=24, max_total_chars=400_000)
    text = "최근 24시간 로그 (KST)\n서버: logs/bot.log, 30일 보관\n" + "─" * 20 + "\n\n" + preview
    if prev_trunc:
        text += "\n\n(미리보기는 뒷부분 위주입니다. 파일에 전체가 담깁니다.)"
    await update.message.reply_text(text)
    if len(full) > 3200 or prev_trunc or full_trunc:
        bio = io.BytesIO(full.encode("utf-8"))
        bio.name = "bot_log_24h.txt"
        await update.message.reply_document(document=bio, caption="최근 24시간 로그 전문")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("시세와 주문 내역을 조회하는 중입니다.")
    data = await asyncio.to_thread(TradeService.get_portfolio_summary)
    rows = [[(f"{t} 지시서", f"status_dive:{t}") for t in STRATEGY]] if STRATEGY else []
    rows.append([("HTML 리포트 받기", "html_report")])
    await update.message.reply_text(UIFormatter.daily_report(data), parse_mode="HTML", reply_markup=_kb(*rows))


def _build_cash_message():
    """KIS 예수금·보유 조회(동기). 성공 시 (msg, None), 실패 시 (None, err)."""
    try:
        token = kis_client.get_kis_access_token()
        usd_cash, _src = kis_client.query_available_cash(token)
        data = kis_client.query_account_raw() or {}
        lines = ["[예수금 조회]", f"달러 예수금: ${usd_cash:,.2f}",
                 f"원화 예수금: ₩{int(kis_client.query_krw_cash(token)):,}"]
        rate, ref_raw = kis_client.get_exchange_rate()
        try:
            dt = parser.parse(ref_raw)
            ref = f"{dt.strftime('%m-%d %H:%M')} KST / {dt.astimezone(pytz.timezone('America/New_York')).strftime('%m-%d %H:%M')} NY"
        except Exception:
            ref = ref_raw
        lines += ["─" * 20, f"환율: {rate:,.2f}원 ({ref})"]

        stock_total = 0.0
        holdings = data.get("output1", []) if data.get("rt_cd") == "0" else []
        if holdings:
            lines.append("\n[보유 종목] (* 봇 운용 종목)")
            for item in holdings:
                sym = item.get("ovrs_pdno", "?").strip()
                qty, avg = float(item.get("ovrs_cblc_qty", "0")), float(item.get("pchs_avg_pric", "0"))
                now = kis_client.get_current_price(sym)
                stock_total += now * qty
                rt = (now - avg) / avg * 100 if avg > 0 else 0.0
                mark = " *" if sym in STRATEGY else ""
                lines.append(f"  {sym}{mark}: {qty:g}주, 현재 ${now:,.2f}, 손익 ${(now - avg) * qty:,.2f} ({rt:.1f}%)")
        lines.append(f"\n현금 ${usd_cash:,.2f} / 주식 ${stock_total:,.2f}")
        return "\n".join(lines), None
    except Exception as e:
        return None, str(e)


async def cmd_cash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("예수금을 조회하는 중입니다.")
    msg, err = await asyncio.to_thread(_build_cash_message)
    await update.message.reply_text(msg if not err else f"조회 오류: {err}")


async def _send_html_report(message) -> None:
    path = await asyncio.to_thread(report_html.render_current)
    cp = utils.aggregate_cumul_profit_by_ticker(utils.load_cumul_profit())
    total = sum(cp.values())
    caption = f"무한매수 운용 리포트. 누적 실현수익 ${total:,.2f}. 파일을 열어 브라우저로 보세요."
    with open(path, "rb") as f:
        await message.reply_document(document=f, filename=path.name, caption=caption)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("리포트를 만드는 중입니다.")
    await _send_html_report(update.message)


async def _cb_html_report(update, context, query, parts):
    await query.message.reply_text("리포트를 만드는 중입니다.")
    await _send_html_report(query.message)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = await update.message.reply_text("계좌 정보를 최신화하는 중입니다.")
    pre_avgs = await asyncio.to_thread(TradeService.capture_pre_sync_avgs)
    ok, res = await asyncio.to_thread(state_manager.sync_account)
    if not ok:
        await wait.edit_text(f"[동기화 실패] {res}")
        return
    lines = [f"[동기화 완료] {res}"]
    try:
        lines += await asyncio.to_thread(TradeService.settle_fills)
        if pre_avgs:
            lines += await asyncio.to_thread(TradeService.reconcile_daily_sell_profits, pre_avgs)
    except Exception as e:
        log.warning("cmd_sync settle failed: %s", e, exc_info=True)
        lines.append(f"체결 정산 중 오류: {e}")
    await wait.edit_text("\n".join(lines))


async def cmd_set_t(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or args[0].upper() not in STRATEGY:
        await update.message.reply_text(
            "사용법: /set_t 종목 [T값]\n예: /set_t TQQQ 7.5\n값을 생략하면 보유 원가로 T를 다시 추정합니다.\n"
            "등록 종목: " + ", ".join(STRATEGY))
        return
    ticker = args[0].upper()
    try:
        t_value = float(args[1]) if len(args) > 1 else None
    except ValueError:
        await update.message.reply_text("T값은 숫자로 입력하세요. 예: /set_t TQQQ 7.5")
        return
    if t_value is not None and not 0 <= t_value <= STRATEGY[ticker]["total_a"] + 5:
        await update.message.reply_text("T값 범위가 올바르지 않습니다.")
        return
    await asyncio.to_thread(state_manager.sync_account)
    t, pool = await asyncio.to_thread(strategy.recalibrate, ticker, t_value)
    await update.message.reply_text(
        f"{ticker} 진행도를 보정했습니다.\nT {t:.2f} / {STRATEGY[ticker]['total_a']:g}, 잔금 ${pool:,.2f} (원금 - 보유 원가)")


# ── 수동 주문 ────────────────────────────────────────────────────

async def cmd_manual_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = await update.message.reply_text("수동 주문 준비 중입니다. 계좌를 동기화하고 주문 상태를 확인합니다.")
    ok, res = await asyncio.to_thread(state_manager.sync_account)
    if not ok:
        await wait.edit_text(f"[동기화 실패] {res}")
        return
    if not STRATEGY:
        await wait.edit_text("등록된 종목이 없습니다. /add_target 으로 추가하세요.")
        return

    lines = ["<b>수동 주문 현황</b> (미국 동부 당일 기준)"]
    rows = []
    for ticker in STRATEGY:
        locked = utils.check_trade_lock(ticker)
        active = await _active_unfilled(ticker)
        lines.append(f"{_e(ticker)}: {'오늘 주문 전송됨' if locked else '오늘 주문 전'}, 미체결 대기 {active}건")
        rows.append([(f"{ticker} 미체결 상세", f"unfilled:{ticker}"), (f"{ticker} 오늘 주문", f"m_plan:{ticker}")])
    lines.append("\n이미 주문이 있는 종목에 다시 주문하면 중복 매수가 됩니다. 먼저 /status 와 미체결 상세를 확인하세요.")
    await wait.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=_kb(*rows))


async def _cb_m_plan(update, context, query, parts):
    """오늘 계획을 보여 주고 전송 여부를 확인받는다."""
    ticker = parts[1]
    info = await asyncio.to_thread(strategy.build_order_info, ticker)
    rows = []
    if info["orders"]:
        rows.append([("이 주문 전송", f"m_go:{ticker}")])
    elif info["mode"] == "first" and info["price"] > 0:
        rows.append([("1주만 진입 (LOC)", f"m_force:{ticker}")])
    rows.append([("취소", "cancel")])
    warn = "\n\n오늘 이미 주문이 전송됐습니다. 중복 주의." if utils.check_trade_lock(ticker) else ""
    await _send(query, UIFormatter.instruction(info) + warn, _kb(*rows))


async def _cb_m_go(update, context, query, parts):
    ticker = parts[1]
    if await _active_unfilled(ticker):
        await query.edit_message_text(f"{ticker}: 이미 접수된 미체결 주문이 있어 중복 방지를 위해 전송하지 않았습니다.")
        return
    await query.edit_message_text(f"{ticker} 주문을 전송하는 중입니다.")
    res = await asyncio.to_thread(TradeService.execute_daily_order, ticker)
    await _send(query, UIFormatter.order_result(res))


async def _cb_m_force(update, context, query, parts):
    ticker = parts[1]
    if await _active_unfilled(ticker):
        await query.edit_message_text(f"{ticker}: 미체결 대기 주문이 있어 전송하지 않았습니다.")
        return
    text, ok = await asyncio.to_thread(strategy.force_first_entry, ticker)
    if ok:
        utils.set_trade_lock(ticker)
        utils.start_cycle_for_new_entry(ticker, increment_on_reentry=True)
    await _send(query, UIFormatter.order_result(text))


async def _cb_unfilled(update, context, query, parts):
    ticker = parts[1]
    try:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
    except Exception as e:
        await query.edit_message_text(f"미체결 조회 오류: {e}")
        return
    if not unfilled:
        await query.edit_message_text(f"{ticker}: 미국 당일 미체결 주문이 없습니다.")
        return
    lines = [f"{u.get('side', '?')} {u.get('qty', 0)}주 @ ${u.get('price', 0.0):.2f} ({u.get('type', '')}) {u.get('status', '')}"
             for u in sorted(unfilled, key=lambda u: u.get("ord_tmd") or "")]
    await _send(query, f"<b>{_e(ticker)} 미체결 상세</b> (미국 동부 당일)\n" + pre("\n".join(lines))
                + "\n'완료'는 주문 처리가 끝났다는 뜻이며 일부 잔량이 남았을 수 있습니다.")


# ── 상세 지시서·주문 내역 ────────────────────────────────────────

def _recent_rows(ticker: str, days: int, pages: int) -> list:
    """사이클 시작일 이후, 주문번호 중복 제거, 의미 있는 가격만, 최신순."""
    rows = kis_client.query_recent_orders(ticker, days, pages) or []
    start = ((utils.load_cycle_state().get(ticker, {}).get("active") or {}).get("start_date") or "").replace("-", "")
    seen, out = set(), []
    for o in rows:
        od, dt = o.get("odno"), (o.get("ord_dt") or "").strip()
        if (od and od in seen) or (start and dt and dt < start):
            continue
        if od:
            seen.add(od)
        try:
            if float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", 0)) or 0) <= 0 and float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", 0)) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            pass
        out.append(o)
    return sorted(out, key=lambda o: (o.get("ord_dt") or "", o.get("ord_tmd") or "", o.get("odno") or ""), reverse=True)


def _i(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


async def _cb_status_dive(update, context, query, parts):
    ticker = parts[1]
    info = await asyncio.to_thread(strategy.build_order_info, ticker)
    text = UIFormatter.instruction(info)
    buttons = []
    try:
        rows = await asyncio.to_thread(_recent_rows, ticker, 5, 3)
        ny = pytz.timezone("US/Eastern")
        import datetime as _dt
        now = _dt.datetime.now(ny)
        days = {(label, d): [] for label, d in (("오늘", now.strftime("%Y%m%d")), ("전일", (now - _dt.timedelta(days=1)).strftime("%Y%m%d")))}
        for o in rows:
            for key in days:
                if (o.get("ord_dt") or "").strip() == key[1]:
                    days[key].append(o)
        lines = []
        for (label, d), day in days.items():
            if not day:
                lines.append(f"{label}: 내역 없음")
                continue
            b_ord = sum(_i(o.get("ft_ord_qty")) for o in day if o.get("sll_buy_dvsn_cd") == "02")
            b_ccl = sum(_i(o.get("ft_ccld_qty")) for o in day if o.get("sll_buy_dvsn_cd") == "02")
            s_ord = sum(_i(o.get("ft_ord_qty")) for o in day if o.get("sll_buy_dvsn_cd") != "02")
            s_ccl = sum(_i(o.get("ft_ccld_qty")) for o in day if o.get("sll_buy_dvsn_cd") != "02")
            lines.append(f"{label}({d[:4]}-{d[4:6]}-{d[6:8]}): 매수 {b_ccl}/{b_ord}주 체결, 매도 {s_ccl}/{s_ord}주 체결")
            buttons.append((f"{label} 상세", f"orders_day:{ticker}:{d}"))
        text += "\n\n<b>증권사 주문 내역</b>\n" + "\n".join(_e(x) for x in lines)
    except Exception as e:
        log.warning("status_dive orders failed: %s", e)
        text += "\n\n증권사 주문 내역을 가져오지 못했습니다."
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=_kb(buttons) if buttons else None)


async def _cb_orders_day(update, context, query, parts):
    ticker, ymd = parts[1], parts[2]
    rows = [o for o in await asyncio.to_thread(_recent_rows, ticker, 10, 4) if (o.get("ord_dt") or "").strip() == ymd]
    title = f"<b>{_e(ticker)} {ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} 주문 상세</b>\n"
    if not rows:
        await query.edit_message_text(title + "내역이 없습니다.", parse_mode="HTML")
        return
    lines = []
    for o in sorted(rows, key=lambda o: (o.get("ord_tmd") or "", o.get("odno") or ""), reverse=True)[:30]:
        t = (o.get("ord_tmd") or "").strip()
        tm = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 else ""
        side = (o.get("sll_buy_dvsn_cd_name") or "").strip() or ("매수" if o.get("sll_buy_dvsn_cd") == "02" else "매도")
        lines.append(f"{tm} {side} {_i(o.get('ft_ord_qty'))}주 @ ${float(o.get('ft_ord_unpr3', o.get('ft_ord_unpr', 0)) or 0):.2f}"
                     f" → 체결 {_i(o.get('ft_ccld_qty'))}주 @ ${float(o.get('ft_ccld_unpr3', o.get('ft_ccld_unpr', 0)) or 0):.2f}"
                     f" ({(o.get('prcs_stat_name') or '').strip()})")
    await query.edit_message_text(title + pre("\n".join(lines)), parse_mode="HTML")


# ── 수익 기록 ────────────────────────────────────────────────────

async def cmd_profit_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = utils.load_cumul_profit()
    history = [h for h in (cp.get("history") or []) if isinstance(h, dict)]
    totals = utils.aggregate_cumul_profit_by_ticker(cp)
    if not history:
        extra = "\n".join(f"{t}: ${v:,.2f}" for t, v in sorted(totals.items()) if abs(v) > 1e-9)
        await update.message.reply_text("확정된 건별 익절 기록이 없습니다." + (f"\n종목별 누적:\n{extra}" if extra else
                                        "\n목표가 익절이 체결되면 날짜와 손익이 여기에 쌓입니다."))
        return
    lines = [f"누적 {t}: ${v:,.2f}" for t, v in sorted(totals.items())] + ["", "최근 5건 (날짜, 종목, 수량, 매도가, 손익)"]
    for h in history[-5:]:
        lines.append(f"{str(h.get('date', '?'))[:10]} {h.get('ticker', '?')} {_i(h.get('sell_qty'))}주 "
                     f"${float(h.get('sell_price') or 0):.2f} {float(h.get('profit') or 0):+,.2f}")
    await update.message.reply_text("<b>실현 수익 요약</b>\n" + pre("\n".join(lines)), parse_mode="HTML",
                                    reply_markup=_kb([("전체 기록 보기", "profit_dive")]))


async def _cb_profit_dive(update, context, query, parts):
    history = [h for h in (utils.load_cumul_profit().get("history") or []) if isinstance(h, dict)]
    if not history:
        await query.message.reply_text("건별 기록이 없습니다.")
        return
    for i in range(0, len(history), 20):
        chunk = history[i:i + 20]
        lines = [f"{str(h.get('date', '?'))[:16]} {h.get('ticker', '?'):<5}{_i(h.get('sell_qty')):>4}주 "
                 f"평단${float(h.get('avg_price') or 0):.2f} 매도${float(h.get('sell_price') or 0):.2f} {float(h.get('profit') or 0):+,.2f}"
                 for h in chunk]
        await query.message.reply_text(f"<b>전체 수익 기록</b> ({i // 20 + 1}/{(len(history) + 19) // 20})\n" + pre("\n".join(lines)),
                                       parse_mode="HTML")


async def cmd_profit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("누적 실현수익 기록을 모두 지웁니다. 되돌릴 수 없습니다. 계속할까요?",
                                    reply_markup=_kb([("초기화", "exec_reset"), ("취소", "cancel")]))


async def _cb_exec_reset(update, context, query, parts):
    utils.save_cumul_profit({"history": [], "processed_orders": []})
    await query.edit_message_text("누적 실현수익 기록을 초기화했습니다.")


# ── 종목 설정 ────────────────────────────────────────────────────

_TICKER_RE = re.compile(r"^[A-Z]{1,6}$")


def _parse_target_args(args: list[str], current: dict | None):
    """(ticker, seed, n, target, error). 수익%는 TQQQ/SOXL이면 공식 값(15/20)이 자동 적용된다."""
    ticker = args[0].upper()
    if not _TICKER_RE.match(ticker):
        return None, 0, 0, 0, "종목 코드는 영문 대문자 1~6자로 입력하세요."
    try:
        seed, n = float(args[1]), float(args[2])
        target = float(args[3]) if len(args) > 3 else float((current or {}).get("target_profit", 10.0))
    except ValueError:
        return None, 0, 0, 0, "원금, 분할 수, 수익%는 숫자로 입력하세요."
    if seed <= 0:
        return None, 0, 0, 0, "원금은 0보다 커야 합니다."
    if int(n) != n or not 5 <= n <= 300:
        return None, 0, 0, 0, "분할 수는 5~300 사이 정수로 입력하세요 (권장 20 또는 40)."
    if not 0 < target <= 200:
        return None, 0, 0, 0, "수익%는 0보다 크고 200 이하여야 합니다."
    return ticker, seed, int(n), target, ""


def _target_usage(cmd: str) -> str:
    return (f"사용법: /{cmd} 종목 원금 분할수 [수익%]\n예: /{cmd} TQQQ 8000 20\n"
            "TQQQ는 +15%, SOXL은 +20% 지정가 익절이 자동 적용됩니다. 그 외 종목은 수익%(기본 10)를 씁니다.\n"
            "분할 수는 20 또는 40을 권장합니다. 공식 V4.0은 TQQQ와 SOXL만 다룹니다.")


async def _save_target(update, args, adding: bool):
    cmd = "add_target" if adding else "edit_target"
    from config_manager import STRATEGY_SETTINGS, save_config
    prev = STRATEGY_SETTINGS.get(args[0].upper()) if args else None
    if len(args) < 3 or (not adding and not prev):
        await update.message.reply_text(_target_usage(cmd) + ("\n등록 종목: " + ", ".join(STRATEGY) if not adding else ""))
        return
    ticker, seed, n, target, err = _parse_target_args(args, prev)
    if err:
        await update.message.reply_text(err)
        return
    new_settings = dict(STRATEGY_SETTINGS)
    new_settings[ticker] = {"seed": seed, "total_a": n, "target_profit": target}
    if not save_config({"STRATEGY_SETTINGS": new_settings}):
        await update.message.reply_text("설정 저장에 실패했습니다.")
        return
    STRATEGY[ticker] = state_manager.make_entry(new_settings[ticker], STRATEGY.get(ticker, {}).get("data"))
    note = "" if adding else "\n진행 중인 사이클의 T와 잔금은 그대로입니다. 필요하면 /set_t 로 보정하세요."
    if ticker not in ("TQQQ", "SOXL"):
        note += "\nTQQQ와 SOXL 외 종목은 공식 방법론의 검증 대상이 아닙니다."
    await update.message.reply_text(
        f"{ticker} 설정을 {'추가' if adding else '수정'}했습니다.\n원금 ${seed:,.0f}, {n}분할, 1회 ${seed / n:,.0f}, "
        f"지정가 익절 +{strategy.profit_target_pct(ticker):g}%{note}")


async def cmd_add_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _save_target(update, context.args, adding=True)


async def cmd_edit_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args and STRATEGY:
        rows = [[(f"{t} 수정 방법", f"edit_pick:{t}")] for t in STRATEGY]
        await update.message.reply_text("수정할 종목을 고르세요.", reply_markup=_kb(*rows))
        return
    await _save_target(update, context.args, adding=False)


async def _cb_edit_pick(update, context, query, parts):
    s = STRATEGY.get(parts[1])
    if not s:
        await query.edit_message_text("등록되지 않은 종목입니다.")
        return
    await query.edit_message_text(
        f"{parts[1]} 현재 원금 ${s['seed']:,.0f}, {s['total_a']:g}분할.\n"
        f"이 형식으로 보내세요: /edit_target {parts[1]} {s['seed']:g} {s['total_a']:g}")


async def cmd_del_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not STRATEGY:
        await update.message.reply_text("등록된 종목이 없습니다.")
        return
    if context.args:
        await _confirm_del(update.message, context.args[0].upper())
        return
    rows = [[(f"{t} 삭제", f"ask_del:{t}")] for t in STRATEGY]
    await update.message.reply_text("감시를 중단할 종목을 고르세요. 실제 계좌의 주식은 팔리지 않습니다.", reply_markup=_kb(*rows))


async def _confirm_del(event, ticker: str):
    if ticker not in STRATEGY:
        await _send(event, f"{_e(ticker)}는 등록된 종목이 아닙니다.")
        return
    await _send(event, f"<b>{_e(ticker)}</b> 감시를 중단할까요? 계좌의 주식은 그대로 남고 봇만 더 이상 관리하지 않습니다.",
                _kb([("삭제", f"exec_del:{ticker}"), ("취소", "cancel")]))


async def _cb_ask_del(update, context, query, parts):
    await _confirm_del(query, parts[1])


async def _cb_exec_del(update, context, query, parts):
    ticker = parts[1]
    from config_manager import STRATEGY_SETTINGS, save_config
    new_settings = {k: v for k, v in STRATEGY_SETTINGS.items() if k != ticker}
    if ticker not in STRATEGY_SETTINGS or not save_config({"STRATEGY_SETTINGS": new_settings}):
        await query.edit_message_text("삭제하지 못했습니다.")
        return
    STRATEGY.pop(ticker, None)
    utils.delete_ticker_from_all_states(ticker)
    await query.edit_message_text(f"{ticker} 감시를 중단했습니다.")


# ── 긴급 청산 ────────────────────────────────────────────────────

async def cmd_liquidate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    held = [t for t in STRATEGY if STRATEGY[t]["data"]["qty"] > 0]
    if not held:
        await update.message.reply_text("보유 수량이 있는 종목이 없습니다.")
        return
    if context.args and context.args[0].upper() in held:
        await _ask_liq(update.message, context.args[0].upper())
        return
    await update.message.reply_text(
        "<b>전량 매도 후 종료</b>\n보유 전량을 매도하고 체결이 확인되면 이 종목의 감시 설정을 삭제합니다. 종목을 고르세요.",
        parse_mode="HTML", reply_markup=_kb(*[[(f"{t} 전량 매도", f"ask_liq:{t}")] for t in held]))


async def _ask_liq(event, ticker: str):
    await _send(event, f"<b>{_e(ticker)}</b> 사이클을 종료합니다. 매도 방식을 고르세요.",
                _kb([("시장가 MOC", f"exec_liq:{ticker}:02"), ("현재가 지정가", f"exec_liq:{ticker}:00")], [("취소", "cancel")]))


async def _cb_ask_liq(update, context, query, parts):
    await _ask_liq(query, parts[1])


async def _cb_exec_liq(update, context, query, parts):
    ticker = parts[1]
    ord_type = parts[2] if len(parts) > 2 else "02"
    if ticker not in STRATEGY or STRATEGY[ticker]["data"]["qty"] <= 0:
        await query.edit_message_text("보유 수량이 없습니다.")
        return
    qty = int(STRATEGY[ticker]["data"]["qty"])
    price = 0.0
    if ord_type == "00":
        price = await asyncio.to_thread(kis_client.get_current_price, ticker)
    name = "시장가(MOC)" if ord_type == "02" else "지정가"
    await query.edit_message_text(f"{ticker} {qty}주 전량 {name} 매도 주문 중입니다.")
    res = await asyncio.to_thread(kis_client.send_order, ticker, qty, price, "SELL", "MOC" if ord_type == "02" else "00")
    if res.get("rt_cd") != "0":
        await query.edit_message_text(f"매도 실패: {res.get('msg1')}")
        return
    STRATEGY[ticker]["liquidating"] = True  # 체결 확인과 수익 기록이 끝난 뒤 장 마감 후 동기화에서 삭제
    utils.set_trade_lock(ticker)
    await query.edit_message_text(
        f"{ticker} 전량 매도 주문을 접수했습니다.\n장 마감 후 체결 확인과 수익 기록이 끝나면 설정이 자동으로 삭제됩니다.")


async def _cb_cancel(update, context, query, parts):
    await query.edit_message_text("작업이 취소되었습니다.")


CALLBACK_HANDLERS = {
    "guide": _cb_guide, "html_report": _cb_html_report,
    "m_plan": _cb_m_plan, "m_go": _cb_m_go, "m_force": _cb_m_force, "unfilled": _cb_unfilled,
    "status_dive": _cb_status_dive, "orders_day": _cb_orders_day, "profit_dive": _cb_profit_dive,
    "exec_reset": _cb_exec_reset, "edit_pick": _cb_edit_pick,
    "ask_del": _cb_ask_del, "exec_del": _cb_exec_del, "ask_liq": _cb_ask_liq, "exec_liq": _cb_exec_liq,
    "cancel": _cb_cancel,
}


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    handler = CALLBACK_HANDLERS.get(parts[0])
    if handler:
        await handler(update, context, query, parts)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "") if update.message else ""
    if text.startswith("/"):
        await update.message.reply_text("알 수 없는 명령어입니다. /help 에서 전체 명령어를 확인하세요.",
                                        reply_markup=main_reply_keyboard_markup())


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if err is None:
        log.error("Telegram handler error (no exception object)")
        return
    log.error("Telegram handler error: %s\n%s", err,
              "".join(traceback.format_exception(type(err), err, getattr(err, "__traceback__", None))))


async def post_init(application):
    """봇 초기화: 메뉴 명령어 등록, 스케줄 복원, 누락 정산 체크."""
    await application.bot.set_my_commands([
        BotCommand("help", "도움말"), BotCommand("status", "현황"), BotCommand("report", "HTML 리포트"),
        BotCommand("cash", "예수금"), BotCommand("profit_history", "수익기록"), BotCommand("logs", "최근 24시간 로그"),
        BotCommand("sync", "동기화와 체결 정산"), BotCommand("manual_order", "수동 주문"), BotCommand("set_t", "진행도 T 보정"),
        BotCommand("add_target", "종목 추가"), BotCommand("edit_target", "종목 수정"), BotCommand("del_target", "종목 삭제"),
        BotCommand("liquidate", "전량 매도 후 종료"),
    ])
    notifier.set_bot(application.bot)

    chat_id = utils.load_chat_id()
    if not chat_id:
        log.info("등록된 채팅이 없습니다. 텔레그램에서 /start 를 보내 주세요.")
        return
    from scheduler import check_missed_settlement, register_schedules
    register_schedules(application.job_queue, chat_id)
    log.info("Registered schedules for chat_id=%s", chat_id)

    def _log_done(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log.error("check_missed_settlement failed: %s", exc, exc_info=True)

    task = asyncio.create_task(check_missed_settlement(application))
    task.add_done_callback(_log_done)
