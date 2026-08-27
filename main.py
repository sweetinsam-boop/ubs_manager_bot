"""
무한매수 텔레그램 봇 엔트리포인트.
핸들러 등록은 커맨드/버튼 매핑 테이블로 일원화합니다.
Docker: WORKDIR=/app, 볼륨 마운트 시 프로젝트 루트가 /app 이므로 storage.paths 기준 경로가 /app 하위로 통일됩니다.
"""
from __future__ import annotations

import sys
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import Update

from bot_logging import setup_logging, get_logger
from config_manager import TELEGRAM_TOKEN
import telegram_bot
import kis_client
import state_manager

# 커맨드 이름 -> 핸들러 함수
COMMAND_HANDLERS = [
    ("start", telegram_bot.start),
    ("help", telegram_bot.cmd_help),
    ("status", telegram_bot.cmd_status),
    ("sync", telegram_bot.cmd_sync),
    ("manual_order", telegram_bot.cmd_manual_order),
    ("cash", telegram_bot.cmd_cash),
    ("report", telegram_bot.cmd_report),
    ("profit_reset", telegram_bot.cmd_profit_reset),
    ("profit_history", telegram_bot.cmd_profit_history),
    ("add_target", telegram_bot.cmd_add_target),
    ("del_target", telegram_bot.cmd_del_target),
    ("edit_target", telegram_bot.cmd_edit_target),
    ("liquidate", telegram_bot.cmd_liquidate),
    ("logs", telegram_bot.cmd_logs),
]

# 버튼 텍스트(Regex) -> 동일 기능 커맨드 핸들러
BUTTON_ALIASES = [
    (r"^📊 현황보기", telegram_bot.cmd_status),
    (r"^💰 예수금확인", telegram_bot.cmd_cash),
    (r"^신규동기화", telegram_bot.cmd_sync),
    (r"^🧾 수동주문", telegram_bot.cmd_manual_order),
    (r"^📚 도움말", telegram_bot.cmd_help),
    (r"^📈 수익보고", telegram_bot.cmd_report),
    (r"^📋 수익기록", telegram_bot.cmd_profit_history),
]


def main():
    setup_logging()
    log = get_logger("main")
    log.info("UBS00 Refactored Bot starting...")

    if not TELEGRAM_TOKEN or not str(TELEGRAM_TOKEN).strip():
        log.error("TELEGRAM_TOKEN 환경변수가 비어 있습니다. .env 또는 실행 환경을 확인하세요.")
        sys.exit(1)

    t = kis_client.get_kis_access_token()
    log.info("KIS token: %s", "OK" if t else "FAILED")

    if t:
        success, msg = state_manager.sync_account()
        log.info("startup sync_account: %s", msg)

    app_tg = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(telegram_bot.post_init)
        .build()
    )

    for cmd_name, handler in COMMAND_HANDLERS:
        app_tg.add_handler(CommandHandler(cmd_name, handler))

    for pattern, handler in BUTTON_ALIASES:
        app_tg.add_handler(MessageHandler(filters.Regex(pattern), handler))

    # pending_edit_target 등 일반 텍스트 입력은 unknown_command 내부에서만 처리; 미등록 /명령에만 안내 메시지
    app_tg.add_handler(MessageHandler(filters.TEXT, telegram_bot.unknown_command))
    app_tg.add_handler(CallbackQueryHandler(telegram_bot.button_callback))
    app_tg.add_error_handler(telegram_bot.error_handler)

    log.info("UBS00 Trading Bot is running (polling)...")
    app_tg.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
