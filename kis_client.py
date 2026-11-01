from __future__ import annotations

import json
import os
import threading
import time
import datetime
import requests
from config_manager import APP_KEY, APP_SECRET, CANO, ACNT_PRDT_CD
from storage.paths import get_path, TOKEN_FILE as _TOKEN_FILE_NAME
from bot_logging import get_logger

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

URL_BASE = "https://openapi.koreainvestment.com:9443"
log = get_logger(__name__)
# storage/ 기준 절대 경로 (Docker/호스트 동일 동작)
TOKEN_FILE = str(get_path(_TOKEN_FILE_NAME))

# 토큰 파일 접근 직렬화 (스케줄러 자동 실행과 텔레그램 수동 명령의 동시 호출 방어)
_token_lock = threading.Lock()


def _safe_json(res: requests.Response):
    """Response를 JSON으로 파싱하되, 실패 시 빈 dict를 반환합니다."""
    try:
        return res.json()
    except Exception:
        return {}


def _kis_token_expired_api_message(data: dict) -> bool:
    """API 응답이 액세스 토큰 만료/무효를 나타내는지 대략 판별합니다."""
    if not isinstance(data, dict):
        return False
    msg_cd = str(data.get("msg_cd") or "")
    msg1 = str(data.get("msg1") or "")
    if "EGW00123" in msg_cd or "EGW00123" in msg1:
        return True
    low = msg1.lower()
    if "token" in low and ("expired" in low or "invalid" in low):
        return True
    if "토큰" in msg1 and ("만료" in msg1 or "유효하지" in msg1):
        return True
    return False


def get_kis_access_token(force_renew: bool = False, force_refresh: bool = False):
    """
    KIS API Access Token을 발급받거나 캐시된 유효 토큰을 반환합니다.

    핵심 정책 (VR_NAS와 동일):
    - KIS는 24시간 안에 동일 키를 재발급 요청해도 같은 키를 돌려주며, 분당 1회 제한이 있음.
    - 따라서 만료 '전' 미리 재발급은 하지 않고, 캐시 만료 시각이 미래라면 그대로 사용한다.
    - force_renew/force_refresh=True는 캐시 무시하고 강제 재발급(만료된 token 응답·HTTP 401 등 사후 대응용).
    - 하위 호환을 위해 force_renew, force_refresh 두 키워드를 모두 받는다.
    """
    force = bool(force_renew or force_refresh)

    seoul = ZoneInfo("Asia/Seoul") if ZoneInfo is not None else None
    now_seoul = datetime.datetime.now(seoul) if seoul is not None else datetime.datetime.now()

    def _read_cached_token() -> tuple[str | None, datetime.datetime | None]:
        """파일에서 토큰과 만료시각(tz-aware)을 읽어 반환. 실패 시 (None, None)."""
        if not os.path.exists(TOKEN_FILE):
            return None, None
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return None, None
        token = saved.get("token")
        exp_raw = saved.get("expire")
        if not token or not exp_raw:
            return None, None
        try:
            expire_naive = datetime.datetime.strptime(str(exp_raw).strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None, None
        expire_time = expire_naive.replace(tzinfo=seoul) if seoul is not None else expire_naive
        return token, expire_time

    with _token_lock:
        if not force:
            cached_token, expire_time = _read_cached_token()
            if cached_token and expire_time is not None and expire_time > now_seoul:
                # 만료 '전' 재발급은 KIS 1분/24시간 제한과 충돌하므로 하지 않음.
                return cached_token

        url = f"{URL_BASE}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
        }
        for mint_attempt in range(4):
            try:
                res = requests.post(
                    url,
                    headers={"content-type": "application/json"},
                    data=json.dumps(body),
                    timeout=15,
                )
            except Exception as e:
                log.warning("token: request error (attempt %d/4): %s", mint_attempt + 1, e)
                # 네트워크 실패 시 캐시 fallback 시도 후 백오프
                cached_token, expire_time = _read_cached_token()
                now_check = datetime.datetime.now(seoul) if seoul is not None else datetime.datetime.now()
                if cached_token and expire_time is not None and expire_time > now_check:
                    return cached_token
                time.sleep(min(60, 5 + mint_attempt * 10))
                continue

            res_data = _safe_json(res)
            new_token = res_data.get("access_token")

            if new_token:
                expire_date = res_data.get("access_token_token_expired")
                if not expire_date:
                    base = datetime.datetime.now(seoul) if seoul is not None else datetime.datetime.now()
                    expire_date = (base + datetime.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
                tmp = TOKEN_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"token": new_token, "expire": expire_date}, f)
                os.replace(tmp, TOKEN_FILE)
                log.info("token: new token issued, expires: %s", expire_date)
                return new_token

            # 토큰 발급 실패: rate-limit / 1분 제한 등은 백오프 후 재시도, 그 외 즉시 캐시 fallback
            error_code = res_data.get("error_code") or res_data.get("msg_cd") or ""
            msg1 = str(res_data.get("msg1") or res_data.get("error_description") or "")
            log.warning(
                "token: request failed status=%s code=%s response=%s",
                getattr(res, "status_code", "?"),
                error_code,
                msg1[:200] if msg1 else res_data,
            )

            cached_token, expire_time = _read_cached_token()
            now_check = datetime.datetime.now(seoul) if seoul is not None else datetime.datetime.now()
            if cached_token and expire_time is not None and expire_time > now_check:
                log.info("token: using existing valid cached token")
                return cached_token

            # 1분 제한(EGW00133/EGW00 계열) 또는 메시지에 '1분' 포함 시 백오프 후 재시도
            is_rate_limited = (
                "EGW00133" in str(error_code)
                or "EGW00" in str(error_code)
                or "1분" in msg1
                or "1 minute" in msg1.lower()
            )
            if is_rate_limited and mint_attempt < 3:
                wait_s = min(70, 15 + mint_attempt * 20)
                log.warning(
                    "token: rate/window limit (attempt %d/4). sleeping %ds: %s",
                    mint_attempt + 1,
                    wait_s,
                    msg1[:120],
                )
                time.sleep(wait_s)
                continue

            return None

        return None


