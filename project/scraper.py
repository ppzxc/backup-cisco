import argparse
import datetime as dt
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from netmiko import ConnectHandler, redispatch

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/ansible/backups"))
INVENTORY_PATH = Path(os.environ.get("INVENTORY_PATH", "devices.yml"))
RETRY_DELAYS = (2, 5)
RETENTION_DAYS = 90
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
HOSTNAME_LINE = re.compile(r"^hostname\s+(\S+)", re.MULTILINE | re.IGNORECASE)
CLI_ERROR_MARKERS = ("% invalid input", "% incomplete command", "% ambiguous command")
REQUIRED_DEVICE_FIELDS = ("name", "host", "username", "password", "port", "device_type")


class InventoryError(ValueError):
    pass


class HostnameMismatchError(RuntimeError):
    pass


def load_inventory(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        inventory = yaml.safe_load(file) or {}
    if not isinstance(inventory, dict):
        raise InventoryError("인벤토리 최상위 값은 맵이어야 합니다.")
    return inventory


def validate_device(device: object, section: str, names: set[str], internal: bool = False) -> dict:
    if not isinstance(device, dict):
        raise InventoryError(f"{section}의 각 대상은 맵이어야 합니다.")
    missing = [field for field in REQUIRED_DEVICE_FIELDS if not device.get(field)]
    if missing:
        raise InventoryError(f"{section} 대상에 필수 값이 없습니다: {', '.join(missing)}")
    name = device["name"]
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise InventoryError(f"{section} 대상 이름은 파일명으로 안전해야 합니다.")
    if name in names:
        raise InventoryError("대상 이름은 인벤토리 전체에서 고유해야 합니다.")
    names.add(name)
    if not isinstance(device["port"], int) or not 1 <= device["port"] <= 65535:
        raise InventoryError(f"{section} 대상 포트는 1~65535 정수여야 합니다.")
    if any(not isinstance(device[field], str) for field in ("host", "username", "password", "device_type")):
        raise InventoryError(f"{section} 대상 접속 정보는 문자열이어야 합니다.")
    if internal and device.get("transport") not in {"ssh", "telnet"}:
        raise InventoryError(f"{section} 대상의 transport는 ssh 또는 telnet이어야 합니다.")
    return device


def validate_inventory(inventory: dict) -> tuple[list[dict], dict | None, list[dict]]:
    direct, internal, bastion = inventory.get("direct_switches", []), inventory.get("internal_switches", []), inventory.get("bastion_switch")
    if not isinstance(direct, list) or not isinstance(internal, list):
        raise InventoryError("direct_switches와 internal_switches는 목록이어야 합니다.")
    if bastion is not None and not isinstance(bastion, dict):
        raise InventoryError("bastion_switch는 맵이어야 합니다.")
    if internal and bastion is None:
        raise InventoryError("internal_switches에는 bastion_switch가 필요합니다.")
    names: set[str] = set()
    return (
        [validate_device(item, "direct_switches", names) for item in direct],
        validate_device(bastion, "bastion_switch", names) if bastion else None,
        [validate_device(item, "internal_switches", names, True) for item in internal],
    )


def retry(operation, label: str):
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return operation()
        except Exception:
            if attempt == len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            print(f"↻ 재시도: {label} — 연결/전송 실패, {delay}초 후 다시 시도합니다 ({attempt + 2}/3).")
            time.sleep(delay)


def connection_settings(device: dict, read_timeout: int = 60) -> dict:
    return {"device_type": device["device_type"], "host": device["host"], "username": device["username"], "password": device["password"], "port": device["port"], "secret": device["password"], "conn_timeout": 30, "read_timeout_override": read_timeout, "global_delay_factor": 2}


def open_connection(device: dict, read_timeout: int = 60):
    return retry(lambda: ConnectHandler(**connection_settings(device, read_timeout=read_timeout)), device["name"])


def is_valid_config(config: str) -> bool:
    return bool(config.strip()) and not any(marker in config.lower() for marker in CLI_ERROR_MARKERS)


def extract_hostname(config: str) -> str | None:
    match = HOSTNAME_LINE.search(config)
    return match.group(1) if match else None


def prompt_hostname(prompt: str) -> str:
    return prompt.strip().rstrip("#>").strip()


def verify_hostname(actual: str | None, expected: str) -> None:
    if actual is None or actual.casefold() != expected.casefold():
        raise HostnameMismatchError(f"hostname mismatch: expected {expected!r}, got {actual!r}")


def save_config(name: str, config: str, run_at: dt.datetime, suffix: str = "") -> Path:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    tag = f"_{suffix}" if suffix else ""
    destination = BACKUP_DIR / f"{name}{tag}_{run_at.strftime('%Y%m%dT%H%M%S')}.txt"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=BACKUP_DIR, delete=False) as file:
        file.write(config)
        temporary = Path(file.name)
    temporary.chmod(0o600)
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination


