"""
무한매수법 V4.0 텔레그램 봇 엔트리포인트.
핸들러 등록은 커맨드/버튼 매핑 테이블로 일원화합니다.
Docker: WORKDIR=/app, 볼륨 마운트 시 프로젝트 루트가 /app 이므로 storage.paths 기준 경로가 /app 하위로 통일됩니다.
"""
from __future__ import annotations

import sys

from telegram import Update
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler, CommandHandler,
                          MessageHandler, TypeHandler, filters)

import kis_client
import state_manager
import telegram_bot
import utils
from bot_logging import get_logger, setup_logging
from config_manager import TELEGRAM_TOKEN

# 순서: 시작/도움말 -> 조회 -> 제어 -> 설정 -> 위험(초기화/청산). /help 텍스트의 분류와 동일하게 유지.
COMMAND_HANDLERS = [
    ("start", telegram_bot.start),
    ("help", telegram_bot.cmd_help),
    ("status", telegram_bot.cmd_status),
    ("report", telegram_bot.cmd_report),
    ("cash", telegram_bot.cmd_cash),
    ("profit_history", telegram_bot.cmd_profit_history),
    ("logs", telegram_bot.cmd_logs),
    ("sync", telegram_bot.cmd_sync),
    ("manual_order", telegram_bot.cmd_manual_order),
    ("set_t", telegram_bot.cmd_set_t),
    ("add_target", telegram_bot.cmd_add_target),
    ("edit_target", telegram_bot.cmd_edit_target),
    ("del_target", telegram_bot.cmd_del_target),
    ("profit_reset", telegram_bot.cmd_profit_reset),
    ("liquidate", telegram_bot.cmd_liquidate),
]

# 하단 메뉴 버튼 텍스트(Regex) -> 같은 기능의 커맨드 핸들러 (이전 버전의 이모지 버튼도 받는다)
BUTTON_ALIASES = [
    (r"^(?:📊 )?현황", telegram_bot.cmd_status),
    (r"^(?:📈 )?(?:수익보고|리포트)", telegram_bot.cmd_report),
    (r"^(?:💰 )?예수금", telegram_bot.cmd_cash),
    (r"^(?:📋 )?수익기록", telegram_bot.cmd_profit_history),
    (r"^(?:신규)?동기화", telegram_bot.cmd_sync),
    (r"^(?:🧾 )?수동주문", telegram_bot.cmd_manual_order),
    (r"^(?:📚 )?도움말", telegram_bot.cmd_help),
]


async def owner_guard(update: Update, context) -> None:
    """등록된 채팅 외의 모든 업데이트를 차단한다. 등록 전에는 /start 만 허용한다."""
    chat = update.effective_chat
    owner = utils.load_chat_id()  # .env의 TELEGRAM_CHAT_ID 우선, 없으면 처음 /start 한 채팅
    if owner is None:
        text = (update.message.text or "") if update.message else ""
        if text.startswith("/start"):
            return
    elif chat is not None and chat.id == owner:
        return
    get_logger("main").warning("차단: 등록되지 않은 채팅의 요청 chat_id=%s", chat.id if chat else None)
    raise ApplicationHandlerStop


def main():
    setup_logging()
    log = get_logger("main")
    log.info("UBS V4.0 bot starting...")

    if not TELEGRAM_TOKEN or not str(TELEGRAM_TOKEN).strip():
        log.error("TELEGRAM_TOKEN 환경변수가 비어 있습니다. setup_wizard.py 를 실행하거나 .env 를 확인하세요.")
        sys.exit(1)

    token = kis_client.get_kis_access_token()
    log.info("KIS token: %s", "OK" if token else "FAILED")
    if token:
        _ok, msg = state_manager.sync_account()
        log.info("startup sync_account: %s", msg)

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(telegram_bot.post_init).build()
    app.add_handler(TypeHandler(Update, owner_guard), group=-1)
    for name, handler in COMMAND_HANDLERS:
        app.add_handler(CommandHandler(name, handler))
    for pattern, handler in BUTTON_ALIASES:
        app.add_handler(MessageHandler(filters.Regex(pattern), handler))
    app.add_handler(MessageHandler(filters.TEXT, telegram_bot.unknown_command))
    app.add_handler(CallbackQueryHandler(telegram_bot.button_callback))
    app.add_error_handler(telegram_bot.error_handler)

    log.info("UBS V4.0 bot is running (polling)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
