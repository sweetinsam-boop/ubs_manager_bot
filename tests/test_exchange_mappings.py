"""KIS 거래소 코드 매핑 회귀 테스트. TQQQ=NASD(주문)/NAS(시세), SOXL=AMEX/AMS 는 절대 통일하면 안 된다.

의존 패키지(requests, python-dotenv 등)가 없는 환경에서는 건너뛴다. 실행: python3 -m unittest discover tests
"""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import kis_client
except ImportError:  # 로컬에 의존 패키지가 없을 때
    kis_client = None


@unittest.skipIf(kis_client is None, "kis_client import 불가")
class ExchangeMappings(unittest.TestCase):
    def test_order_exchange_codes(self):
        self.assertEqual(kis_client.get_exchange_code("TQQQ"), "NASD")
        self.assertEqual(kis_client.get_exchange_code("SQQQ"), "NASD")
        self.assertEqual(kis_client.get_exchange_code("SOXL"), "AMEX")

    def test_price_exchange_codes(self):
        self.assertEqual(kis_client.get_price_exchange_code("TQQQ"), "NAS")
        self.assertEqual(kis_client.get_price_exchange_code("SOXL"), "AMS")

    def test_available_cash_query_uses_tqqq_nasd(self):
        # SOXL(AMEX)과 NASD 조합은 KIS API 매칭 실패를 일으킨다
        src = inspect.getsource(kis_client.query_available_cash)
        self.assertIn('"OVRS_EXCG_CD": "NASD"', src)
        self.assertIn('"ITEM_CD": "TQQQ"', src)


if __name__ == "__main__":
    unittest.main()