def get_kis_hashkey(body: dict) -> str:
    """
    문서: /uapi/hashkey
    - POST 주문/정정/취소 등의 body 값을 해시로 서명하는 용도.
    - 문서상 비필수이지만, 적용 시 중간 변조 방지 목적.
    """
    url = f"{URL_BASE}/uapi/hashkey"
    headers = {
        "content-type": "application/json",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
    }
    try:
        res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
        res_data = _safe_json(res)
        return res_data.get("HASH", "") or ""
    except Exception:
        return ""


_MAX_API_RETRIES = 5


def kis_api_call(method, url, headers, params=None, data=None):
    if not headers:
        return None
    auth = headers.get("authorization") or ""
    if not auth.strip() or auth == "Bearer None":
        log.debug("kis_api_call: skip — invalid authorization")
        return None

    safe_headers = dict(headers)
    token_refreshed_once = False

    for attempt in range(_MAX_API_RETRIES):
        try:
            if method == "GET":
                raw = requests.get(url, headers=safe_headers, params=params, timeout=10)
            elif method == "POST":
                raw = requests.post(url, headers=safe_headers, data=json.dumps(data) if data else None, timeout=10)
            else:
                return None

            # Rate Limit(EGW00201) 감지
            raw_text = raw.text or ""
            if "초당 거래건수를 초과" in raw_text or "EGW00201" in raw_text:
                if attempt < _MAX_API_RETRIES - 1:
                    wait = 1.5 * (attempt + 1)
                    log.warning("kis_api: Rate Limit (attempt %d/%d), sleeping %.1fs", attempt + 1, _MAX_API_RETRIES, wait)
                    time.sleep(wait)
                    continue

            res = _safe_json(raw)

            # 토큰 만료/무효 응답 감지
            need_refresh = (
                ('만료된 token' in str(res.get('msg1', '')))
                or _kis_token_expired_api_message(res)
            )
            if need_refresh and not token_refreshed_once:
                log.info("kis_api: token expired/invalid, forcing renewal")
                new_token = get_kis_access_token(force_renew=True)
                if new_token:
                    token_refreshed_once = True
                    safe_headers["authorization"] = f"Bearer {new_token}"
                    time.sleep(1.0)
                    continue
            return res

        except (requests.ConnectionError, requests.Timeout, OSError) as e:
            log.warning("kis_api: network error (attempt %d/%d): %s", attempt + 1, _MAX_API_RETRIES, e)
            if attempt == _MAX_API_RETRIES - 1:
                return None
            time.sleep(1.0 * (attempt + 1))

    return None

def query_account_raw(exchange_code=""):
    token = get_kis_access_token()
    if not token:
        log.warning("query_account_raw: no access token")
        return None
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-balance"
    headers = {
        "Content-Type": "application/json", "authorization": f"Bearer {token}",
        # 문서: 해외주식 잔고 TR_ID = TTTS3012R (실전)
        "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "TTTS3012R", "custtype": "P"
    }
    params = {
        "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": exchange_code,
        "TR_CRCY_CD": "USD", "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""
    }
    return kis_api_call("GET", url, headers, params=params)

def query_available_cash(token, debug=False):
    """해외주식 매수가능금액 조회(TTTS3007R). output.ord_psbl_frcr_amt에서 외화 주문가능금액을 추출합니다."""
    logs = []
    if not token:
        if debug:
            return 0.0, "토큰없음", logs
        return 0.0, "토큰없음"

    try:
        url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-psamount"
        headers = {
            "Content-Type": "application/json", "authorization": f"Bearer {token}",
            "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "TTTS3007R", "custtype": "P"
        }
        # [WARNING] OVRS_EXCG_CD가 NASD일 때, ITEM_CD 역시 나스닥 상장 종목(TQQQ 등)이어야 정상 연동됩니다.
        # SOXL(AMEX)과 NASD 조합은 KIS API 매칭 실패를 유발하므로 절대 변경하지 마십시오.
        params = {
            "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "OVRS_EXCG_CD": "NASD", "OVRS_ORD_UNPR": "1", "ITEM_CD": "TQQQ"
        }
        res = kis_api_call("GET", url, headers, params=params)
        if res is None:
            logs.append("psamount: ERROR no response")
            if debug:
                return 0.0, "조회실패", logs
            return 0.0, "조회실패"

        rt_cd = res.get('rt_cd', '?')
        if rt_cd == '0':
            val = float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
            logs.append(f"psamount: OK -> ${val:,.2f}")
            if debug:
                return val, "psamount", logs
            return val, "psamount"
        else:
            msg1 = res.get('msg1', '')
            logs.append(f"psamount: FAIL rt_cd={rt_cd} msg={msg1[:80]}")
    except Exception as e:
        logs.append(f"psamount: ERROR {str(e)[:80]}")

    if debug:
        return 0.0, "조회실패", logs
    return 0.0, "조회실패"


