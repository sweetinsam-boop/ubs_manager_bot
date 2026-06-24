from __future__ import annotations

import logging
import asyncio

log = logging.getLogger(__name__)

class NotificationService:
    def __init__(self, bot_instance=None):
        self.bot = bot_instance

    def set_bot(self, bot_instance):
        self.bot = bot_instance

    async def broadcast_alert(self, chat_id: str, message: str, parse_mode: str = None, retries: int = 3):
        """ 
        텔레그램 메시지 전송 추상화. 
        실패 시 정해진 횟수만큼 재시도하여 전송 신뢰성을 확보합니다.
        """
        if not self.bot:
            log.warning("NotificationService 봇 인스턴스가 설정되지 않아 메시지 전송 생략: %s", message[:50] + "...")
            return

        # 마크다운/HTML 등 기호에 의한 렌더링 오류 방지를 위해 전처리 권장
        for attempt in range(1, retries + 1):
            try:
                await self.bot.send_message(chat_id=chat_id, text=message, parse_mode=parse_mode)
                if attempt > 1:
                    log.info(f"메시지 전송 성공 (재시도 {attempt}회차)")
                return True
            except Exception as e:
                log.error(f"메시지 전송 실패 (시도 {attempt}/{retries}): {e}")
                if attempt < retries:
                    await asyncio.sleep(2 * attempt) # 지수 백오프 적용
        return False

# 싱글톤 인스턴스
notifier = NotificationService()
