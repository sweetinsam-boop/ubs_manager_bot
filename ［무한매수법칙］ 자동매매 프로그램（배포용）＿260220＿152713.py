#최초 개발자: 승승장군
#마음껏 배포 하셔도 좋으나, 최초 개발자와 배포조건만 삭제하지 말아주세요
#[배포조건]
#최초 개발자는 지우지 않는다
#절대로 라오어 게시판에서만 재배포한다
#절대로 상업적으로 이용하지 않고 무료로만 배포한다
#수정을 했다면 기존 명령은 삭제하지말고 '#'으로 주석처리하여 업데이트 된 내용을 기록 유지한다
#본 코딩은 SOXL과 TQQQ를 각각 목표 12%, 10%를 20회분할로 무한매수하는 버전입니다

import os
import requests
import json
import yfinance as yf
import datetime
import pytz
import math
import pandas_market_calendars as mcal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

TELEGRAM_TOKEN = "따옴표 안에 텔레 봇 토큰을 직접입력 하세요"
APP_KEY = "따옴표 안에 한국투자증권 키를 직접 입력하세요"
APP_SECRET = "따옴표 안에 한국투자증권 시크릿 키를 직접 입력하세요"
CANO = "따옴표 안에 주식계좌 8자리를 직접입력하세요"

ACNT_PRDT_CD = "따옴표 안에 주식계좌 뒷 2자리를 직접 입력하세요. 보통 22 또는 01 입니다"
URL_BASE = "https://openapi.koreainvestment.com:9443"
TOKEN_FILE = "token.dat"

#여기는 시드머니(FIXED_SEED)를 고정시킨 가격 입니다. 사용자님 시드머니를 넣으시면 됩니다.
FIXED_SEED = 00000.0
CURRENT_CASH = 0.0
TRADE_LOCK_FILE = "trade_lock.json"
CHAT_ID_FILE = "chat_id.dat"
CUMUL_PROFIT_FILE = "cumul_profit.json"

def save_chat_id(chat_id):
    with open(CHAT_ID_FILE, 'w') as f:
        f.write(str(chat_id))

def load_chat_id():
    if os.path.exists(CHAT_ID_FILE):
        with open(CHAT_ID_FILE, 'r') as f:
            try:
                return int(f.read().strip())
            except Exception:
                return None
    return None

def get_us_today():
    est = pytz.timezone('US/Eastern')
    return datetime.datetime.now(est).strftime('%Y-%m-%d')

def check_trade_lock(ticker):
    if not os.path.exists(TRADE_LOCK_FILE):
        return False
    try:
        with open(TRADE_LOCK_FILE, 'r') as f:
            locks = json.load(f)
            return locks.get(ticker) == get_us_today()
    except Exception:
        return False

def set_trade_lock(ticker):
    locks = {}
    if os.path.exists(TRADE_LOCK_FILE):
        try:
            with open(TRADE_LOCK_FILE, 'r') as f:
                locks = json.load(f)
        except Exception:
            pass
    locks[ticker] = get_us_today()
    with open(TRADE_LOCK_FILE, 'w') as f:
        json.dump(locks, f)

def load_cumul_profit():
    if os.path.exists(CUMUL_PROFIT_FILE):
        try:
            with open(CUMUL_PROFIT_FILE, 'r') as f:
                data = json.load(f)
                return {
                    "SOXL": float(data.get("SOXL", 0.0)),
                    "TQQQ": float(data.get("TQQQ", 0.0)),
                    "history": data.get("history", []),
                    "processed_orders": data.get("processed_orders", [])
                }
        except Exception:
            pass
    return {"SOXL": 0.0, "TQQQ": 0.0, "history": [], "processed_orders": []}

def save_cumul_profit(data):
    with open(CUMUL_PROFIT_FILE, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def add_cumul_profit(ticker, profit_amount, sell_qty, sell_price, avg_price, order_no=""):
    if sell_qty <= 0 or avg_price <= 0:
        print(f"[cumul_profit] skip invalid: ticker={ticker}, profit={profit_amount}, qty={sell_qty}, avg={avg_price}")
        return load_cumul_profit().get(ticker, 0.0)
    data = load_cumul_profit()
    processed = data.get("processed_orders", [])
    if order_no and order_no in processed:
        print(f"[cumul_profit] skip duplicate order: {order_no}")
        return data.get(ticker, 0.0)
    data[ticker] = data.get(ticker, 0.0) + profit_amount
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.datetime.now(kst).strftime('%Y-%m-%d %H:%M')
    data["history"].append({
        "date": now_kst,
        "ticker": ticker,
        "profit": round(profit_amount, 2),
        "sell_qty": sell_qty,
        "sell_price": round(sell_price, 2),
        "avg_price": round(avg_price, 2),
        "cumul_total": round(data[ticker], 2),
        "order_no": order_no
    })
    if order_no:
        processed.append(order_no)
        if len(processed) > 200:
            processed = processed[-200:]
        data["processed_orders"] = processed
    if len(data["history"]) > 100:
        data["history"] = data["history"][-100:]
    save_cumul_profit(data)
    return data[ticker]

STRATEGY = {
    "SOXL": {
        "seed": FIXED_SEED,
        "total_a": 20.0,
        "target_profit": 12.0,
        "data": {"avg": 0.0, "qty": 0.0, "cumul": 0.0}
    },
    "TQQQ": {
        "seed": FIXED_SEED,
        "total_a": 20.0,
        "target_profit": 10.0,
        "data": {"avg": 0.0, "qty": 0.0, "cumul": 0.0}
    }
}

def is_market_open_today():
    est = pytz.timezone('US/Eastern')
    today_est = datetime.datetime.now(est).date()
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=today_est, end_date=today_est)
    return not schedule.empty

_last_token_renewal = None

