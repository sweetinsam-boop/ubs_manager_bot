"""런타임 종목 상태(STRATEGY)와 계좌 동기화.

STRATEGY[티커] = {seed, total_a, target_profit, data:{avg, qty, cumul}}
  seed          이 종목에 배정한 원금($)
  total_a       분할 수 N (20 또는 40 권장)
  target_profit TQQQ/SOXL 외 종목의 3/4 지정가 익절 목표%
  data          증권사 잔고 기준 평단·수량·보유원가 (sync_account가 채움)
"""
from config_manager import STRATEGY_SETTINGS
from v4_rules import default_pct

CURRENT_CASH = 0.0
STRATEGY = {}


def make_entry(ticker: str, sv: dict, data: dict | None = None) -> dict:
    """trading_settings.json의 종목 설정 → 런타임 엔트리. 익절 %가 없으면 공식 기본값(TQQQ 15, SOXL 20, 그 외 10)."""
    return {
        "seed": float(sv.get("seed", 0.0)),
        "total_a": float(sv.get("total_a", 20.0)),
        "target_profit": float(sv.get("target_profit") or default_pct(ticker)),
        "liquidating": bool(sv.get("liquidating")),  # /liquidate 진행 중 (재시작해도 새 사이클을 열지 않게 설정에 저장)
        "data": data if data is not None else {"avg": 0.0, "qty": 0.0, "cumul": 0.0},
    }


for _ticker, _sv in STRATEGY_SETTINGS.items():
    STRATEGY[_ticker] = make_entry(_ticker, _sv)


def sync_account():
    """KIS API를 통해 실시간 잔고와 예수금을 동기화합니다."""
    import kis_client
    import utils
    global CURRENT_CASH
    try:
        token = kis_client.get_kis_access_token()
        if not token:
            return False, "KIS 액세스 토큰을 받지 못했습니다. APP_KEY·APP_SECRET·네트워크를 확인하세요."

        # exchange_code를 공백("")으로 넘겨 미국 전체 거래소(나스닥, 아멕스, 뉴욕) 잔고를 1회에 일괄 조회합니다.
        res_bal = kis_client.query_account_raw("")

        # [방어] API 응답이 정상이 아니면 기존 상태를 보존한다.
        # 일시 장애 때 보유량을 0으로 덮어쓰면 중복 매수가 발생한다.
        if not res_bal or res_bal.get('rt_cd') != '0':
            rt_cd_val = res_bal.get('rt_cd') if res_bal else 'None'
            msg1_val = res_bal.get('msg1', '') if res_bal else '응답 없음'
            return False, f"KIS 잔고 조회 실패 (rt_cd={rt_cd_val}: {msg1_val}). 기존 상태를 유지합니다."

        # 전량 매도된 종목은 output1에 아예 없으므로, 먼저 모두 0으로 리셋한 뒤 응답으로 채운다.
        prev_qtys = {tk: float(STRATEGY[tk]["data"].get("qty", 0) or 0.0) for tk in STRATEGY}
        for tk in STRATEGY:
            STRATEGY[tk]["data"] = {"avg": 0.0, "qty": 0.0, "cumul": 0.0}

        for item in res_bal.get('output1', []):
            ticker = item['ovrs_pdno'].strip()
            if ticker not in STRATEGY:
                continue
            avg = float(item['pchs_avg_pric'])
            qty = float(item['ovrs_cblc_qty'])
            STRATEGY[ticker]["data"] = {"avg": avg, "qty": qty, "cumul": avg * qty}
            if qty > 0:
                if prev_qtys.get(ticker, 0.0) <= 0:
                    # 0주에서 1주 이상으로 바뀌면 새 사이클 시작(재진입이면 회차 +1)
                    utils.start_cycle_for_new_entry(ticker, increment_on_reentry=True)
                else:
                    utils.update_cycle_info(ticker, start_date=utils.get_us_today())

        cash, source = kis_client.query_available_cash(token)
        CURRENT_CASH = cash

        # 보유 중일 때만 갱신. 전량 매도 후에도 키는 유지(직전 평단) → 장마감 reconcile용.
        lk = utils.load_last_known_hold_avgs()
        for tk in STRATEGY:
            d = STRATEGY[tk]["data"]
            q = float(d.get("qty", 0) or 0)
            a = float(d.get("avg", 0) or 0)
            if q > 0 and a > 0:
                lk[tk] = a
        utils.save_last_known_hold_avgs(lk)

        return True, f"동기화 성공 (잔고: ${cash:,.2f}, {source})"
    except Exception as e:
        return False, str(e)
