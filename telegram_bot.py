from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand,
)
from telegram.ext import ContextTypes

import kis_client
import strategy
import utils
from state_manager import STRATEGY, CURRENT_CASH
from trade_service import TradeService
from notification_service import notifier
from ui_formatter import UIFormatter
import guide_theory

log = logging.getLogger(__name__)

# --- 공통 헬퍼 ---

def main_reply_keyboard_markup():
    """하단 고정 버튼 메뉴"""
    keyboard = [
        [KeyboardButton("📊 현황보기"), KeyboardButton("💰 예수금확인")],
        [KeyboardButton("신규동기화"), KeyboardButton("🧾 수동주문")],
        [KeyboardButton("📈 수익보고"), KeyboardButton("📋 수익기록")],
        [KeyboardButton("🕔 프리마켓 ON"), KeyboardButton("🔕 프리마켓 OFF")],
        [KeyboardButton("📚 도움말")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def _runtime_strategy_entry(ticker, s_val) -> dict:
    """state_manager의 STRATEGY 딕셔너리에 들어갈 런타임 구조체 생성"""
    from state_manager import normalize_strategy_method, _parse_one_time_usd
    return {
        "seed": float(s_val.get("seed", 0.0)),
        "total_a": float(s_val.get("total_a", 20.0)),
        "target_profit": float(s_val.get("target_profit", 10.0)),
        "method": normalize_strategy_method(s_val.get("method")),
        "one_time_usd": _parse_one_time_usd(s_val.get("one_time_usd")),
        "data": {"avg": 0.0, "qty": 0.0, "cumul": 0.0},
    }

def _v30_v40_hardcoded_profit(ticker: str) -> float | None:
    """TQQQ/SOXL 종목에 대해 V3.0/V4.0일 때 하드코딩된 목표수익률을 반환합니다."""
    t = str(ticker or "").strip().upper()
    if t == "TQQQ": return 10.0
    if t == "SOXL": return 12.0
    return None

def _message_has_bot_command(message) -> bool:
    """메시지에 봇 커맨드 엔티티가 포함되어 있는지 확인합니다."""
    if not message or not message.entities:
        return False
    return any(e.type == "bot_command" for e in message.entities)


# --- 명령어 핸들러 ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    utils.save_chat_id(chat_id)
    
    welcome_msg = (
        "👋 **안녕하세요! UBS Manager Trading Bot입니다.**\n\n"
        "이 봇은 무한매수법(V2.2, V3.0, V4.0)을 기반으로 한 자동 매매 및 모니터링 시스템입니다.\n\n"
        "**주요 명령어:**\n"
        "/status - 현재 종목별 투자 현황 및 매매 지시서 확인\n"
        "/sync - 증권사 잔고와 실시간 동기화\n"
        "/report - 기간별 수익 및 성과 리포트\n"
        "/help - 전체 명령어 가이드\n\n"
        "아래 메뉴 버튼을 사용하여 간편하게 조작하실 수 있습니다."
    )
    await update.message.reply_text(
        UIFormatter.clean_text(welcome_msg), 
        reply_markup=main_reply_keyboard_markup()
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """현재 투자 현황 및 지시서 요약"""
    # 1. 최신 시세 및 잔고 동기화 (선택적: 너무 잦으면 생략 가능하지만 상태 정확성을 위해 권장)
    import state_manager
    sync_ok, sync_msg = await asyncio.to_thread(state_manager.sync_account)
    if not sync_ok:
        log.warning("cmd_status 동기화 실패: %s", sync_msg)
    
    # 2. 리포트 데이터 구성
    report_data = TradeService.prepare_daily_report_data()
    summary_text = UIFormatter.format_daily_report(report_data)
    
    # 3. 상세 지시서 확인용 인라인 버튼 구성
    keyboard = []
    for tkr in sorted(STRATEGY.keys()):
        keyboard.append([InlineKeyboardButton(f"🔍 {tkr} 상세 지시서", callback_data=f"status_dive:{tkr}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(
        UIFormatter.clean_text(summary_text),
        reply_markup=reply_markup
    )


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수동 잔고 동기화"""
    msg = await update.message.reply_text("🔄 증권사 계좌 정보를 가져오는 중입니다...")
    
    import state_manager
    success, res_msg = await asyncio.to_thread(state_manager.sync_account)
    
    sync_done = f"✅ **동기화 완료**\n\n{res_msg}" if success else f"❌ **동기화 실패**\n\n{res_msg}"
    await msg.edit_text(UIFormatter.clean_text(sync_done))


async def cmd_cash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """예수금 정보 확인"""
    import state_manager
    # 동기화 먼저 수행
    await asyncio.to_thread(state_manager.sync_account)
    
    msg = f"💰 **현재 보유 달러 예수금**\n\n=> ${CURRENT_CASH:,.2f}"
    await update.message.reply_text(UIFormatter.clean_text(msg))


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수익 보고서 및 성과 차트"""
    msg = await update.message.reply_text("📊 성과 데이터를 분석하고 차트를 생성 중입니다...")
    
    # 1. 텍스트 리포트 생성
    report_text = await asyncio.to_thread(TradeService.generate_settlement_report)
    
    # 2. 차트 생성
    from visualization_service import generate_asset_chart
    chart_path = "tmp/performance_chart.png"
    success, result = await asyncio.to_thread(generate_asset_chart, chart_path)
    
    if success:
        with open(chart_path, 'rb') as f:
            await update.message.reply_photo(photo=f, caption=UIFormatter.clean_text(report_text))
        await msg.delete()
    else:
        log.warning("차트 생성 실패: %s", result)
        await msg.edit_text(UIFormatter.clean_text(report_text + f"\n\n(차트 생성 실패: {result})"))


async def cmd_manual_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수동 주문 인터페이스 (종목 선택)"""
    if not STRATEGY:
        await update.message.reply_text("❌ 등록된 투자 종목이 없습니다. /add_target 명령어로 종목을 추가하세요.")
        return

    keyboard = []
    for tkr in sorted(STRATEGY.keys()):
        keyboard.append([InlineKeyboardButton(f"🏦 {tkr} 매매 주문 전송", callback_data=f"unfilled:{tkr}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        "🧾 **수동 주문 메뉴**\n\n"
        "주문을 전송할 종목을 선택하세요.\n"
        "선택 시 미체결 내역을 먼저 확인한 후 주문 페이지로 이동합니다."
    )
    await update.message.reply_text(UIFormatter.clean_text(msg), reply_markup=reply_markup)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """명령어 가이드 및 이론 설명"""
    help_text = (
        "📚 **UBS Manager Bot 명령어 가이드**\n\n"
        "**기본 명령어**\n"
        "/status - 현재 투자 현황 요약\n"
        "/sync - 잔고 동기화\n"
        "/report - 성과 보고 및 차트\n"
        "/cash - 예수금 확인\n"
        "/logs - 최근 로그 확인\n\n"
        "**설정 및 관리**\n"
        "/add_target - 종목 추가\n"
        "/edit_target - 종목 설정 수정\n"
        "/del_target - 종목 삭제 (감시 중단)\n"
        "/liquidate - 전량 익절 매도 및 사이클 종료\n"
        "/profit_history - 전체 수익 기록 요약\n"
        "/profit_reset - 수익 기록 초기화\n\n"
        "**기능 제어**\n"
        "/pre_on - 프리마켓 감시 활성화\n"
        "/pre_off - 프리마켓 감시 비활성화\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("📘 V2.2 이론", callback_data="guide:theory_22"), InlineKeyboardButton("📗 V3.0 이론", callback_data="guide:theory_30")],
        [InlineKeyboardButton("📙 V4.0 이론", callback_data="guide:theory_40"), InlineKeyboardButton("📊 버전별 비교", callback_data="guide:theory_compare")],
        [InlineKeyboardButton("📜 전체 수익 상세 기록", callback_data="profit_dive")],
    ]
    
    await update.message.reply_text(
        UIFormatter.clean_text(help_text), 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """최근 로그 조회"""
    from bot_logging import read_recent_log_text
    log_text, truncated = read_recent_log_text(hours=24, max_total_chars=3500)
    
    title = "📋 **최근 24시간 시스템 로그**"
    if truncated: title += " (일부 생략됨)"
    
    await update.message.reply_text(f"{title}\n\n<pre>{log_text}</pre>", parse_mode="HTML")


async def cmd_add_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """종목 추가 (가이드 메시지)"""
    msg = (
        "➕ **종목 추가 안내**\n\n"
        "종목 추가는 한 줄 명령어로 가능합니다.\n"
        "`/add_target [티커] [시드] [분할] [익절%] [버전(2.2/3.0/4.0)]`\n\n"
        "**예시:**\n"
        "`/add_target TQQQ 2000 40 10 3.0`\n"
        "-> TQQQ를 시드 $2000, 40분할, 10% 익절, V3.0으로 추가\n\n"
        "💡 V3.0/V4.0의 TQQQ(10%), SOXL(12%)은 익절%를 입력해도 자동 설정값이 우선 적용됩니다."
    )
    
    if not context.args or len(context.args) < 5:
        await update.message.reply_text(UIFormatter.clean_text(msg))
        return

    try:
        ticker = context.args[0].upper()
        seed = float(context.args[1])
        total_a = int(context.args[2])
        profit = float(context.args[3])
        method = context.args[4]
        
        # V3.0/V4.0 하드코딩 체크
        if method in ("3.0", "4.0"):
            hard_p = _v30_v40_hardcoded_profit(ticker)
            if hard_p is not None: profit = hard_p

        from config_manager import STRATEGY_SETTINGS, save_config
        if ticker in STRATEGY_SETTINGS:
            await update.message.reply_text(f"❌ {ticker}는 이미 등록된 종목입니다. 수정은 /edit_target을 사용하세요.")
            return

        new_settings = dict(STRATEGY_SETTINGS)
        s_val = {"seed": seed, "total_a": total_a, "target_profit": profit, "method": method}
        new_settings[ticker] = s_val
        
        if save_config({"STRATEGY_SETTINGS": new_settings}):
            STRATEGY[ticker] = _runtime_strategy_entry(ticker, s_val)
            await update.message.reply_text(f"✅ {ticker}가 성공적으로 추가되었습니다! (/sync 명령어로 잔고를 동기화하세요)")
        else:
            await update.message.reply_text("❌ 설정 저장 중 오류가 발생했습니다.")
            
    except Exception as e:
        await update.message.reply_text(f"❌ 형식 오류: {str(e)}\n\n{msg}")


async def cmd_edit_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """종목 설정 수정 (인라인 버튼으로 티커 선택)"""
    if not STRATEGY:
        await update.message.reply_text("❌ 수정할 종목이 없습니다.")
        return

    keyboard = []
    for tkr in sorted(STRATEGY.keys()):
        keyboard.append([InlineKeyboardButton(f"📝 {tkr} 설정 수정", callback_data=f"edit_pick:{tkr}")])
    
    await update.message.reply_text(
        "📝 **수정할 종목을 선택하세요**", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_del_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """종목 삭제 (인라인 버튼으로 확인)"""
    if not STRATEGY:
        await update.message.reply_text("❌ 삭제할 종목이 없습니다.")
        return

    keyboard = []
    for tkr in sorted(STRATEGY.keys()):
        keyboard.append([InlineKeyboardButton(f"🗑️ {tkr} 삭제", callback_data=f"ask_del:{tkr}")])
    
    await update.message.reply_text(
        "🗑️ **삭제할 종목을 선택하세요**\n(감시 대상에서만 제외되며 실제 주식은 팔지 않습니다)", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_liquidate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """전량 이익 매도 및 종료"""
    if not STRATEGY:
        await update.message.reply_text("❌ 매도할 종목이 없습니다.")
        return

    keyboard = []
    for tkr in sorted(STRATEGY.keys()):
        if STRATEGY[tkr]["data"]["qty"] > 0:
            keyboard.append([InlineKeyboardButton(f"🚨 {tkr} 전량매도 및 종료", callback_data=f"ask_liq:{tkr}")])
    
    if not keyboard:
        await update.message.reply_text("❌ 현재 보유 중인(qty > 0) 종목이 없습니다.")
        return

    await update.message.reply_text(
        "🚨 **전량 이익 매도 및 종료**\n\n종료할 종목을 선택하세요. 즉시 시장가/지정가 주문을 전송하고 사이클을 마무리합니다.", 
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_profit_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """누적 수익 기록 요약"""
    cp = utils.load_cumul_profit()
    summary = utils.aggregate_cumul_profit_by_ticker(cp)
    
    if not summary:
        await update.message.reply_text("📜 기록된 수익 내역이 없습니다.")
        return

    msg = "📋 **종목별 누적 실현 수익**\n\n"
    total_sum = 0.0
    for tkr, val in sorted(summary.items()):
        msg += f"- {tkr}: **${val:,.2f}**\n"
        total_sum += val
    
    msg += f"\n💰 **총 누적 합계: ${total_sum:,.2f}**"
    
    keyboard = [[InlineKeyboardButton("📜 전체 상세 기록 보기", callback_data="profit_dive")]]
    await update.message.reply_text(UIFormatter.clean_text(msg), reply_markup=InlineKeyboardMarkup(keyboard))


async def cmd_profit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """수익 기록 초기화"""
    # 안전장치: 인라인 버튼으로 재확인 (여기서는 간단히 명령어 인자로 체크)
    if not context.args or context.args[0] != "CONFIRM":
        await update.message.reply_text("⚠️ **수익 기록 초기화**\n\n정말 초기화하시려면 `/profit_reset CONFIRM` 이라고 입력하세요.\n이 작업은 되돌릴 수 없습니다.")
        return

    utils.save_cumul_profit({"history": [], "processed_orders": []})
    await update.message.reply_text("✅ 모든 수익 기록이 초기화되었습니다.")


async def cmd_pre_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """프리마켓 감시 ON"""
    from config_manager import save_config
    if save_config({"ENABLE_PREMARKET": True}):
        await update.message.reply_text("🕔 **프리마켓 감시가 활성화되었습니다.**\n장 시작 전 목표가 도달 시 알림 및 자동 매도를 시도합니다.")
    else:
        await update.message.reply_text("❌ 설정 변경 실패")


async def cmd_pre_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """프리마켓 감시 OFF"""
    from config_manager import save_config
    if save_config({"ENABLE_PREMARKET": False}):
        await update.message.reply_text("🔕 **프리마켓 감시가 비활성화되었습니다.**")
    else:
        await update.message.reply_text("❌ 설정 변경 실패")


# --- 콜백 및 내부 실행 로직 ---

async def _execute_del_target(event, ticker: str):
    """종목 삭제 실행"""
    from config_manager import STRATEGY_SETTINGS, save_config
    from state_manager import STRATEGY
    
    if ticker in STRATEGY_SETTINGS:
        new_settings = dict(STRATEGY_SETTINGS)
        del new_settings[ticker]
        if save_config({"STRATEGY_SETTINGS": new_settings}):
            # 런타임 상태 및 사이클 상태 정리
            if ticker in STRATEGY: del STRATEGY[ticker]
            utils.delete_ticker_from_all_states(ticker)
            
            await event.edit_message_text(f"✅ {ticker} 종목이 성공적으로 삭제되었습니다.")
            return
    await event.edit_message_text("❌ 삭제 실패")


async def _execute_liquidate(event, ticker: str, ord_type: str = "02"):
    """전량 매도 실행 로직"""
    qty = STRATEGY[ticker]["data"]["qty"]
    if qty <= 0:
        await event.edit_message_text(f"❌ {ticker} 보유 수량이 없습니다.")
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
    content = guide_theory.THEORY_22 if guide_type == "theory_22" else \
              guide_theory.THEORY_30 if guide_type == "theory_30" else \
              guide_theory.THEORY_40 if guide_type == "theory_40" else \
              guide_theory.THEORY_COMPARE if guide_type == "theory_compare" else ""
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
        import traceback
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
        BotCommand("pre_on", "🕔 프리마켓 ON"),
        BotCommand("pre_off", "🔕 프리마켓 OFF"),
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