def collect_config(connection, command: str = "show running-config", read_timeout: int = 60) -> str:
    if not connection.check_enable_mode():
        connection.enable()
    config = connection.send_command(command, read_timeout=read_timeout)
    if not is_valid_config(config):
        raise RuntimeError("invalid configuration output")
    return config


def disconnect(connection) -> None:
    if connection is not None:
        connection.disconnect()


def backup_direct(device: dict, run_at: dt.datetime, command: str = "show running-config", read_timeout: int = 60, suffix: str = "") -> tuple[bool, str]:
    connection = None
    try:
        print(f"▶ 백업 시도: {device['name']} (직접 연결)")
        connection = open_connection(device, read_timeout=read_timeout)
        verify_hostname(prompt_hostname(connection.find_prompt()), device["name"])
    except HostnameMismatchError:
        print(f"❌ 백업 실패: {device['name']} — 직접 대상 장비 이름 불일치")
        return False, "직접 대상 장비 이름 불일치"
    except Exception:
        print(f"❌ 백업 실패: {device['name']} — 직접 연결/인증 실패")
        return False, "직접 연결/인증 실패"
    try:
        config = collect_config(connection, command=command, read_timeout=read_timeout)
        verify_hostname(extract_hostname(config), device["name"])
        path = save_config(device["name"], config, run_at, suffix=suffix)
        print(f"✅ 백업 성공: {device['name']} → {path.name}")
        return True, ""
    except HostnameMismatchError:
        print(f"❌ 백업 실패: {device['name']} — 직접 대상 장비 이름 불일치")
        return False, "직접 대상 장비 이름 불일치"
    except Exception:
        print(f"❌ 백업 실패: {device['name']} — 직접 설정 수집 실패")
        return False, "직접 설정 수집 실패"
    finally:
        disconnect(connection)


def jump_to_internal(connection, device: dict) -> None:
    before_prompt = connection.find_prompt()
    command = f"ssh -p {device['port']} -l {device['username']} {device['host']}" if device["transport"] == "ssh" else f"telnet {device['host']} {device['port']}"
    connection.write_channel(f"{command}\n")
    time.sleep(2)
    output = connection.read_channel().lower()
    if any(marker in output for marker in ("username", "login", "verification")):
        connection.write_channel(f"{device['username']}\n")
        time.sleep(1)
        output = connection.read_channel().lower()
    if "password" not in output:
        raise RuntimeError("jump authentication prompt not received")
    connection.write_channel(f"{device['password']}\n")
    time.sleep(2)
    redispatch(connection, device_type=device["device_type"])
    connection.secret = device["password"]
    after_prompt = connection.find_prompt()
    if after_prompt == before_prompt:
        raise RuntimeError("jump to internal switch did not change the active session")
    verify_hostname(prompt_hostname(after_prompt), device["name"])


def backup_internal(bastion: dict, device: dict, run_at: dt.datetime, command: str = "show running-config", read_timeout: int = 60, suffix: str = "") -> tuple[bool, str]:
    connection = None
    try:
        print(f"▶ 백업 시도: {device['name']} (Bastion 경유 연결)")
        connection = open_connection(bastion, read_timeout=read_timeout)
    except Exception:
        print(f"❌ 백업 실패: {device['name']} — Bastion 연결/인증 실패")
        return False, "Bastion 연결/인증 실패"
    try:
        try:
            jump_to_internal(connection, device)
        except HostnameMismatchError:
            print(f"❌ 백업 실패: {device['name']} — 내부망 대상 장비 이름 불일치")
            return False, "내부망 대상 장비 이름 불일치"
        except Exception:
            print(f"❌ 백업 실패: {device['name']} — 내부망 점프 연결/인증 실패")
            return False, "내부망 점프 연결/인증 실패"
        try:
            config = collect_config(connection, command=command, read_timeout=read_timeout)
            verify_hostname(extract_hostname(config), device["name"])
            path = save_config(device["name"], config, run_at, suffix=suffix)
            print(f"✅ 백업 성공: {device['name']} → {path.name}")
            return True, ""
        except HostnameMismatchError:
            print(f"❌ 백업 실패: {device['name']} — 내부망 대상 장비 이름 불일치")
            return False, "내부망 대상 장비 이름 불일치"
        except Exception:
            print(f"❌ 백업 실패: {device['name']} — 내부망 설정 수집 실패")
            return False, "내부망 설정 수집 실패"
    finally:
        disconnect(connection)


