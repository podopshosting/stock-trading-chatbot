"""
Canonical replay datasets.

"AAPL, last 250 days" is not an experiment identifier: it means
something different every day, so two runs a week apart get reported
under the same name. A dataset here is addressed by its CONTENT, and
the manifest records what was asked for, what arrived, and what was
wrong with it.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from agent.replay.data import Bar                                 # noqa: E402
from agent.replay.datasets import (                               # noqa: E402
    ADJUSTMENT_PROVIDER_SPLIT, ADJUSTMENT_UNKNOWN, SESSION_ALL,
    SESSION_REGULAR, DatasetError, build_manifest, list_datasets, load,
    normalise_bars, save,
)


def row(ts, close=100.0, volume=1000.0):
    return {"timestamp": ts, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close, "volume": volume}


class TestNormalisation(unittest.TestCase):

    def test_bars_come_back_in_deterministic_order(self):
        bars, _ = normalise_bars(
            [row("2026-01-02T15:02:00Z"), row("2026-01-02T15:00:00Z"),
             row("2026-01-02T15:01:00Z")], "1min")
        self.assertEqual([b.timestamp for b in bars],
                         sorted(b.timestamp for b in bars))
        self.assertEqual(len(bars), 3)

    def test_a_duplicate_timestamp_is_dropped_and_counted(self):
        """Two bars at one instant would break a series the replay
        reads as strictly increasing."""
        bars, report = normalise_bars(
            [row("2026-01-02T15:00:00Z", 100.0),
             row("2026-01-02T15:00:00Z", 101.0)], "1min")
        self.assertEqual(len(bars), 1)
        self.assertEqual(report["duplicates_dropped"], 1)

    def test_timestamps_are_normalised_to_utc(self):
        bars, _ = normalise_bars([row("2026-01-02T10:00:00-05:00")], "1min")
        self.assertTrue(bars[0].timestamp.endswith("+00:00"))
        self.assertIn("15:00:00", bars[0].timestamp)

    def test_a_naive_timestamp_is_read_as_utc(self):
        bars, _ = normalise_bars([row("2026-01-02T15:00:00")], "1min")
        self.assertEqual(bars[0].timestamp, "2026-01-02T15:00:00+00:00")

    def test_an_unparseable_row_is_dropped_and_counted(self):
        bars, report = normalise_bars(
            [row("not-a-time"), {"timestamp": "2026-01-02T15:00:00Z"},
             row("2026-01-02T15:01:00Z")], "1min")
        self.assertEqual(len(bars), 1)
        self.assertEqual(report["unparseable_dropped"], 2)

    def test_a_hole_is_counted_and_never_filled(self):
        """A fabricated bar is indistinguishable from a real one once
        it is in the series."""
        bars, report = normalise_bars(
            [row("2026-01-02T15:00:00Z"), row("2026-01-02T15:05:00Z")],
            "1min")
        self.assertEqual(len(bars), 2, "the gap must not be filled")
        self.assertEqual(report["missing_intervals"], 4)
        self.assertTrue(report["missing_detail"])

    def test_weekends_are_not_missing_daily_bars(self):
        """Counting daily bars against a 24-hour interval reported 111
        holes in 250 trading days, which made every daily dataset
        incomplete and the flag useless."""
        _bars, report = normalise_bars(
            [row("2026-01-02T05:00:00Z"),      # Friday
             row("2026-01-05T05:00:00Z")],     # Monday
            "1day")
        self.assertEqual(report["missing_intervals"], 0)

    def test_a_missing_weekday_in_daily_bars_is_counted(self):
        """The control: a genuine weekday hole must still register."""
        _bars, report = normalise_bars(
            [row("2026-01-05T05:00:00Z"),      # Monday
             row("2026-01-08T05:00:00Z")],     # Thursday
            "1day")
        self.assertEqual(report["missing_intervals"], 2)

    def test_regular_session_filtering_drops_premarket(self):
        bars, report = normalise_bars(
            [row("2026-06-02T12:00:00Z"),      # 08:00 ET, pre-market
             row("2026-06-02T14:00:00Z"),      # 10:00 ET, regular
             row("2026-06-02T21:00:00Z")],     # 17:00 ET, after hours
            "1min", session_policy=SESSION_REGULAR)
        self.assertEqual(len(bars), 1)
        self.assertEqual(report["filtered_out_of_session"], 2)

    def test_daily_bars_are_never_filtered_out_of_session(self):
        bars, _ = normalise_bars([row("2026-06-02T04:00:00Z")], "1day",
                                 session_policy=SESSION_REGULAR)
        self.assertEqual(len(bars), 1)


class TestTheManifestIsContentAddressed(unittest.TestCase):

    def _bars(self, close=100.0):
        series, _ = normalise_bars(
            [row(f"2026-01-02T15:{i:02d}:00Z", close + i) for i in range(5)],
            "1min")
        return {"XYZ": series}

    def test_the_same_bars_give_the_same_id(self):
        a = build_manifest(self._bars(), provider="p", timeframe="1min")
        b = build_manifest(self._bars(), provider="p", timeframe="1min")
        self.assertEqual(a.dataset_id, b.dataset_id)
        self.assertEqual(a.checksum, b.checksum)

    def test_different_bars_give_a_different_id(self):
        """A revised window must not be mistaken for the same
        experiment."""
        a = build_manifest(self._bars(100.0), provider="p",
                           timeframe="1min")
        b = build_manifest(self._bars(101.0), provider="p",
                           timeframe="1min")
        self.assertNotEqual(a.dataset_id, b.dataset_id)

    def test_the_symbol_is_part_of_the_address(self):
        """Two symbols with identical bars are different datasets.

        Without the symbol in the digest they collide, so a replay of
        one would be reported under the other's id - and a mutation
        dropping the symbol from the checksum survived until this test
        existed.
        """
        series = self._bars()["XYZ"]
        a = build_manifest({"XYZ": series}, provider="p", timeframe="1min")
        b = build_manifest({"ABC": series}, provider="p", timeframe="1min")
        self.assertNotEqual(a.checksum, b.checksum)
        self.assertNotEqual(a.dataset_id, b.dataset_id)

    def test_the_symbol_set_is_part_of_the_address(self):
        """A two-symbol dataset is not the same as either alone."""
        series = self._bars()["XYZ"]
        one = build_manifest({"XYZ": series}, provider="p",
                             timeframe="1min")
        two = build_manifest({"XYZ": series, "ABC": series}, provider="p",
                             timeframe="1min")
        self.assertNotEqual(one.checksum, two.checksum)

    def test_the_id_does_not_depend_on_when_it_was_fetched(self):
        a = build_manifest(self._bars(), provider="p", timeframe="1min",
                           retrieved_at="2026-01-01T00:00:00+00:00")
        b = build_manifest(self._bars(), provider="p", timeframe="1min",
                           retrieved_at="2026-06-01T00:00:00+00:00")
        self.assertEqual(a.checksum, b.checksum)
        self.assertNotEqual(a.retrieved_at, b.retrieved_at)

    def test_an_empty_dataset_is_refused(self):
        """An empty dataset is indistinguishable from a failed fetch."""
        for empty in ({}, {"XYZ": []}):
            with self.subTest(value=empty):
                with self.assertRaises(DatasetError):
                    build_manifest(empty, provider="p", timeframe="1min")

    def test_the_manifest_records_what_was_wrong_with_the_data(self):
        series, report = normalise_bars(
            [row("2026-01-02T15:00:00Z"), row("2026-01-02T15:00:00Z"),
             row("2026-01-02T15:05:00Z")], "1min")
        manifest = build_manifest({"XYZ": series}, provider="p",
                                  timeframe="1min",
                                  reports={"XYZ": report})
        self.assertEqual(manifest.duplicates_dropped["XYZ"], 1)
        self.assertEqual(manifest.missing_intervals["XYZ"], 4)
        self.assertFalse(manifest.is_complete)

    def test_an_undeclared_adjustment_is_noted_not_assumed(self):
        manifest = build_manifest(self._bars(), provider="p",
                                  timeframe="1min",
                                  adjustment=ADJUSTMENT_UNKNOWN)
        self.assertTrue(any("UNKNOWN rather than assumed" in n
                            for n in manifest.notes))

    def test_a_declared_adjustment_is_not_noted(self):
        manifest = build_manifest(
            self._bars(), provider="p", timeframe="1min",
            adjustment=ADJUSTMENT_PROVIDER_SPLIT)
        self.assertFalse(any("UNKNOWN" in n for n in manifest.notes))

    def test_a_complete_series_reports_complete(self):
        series, report = normalise_bars(
            [row(f"2026-01-02T15:{i:02d}:00Z") for i in range(5)], "1min")
        manifest = build_manifest({"XYZ": series}, provider="p",
                                  timeframe="1min",
                                  reports={"XYZ": report})
        self.assertTrue(manifest.is_complete)


class TestPersistenceVerifiesTheAddress(unittest.TestCase):

    def _dataset(self):
        series, report = normalise_bars(
            [row(f"2026-01-02T15:{i:02d}:00Z", 100 + i) for i in range(5)],
            "1min")
        bars = {"XYZ": series}
        return build_manifest(bars, provider="p", timeframe="1min",
                              reports={"XYZ": report}), bars

    def test_a_round_trip_preserves_the_bars(self):
        manifest, bars = self._dataset()
        with tempfile.TemporaryDirectory() as root:
            save(manifest, bars, root=root)
            back, loaded = load(manifest.dataset_id, root=root)
            self.assertEqual(back.dataset_id, manifest.dataset_id)
            self.assertEqual([b.as_dict() for b in loaded["XYZ"]],
                             [b.as_dict() for b in bars["XYZ"]])

    def test_tampered_bars_fail_the_checksum(self):
        """An unverified content address is just a filename."""
        manifest, bars = self._dataset()
        with tempfile.TemporaryDirectory() as root:
            save(manifest, bars, root=root)
            path = pathlib.Path(root) / manifest.dataset_id / "bars.json"
            data = json.loads(path.read_text())
            data["XYZ"][0]["close"] = 999.0
            path.write_text(json.dumps(data))
            with self.assertRaises(DatasetError) as caught:
                load(manifest.dataset_id, root=root)
            self.assertIn("checksum", str(caught.exception))

    def test_resaving_the_same_dataset_is_a_no_op(self):
        manifest, bars = self._dataset()
        with tempfile.TemporaryDirectory() as root:
            save(manifest, bars, root=root)
            save(manifest, bars, root=root)
            self.assertEqual(len(list_datasets(root=root)), 1)

    def test_a_missing_dataset_raises_rather_than_returning_empty(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(DatasetError):
                load("ds_doesnotexist", root=root)

    def test_listing_an_absent_root_is_empty_not_an_error(self):
        self.assertEqual(
            list_datasets(root="/tmp/definitely-not-a-dataset-root-xyz"), [])
