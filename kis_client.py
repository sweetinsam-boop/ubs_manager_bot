from __future__ import annotations

import datetime
import json
import logging
import requests
import time
from typing import Any, Optional

from config_manager import APP_KEY, APP_SECRET, CANO, ACNT_PRDT_CD
from bot_logging import get_logger

log = get_logger(__name__)

# --- KIS API 공통 설정 ---
URL_BASE = "https://openapi.koreainvestment.com:9443" # 실전투자용
# URL_BASE = "https://openapivts.koreainvestment.com:29443" # 모의투자용

_TOKEN_CACHE = {"token": None, "expiry": 0}

def get_kis_access_token() -> str:
    """OAuth 토큰 발급 및 캐싱 (token.dat 파일로 영속화)"""
    from storage.paths import get_path, TOKEN_FILE
    token_path = get_path(TOKEN_FILE)
    
    # 1. 파일에서 토큰 로드 시도
    try:
        if token_path.exists():
            with open(token_path, "r") as f:
                data = json.load(f)
                if data.get("expiry", 0) > time.time() + 600: # 만료 10분 전까지 유효
                    return data["token"]
    except: pass

    # 2. 신규 토큰 발급
    log.info("KIS 신규 액세스 토큰 발급 시도...")
    url = f"{URL_BASE}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }
    try:
        res = requests.post(url, json=body, timeout=10)
        res_data = res.json()
        if "access_token" in res_data:
            token = res_data["access_token"]
            expiry = time.time() + int(res_data.get("expires_in", 3600))
            # 파일에 저장
            with open(token_path, "w") as f:
                json.dump({"token": token, "expiry": expiry}, f)
            return token
        else:
            log.error("KIS 토큰 발급 실패: %s", res_data)
    except Exception as e:
        log.error("KIS 토큰 API 호출 중 예외: %s", e)
    return ""


def kis_api_call(method: str, url: str, headers: dict, params: dict = None, body: dict = None) -> dict:
    """KIS API 통합 호출 헬퍼 (초당 호출 제한 방어 포함)"""
    time.sleep(0.05) # TPS 제한 방어 (초당 20회 미만)
    try:
        if method == "GET":
            res = requests.get(url, headers=headers, params=params, timeout=15)
        else:
            res = requests.post(url, headers=headers, json=body, timeout=15)
        return res.json()
    except Exception as e:
        log.error("KIS API 호출 예외 (%s): %s", url.split("/")[-1], e)
        return {"rt_cd": "9", "msg1": str(e)}


# --- 계좌/주문 관련 API ---

def query_account_raw() -> dict:
    """해외주식 잔고 조회 (TTTS3012R)"""
    token = get_kis_access_token()
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3012R", # 실전용 (모의는 다름)
        "custtype": "P",
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": "NASD", # 대표로 나스닥 조회 (보통 전 종목 나옴)
        "TR_CRCY_CD": "USD",
        "CTX_AREA_NK200": "",
        "CTX_AREA_FK200": "",
    }
    return kis_api_call("GET", url, headers, params=params)


def query_available_cash(token: str = None) -> tuple[float, str]:
    """외화 예수금 조회 (TTTS3011R)"""
    if not token: token = get_kis_access_token()
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-present-balance"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3011R",
        "custtype": "P",
    }
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "WCRC_FRCR_DVSN_CD": "02", # 외화
        "NATN_CD": "840", # 미국
        "TR_CRCY_CD": "USD",
    }
    res = kis_api_call("GET", url, headers, params=params)
    if res.get("rt_cd") == "0":
        # 출금 가능 외화 금액 또는 외화 예수금 합계 선택
        out = res.get("output2", {})
        return float(out.get("frcr_evlu_amt2", 0)), "실시간 계좌"
    return 0.0, "조회 실패"


def send_order(ticker: str, qty: int, price: float, side: str = "BUY", ord_type: str = "LOC") -> dict:
    """해외주식 주문 실행 (TTTT1002U: 매수, TTTT1006U: 매도)"""
    token = get_kis_access_token()
    side = side.upper()
    tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/order"
    
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": tr_id,
        "custtype": "P",
    }
    
    # ord_type 매핑: LOC="32", 지정가="00", 시장가(MOC)="02"
    ord_dvsn = "32" if ord_type.upper() == "LOC" else \
               "02" if ord_type.upper() == "MOC" else "00"
    
    # 거래소 코드 판별
    excg = get_exchange_code(ticker)
    
    body = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "OVRS_EXCG_CD": excg,
        "PDNO": ticker,
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": f"{price:.2f}" if ord_dvsn != "02" else "0",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN_CD": ord_dvsn,
    }
    return kis_api_call("POST", url, headers, body=body)


# --- 시세 관련 API ---

_US_EXCHANGES_ALL = ("NASD", "NYSE", "AMEX")

def get_exchange_code(ticker: str) -> str:
    """티커에 따른 거래소 코드 반환 (캐싱 가능)"""
    # [방어] SOXL, TQQQ 등 주요 티커는 즉시 반환하여 호출 횟수 절약
    tk = ticker.upper()
    if tk in ("TQQQ", "SOXL", "SQQQ", "TSLA", "AAPL", "NVDA", "MSFT", "GOOGL"):
        return "NASD"
    # 나머지는 시세 조회를 통해 판별 가능하나, 보통 미국 ETF는 NASD/NYSE 중 하나
    return "NASD"