def query_krw_cash(token):
    """국내 주식 주문가능금액 조회 TR을 사용하여 원화 잔고를 반환합니다."""
    if not token:
        return 0.0
    url = f"{URL_BASE}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
    headers = {
        "Content-Type": "application/json", "authorization": f"Bearer {token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "TTTC8908R", "custtype": "P"
    }
    # 국내 종목 아무거나 하나 필요함 (삼성전자: 005930)
    params = {
        "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "PDNO": "005930",
        "ORD_UNPR": "0", "ORD_DVSN": "00", "CMA_EVLU_AMT_ICLD_YN": "Y", "OVRS_ICLD_YN": "N"
    }
    try:
        res = kis_api_call("GET", url, headers, params=params)
        if res and res.get('rt_cd') == '0':
            return float(res.get('output', {}).get('ord_psbl_cash', 0))
    except Exception as e:
        log.warning("KRW ord_psbl_cash 조회 실패: %s", e)
    return 0.0

def get_exchange_rate():
    """yfinance를 사용하여 USD/KRW 실시간 환율과 기준 시각을 반환합니다."""
    import yfinance as yf
    try:
        ticker = yf.Ticker("USDKRW=X")
        # fast_info를 사용하여 최신 가격과 시간 추출
        info = ticker.fast_info
        rate = info['last_price']
        # 타임스탬프를 datetime 객체로 변환 (UTC -> KST)
        from datetime import datetime
        import pytz
        kst = pytz.timezone('Asia/Seoul')
        ref_time = datetime.now(kst).strftime('%Y-%m-%d %H:%M:%S')
        return rate, ref_time
    except Exception as e:
        log.warning("환율 조회 실패: %s", e)
        return 0.0, "N/A"


def get_exchange_code(ticker):
    """
    [CRITICAL] KIS 거래소 코드 반환. 
    실전투자 주문 API 기준 TQQQ=NASD, SOXL=AMEX를 사용합니다.
    TQQQ(나스닥)와 SOXL(아멕스)의 상장 거래소가 상이하므로, 리팩토링 시 절대 하드코딩하거나 통일하지 마십시오.
    """
    tk = str(ticker).strip().upper()
    if tk in ("TQQQ", "SQQQ", "TSLA", "AAPL", "NVDA", "MSFT", "GOOGL"):
        return "NASD"
    elif tk in ("SOXL",):
        return "AMEX"
    # 기본 fallback
    return "NASD"

def send_order(ticker, qty, price, side="BUY", order_type="00", max_retries=2):
    actual_order_type = order_type
    if order_type == "LOC": actual_order_type = "34"
    elif order_type == "LOO": actual_order_type = "02"
    elif order_type == "MOC": actual_order_type = "33"
    elif order_type == "MOO": actual_order_type = "31"

    for attempt in range(max_retries + 1):
        try:
            token = get_kis_access_token()
            if not token:
                return {"rt_cd": "-1", "msg1": "access_token 없음"}
            url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/order"
            tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
            excg_cd = get_exchange_code(ticker)

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

            # 문서: hashkey는 비필수이나, POST body 변조 방지 목적의 보안 요소로 사용 가능
            hk = get_kis_hashkey(body)
            if hk:
                headers = dict(headers)
                headers["hashkey"] = hk

            res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
            result = _safe_json(res)

            if result.get('rt_cd') == '0':
                return result

            if attempt < max_retries:
                time.sleep(1)
                continue
            return result
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1)
                continue
            return {"rt_cd": "-1", "msg1": f"통신오류: {str(e)}"}

def cancel_order(ticker, order_no, qty, max_retries=2):
    """
    해외주식 주문 취소 (TTTT1004U).
    미체결 지정가 매도 등을 수동 취소할 때 사용합니다.

    Args:
        ticker: 종목 코드
        order_no: 원 주문번호 (ODNO)
        qty: 취소 수량
        max_retries: 최대 재시도 횟수
    Returns:
        KIS API 응답 dict
    """
    for attempt in range(max_retries + 1):
        try:
            token = get_kis_access_token()
            if not token:
                return {"rt_cd": "-1", "msg1": "access_token 없음"}
            url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/order-rvsecncl"
            excg_cd = get_exchange_code(ticker)

            headers = {
                "Content-Type": "application/json", "authorization": f"Bearer {token}",
                "appkey": APP_KEY, "appsecret": APP_SECRET,
                # 문서: 해외주식 주문정정취소 TR_ID = TTTT1004U (실전)
                "tr_id": "TTTT1004U", "custtype": "P"
            }

            body = {
                "CANO": CANO, "ACNT_PRDT_CD": ACNT_PRDT_CD, "OVRS_EXCG_CD": excg_cd,
                "PDNO": ticker,
                "ORGN_ODNO": str(order_no),
                "RVSE_CNCL_DVSN_CD": "02",  # 02 = 취소
                "ORD_QTY": str(int(qty)),
                "OVRS_ORD_UNPR": "0",  # 취소 시 가격 불필요
                "MGCO_APTM_ODNO": "",
                "ORD_SVR_DVSN_CD": "0"
            }

            hk = get_kis_hashkey(body)
            if hk:
                headers = dict(headers)
                headers["hashkey"] = hk

            res = requests.post(url, headers=headers, data=json.dumps(body), timeout=10)
            result = _safe_json(res)

            if result.get('rt_cd') == '0':
                log.info("cancel_order success: ticker=%s, odno=%s, qty=%d", ticker, order_no, qty)
                return result

            if attempt < max_retries:
                time.sleep(1)
                continue
            log.warning("cancel_order failed: ticker=%s, odno=%s, result=%s", ticker, order_no, result)
            return result
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1)
                continue
            return {"rt_cd": "-1", "msg1": f"통신오류: {str(e)}"}


