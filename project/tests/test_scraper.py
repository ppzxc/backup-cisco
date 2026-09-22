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
        connection.find_prompt.return_value = "switch-01#"
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "collect_config", side_effect=RuntimeError("secret command detail")), patch("sys.stdout", output):
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "직접 설정 수집 실패")
        self.assertIn("직접 설정 수집 실패", output.getvalue())
        self.assertNotIn("secret command detail", output.getvalue())

    def test_direct_backup_succeeds_when_hostname_matches(self):
        # 프롬프트와 수집한 설정 양쪽 모두 인벤토리 이름과 일치하는 정상 경로.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.return_value = "switch-01#"
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "collect_config", return_value="hostname switch-01\n"), patch.object(scraper, "save_config", return_value=Path("switch-01_20260821T030000.txt")) as save_config:
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        save_config.assert_called_once()

    def test_direct_backup_reports_prompt_hostname_mismatch(self):
        # 연결 직후 프롬프트의 장비 이름이 인벤토리 대상과 다르면, 설정을
        # 수집하기 전에 "직접 대상 장비 이름 불일치"로 실패해야 한다.
        output = StringIO()
        connection = unittest.mock.MagicMock()
        connection.find_prompt.return_value = "other-switch#"
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "collect_config") as collect_config, patch("sys.stdout", output):
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "직접 대상 장비 이름 불일치")
        self.assertIn("직접 대상 장비 이름 불일치", output.getvalue())
        collect_config.assert_not_called()

    def test_direct_backup_reports_config_hostname_mismatch(self):
        # 프롬프트는 일치하지만, 수집한 설정 안의 hostname 줄이 다른 장비를
        # 가리키면 저장 없이 "직접 대상 장비 이름 불일치"로 실패해야 한다.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.return_value = "switch-01#"
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "collect_config", return_value="hostname other-switch\n"), patch.object(scraper, "save_config") as save_config:
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "직접 대상 장비 이름 불일치")
        save_config.assert_not_called()

    def test_direct_backup_reports_config_hostname_missing_is_fail_closed(self):
        # 설정 자체는 유효하지만(비어 있지 않고 CLI 오류 표식도 없음) hostname
        # 줄이 아예 없으면, "유효한 설정 수집"과 무관하게 이름 불일치로
        # fail-closed 되어야 한다.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.return_value = "switch-01#"
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "collect_config", return_value="version 17.1\n"), patch.object(scraper, "save_config") as save_config:
            ok, reason = scraper.backup_direct(DEVICE, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "직접 대상 장비 이름 불일치")
        save_config.assert_not_called()

    def test_save_config_with_suffix(self):
        old_dir = scraper.BACKUP_DIR
        with tempfile.TemporaryDirectory() as directory:
            scraper.BACKUP_DIR = Path(directory)
            path = scraper.save_config("switch-01", "tech support output\n", dt.datetime(2026, 8, 21, 3, 0, 0), suffix="tech")
            self.assertEqual(path.name, "switch-01_tech_20260821T030000.txt")
            self.assertEqual(path.read_text(encoding="utf-8"), "tech support output\n")
        scraper.BACKUP_DIR = old_dir

    def test_parse_arguments_default_and_custom(self):
        args_default = scraper.parse_arguments([])
        self.assertFalse(args_default.tech_support)
        self.assertIsNone(args_default.target)

        args_tech = scraper.parse_arguments(["--tech-support", "--target", "switch-01"])
        self.assertTrue(args_tech.tech_support)
        self.assertEqual(args_tech.target, "switch-01")

    def test_report_text_custom_title(self):
        report = scraper.report_text(dt.datetime(2026, 8, 21), ["switch-01"], [], title="[시스코 스위치 Tech-Support 수집 결과 리포트]")
        self.assertIn("[시스코 스위치 Tech-Support 수집 결과 리포트]", report)
        self.assertIn("switch-01", report)

    def test_jump_to_internal_raises_when_prompt_unchanged_after_redispatch(self):
        # 점프 명령/비밀번호 전송 후 실제로 내부망 프롬프트로 넘어가지 못하고
        # bastion 세션으로 조용히 되돌아간 경우를 흉내낸다.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["bastion01#", "bastion01#"]
        connection.read_channel.return_value = "Password: "
        device = DEVICE | {"name": "internal-01", "transport": "ssh"}
        with patch.object(scraper, "redispatch"):
            with self.assertRaises(RuntimeError):
                scraper.jump_to_internal(connection, device)

    def test_jump_to_internal_succeeds_when_prompt_changes_after_redispatch(self):
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["bastion01#", "internal-01#"]
        connection.read_channel.return_value = "Password: "
        device = DEVICE | {"name": "internal-01", "transport": "ssh"}
        with patch.object(scraper, "redispatch"):
            scraper.jump_to_internal(connection, device)

    def test_backup_internal_reports_jump_stage_when_prompt_unchanged(self):
        # ns0281/ns0282 실제 신고 증상 재현: bastion(ns0278)을 거쳐 내부망으로
        # 점프하려 했으나 조용히 실패하면, bastion 자신의 설정이 내부망 장비
        # 이름으로 저장되지 않고 "내부망 점프 연결/인증 실패"로 보고되어야 한다.
        output = StringIO()
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["ns0278#", "ns0278#"]
        connection.read_channel.return_value = "Password: "
        bastion = DEVICE | {"name": "ns0278"}
        internal = DEVICE | {"name": "ns0281", "transport": "ssh"}
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "redispatch"), patch.object(scraper, "save_config") as save_config, patch("sys.stdout", output):
            ok, reason = scraper.backup_internal(bastion, internal, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "내부망 점프 연결/인증 실패")
        self.assertIn("내부망 점프 연결/인증 실패", output.getvalue())
        # 신고된 실제 증상을 직접 잠근다: ns0278의 설정이 ns0281 이름으로
        # 저장되면 안 된다. 실패 문자열만 확인하면 save_config가 우연히
        # 실행되어도 통과할 수 있으므로, 저장 자체가 호출되지 않았음을 검증한다.
        save_config.assert_not_called()
        # 점프가 실패해도 bastion에 연 vty 세션은 반드시 정리되어야 한다.
        # (연결 누수 시 매 실행마다 ns0278에 vty 세션이 쌓여 결국 세션 한도를
        # 소진, bastion 자신의 백업까지 실패시킬 수 있다.)
        connection.disconnect.assert_called_once()

    def test_backup_internal_reports_jump_stage_when_find_prompt_raises(self):
        # netmiko의 find_prompt()는 채널이 인식 가능한 프롬프트를 만들어내지
        # 못하면 문자열 대신 ValueError를 던질 수 있다. 이 경로도 다른 대상의
        # 수집을 막지 않고 "내부망 점프 연결/인증 실패"로 보고되어야 한다.
        output = StringIO()
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ValueError("Unable to find prompt")
        connection.read_channel.return_value = "Password: "
        bastion = DEVICE | {"name": "ns0278"}
        internal = DEVICE | {"name": "ns0281", "transport": "ssh"}
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "redispatch"), patch("sys.stdout", output):
            ok, reason = scraper.backup_internal(bastion, internal, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "내부망 점프 연결/인증 실패")
        self.assertIn("내부망 점프 연결/인증 실패", output.getvalue())
        connection.disconnect.assert_called_once()

    def test_jump_to_internal_raises_hostname_mismatch_when_prompt_does_not_match_target(self):
        # 세션이 실제로 다른 프롬프트로 넘어가긴 했으니(before/after 불일치
        # 검사는 통과) "점프 자체는 성공"했지만, 그 프롬프트가 의도한 내부망
        # 대상 이름이 아닌 경우를 흉내낸다.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["bastion01#", "some-other-switch#"]
        connection.read_channel.return_value = "Password: "
        device = DEVICE | {"name": "internal-01", "transport": "ssh"}
        with patch.object(scraper, "redispatch"):
            with self.assertRaises(scraper.HostnameMismatchError):
                scraper.jump_to_internal(connection, device)

    def test_backup_internal_reports_jump_stage_hostname_mismatch(self):
        output = StringIO()
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["ns0278#", "wrong-name#"]
        connection.read_channel.return_value = "Password: "
        bastion = DEVICE | {"name": "ns0278"}
        internal = DEVICE | {"name": "ns0281", "transport": "ssh"}
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "redispatch"), patch.object(scraper, "save_config") as save_config, patch("sys.stdout", output):
            ok, reason = scraper.backup_internal(bastion, internal, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "내부망 대상 장비 이름 불일치")
        self.assertIn("내부망 대상 장비 이름 불일치", output.getvalue())
        save_config.assert_not_called()
        connection.disconnect.assert_called_once()

    def test_backup_internal_reports_config_hostname_mismatch(self):
        # 점프 자체는 올바른 대상으로 성공했지만, 수집한 설정 안의 hostname
        # 줄이 다른 장비를 가리키는 경우.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["ns0278#", "ns0281#"]
        connection.read_channel.return_value = "Password: "
        bastion = DEVICE | {"name": "ns0278"}
        internal = DEVICE | {"name": "ns0281", "transport": "ssh"}
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "redispatch"), patch.object(scraper, "collect_config", return_value="hostname other-switch\n"), patch.object(scraper, "save_config") as save_config:
            ok, reason = scraper.backup_internal(bastion, internal, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "내부망 대상 장비 이름 불일치")
        save_config.assert_not_called()

    def test_backup_internal_reports_config_hostname_missing_is_fail_closed(self):
        # 점프는 올바른 대상으로 성공했지만, 수집한 설정에 hostname 줄이 아예
        # 없으면 "유효한 설정 수집"과 무관하게 이름 불일치로 fail-closed
        # 되어야 한다.
        connection = unittest.mock.MagicMock()
        connection.find_prompt.side_effect = ["ns0278#", "ns0281#"]
        connection.read_channel.return_value = "Password: "
        bastion = DEVICE | {"name": "ns0278"}
        internal = DEVICE | {"name": "ns0281", "transport": "ssh"}
        with patch.object(scraper, "open_connection", return_value=connection), patch.object(scraper, "redispatch"), patch.object(scraper, "collect_config", return_value="version 17.1\n"), patch.object(scraper, "save_config") as save_config:
            ok, reason = scraper.backup_internal(bastion, internal, dt.datetime(2026, 8, 21))
        self.assertFalse(ok)
        self.assertEqual(reason, "내부망 대상 장비 이름 불일치")
        save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
