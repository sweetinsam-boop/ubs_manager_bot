from __future__ import annotations

class UIFormatter:
    """텔레그램 메시지 포맷팅 유틸리티"""

    @staticmethod
    def clean_text(text: str) -> str:
        """Markdown 예약어 충돌 방지 등을 위한 텍스트 정규화 (필요시 구현)"""
        return text

    @staticmethod
    def format_alert(title: str, body: str) -> str:
        """강조된 알림 형식"""
        return f"🔔 **{title}**\n\n{body}"

    @staticmethod
    def format_daily_report(data: dict) -> str:
        """/status 종합 리포트 포맷팅"""
        res = f"{data['title']} ({data['us_date_display']})\n"
        res += f"상태: {data['market_session']} | 프리마켓: {'ON' if data['premarket_on'] else 'OFF'}\n"
        res += "--------------------------\n"
        
        if not data["tickers"]:
            res += "현재 감시 중인 종목이 없습니다.\n"
        else:
            for t in data["tickers"]:
                lock_stat = " ✅" if t["locked"] else ""
                res += f"🔹 **{t['ticker']}** ({t['method_disp']}){lock_stat}\n"
                res += f"  - 수익: **{t['pnl_rt']:+.2f}%** (${t['pnl_amt']:,.2f})\n"
                res += f"  - 평단/현재: ${t['avg']:.2f} / ${t['now_p']:.2f}\n"
                res += f"  - 보유: {t['qty']}주 | {t['t_disp']}\n"
                if t["unfilled_cnt"] > 0:
                    res += f"  - ⏳ 미체결: {t['unfilled_cnt']}건 ({t['unfilled_qty']}주)\n"
                res += "\n"

        res += "--------------------------\n"
        res += f"💰 총 평가액: **${data['total_value']:,.2f}**\n"
        res += f"💵 달러 예수금: **${data['current_cash']:,.2f}**"
        return res