def get_kis_access_token(force_renew=False):
    global _last_token_renewal

    if not force_renew and os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, 'r') as f:
                saved = json.load(f)
            expire_time = datetime.datetime.strptime(saved['expire'], '%Y-%m-%d %H:%M:%S')
            if expire_time > datetime.datetime.now() + datetime.timedelta(hours=1):
                return saved['token']
        except Exception:
            pass

    if force_renew and _last_token_renewal:
        elapsed = (datetime.datetime.now() - _last_token_renewal).total_seconds()
        if elapsed < 300:
            if os.path.exists(TOKEN_FILE):
                try:
                    with open(TOKEN_FILE, 'r') as f:
                        saved = json.load(f)
                    print(f"[token] Skipping renewal (last renewal {int(elapsed)}s ago)")
                    return saved['token']
                except Exception:
                    pass

    url = f"{URL_BASE}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    try:
        res = requests.post(url, headers={"content-type": "application/json"}, data=json.dumps(body))
        res_data = res.json()
        new_token = res_data.get('access_token')
        if new_token:
            _last_token_renewal = datetime.datetime.now()
            expire_date = (datetime.datetime.now() + datetime.timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            with open(TOKEN_FILE, 'w') as f:
                json.dump({'token': new_token, 'expire': expire_date}, f)
            print(f"[token] New token issued, expires: {expire_date}")
            return new_token
        else:
            print(f"[token] Token request failed: {res_data}")
    except Exception as e:
        print(f"[token] Token request error: {e}")
    return None

def kis_api_call(method, url, headers, params=None, data=None):
    res = None
    if method == "GET":
        res = requests.get(url, headers=headers, params=params).json()
    elif method == "POST":
        res = requests.post(url, headers=headers, data=json.dumps(data) if data else None).json()

    if res and res.get('msg1', '').find('만료된 token') >= 0:
        print("[kis_api] Token expired, forcing renewal...")
        new_token = get_kis_access_token(force_renew=True)
        if new_token:
            headers = dict(headers)
            headers["authorization"] = f"Bearer {new_token}"
            if method == "GET":
                res = requests.get(url, headers=headers, params=params).json()
            elif method == "POST":
                res = requests.post(url, headers=headers, data=json.dumps(data) if data else None).json()
    return res

def query_account_raw():
    token = get_kis_access_token()
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    headers = {
        "Content-Type": "application/json", "authorization": f"Bearer {token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "JTTT3012R", "custtype": "P"
    }
    params = {
        "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": "NASD",
        "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
    }
    return kis_api_call("GET", url, headers, params=params)

def parse_output2(output2):
    if isinstance(output2, list) and len(output2) > 0:
        output2 = output2[0]
    if not isinstance(output2, dict):
        return {}
    return output2

def query_available_cash(token, debug=False):
    logs = []

    api_attempts = [
        {
            "name": "psamount-JTTT",
            "url": f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-psamount",
            "tr_id": "JTTT3007R",
            "params": {"CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "10", "ITEM_CD": "SOXL"},
            "cash_field": "output.ord_psbl_frcr_amt"
        },
        {
            "name": "psamount-TTTS",
            "url": f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-psamount",
            "tr_id": "TTTS3007R",
            "params": {"CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "10", "ITEM_CD": "SOXL"},
            "cash_field": "output.ord_psbl_frcr_amt"
        },
        {
            "name": "present-CTRP",
            "url": f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-present-balance",
            "tr_id": "CTRP6504R",
            "params": {"CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00"},
            "cash_field": "scan_all"
        },
        {
            "name": "deposit-TTTC",
            "url": f"{URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            "tr_id": "TTTC8908R",
            "params": {"CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "PDNO": "SOXL", "ORD_UNPR": "10", "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "Y"},
            "cash_field": "scan_all"
        },
    ]

    for attempt in api_attempts:
        try:
            headers = {
                "Content-Type": "application/json", "authorization": f"Bearer {token}",
                "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": attempt["tr_id"], "custtype": "P"
            }
            res = kis_api_call("GET", attempt["url"], headers, params=attempt["params"])
            if res is None:
                logs.append(f"{attempt['name']}: ERROR no response")
                continue
            rt_cd = res.get('rt_cd', '?')
            msg1 = res.get('msg1', '')

            if rt_cd == '0':
                if attempt["cash_field"] == "scan_all":
                    all_fields = {}
                    for out_key in ['output', 'output1', 'output2', 'output3']:
                        out = res.get(out_key)
                        if out is None:
                            continue
                        if isinstance(out, list):
                            if len(out) > 0:
                                out = out[0]
                            else:
                                continue
                        if isinstance(out, dict):
                            for k, v in out.items():
                                try:
                                    fv = float(v)
                                    if fv != 0:
                                        all_fields[f"{out_key}.{k}"] = v
                                except:
                                    pass
                    if debug:
                        logs.append(f"{attempt['name']}: OK\n" + "\n".join(f"  {k}: {v}" for k, v in all_fields.items()))
                    o2 = res.get('output2', {})
                    if isinstance(o2, list) and len(o2) > 0:
                        o2 = o2[0]
                    if isinstance(o2, dict):
                        val = float(o2.get('frcr_drwg_psbl_amt_1', 0))
                        if val > 0:
                            logs.append(f"-> 주문가능: ${val:,.2f}")
                            if debug:
                                return val, f"{attempt['name']}", logs
                            return val, f"{attempt['name']}(output2.frcr_drwg_psbl_amt_1)"
                else:
                    parts = attempt["cash_field"].split(".")
                    val = float(res.get(parts[0], {}).get(parts[1], 0))
                    if val > 0:
                        logs.append(f"{attempt['name']}: OK -> ${val:,.2f}")
                        if debug:
                            return val, attempt['name'], logs
                        return val, attempt['name']
                    else:
                        logs.append(f"{attempt['name']}: OK but val=0 (field={attempt['cash_field']})")
            else:
                logs.append(f"{attempt['name']}: FAIL rt_cd={rt_cd} msg={msg1[:80]}")
        except Exception as e:
            logs.append(f"{attempt['name']}: ERROR {str(e)[:80]}")

    if debug:
        return 0.0, "조회실패", logs
    return 0.0, "조회실패"

def sync_account():
    global CURRENT_CASH
    try:
        token = get_kis_access_token()

        url_bal = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
        headers_bal = {
            "Content-Type": "application/json", "authorization": f"Bearer {token}",
            "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "JTTT3012R", "custtype": "P"
        }
        params_bal = {
            "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": "NASD",
            "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
        }
        res_bal = requests.get(url_bal, headers=headers_bal, params=params_bal).json()

        for ticker in STRATEGY:
            STRATEGY[ticker]["data"] = {"avg": 0.0, "qty": 0.0, "cumul": 0.0}

        if res_bal.get('rt_cd') == '0':
            for item in res_bal.get('output1', []):
                ticker = item['ovrs_pdno']
                if ticker in STRATEGY:
                    avg = float(item['pchs_avg_pric'])
                    qty = float(item['ovrs_cblc_qty'])
                    STRATEGY[ticker]["data"] = {"avg": avg, "qty": qty, "cumul": avg * qty}

        cash, source = query_available_cash(token)
        CURRENT_CASH = cash

        return True, f"동기화 성공 (잔고: ${cash:,.2f}, {source})"
    except Exception as e:
        return False, str(e)

EXCHANGE_CODE = {
    "SOXL": "AMEX",
    "TQQQ": "NASD",
}

import time as time_module

def send_order(ticker, qty, price, side="BUY", order_type="00", max_retries=2):
    actual_order_type = order_type
    if order_type == "LOC":
        actual_order_type = "34"
    elif order_type == "LOO":
        actual_order_type = "02"
    elif order_type == "MOC":
        actual_order_type = "33"
    elif order_type == "MOO":
        actual_order_type = "31"

    for attempt in range(max_retries + 1):
        try:
            token = get_kis_access_token()
            url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/order"
            tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
            excg_cd = EXCHANGE_CODE.get(ticker, "NASD")

            headers = {
                "Content-Type": "application/json", "authorization": f"Bearer {token}",
                "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": tr_id, "custtype": "P"
            }

            body = {
                "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg_cd,
                "PDNO": ticker, "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{price:.2f}",
                "CTAC_TLNO": "", "MGCO_APTM_ODNO": "",
                "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": actual_order_type
            }

            res = requests.post(url, headers=headers, data=json.dumps(body))
            result = res.json()

            if result.get('rt_cd') == '0':
                if attempt > 0:
                    result['_retry_info'] = f"{attempt}회 재시도 후 성공"
                return result

            if attempt < max_retries:
                time_module.sleep(1)
                continue

            return result
        except Exception as e:
            if attempt < max_retries:
                time_module.sleep(1)
                continue
            return {"rt_cd": "-1", "msg1": f"통신오류({attempt+1}회시도): {str(e)}"}

def check_order_filled(order_no, ticker, side="SELL", max_checks=10, interval=3):
    for i in range(max_checks):
        try:
            token = get_kis_access_token()
            url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-ccnl"
            kst = pytz.timezone('Asia/Seoul')
            today_str = datetime.datetime.now(kst).strftime('%Y%m%d')
            two_days_ago = (datetime.datetime.now(kst) - datetime.timedelta(days=2)).strftime('%Y%m%d')
            excg_cd = EXCHANGE_CODE.get(ticker, "NASD")
            headers = {
                "Content-Type": "application/json", "authorization": f"Bearer {token}",
                "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "JTTT3001R", "custtype": "P"
            }
            params = {
                "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD,
                "PDNO": ticker, "ORD_STRT_DT": two_days_ago, "ORD_END_DT": today_str,
                "SLL_BUY_DVSN": "01" if side == "SELL" else "02",
                "CCLD_NCCS_DVSN": "01",
                "OVRS_EXCG_CD": excg_cd, "SORT_SQN": "DS",
                "ORD_GNO_BRNO": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
            }
            res = kis_api_call("GET", url, headers, params=params)
            if res and res.get('rt_cd') == '0':
                for o in res.get('output', []):
                    if o.get('odno') == order_no or o.get('orgn_odno') == order_no:
                        filled_qty = int(float(o.get('ft_ccld_qty', '0')))
                        if filled_qty > 0:
                            filled_price = float(o.get('ft_ccld_unpr3', o.get('ft_ccld_unpr', '0')))
                            print(f"[fill_check] {ticker} order {order_no} filled: {filled_qty}주 @ ${filled_price:.2f}")
                            return True, filled_qty, filled_price
        except Exception as e:
            print(f"[fill_check] Error checking order {order_no}: {e}")
        if i < max_checks - 1:
            time_module.sleep(interval)
    return False, 0, 0

PRICE_EXCHANGE_CODE = {
    "SOXL": "AMS",
    "TQQQ": "NAS",
}

def is_us_market_open():
    est = pytz.timezone('US/Eastern')
    now = datetime.datetime.now(est)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()
    if weekday >= 5:
        return False
    if (hour == 9 and minute >= 30) or (hour >= 10 and hour < 16):
        return True
    return False

def get_market_session():
    est = pytz.timezone('US/Eastern')
    now = datetime.datetime.now(est)
    hour = now.hour
    minute = now.minute
    weekday = now.weekday()

    if weekday >= 5:
        return "closed"
    if (hour == 9 and minute >= 30) or (hour >= 10 and hour < 16):
        return "regular"
    if hour >= 16 and hour < 20:
        return "aftermarket"
    if hour >= 4 and (hour < 9 or (hour == 9 and minute < 30)):
        return "premarket"
    return "closed"

def get_market_session_kr():
    session = get_market_session()
    return {"regular": "정규장 거래 중", "aftermarket": "애프터마켓", "premarket": "프리마켓", "closed": "장 마감"}.get(session, "장 마감")

# [수정 전: 데이터 사용량 과다 방식]
# def get_current_price(ticker):
#     session = get_market_session()
#     if session == "regular":
#         try:
#             token = get_kis_access_token()
#             excg = PRICE_EXCHANGE_CODE.get(ticker, "NAS")
#             url = f"{URL_BASE}/uapi/overseas-price/v1/quotations/price"
#             headers = {"Content-Type": "application/json", "authorization": f"Bearer {token}", "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "HHDFS76200200", "custtype": "P"}
#             params = {"AUTH": "", "EXCD": excg, "SYMB": ticker}
#             res = requests.get(url, headers=headers, params=params).json()
#             if res.get('rt_cd') == '0':
#                 output = res.get('output', {})
#                 last = float(output.get('last', '0'))
#                 if last > 0: return last
#         except Exception: pass
#     try:
#         stock = yf.Ticker(ticker)
#         info = stock.info  # <--- 이 부분이 수백 줄의 데이터를 가져와 트래픽을 유발함
#         regular_price = info.get('currentPrice') or info.get('regularMarketPrice')
#         if regular_price and float(regular_price) > 0: return float(regular_price)
#     except Exception: pass
#     return 0.0

# [수정 후: GCP 무료 티어 최적화 방식]
def get_current_price(ticker):
    # KIS API 로직은 동일하게 유지 (가장 정확한 가격)
    session = get_market_session()
    if session == "regular":
        try:
            token = get_kis_access_token()
            excg = PRICE_EXCHANGE_CODE.get(ticker, "NAS")
            url = f"{URL_BASE}/uapi/overseas-price/v1/quotations/price"
            headers = {"Content-Type": "application/json", "authorization": f"Bearer {token}", "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "HHDFS76200200", "custtype": "P"}
            params = {"AUTH": "", "EXCD": excg, "SYMB": ticker}
            res = requests.get(url, headers=headers, params=params).json()
            if res.get('rt_cd') == '0':
                last = float(res.get('output', {}).get('last', '0'))
                if last > 0: return last
        except Exception: pass

    # yfinance 호출 시 .info 대신 .fast_info를 사용하여 필요한 현재가만 수신
    try:
        stock = yf.Ticker(ticker)
        return stock.fast_info['last_price'] 
    except Exception:
        return 0.0

def get_prev_close(ticker):
    try:
        token = get_kis_access_token()
        url = f"{URL_BASE}/uapi/overseas-price/v1/quotations/price"
        excg = PRICE_EXCHANGE_CODE.get(ticker, "NAS")
        headers = {
            "Content-Type": "application/json", "authorization": f"Bearer {token}",
            "appkey": APP_KEY, "appsecret": APP_SECRET,
            "tr_id": "HHDFS76200200", "custtype": "P"
        }
        params = {"AUTH": "", "EXCD": excg, "SYMB": ticker}
        res = requests.get(url, headers=headers, params=params).json()
        if res.get('rt_cd') == '0':
            output = res.get('output', {})
            base = float(output.get('base', '0'))
            if base > 0:
                return base
    except Exception:
        pass
    stock = yf.Ticker(ticker)
    hist = stock.history(period="5d")
    if len(hist) >= 2:
        return hist['Close'].iloc[-2]
    return hist['Close'].iloc[-1]

def generate_ticker_message(ticker):
    s = STRATEGY[ticker]
    d = s["data"]

    one_time_amt = s["seed"] / s["total_a"]
    half_amt = one_time_amt / 2.0

    try:
        curr_price = get_current_price(ticker)

        avg_price = d["avg"]
        qty_total = int(d["qty"])
        profit_rate = (curr_price - avg_price) / avg_price * 100 if avg_price > 0 else 0.0

        t_val = round(d["cumul"] / one_time_amt, 2) if one_time_amt > 0 and d["cumul"] > 0 else 0.0
        star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]
        star_price = avg_price * (1 + star_pct / 100) if avg_price > 0 else curr_price * 1.05

        target_price = avg_price * (1 + s["target_profit"] / 100) if avg_price > 0 else curr_price * (1 + s["target_profit"] / 100)
        qty_3_4 = math.floor(qty_total * 0.75)
        qty_1_4 = qty_total - qty_3_4

        msg = f"[{ticker} 지시서 - 목표 {s['target_profit']}%]\n"
        msg += f"현재가: ${curr_price:.2f} / 평단: ${avg_price:.2f} ({profit_rate:.2f}%)\n"
        msg += f"수량: {qty_total}주 / 회차: {t_val}T / 별%: {star_pct:.2f}%\n"
        msg += f"시드: ${s['seed']:.0f} (1회분: ${one_time_amt:.0f})\n"
        msg += f"--------------------------\n"

        if qty_total == 0:
            msg += f"[첫 매수] 1회분(${one_time_amt:.0f}) 약 {math.floor(one_time_amt / curr_price)}주 매수\n"
        elif t_val > (s["total_a"] - 1):
            msg += f"[매도] (쿼터손절 진행)\n"
            msg += f"1. [MOC 매도] 1/4 물량 ({qty_1_4}주)을 무조건 매도(MOC)\n"
            msg += f"2. [지정가 매도] 잔여 3/4 물량 ({qty_3_4}주)을 목표가 ${target_price:.2f} (+{s['target_profit']}%)에 예약\n"
        else:
            midpoint = s["total_a"] / 2
            is_first_half = (t_val <= midpoint)

            if is_first_half:
                b2 = math.floor(half_amt / star_price) if star_price > 0 else 0
                b1 = math.floor(half_amt / (avg_price if avg_price > 0 else curr_price)) + 1
                msg += f"[매수 - 전반전 (T≤{int(midpoint)})]\n"
                msg += f"1. [LOC] 별%가격 ${star_price:.2f} 에 -> {b2}주 (0.5회분)\n"
                msg += f"2. [LOC] 평단가 ${avg_price:.2f} 에 -> {b1}주 (0.5회분+1주)\n\n"
            else:
                b2 = math.floor(one_time_amt / star_price) if star_price > 0 else 0
                msg += f"[매수 - 후반전 (T>{int(midpoint)})]\n"
                msg += f"1. [LOC] 별%가격 ${star_price:.2f} 에 -> {b2}주 (1회분 전체)\n\n"

            try:
                prev_close = get_prev_close(ticker)
                base_price = min(avg_price, prev_close)
                turbo_price = base_price * 0.95
                turbo_qty = math.floor(one_time_amt / turbo_price)
                msg += f"[가속매수 - 1회분 추가 LOC]\n"
                msg += f"-> min(평단${avg_price:.2f}, 전일종가${prev_close:.2f})x0.95 = ${turbo_price:.2f} 에 {turbo_qty}주\n\n"
            except Exception:
                msg += f"[가속매수] 전일종가 조회 실패\n\n"

            base_qty = math.floor(one_time_amt / avg_price) if avg_price > 0 else 0
            msg += f"[줍줍 - 폭락장 대비 추가 매수]\n"
            for i in range(1, 7):
                grid_p = round(one_time_amt / (base_qty + i), 2)
                if grid_p <= 0:
                    break
                msg += f"  - LOC ${grid_p:.2f} x 1주\n"

            msg += f"\n[매도]\n"
            msg += f"1. [지정가] 3/4물량({qty_3_4}주) -> ${target_price:.2f} (+{s['target_profit']}%)\n"
            msg += f"2. [LOC] 1/4물량({qty_1_4}주) -> ${star_price + 0.01:.2f} (별%+0.01)\n"

        return msg + "\n"
    except Exception:
        return f"{ticker} 데이터 오류\n"

def build_order_info(ticker, no_turbo=False):
    s = STRATEGY[ticker]
    d = s["data"]

    one_time_amt = s["seed"] / s["total_a"]
    half_amt = one_time_amt / 2.0

    avg_price = d.get("avg", 0)
    qty_total = int(d.get("qty", 0))

    t_val = round(d.get("cumul", 0) / one_time_amt, 2) if one_time_amt > 0 and d.get("cumul", 0) > 0 else 0.0
    star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]
    star_price = avg_price * (1 + star_pct / 100) if avg_price > 0 else 0

    target_price = avg_price * (1 + s["target_profit"] / 100) if avg_price > 0 else 0
    qty_3_4 = math.floor(qty_total * 0.75)
    qty_1_4 = qty_total - qty_3_4

    midpoint = s["total_a"] / 2
    is_first_half = (t_val <= midpoint)

    if is_first_half:
        b2 = math.floor(half_amt / star_price) if star_price > 0 else 0
        b1 = (math.floor(half_amt / avg_price) + 1) if avg_price > 0 else 0
    else:
        b2 = math.floor(one_time_amt / star_price) if star_price > 0 else 0
        b1 = 0

    turbo_price = 0.0
    turbo_qty = 0
    if not no_turbo and avg_price > 0 and qty_total > 0:
        try:
            prev_close = get_prev_close(ticker)
            base_price = min(avg_price, prev_close)
            turbo_price = base_price * 0.95
            turbo_qty = math.floor(one_time_amt / turbo_price)
        except Exception:
            pass

    force_quarter_sell = False
    if t_val >= 19:
        force_quarter_sell = True

    return {
        "avg_price": avg_price,
        "star_price": star_price,
        "target_price": target_price,
        "b1": b1,
        "b2": b2,
        "qty_total": qty_total,
        "qty_3_4": qty_3_4,
        "qty_1_4": qty_1_4,
        "t_val": t_val,
        "is_first_half": is_first_half,
        "turbo_price": turbo_price,
        "turbo_qty": turbo_qty,
        "no_turbo": no_turbo,
        "force_quarter_sell": force_quarter_sell,
    }

def estimate_required_amount(info, seed=None, total_a=None):
    total = 0.0
    if info.get("force_quarter_sell"):
        return 0.0
    if info["b1"] > 0:
        total += info["b1"] * info["avg_price"]
    if info["b2"] > 0:
        total += info["b2"] * info["star_price"]
    if info["turbo_qty"] > 0:
        total += info["turbo_qty"] * info["turbo_price"]
    if info["avg_price"] > 0 and seed and total_a:
        ota = seed / total_a
        bq = math.floor(ota / info["avg_price"])
        for i in range(1, 7):
            gp = ota / (bq + i)
            if gp <= 0:
                break
            total += gp
    return total

async def cmd_cash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("주문가능금액 조회 중...")
    try:
        token = get_kis_access_token()
        if not token:
            await update.message.reply_text("KIS API 토큰 발급 실패.\nAPP_KEY/APP_SECRET 확인 필요.")
            return
        usd_cash, cash_source, debug_logs = query_available_cash(token, debug=True)

        data = query_account_raw()
        rt_cd = data.get('rt_cd', 'N/A')
        msg1 = data.get('msg1', '')
        if rt_cd != '0':
            print(f"[cmd_cash] KIS API error: rt_cd={rt_cd}, msg1={msg1}")
        o2 = parse_output2(data.get('output2', {})) if rt_cd == '0' else {}
        ovrs_tot_pfls = o2.get('ovrs_tot_pfls', '0')

        msg = f"[예수금 조회]\n"
        msg += f"주문가능금액: ${usd_cash:,.2f}\n"
        msg += f"해외 총손익: ${float(ovrs_tot_pfls):,.2f}\n"

        holdings = data.get('output1', []) if data.get('rt_cd') == '0' else []
        all_stock_evl = 0.0
        if holdings:
            msg += "\n[보유 종목]\n"
            for item in holdings:
                sym = item.get('ovrs_pdno', '?')
                qty_str = item.get('ovrs_cblc_qty', '0')
                avg = item.get('pchs_avg_pric', '0')
                qty_f = float(qty_str)
                avg_f = float(avg)
                try:
                    now_f = get_current_price(sym)
                    if now_f <= 0:
                        now_f = float(item.get('now_pric2', item.get('ovrs_now_pric1', '0')))
                except Exception:
                    now_f = float(item.get('now_pric2', item.get('ovrs_now_pric1', '0')))
                pnl = now_f * qty_f - avg_f * qty_f
                pnl_rt = ((now_f - avg_f) / avg_f * 100) if avg_f > 0 else 0.0
                all_stock_evl += now_f * qty_f
                in_strategy = " *" if sym in STRATEGY else ""
                msg += f"  {sym}{in_strategy}: {qty_str}주 / 평단${avg_f:,.2f} / 현재${now_f:,.2f} / 손익${pnl:,.2f}({pnl_rt:.1f}%)\n"

        msg += f"\n현금: ${usd_cash:,.2f} / 주식: ${all_stock_evl:,.2f}\n"
        msg += f"시드(고정): ${FIXED_SEED:,.2f}\n"
        await update.message.reply_text(msg)

        if usd_cash < 0.01 and all_stock_evl < 0.01:
            dbg = f"[API 디버그]\n"
            dbg += f"토큰: {'OK' if token else 'FAIL'}\n"
            dbg += f"APP_KEY: {'설정됨' if APP_KEY else '미설정'}\n"
            dbg += f"APP_SECRET: {'설정됨' if APP_SECRET else '미설정'}\n"
            dbg += f"CANO: {'설정됨' if CANO else '미설정'}\n"
            dbg += f"계좌조회: {'성공' if rt_cd == '0' else f'실패({rt_cd}: {msg1[:60]})'}\n"
            dbg += f"출처: {cash_source}\n"
            if debug_logs:
                for dl in debug_logs[-5:]:
                    dbg += f"  {str(dl)[:100]}\n"
            await update.message.reply_text(dbg)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[cmd_cash] Exception: {tb}")
        await update.message.reply_text(f"조회 오류: {str(e)}")

async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    locked_tickers = [t for t in STRATEGY if check_trade_lock(t)]
    if len(locked_tickers) == len(STRATEGY):
        lock_msg = "[안전장치] 모든 종목이 오늘(미국장 기준) 이미 매매 완료되어 잠금 상태입니다.\n\n"
        lock_msg += "잠금 종목: " + ", ".join(locked_tickers) + "\n\n"
        lock_msg += "중복 주문을 방지하기 위해 수동 주문이 차단됩니다.\n"
        lock_msg += "잠금 해제가 필요하면 /reset 명령을 사용하세요."
        await update.message.reply_text(lock_msg)
        return

    if locked_tickers:
        await update.message.reply_text(f"[안전장치] 잠금 종목: {', '.join(locked_tickers)} (오늘 매매 완료)\n잠금되지 않은 종목만 주문합니다.")

    await update.message.reply_text("계좌 통합 동기화 및 상세 지시서 작성 중...")
    result = sync_account()
    success = result[0]
    res_msg = result[1]

    if not success:
        await update.message.reply_text(f"동기화 실패: {res_msg}")
        return

    target_hit = {}
    for ticker in STRATEGY:
        s = STRATEGY[ticker]
        d = s["data"]
        avg_p = d.get("avg", 0)
        qty_t = int(d.get("qty", 0))
        if avg_p > 0 and qty_t > 0:
            target_p = avg_p * (1 + s["target_profit"] / 100)
            try:
                curr_p = get_current_price(ticker)
                if curr_p >= target_p:
                    target_hit[ticker] = {"curr": curr_p, "target": target_p, "avg": avg_p, "qty": qty_t, "profit_pct": s["target_profit"]}
            except Exception:
                pass

    if target_hit:
        hit_msg = "목표가 돌파 감지! (전량 익절 + 즉시 재진입)\n\n"
        hit_keyboard = []
        for ticker, h in target_hit.items():
            profit_rt = (h["curr"] - h["avg"]) / h["avg"] * 100
            one_time = STRATEGY[ticker]["seed"] / STRATEGY[ticker]["total_a"]
            entry_p = round(h["curr"] * 1.15, 2)
            entry_qty = math.floor(one_time / h["curr"])
            hit_msg += f"[{ticker} 목표가 달성! 전량 익절 & 재진입]\n"
            hit_msg += f"  평단: ${h['avg']:.2f} / 현재가: ${h['curr']:.2f} ({profit_rt:.2f}%)\n"
            hit_msg += f"  1단계(전량매도): {h['qty']}주 지정가(${h['target']:.2f}) 매도\n"
            hit_msg += f"  2단계(LOC재진입): {entry_qty}주 상향가(${entry_p:.2f}) LOC 매수\n\n"
            cb = f"profit:{ticker}:{h['qty']}:{h['target']:.2f}:{entry_qty}:{entry_p:.2f}"
            hit_keyboard.append([InlineKeyboardButton(f"{ticker} 익절+재진입 실행", callback_data=cb)])
        await update.message.reply_text(hit_msg, reply_markup=InlineKeyboardMarkup(hit_keyboard))

        remaining_tickers = [t for t in STRATEGY if t not in target_hit]
        if not remaining_tickers:
            return

        await update.message.reply_text("목표가 미달 종목 지시서 작성 중...")

    no_turbo = False
    adjustment_stage = 0

    all_infos = {}
    for ticker in STRATEGY:
        if ticker in target_hit:
            continue
        all_infos[ticker] = build_order_info(ticker)

    if not all_infos:
        return

    total_required = sum(estimate_required_amount(all_infos[t], STRATEGY[t]["seed"], STRATEGY[t]["total_a"]) for t in all_infos)

    if CURRENT_CASH > 0 and total_required > CURRENT_CASH:
        adjustment_stage = 1
        no_turbo = True
        for ticker in all_infos:
            all_infos[ticker] = build_order_info(ticker, no_turbo=True)
        total_required = sum(estimate_required_amount(all_infos[t], STRATEGY[t]["seed"], STRATEGY[t]["total_a"]) for t in all_infos)

    if CURRENT_CASH > 0 and total_required > CURRENT_CASH and adjustment_stage == 1:
        adjustment_stage = 2
        for ticker in all_infos:
            info = all_infos[ticker]
            if info["qty_total"] > 0:
                info["force_quarter_sell"] = True
                info["qty_3_4"] = math.floor(info["qty_total"] * 0.75)
                info["qty_1_4"] = info["qty_total"] - info["qty_3_4"]
        total_required = sum(estimate_required_amount(all_infos[t], STRATEGY[t]["seed"], STRATEGY[t]["total_a"]) for t in all_infos)

    final_msg = f"무한매수 V2.2 통합 지시서\n"
    final_msg += f"보유달러: ${CURRENT_CASH:,.2f} / 시드: ${FIXED_SEED:,.0f}\n"
    if adjustment_stage > 0:
        final_msg += f"[자금조정 {adjustment_stage}단계 적용]\n"
        if adjustment_stage >= 1:
            final_msg += f"  1단계: 가속매수 제외\n"
        if adjustment_stage >= 2:
            final_msg += f"  2단계: 잔금소진 -> 쿼터매도 발동\n"
    final_msg += "\n"

    for ticker in all_infos:
        info = all_infos[ticker]
        s = STRATEGY[ticker]
        d = s["data"]
        one_time_amt = s["seed"] / s["total_a"]

        if info["force_quarter_sell"] and info["qty_total"] > 0:
            reason = f"T={info['t_val']:.1f}>=19" if info["t_val"] >= 19 else "잔금소진"
            final_msg += f"[{ticker} - 쿼터매도 ({reason})]\n"
            final_msg += f"평단: ${info['avg_price']:.2f} / 수량: {info['qty_total']}주\n"
            final_msg += f"1. [MOC 매도] 1/4물량({info['qty_1_4']}주) 무조건 매도\n"
            final_msg += f"2. [지정가 매도] 3/4물량({info['qty_3_4']}주) -> ${info['target_price']:.2f}(+{s['target_profit']}%)\n"
            final_msg += f"  -> 매수 없음 (쿼터매도 진행)\n\n"
        else:
            final_msg += generate_ticker_message(ticker)
            req = estimate_required_amount(info, s["seed"], s["total_a"])
            if req > 0:
                turbo_label = "" if no_turbo else " + 가속"
                final_msg += f"  -> 필요금액(1회분{turbo_label}): ${req:,.2f}\n\n"

    if total_required > 0:
        final_msg += f"==========================\n"
        final_msg += f"전체 매수 필요금액: ${total_required:,.2f}\n"
        final_msg += f"보유 달러: ${CURRENT_CASH:,.2f}\n"
        if CURRENT_CASH >= total_required:
            final_msg += f"잔고 충분 (여유: ${CURRENT_CASH - total_required:,.2f})\n"
        else:
            shortage = total_required - CURRENT_CASH
            final_msg += f"!!! 잔고 부족: ${shortage:,.2f} 모자람 !!!\n"
        final_msg += f"==========================\n"

    await update.message.reply_text(final_msg)

    keyboard = []
    order_summary = "[수동 주문] 버튼을 눌러 즉시 주문할 수 있습니다.\n(18:30 자동매매와 별도로 수동 실행용)\n"
    if adjustment_stage > 0:
        order_summary += f"(자금조정 {adjustment_stage}단계 적용됨)\n"
    order_summary += "\n"

    for ticker in all_infos:
        info = all_infos[ticker]

        if info["force_quarter_sell"] and info["qty_total"] > 0:
            reason = f"T={info['t_val']:.1f}>=19" if info["t_val"] >= 19 else "잔금소진"
            order_summary += f"{ticker}: 쿼터매도 ({reason}) / 매도 {info['qty_3_4']}+{info['qty_1_4']}주\n"
            cb_sell = f"qsell:{ticker}:{info['qty_3_4']}:{info['target_price']:.2f}:{info['qty_1_4']}"
            keyboard.append([InlineKeyboardButton(f"{ticker} 쿼터매도 실행 ({reason})", callback_data=cb_sell)])
            continue

        if info["qty_total"] == 0:
            try:
                curr_price = get_current_price(ticker)
                one_time_amt = STRATEGY[ticker]["seed"] / STRATEGY[ticker]["total_a"]
                entry_amt = one_time_amt
                entry_price = curr_price * 1.15
                entry_qty = math.floor(entry_amt / curr_price)
                order_summary += f"{ticker}: 신규 진입 {entry_qty}주 (${entry_price:.2f} LOC)\n"
                cb_entry = f"entry:{ticker}:{entry_qty}:{entry_price:.2f}"
                keyboard.append([InlineKeyboardButton(f"{ticker} 최초 매수 실행 ({entry_qty}주)", callback_data=cb_entry)])
            except Exception:
                pass
            continue

        if info["t_val"] > (STRATEGY[ticker]["total_a"] - 1):
            continue

        turbo_txt = ""
        if info["turbo_qty"] > 0:
            turbo_txt = f" / 가속 {info['turbo_qty']}주"
        half_label = "전반전" if info.get("is_first_half", True) else "후반전"
        if info["b1"] > 0:
            buy_txt = f"매수 {info['b1']}+{info['b2']}주"
        else:
            buy_txt = f"매수 {info['b2']}주(별%)"
        order_summary += f"{ticker}({half_label}): {buy_txt}{turbo_txt} / 줍줍 6주 / 매도 {info['qty_3_4']}+{info['qty_1_4']}주\n"
        cb_all = f"all:{ticker}:{info['b1']}:{info['avg_price']:.2f}:{info['b2']}:{info['star_price']:.2f}:{info['qty_3_4']}:{info['target_price']:.2f}:{info['qty_1_4']}:{info['turbo_qty']}:{info['turbo_price']:.2f}"
        keyboard.append([InlineKeyboardButton(f"{ticker} 통합 주문 실행", callback_data=cb_all)])

    if keyboard:
        await update.message.reply_text(order_summary, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    action = parts[0]
    ticker = parts[1]

    if check_trade_lock(ticker):
        lock_msg = f"[안전장치 - 중복 주문 차단]\n\n"
        lock_msg += f"{ticker} 종목은 오늘(미국장 기준) 이미 매매가 접수되었습니다.\n"
        lock_msg += f"중복 주문을 차단했습니다.\n\n"
        lock_msg += f"잠금 해제가 필요하면 /reset 명령을 사용하세요."
        await query.edit_message_text(text=lock_msg)
        return

    if action == "profit":
        sell_qty = int(parts[2])
        sell_price = float(parts[3])
        buy_qty = int(parts[4])
        buy_price = float(parts[5])
        await query.edit_message_text(text=f"{ticker} 익절+재진입 주문 전송 중...")
        result_msg = f"{ticker} 익절+재진입 결과\n\n"

        result_msg += f"[1단계: 전량 지정가 매도]\n"
        res_sell = send_order(ticker, sell_qty, sell_price, "SELL", "00")
        if res_sell.get('rt_cd') == '0':
            result_msg += f"  {sell_qty}주 x ${sell_price:.2f}: 성공\n"
        else:
            result_msg += f"  {sell_qty}주 x ${sell_price:.2f}: 실패({res_sell.get('msg1', '')})\n"

        result_msg += f"\n[2단계: LOC 재진입 매수]\n"
        res_buy = send_order(ticker, buy_qty, buy_price, "BUY", "LOC")
        if res_buy.get('rt_cd') == '0':
            result_msg += f"  {buy_qty}주 x ${buy_price:.2f} (LOC): 성공\n"
        else:
            result_msg += f"  {buy_qty}주 x ${buy_price:.2f} (LOC): 실패({res_buy.get('msg1', '')})\n"

        set_trade_lock(ticker)
        await query.edit_message_text(text=result_msg)
        return

    if action == "tgtsell":
        sell_qty = int(parts[2])
        sell_price = float(parts[3])
        await query.edit_message_text(text=f"{ticker} 목표가 돌파 전량 매도 주문 전송 중...")
        result_msg = f"{ticker} 목표가 돌파 매도 결과\n\n"
        res = send_order(ticker, sell_qty, sell_price, "SELL", "00")
        if res.get('rt_cd') == '0':
            result_msg += f"지정가 매도 성공: {sell_qty}주 x ${sell_price:.2f}\n"
            set_trade_lock(ticker)
        else:
            result_msg += f"지정가 매도 실패: {res.get('msg1', '알수없는오류')}\n"
        await query.edit_message_text(text=result_msg)
        return

    if action == "entry":
        entry_qty = int(parts[2])
        entry_price = float(parts[3])

        required = entry_qty * entry_price
        if CURRENT_CASH > 0 and CURRENT_CASH < required:
            alert_msg = f"[주문 중단: 예수금 부족]\n"
            alert_msg += f"필요 금액: ${required:.2f}\n"
            alert_msg += f"현재 잔액: ${CURRENT_CASH:.2f}\n"
            alert_msg += f"부족한 달러를 입금하거나 주문을 조정하세요."
            await query.edit_message_text(text=alert_msg)
            return

        await query.edit_message_text(text=f"{ticker} 최초 매수(+15% LOC) 주문 전송 중...")

        result_msg = f"{ticker} 최초 매수 결과\n\n"
        if entry_qty > 0:
            res = send_order(ticker, entry_qty, entry_price, "BUY", "LOC")
            if res.get('rt_cd') == '0':
                result_msg += f"주문수량: {entry_qty}주 / 주문가: ${entry_price:.2f}\n"
                result_msg += f"결과: 성공\n"
            else:
                result_msg += f"주문수량: {entry_qty}주 / 주문가: ${entry_price:.2f}\n"
                result_msg += f"결과: 실패({res.get('msg1', '')})\n"
        else:
            result_msg += "주문 수량이 0주입니다.\n"

        set_trade_lock(ticker)
        await context.bot.send_message(chat_id=query.message.chat_id, text=result_msg)
        return

    elif action == "all":
        b1_qty = int(parts[2])
        avg_p = float(parts[3])
        b2_qty = int(parts[4])
        star_p = float(parts[5])
        q_3_4 = int(parts[6])
        target_p = float(parts[7])
        q_1_4 = int(parts[8])
        turbo_qty = int(parts[9]) if len(parts) > 9 else 0
        turbo_p = float(parts[10]) if len(parts) > 10 else 0.0

        required_amt = 0.0
        if b1_qty > 0:
            required_amt += b1_qty * avg_p
        if b2_qty > 0:
            required_amt += b2_qty * star_p
        if turbo_qty > 0 and turbo_p > 0:
            required_amt += turbo_qty * turbo_p
        if avg_p > 0:
            s_req = STRATEGY.get(ticker, {})
            req_one_time = s_req.get("seed", FIXED_SEED/2) / s_req.get("total_a", 20)
            req_base_qty = math.floor(req_one_time / avg_p)
            for ri in range(1, 7):
                rp = req_one_time / (req_base_qty + ri)
                if rp > 0:
                    required_amt += rp

        if CURRENT_CASH > 0 and CURRENT_CASH < required_amt:
            alert_msg = f"[주문 중단: 예수금 부족]\n"
            alert_msg += f"필요 금액(추정): ${required_amt:.2f}\n"
            alert_msg += f"현재 잔액: ${CURRENT_CASH:.2f}\n"
            alert_msg += f"부족한 달러를 입금하거나 쿼터손절을 검토하세요."
            await query.edit_message_text(text=alert_msg)
            return

        await query.edit_message_text(text=f"{ticker} 통합 주문 전송 중 (매수+가속+줍줍+매도)...")

        result_msg = f"{ticker} 통합 주문 결과\n"
        result_msg += f"(예수금: ${CURRENT_CASH:.2f})\n\n"

        buy_success = 0
        buy_total = 0

        result_msg += "[기본 매수 - LOC]\n"
        if b1_qty > 0:
            res1 = send_order(ticker, b1_qty, avg_p, "BUY", "LOC")
            buy_total += 1
            if res1.get('rt_cd') == '0':
                buy_success += 1
                result_msg += f"1. 평단가(${avg_p:.2f}) {b1_qty}주: 성공\n"
            else:
                result_msg += f"1. 평단가(${avg_p:.2f}) {b1_qty}주: 실패({res1.get('msg1', '')})\n"

        if b2_qty > 0:
            res2 = send_order(ticker, b2_qty, star_p, "BUY", "LOC")
            buy_total += 1
            if res2.get('rt_cd') == '0':
                buy_success += 1
                result_msg += f"2. 별가(${star_p:.2f}) {b2_qty}주: 성공\n"
            else:
                result_msg += f"2. 별가(${star_p:.2f}) {b2_qty}주: 실패({res2.get('msg1', '')})\n"

        turbo_success = 0
        if turbo_qty > 0 and turbo_p > 0:
            result_msg += f"\n[가속 매수 - LOC]\n"
            res_turbo = send_order(ticker, turbo_qty, turbo_p, "BUY", "LOC")
            if res_turbo.get('rt_cd') == '0':
                turbo_success = 1
                result_msg += f"-> ${turbo_p:.2f} 에 {turbo_qty}주: 성공\n"
            else:
                result_msg += f"-> ${turbo_p:.2f} 에 {turbo_qty}주: 실패({res_turbo.get('msg1', '')})\n"

        grid_success = 0
        grid_total = 0
        if avg_p > 0:
            s = STRATEGY.get(ticker, {})
            grid_one_time = s.get("seed", FIXED_SEED/2) / s.get("total_a", 20)
            base_qty = math.floor(grid_one_time / avg_p)
            result_msg += f"\n[줍줍 매수 - 폭락장 대비 6단계]\n"
            for gi in range(1, 7):
                gp = round(grid_one_time / (base_qty + gi), 2)
                if gp <= 0:
                    break
                grid_total += 1
                res_grid = send_order(ticker, 1, gp, "BUY", "LOC")
                if res_grid.get('rt_cd') == '0':
                    grid_success += 1
                    result_msg += f"  - LOC ${gp:.2f} x 1주: 성공\n"
                else:
                    result_msg += f"  - LOC ${gp:.2f} x 1주: 실패({res_grid.get('msg1', '')})\n"

        sell_success = 0
        sell_total = 0

        result_msg += f"\n[매도]\n"
        if q_3_4 > 0:
            res_s1 = send_order(ticker, q_3_4, target_p, "SELL", "00")
            sell_total += 1
            if res_s1.get('rt_cd') == '0':
                sell_success += 1
                result_msg += f"1. 지정가 3/4({q_3_4}주) ${target_p:.2f}: 성공\n"
            else:
                result_msg += f"1. 지정가 3/4({q_3_4}주) ${target_p:.2f}: 실패({res_s1.get('msg1', '')})\n"

        if q_1_4 > 0:
            loc_sell_p = star_p + 0.01
            res_s2 = send_order(ticker, q_1_4, loc_sell_p, "SELL", "LOC")
            sell_total += 1
            if res_s2.get('rt_cd') == '0':
                sell_success += 1
                result_msg += f"2. LOC 1/4({q_1_4}주) ${loc_sell_p:.2f}: 성공\n"
            else:
                result_msg += f"2. LOC 1/4({q_1_4}주) ${loc_sell_p:.2f}: 실패({res_s2.get('msg1', '')})\n"

        result_msg += f"\n---총계---\n"
        result_msg += f"매수(기본): {buy_total}건 중 {buy_success}건 성공\n"
        if turbo_qty > 0:
            result_msg += f"매수(가속): 1건 중 {turbo_success}건 성공\n"
        result_msg += f"매수(줍줍): {grid_total}건 중 {grid_success}건 성공\n"
        result_msg += f"매도(예약): {sell_total}건 중 {sell_success}건 성공\n"
        set_trade_lock(ticker)

    elif action == "qsell":
        q_3_4 = int(parts[2])
        target_p = float(parts[3])
        q_1_4 = int(parts[4])

        await query.edit_message_text(text=f"{ticker} 쿼터매도 주문 전송 중...")

        result_msg = f"{ticker} 쿼터매도 결과\n\n"
        sell_success = 0
        sell_total = 0

        if q_1_4 > 0:
            res_moc = send_order(ticker, q_1_4, 0, "SELL", "MOC")
            sell_total += 1
            if res_moc.get('rt_cd') == '0':
                sell_success += 1
                result_msg += f"1. MOC 매도 1/4({q_1_4}주): 성공\n"
            else:
                result_msg += f"1. MOC 매도 1/4({q_1_4}주): 실패({res_moc.get('msg1', '')})\n"

        if q_3_4 > 0:
            res_lmt = send_order(ticker, q_3_4, target_p, "SELL", "00")
            sell_total += 1
            if res_lmt.get('rt_cd') == '0':
                sell_success += 1
                result_msg += f"2. 지정가 매도 3/4({q_3_4}주) ${target_p:.2f}: 성공\n"
            else:
                result_msg += f"2. 지정가 매도 3/4({q_3_4}주) ${target_p:.2f}: 실패({res_lmt.get('msg1', '')})\n"

        result_msg += f"\n매도: {sell_total}건 중 {sell_success}건 성공\n"
        set_trade_lock(ticker)

    else:
        result_msg = "알 수 없는 명령입니다."

    await context.bot.send_message(chat_id=query.message.chat_id, text=result_msg)

def execute_all_order(ticker, info):
    s = STRATEGY[ticker]
    avg_p = info["avg_price"]
    star_p = info["star_price"]
    target_p = info["target_price"]
    b1_qty = info["b1"]
    b2_qty = info["b2"]
    q_3_4 = info["qty_3_4"]
    q_1_4 = info["qty_1_4"]
    turbo_qty = info["turbo_qty"]
    turbo_p = info["turbo_price"]

    is_first_half = info.get("is_first_half", True)
    half_label = "전반전" if is_first_half else "후반전"
    result_msg = f"{ticker} 자동 주문 결과 ({half_label})\n\n"
    buy_success = 0
    buy_total = 0

    result_msg += "[기본 매수 - LOC]\n"
    if b1_qty > 0:
        res1 = send_order(ticker, b1_qty, avg_p, "BUY", "LOC")
        buy_total += 1
        if res1.get('rt_cd') == '0':
            buy_success += 1
            result_msg += f"1. 평단가(${avg_p:.2f}) {b1_qty}주: 성공\n"
        else:
            result_msg += f"1. 평단가(${avg_p:.2f}) {b1_qty}주: 실패({res1.get('msg1', '')})\n"

    if b2_qty > 0:
        res2 = send_order(ticker, b2_qty, star_p, "BUY", "LOC")
        buy_total += 1
        if res2.get('rt_cd') == '0':
            buy_success += 1
            result_msg += f"2. 별가(${star_p:.2f}) {b2_qty}주: 성공\n"
        else:
            result_msg += f"2. 별가(${star_p:.2f}) {b2_qty}주: 실패({res2.get('msg1', '')})\n"

    turbo_success = 0
    if turbo_qty > 0 and turbo_p > 0:
        result_msg += f"\n[가속 매수 - LOC]\n"
        res_turbo = send_order(ticker, turbo_qty, turbo_p, "BUY", "LOC")
        if res_turbo.get('rt_cd') == '0':
            turbo_success = 1
            result_msg += f"-> ${turbo_p:.2f} 에 {turbo_qty}주: 성공\n"
        else:
            result_msg += f"-> ${turbo_p:.2f} 에 {turbo_qty}주: 실패({res_turbo.get('msg1', '')})\n"

    grid_success = 0
    grid_total = 0
    if avg_p > 0:
        grid_one_time = s.get("seed", FIXED_SEED/2) / s.get("total_a", 20)
        base_qty = math.floor(grid_one_time / avg_p)
        result_msg += f"\n[줍줍 매수 - 폭락장 대비 6단계]\n"
        for gi in range(1, 7):
            gp = round(grid_one_time / (base_qty + gi), 2)
            if gp <= 0:
                break
            grid_total += 1
            res_grid = send_order(ticker, 1, gp, "BUY", "LOC")
            if res_grid.get('rt_cd') == '0':
                grid_success += 1
                result_msg += f"  - LOC ${gp:.2f} x 1주: 성공\n"
            else:
                result_msg += f"  - LOC ${gp:.2f} x 1주: 실패({res_grid.get('msg1', '')})\n"

    sell_success = 0
    sell_total = 0
    result_msg += f"\n[매도]\n"
    if q_3_4 > 0:
        res_s1 = send_order(ticker, q_3_4, target_p, "SELL", "00")
        sell_total += 1
        if res_s1.get('rt_cd') == '0':
            sell_success += 1
            result_msg += f"1. 지정가 3/4({q_3_4}주) ${target_p:.2f}: 성공\n"
        else:
            result_msg += f"1. 지정가 3/4({q_3_4}주) ${target_p:.2f}: 실패({res_s1.get('msg1', '')})\n"

    if q_1_4 > 0:
        loc_sell_p = star_p + 0.01
        res_s2 = send_order(ticker, q_1_4, loc_sell_p, "SELL", "LOC")
        sell_total += 1
        if res_s2.get('rt_cd') == '0':
            sell_success += 1
            result_msg += f"2. LOC 1/4({q_1_4}주) ${loc_sell_p:.2f}: 성공\n"
        else:
            result_msg += f"2. LOC 1/4({q_1_4}주) ${loc_sell_p:.2f}: 실패({res_s2.get('msg1', '')})\n"

    result_msg += f"\n---총계---\n"
    result_msg += f"매수(기본): {buy_total}건 중 {buy_success}건 성공\n"
    if turbo_qty > 0:
        result_msg += f"매수(가속): 1건 중 {turbo_success}건 성공\n"
    result_msg += f"매수(줍줍): {grid_total}건 중 {grid_success}건 성공\n"
    result_msg += f"매도(예약): {sell_total}건 중 {sell_success}건 성공\n"
    set_trade_lock(ticker)
    return result_msg

# [수정 전: 매 분마다 계좌 동기화 실행 방식]
# async def premarket_target_check(context: ContextTypes.DEFAULT_TYPE):
#     chat_id = context.job.chat_id
#     if not is_market_open_today(): return
#     sync_account() # <--- 매 분마다 무조건 계좌 정보를 긁어와 데이터 낭비 발생
#     for ticker in STRATEGY:
#         if check_trade_lock(ticker): continue
#         # ... (중략) ...

# [수정 후: 가격 확인 후 필요할 때만 계좌 동기화 방식]
async def premarket_target_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.datetime.now(kst)

    # 지정된 시간대가 아니면 통신 자체를 시도하지 않음
    if not (now_kst.hour == 18 and now_kst.minute < 30): return
    if not is_market_open_today(): return

    for ticker in STRATEGY:
        if check_trade_lock(ticker): continue
        
        # 1. 가격만 먼저 가볍게 체크
        curr_p = get_current_price(ticker)
        s = STRATEGY[ticker]
        avg_p = s["data"].get("avg", 0)

        # 2. 실제 목표가에 도달했을 때만 계좌 정보를 동기화하고 주문 실행
        if avg_p > 0 and curr_p >= (avg_p * (1 + s["target_profit"] / 100)):
            sync_account() 
            # (이후 익절/재진입 주문 로직 동일하게 수행)
            await context.bot.send_message(chat_id=chat_id, text=f"[{ticker}] 프리마켓 목표가 도달! 자동 익절 실행")

        s = STRATEGY[ticker]
        d = s["data"]
        avg_p = d.get("avg", 0)
        qty_t = int(d.get("qty", 0))

        if avg_p <= 0 or qty_t <= 0:
            continue

        target_p = avg_p * (1 + s["target_profit"] / 100)

        try:
            curr_p = get_current_price(ticker)
            if curr_p <= 0:
                continue

            if curr_p >= target_p:
                profit_rt = (curr_p - avg_p) / avg_p * 100
                one_time = s["seed"] / s["total_a"]
                entry_p = round(curr_p * 1.15, 2)
                entry_qty = math.floor(one_time / curr_p)

                kst = pytz.timezone('Asia/Seoul')
                now_kst = datetime.datetime.now(kst)

                alert_msg = f"[프리마켓 목표가 돌파!] {now_kst.strftime('%H:%M')} KST\n\n"
                alert_msg += f"[{ticker}] 현재가: ${curr_p:.2f} >= 목표가: ${target_p:.2f}\n"
                alert_msg += f"  평단: ${avg_p:.2f} / 수익률: {profit_rt:+.1f}%\n"
                alert_msg += f"  전량 매도({qty_t}주) 주문 실행!\n"
                await context.bot.send_message(chat_id=chat_id, text=alert_msg)

                res_sell = send_order(ticker, qty_t, curr_p, "SELL", "00")
                sell_success = res_sell.get('rt_cd') == '0'

                result_msg = f"[{ticker} 프리마켓 매도 주문]\n"
                result_msg += f"지정가 매도 {qty_t}주 ${curr_p:.2f}: {'주문 접수' if sell_success else '실패('+res_sell.get('msg1','')+')'}\n"

                if sell_success:
                    order_no = res_sell.get('output', {}).get('ODNO', '')
                    result_msg += f"주문번호: {order_no}\n체결 확인 중... (최대 30초)\n"
                    await context.bot.send_message(chat_id=chat_id, text=result_msg)

                    filled, filled_qty, filled_price = check_order_filled(order_no, ticker, "SELL", max_checks=10, interval=3)

                    if filled:
                        realized = (filled_price - avg_p) * filled_qty
                        new_cumul = add_cumul_profit(ticker, realized, filled_qty, filled_price, avg_p, order_no=order_no)
                        fill_msg = f"[{ticker} 매도 체결 확인]\n"
                        fill_msg += f"체결: {filled_qty}주 @ ${filled_price:.2f}\n"
                        fill_msg += f"실현수익: ${realized:+,.2f} (누적: ${new_cumul:,.2f})\n\n"
                        fill_msg += f"LOC 종가 재진입 매수 주문 실행...\n"
                        await context.bot.send_message(chat_id=chat_id, text=fill_msg)

                        res_buy = send_order(ticker, entry_qty, entry_p, "BUY", "LOC")
                        buy_msg = f"[{ticker} LOC 재진입 결과]\n"
                        buy_msg += f"LOC 매수 {entry_qty}주 ${entry_p:.2f}: {'성공' if res_buy.get('rt_cd')=='0' else '실패('+res_buy.get('msg1','')+')'}\n"
                        await context.bot.send_message(chat_id=chat_id, text=buy_msg)
                    else:
                        await context.bot.send_message(chat_id=chat_id, text=f"[{ticker}] 매도 미체결 (30초 대기 후 체결 안됨)\nLOC 재진입 보류. /sync 로 수동 확인하세요.")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=result_msg)

                set_trade_lock(ticker)
        except Exception as e:
            print(f"[premarket_check] {ticker} error: {e}")

_daily_trade_running = False

async def daily_market_check(context: ContextTypes.DEFAULT_TYPE):
    global _daily_trade_running
    chat_id = context.job.chat_id

    if _daily_trade_running:
        print("[daily_market_check] Already running, skipping duplicate call")
        return

    if not is_market_open_today():
        await context.bot.send_message(chat_id=chat_id, text="오늘은 미국 증시 휴장일입니다. 자동매매를 건너뜁니다.")
        return

    _daily_trade_running = True
    try:
        await _run_daily_market(context, chat_id)
    finally:
        _daily_trade_running = False

async def _run_daily_market(context, chat_id):
    await context.bot.send_message(chat_id=chat_id, text="[자동매매] 18:30 미국장 오픈 확인! 자동 주문 시작합니다.")

    sync_account()

    profit_tickers = []
    normal_tickers = []

    for ticker in STRATEGY:
        if check_trade_lock(ticker):
            await context.bot.send_message(chat_id=chat_id, text=f"[안전장치] {ticker}: 오늘 이미 매매 기록 있음. 건너뜁니다.")
            continue

        s = STRATEGY[ticker]
        d = s["data"]
        avg_p = d.get("avg", 0)
        qty_t = int(d.get("qty", 0))

        if avg_p > 0 and qty_t > 0:
            target_p = avg_p * (1 + s["target_profit"] / 100)
            try:
                curr_p = get_current_price(ticker)
                if curr_p >= target_p:
                    profit_rt = (curr_p - avg_p) / avg_p * 100
                    one_time = s["seed"] / s["total_a"]
                    entry_p = round(curr_p * 1.15, 2)
                    entry_qty = math.floor(one_time / curr_p)
                    await context.bot.send_message(chat_id=chat_id, text=f"[{ticker}] 목표가 달성! 전량 익절 매도 주문 실행\n현재가: ${curr_p:.2f} / 평단: ${avg_p:.2f} ({profit_rt:.1f}%)")
                    res_sell = send_order(ticker, qty_t, curr_p, "SELL", "00")
                    sell_success = res_sell.get('rt_cd') == '0'

                    sell_msg = f"[{ticker} 매도 주문]\n"
                    sell_msg += f"지정가 매도 {qty_t}주 ${curr_p:.2f}: {'주문 접수' if sell_success else '실패('+res_sell.get('msg1','')+')'}\n"

                    if sell_success:
                        order_no = res_sell.get('output', {}).get('ODNO', '')
                        sell_msg += f"주문번호: {order_no}\n체결 확인 중... (최대 30초)\n"
                        await context.bot.send_message(chat_id=chat_id, text=sell_msg)

                        filled, filled_qty, filled_price = check_order_filled(order_no, ticker, "SELL", max_checks=10, interval=3)
                        if filled:
                            realized = (filled_price - avg_p) * filled_qty
                            new_cumul = add_cumul_profit(ticker, realized, filled_qty, filled_price, avg_p, order_no=order_no)
                            fill_msg = f"[{ticker} 매도 체결 확인]\n"
                            fill_msg += f"체결: {filled_qty}주 @ ${filled_price:.2f}\n"
                            fill_msg += f"실현수익: ${realized:+,.2f} (누적: ${new_cumul:,.2f})\n\n"
                            fill_msg += f"LOC 종가 재진입 매수 주문 실행...\n"
                            await context.bot.send_message(chat_id=chat_id, text=fill_msg)

                            res_buy = send_order(ticker, entry_qty, entry_p, "BUY", "LOC")
                            buy_msg = f"[{ticker} LOC 재진입 결과]\n"
                            buy_msg += f"LOC 매수 {entry_qty}주 ${entry_p:.2f}: {'성공' if res_buy.get('rt_cd')=='0' else '실패('+res_buy.get('msg1','')+')'}\n"
                            await context.bot.send_message(chat_id=chat_id, text=buy_msg)
                        else:
                            await context.bot.send_message(chat_id=chat_id, text=f"[{ticker}] 매도 미체결 (30초 대기 후 체결 안됨)\nLOC 재진입 보류. /sync 로 수동 확인하세요.")
                    else:
                        await context.bot.send_message(chat_id=chat_id, text=sell_msg)

                    set_trade_lock(ticker)
                    profit_tickers.append(ticker)
                    continue
            except Exception:
                pass

        normal_tickers.append(ticker)

    all_infos = {}
    for ticker in normal_tickers:
        s = STRATEGY[ticker]
        d = s["data"]
        info = build_order_info(ticker)

        if info["qty_total"] == 0:
            try:
                curr_price = get_current_price(ticker)
                one_time_amt = s["seed"] / s["total_a"]
                entry_price = curr_price * 1.15
                entry_qty = math.floor(one_time_amt / curr_price)
                if entry_qty > 0:
                    res = send_order(ticker, entry_qty, entry_price, "BUY", "LOC")
                    result_msg = f"{ticker} 최초 진입 자동 실행\n"
                    result_msg += f"{entry_qty}주 ${entry_price:.2f} LOC: {'성공' if res.get('rt_cd')=='0' else '실패('+res.get('msg1','')+')'}\n"
                    set_trade_lock(ticker)
                    await context.bot.send_message(chat_id=chat_id, text=result_msg)
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"{ticker} 최초 진입 오류: {str(e)}")
            continue

        if info["force_quarter_sell"] and info["qty_total"] > 0:
            result_msg = f"{ticker} 쿼터매도 자동 실행 (T={info['t_val']:.1f})\n\n"
            if info["qty_1_4"] > 0:
                res_moc = send_order(ticker, info["qty_1_4"], 0, "SELL", "MOC")
                result_msg += f"MOC 1/4({info['qty_1_4']}주): {'성공' if res_moc.get('rt_cd')=='0' else '실패'}\n"
            if info["qty_3_4"] > 0:
                res_lmt = send_order(ticker, info["qty_3_4"], info["target_price"], "SELL", "00")
                result_msg += f"지정가 3/4({info['qty_3_4']}주) ${info['target_price']:.2f}: {'성공' if res_lmt.get('rt_cd')=='0' else '실패'}\n"
            set_trade_lock(ticker)
            await context.bot.send_message(chat_id=chat_id, text=result_msg)
            continue

        all_infos[ticker] = info

    if all_infos:
        no_turbo = False
        adjustment_stage = 0

        total_required = sum(estimate_required_amount(all_infos[t], STRATEGY[t]["seed"], STRATEGY[t]["total_a"]) for t in all_infos)

        if CURRENT_CASH > 0 and total_required > CURRENT_CASH:
            adjustment_stage = 1
            no_turbo = True
            for ticker in all_infos:
                all_infos[ticker] = build_order_info(ticker, no_turbo=True)
            total_required = sum(estimate_required_amount(all_infos[t], STRATEGY[t]["seed"], STRATEGY[t]["total_a"]) for t in all_infos)

        if CURRENT_CASH > 0 and total_required > CURRENT_CASH and adjustment_stage == 1:
            adjustment_stage = 2
            for ticker in list(all_infos.keys()):
                info = all_infos[ticker]
                if info["qty_total"] > 0:
                    info["force_quarter_sell"] = True
                    info["qty_3_4"] = math.floor(info["qty_total"] * 0.75)
                    info["qty_1_4"] = info["qty_total"] - info["qty_3_4"]
            total_required = sum(estimate_required_amount(all_infos[t], STRATEGY[t]["seed"], STRATEGY[t]["total_a"]) for t in all_infos)

        if adjustment_stage > 0:
            stage_msg = f"[자금 조정 {adjustment_stage}단계 적용]\n"
            if adjustment_stage >= 1:
                stage_msg += "1단계: 가속매수 제외\n"
            if adjustment_stage >= 2:
                stage_msg += "2단계: 잔금소진 -> 쿼터매도 발동\n"
            stage_msg += f"필요금액: ${total_required:,.2f} / 보유: ${CURRENT_CASH:,.2f}\n"
            await context.bot.send_message(chat_id=chat_id, text=stage_msg)

        for ticker in all_infos:
            info = all_infos[ticker]
            if info.get("force_quarter_sell") and info["qty_total"] > 0:
                reason = f"T={info['t_val']:.1f}>=19" if info["t_val"] >= 19 else "잔금소진"
                result_msg = f"{ticker} 쿼터매도 자동 실행 ({reason})\n\n"
                if info["qty_1_4"] > 0:
                    res_moc = send_order(ticker, info["qty_1_4"], 0, "SELL", "MOC")
                    result_msg += f"MOC 1/4({info['qty_1_4']}주): {'성공' if res_moc.get('rt_cd')=='0' else '실패'}\n"
                if info["qty_3_4"] > 0:
                    res_lmt = send_order(ticker, info["qty_3_4"], info["target_price"], "SELL", "00")
                    result_msg += f"지정가 3/4({info['qty_3_4']}주) ${info['target_price']:.2f}: {'성공' if res_lmt.get('rt_cd')=='0' else '실패'}\n"
                set_trade_lock(ticker)
                await context.bot.send_message(chat_id=chat_id, text=result_msg)
                continue
            await context.bot.send_message(chat_id=chat_id, text=f"[{ticker}] 통합 주문 자동 전송 중...")
            result_msg = execute_all_order(ticker, info)
            await context.bot.send_message(chat_id=chat_id, text=result_msg)

    summary = "[자동매매 일일 요약 리포트]\n"
    summary += f"날짜: {get_us_today()}\n"
    summary += f"보유 달러: ${CURRENT_CASH:,.2f}\n\n"

    for ticker in STRATEGY:
        s = STRATEGY[ticker]
        d = s["data"]
        avg_p = d.get("avg", 0)
        qty_t = int(d.get("qty", 0))
        if ticker in profit_tickers:
            summary += f"{ticker}: 목표가 익절 + 재진입 실행\n"
        elif qty_t == 0:
            summary += f"{ticker}: 최초 진입 또는 보유 없음\n"
        elif ticker in all_infos:
            info = all_infos[ticker]
            summary += f"{ticker}: 통합 주문 (T={info['t_val']:.1f})\n"
            summary += f"  평단 ${avg_p:.2f} / {qty_t}주\n"
        elif check_trade_lock(ticker):
            summary += f"{ticker}: 오늘 이미 처리됨 (중복 방지)\n"
        else:
            summary += f"{ticker}: 쿼터매도 또는 기타\n"

    summary += f"\n다음 자동매매: 내일 18:30 KST"
    await context.bot.send_message(chat_id=chat_id, text=summary)

def query_filled_orders():
    try:
        token = get_kis_access_token()
        url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-ccnl"

        kst = pytz.timezone('Asia/Seoul')
        today_str = datetime.datetime.now(kst).strftime('%Y%m%d')
        two_days_ago = (datetime.datetime.now(kst) - datetime.timedelta(days=2)).strftime('%Y%m%d')
        print(f"[query_filled] date range: {two_days_ago} ~ {today_str} (KST)")

        all_orders = []
        for excg in ["NASD", "NYSE", "AMEX"]:
            headers = {
                "Content-Type": "application/json", "authorization": f"Bearer {token}",
                "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "JTTT3001R", "custtype": "P"
            }
            params = {
                "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD,
                "PDNO": "", "ORD_STRT_DT": two_days_ago, "ORD_END_DT": today_str,
                "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "01",
                "OVRS_EXCG_CD": excg, "SORT_SQN": "DS",
                "ORD_GNO_BRNO": "", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
            }
            res = requests.get(url, headers=headers, params=params).json()
            if res.get('rt_cd') == '0':
                orders = res.get('output', [])
                print(f"[query_filled] {excg}: {len(orders)} orders found")
                all_orders.extend(orders)
            else:
                print(f"[query_filled] {excg}: error - {res.get('msg1', '')}")

        return {"rt_cd": "0", "output": all_orders}
    except Exception as e:
        return {"rt_cd": "-1", "msg1": str(e)}

async def settlement_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    est = pytz.timezone('US/Eastern')
    today_est = datetime.datetime.now(est).date()
    nyse = mcal.get_calendar('NYSE')
    schedule = nyse.schedule(start_date=today_est, end_date=today_est)
    if schedule.empty:
        yesterday = (today_est - datetime.timedelta(days=1))
        schedule2 = nyse.schedule(start_date=yesterday, end_date=yesterday)
        if schedule2.empty:
            return

    kst = pytz.timezone('Asia/Seoul')
    now_kst = datetime.datetime.now(kst)
    print(f"[settlement] Running at KST={now_kst.strftime('%Y-%m-%d %H:%M')}, EST={datetime.datetime.now(est).strftime('%Y-%m-%d %H:%M')}")

    res = query_filled_orders()
    if res.get('rt_cd') != '0':
        await context.bot.send_message(chat_id=chat_id, text=f"[체결 확인] 체결 내역 조회 실패: {res.get('msg1', '알수없는 오류')}")
        return

    orders = res.get('output', [])
    print(f"[settlement] Found {len(orders)} filled orders")
    if not orders:
        await context.bot.send_message(chat_id=chat_id, text="[체결 확인] 최근 체결된 주문이 없습니다.")
        return

    sync_account()

    msg = "[당일 체결 확인 리포트]\n\n"
    buy_amt = 0.0
    sell_amt = 0.0
    buy_cnt = 0
    sell_cnt = 0
    profit_recorded = {}

    for o in orders:
        ticker = o.get('ovrs_pdno', '?')
        side = "매수" if o.get('sll_buy_dvsn_cd', '') == '02' else "매도"
        filled_qty = int(float(o.get('ft_ccld_qty', '0')))
        filled_price = float(o.get('ft_ccld_unpr3', o.get('ft_ccld_unpr', '0')))
        total = filled_qty * filled_price
        order_no = o.get('odno', '')

        if filled_qty > 0:
            msg += f"  {ticker} {side} {filled_qty}주 x ${filled_price:.2f} = ${total:.2f}\n"
            if side == "매수":
                buy_amt += total
                buy_cnt += 1
            else:
                sell_amt += total
                sell_cnt += 1
                if ticker in STRATEGY:
                    avg_p = STRATEGY[ticker]["data"]["avg"]
                    if avg_p > 0:
                        realized = (filled_price - avg_p) * filled_qty
                        new_cumul = add_cumul_profit(ticker, realized, filled_qty, filled_price, avg_p, order_no=order_no)
                        if ticker not in profit_recorded:
                            profit_recorded[ticker] = 0.0
                        profit_recorded[ticker] = new_cumul
                        msg += f"    → 실현수익: ${realized:+,.2f} (평단가 ${avg_p:.2f})\n"
                    else:
                        print(f"[settlement] {ticker} avg=0, sell profit skipped (order_no={order_no}) - likely already recorded inline")

    msg += f"\n총 매수: {buy_cnt}건 (${buy_amt:,.2f})\n"
    msg += f"총 매도: {sell_cnt}건 (${sell_amt:,.2f})\n"

    cp = load_cumul_profit()
    if cp.get("SOXL", 0) > 0 or cp.get("TQQQ", 0) > 0:
        msg += f"\n[실현 누적수익]\n"
        for ticker in STRATEGY:
            cumul = cp.get(ticker, 0.0)
            if cumul != 0:
                msg += f"  {ticker}: ${cumul:,.2f}\n"

    msg += f"\n현재 잔고: ${CURRENT_CASH:,.2f}"
    await context.bot.send_message(chat_id=chat_id, text=msg)

async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("수익률 현황 조회 중...")
    sync_account()

    msg = "[포트폴리오 현황 리포트]\n\n"
    total_invested = 0.0
    total_value = 0.0
    for ticker in STRATEGY:
        s = STRATEGY[ticker]
        d = s["data"]
        avg_p = d.get("avg", 0)
        qty_t = int(d.get("qty", 0))
        cumul = d.get("cumul", 0)

        one_time_amt = s["seed"] / s["total_a"]
        t_val = round(cumul / one_time_amt, 2) if one_time_amt > 0 and cumul > 0 else 0.0
        star_pct = round(s["target_profit"] * (1.0 - 2.0 * t_val / s["total_a"]), 2) if t_val > 0 else s["target_profit"]

        msg += f"[{ticker}] 목표 {s['target_profit']}%\n"
        msg += f"  시드: ${s['seed']:,.0f} (1회분: ${one_time_amt:,.0f})\n"

        if qty_t == 0:
            msg += f"  보유 없음 (T=0)\n\n"
            continue

        invested = avg_p * qty_t
        total_invested += invested

        try:
            curr_p = get_current_price(ticker)
            curr_value = curr_p * qty_t
            total_value += curr_value
            pnl = curr_value - invested
            pnl_pct = (curr_p - avg_p) / avg_p * 100

            target_p = avg_p * (1 + s["target_profit"] / 100)
            to_target = (target_p - curr_p) / curr_p * 100

            msg += f"  평단: ${avg_p:.2f} / 현재가: ${curr_p:.2f}\n"
            msg += f"  수량: {qty_t}주 / 투자금: ${invested:,.2f}\n"
            msg += f"  평가금: ${curr_value:,.2f}\n"
            msg += f"  손익: ${pnl:,.2f} ({pnl_pct:+.2f}%)\n"
            midpoint = s["total_a"] / 2
            half_label = "전반전" if t_val <= midpoint else "후반전"
            msg += f"  T값: {t_val} ({half_label}) / 별%: {star_pct:.2f}%\n"
            msg += f"  목표가: ${target_p:.2f} (현재가 대비 {to_target:+.1f}%)\n"
            remaining = s["total_a"] - t_val
            msg += f"  남은 회차: {remaining:.1f}회\n\n"
        except Exception:
            total_value += invested
            midpoint = s["total_a"] / 2
            half_label = "전반전" if t_val <= midpoint else "후반전"
            msg += f"  평단: ${avg_p:.2f} / 수량: {qty_t}주\n"
            msg += f"  T값: {t_val} ({half_label}) / 별%: {star_pct:.2f}%\n"
            msg += f"  현재가 조회 실패\n\n"

    msg += f"==========================\n"
    msg += f"총 투자금: ${total_invested:,.2f}\n"
    msg += f"총 평가금: ${total_value:,.2f}\n"
    total_pnl = total_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    msg += f"총 손익: ${total_pnl:,.2f} ({total_pnl_pct:+.2f}%)\n"
    msg += f"보유 달러: ${CURRENT_CASH:,.2f}\n"

    cp_all = load_cumul_profit()
    total_cumul = cp_all.get("SOXL", 0.0) + cp_all.get("TQQQ", 0.0)
    if total_cumul != 0:
        msg += f"\n[실현 누적수익]\n"
        for t in STRATEGY:
            cp_t = cp_all.get(t, 0.0)
            if cp_t != 0:
                msg += f"  {t}: ${cp_t:,.2f}\n"
        msg += f"  합계: ${total_cumul:,.2f}\n"
        recent = cp_all.get("history", [])
        if recent:
            last = recent[-1]
            msg += f"  최근: {last['date']} {last['ticker']} ${last['profit']:+,.2f}\n"

    msg += f"==========================\n"

    await update.message.reply_text(msg)

def force_renew_token():
    url = f"{URL_BASE}/oauth2/tokenP"
    body = {"grant_type": "client_credentials", "appkey": APP_KEY, "appsecret": APP_SECRET}
    res = requests.post(url, headers={"content-type": "application/json"}, data=json.dumps(body))
    res_data = res.json()
    new_token = res_data.get('access_token')
    if new_token:
        expire_date = (datetime.datetime.now() + datetime.timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        with open(TOKEN_FILE, 'w') as f:
            json.dump({'token': new_token, 'expire': expire_date}, f)
        return True, expire_date
    return False, None

async def token_renewal_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        need_renewal = False
        old_expire = "없음"

        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE, 'r') as f:
                    saved = json.load(f)
                expire_time = datetime.datetime.strptime(saved['expire'], '%Y-%m-%d %H:%M:%S')
                old_expire = saved['expire']
                remaining = expire_time - datetime.datetime.now()
                hours_left = remaining.total_seconds() / 3600
                if hours_left < 2:
                    need_renewal = True
            except Exception:
                need_renewal = True
        else:
            need_renewal = True

        if need_renewal:
            success, new_expire = force_renew_token()
            if success:
                await context.bot.send_message(chat_id=chat_id, text=f"[토큰 갱신 완료]\n이전 만료: {old_expire}\n새 만료: {new_expire}")
            else:
                await context.bot.send_message(chat_id=chat_id, text="[토큰 갱신 실패] API 키를 확인하세요")
    except Exception as e:
        try:
            success, new_expire = force_renew_token()
            if success:
                await context.bot.send_message(chat_id=chat_id, text=f"[토큰 복구 갱신 완료]\n새 만료: {new_expire}")
                return
        except Exception:
            pass
        await context.bot.send_message(chat_id=chat_id, text=f"[토큰 갱신 오류] {str(e)}")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(TRADE_LOCK_FILE):
        try:
            with open(TRADE_LOCK_FILE, 'r') as f:
                locks = json.load(f)
            os.remove(TRADE_LOCK_FILE)
            cleared = ", ".join([f"{k}({v})" for k, v in locks.items()])
            await update.message.reply_text(f"[잠금 해제 완료]\n해제된 종목: {cleared}\n\n이제 /sync 로 다시 주문할 수 있습니다.")
        except Exception:
            os.remove(TRADE_LOCK_FILE)
            await update.message.reply_text("[잠금 해제 완료] 모든 종목 잠금이 초기화되었습니다.\n\n이제 /sync 로 다시 주문할 수 있습니다.")
    else:
        await update.message.reply_text("현재 잠금된 종목이 없습니다.")

async def cmd_profit_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = load_cumul_profit()
    soxl_p = cp.get("SOXL", 0.0)
    tqqq_p = cp.get("TQQQ", 0.0)
    total_p = soxl_p + tqqq_p

    if total_p == 0:
        await update.message.reply_text("초기화할 누적 수익이 없습니다.")
        return

    save_cumul_profit({"SOXL": 0.0, "TQQQ": 0.0, "history": [], "processed_orders": []})

    msg = "[누적 수익 기록 초기화 완료]\n\n"
    msg += f"초기화된 수익 기록:\n"
    if soxl_p != 0:
        msg += f"  SOXL: ${soxl_p:,.2f}\n"
    if tqqq_p != 0:
        msg += f"  TQQQ: ${tqqq_p:,.2f}\n"
    msg += f"  합계: ${total_p:,.2f}\n\n"
    msg += f"수익금은 계좌에 현금으로 보유 중입니다."
    await update.message.reply_text(msg)

async def cmd_profit_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cp = load_cumul_profit()
    history = cp.get("history", [])

    if not history:
        await update.message.reply_text("기록된 수익 내역이 없습니다.")
        return

    msg = "[수익 누적 내역]\n\n"
    for h in history[-20:]:
        msg += f"{h['date']} | {h['ticker']} | "
        msg += f"{h['sell_qty']}주 ${h['sell_price']:.2f} (평단 ${h['avg_price']:.2f})\n"
        msg += f"  수익: ${h['profit']:+,.2f} / 누적: ${h['cumul_total']:,.2f}\n"

    soxl_p = cp.get("SOXL", 0.0)
    tqqq_p = cp.get("TQQQ", 0.0)
    msg += f"\n[현재 누적 합계]\n"
    msg += f"  SOXL: ${soxl_p:,.2f} / TQQQ: ${tqqq_p:,.2f}\n"
    msg += f"  총합: ${soxl_p + tqqq_p:,.2f}"
    await update.message.reply_text(msg)

# [수정 전: 너무 잦은 통신 주기]
# def register_schedules(job_queue, chat_id):
#     kst = pytz.timezone('Asia/Seoul')
#     job_queue.run_daily(daily_market_check, time=datetime.time(18,30,tzinfo=kst), chat_id=chat_id)
#     job_queue.run_repeating(token_renewal_check, interval=3600, first=10, chat_id=chat_id)
#     job_queue.run_repeating(premarket_target_check, interval=60, first=30, chat_id=chat_id)

# [수정 후: 가독성과 문법 오류를 해결한 버전]
def register_schedules(job_queue, chat_id):
    kst = pytz.timezone('Asia/Seoul')
    
    # 기존 등록된 모든 작업 삭제 (중복 방지)
    for job in job_queue.jobs():
        job.schedule_removal()

    # 1. 정규장 자동매매 (하루 1회)
    job_queue.run_daily(daily_market_check, time=datetime.time(18, 30, tzinfo=kst), days=(0,1,2,3,4), chat_id=chat_id)
    
    # 2. 당일 체결 확인 (하루 1회)
    job_queue.run_daily(settlement_check, time=datetime.time(8, 0, tzinfo=kst), days=(0,1,2,3,4,5), chat_id=chat_id)

    # 3. KIS 토큰 갱신 (6시간마다)
    job_queue.run_repeating(token_renewal_check, interval=21600, first=10, chat_id=chat_id)

    # 4. 프리마켓 체크 (5분마다) - 데이터 절약을 위해 300초 설정
    job_queue.run_repeating(
        premarket_target_check, 
        interval=300, 
        first=30, 
        chat_id=chat_id, 
        name=f"premarket_{chat_id}"
    )

    print(f"[schedule] All jobs registered for chat_id={chat_id}")

# [수정 전: 이전 안내 문구]
# async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     chat_id = update.message.chat_id
#     save_chat_id(chat_id)
#     register_schedules(context.job_queue, chat_id)
#     await update.message.reply_text("SOXL & TQQQ 자동매매봇 가동!\n\n- 매일 18:00~18:30(KST) 프리마켓 목표가 체크 (1분마다, 초과 시 자동 익절+LOC재진입)\n- 매일 18:30(KST) 자동매매 실행\n- 매일 08:00(KST) 당일 체결 확인\n- API 토큰 자동 갱신 (1시간마다 체크)\n\n/sync: 수동 주문 (필요시)...")

# [수정 후: 최적화된 주기에 맞춘 안내 문구]
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    save_chat_id(chat_id)
    register_schedules(context.job_queue, chat_id)

    # 안내 문구를 실제 로직(5분, 6시간)에 맞게 수정했습니다.
    welcome_msg = (
        "SOXL & TQQQ 자동매매봇 가동!\n\n"
        "- 매일 18:00~18:30(KST) 프리마켓 목표가 체크 (5분마다, 초과 시 자동 익절+LOC재진입)\n"
        "- 매일 18:30(KST) 자동매매 실행\n"
        "- 매일 08:00(KST) 당일 체결 확인\n"
        "- API 토큰 자동 갱신 (6시간마다 체크)\n\n"
        "/sync: 수동 주문 (필요시)\n"
        "/status: 현재 시세 조회\n"
        "/report: 수익률 현황\n"
        "/cash: 예수금 확인\n"
        "/reset: 매매 잠금 해제\n"
        "/profit_history: 수익 누적 내역\n"
        "/profit_reset: 누적 수익 초기화"
    )
    
    await update.message.reply_text(welcome_msg)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("실시간 시세 조회 중...")
    try:
        has_holdings = any(
            int(STRATEGY[t]["data"].get("qty", 0)) > 0 for t in STRATEGY
        )
        if not has_holdings:
            sync_account()

        est = pytz.timezone('US/Eastern')
        now_est = datetime.datetime.now(est)
        kst = pytz.timezone('Asia/Seoul')
        now_kst = datetime.datetime.now(kst)

        market_state = get_market_session_kr()

        msg = f"[실시간 시세 현황]\n"
        msg += f"조회시각: {now_kst.strftime('%H:%M:%S')} KST\n"
        msg += f"시장상태: {market_state}\n\n"

        for ticker in STRATEGY:
            try:
                curr_p = get_current_price(ticker)
                s = STRATEGY[ticker]
                d = s["data"]
                avg_p = d.get("avg", 0)
                qty_t = int(d.get("qty", 0))

                msg += f"[{ticker}]\n"
                msg += f"  현재가: ${curr_p:.4f}\n"

                if avg_p > 0 and qty_t > 0:
                    pnl_pct = (curr_p - avg_p) / avg_p * 100
                    pnl_amt = (curr_p - avg_p) * qty_t
                    target_p = avg_p * (1 + s["target_profit"] / 100)
                    msg += f"  평단: ${avg_p:.2f} / {qty_t}주\n"
                    msg += f"  손익: ${pnl_amt:,.2f} ({pnl_pct:+.2f}%)\n"
                    msg += f"  목표가: ${target_p:.2f}\n"
                else:
                    msg += f"  보유 없음\n"
                msg += "\n"
            except Exception as e:
                msg += f"[{ticker}] 조회 실패: {str(e)}\n\n"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"조회 오류: {str(e)}")

