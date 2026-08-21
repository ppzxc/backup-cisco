"""Collect Cisco running configurations from direct and bastion-routed targets."""

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
CLI_ERROR_MARKERS = ("% invalid input", "% incomplete command", "% ambiguous command")
REQUIRED_DEVICE_FIELDS = ("name", "host", "username", "password", "port", "device_type")


class InventoryError(ValueError):
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


def connection_settings(device: dict) -> dict:
    return {"device_type": device["device_type"], "host": device["host"], "username": device["username"], "password": device["password"], "port": device["port"], "secret": device["password"], "conn_timeout": 30, "read_timeout_override": 60, "global_delay_factor": 2}


def open_connection(device: dict):
    return retry(lambda: ConnectHandler(**connection_settings(device)), device["name"])


def is_valid_config(config: str) -> bool:
    return bool(config.strip()) and not any(marker in config.lower() for marker in CLI_ERROR_MARKERS)


def save_config(name: str, config: str, run_at: dt.datetime) -> Path:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUP_DIR.chmod(0o700)
    destination = BACKUP_DIR / f"{name}_{run_at.strftime('%Y%m%dT%H%M%S')}.txt"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=BACKUP_DIR, delete=False) as file:
        file.write(config)
        temporary = Path(file.name)
    temporary.chmod(0o600)
    temporary.replace(destination)
    destination.chmod(0o600)
    return destination


def collect_config(connection) -> str:
    if not connection.check_enable_mode():
        connection.enable()
    config = connection.send_command("show running-config", read_timeout=60)
    if not is_valid_config(config):
        raise RuntimeError("invalid configuration output")
    return config


def disconnect(connection) -> None:
    if connection is not None:
        connection.disconnect()


def backup_direct(device: dict, run_at: dt.datetime) -> tuple[bool, str]:
    connection = None
    try:
        print(f"▶ 백업 시도: {device['name']} (직접 연결)")
        connection = open_connection(device)
        path = save_config(device["name"], collect_config(connection), run_at)
        print(f"✅ 백업 성공: {device['name']} → {path.name}")
        return True, ""
    except Exception:
        print(f"❌ 백업 실패: {device['name']} — 직접 연결 또는 설정 수집 실패")
        return False, "직접 연결 또는 설정 수집 실패"
    finally:
        disconnect(connection)


def jump_to_internal(connection, device: dict) -> None:
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


def backup_internal(bastion: dict, device: dict, run_at: dt.datetime) -> tuple[bool, str]:
    connection = None
    try:
        print(f"▶ 백업 시도: {device['name']} (Bastion 경유 연결)")
        connection = open_connection(bastion)
        jump_to_internal(connection, device)
        path = save_config(device["name"], collect_config(connection), run_at)
        print(f"✅ 백업 성공: {device['name']} → {path.name}")
        return True, ""
    except Exception:
        print(f"❌ 백업 실패: {device['name']} — Bastion 경유 연결 또는 설정 수집 실패")
        return False, "Bastion 경유 연결 또는 설정 수집 실패"
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


def report_text(run_at: dt.datetime, successes: list[str], failures: list[tuple[str, str]]) -> str:
    lines = ["[시스코 스위치 백업 결과 리포트]", f"- 일자: {run_at.strftime('%Y%m%d')}", f"- 총 대상: {len(successes) + len(failures)}대 (성공: {len(successes)}대, 실패: {len(failures)}대)", "", f"[성공 목록 ({len(successes)}대)]", ", ".join(successes) if successes else "없음", "", f"[실패 목록 ({len(failures)}대)]"]
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


def run() -> int:
    try:
        inventory = load_inventory(INVENTORY_PATH)
        direct, bastion, internal = validate_inventory(inventory)
    except (OSError, yaml.YAMLError, InventoryError):
        print("[인벤토리] 구성 오류로 백업을 시작하지 않습니다.")
        return 1
    run_at, successes, failures = dt.datetime.now().astimezone(), [], []
    for device in direct:
        ok, reason = backup_direct(device, run_at)
        successes.append(device["name"]) if ok else failures.append((device["name"], reason))
    if bastion:
        ok, reason = backup_direct(bastion, run_at)
        successes.append(bastion["name"]) if ok else failures.append((bastion["name"], reason))
        for device in internal:
            ok, reason = backup_internal(bastion, device, run_at)
            successes.append(device["name"]) if ok else failures.append((device["name"], reason))
    cleanup_artifacts(run_at)
    report = report_text(run_at, successes, failures)
    print(f"\n{report}")
    return 1 if failures or not send_notification(inventory, report) else 0


if __name__ == "__main__":
    sys.exit(run())
