import datetime as dt
import os
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper


DEVICE = {"name": "switch-01", "host": "192.0.2.1", "username": "admin", "password": "secret", "port": 22, "device_type": "cisco_ios"}


class ScraperTests(unittest.TestCase):
    def test_missing_direct_switches_is_an_empty_list(self):
        direct, bastion, internal = scraper.validate_inventory({})
        self.assertEqual((direct, bastion, internal), ([], None, []))

    def test_internal_switch_requires_bastion(self):
        with self.assertRaises(scraper.InventoryError):
            scraper.validate_inventory({"internal_switches": [DEVICE | {"transport": "ssh"}]})

    def test_duplicate_or_unsafe_name_is_rejected(self):
        with self.assertRaises(scraper.InventoryError):
            scraper.validate_inventory({"direct_switches": [DEVICE, DEVICE | {"host": "192.0.2.2"}]})
        with self.assertRaises(scraper.InventoryError):
            scraper.validate_inventory({"direct_switches": [DEVICE | {"name": "../escape"}]})

    def test_internal_transport_is_explicit(self):
        with self.assertRaises(scraper.InventoryError):
            scraper.validate_inventory({"bastion_switch": DEVICE | {"name": "bastion"}, "internal_switches": [DEVICE | {"name": "internal"}]})

    def test_invalid_configuration_output_is_not_accepted(self):
        self.assertFalse(scraper.is_valid_config(""))
        self.assertFalse(scraper.is_valid_config("% Invalid input detected"))
        self.assertTrue(scraper.is_valid_config("version 17.1\nhostname switch-01"))

    def test_artifact_is_unique_per_run_and_private(self):
        old_dir = scraper.BACKUP_DIR
        with tempfile.TemporaryDirectory() as directory:
            scraper.BACKUP_DIR = Path(directory)
            path = scraper.save_config("switch-01", "hostname switch-01\n", dt.datetime(2026, 8, 21, 3, 0, 0))
            self.assertEqual(path.name, "switch-01_20260821T030000.txt")
            self.assertEqual(path.read_text(encoding="utf-8"), "hostname switch-01\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        scraper.BACKUP_DIR = old_dir

    def test_cleanup_keeps_recent_artifacts(self):
        old_dir = scraper.BACKUP_DIR
        with tempfile.TemporaryDirectory() as directory:
            scraper.BACKUP_DIR = Path(directory)
            old = scraper.BACKUP_DIR / "old.txt"
            recent = scraper.BACKUP_DIR / "recent.txt"
            old.write_text("old")
            recent.write_text("recent")
            old_timestamp = (dt.datetime.now() - dt.timedelta(days=91)).timestamp()
            os.utime(old, (old_timestamp, old_timestamp))
            scraper.cleanup_artifacts(dt.datetime.now())
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
        scraper.BACKUP_DIR = old_dir

    def test_report_masks_exception_details(self):
        report = scraper.report_text(dt.datetime(2026, 8, 21), [], [("switch-01", "직접 연결 또는 설정 수집 실패")])
        self.assertIn("switch-01", report)
        self.assertNotIn("secret", report)

    def test_retry_uses_all_three_attempts(self):
        attempts = []
        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("temporary")
            return "ok"
        with patch.object(scraper.time, "sleep"):
            self.assertEqual(scraper.retry(operation, "test"), "ok")
        self.assertEqual(len(attempts), 3)

    def test_direct_backup_reports_connection_stage_without_exception_details(self):
        output = StringIO()
        with patch.object(scraper, "open_connection", side_effect=OSError("secret connection detail")), patch("sys.stdout", output):
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "직접 연결/인증 실패")
        self.assertIn("직접 연결/인증 실패", output.getvalue())
        self.assertNotIn("secret connection detail", output.getvalue())

    def test_direct_backup_reports_collection_stage_separately(self):
        output = StringIO()
        connection = unittest.mock.MagicMock()
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "collect_config", side_effect=RuntimeError("secret command detail")), patch("sys.stdout", output):
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "직접 설정 수집 실패")
        self.assertIn("직접 설정 수집 실패", output.getvalue())
        self.assertNotIn("secret command detail", output.getvalue())


if __name__ == "__main__":
    unittest.main()