def get_price_exchange_code(ticker):
    """
    [CRITICAL] KIS 시세 조회용 거래소 코드 반환. 
    미국 시세 API는 나스닥=NAS, 아멕스=AMS, 뉴욕=NYS를 사용합니다.
    TQQQ(NAS)와 SOXL(AMS)의 시세 조회용 거래소 코드가 상이하므로 절대 통일하거나 하드코딩하지 마십시오.
    """
    tk = str(ticker).strip().upper()
    if tk in ("TQQQ", "SQQQ", "TSLA", "AAPL", "NVDA", "MSFT", "GOOGL"):
        return "NAS"
    elif tk in ("SOXL",):
        return "AMS"
    return "NAS"


def get_current_price(ticker):
    """해외주식 현재가 조회. KIS API 1회 호출, 실패 시 yfinance fallback."""
    from utils import get_market_session
    import yfinance as yf
    
    session = get_market_session()
    if session == "regular":
        token = None
        try:
            token = get_kis_access_token()
        except Exception:
            pass
        if token:
            try:
                url = f"{URL_BASE}/uapi/overseas-price/v1/quotations/price"
                headers = {
                    "Content-Type": "application/json",
                    "authorization": f"Bearer {token}",
                    "appkey": APP_KEY,
                    "appsecret": APP_SECRET,
                    "tr_id": "HHDFS00000300",
                    "custtype": "P",
                }
                params = {"AUTH": "", "EXCD": get_price_exchange_code(ticker), "SYMB": ticker}
                res = kis_api_call("GET", url, headers, params=params)
                if res and res.get('rt_cd') == '0':
                    last = float(res.get('output', {}).get('last', '0'))
                    if last > 0:
                        return last
            except Exception:
                pass

    # yfinance fallback (fast_info)
    try:
        stock = yf.Ticker(ticker)
        price = stock.fast_info['last_price']
        if price and price > 0:
            return price
    except Exception:
        pass

    # yfinance fallback (history)
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")
        if hist is not None and not hist.empty:
            return float(hist['Close'].iloc[-1])
    except Exception:
        pass

    return 0.0


def get_prev_close(ticker):
    import yfinance as yf

    # KIS API: NAS 고정 1회 시도 (base = 전일 종가)
    try:
        token = get_kis_access_token()
        if token:
            try:
                url = f"{URL_BASE}/uapi/overseas-price/v1/quotations/price"
                headers = {
                    "Content-Type": "application/json",
                    "authorization": f"Bearer {token}",
                    "appkey": APP_KEY,
                    "appsecret": APP_SECRET,
                    "tr_id": "HHDFS00000300",
                    "custtype": "P"
                }
                params = {"AUTH": "", "EXCD": get_price_exchange_code(ticker), "SYMB": ticker}
                res = kis_api_call("GET", url, headers, params=params)
                if res and res.get('rt_cd') == '0':
                    base = float(res.get('output', {}).get('base', '0'))
                    if base > 0:
                        return base
            except Exception:
                pass
    except Exception:
        pass
    
    # yfinance fallback
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="5d")
        if hist is not None and not hist.empty:
            if len(hist) >= 2:
                return float(hist['Close'].iloc[-2])
            return float(hist['Close'].iloc[-1])
    except Exception:
        pass

    return 0.0

def get_avg_close_last_n_days(ticker: str, n: int = 5) -> float:
    """직전 n거래일 종가 평균(V4.0 리버스 별지점). 장중에는 오늘의 미완성 봉을 제외한다. 실패 시 0."""
    import pytz
    import yfinance as yf
    if n < 1:
        return 0.0
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{max(n, 5) + 5}d")
        if hist is None or hist.empty:
            return 0.0
        now_ny = datetime.datetime.now(pytz.timezone("America/New_York"))
        if hist.index[-1].date() == now_ny.date() and now_ny.hour < 16:
            hist = hist.iloc[:-1]  # 오늘 봉은 아직 종가가 아니다
        closes = hist["Close"].dropna().iloc[-n:]
        if closes.empty:
            return 0.0
        return float(closes.mean())
    except Exception:
        return 0.0


