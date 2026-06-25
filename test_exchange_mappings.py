"""거래소 코드 및 시세 코드 매핑 검증 테스트 스크립트.

리팩토링 시 이 테스트가 무조건 통과해야 합니다.
실행 방법: python3 test_exchange_mappings.py
"""
import sys
import os

# 모듈 경로 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import kis_client
except ImportError as e:
    print(f"Error: ubs_manager_telegram_bot 모듈을 불러올 수 없습니다. - {e}")
    sys.exit(1)

def test_exchange_codes():
    print("Testing get_exchange_code()...")
    # 나스닥 계열 종목 테스트
    assert kis_client.get_exchange_code("TQQQ") == "NASD", "TQQQ 주문 거래소는 NASD여야 합니다."
    assert kis_client.get_exchange_code("SQQQ") == "NASD", "SQQQ 주문 거래소는 NASD여야 합니다."
    
    # 아멕스 계열 종목 테스트 (가장 중요)
    assert kis_client.get_exchange_code("SOXL") == "AMEX", "SOXL 주문 거래소는 AMEX여야 합니다."
    print("✓ get_exchange_code() 테스트 통과!")

def test_price_exchange_codes():
    print("Testing get_price_exchange_code()...")
    # 나스닥 계열 시세 코드 테스트
    assert kis_client.get_price_exchange_code("TQQQ") == "NAS", "TQQQ 시세 거래소는 NAS여야 합니다."
    
    # 아멕스 계열 시세 코드 테스트 (가장 중요)
    assert kis_client.get_price_exchange_code("SOXL") == "AMS", "SOXL 시세 거래소는 AMS여야 합니다."
    print("✓ get_price_exchange_code() 테스트 통과!")

def test_query_available_cash_params():
    print("Testing query_available_cash query parameter rules...")
    # query_available_cash의 파라미터는 반드시 TQQQ와 NASD 조합이어야 함
    # (SOXL을 쓰면 KIS API 매칭 실패 유발)
    import inspect
    source = inspect.getsource(kis_client.query_available_cash)
    assert '"OVRS_EXCG_CD": "NASD"' in source, "예수금 조회 시 OVRS_EXCG_CD는 NASD여야 합니다."
    assert '"ITEM_CD": "TQQQ"' in source, "예수금 조회 시 ITEM_CD는 TQQQ여야 합니다."
    print("✓ query_available_cash 파라미터 규칙 검증 통과!")

def main():
    try:
        test_exchange_codes()
        test_price_exchange_codes()
        test_query_available_cash_params()
        print("\n[성공] 모든 거래소 코드 매핑 검증 테스트가 완료되었습니다. 리팩토링 안전 상태입니다.")
    except AssertionError as e:
        print(f"\n[실패] 거래소 코드 매핑 검증 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