def cleanup_artifacts(now: dt.datetime) -> None:
    try:
        cutoff = now.timestamp() - RETENTION_DAYS * 86400
        for path in BACKUP_DIR.glob("*.txt"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
    except OSError:
        print("[보존 정책] 만료 산출물 정리에 실패했습니다.")


def report_text(run_at: dt.datetime, successes: list[str], failures: list[tuple[str, str]], title: str = "[시스코 스위치 백업 결과 리포트]") -> str:
    lines = [title, f"- 일자: {run_at.strftime('%Y%m%d')}", f"- 총 대상: {len(successes) + len(failures)}대 (성공: {len(successes)}대, 실패: {len(failures)}대)", "", f"[성공 목록 ({len(successes)}대)]", ", ".join(successes) if successes else "없음", "", f"[실패 목록 ({len(failures)}대)]"]
    return "\n".join(lines + ([f"- {name}: {reason}" for name, reason in failures] if failures else ["없음"]))


def send_notification(inventory: dict, payload: str) -> bool:
    url = os.environ.get("WEBHOOK_URL") or inventory.get("webhook_url")
    token = os.environ.get("WEBHOOK_TOKEN") or inventory.get("webhook_token")
    if not url:
        print("[결과 알림] 비활성화됨.")
        return True
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if token:
        headers["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
    def post() -> None:
        request = urllib.request.Request(url, data=payload.encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=10):
            pass
    try:
        retry(post, "결과 알림")
        print("[결과 알림] 전송 성공.")
        return True
    except Exception:
        print("[결과 알림] 전송 실패.")
        return False


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="시스코 스위치 설정 및 진단 정보 수집 스크립트")
    parser.add_argument("--tech-support", action="store_true", help="show tech-support 진단 정보 수집 (미지정 시 show running-config 백업)")
    parser.add_argument("--target", type=str, help="특정 장비 1대만 지정하여 수집 (예: ns0278)")
    parser.add_argument("--timeout", type=int, help="명령어 응답 대기 시간(초) 직접 지정 (기본값: tech-support는 300초, 백업은 60초)")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        inventory = load_inventory(INVENTORY_PATH)
        direct, bastion, internal = validate_inventory(inventory)
    except (OSError, yaml.YAMLError, InventoryError):
        print("[인벤토리] 구성 오류로 백업을 시작하지 않습니다.")
        return 1

    command = "show tech-support" if args.tech_support else "show running-config"
    default_timeout = 300 if args.tech_support else 60
    read_timeout = args.timeout if args.timeout and args.timeout > 0 else default_timeout
    suffix = "tech" if args.tech_support else ""
    title = "[시스코 스위치 Tech-Support 수집 결과 리포트]" if args.tech_support else "[시스코 스위치 백업 결과 리포트]"

    if args.target:
        all_targets = [d["name"] for d in direct] + ([bastion["name"]] if bastion else []) + [d["name"] for d in internal]
        if args.target not in all_targets:
            print(f"[인벤토리] 대상 장비 '{args.target}'를 인벤토리에서 찾을 수 없습니다.")
            return 1
        direct = [d for d in direct if d["name"] == args.target]
        bastion_target = bool(bastion and bastion["name"] == args.target)
        internal = [d for d in internal if d["name"] == args.target]
    else:
        bastion_target = bool(bastion)

    run_at, successes, failures = dt.datetime.now().astimezone(), [], []
    for device in direct:
        ok, reason = backup_direct(device, run_at, command=command, read_timeout=read_timeout, suffix=suffix)
        successes.append(device["name"]) if ok else failures.append((device["name"], reason))

    if bastion:
        if bastion_target:
            ok, reason = backup_direct(bastion, run_at, command=command, read_timeout=read_timeout, suffix=suffix)
            successes.append(bastion["name"]) if ok else failures.append((bastion["name"], reason))
        for device in internal:
            ok, reason = backup_internal(bastion, device, run_at, command=command, read_timeout=read_timeout, suffix=suffix)
            successes.append(device["name"]) if ok else failures.append((device["name"], reason))

    cleanup_artifacts(run_at)
    report = report_text(run_at, successes, failures, title=title)
    print(f"\n{report}")
    notify_ok = send_notification(inventory, report)
    return 1 if failures or not notify_ok else 0


if __name__ == "__main__":
    sys.exit(run())