def query_ccnl_raw(ticker="", excg="NASD", start_dt=None, end_dt=None, ccld_dvsn="00", ctx_nk200="", ctx_fk200="", tr_cont=""):
    """
    해외주식 주문/체결 내역을 조회 (TTTS3035R).
    - 문서: ORD_STRT_DT/ORD_END_DT는 현지(미국) 날짜 기준. 전종목 조회 시 PDNO="%".
    - 연속조회: ctx_nk200/ctx_fk200에 이전 응답의 ctx_area_nk200/ctx_area_fk200, tr_cont="N" 전달.
    - 응답 필드명은 소문자로 반환됨에 주의.
    """
    token = get_kis_access_token()
    if not token:
        return {"rt_cd": "-1", "msg1": "access_token 없음"}
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-ccnl"
    headers = {
        "Content-Type": "application/json", "authorization": f"Bearer {token}",
        "appkey": APP_KEY, "appsecret": APP_SECRET, "tr_id": "TTTS3035R", "custtype": "P"
    }
    if tr_cont:
        headers["tr_cont"] = tr_cont

    if not start_dt or not end_dt:
        # 문서상 조회일은 미국 현지 기준. KST 날짜를 쓰면 일자가 하루 어긋나 주문이 누락될 수 있음.
        import pytz

        ny_tz = pytz.timezone("America/New_York")
        now_ny = datetime.datetime.now(ny_tz)
        end_dt = end_dt or now_ny.strftime("%Y%m%d")
        start_dt = start_dt or (now_ny - datetime.timedelta(days=7)).strftime("%Y%m%d")

    # 문서: 전종목 조회 시 PDNO="%" 입력
    pdno = "%" if ticker == "" else ticker
    params = {
        "CANO": CANO,
        "ACNT_PRDT_CD": ACNT_PRDT_CD,
        "PDNO": pdno,
        "ORD_STRT_DT": start_dt,
        "ORD_END_DT": end_dt,
        "SLL_BUY_DVSN": "00",
        "CCLD_NCCS_DVSN": ccld_dvsn,
        "OVRS_EXCG_CD": excg,
        "SORT_SQN": "DS",
        "ORD_DT": "",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "CTX_AREA_NK200": ctx_nk200 or "",
        "CTX_AREA_FK200": ctx_fk200 or ""
    }
    
    try:
        res = _safe_json(requests.get(url, headers=headers, params=params, timeout=10))
        return res
    except Exception as e:
        return {"rt_cd": "-1", "msg1": str(e)}


def query_ccnl_collect_all_pages(
    ticker: str,
    excg: str,
    start_dt: str,
    end_dt: str,
    ccld_dvsn: str = "01",
    max_pages: int = 40,
) -> list:
    """주문체결내역 연속조회로 기간 전체 행을 모읍니다."""
    acc = []
    nk, fk, tr_cont = "", "", ""
    for _ in range(max_pages):
        res = query_ccnl_raw(
            ticker=ticker,
            excg=excg,
            start_dt=start_dt,
            end_dt=end_dt,
            ccld_dvsn=ccld_dvsn,
            ctx_nk200=nk,
            ctx_fk200=fk,
            tr_cont=tr_cont,
        )
        if res.get("rt_cd") != "0":
            break
        acc.extend(res.get("output", []) or [])
        nk = res.get("ctx_area_nk200", "") or ""
        fk = res.get("ctx_area_fk200", "") or ""
        cont = (res.get("tr_cont") or "").strip()
        if cont in ("M", "F"):
            tr_cont = "N"
        else:
            break
    return acc


# 미국 주식은 거래소별로 API가 갈라지므로 단일 OVRS_EXCG_CD 추정만으로는 체결이 누락될 수 있어 3곳 모두 조회합니다.
_US_EXCHANGES_ALL: tuple[str, ...] = ("NASD", "NYSE", "AMEX")


def query_ccnl_collect_all_pages_multi_excg(
    ticker: str,
    start_dt: str,
    end_dt: str,
    ccld_dvsn: str = "01",
    max_pages: int = 8,
) -> list:
    """
    NASD 1회 조회로 미국 전체(나스닥+뉴욕+아멕스) 체결 내역을 수집합니다.
    MCP 문서: ovrs_excg_cd에 NASD 입력 시 미국시장 전체 조회.
    """
    return query_ccnl_collect_all_pages(
        ticker, "NASD", start_dt, end_dt, ccld_dvsn=ccld_dvsn, max_pages=max_pages
    )




