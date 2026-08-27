"""
텔레그램 봇: 커맨드/콜백 핸들러 및 메뉴.
콜백 액션은 CALLBACK_HANDLERS 매핑으로 디스패치합니다.
"""
from __future__ import annotations

import asyncio
import math
import os
import io
import traceback
import datetime
import pytz
from dateutil import parser
import pandas as pd
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, BotCommand
from telegram.constants import MessageEntityType
from telegram.ext import ContextTypes

import kis_client
import strategy
import utils
import state_manager
import guide_theory
from state_manager import STRATEGY, CURRENT_CASH
from bot_logging import get_logger, read_recent_log_text
from notification_service import notifier
from ui_formatter import UIFormatter
from trade_service import TradeService

log = get_logger(__name__)


def _v30_v40_hardcoded_profit(ticker: str):
    """V3.0/V4.0 하드코딩 수익률: TQQQ=15%, SOXL=20%, 그 외=None(사용자 입력 필요)."""
    t = ticker.upper()
    if t == "TQQQ":
        return 15.0
    if t == "SOXL":
        return 20.0
    return None


def _runtime_strategy_entry(ticker: str, sv: dict, data=None) -> dict:
    """trading_settings 종목 dict → STRATEGY 런타임 엔트리 (method·one_time_usd 유지 규칙 동일)."""
    ot = sv.get("one_time_usd")
    try:
        one_time_usd = float(ot) if ot is not None and str(ot).strip() != "" else None
    except (TypeError, ValueError):
        one_time_usd = None
    if data is None:
        data = STRATEGY.get(ticker, {}).get("data", {"avg": 0.0, "qty": 0.0, "cumul": 0.0})
    return {
        "seed": float(sv["seed"]),
        "total_a": float(sv["total_a"]),
        "target_profit": float(sv["target_profit"]),
        "method": state_manager.normalize_strategy_method(sv.get("method")),
        "one_time_usd": one_time_usd,
        "data": data,
    }


# 매뉴얼 가이드 타입별 설명 (guide 콜백용)
GUIDE_TEXTS = {
    "status": (
        "🔍 **Status(현황보기) 딥다이브**\n\n"
        "• **입력 유입**: KIS 실시간 API를 통해 종목별 평단가, 보유 수량, 현재 가격을 수집합니다.\n"
        "• **내부 계산**: 사용자가 설정한 분할 횟수와 현재 평단을 대조하여 '별 포인트'와 '탈출 포인트'를 연산합니다.\n"
        "• **교차 검증**: 이론값만 보여주는 것이 아니라, 증권사 서버의 '미체결 주문 리포트'를 직접 읽어와 실제 주문이 들어갔는지 0.1초 단위로 비교합니다.\n"
        "• **결과물**: [현재 수익률], [이론상 매수/매도 계획], [실제 증권사 대기 주문]을 하나로 합친 통합 리포트를 제공합니다."
    ),
    "cash": (
        "💰 **Cash & Report 딥다이브**\n\n"
        "• **입력 유입**: 계좌 내 외화/원화 현고와 한국투자증권 고시 환율 정보를 가져옵니다.\n"
        "• **내부 계산**: 환율을 적용하여 전체 자산을 '달러($)' 기준으로 통합 환산하고, `cumul_profit.json` 파일에 저장된 과거 익절 기록을 합산합니다.\n"
        "• **결과물**: 실시간 가용 자산 현황과 지금까지 누적된 확정 수익금을 원화와 달러로 동시에 리포트합니다."
    ),
    "sync": (
        "🔄 **Sync(계좌 동기화) 딥다이브**\n\n"
        "• **입력 유입**: `trading_settings.json` 설정값과 증권사 잔고 데이터를 다시 읽어옵니다.\n"
        "• **내부 계산**: 보유수량/평단/예수금을 최신화하고, 오늘의 매매 계획 계산이 정확해지도록 상태를 갱신합니다.\n"
        "• **결과물**: 동기화 완료 메시지만 반환합니다. 주문 전송은 `/manual_order` 또는 자동 스케줄에 의해 수행됩니다."
    ),
    "manual_order": (
        "🧾 **Manual Order(수동 주문) 딥다이브**\n\n"
        "• **1단계**: 먼저 `/sync`와 동일하게 계좌 동기화를 수행합니다.\n"
        "• **2단계**: 증권사 서버의 미체결 주문을 조회해 '이미 주문이 있는지'를 종목별로 보여줍니다.\n"
        "• **3단계**: 사용자가 종목을 선택하면, 무한매수 방법론에 따른 '오늘의 LOC 주문'을 전송합니다.\n"
        "• **주의**: 이미 오늘 주문을 넣은 종목에 추가 주문을 넣으면 과매수/중복 주문이 될 수 있어, 실행 전 현황을 반드시 확인해야 합니다."
    ),
    "setup": (
        "📝 **Add & Edit(설정관리) 딥다이브**\n\n"
        "• **입력 유입**: 사용자의 텍스트 명령어(티커, 시드, 횟수 등)를 파싱합니다.\n"
        "• **내부 계산**: 입력받은 '전체 시드'를 '분할 횟수'로 나누어 1회 기본 매수금을 정하고, 목표 수익률에 따른 하락장 대응 로직(LIFO 매도 등)을 설정 파일에 반영합니다.\n"
        "• **결과물**: 봇의 감시 목록에 해당 종목이 추가되며, 즉시 첫 번째 동기화 작업을 수행하여 감시 사이클을 가동합니다."
    ),
    "sell": (
        "🚨 **Liquidate & 사이클 종료 딥다이브**\n\n"
        "• **입력 유입**: 긴급 전량 매도 신호 또는 목표가 도달 신호를 받습니다.\n"
        "• **내부 계산**: 일반적인 지정가가 아닌 'MOC(Market on Close - 장마감 시장가)' 주문을 활용하여 반드시 오늘 내로 체결되도록 신뢰도를 높입니다.\n"
        "• **결과물**: 모든 물량이 청산되면 투자 사이클이 종료되었음을 알리고, 해당 기간의 S&P 500 상승분과 내 수익을 정밀 비교하는 '종합 성적표'를 발송합니다."
    ),
    "theory_22": guide_theory.THEORY_22,
    "theory_30": guide_theory.THEORY_30,
    "theory_40": guide_theory.THEORY_40,
    "theory_compare": guide_theory.THEORY_COMPARE,
}


def _message_has_bot_command(message) -> bool:
    if not message or not getattr(message, "text", None):
        return False
    for ent in message.entities or ():
        if ent.type == MessageEntityType.BOT_COMMAND:
            return True
    return False


def _build_cmd_cash_message():
    """KIS 예수금·보유 조회(동기). 성공 시 (msg, None), 실패 시 (None, err_str)."""
    try:
        token = kis_client.get_kis_access_token()
        usd_cash, cash_source = kis_client.query_available_cash(token)
        data = kis_client.query_account_raw() or {}
        rt_cd = data.get("rt_cd", "N/A")

        msg = f"[예수금 조회]\n"
        msg += f"달러 예수금: ${usd_cash:,.2f}\n"

        krw_cash = kis_client.query_krw_cash(token)
        msg += f"원화 예수금: ₩{int(krw_cash):,}원\n"

        rate, ref_time_raw = kis_client.get_exchange_rate()

        try:
            dt_obj = parser.parse(ref_time_raw)
            kst_str = dt_obj.strftime("%m-%d %H:%M")
            ny_tz = pytz.timezone("America/New_York")
            ny_str = dt_obj.astimezone(ny_tz).strftime("%m-%d %H:%M")
            ref_time_display = f"{kst_str} KST / {ny_str} NY"
        except Exception:
            ref_time_display = ref_time_raw

        msg += f"--------------------------\n"
        msg += f"실시간 환율: {rate:,.2f}원 ({ref_time_display})\n"

        holdings = data.get("output1", []) if rt_cd == "0" else []
        all_stock_evl = 0.0
        if holdings:
            msg += "\n[보유 종목]\n"
            for item in holdings:
                sym = item.get("ovrs_pdno", "?").strip()
                qty_str = item.get("ovrs_cblc_qty", "0")
                avg = item.get("pchs_avg_pric", "0")
                qty_f = float(qty_str)
                avg_f = float(avg)
                now_f = kis_client.get_current_price(sym)
                pnl = now_f * qty_f - avg_f * qty_f
                pnl_rt = ((now_f - avg_f) / avg_f * 100) if avg_f > 0 else 0.0
                all_stock_evl += now_f * qty_f
                in_strategy = " *" if sym in STRATEGY else ""
                msg += f"  {sym}{in_strategy}: {qty_str}주 / 현재${now_f:,.2f} / 손익${pnl:,.2f}({pnl_rt:.1f}%)\n"

        msg += f"\n현금: ${usd_cash:,.2f} / 주식: ${all_stock_evl:,.2f}\n"
        return msg, None
    except Exception as e:
        return None, str(e)


def _build_manual_order_summary_text():
    summary = "[수동 주문] 현재 주문/잠금 현황 (미국 동부 당일 미체만)\n"
    summary += "--------------------------\n"
    locked_tickers = []
    for ticker in STRATEGY:
        locked = utils.check_trade_lock(ticker)
        if locked:
            locked_tickers.append(ticker)
        unfilled = []
        try:
            unfilled = kis_client.query_unfilled_orders_today_us(ticker)
        except Exception:
            unfilled = []
        try:
            locked_by_broker = bool(unfilled)
        except Exception:
            locked_by_broker = False
        if locked_by_broker and not locked:
            locked = True
            locked_tickers.append(ticker)
            try:
                utils.set_trade_lock(ticker)
            except Exception:
                pass
        lock_tag = "LOCKED" if locked else "OPEN"
        if unfilled:
            active_cnt = sum(1 for u in unfilled if u.get("status") == "대기")
            summary += f"• {ticker} [{lock_tag}]: 당일 미체결 {len(unfilled)}건 (대기 {active_cnt}건)\n"
        else:
            summary += f"• {ticker} [{lock_tag}]: 당일 미체결 없음\n"

    if locked_tickers:
        summary += "\n오늘 주문 전송됨(LOCKED) 종목: " + ", ".join(sorted(set(locked_tickers))) + "\n"
        summary += "  └ (수동 주문은 가능하지만, 중복/과매수 위험이 있어 주의가 필요합니다.)\n"

    summary += "\n주의(추가매수/추가주문)\n"
    summary += "- 이미 오늘 주문을 전송한 종목에 대해 추가로 주문을 넣으면, 전략 의도와 다르게 과매수/중복 주문이 될 수 있습니다.\n"
    summary += "- 반드시 /status에서 최근 주문·체결 내역을 확인한 뒤 진행하세요.\n"
    return summary