async def post_init(application):
    saved_chat_id = load_chat_id()
    if saved_chat_id:
        try:
            sync_account()
            print("[auto-init] Account synced on startup")
        except Exception as e:
            print(f"[auto-init] Account sync failed: {e}")
        register_schedules(application.job_queue, saved_chat_id)
        print(f"[auto-init] Schedules auto-registered for saved chat_id={saved_chat_id}")
        try:
            await application.bot.send_message(chat_id=saved_chat_id, text="[시스템] 봇이 재시작되었습니다. 계좌 동기화 및 스케줄 자동 등록 완료.")
        except Exception as e:
            print(f"[auto-init] Failed to send restart notification: {e}")
    else:
        print("[auto-init] No saved chat_id found. Please send /start to register schedules.")

async def error_handler(update, context):
    import telegram.error
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        print(f"[telegram] Conflict: 다른 봇 인스턴스와 충돌 - 자동 복구됨")
    elif isinstance(err, telegram.error.NetworkError):
        print(f"[telegram] NetworkError: {err} - 자동 재시도")
    elif isinstance(err, telegram.error.TimedOut):
        print(f"[telegram] TimedOut - 자동 재시도")
    else:
        print(f"[telegram] Error: {type(err).__name__}: {err}")

def main():
    if not TELEGRAM_TOKEN:
        return

    try:
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", json={"drop_pending_updates": True}, timeout=10)
        print(f"[startup] deleteWebhook: {resp.json().get('ok')}")
        resp2 = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/close", timeout=10)
        print(f"[startup] close session: {resp2.json()}")
        time_module.sleep(2)
    except Exception as e:
        print(f"[startup] Pre-cleanup: {e}")

    t = get_kis_access_token()
    print(f"[startup] KIS token: {'OK' if t else 'FAILED - will renew on first API call'}")
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("cash", cmd_cash))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("profit_reset", cmd_profit_reset))
    app.add_handler(CommandHandler("profit_history", cmd_profit_history))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)
    print("SOXL & TQQQ 자동매매봇 실행 중...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()