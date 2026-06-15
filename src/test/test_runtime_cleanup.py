import gzip
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.tools import runtime_cleanup


NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def make_args(**overrides):
    values = {
        "apply": False,
        "now": None,
        "market_ranker_jsonl_days": 2,
        "market_ranker_gz_days": 30,
        "pool_scanner_jsonl_days": 7,
        "pool_scanner_gz_days": 30,
        "social_jsonl_days": 14,
        "social_gz_days": 45,
        "logs_days": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def write_file(path, content="payload\n", modified_at=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    timestamp = modified_at.timestamp()
    path.touch()
    import os

    os.utime(path, (timestamp, timestamp))
    return path


class RuntimeCleanupTests(unittest.TestCase):
    def test_discovers_old_market_ranker_snapshots_for_compression(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old_snapshot = write_file(
                root / "data" / "market_ranker" / "snapshots_2026-06-10.jsonl",
                modified_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
            )
            write_file(
                root / "data" / "market_ranker" / "snapshots_2026-06-15.jsonl",
                modified_at=datetime(2026, 6, 15, 11, 0, 0, tzinfo=timezone.utc),
            )

            policies = runtime_cleanup.build_policies(make_args(), project_root=root)
            actions = runtime_cleanup.discover_actions(policies, NOW, project_root=root)

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "compress")
            self.assertEqual(actions[0].path, old_snapshot)
            self.assertEqual(actions[0].target_path, old_snapshot.with_name(f"{old_snapshot.name}.gz"))

    def test_apply_compresses_and_removes_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            snapshot = write_file(
                root / "data" / "market_ranker" / "snapshots_2026-06-10.jsonl",
                content="line one\nline two\n",
                modified_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
            )

            policies = runtime_cleanup.build_policies(make_args(), project_root=root)
            actions = runtime_cleanup.discover_actions(policies, NOW, project_root=root)
            runtime_cleanup.apply_actions(actions)

            gzip_path = snapshot.with_name(f"{snapshot.name}.gz")
            self.assertFalse(snapshot.exists())
            self.assertTrue(gzip_path.exists())
            with gzip.open(gzip_path, "rt", encoding="utf-8") as file:
                self.assertEqual(file.read(), "line one\nline two\n")
            self.assertEqual(actions[0].status, "done")

    def test_deletes_expired_compressed_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            compressed = write_file(
                root / "data" / "market_ranker" / "snapshots_2026-05-01.jsonl.gz",
                modified_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
            )

            policies = runtime_cleanup.build_policies(make_args(), project_root=root)
            actions = runtime_cleanup.discover_actions(policies, NOW, project_root=root)

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].action, "delete")
            runtime_cleanup.apply_actions(actions)
            self.assertFalse(compressed.exists())

    def test_protects_watchlist_even_if_pattern_would_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            policy = runtime_cleanup.CleanupPolicy(
                name="dangerous",
                directory=root / "data",
                pattern="*.json",
                action="delete",
                older_than_days=0,
            )
            write_file(root / "data" / "watchlist.json")

            actions = runtime_cleanup.discover_actions([policy], NOW, project_root=root)

            self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
