"""V4.0 규칙 검증. 기대값은 라오어 카페 'V4.0 방법론' 본문 예시에서 가져왔습니다.

실행: python3 -m unittest discover tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import v4_rules as r  # noqa: E402


def _by_kind(plan, kind):
    return [o for o in plan["orders"] if o["kind"] == kind]


class StarFormula(unittest.TestCase):
    def test_official_coefficients(self):
        self.assertAlmostEqual(r.star_pct("TQQQ", 10, 20), 0.0)       # 15-1.5T (설정 없으면 공식 기본값)
        self.assertAlmostEqual(r.star_pct("TQQQ", 10, 40, 15), 7.5)   # 15-0.75T
        self.assertAlmostEqual(r.star_pct("SOXL", 8.6, 20, 20), 2.8)  # 20-2T (카페 예시)
        self.assertAlmostEqual(r.star_pct("SOXL", 10, 40), 10.0)      # 20-T
        self.assertAlmostEqual(r.star_pct("TQQQ", 20, 20), -15.0)     # 마지막 T는 쿼터손절 -15%

    def test_user_can_override_profit_pct(self):
        self.assertEqual(r.limit_pct("TQQQ", 12), 12.0)
        self.assertAlmostEqual(r.star_pct("TQQQ", 10, 20, 12), 12 - 1.2 * 10)  # 기준% 12 → 12-1.2T
        p = r.plan_normal("TQQQ", 12, 20, 5, 4000, 50.0, 40)
        self.assertEqual(p["target"], 56.0)  # 평단 +12%
        self.assertAlmostEqual(r.reverse_exit_price("TQQQ", 100, 12), 88.0)

    def test_star_point_example(self):
        # SOXL 20분할, 평단 38.30, T=8.6 → 별지점 39.37
        self.assertEqual(r.star_point(38.30, r.star_pct("SOXL", 8.6, 20)), 39.37)


class NormalPlan(unittest.TestCase):
    def test_first_half_matches_cafe_table(self):
        # 카페 전반전 표: 별지점 78.12, 평단 69.75, 1회분 약 539.2
        # 40분할 TQQQ T=4 → 별% 12% → 69.75*1.12 = 78.12
        p = r.plan_normal("TQQQ", 15, 40, 4, 539.2 * 36, 69.75, 141)
        self.assertEqual(p["star_point"], 78.12)
        self.assertEqual(p["star_buy"], 78.11)
        self.assertAlmostEqual(p["one_time"], 539.2, places=2)
        star, avg = _by_kind(p, "star")[0], _by_kind(p, "avg")[0]
        self.assertEqual((star["price"], star["qty"]), (78.11, 3))
        self.assertEqual((avg["price"], avg["qty"]), (69.75, 4))
        grids = [o["price"] for o in _by_kind(p, "grid")]
        self.assertEqual(grids[:2], [67.4, 59.91])  # 1회분/(7+1), /(7+2)

    def test_second_half_matches_cafe_table(self):
        # 후반전 표: 별지점 59.55, 매수 59.54 x9, 줍줍 56.85/51.68/47.37
        p = r.plan_normal("TQQQ", 15, 40, 24, 568.48 * 16, 61.39, 100)  # 카페 표의 1회분은 반올림 값
        self.assertEqual(p["star_point"], 59.55)
        star = _by_kind(p, "star")[0]
        self.assertEqual((star["price"], star["qty"]), (59.54, 9))
        self.assertEqual(_by_kind(p, "avg"), [])
        self.assertEqual([o["price"] for o in _by_kind(p, "grid")][:3], [56.85, 51.68, 47.37])

    def test_sell_split_matches_cafe_table(self):
        # 141주 → 쿼터 LOC 35주 + 지정가 106주
        p = r.plan_normal("TQQQ", 15, 40, 24, 9000, 61.39, 141)
        self.assertEqual(_by_kind(p, "sell_quarter")[0]["qty"], 35)
        self.assertEqual(_by_kind(p, "sell_limit")[0]["qty"], 106)
        self.assertEqual(_by_kind(p, "sell_limit")[0]["price"], round(61.39 * 1.15, 2))

    def test_grid_never_above_main_buys(self):
        p = r.plan_normal("SOXL", 20, 20, 15, 800, 30.0, 50)
        buys = [o["price"] for o in p["orders"] if o["kind"] in ("avg", "star")]
        for g in _by_kind(p, "grid"):
            self.assertLess(g["price"], min(buys))


class Reverse(unittest.TestCase):
    def test_exhaustion_threshold(self):
        self.assertFalse(r.is_exhausted(19, 20))
        self.assertTrue(r.is_exhausted(19.01, 20))
        self.assertTrue(r.is_exhausted(39.5, 40))

    def test_day1_moc_floor(self):
        self.assertEqual(r.plan_reverse("TQQQ", 40, 200, 0, 0, True)["orders"][0]["qty"], 10)  # 200/20
        self.assertEqual(r.plan_reverse("TQQQ", 20, 198, 0, 0, True)["orders"][0]["qty"], 19)  # 198/10 내림

    def test_day2_sells_ninth_of_remaining(self):
        p = r.plan_reverse("TQQQ", 40, 190, 700, 48.62, False)
        self.assertEqual(_by_kind(p, "rev_sell")[0]["qty"], 9)
        self.assertEqual(_by_kind(p, "rev_sell")[0]["price"], 48.62)
        self.assertEqual(_by_kind(p, "rev_buy")[0]["price"], 48.61)
        self.assertAlmostEqual(p["quarter_amt"], 175.0)  # 카페 예시: (잔금+매도금)/4

    def test_exit_threshold(self):
        self.assertAlmostEqual(r.reverse_exit_price("SOXL", 40.0), 32.0)   # 카페 예시
        self.assertAlmostEqual(r.reverse_exit_price("TQQQ", 40.0), 34.0)


class Settle(unittest.TestCase):
    def _o(self, kind, filled=1, notional=0.0, half=None):
        return {"kind": kind, "filled": filled, "notional": notional, "half": half}

    def test_first_buy_is_one_turn(self):
        t, pool = r.settle(0, 1000, 20, [self._o("first", notional=478)])
        self.assertEqual((t, pool), (1.0, 522))

    def test_first_half_partial_and_full(self):
        half = self._o("star", half="first"), self._o("avg", half="first")
        self.assertEqual(r.settle(5, 1000, 20, [half[0]])[0], 5.5)
        self.assertEqual(r.settle(5, 1000, 20, list(half))[0], 6.0)

    def test_grid_fill_counts_full_turn(self):
        self.assertEqual(r.settle(5, 1000, 20, [self._o("grid", half="first")])[0], 6.0)

    def test_second_half_one_turn(self):
        self.assertEqual(r.settle(12, 1000, 20, [self._o("star", half="second")])[0], 13.0)
        self.assertEqual(r.settle(12, 1000, 20, [self._o("star", filled=0, half="second")])[0], 12.0)

    def test_quarter_sell_and_limit_sell(self):
        self.assertAlmostEqual(r.settle(7, 100, 20, [self._o("sell_quarter")])[0], 5.25)  # 카페 예시
        t, _ = r.settle(8, 100, 20, [self._o("sell_limit"), self._o("star", half="first")])
        self.assertAlmostEqual(t, 8 * 0.25 + 0.5)  # (T*0.25 + 0.5)

    def test_reverse_t_examples(self):
        t, _ = r.settle(39.5, 400, 40, [self._o("rev_moc", notional=300)])
        self.assertAlmostEqual(t, 37.525)
        t, pool = r.settle(t, 700, 40, [self._o("rev_buy", notional=100)])
        self.assertAlmostEqual(t, 38.14375)
        self.assertEqual(pool, 600)

    def test_pool_tracks_cash(self):
        _, pool = r.settle(5, 1000, 20, [self._o("star", notional=300, half="first"),
                                         self._o("sell_quarter", notional=120)])
        self.assertEqual(pool, 820)


class Misc(unittest.TestCase):
    def test_implied_t_rounds_up(self):
        self.assertEqual(r.implied_t(478, 10000, 20), 0.96)

    def test_first_buy_plan(self):
        p = r.plan_first(500, 45.93)
        self.assertEqual(p["orders"][0]["price"], 50.52)  # 현재가 +10%
        self.assertEqual(p["orders"][0]["qty"], 10)
        self.assertIsNone(r.plan_first(10, 45.93))  # 1주도 못 사면 None


if __name__ == "__main__":
    unittest.main()