def query_ccnl_for_report(excg_list=None, days_back=3, max_pages=2):
    """
    미국 시장 기준 전일·당일 주문체결 내역을 수집해 리포트용 리스트로 반환.
    MCP 문서 기준 NASD 1회 조회로 미국 전체 체결을 수집합니다.
    excg_list 파라미터는 하위 호환용으로 유지하되 내부에서 무시합니다.
    """
    _ = excg_list
    import pytz
    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.datetime.now(ny_tz)
    end_dt = now_ny.strftime("%Y%m%d")
    start_dt = (now_ny - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
    all_orders = []
    nk200, fk200 = "", ""
    tr_cont = ""
    for _ in range(max_pages):
        res = query_ccnl_raw(
            ticker="",
            excg="NASD",
            start_dt=start_dt,
            end_dt=end_dt,
            ccld_dvsn="01",
            ctx_nk200=nk200,
            ctx_fk200=fk200,
            tr_cont=tr_cont,
        )
        if res.get("rt_cd") != "0":
            break
        out = res.get("output", [])
        if out:
            all_orders.extend(out)
        nk200 = res.get("ctx_area_nk200", "") or ""
        fk200 = res.get("ctx_area_fk200", "") or ""
        cont = (res.get("tr_cont") or "").strip()
        if cont in ("M", "F"):
            tr_cont = "N"
        else:
            break
    return all_orders


def query_recent_orders(ticker: str, days_back: int = 5, max_pages: int = 3):
    """
    특정 종목의 최근 주문/체결 내역을 조회합니다.
    - 문서: 해외주식 주문체결내역(/inquire-ccnl) TTTS3035R
    - 반환: 원본 output 배열(소문자 키) 리스트
    """
    import pytz

    ny_tz = pytz.timezone("America/New_York")
    now_ny = datetime.datetime.now(ny_tz)
    end_dt = now_ny.strftime("%Y%m%d")
    start_dt = (now_ny - datetime.timedelta(days=days_back)).strftime("%Y%m%d")

    excg = get_exchange_code(ticker)
    nk200, fk200 = "", ""
    tr_cont = ""
    out = []

    for _ in range(max_pages):
        res = query_ccnl_raw(
            ticker=ticker,
            excg=excg,
            start_dt=start_dt,
            end_dt=end_dt,
            ccld_dvsn="00",  # 전체(체결/미체결)
            ctx_nk200=nk200,
            ctx_fk200=fk200,
            tr_cont=tr_cont,
        )
        if res.get("rt_cd") != "0":
            break
        rows = res.get("output", []) or []
        if rows:
            out.extend(rows)

        nk200 = res.get("ctx_area_nk200", "") or ""
        fk200 = res.get("ctx_area_fk200", "") or ""
        cont = (res.get("tr_cont") or "").strip()
        if cont in ("M", "F"):
            tr_cont = "N"
        else:
            break

    return out


def fills_by_order(ticker: str, days_back: int = 10, max_pages: int = 6) -> dict[str, tuple[int, float]] | None:
    """
    주문번호 → (누적 체결수량, 체결금액 $). 조회 자체가 실패하면 None (미체결과 구분하기 위함).
    부분체결 행이 여러 줄이면 누적수량 증가분 × 해당 행 체결가로 합산한다.
    """
    import pytz

    now_ny = datetime.datetime.now(pytz.timezone("America/New_York"))
    end_dt = now_ny.strftime("%Y%m%d")
    start_dt = (now_ny - datetime.timedelta(days=days_back)).strftime("%Y%m%d")
    excg = get_exchange_code(ticker)
    rows, nk, fk, tr_cont = [], "", "", ""
    for page in range(max_pages):
        res = query_ccnl_raw(ticker=ticker, excg=excg, start_dt=start_dt, end_dt=end_dt,
                             ccld_dvsn="00", ctx_nk200=nk, ctx_fk200=fk, tr_cont=tr_cont)
        if res.get("rt_cd") != "0":
            if page == 0:
                return None
            break
        rows.extend(res.get("output", []) or [])
        nk, fk = res.get("ctx_area_nk200", "") or "", res.get("ctx_area_fk200", "") or ""
        if (res.get("tr_cont") or "").strip() not in ("M", "F"):
            break
        tr_cont = "N"

    out: dict[str, tuple[int, float]] = {}
    for o in sorted(rows, key=lambda r: (str(r.get("ord_dt", "")), str(r.get("ord_tmd", "")))):
        try:
            q = int(float(o.get("ft_ccld_qty", 0) or 0))
            px = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", 0)) or 0)
        except (TypeError, ValueError):
            continue
        for key in {str(o.get("odno", "") or "").strip(), str(o.get("orgn_odno", "") or "").strip()} - {""}:
            last_q, total = out.get(key, (0, 0.0))
            if q > last_q:
                out[key] = (q, total + (q - last_q) * px)
    return out













def _rows_to_unfilled_entries(rows):
    """체결내역 API output 행들에서 미체결 항목만 추출."""
    orders = []
    for o in rows or []:
        try:
            ord_qty = int(float(o.get("ft_ord_qty", 0)))
            ccld_qty = int(float(o.get("ft_ccld_qty", 0)))
            nccs_qty = int(float(o.get("nccs_qty", 0) or 0))

            has_unfilled = nccs_qty > 0
            if not has_unfilled:
                has_unfilled = ord_qty > ccld_qty

            if not has_unfilled:
                continue

            side_cd = o.get("sll_buy_dvsn_cd")
            side = "매수" if side_cd == "02" else "매도"
            qty = nccs_qty if nccs_qty > 0 else (ord_qty - ccld_qty)

            price = float(o.get("ft_ord_unpr3", o.get("ft_ord_unpr", 0)))
            if price == 0:
                price = float(o.get("ft_ccld_unpr3", o.get("ft_ccld_unpr", 0)))

            order_type_name = o.get("sll_buy_dvsn_cd_name", "")
            status = o.get("prcs_stat_name", "대기")
            ord_dt = (o.get("ord_dt") or "").strip()
            ord_tmd = (o.get("ord_tmd") or "").strip()

            orders.append(
                {
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "type": order_type_name,
                    "status": status,
                    "ord_no": o.get("odno"),
                    "ord_dt": ord_dt,
                    "ord_tmd": ord_tmd,
                    "ord_qty": ord_qty,
                    "ccld_qty": ccld_qty,
                    "nccs_qty": nccs_qty,
                }
            )
        except Exception:
            pass
    return orders


def query_unfilled_orders_raw(ticker: str = "", max_pages: int = 5) -> list:
    """
    해외주식 미체결내역 API(TTTS3018R)를 호출하여 연속조회로 미체결 주문 행들을 반환합니다.
    """
    token = get_kis_access_token()
    if not token:
        return []
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-nccs"
    base_headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3018R",
        "custtype": "P",
    }
    
    rows = []
    nk200, fk200 = "", ""
    tr_cont = ""
    for _ in range(max_pages):
        headers = dict(base_headers)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "OVRS_EXCG_CD": get_exchange_code(ticker),
            "SORT_SQN": "DS",
            "CTX_AREA_NK200": nk200,
            "CTX_AREA_FK200": fk200,
        }
        res = kis_api_call("GET", url, headers, params=params)
        if not res or res.get("rt_cd") != "0":
            break
        
        out = res.get("output", [])
        if out:
            rows.extend(out)
            
        nk200 = res.get("ctx_area_nk200", "") or ""
        fk200 = res.get("ctx_area_fk200", "") or ""
        cont = (res.get("tr_cont") or "").strip()
        if cont in ("M", "F"):
            tr_cont = "N"
        else:
            break
            
    if ticker:
        ticker = ticker.strip().upper()
        rows = [r for r in rows if (r.get("pdno") or "").strip().upper() == ticker]
        
    return rows


