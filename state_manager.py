from config_manager import STRATEGY_SETTINGS

CURRENT_CASH = 0.0
STRATEGY = {}

def normalize_strategy_method(raw) -> str:
    m = str(raw or "2.2").strip().lower()
    if m in ("4.0", "4", "v4", "v4.0"):
        return "4.0"
    if m in ("3.0", "3", "v3", "v3.0"):
        return "3.0"
    return "2.2"


def _parse_one_time_usd(ot):
    if ot is None or str(ot).strip() == "":
        return None
    try:
        return float(ot)
    except (TypeError, ValueError):
        return None


for ticker, s_val in STRATEGY_SETTINGS.items():
    ot = s_val.get("one_time_usd")
    STRATEGY[ticker] = {
        "seed": float(s_val.get("seed", 0.0)),
        "total_a": float(s_val.get("total_a", 20.0)),
        "target_profit": float(s_val.get("target_profit", 10.0)),
        "method": normalize_strategy_method(s_val.get("method")),
        "one_time_usd": _parse_one_time_usd(ot),
        "data": {"avg": 0.0, "qty": 0.0, "cumul": 0.0},
    }

def sync_account():
    """KIS API를 통해 실시간 잔고와 예수금을 동기화합니다."""
    import kis_client
    global CURRENT_CASH
    try:
        token = kis_client.get_kis_access_token()
        if not token:
            return False, "KIS 액세스 토큰을 받지 못했습니다. APP_KEY·APP_SECRET·네트워크를 확인하세요."
        res_bal = kis_client.query_account_raw()
        
        # [방어] API 응답이 정상이 아니면 기존 상태를 보존 (Fail-Safe)
        if not res_bal or res_bal.get('rt_cd') != '0':
            rt_cd_val = res_bal.get('rt_cd') if res_bal else 'None'
            msg1_val = res_bal.get('msg1', '') if res_bal else '응답 없음'
            return False, f"KIS 잔고 조회 실패 (rt_cd={rt_cd_val}: {msg1_val}). 기존 상태를 유지합니다."

        import utils
        for tk in STRATEGY:
            STRATEGY[tk]["data"] = {"avg": 0.0, "qty": 0.0, "cumul": 0.0}

        for item in res_bal.get('output1', []):
            ticker = item['ovrs_pdno'].strip()
            if ticker in STRATEGY:
                avg = float(item['pchs_avg_pric'])
                qty = float(item['ovrs_cblc_qty'])
                STRATEGY[ticker]["data"] = {"avg": avg, "qty": qty, "cumul": avg * qty}

                if qty > 0:
                    utils.update_cycle_info(ticker, start_date=utils.get_us_today())

        import v3_state
        for tk in STRATEGY:
            if STRATEGY[tk].get("method") != "3.0":
                continue
            d = STRATEGY[tk]["data"]
            avg_f = float(d.get("avg", 0) or 0)
            qty_f = float(d.get("qty", 0) or 0)
            v3_state.seed_if_needed(tk, avg_f, qty_f)
            v3_state.reconcile_recent_buys(tk)
            STRATEGY[tk]["data"]["cumul"] = v3_state.get_buy_cumul(tk)

        cash, source = kis_client.query_available_cash(token)
        CURRENT_CASH = cash

        import utils as _utils_lk
        lk = _utils_lk.load_last_known_hold_avgs()
        for tk in STRATEGY:
            d = STRATEGY[tk]["data"]
            q = float(d.get("qty", 0) or 0)
            a = float(d.get("avg", 0) or 0)
            if q > 0 and a > 0:
                lk[tk] = a
        _utils_lk.save_last_known_hold_avgs(lk)

        import strategy
        pending_bits = []
        for tk in STRATEGY:
            if STRATEGY[tk].get("method") == "4.0":
                m = strategy.v4_try_resolve_pending_moc(tk)
                if m:
                    pending_bits.append(m)
        extra = ""
        if pending_bits:
            extra = " | " + " / ".join(pending_bits)
        return True, f"동기화 성공 (잔고: ${cash:,.2f}, {source}){extra}"
    except Exception as e:
        return False, str(e)
