"""
텔레그램 메시지 vs 콜백 쿼리 공통 응답 헬퍼.
핸들러에서 reply_text / edit_message_text 분기를 한 곳에서 처리합니다.
"""
from telegram import Update
from telegram.ext import ContextTypes


async def respond(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    query=None,
    parse_mode=None,
    reply_markup=None,
    **kwargs,
) -> None:
    """
    메시지 핸들러인 경우 reply_text, 콜백 쿼리인 경우 edit_message_text로 응답합니다.

    - 메시지: respond(update, context, "메시지")
    - 콜백:   respond(update, context, "메시지", query=update.callback_query)
    """
    if query is not None:
        await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )
    else:
        await update.message.reply_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs,
        )