def main_reply_keyboard_markup() -> ReplyKeyboardMarkup:
    """상설 메뉴 키보드 (/start·알 수 없는 입력 안내 등에서 공통 사용)."""
    rows = [
        ["📊 현황보기(/status)", "💰 예수금확인(/cash)"],
        ["📈 수익보고(/report)"],
        ["신규동기화(/sync)", "🧾 수동주문(/manual_order)"],
        ["📚 도움말(/help)", "📋 수익기록(/profit_history)"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    utils.save_chat_id(chat_id)
    
    from scheduler import register_schedules
    register_schedules(context.job_queue, chat_id)
    
    markup = main_reply_keyboard_markup()
    
    welcome_msg = (
        "🚀 **무한매수봇 2.0 - 당신의 개인 투자 비서**\n\n"
        "봇이 정상적으로 가동되었습니다! 설정하신 전략에 따라 매일 밤 자동으로 매매를 수행합니다.\n\n"
        "⏰ **핵심 자동화 스케줄 (한국 시간 기준)**\n"
        "• **22:35 / 23:35**: 정규장 오픈 및 일일 주문 전송\n"
        "• **05:00 / 06:00**: 정규장 마감 알림\n"
        "• **장 마감 후(미국 동부, `trading_settings.json`의 `POST_CLOSE_SYNC_NY`, 기본 16:40)**: 계좌 자동 동기화(`/sync`와 동일)\n"
        "• **08:00 (화~토, KST)**: 종합 정산 리포트 및 수익 보고 (일·월 아침은 생략)\n\n"
        "💡 아래 메뉴 버튼을 누르면 타이핑 없이 주요 기능을 즉시 실행할 수 있습니다. 자세한 사용법은 `/help`를 확인해주세요!\n"
        "🛠 오류·스케줄 이상 시 서버 기록은 `/logs` (최근 24시간)로 확인할 수 있습니다."
    )
    await update.message.reply_text(welcome_msg, reply_markup=markup)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Telegram Markdown은 '_' 등에서 엔티티 파싱 오류가 잦아 help는 HTML로 고정합니다.
    help_text = (
        "📚 <b>[무한매수봇 도움말]</b>\n\n"
        "<b>핵심 흐름</b>\n"
        "- <b>자동 운용</b>: 매일 정규장 오픈 시점에 전략에 따른 주문(LOC 중심)을 자동 전송하고, 다음날 아침 리포트로 체결을 요약합니다.\n"
        "- <b>동기화</b>(<code>/sync</code>): 내 계좌의 <b>잔고/평단/현금</b>을 봇 상태와 맞춥니다. (입금/설정변경 후 권장)\n"
        "- <b>수동주문</b>(<code>/manual_order</code>): 먼저 <b>이미 들어간 주문/미체결 여부</b>를 보여준 뒤, 종목을 선택해 <b>오늘의 LOC 주문</b>을 넣습니다.\n\n"
        "<b>투자 사이클 예시</b>\n"
        "- Day 1: <code>/add_target</code>로 종목 추가 → 자동/수동주문으로 첫 진입\n"
        "- Day N: 하락 시 분할 매수로 평단 조정 (전략 파라미터 기반)\n"
        "- Day M: 목표 수익률 도달 시 전량 익절 매도 → 사이클 종료 보고\n\n"
        "<b>명령어</b>\n"
        "- <b>조회</b>: <code>/status</code>(현황+최근 주문·체결), <code>/cash</code>(예수금/보유), <code>/report</code>(누적 수익), <code>/profit_history</code>(익절 기록), <code>/logs</code>(최근 24시간 서버 로그)\n"
        "- <b>제어</b>: <code>/sync</code>(계좌 동기화), <code>/manual_order</code>(수동 주문)\n"
        "- <b>설정</b>: <code>/add_target</code>(추가), <code>/edit_target</code>(수정), <code>/del_target</code>(삭제)\n"
        "- <b>긴급</b>: <code>/liquidate</code>(전량 매도 및 종료)\n\n"
        "<b>무한매수법 3.0</b>\n"
        "- <code>/add_target 티커 시드 분할 목표% 3.0</code> 처럼 마지막에 <code>3.0</code>을 주면 해당 종목만 V3.0 규칙(별%/쿼터, LOC 매도=별%가 등)을 씁니다. 생략 시 2.2.\n"
        "- 1회 매수금을 시드÷분할이 아니라 고정 달러로 쓰려면 <code>trading_settings.json</code>의 해당 종목에 <code>one_time_usd</code>를 넣으세요.\n"
        "- <b>V3.0 T·회차</b>: 실매수 누적 USD(<code>v3_buy_state.json</code>)÷1회 기준입니다. <b>2.2</b>는 잔고(평단×수량)÷1회 근사입니다.\n\n"
        "<b>무한매수법 4.0</b>\n"
        "- 마지막 인자에 <code>4.0</code>: 일반모드(잔금÷(N-T) 1회매수, 별지점 매수-0.01/매도, 쿼터·매수·리버스 T 갱신은 <b>체결 누적</b> 기준) 및 소진 후 리버스(첫날 MOC 분할, 이후 5일종가 별지점).\n"
        "- T는 <code>v4_strategy_state.json</code>에 저장됩니다(최초는 잔고 기반 추정). MOC 등 장 마감 체결은 pending 후 <code>/sync</code>에서 재확인합니다.\n\n"
        "💡 아래 버튼에서 기능 원리·<b>버전별 이론(상세)</b>·<b>버전 비교·시장별 성향</b>을 볼 수 있습니다."
    )

    keyboard = [
        [InlineKeyboardButton("📊 Status 원리", callback_data="guide:status"),
         InlineKeyboardButton("💰 Cash/Report 원리", callback_data="guide:cash")],
        [InlineKeyboardButton("🔄 Sync 원리", callback_data="guide:sync"),
         InlineKeyboardButton("🧾 Manual Order 원리", callback_data="guide:manual_order")],
        [InlineKeyboardButton("📝 설정/추가 원리", callback_data="guide:setup"),
         InlineKeyboardButton("🚨 특별매도 원리", callback_data="guide:sell")],
        [
            InlineKeyboardButton("이론V2.2", callback_data="guide:theory_22"),
            InlineKeyboardButton("이론V3.0", callback_data="guide:theory_30"),
            InlineKeyboardButton("이론V4.0", callback_data="guide:theory_40"),
        ],
        [InlineKeyboardButton("버전 비교·시장별 유리", callback_data="guide:theory_compare")],
    ]
    await update.message.reply_text(help_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """등록 채팅 전용. 최근 24시간(KST) 로그 — 서버에는 30일 롤링 저장."""
    cid = utils.load_chat_id()
    if not cid:
        await update.message.reply_text("먼저 /start 로 이 채팅을 등록한 뒤 /logs 를 사용할 수 있습니다.")
        return
    if update.effective_chat.id != cid:
        await update.message.reply_text("이 명령은 봇에 등록된 채팅에서만 사용할 수 있습니다.")
        return

    await update.message.reply_text("최근 24시간 로그를 불러오는 중…")
    preview, prev_trunc = read_recent_log_text(hours=24, max_total_chars=3500)
    full, full_trunc = read_recent_log_text(hours=24, max_total_chars=400_000)

    head = (
        "최근 24시간 로그 (KST)\n"
        "서버: logs/bot.log · 자정마다 회전 · 최대 30일 보관\n"
        + "━" * 22
        + "\n\n"
    )
    text = head + preview
    if prev_trunc:
        text += "\n\n(미리보기는 뒷부분 위주입니다. 파일에 전체가 담깁니다.)"
    await update.message.reply_text(text)

    if len(full) > 3200 or prev_trunc or full_trunc:
        bio = io.BytesIO(full.encode("utf-8"))
        bio.name = "ubs_bot_log_24h.txt"
        cap = "최근 24시간 로그 전문"
        if full_trunc:
            cap += " (40만자 초과분 제외)"
        await update.message.reply_document(document=bio, caption=cap)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("실시간 시세 및 실제 주문 내역을 조회 중입니다...")
    
    from scheduler import generate_combined_report
    msg = await generate_combined_report(is_full=False)
    
    keyboard = []
    for ticker in STRATEGY:
        keyboard.append([InlineKeyboardButton(f"[상세 지시서] {ticker}", callback_data=f"status_dive:{ticker}")])
        
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_cash(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text("주문가능금액 조회 중...")
    msg, err = await asyncio.to_thread(_build_cmd_cash_message)
    if err:
        await update.message.reply_text(f"조회 오류: {err}")
        return
    await update.message.reply_text(msg)

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_wait = await update.message.reply_text("계좌 정보를 최신화하는 중입니다... (잔고, 평단가, 예수금을 동기화합니다.)")
    pre_avgs = await asyncio.to_thread(TradeService.capture_pre_sync_avgs)
    success, res_msg = await asyncio.to_thread(state_manager.sync_account)
    if not success:
        await msg_wait.edit_text(f"[동기화 실패] {res_msg}")
        return

    sync_done = (
        "[동기화 완료] 모든 종목의 최신 정보가 반영되었습니다.\n\n"
        "오늘의 LOC 주문을 수동으로 넣으려면 /manual_order를 실행해 주세요.\n"
        "(먼저 현재 주문/미체결을 보여주고, 그 다음 종목 선택 -> 주문 전송 순서로 진행합니다.)"
    )
    await msg_wait.edit_text(sync_done)

    if pre_avgs:
        try:
            profit_msgs = await asyncio.to_thread(
                TradeService.reconcile_daily_sell_profits, pre_avgs
            )
            if profit_msgs:
                await update.message.reply_text(
                    "[동기화·매도실현 보정]\n\n" + "\n".join(profit_msgs)
                )
        except Exception as e:
            log.warning("cmd_sync reconcile failed: %s", e, exc_info=True)


async def cmd_manual_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    수동 주문 플로우:
    1) 동기화(잔고/평단/현금) 수행
    2) 현재 미체결 주문 및 잠금 상태를 보여줌
    3) 사용자가 종목을 선택하면 무한매수 방법론에 따른 '오늘의 LOC 주문' 전송
       - (주의) 기존에는 reset으로 잠금을 해제했으나, 이제는 manual_order에서 안내 후 수동 주문을 허용합니다.
    """
    msg_wait = await update.message.reply_text(
        "수동 주문 준비 중...\n(1/2) 계좌 동기화 및 주문 상태를 확인합니다.",
    )
    success, res_msg = await asyncio.to_thread(state_manager.sync_account)
    if not success:
        await msg_wait.edit_text(f"[동기화 실패] {res_msg}")
        return

    summary = await asyncio.to_thread(_build_manual_order_summary_text)
    await msg_wait.edit_text(summary)

    # 2) 종목 선택 버튼 (기존 콜백 주문 로직 재사용)
    all_infos = {}
    for ticker in STRATEGY:
        all_infos[ticker] = strategy.build_order_info(ticker)

    keyboard = []
    for ticker, info in all_infos.items():
        # (선택 전 검증 강화) 미체결 상세 보기 버튼 제공
        keyboard.append(
            [
                InlineKeyboardButton(f"🔎 {ticker} 미체결 상세", callback_data=f"m_unfilled:{ticker}"),
            ]
        )
        if info["qty_total"] == 0:
            if info.get("b1", 0) > 0:
                cb = f"m_entryask:{ticker}"
                keyboard.append([InlineKeyboardButton(
                    f"🚀 {ticker} 새 사이클 LOC 진입 ({info['b1']}주 @ ${info['avg_price']:.2f})",
                    callback_data=cb,
                )])
            else:
                try:
                    _excg = kis_client.get_price_exchange_code(ticker)
                except Exception:
                    _excg = "?"
                keyboard.append([InlineKeyboardButton(
                    f"⚠️ {ticker} 시세조회 실패 (EXCD={_excg}) - 진입 불가", callback_data="noop"
                )])
        else:
            keyboard.append(
                [InlineKeyboardButton(f"🏦 {ticker} 오늘 LOC 매매 주문", callback_data=f"m_all:{ticker}")]
            )

    await update.message.reply_text(
        "(2/2) 먼저 미체결 상세를 확인한 뒤, 주문할 종목을 선택하세요.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = utils.load_cumul_profit()
    by_t = utils.aggregate_cumul_profit_by_ticker(cp)

    rows: list[tuple[str, float]] = []
    seen: set[str] = set()
    for t in STRATEGY:
        tu = str(t).strip().upper()
        rows.append((tu, float(by_t.get(tu, 0.0))))
        seen.add(tu)
    for tu in sorted(by_t.keys()):
        if tu not in seen:
            rows.append((tu, float(by_t[tu])))
            seen.add(tu)

    msg = "[무한매수 실현수익 보고서]\n"
    total = 0.0
    for ticker, profit in rows:
        msg += f"  {ticker} 누적: ${profit:,.2f}\n"
        total += profit

    msg += "  ------------------\n"
    msg += f"  총 합계: ${total:,.2f}\n"
    await update.message.reply_text(msg)


async def cmd_profit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    utils.save_cumul_profit({"history": [], "processed_orders": []})
    await update.message.reply_text("누적 익절 수익 기록이 초기화되었습니다.")

async def cmd_profit_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = utils.load_cumul_profit()
    history = [h for h in (cp.get("history") or []) if isinstance(h, dict)]
    by_ticker = utils.aggregate_cumul_profit_by_ticker(cp)

    if not history:
        if any(abs(v) > 1e-9 for v in by_ticker.values()):
            lines = "\n".join(f"💰 {t}: ${v:,.2f}" for t, v in sorted(by_ticker.items()) if abs(v) > 1e-9)
            await update.message.reply_text(
                "📋 건별 익절 이력(history)은 없습니다.\n\n"
                f"다만 cumul_profit.json에 종목별 누적만 있는 상태입니다:\n\n{lines}\n\n"
                "· /report와 동일한 숫자입니다.\n"
                "· 이후 봇이 목표가 익절 체결을 기록하면 여기에 날짜·건당 내역이 쌓입니다.\n"
                "· 예전에 정규장 익절만 하고 기록 로직이 없던 때의 잔여일 수 있습니다."
            )
            return
        await update.message.reply_text(
            "📋 현재 기록된 실현 수익이 없습니다.\n\n"
            "목표가에 도달한 봇 익절 매도 가 체결 확인되면 history에 자동으로 쌓입니다. 첫 익절을 응원합니다! 🚀"
        )
        return

    totals: dict[str, float] = {}
    for h in history:
        tx = str(h.get("ticker") or "").strip().upper()
        if not tx:
            continue
        try:
            totals[tx] = totals.get(tx, 0.0) + float(h.get("profit") or 0)
        except (TypeError, ValueError):
            continue

    total_str = "\n".join(f"💰 {tx}: ${v:,.2f}" for tx, v in sorted(totals.items()))
    msg = (
        "📋 무한매수 실현 수익 요약 (history 기준)\n\n"
        f"{total_str}\n\n"
        "--- 최근 5건 (날짜 | 티커 | 버전 | 손익) ---\n"
    )
    for h in history[-5:]:
        vlab = strategy.format_method_display(h.get("method"))
        d = h.get("date", "?")
        tic = str(h.get("ticker") or "?").strip().upper()
        try:
            val_p = float(h.get("profit") or 0)
        except (TypeError, ValueError):
            val_p = 0.0
        msg += f"✅ {d} | {tic} | {vlab} | **${val_p:+,.2f}**\n"

    keyboard = [[InlineKeyboardButton("🔍 전체 수익 기록 상세보기 (Deep-Dive)", callback_data="profit_dive")]]
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))



async def cmd_add_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        msg = (
            "➕ **종목 추가(Add Target) 가이드**\n\n"
            "인자 없이 호출하면 이 안내만 표시됩니다. "
            "RSI·차트는 증권사 앱/MTS나 별도 시세 도구에서 확인하세요.\n\n"
            "**[참고] 흔히 쓰는 3배 레버리지 예시 티커**\n"
            "`TQQQ` `UPRO` `SOXL` `TECL` `LABU` `NAIL` `DPST` … (투자 판단은 본인 책임)\n\n"
            "**[방법론별 기본 익절목표 안내]**\n"
            "• V2.2: 사용자 입력 적용 (기본 10%)\n"
            "• V3.0·V4.0: TQQQ 15%, SOXL 20% (수익률 생략 가능)\n\n"
            "**[사용법]**\n"
            "1. 전량 입력: `/add_target 티커 시드 횟수 수익% 버전`\n"
            "2. 수익 생략(TQQQ/SOXL): `/add_target 티커 시드 횟수 버전` (자동 15/20%)\n"
            "3. 버전 생략: `/add_target 티커 시드 횟수 수익%` (V2.2)\n"
            "4. 모두 생략: `/add_target 티커 시드 횟수` (10% / V2.2)\n\n"
            "예: `/add_target TQQQ 3000 20 4.0` (V4.0, 15% 자동)"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return
    
    ticker = args[0].upper()
    try:
        seed = float(args[1])
        total_a = float(args[2])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ 시드와 분할횟수는 숫자여야 합니다.")
        return

    # 지능적 인자 파싱 (add_target)
    target_p = 10.0
    method = "2.2"

    if len(args) == 4:
        val = args[3]
        if val.lower() in ("3.0", "3", "v3", "v3.0", "4.0", "4", "v4", "v4.0"):
            method = state_manager.normalize_strategy_method(val)
            if ticker == "TQQQ": target_p = 15.0
            elif ticker == "SOXL": target_p = 20.0
        else:
            try:
                target_p = float(val)
            except ValueError:
                pass
    elif len(args) >= 5:
        try:
            target_p = float(args[3])
        except ValueError:
            pass
        method = state_manager.normalize_strategy_method(args[4])
        # V3.0/V4.0: TQQQ/SOXL은 하드코딩 수익률 자동 적용 (사용자 입력 무시)
        if method in ("3.0", "4.0"):
            hp = _v30_v40_hardcoded_profit(ticker)
            if hp is not None:
                target_p = hp
    else:
        if ticker == "TQQQ": target_p = 15.0
        elif ticker == "SOXL": target_p = 20.0

    from config_manager import STRATEGY_SETTINGS, save_config
    new_settings = dict(STRATEGY_SETTINGS)
    prev = STRATEGY_SETTINGS.get(ticker, {})
    entry = {**prev, "seed": seed, "total_a": total_a, "target_profit": target_p, "method": method}
    new_settings[ticker] = entry

    if save_config({"STRATEGY_SETTINGS": new_settings}):
        STRATEGY[ticker] = _runtime_strategy_entry(ticker, entry)
        m = STRATEGY[ticker]["method"]
        effective_note = ""
        if m in ("3.0", "4.0"):
            if ticker == "TQQQ":
                effective_note = f"\n⚠️ V{m} 적용: TQQQ 익절목표는 15%(고정)로 운용됩니다."
            elif ticker == "SOXL":
                effective_note = f"\n⚠️ V{m} 적용: SOXL 익절목표는 20%(고정)로 운용됩니다."
        
        await update.message.reply_text(
            f"✅ {ticker} 설정 완료!\n- 시드: ${seed}\n- 횟수: {total_a}회\n- 목표: {target_p}%\n- 방식: {m}{effective_note}"
        )
    else:
        await update.message.reply_text("❌ 설정 저장 실패")

async def cmd_edit_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3 and len(args) != 0:
        await update.message.reply_text("❌ 사용법: `/edit_target 티커 시드 횟수 [수익%] [버젼]`")
        return

    if len(args) == 0:
        from state_manager import STRATEGY
        if not STRATEGY:
            await update.message.reply_text("현재 투자 중인 종목이 없습니다.")
            return

        msg = (
            "📝 <b>수정할 종목을 선택하세요</b>\n\n"
            "선택 후에는 단계별로 값을 입력합니다. (시드 → 분할횟수 → 목표수익률)\n"
            "또는 한 줄로도 입력할 수 있습니다.\n"
            "<code>시드 분할횟수 [수익%] [버전]</code>\n\n"
            "<b>[방법론별 기본 익절목표 참고]</b>\n"
            "• V2.2: 사용자 입력 적용 (기본 10%)\n"
            "• V3.0·V4.0: TQQQ 15%, SOXL 20% (수익률 생략 가능)\n\n"
            "취소하려면 <code>취소</code> 라고 입력하세요."
        )
        keyboard = []
        for t in STRATEGY.keys():
            keyboard.append([InlineKeyboardButton(f"📝 {t} 수정", callback_data=f"edit_pick:{t}")])
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    ticker = args[0].upper()
    from state_manager import STRATEGY
    if ticker not in STRATEGY:
        await update.message.reply_text(f"❌ {ticker} 종목은 현재 투자 대상이 아닙니다.")
        return
        
    try:
        seed = float(args[1])
        total_a = float(args[2])
    except (IndexError, ValueError):
        await update.message.reply_text("❌ 시드와 분할횟수는 숫자여야 합니다.")
        return

    # 지능적 인자 파싱 (edit_target)
    target_p = STRATEGY[ticker].get("target_profit", 10.0)
    method = STRATEGY[ticker].get("method", "2.2")

    if len(args) == 4:
        val = args[3]
        if val.lower() in ("3.0", "3", "v3", "v3.0", "4.0", "4", "v4", "v4.0"):
            method = state_manager.normalize_strategy_method(val)
            if ticker == "TQQQ": target_p = 15.0
            elif ticker == "SOXL": target_p = 20.0
        else:
            try:
                target_p = float(val)
            except ValueError:
                pass
    elif len(args) >= 5:
        try:
            target_p = float(args[3])
        except ValueError:
            pass
        method = state_manager.normalize_strategy_method(args[4])
        # V3.0/V4.0: TQQQ/SOXL은 하드코딩 수익률 자동 적용 (사용자 입력 무시)
        if method in ("3.0", "4.0"):
            hp = _v30_v40_hardcoded_profit(ticker)
            if hp is not None:
                target_p = hp

    from config_manager import STRATEGY_SETTINGS, save_config
    new_settings = dict(STRATEGY_SETTINGS)
    merged = {**STRATEGY_SETTINGS.get(ticker, {}), "seed": seed, "total_a": total_a, "target_profit": target_p, "method": method}
    new_settings[ticker] = merged

    if save_config({"STRATEGY_SETTINGS": new_settings}):
        STRATEGY[ticker] = _runtime_strategy_entry(ticker, merged)
        m = STRATEGY[ticker]["method"]
        effective_note = ""
        if m in ("3.0", "4.0"):
            if ticker == "TQQQ":
                effective_note = f"\n⚠️ V{m} 적용: TQQQ 익절목표는 15%(고정)로 운용됩니다."
            elif ticker == "SOXL":
                effective_note = f"\n⚠️ V{m} 적용: SOXL 익절목표는 20%(고정)로 운용됩니다."
        
        await update.message.reply_text(
            f"✅ {ticker} 설정 변경 완료!\n- 시드: ${seed:,.0f}\n- 분할횟수: {total_a}회\n- 목표수익률: {target_p}%\n- 방식: {m}{effective_note}"
        )
    else:
        await update.message.reply_text("❌ 설정 저장 실패")

async def cmd_del_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config_manager import STRATEGY_SETTINGS
    args = context.args
    
    if not args:
        if not STRATEGY_SETTINGS:
            await update.message.reply_text("현재 등록된 종목이 없습니다.")
            return
            
        keyboard = []
        for ticker in STRATEGY_SETTINGS:
            keyboard.append([InlineKeyboardButton(f"🗑️ {ticker} 삭제", callback_data=f"ask_del:{ticker}")])
            
        markup = InlineKeyboardMarkup(keyboard)
        msg = (
            "🗑️ **삭제할 종목을 선택하세요**\n\n"
            "**주의사항:**\n"
            "- 이 작업은 봇의 **감시 목록에서 해당 종목을 제외**합니다.\n"
            "- 실제 증권 계좌에 있는 **주식은 매도되지 않습니다.**\n"
            "- 봇이 더 이상 해당 종목을 관리하지 않게 됩니다."
        )
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=markup)
        return
    
    ticker = args[0].upper()
    await _execute_del_target(update, ticker)

async def _execute_del_target(event, ticker):
    from config_manager import STRATEGY_SETTINGS, save_config
    if ticker not in STRATEGY_SETTINGS:
        msg = f"대상 {ticker}를 찾을 수 없습니다."
        if hasattr(event, 'reply_text'): await event.reply_text(msg)
        else: await event.edit_message_text(msg)
        return
    
    new_settings = dict(STRATEGY_SETTINGS)
    del new_settings[ticker]
    
    if save_config({"STRATEGY_SETTINGS": new_settings}):
        from state_manager import STRATEGY
        if ticker in STRATEGY: del STRATEGY[ticker]
        
        # 모든 상태 파일 정리 (cumul_profit history 보존 포함)
        utils.delete_ticker_from_all_states(ticker)
        
        msg = f"🗑️ **{ticker} 삭제 완료!**\n\n이제 봇이 {ticker} 종목을 더 이상 감시하거나 매매하지 않습니다."
        if hasattr(event, 'reply_text'): await event.reply_text(msg, parse_mode='Markdown')
        else: await event.edit_message_text(msg, parse_mode='Markdown')
    else:
        msg = "❌ 설정 저장 실패"
        if hasattr(event, 'reply_text'): await event.reply_text(msg)
        else: await event.edit_message_text(msg)


async def cmd_liquidate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from state_manager import STRATEGY
    args = context.args
    
    if not args:
        # 보유 중인 종목 리스트 추출
        liquid_targets = [t for t in STRATEGY if STRATEGY[t]["data"]["qty"] > 0]
        if not liquid_targets:
            await update.message.reply_text("보유 수량이 있는 종목이 없습니다.")
            return
            
        keyboard = []
        for ticker in liquid_targets:
            keyboard.append([InlineKeyboardButton(f"🚨 {ticker} 전량 매도", callback_data=f"ask_liq:{ticker}")])
            
        markup = InlineKeyboardMarkup(keyboard)
        msg = (
            "🚨 **전량 매매 및 종료(Liquidate) 안내**\n\n"
            "이 명령은 다음 작업을 수행합니다:\n"
            "1. 해당 종목의 **모든 잔고를 시장가(MOC)로 즉시 매도**합니다.\n"
            "2. 봇의 투자 대상 목록에서 **해당 종목 설정을 삭제**합니다.\n"
            "3. 즉, 해당 종목에 대한 **무한매수 투자를 완전히 종료**합니다.\n\n"
            "매도할 종목을 선택해 주세요:"
        )
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=markup)
        return
    
    ticker = args[0].upper()
    await _execute_liquidate(update, ticker)

async def _execute_liquidate(event, ticker, ord_type="02"):
    from state_manager import STRATEGY
    if ticker not in STRATEGY:
        msg = "해당 종목을 찾을 수 없습니다."
        if hasattr(event, 'reply_text'): await event.reply_text(msg)
        else: await event.edit_message_text(msg)
        return
    
    qty = STRATEGY[ticker]["data"]["qty"]
    if qty <= 0:
        msg = "보유 수량이 없습니다."
        if hasattr(event, 'reply_text'): await event.reply_text(msg)
        else: await event.edit_message_text(msg)
        return
        
    ord_type_name = "시장가(MOC)" if ord_type == "02" else "지정가"
    curr_p = 0
    if ord_type == "00":
        curr_p = await asyncio.to_thread(kis_client.get_current_price, ticker)
        
    status_msg = f"🚨 {ticker} {qty}주를 전량 {ord_type_name} 매도 주문 처리합니다..."
    if hasattr(event, 'reply_text'): await event.reply_text(status_msg)
    else: await event.edit_message_text(status_msg)
    
    from kis_client import send_order
    res = await asyncio.to_thread(send_order, ticker, int(qty), curr_p, "SELL", ord_type)
    
    if res.get('rt_cd') == '0':
        # [수정] 즉시 삭제하지 않고 liquidating 플래그만 설정
        # → 장 마감 후 post_close_sync_job에서 체결 확인 및 수익 기록이 완료된 뒤 삭제됩니다.
        STRATEGY[ticker]["liquidating"] = True
        utils.set_trade_lock(ticker)
        final_msg = (
            f"✅ **{ticker} 전량 매도 주문 전송 완료!**\n\n"
            f"- {qty}주 {ord_type_name} 매도 주문 접수됨\n"
            f"- 장 마감 후 체결 확인 및 수익 기록이 자동 처리됩니다.\n"
            f"- 수익 반영 완료 후 포트폴리오 설정에서 자동 삭제됩니다."
        )
        if hasattr(event, 'reply_text'): await event.reply_text(final_msg, parse_mode='Markdown')
        else: await event.edit_message_text(final_msg, parse_mode='Markdown')
    else:
        error_msg = f"❌ 매도 실패: {res.get('msg1')}"
        if hasattr(event, 'reply_text'): await event.reply_text(error_msg)
        else: await event.edit_message_text(error_msg)


async def _cb_all(update, context, query, parts):
    ticker = parts[1]
    
    # 중복 주문 확인 (미체결 주문 존재 여부)
    try:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        active_cnt = sum(1 for u in unfilled if u.get("status") == "대기") if unfilled else 0
        if active_cnt > 0:
            msg = (
                f"❌ **{ticker} 수동주문 취소**\n\n"
                f"계좌에 이미 접수된 미체결 주문({active_cnt}건)이 존재합니다.\n"
                f"중복 주문을 방지하기 위해 주문을 실행하지 않았습니다."
            )
            await query.edit_message_text(msg, parse_mode="Markdown")
            return
    except Exception as e:
        log.warning("수동주문 미체결 확인 중 오류 발생: %s", e)

    info = strategy.build_order_info(ticker)
    res = await asyncio.to_thread(strategy.execute_all_order, ticker, info)
    await query.edit_message_text(res)


async def _cb_entry(update, context, query, parts):
    ticker = parts[1]

    # 중복 주문 확인 (미체결 주문 존재 여부)
    try:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        active_cnt = sum(1 for u in unfilled if u.get("status") == "대기") if unfilled else 0
        if active_cnt > 0:
            msg = (
                f"❌ **{ticker} 수동주문(진입) 취소**\n\n"
                f"계좌에 이미 접수된 미체결 주문({active_cnt}건)이 존재합니다.\n"
                f"중복 주문을 방지하기 위해 주문을 실행하지 않았습니다."
            )
            await query.edit_message_text(msg, parse_mode="Markdown")
            return
    except Exception as e:
        log.warning("수동주문 미체결 확인 중 오류 발생: %s", e)

    # 잔고 0주 진입은 모든 버전 공통으로 1회분 LOC 주문 정보를 strategy에서 재계산해 사용
    info = await asyncio.to_thread(strategy.build_order_info, ticker)
    if int(info.get("qty_total", 0) or 0) != 0:
        await query.edit_message_text(
            f"ℹ️ {ticker}는 현재 보유 수량이 0주가 아니어서 '새 사이클 진입' 경로를 사용할 수 없습니다.\n"
            "다시 /manual_order에서 '오늘 LOC 매매 주문'을 사용해 주세요."
        )
        return
    if int(info.get("b1", 0) or 0) <= 0 or float(info.get("avg_price", 0) or 0) <= 0:
        try:
            _excg = kis_client.get_price_exchange_code(ticker)
        except Exception:
            _excg = "?"
        await query.edit_message_text(
            f"❌ {ticker} 새 사이클 1회분 LOC 주문 정보를 만들지 못했습니다.\n"
            f"- 시세/기준가 확인 실패(EXCD={_excg})\n"
            "- 잠시 후 다시 시도하거나 /sync 후 재시도해 주세요."
        )
        return

    res = await asyncio.to_thread(strategy.execute_all_order, ticker, info)
    await query.edit_message_text(res)
    if "성공" in str(res):
        utils.set_trade_lock(ticker)


def _prepare_manual_entry_cycle_state(ticker: str) -> tuple[int, str]:
    """수동 새 사이클 진입 전 회차 상태를 보정합니다(중복 +1 방지)."""
    new_cycle, did_inc = utils.start_cycle_for_new_entry(
        ticker, increment_on_reentry=True
    )
    action = "증가" if did_inc else "유지(중복증가 방지)"
    return new_cycle, action


async def _cb_entryask(update, context, query, parts):
    ticker = parts[1] if len(parts) > 1 else ""
    if not ticker:
        await query.edit_message_text("티커 정보가 없어 주문을 진행할 수 없습니다.")
        return

    tl = utils.get_cycle_timeline(ticker)
    current_cycle = int(tl.get("current_cycle", 1) or 1)
    msg = (
        f"🧭 **{ticker} 새 사이클 수동 진입(잔고 0주)**\n\n"
        "주문 전 회차 처리를 자동 반영합니다.\n"
        f"- 현재 회차: {current_cycle}회차\n"
        f"- 이전 회차: 시작 {tl.get('previous_start', '-')} / 종료 {tl.get('previous_end', '-')}\n"
        f"- 현재 회차 시작일: {tl.get('current_start', '-')}\n"
        "- 재진입이면 회차 +1, 이미 반영된 상태면 유지(중복 증가 방지)\n"
        "- 주문은 모든 버전 공통으로 1회분 LOC 진입으로 전송됩니다."
    )
    keyboard = [
        [InlineKeyboardButton("새 사이클 진입(회차 자동 반영)", callback_data=f"m_entrycycle:{ticker}:auto")],
        [InlineKeyboardButton("취소", callback_data="cancel")],
    ]
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def _cb_entrycycle(update, context, query, parts):
    ticker = parts[1] if len(parts) > 1 else ""
    if not ticker:
        await query.edit_message_text("티커 정보가 없어 주문을 진행할 수 없습니다.")
        return

    info = await asyncio.to_thread(strategy.build_order_info, ticker)
    if int(info.get("qty_total", 0) or 0) != 0:
        await query.edit_message_text(
            f"ℹ️ {ticker}는 현재 보유 수량이 0주가 아니어서 새 사이클 진입을 건너뜁니다.\n"
            "다시 /manual_order에서 '오늘 LOC 매매 주문'을 사용해 주세요."
        )
        return
    if int(info.get("b1", 0) or 0) <= 0 or float(info.get("avg_price", 0) or 0) <= 0:
        s = STRATEGY.get(ticker, {})
        seed = float(s.get("seed", 0) or 0)
        total_a = float(s.get("total_a", 0) or 0)
        one_time = strategy.one_time_for_strategy(s) if s else 0.0
        curr = float(info.get("first_buy_curr_price", 0) or 0)
        need_split = int(seed // curr) if curr > 0 and seed > 0 else 0

        if curr > 0 and one_time > 0:
            msg = (
                f"⚠️ **{ticker} 진입 불가: 1회분 매수금 부족**\n\n"
                f"- 현재가: ${curr:.2f}\n"
                f"- 1회분 매수금: ${one_time:.2f} (시드 ${seed:.0f} / 분할 {int(total_a) if total_a > 0 else 0})\n"
                "- 계산 결과: `floor(1회분/현재가)=0주` 이므로 주문을 만들 수 없습니다.\n\n"
                "아래 중 하나를 선택해 진행하세요."
            )
            kbd = [
                [InlineKeyboardButton("① 분할/시드 설정 수정", callback_data=f"edit_pick:{ticker}")],
                [InlineKeyboardButton("③ 이번만 1주 강제 LOC 진입", callback_data=f"m_entryforce:{ticker}")],
                [InlineKeyboardButton("취소", callback_data="cancel")],
            ]
            if need_split >= 1:
                msg += f"\n\n참고: 현재가 기준 최소 분할횟수는 약 {need_split}회 이하입니다."
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))
            return

        try:
            _excg = kis_client.get_price_exchange_code(ticker)
        except Exception:
            _excg = "?"
        await query.edit_message_text(
            f"❌ {ticker} 새 사이클 1회분 LOC 주문 정보를 만들지 못했습니다.\n"
            f"- 시세/기준가 확인 실패(EXCD={_excg})"
        )
        return

    new_cycle, action = _prepare_manual_entry_cycle_state(ticker)
    res = await asyncio.to_thread(strategy.execute_all_order, ticker, info)
    tl = utils.get_cycle_timeline(ticker)
    head = (
        f"[회차 {action}] {ticker} → 현재 {new_cycle}회차\n"
        f"- 이전 회차: 시작 {tl.get('previous_start', '-')} / 종료 {tl.get('previous_end', '-')}\n"
        f"- 현재 회차 시작일: {tl.get('current_start', '-')}\n\n"
    )
    await query.edit_message_text(head + str(res))
    if "성공" in str(res):
        utils.set_trade_lock(ticker)


async def _cb_entryforce(update, context, query, parts):
    """1회분 부족 시 사용자가 선택한 '이번만 1주 강제 LOC 진입'."""
    ticker = parts[1] if len(parts) > 1 else ""
    if not ticker:
        await query.edit_message_text("티커 정보가 없어 주문을 진행할 수 없습니다.")
        return

    try:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        active_cnt = sum(1 for u in unfilled if u.get("status") == "대기") if unfilled else 0
        if active_cnt > 0:
            await query.edit_message_text(
                f"❌ {ticker} 강제 진입 취소\n"
                f"- 계좌에 미체결 대기 주문 {active_cnt}건이 있어 중복 위험이 큽니다."
            )
            return
    except Exception as e:
        log.warning("강제 진입 전 미체결 확인 실패: %s", e)

    px = 0.0
    try:
        p = await asyncio.to_thread(kis_client.get_current_price, ticker)
        px = float(p or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        try:
            p2 = await asyncio.to_thread(kis_client.get_prev_close, ticker)
            px = float(p2 or 0)
        except Exception:
            px = 0.0
    if px <= 0:
        await query.edit_message_text(
            f"❌ {ticker} 강제 진입 실패: 현재가/전일가를 가져오지 못했습니다."
        )
        return

    loc_price = round(px * 1.10, 2)
    new_cycle, action = _prepare_manual_entry_cycle_state(ticker)
    res = await asyncio.to_thread(kis_client.send_order, ticker, 1, loc_price, "BUY", "LOC")

    tl = utils.get_cycle_timeline(ticker)
    msg = (
        f"[회차 {action}] {ticker} 1주 강제 LOC 진입\n"
        f"- 주문: 1주 @ ${loc_price:.2f} (기준가 ${px:.2f})\n"
        f"- 이전 회차: 시작 {tl.get('previous_start', '-')} / 종료 {tl.get('previous_end', '-')}\n"
        f"- 현재 회차: {new_cycle}회차 (시작일 {tl.get('current_start', '-')})\n"
        f"- 결과: {res.get('msg1', '')}"
    )
    await query.edit_message_text(msg)
    if res.get("rt_cd") == "0":
        utils.set_trade_lock(ticker)


async def _cb_status_dive(update, context, query, parts):
    ticker = parts[1]
    s = STRATEGY.get(ticker, {})
    ver = strategy.format_method_display(s.get("method", "2.2"))
    theory_msg = strategy.generate_ticker_message(ticker)

    # 최근 주문/체결 내역(증권사 서버) 조회: 날짜/시간 포함
    try:
        recent = await asyncio.to_thread(
            kis_client.query_recent_orders, ticker, 5, 3
        )
    except Exception:
        recent = []

    # 사이클 시작일 이후 내역만 표시 (없으면 최근 N일 그대로)
    try:
        cycle_state = utils.load_cycle_state()
        _cs_info = cycle_state.get(ticker, {}) or {}
        _cs_active = _cs_info.get("active") or {}
        start_date_str = _cs_active.get("start_date") or ""
        start_ymd = start_date_str.replace("-", "") if start_date_str else ""
    except Exception:
        start_ymd = ""

    if recent:
        # 주문번호 기준 중복 제거
        seen = set()
        rows = []
        for o in recent:
            odno = o.get("odno")
            if odno and odno in seen:
                continue
            if odno:
                seen.add(odno)
            rows.append(o)

        # (필터) 사이클 시작일 이전 데이터 제거 + 0가격 잡음 제거
        filtered = []
        for o in rows:
            ord_dt = (o.get("ord_dt") or "").strip()
            if start_ymd and ord_dt and ord_dt < start_ymd:
                continue
            try:
                ord_pr = float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", "0") or 0) or 0)
                ccld_pr = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0") or 0) or 0)
                # LOC/지정가 계열에서 0.00은 의미 없는 잡음으로 간주 (예외 케이스는 남겨도 되지만 노이즈가 큼)
                if ord_pr <= 0 and ccld_pr <= 0:
                    continue
            except Exception:
                pass
            filtered.append(o)
        rows = filtered

        # 최신순 정렬 (ord_dt/ord_tmd)
        def _k(o):
            return (o.get("ord_dt") or "", o.get("ord_tmd") or "", o.get("odno") or "")

        rows = sorted(rows, key=_k, reverse=True)

        # 오늘/전일 기준으로 요약 표시 (길이 축소) + 필요 시 날짜별 상세 버튼 제공
        try:
            import datetime as _dt
            import pytz as _pytz
            est = _pytz.timezone("US/Eastern")
            now_est = _dt.datetime.now(est)
            today_ymd = now_est.strftime("%Y%m%d")
            yday_ymd = (now_est - _dt.timedelta(days=1)).strftime("%Y%m%d")
        except Exception:
            today_ymd = ""
            yday_ymd = ""

        def _summarize_day(day_rows):
            stats = {
                "buy_ord": 0,
                "sell_ord": 0,
                "buy_ccld": 0,
                "sell_ccld": 0,
                "left": 0,
                "status": {},
                "count": 0,
            }
            for o in day_rows:
                stats["count"] += 1
                side_cd = o.get("sll_buy_dvsn_cd")
                is_buy = side_cd == "02"
                try:
                    ord_qty = int(float(o.get("ft_ord_qty", "0") or 0))
                    ccld_qty = int(float(o.get("ft_ccld_qty", "0") or 0))
                    nccs_qty = int(float(o.get("nccs_qty", "0") or 0))
                except Exception:
                    ord_qty, ccld_qty, nccs_qty = 0, 0, 0
                if is_buy:
                    stats["buy_ord"] += ord_qty
                    stats["buy_ccld"] += ccld_qty
                else:
                    stats["sell_ord"] += ord_qty
                    stats["sell_ccld"] += ccld_qty
                stats["left"] += max(nccs_qty, 0)
                st = (o.get("prcs_stat_name") or "").strip() or "?"
                stats["status"][st] = stats["status"].get(st, 0) + 1
            return stats

        def _fmt_status_counts(status_map):
            # 상위 2개만 노출
            items = sorted(status_map.items(), key=lambda kv: kv[1], reverse=True)
            return ", ".join([f"{k}{v}" for k, v in items[:2]]) if items else "-"

        day_map = {}
        for o in rows:
            d = (o.get("ord_dt") or "").strip()
            if not d:
                continue
            day_map.setdefault(d, []).append(o)

        scope_label = (
            f"사이클 시작일({start_ymd[:4]}-{start_ymd[4:6]}-{start_ymd[6:8]}) 이후"
            if start_ymd
            else "최근 5일"
        )
        order_summary = f"\n📝 **증권사 주문 내역 요약({scope_label})**\n"

        keyboard = []
        for label, d in (("오늘", today_ymd), ("전일", yday_ymd)):
            if not d or d not in day_map:
                order_summary += f"- {label}: 내역 없음\n"
                continue
            s = _summarize_day(day_map[d])
            d_disp = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            order_summary += (
                f"- {label}({d_disp}): "
                f"매수 주문 {s['buy_ord']}주/체결 {s['buy_ccld']}주, "
                f"매도 주문 {s['sell_ord']}주/체결 {s['sell_ccld']}주, "
                f"잔량 {s['left']}주, 상태({ _fmt_status_counts(s['status']) })\n"
            )
            keyboard.append([InlineKeyboardButton(f"📄 {label} 상세보기", callback_data=f"orders_day:{ticker}:{d}")])

        if keyboard:
            order_summary += "\n(상세가 필요하면 아래 버튼을 눌러 확인하세요.)\n"
        else:
            order_summary += "\n(오늘/전일 내역이 없습니다.)\n"
    else:
        order_summary = "\n⚠️ **증권사 주문 내역을 가져오지 못했습니다.**\n(휴장일/네트워크/조회기간 이슈일 수 있습니다.)\n"
    content = (
        f"🔍 **{ticker} 상세 매매 지시서 (Deep-Dive)**\n"
        f"📌 무한매수 버전: **{ver}**\n"
        "--------------------------\n"
        f"{theory_msg}\n{order_summary}"
    )
    if recent and 'keyboard' in locals() and keyboard:
        await query.message.reply_text(content, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.message.reply_text(content, parse_mode="Markdown")


async def _cb_orders_day(update, context, query, parts):
    """일자별 주문 상세 보기."""
    ticker = parts[1] if len(parts) > 1 else ""
    ymd = parts[2] if len(parts) > 2 else ""
    if not ticker or not ymd:
        await query.edit_message_text("요청 정보가 부족합니다.")
        return

    try:
        recent = await asyncio.to_thread(
            kis_client.query_recent_orders, ticker, 10, 4
        )
    except Exception:
        recent = []

    # 사이클 시작일 이후 내역만 표시 (없으면 최근 N일 그대로)
    try:
        cycle_state = utils.load_cycle_state()
        _cs_info = cycle_state.get(ticker, {}) or {}
        _cs_active = _cs_info.get("active") or {}
        start_date_str = _cs_active.get("start_date") or ""
        start_ymd = start_date_str.replace("-", "") if start_date_str else ""
    except Exception:
        start_ymd = ""

    rows = []
    for o in recent or []:
        ord_dt = (o.get("ord_dt") or "").strip()
        if start_ymd and ord_dt and ord_dt < start_ymd:
            continue
        if ord_dt != ymd:
            continue
        try:
            ord_pr = float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", "0") or 0) or 0)
            ccld_pr = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0") or 0) or 0)
            if ord_pr <= 0 and ccld_pr <= 0:
                continue
        except Exception:
            pass
        rows.append(o)

    rows = sorted(rows, key=lambda o: (o.get("ord_tmd") or "", o.get("odno") or ""), reverse=True)
    d_disp = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}" if len(ymd) >= 8 else ymd

    if not rows:
        await query.edit_message_text(f"📄 **{ticker} {d_disp} 주문 상세**\n\n- 내역이 없습니다.", parse_mode="Markdown")
        return

    msg = f"📄 **{ticker} {d_disp} 주문 상세**\n\n"
    for o in rows[:30]:
        ord_tmd = (o.get("ord_tmd") or "").strip()
        tm_disp = f"{ord_tmd[:2]}:{ord_tmd[2:4]}:{ord_tmd[4:6]}" if len(ord_tmd) >= 6 else ""
        side_name = (o.get("sll_buy_dvsn_cd_name") or "").strip()
        if not side_name:
            side_name = "매수" if o.get("sll_buy_dvsn_cd") == "02" else "매도"
        ord_qty = int(float(o.get("ft_ord_qty", "0") or 0))
        ord_pr = float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", "0") or 0) or 0)
        ccld_qty = int(float(o.get("ft_ccld_qty", "0") or 0))
        ccld_pr = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", "0") or 0) or 0)
        nccs_qty = int(float(o.get("nccs_qty", "0") or 0))
        status = (o.get("prcs_stat_name") or "").strip()
        msg += f"- {tm_disp} {side_name} {ord_qty}주 @ ${ord_pr:.2f} → 체결 {ccld_qty}주 @ ${ccld_pr:.2f} / 잔량 {nccs_qty}주 ({status})\n"

    if len(rows) > 30:
        msg += "\n... (생략)\n"
    await query.edit_message_text(msg, parse_mode="Markdown")


async def _cb_unfilled(update, context, query, parts):
    """선택 전 검증: 해당 종목의 미체결 주문 상세를 보여줍니다."""
    ticker = parts[1] if len(parts) > 1 else ""
    if not ticker:
        await query.edit_message_text("티커 정보가 없습니다.")
        return

    try:
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
    except Exception as e:
        await query.edit_message_text(f"미체결 조회 오류: {str(e)}")
        return

    if not unfilled:
        msg = (
            f"🔎 **{ticker} 미체결 상세 (미국 동부 당일)**\n\n"
            "- 당일(미국) 미체결 주문이 없습니다."
        )
        # 미체결이 없으면 바로 주문 진행 버튼을 제공
        info = strategy.build_order_info(ticker)
        if info.get("qty_total", 0) == 0:
            cb = f"m_entryask:{ticker}"
            buttons = [[InlineKeyboardButton(f"🚀 {ticker} 오늘 LOC 진입 주문", callback_data=cb)]]
        else:
            buttons = [[InlineKeyboardButton(f"🏦 {ticker} 오늘 LOC 매매 주문", callback_data=f"m_all:{ticker}")]]
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # 날짜별로 묶어서 표시 (미국 현지 ord_dt 기준)
    def _date_key(u):
        return u.get("ord_dt") or "unknown"

    unfilled_sorted = sorted(unfilled, key=lambda u: (_date_key(u), u.get("ord_tmd") or "", u.get("ord_no") or ""))
    groups = {}
    for u in unfilled_sorted:
        groups.setdefault(_date_key(u), []).append(u)

    msg = f"🔎 **{ticker} 미체결 상세 (미국 동부 당일)**\n\n"
    for ord_dt, items in groups.items():
        if ord_dt and len(ord_dt) >= 8:
            dt_disp = f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:8]}"
        else:
            dt_disp = "날짜 미상"
        msg += f"📅 **{dt_disp}**\n"
        for u in items:
            side = u.get("side", "?")
            qty = u.get("qty", 0)
            price = u.get("price", 0.0)
            typ = u.get("type", "")
            status = u.get("status", "")
            tmd = u.get("ord_tmd") or ""
            tmd_disp = f"{tmd[:2]}:{tmd[2:4]}:{tmd[4:6]}" if len(tmd) >= 6 else ""
            prefix = f"{tmd_disp} " if tmd_disp else ""
            msg += f"• {prefix}{side} {qty}주 @ ${price:.2f} ({typ}) - {status}\n"
        msg += "\n"

    msg += (
        "ℹ️ **'완료'의 의미**\n"
        "- 증권사 기준으로 해당 주문 건의 **처리 상태가 종료(완료)** 되었음을 의미합니다.\n"
        "- 다만 이 리스트는 `주문수량 > 체결수량`인 경우를 모아 보여주므로, '완료'라도 **일부 미체결 잔량**이 남아 있을 수 있습니다.\n"
        "- (예) 지정가/LOC 조건 불충족으로 체결이 끝나고 주문이 종료된 경우, 또는 일부 체결 후 잔량이 남은 채로 종료된 경우\n\n"
        "⚠️ 위 미체결(또는 잔량)이 이미 존재한다면, 추가 주문은 중복/과매수가 될 수 있습니다.\n"
    )

    # 이 화면에서 바로 주문 진행 버튼 제공
    info = strategy.build_order_info(ticker)
    if info.get("qty_total", 0) == 0:
        cb = f"m_entryask:{ticker}"
        buttons = [[InlineKeyboardButton(f"🚀 {ticker} 오늘 LOC 진입 주문", callback_data=cb)]]
    else:
        buttons = [[InlineKeyboardButton(f"🏦 {ticker} 오늘 LOC 매매 주문", callback_data=f"m_all:{ticker}")]]

    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


def _format_profit_history_row(h: dict) -> str:
    """history 한 줄 (텔레그램 길이·파싱 실패에 덜 취약하게)."""
    vlab = strategy.format_method_display(h.get("method"))
    d = h.get("date", "?")
    tic = str(h.get("ticker") or "?").strip().upper()
    try:
        sq = int(h.get("sell_qty", h.get("qty", 0)) or 0)
    except (TypeError, ValueError):
        sq = 0
    try:
        ap = float(h.get("avg_price", h.get("avg", 0)) or 0)
    except (TypeError, ValueError):
        ap = 0.0
    try:
        sp = float(h.get("sell_price", h.get("sell", 0)) or 0)
    except (TypeError, ValueError):
        sp = 0.0
    try:
        pf = float(h.get("profit") or 0)
    except (TypeError, ValueError):
        pf = 0.0
    od = str(h.get("order_no") or "").strip()
    od_s = f" | 주문:{od}" if od else ""
    return f"• {d} | {tic} | {vlab} | {sq}주 | 평단:${ap:.2f}/매도:${sp:.2f} | **${pf:+,.2f}**{od_s}\n"


async def _cb_profit_dive(update, context, query, parts):
    cp = utils.load_cumul_profit()
    history = [h for h in (cp.get("history") or []) if isinstance(h, dict)]
    if not history:
        await query.message.reply_text(
            "건별 `history`가 없습니다. `/profit_history` 요약과 같습니다.\n"
            "누적만 있는 경우 `/report`를 확인하세요."
        )
        return
    # 텔레그램 단일 메시지 상한(4096) 대비 여유 있게 분할
    chunk_size = 18
    for i in range(0, len(history), chunk_size):
        chunk = history[i : i + chunk_size]
        part_n = i // chunk_size + 1
        total_parts = (len(history) + chunk_size - 1) // chunk_size
        msg = f"📜 **전체 수익 기록** ({part_n}/{total_parts})\n\n"
        for h in chunk:
            msg += _format_profit_history_row(h)
        try:
            await query.message.reply_text(msg, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(msg)


TG_MESSAGE_SAFE = 3900


async def _reply_long_markdown(message, text: str) -> None:
    """Markdown 본문이 길면 여러 메시지로 나눕니다(이론 가이드용)."""
    t = (text or "").strip()
    if not t:
        await message.reply_text("(내용 없음)")
        return

    async def _send_chunk(chunk: str) -> None:
        try:
            await message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            # Telegram Markdown: 밑줄·별표 불균형 등으로 파싱 실패 시 그대로 전송
            await message.reply_text(chunk)

    while t:
        if len(t) <= TG_MESSAGE_SAFE:
            await _send_chunk(t)
            break
        cut = t.rfind("\n\n", 0, TG_MESSAGE_SAFE)
        if cut < TG_MESSAGE_SAFE // 3:
            cut = TG_MESSAGE_SAFE
        part = t[:cut].strip()
        t = t[cut:].strip()
        if part:
            await _send_chunk(part)


async def _cb_guide(update, context, query, parts):
    guide_type = parts[1]
    content = GUIDE_TEXTS.get(guide_type, "")
    if not content:
        await query.message.reply_text("해당 가이드가 없습니다.")
        return
    if guide_type.startswith("theory_"):
        await _reply_long_markdown(query.message, content)
    else:
        await query.message.reply_text(content, parse_mode="Markdown")


async def _cb_ask_del(update, context, query, parts):
    ticker = parts[1]
    keyboard = [
        [InlineKeyboardButton("✅ 확인", callback_data=f"exec_del:{ticker}"), InlineKeyboardButton("❌ 취소", callback_data="cancel")],
    ]
    await query.edit_message_text(
        f"🗑️ **{ticker}**를 정말로 삭제하시겠습니까?\n\n(참고: 감시만 중단되며 실제 주식은 팔지 않습니다.)",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _cb_exec_del(update, context, query, parts):
    await _execute_del_target(query, parts[1])


async def _cb_ask_liq(update, context, query, parts):
    ticker = parts[1]
    keyboard = [
        [
            InlineKeyboardButton("🚀 시장가(MOC) 매도", callback_data=f"exec_liq:{ticker}:02"),
            InlineKeyboardButton("🎯 지정가(현재가) 매도", callback_data=f"exec_liq:{ticker}:00"),
        ],
        [InlineKeyboardButton("❌ 취소", callback_data="cancel")],
    ]
    await query.edit_message_text(
        f"🚨 **{ticker}** 사이클을 완전히 종료하시겠습니까?\n\n"
        "아래 매도 방식을 선택해 주세요.\n"
        "장 마감 후 체결 확인 및 수익 기록이 완료되면 설정이 자동 삭제됩니다.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _cb_exec_liq(update, context, query, parts):
    # parts: ["exec_liq", ticker, ord_type]
    ord_type = parts[2] if len(parts) >= 3 else "02"
    await _execute_liquidate(query, parts[1], ord_type=ord_type)


async def _cb_cancel(update, context, query, parts):
    await query.edit_message_text("작업이 취소되었습니다.")


async def _cb_edit_pick(update, context, query, parts):
    """edit_target 워크플로우: 수정할 티커 선택 → 단계별 입력 시작."""
    ticker = parts[1] if len(parts) > 1 else ""
    if not ticker:
        await query.edit_message_text("티커 정보가 없습니다.")
        return

    from state_manager import STRATEGY
    if ticker not in STRATEGY:
        await query.edit_message_text(f"❌ {ticker} 종목은 현재 투자 대상이 아닙니다.")
        return

    s = STRATEGY[ticker]
    method = s.get("method", "2.2")
    # V3.0/V4.0 TQQQ/SOXL: 수익률 자동 적용 여부 판단
    auto_profit = None
    if method in ("3.0", "4.0"):
        auto_profit = _v30_v40_hardcoded_profit(ticker)

    context.user_data["pending_edit_target"] = {
        "ticker": ticker,
        "stage": "seed",
        "seed": None,
        "total_a": None,
        "profit": None,
        "auto_profit": auto_profit,
        "current": {
            "seed": s.get("seed", 0),
            "total_a": s.get("total_a", 0),
            "profit": s.get("target_profit", 0),
            "method": method,
            "one_time_usd": s.get("one_time_usd"),
        },
    }

    cur = context.user_data["pending_edit_target"]["current"]
    ot_disp = cur.get("one_time_usd")
    ot_line = f"\n- <code>one_time_usd</code>: <code>{ot_disp}</code>" if ot_disp is not None else ""

    if auto_profit is not None:
        # V3.0/V4.0 TQQQ/SOXL: 2단계만 (시드 → 분할횟수)
        msg = (
            f"📝 <b>{ticker} 설정 수정</b>\n\n"
            f"- 현재 시드: <code>{cur['seed']}</code>\n"
            f"- 현재 분할횟수: <code>{cur['total_a']}</code>\n"
            f"- 목표수익률: <code>{auto_profit}</code>% (V{method} 자동 적용)\n"
            f"- 방식(method): <code>{method}</code>{ot_line}\n\n"
            f"⚠️ V{method}에서 {ticker}의 목표수익률은 <b>{auto_profit}%</b>로 자동 적용됩니다.\n\n"
            "<b>1/2</b> 새 <b>시드</b>를 입력해 주세요. (예: <code>2000</code>)\n"
            "또는 한 줄로 <code>시드 분할횟수</code> (예: <code>2000 40</code>)\n\n"
            "취소하려면 <code>취소</code> 라고 입력하세요."
        )
    else:
        msg = (
            f"📝 <b>{ticker} 설정 수정</b>\n\n"
            f"- 현재 시드: <code>{cur['seed']}</code>\n"
            f"- 현재 분할횟수: <code>{cur['total_a']}</code>\n"
            f"- 현재 목표수익률: <code>{cur['profit']}</code>%\n"
            f"- 방식(method): <code>{cur.get('method', '2.2')}</code>{ot_line}\n\n"
            "<b>1/3</b> 새 <b>시드</b>를 입력해 주세요. (예: <code>2000</code>)\n"
            "또는 한 줄로 <code>시드 분할횟수 목표수익률</code> (예: <code>2000 40 10</code>)\n\n"
            "취소하려면 <code>취소</code> 라고 입력하세요."
        )
    await query.edit_message_text(msg, parse_mode="HTML")


CALLBACK_HANDLERS = {
    "all": _cb_all,
    "entry": _cb_entry,
    "entryask": _cb_entryask,
    "entrycycle": _cb_entrycycle,
    "entryforce": _cb_entryforce,
    "status_dive": _cb_status_dive,
    "orders_day": _cb_orders_day,
    "unfilled": _cb_unfilled,
    "profit_dive": _cb_profit_dive,
    "guide": _cb_guide,
    "ask_del": _cb_ask_del,
    "exec_del": _cb_exec_del,
    "ask_liq": _cb_ask_liq,
    "exec_liq": _cb_exec_liq,
    "edit_pick": _cb_edit_pick,
    "cancel": _cb_cancel,
}


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[0]
    ticker = parts[1] if len(parts) > 1 else ""

    # 수동 주문(manual_order)에서는 잠금 안내 후 사용자가 선택한 주문을 허용합니다.
    is_manual = action.startswith("m_")
    if is_manual:
        action = action[2:]

    # trade_lock은 "오늘의 주문 전송" 중복만 막는 용도이므로, 실제 주문 액션에만 적용합니다.
    if (not is_manual) and action in ("all", "entry") and ticker and utils.check_trade_lock(ticker):
        unfilled = await asyncio.to_thread(kis_client.query_unfilled_orders_today_us, ticker)
        lock_msg = f"⚠️ **{ticker}**는 이미 오늘 매매 지시가 전송되었습니다.\n\n"
        if unfilled:
            is_active = any(u["status"] == "대기" for u in unfilled)
            if is_active:
                lock_msg += "⏳ **증권사 대기 중인 주문 (미국 당일):**\n"
                for u in unfilled:
                    if u["status"] == "대기":
                        lock_msg += f"• {u['side']} {u['qty']}주 @ ${u['price']:.2f} ({u['type']})\n"
                lock_msg += "\n(💡 장 마감 시점에 가격 조건이 맞으면 체결됩니다.)"
            else:
                lock_msg += "❕ **체결되지 않고 종료된 주문 (미국 당일):**\n"
                for u in unfilled:
                    lock_msg += f"• {u['side']} {u['qty']}주 @ ${u['price']:.2f} ({u['type']}) - {u['status']}\n"
                lock_msg += "\n(💡 지정가/LOC 조건이 맞지 않아 체결되지 않았습니다.)"
        else:
            lock_msg += "✅ 모든 주문이 이미 체결된 상태입니다."
        await query.edit_message_text(lock_msg, parse_mode="Markdown")
        return

    handler = CALLBACK_HANDLERS.get(action)
    if handler:
        await handler(update, context, query, parts)




async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # edit_target 단계형 워크플로우 입력 처리
    pending = context.user_data.get("pending_edit_target")
    if pending and update and getattr(update, "message", None) and getattr(update.message, "text", None):
        text = update.message.text.strip()
        if text in ("취소", "cancel", "/cancel"):
            context.user_data.pop("pending_edit_target", None)
            await update.message.reply_text("✅ 설정 수정이 취소되었습니다.")
            return

        def _validate(seed_v, total_a_v, profit_v):
            if seed_v <= 0:
                return "❌ 시드는 0보다 커야 합니다."
            if int(total_a_v) != total_a_v:
                return "❌ 분할횟수는 정수여야 합니다. (예: 40)"
            if total_a_v < 1 or total_a_v > 300:
                return "❌ 분할횟수는 1~300 범위에서 입력해 주세요."
            if profit_v <= 0 or profit_v > 200:
                return "❌ 목표수익률은 0보다 크고 200(%) 이하여야 합니다."
            return ""

        parts = text.split()
        auto_profit = pending.get("auto_profit")

        # 한 줄 입력: V3.0/V4.0 자동수익률 종목은 2개, 그 외 3개
        if auto_profit is not None and len(parts) == 2:
            try:
                seed = float(parts[0])
                total_a = float(parts[1])
            except ValueError:
                await update.message.reply_text("❌ 숫자 입력이 올바르지 않습니다. 예: 2000 40")
                return
            profit = auto_profit
            err = _validate(seed, total_a, profit)
            if err:
                await update.message.reply_text(err)
                return
            pending["seed"] = seed
            pending["total_a"] = int(total_a)
            pending["profit"] = profit
            pending["stage"] = "done"
        elif len(parts) == 3:
            try:
                seed = float(parts[0])
                total_a = float(parts[1])
                profit = float(parts[2])
            except ValueError:
                await update.message.reply_text("❌ 숫자 입력이 올바르지 않습니다. 예: 2000 40 10")
                return
            # V3.0/V4.0 자동수익률 종목이면 사용자 입력 무시, 하드코딩 적용
            if auto_profit is not None:
                profit = auto_profit
            err = _validate(seed, total_a, profit)
            if err:
                await update.message.reply_text(err)
                return
            pending["seed"] = seed
            pending["total_a"] = int(total_a)
            pending["profit"] = profit
            pending["stage"] = "done"
        else:
            stage = pending.get("stage", "seed")
            # 단계형 입력: seed → split → (profit - V3.0/V4.0 자동적용 시 생략)
            if stage == "seed":
                try:
                    seed = float(text)
                except ValueError:
                    await update.message.reply_text("❌ 새 시드를 숫자로 입력해 주세요. 예: 2000")
                    return
                if seed <= 0:
                    await update.message.reply_text("❌ 시드는 0보다 커야 합니다.")
                    return
                pending["seed"] = seed
                pending["stage"] = "split"
                if auto_profit is not None:
                    await update.message.reply_text("✅ 2/2 새 <b>분할횟수</b>(정수)를 입력해 주세요. 예: <code>40</code>", parse_mode="HTML")
                else:
                    await update.message.reply_text("✅ 2/3 새 <b>분할횟수</b>(정수)를 입력해 주세요. 예: <code>40</code>", parse_mode="HTML")
                return

            if stage == "split":
                try:
                    total_a = float(text)
                except ValueError:
                    await update.message.reply_text("❌ 분할횟수는 정수로 입력해 주세요. 예: 40")
                    return
                if int(total_a) != total_a:
                    await update.message.reply_text("❌ 분할횟수는 정수여야 합니다. 예: 40")
                    return
                total_a_i = int(total_a)
                if total_a_i < 1 or total_a_i > 300:
                    await update.message.reply_text("❌ 분할횟수는 1~300 범위에서 입력해 주세요.")
                    return
                pending["total_a"] = total_a_i

                # V3.0/V4.0 자동수익률: profit 단계 건너뛰고 바로 완료
                if auto_profit is not None:
                    pending["profit"] = auto_profit
                    pending["stage"] = "done"
                else:
                    pending["stage"] = "profit"
                    await update.message.reply_text("✅ 3/3 새 <b>목표수익률(%)</b>을 입력해 주세요. 예: <code>10</code>", parse_mode="HTML")
                    return

            if stage == "profit":
                try:
                    profit = float(text)
                except ValueError:
                    await update.message.reply_text("❌ 목표수익률(%)은 숫자로 입력해 주세요. 예: 10")
                    return
                if profit <= 0 or profit > 200:
                    await update.message.reply_text("❌ 목표수익률은 0보다 크고 200(%) 이하여야 합니다.")
                    return
                pending["profit"] = profit
                pending["stage"] = "done"

        if pending.get("stage") == "done":
            ticker = pending.get("ticker")
            seed = float(pending.get("seed"))
            total_a = int(pending.get("total_a"))
            profit = float(pending.get("profit"))
            err = _validate(seed, float(total_a), profit)
            if err:
                await update.message.reply_text(err)
                return

            from config_manager import STRATEGY_SETTINGS, save_config
            from state_manager import STRATEGY

            if ticker not in STRATEGY:
                context.user_data.pop("pending_edit_target", None)
                await update.message.reply_text(f"❌ {ticker} 종목은 현재 투자 대상이 아닙니다.")
                return

            new_settings = dict(STRATEGY_SETTINGS)
            merged = {**STRATEGY_SETTINGS.get(ticker, {}), "seed": seed, "total_a": total_a, "target_profit": profit}
            new_settings[ticker] = merged
            if save_config({"STRATEGY_SETTINGS": new_settings}):
                STRATEGY[ticker] = _runtime_strategy_entry(ticker, merged)
                context.user_data.pop("pending_edit_target", None)
                await update.message.reply_text(
                    f"✅ {ticker} 설정 변경 완료!\n- 시드: ${seed:,.0f}\n- 분할횟수: {total_a}회\n- 목표수익률: {profit}%\n- 방식: {STRATEGY[ticker]['method']} (method/one_time_usd 유지)",
                )
                return
            await update.message.reply_text("❌ 설정 저장 실패")
            return

    if not _message_has_bot_command(update.message):
        return

    msg = (
        "❓ **알 수 없는 명령어입니다.**\n\n"
        "입력하신 내용을 이해하지 못했습니다. 아래 버튼을 눌러 명령을 실행하거나, `/help`를 입력해 전체 명령어 리스트를 확인해 주세요!"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_reply_keyboard_markup())

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if err is not None:
        tb = getattr(err, "__traceback__", None)
        log.error(
            "Telegram handler error: %s\n%s",
            err,
            "".join(traceback.format_exception(type(err), err, tb)),
        )
    else:
        log.error("Telegram handler error (no exception object)")

async def post_init(application):
    """봇 초기화: 메뉴 명령어 등록, 스케줄 복원, 누락 정산 체크."""
    commands = [
        BotCommand("status", "📊 현황보기"),
        BotCommand("cash", "💰 예수금확인"),
        BotCommand("report", "📈 수익보고"),
        BotCommand("sync", "🔄 신규동기화"),
        BotCommand("manual_order", "🧾 수동주문"),
        BotCommand("help", "📚 도움말"),
        BotCommand("profit_history", "📋 수익기록"),
        BotCommand("add_target", "➕ 종목추가"),
        BotCommand("edit_target", "📝 종목수정"),
        BotCommand("del_target", "🗑️ 종목삭제"),
        BotCommand("liquidate", "🚨 전량이익매도 및 종료"),
        BotCommand("logs", "📋 최근 24시간 로그"),
    ]
    await application.bot.set_my_commands(commands)
    
    # NotificationService에 실시간 봇 인스턴스 주입
    notifier.set_bot(application.bot)
    
    chat_id = utils.load_chat_id()
    if chat_id:
        from scheduler import register_schedules, check_missed_settlement
        register_schedules(application.job_queue, chat_id)
        log.info("Registered schedules for chat_id=%s", chat_id)
        
        # 8시 리포트 누락 체크 (비동기로 실행)
        def _log_missed_settlement_task(t: asyncio.Task) -> None:
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                log.error("check_missed_settlement failed: %s", exc, exc_info=True)

        _t = asyncio.create_task(check_missed_settlement(application))
        _t.add_done_callback(_log_missed_settlement_task)
