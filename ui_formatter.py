from __future__ import annotations


class UIFormatter:
    """
    이모지와 텔레그램 마크다운을 전면 배제하여
    시스템 메시지처럼 가독성 높은 리포트를 생성합니다.
    """

    @staticmethod
    def format_daily_report(data: dict) -> str:
        msg = f"{data.get('title', '[현황 요약]')}\n"
        msg += f"기록시점: {data['recorded_at']}\n"
        
        market_label = f"미국 {data['us_date_display']} 정규장 종료" if data['is_closed'] else data['market_session']
        msg += f"시장세션: {market_label}\n"
        msg += "--------------------------\n"

        # 최근 체결 내역
        msg += "[주요 최근 체결 내역]\n"
        if not data.get("recent_orders"):
            msg += "  - 최근 체결 내역 없음\n"
        else:
            # 그룹핑 by ticker
            t_grp = {}
            for o in data["recent_orders"]:
                t_grp.setdefault(o["ticker"], []).append(o)
            for tkr, lst in t_grp.items():
                msg += f"- {tkr}\n"
                for o in lst:
                    dt = f"{o['ord_dt'][:4]}-{o['ord_dt'][4:6]}-{o['ord_dt'][6:8]}" if len(o["ord_dt"]) >= 8 else o["ord_dt"]
                    tag_str = o["tags"]
                    if o["ord_prs"]:
                        tag_str += f"({' ,'.join(f'${p:.2f}' for p in o['ord_prs'])})"
                    msg += f"  [{dt}] {o['side_label']} {o['ccld_qty']}주 체결단가 ${o['avg_fill_pr']:.2f} ({tag_str})\n"
        msg += "\n"

        # 계좌
        msg += "[섹터/종목 요약]\n"
        
        # T 가이드라인 문구
        if "2.2" in data["methods_in_use"]:
            msg += "가이드: V2.2 T는 평단x수량 기준 근사치 표기입니다.\n"
        if "3.0" in data["methods_in_use"]:
            msg += "가이드: V3.0 T는 실제 누적매수액 기준 올림 체계입니다.\n"
        if "4.0" in data["methods_in_use"]:
            msg += "가이드: V4.0 T는 상태파일에 저장되며 체결 시 갱신됩니다.\n"
        msg += "\n"

        for t_info in data["tickers"]:
            msg += f"[{t_info['ticker']}] 무한매수 {t_info['method_disp']} {t_info['cycle_count']}회차 - 보유 {t_info['qty']}주\n"
            msg += f"  {t_info['t_disp']}\n"
            msg += f"  시작:{t_info['start_date']} / 시드:${t_info['seed']:,.0f} / 익절목표:{t_info['target_p']}%\n"
            
            if t_info['qty'] > 0:
                msg += f"  손익: ${t_info['pnl_amt']:,.2f} ({t_info['pnl_rt']:+.2f}%) / 평단 ${t_info['avg']:.2f} / 현재 ${t_info['now_p']:.2f}\n"
            else:
                msg += "  손익: 미보유 상태\n"
                
            lock_label = "LOCKED(주문중복방지 보류)" if t_info['locked'] else "OPEN(주문진입 가능)"
            unf_label = (
                f"당일 미체결잔량 {t_info['unfilled_qty']}주 ({t_info['active_cnt']}건)"
                if t_info["unfilled_cnt"] > 0
                else "당일 미체결 없음"
            )
            msg += f"  상태: {lock_label} / {unf_label}\n\n"

        if data["total_cost"] > 0:
            tot_pnl = data["total_value"] - data["total_cost"]
            tot_rt = (tot_pnl / data["total_cost"]) * 100
            msg += f"[총합 포트폴리오 손익]\n=> ${tot_pnl:,.2f} ({tot_rt:+.2f}%)\n"
        else:
            msg += "[총합 포트폴리오 손익]\n=> 현금 100% 보유\n"

        msg += f"보유 달러 예수금: ${data['current_cash']:,.2f}\n"
        total_asset = data["total_value"] + data["current_cash"]
        msg += f"계좌 총 평가금액: ${total_asset:,.2f}\n"

        return msg

    @staticmethod
    def format_alert(title: str, msg: str) -> str:
        return f"[{title}]\n{msg}"

    @staticmethod
    def clean_text(text: str) -> str:
        """ 잔여 이모지 및 마크다운 기호를 완전히 삭제합니다 """
        import re
        text = text.replace('**', '').replace('`', '').replace('_', '')
        # 자주 쓰인 이모지 전부 공백 제거
        emojis = ['🚀', '💰', '📈', '📊', '📉', '🟢', '🟥', '🟩', '🔔', 'ℹ️', '💤', '🌅', '🎉', '💹', '🧾', '🧮', '🏦', '🔹', '📜', '🔍', '⏳', '✅', '❌', '⚠️', '🚨', '💡', '🛑']
        for e in emojis:
            text = text.replace(e, '')
        return text
