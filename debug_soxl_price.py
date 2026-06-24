"""SOXL 시세 조회 디버깅 — NAS Docker에서 실행하세요.

3가지 거래소 코드(NAS/NYS/AMS)와 yfinance 조회를 모두 시도하여
어떤 경로에서 실패하는지 진단합니다.

사용법: python3 debug_soxl_price.py
"""
import json, requests, os, sys

# 환경 로드
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_manager import APP_KEY, APP_SECRET
from kis_client import get_kis_access_token, URL_BASE

ticker = "SOXL"
token = get_kis_access_token()
print(f"[1] KIS 토큰: {'있음' if token else '없음(치명적)'}")
if not token:
    print("토큰 없이는 KIS API 사용 불가. APP_KEY/APP_SECRET 확인 필요.")

# KIS 시세 API를 3개 거래소 코드로 모두 시도
for excg in ["NAS", "NYS", "AMS"]:
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
        params = {"AUTH": "", "EXCD": excg, "SYMB": ticker}
        res = requests.get(url, headers=headers, params=params, timeout=10)
        data = res.json()
        rt_cd = data.get("rt_cd", "?")
        last_price = data.get("output", {}).get("last", "0")
        base_price = data.get("output", {}).get("base", "0")
        msg1 = data.get("msg1", "")
        print(f"[2] KIS 시세 EXCD={excg}: rt_cd={rt_cd}, last={last_price}, base={base_price}, msg={msg1[:80]}")
    except Exception as e:
        print(f"[2] KIS 시세 EXCD={excg}: 예외 - {e}")

# yfinance 조회
try:
    import yfinance as yf
    t = yf.Ticker(ticker)
    fi = t.fast_info
    print(f"[3] yfinance exchange: {fi.get('exchange', 'N/A')}")
    print(f"[3] yfinance last_price: {fi.get('last_price', 'N/A')}")
    print(f"[3] yfinance previous_close: {fi.get('previous_close', 'N/A')}")
except Exception as e:
    print(f"[3] yfinance 실패: {e}")

# yfinance history 방식도 시도
try:
    import yfinance as yf
    stock = yf.Ticker(ticker)
    hist = stock.history(period="5d")
    if hist is not None and not hist.empty:
        print(f"[4] yfinance history (최근 5일):")
        for idx, row in hist.iterrows():
            print(f"    {idx.strftime('%Y-%m-%d')}: Close={row['Close']:.2f}")
    else:
        print(f"[4] yfinance history: 비어있음")
except Exception as e:
    print(f"[4] yfinance history 실패: {e}")

print("\n=== 결론 ===")
print("KIS API에서 last > 0인 EXCD를 확인하세요. 그것이 올바른 거래소 코드입니다.")
print("yfinance exchange가 BTS/PCX 등인 경우, 코드의 매핑이 올바른지 확인하세요.")
