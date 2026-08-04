import unittest

from src.tools.stop_loss_filter_study import evaluate_filters


class StopLossFilterStudyTests(unittest.TestCase):
    def test_quote_liquidity_filter_counts_catastrophes_and_missing_as_removed(self):
        rows = [
            {"pnl_pct": -100, "quote_liquidity_usd": 0.5},
            {"pnl_pct": -95, "quote_liquidity_usd": None},
            {"pnl_pct": 20, "quote_liquidity_usd": 10},
        ]
        result = next(row for row in evaluate_filters(rows) if row["filter"] == "QLiq >= $1")
        self.assertEqual(result["known"], 2)
        self.assertEqual(result["kept"], 1)
        self.assertEqual(result["cat_removed"], 2)
        self.assertEqual(result["noncat_removed"], 0)


if __name__ == "__main__":
    unittest.main()