def get_current_price(ticker: str) -> float:
    """실시간 시세 조회 (HHDFS00000300)"""
    token = get_kis_access_token()
    url = f"{URL_BASE}/uapi/overseas-price/v1/quotations/price"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "HHDFS00000300",
        "custtype": "P",
    }
    # 거래소 코드(EXCD)를 정확히 넣어야 함. 실패 시 순회하며 찾음.
    for ex in ("NAS", "NYS", "AMS"):
        params = {"AUTH": "", "EXCD": ex, "SYMB": ticker}
        res = kis_api_call("GET", url, headers, params=params)
        if res.get("rt_cd") == "0":
            last = res.get("output", {}).get("last")
            if last and float(last) > 0:
                return float(last)
    return 0.0


def get_prev_close(ticker: str) -> float:
    """전일 종가 조회"""
    # get_current_price 응답의 'base' 필드 등을 활용 가능
    # 여기서는 yfinance 등을 Fallback으로 사용할 수도 있음
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        return float(t.fast_info['previous_close'])
    except: return 0.0


def get_history_prices(ticker: str, limit: int = 5) -> list[float]:
    """최근 N일 종가 리스트 (yfinance 활용 권장)"""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{limit+5}d") # 넉넉히 조회 후 필터링
        return hist['Close'].tail(limit).tolist()
    except: return []


# --- 주문 내역 조회 및 유틸 ---

def query_ccnl_raw(ticker="", excg="NASD", start_dt="", end_dt="", ccld_dvsn="00", ctx_nk200="", ctx_fk200="", tr_cont="") -> dict:
    """해외주식 주문체결내역 조회 (TTTS3035R)"""
    token = get_kis_access_token()
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-ccnl"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3035R",
        "custtype": "P",
    }
    if tr_cont: headers["tr_cont"] = tr_cont
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": ticker,
        "ORD_STRT_DT": start_dt,
        "ORD_END_DT": end_dt,
        "SLL_BUY_DVSN_CD": "00", # 전체
        "CCLD_DVSN": ccld_dvsn,
        "OVRS_EXCG_CD": excg,
        "SORT_SQN": "DS", # 최신순
        "CTX_AREA_NK200": ctx_nk200,
        "CTX_AREA_FK200": ctx_fk200,
    }
    return kis_api_call("GET", url, headers, params=params)


# ... (이하 V3/V4 전용 상세 조회 함수들: sum_buy_notional_for_order 등 기존과 동일하게 유지) ...
def query_recent_orders(ticker: str, days_back: int = 5, max_pages: int = 3):
    import pytz
    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.datetime.now(ny_tz)
    end_dt = now_ny.strftime("%Y%m%d")
    start_dt = (now_ny - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
    excg = get_exchange_code(ticker)
    nk200, fk200, tr_cont, out = "", "", "", []
    for _ in range(max_pages):
        res = query_ccnl_raw(ticker=ticker, excg=excg, start_dt=start_dt, end_dt=end_dt, ctx_nk200=nk200, ctx_fk200=fk200, tr_cont=tr_cont)
        if res.get("rt_cd") != "0": break
        rows = res.get("output", []) or []
        if rows: out.extend(rows)
        nk200 = res.get("ctx_area_nk200", "") or ""
        fk200 = res.get("ctx_area_fk200", "") or ""
        cont = (res.get("tr_cont") or "").strip()
        if cont in ("M", "F"): tr_cont = "N"
        else: break
    return out

def wait_order_min_filled(order_no, ticker, min_qty, max_checks=36, interval=5):
    on = str(order_no or "").strip()
    for _ in range(max_checks):
        last = sum_ccld_qty_for_order(on, ticker)
        if last >= min_qty: return True, last, 0
        time.sleep(interval)
    return False, 0, 0

def sum_ccld_qty_for_order(order_no, ticker):
    on = str(order_no or "").strip()
    rows = query_recent_orders(ticker, days_back=7, max_pages=6)
    best = 0
    for o in rows or []:
        if o.get("odno") == on or o.get("orgn_odno") == on:
            cq = int(float(o.get("ft_ccld_qty", 0)))
            if cq > best: best = cq
    return best

def query_unfilled_orders_today_us(ticker: str) -> list:
    import pytz
    ny_tz = pytz.timezone("America/New_York")
    today_ymd = datetime.datetime.now(ny_tz).strftime("%Y%m%d")
    raw_rows = query_recent_orders(ticker, days_back=0, max_pages=8)
    orders = []
    for o in raw_rows:
        if (o.get("ord_dt") or "").strip() == today_ymd:
            q = int(float(o.get("ft_ord_qty", 0))) - int(float(o.get("ft_ccld_qty", 0)))
            if q > 0:
                orders.append({"side": "매수" if o.get("sll_buy_dvsn_cd")=="02" else "매도", "qty": q, "price": float(o.get("ft_ord_unpr3", 0)), "status": o.get("prcs_stat_name", "대기"), "ord_no": o.get("odno"), "type": o.get("sll_buy_dvsn_cd_name")})
    return orders

def collect_recent_buy_odnos_with_fill(ticker: str) -> list[str]:
    rows = query_recent_orders(ticker, days_back=14, max_pages=8)
    return [o.get("odno") for o in rows if o.get("sll_buy_dvsn_cd")=="02" and int(float(o.get("ft_ccld_qty", 0))) > 0]

def sum_buy_notional_for_order(order_no, ticker):
    rows = query_recent_orders(ticker, days_back=14, max_pages=8)
    total = 0.0
    for o in rows:
        if (o.get("odno") == order_no or o.get("orgn_odno") == order_no) and o.get("sll_buy_dvsn_cd")=="02":
            total += int(float(o.get("ft_ccld_qty", 0))) * float(o.get("ft_ccld_unpr3", 0))
    return total
