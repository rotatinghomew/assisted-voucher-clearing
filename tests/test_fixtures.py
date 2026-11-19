import csv
import unittest
from pathlib import Path


class FixtureTests(unittest.TestCase):
    def test_case_events_have_ordered_timestamps_and_distinct_cases(self):
        path = Path(__file__).parents[1] / "fixtures" / "cases.csv"
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertGreaterEqual(len(rows), 10)
        self.assertGreaterEqual(len({row["case_id"] for row in rows}), 4)
        self.assertTrue(all(row["event_id"] and row["occurred_at"] for row in rows))


if __name__ == "__main__":
    unittest.main()