def query_unfilled_orders_today_us(ticker: str) -> list:
    """
    미국 동부 당일 주문일(ord_dt)인 미체결만 반환.
    스케줄 확인·/status·수동주문 등 미체결 표시는 모두 이 함수로 통일합니다.
    """
    import pytz

    ny_tz = pytz.timezone("America/New_York")
    today_ymd = datetime.datetime.now(ny_tz).strftime("%Y%m%d")
    raw_rows = query_unfilled_orders_raw(ticker)
    orders = _rows_to_unfilled_entries(raw_rows)
    from utils import normalize_ord_yyyymmdd

    today_orders = [
        o for o in orders if normalize_ord_yyyymmdd(o.get("ord_dt")) == today_ymd
    ]
    unique_orders = []
    seen_nos = set()
    for o in today_orders:
        no = o.get("ord_no")
        if no in seen_nos:
            continue
        seen_nos.add(no)
        unique_orders.append(o)
    return unique_orders




def _flatten_kis_output_blocks(res: dict) -> list:
    """output / output1 / output2 를 한 리스트로 합칩니다(단일 dict는 1행)."""
    acc = []
    if not res:
        return acc
    for key in ("output", "output1", "output2"):
        v = res.get(key)
        if v is None:
            continue
        if isinstance(v, list):
            acc.extend(v)
        elif isinstance(v, dict):
            acc.append(v)
    return acc


def query_period_profit_all_rows(
    inqr_strt_dt: str,
    inqr_end_dt: str,
    ovrs_excg_cd: str,
    pdno: str = "",
    crcy_cd: str = "USD",
    natn_cd: str = "",
    wcrc_frcr_dvsn_cd: str = "01",
    max_pages: int = 30,
) -> list:
    """
    해외주식 기간손익 API (문서 v1_해외주식-032, URL inquire-period-profit, 실전 TR TTTS3039R).
    연속조회로 output1·output2 행을 모두 합쳐 반환합니다.
    """
    token = get_kis_access_token()
    if not token:
        return []
    url = f"{URL_BASE}/uapi/overseas-stock/v1/trading/inquire-period-profit"
    base_headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "TTTS3039R",
        "custtype": "P",
    }
    rows = []
    nk, fk = "", ""
    tr_cont = ""
    for _ in range(max_pages):
        headers = dict(base_headers)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        params = {
            "CANO": CANO,
            "ACNT_PRDT_CD": ACNT_PRDT_CD,
            "OVRS_EXCG_CD": ovrs_excg_cd,
            "NATN_CD": natn_cd,
            "CRCY_CD": crcy_cd,
            "PDNO": pdno,
            "INQR_STRT_DT": inqr_strt_dt,
            "INQR_END_DT": inqr_end_dt,
            "WCRC_FRCR_DVSN_CD": wcrc_frcr_dvsn_cd,
            "CTX_AREA_NK200": nk,
            "CTX_AREA_FK200": fk,
        }
        res = kis_api_call("GET", url, headers, params=params)
        if res.get("rt_cd") != "0":
            log.warning(
                "period-profit rt_cd=%s msg=%s exc=%s",
                res.get("rt_cd"),
                (res.get("msg1") or "")[:120],
                ovrs_excg_cd,
            )
            break
        rows.extend(_flatten_kis_output_blocks(res))
        nk = res.get("ctx_area_nk200", "") or ""
        fk = res.get("ctx_area_fk200", "") or ""
        cont = (res.get("tr_cont") or "").strip()
        if cont in ("M", "F"):
            tr_cont = "N"
        else:
            break
    return rows




