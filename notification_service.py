from __future__ import annotations

import logging
from telegram import Bot
from telegram.error import TelegramError

log = logging.getLogger(__name__)

class NotificationService:
    """텔레그램 알림 발송 전담 서비스"""
    def __init__(self):
        self.bot: Bot | None = None

    def set_bot(self, bot: Bot):
        self.bot = bot

    async def broadcast_alert(self, chat_id: int, message: str, parse_mode: str = "Markdown"):
        """지정한 chat_id로 알림 발송"""
        if not self.bot:
            log.warning("Bot 인스턴스가 설정되지 않아 알림을 보낼 수 없습니다.")
            return
        
        try:
            await self.bot.send_message(chat_id=chat_id, text=message, parse_mode=parse_mode)
        except TelegramError as e:
            log.error("알림 발송 실패: %s", e)

notifier = NotificationService()