def _kis_parse_ymd_to_date(val) -> datetime.date | None:
    if val is None:
        return None
    s = str(val).strip().replace("-", "")
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime.datetime.strptime(s[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return None


def _kis_row_ticker(row: dict) -> str:
    for k in ("ovrs_pdno", "pdno", "symb", "prdt_cd", "ovrs_item_cd", "item_cd"):
        v = row.get(k)
        if v is None:
            continue
        t = str(v).strip().upper()
        if t and t != "%":
            return t
    return ""


def _kis_row_pick_float(row: dict, keys: tuple) -> float | None:
    for k in keys:
        if k not in row:
            continue
        try:
            return float(str(row[k]).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            continue
    return None


def _kis_row_day_flow_usd(row: dict) -> float:
    """
    일자별 손익·증감으로 쓸 수 있는 값 1개를 고릅니다(문서·버전에 따라 필드명이 다를 수 있음).
    전일대비·당일변동 계열(vrss, thdt 등)을 우선합니다.
    """
    priority = (
        "thdt_evlu_pfls_vrss",
        "evlu_pfls_vrss",
        "evlu_pfls_vrss_amt",
        "d1_evlu_pfls_vrss",
        "bfwr_evlu_pfls_vrss",
        "ovrs_rlz_pl_amt",
        "rlz_pl_amt",
        "frcr_rlz_pl_amt",
        "frcr_rlz_pl",
        "ovrs_evlu_pfls_amt",
        "evlu_pfls_amt",
        "frcr_evlu_pfls_amt",
        "stk_evlu_pfls_amt",
        "deal_pfls_amt",
        "ntby_amt",
        "frcr_ntby_amt",
        "ovrs_tot_pfls",
    )
    for k in priority:
        v = _kis_row_pick_float(row, (k,))
        if v is not None and v != 0:
            return v
    for k in priority:
        v = _kis_row_pick_float(row, (k,))
        if v is not None:
            return v
    best = 0.0
    for k, v in row.items():
        kl = str(k).lower()
        if not any(x in kl for x in ("pfls", "rlz_pl", "evlu_pfls", "ntby", "vrss")):
            continue
        if "tot" in kl and "ovrs_tot" not in kl:
            continue
        try:
            x = float(str(v).replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            continue
        if x != 0:
            best = x
    return best




def _kis_row_account_ledger_date(row: dict) -> datetime.date | None:
    """
    기간손익(TTTS3039R) 일자별 차트용 기준일. 결제일(stlm)보다 거래·체결 일자를 우선
    (stlm을 먼저 쓰면 미국 장마감 3/20이 결제일로 밀려 차트/필터에서 빠질 수 있음).
    """
    for k in (
        "trad_dt",
        "tr_dt",
        "erlm_dt",
        "ord_dt",
        "deal_dt",
        "cntg_dt",
        "ccld_dt",
        "frm_dt",
        "trde_dt",
        "stlm_dt",
        "stlm_date",
        "ovrs_stlm_dt",
    ):
        d = _kis_parse_ymd_to_date(row.get(k))
        if d:
            return d
    return None


def _dedupe_period_profit_rows(rows: list) -> list:
    """동일 (기준일, 종목) 행이 NASD/NYSE/AMEX 중복 조회로 여러 번 올 수 있어 1행으로 줄입니다."""
    by_key: dict[tuple, dict] = {}
    for row in rows:
        sym = _kis_row_ticker(row)
        if not sym or sym == "%":
            continue
        d = _kis_row_account_ledger_date(row)
        if not d:
            continue
        k = (d, sym)
        fl = _kis_row_day_flow_usd(row)
        if k not in by_key:
            by_key[k] = row
            continue
        prev_f = _kis_row_day_flow_usd(by_key[k])
        if prev_f == 0 and fl != 0:
            by_key[k] = row
        elif fl != 0 and prev_f != 0 and abs(fl) > abs(prev_f):
            by_key[k] = row
    return list(by_key.values())








def account_daily_pnl_dataframe(start_ymd: str, end_ymd: str) -> tuple[object | None, str]:
    """
    해외주식 기간손익 API TTTS3039R (inquire-period-profit): PDNO 비우고 전체 조회 후
    (일자, 종목) 중복 제거 → 일자별 달러 변동 합산. **계좌 API 응답**이며 시세 API가 아님.
    MTS '손익추이'와 유사하나 필드·반영 시점 차이가 있을 수 있음.
    """
    import pandas as pd

    start_d = _kis_parse_ymd_to_date(start_ymd)
    end_d = _kis_parse_ymd_to_date(end_ymd)
    if not start_d or not end_d:
        return None, "bad date range"

    # 조회 종료일을 며칠 넓혀 장마감 직후·결제 반영 전에 API가 늦게 붙는 행을 받기 위함
    end_q = end_d + datetime.timedelta(days=7)
    end_query_ymd = end_q.strftime("%Y%m%d")

    all_rows = query_period_profit_all_rows(start_ymd, end_query_ymd, "", pdno="", max_pages=50)
    note = f"TTTS3039R 원시행={len(all_rows)}"
    if not all_rows:
        return None, note + " | 응답 없음"

    deduped = _dedupe_period_profit_rows(all_rows)
    note += f" dedupe={len(deduped)}"

    rec = []
    for row in deduped:
        sym = _kis_row_ticker(row)
        d = _kis_row_account_ledger_date(row)
        if not sym or not d:
            continue
        fl = _kis_row_day_flow_usd(row)
        rec.append({"date": pd.Timestamp(d), "daily_pnl": fl})

    if not rec:
        return None, note + " | 파싱 불가"

    df = pd.DataFrame(rec)
    df = df.groupby("date", as_index=False)["daily_pnl"].sum().sort_values("date")
    # 증권사는 결제일·한국 일자 등으로 찍히는 경우가 있어 NYSE 캘린더로 걸러내지 않음.
    # 표시·누적은 호출자가 넘긴 end_d(조회 기준 '오늘')까지의 일자만 사용.
    df["_cd"] = df["date"].dt.normalize().dt.date
    df = df[(df["_cd"] >= start_d) & (df["_cd"] <= end_d)].drop(columns=["_cd"])
    if df.empty:
        return None, note + f" | 기간필터({start_d}~{end_d}) 후 비어있음"

    df["cum_pnl"] = df["daily_pnl"].cumsum()
    return df.reset_index(drop=True), note + f" | 일자={len(df)}"


