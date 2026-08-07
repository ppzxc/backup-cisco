import os
import time
import datetime
import yaml
import urllib.request
import urllib.error
from netmiko import ConnectHandler, redispatch

today = datetime.datetime.now().strftime("%Y%m%d")

# YAML 설정 파일 로드
with open("devices.yml", "r", encoding="utf-8") as f:
    config_data = yaml.safe_load(f)

direct_switches = config_data.get("direct_switches", [])
bastion_info = config_data.get("bastion_switch")
internal_switches = config_data.get("internal_switches", [])

# 웹훅 설정 (환경변수 > devices.yml 설정)
webhook_url = os.environ.get("WEBHOOK_URL") or config_data.get("webhook_url")
webhook_token = os.environ.get("WEBHOOK_TOKEN") or config_data.get("webhook_token")
if webhook_token and not webhook_token.startswith("Bearer "):
    webhook_token = f"Bearer {webhook_token}"

success_list = []
failed_list = []

def backup_device(device_info, custom_name):
    device = {
        'device_type': device_info['device_type'],
        'host': device_info['host'],
        'username': device_info['username'],
        'password': device_info['password'],
        'port': device_info['port'],
        'secret': device_info['password'],  # 🔥 로그인 암호를 이네이블 암호로 사용
        'global_delay_factor': 2,
    }
    print(f"[{custom_name}] 직접 접속 시도 ({device_info['host']}:{device_info['port']})...")
    try:
        with ConnectHandler(**device) as net_connect:
            net_connect.find_prompt()
            if not net_connect.check_enable_mode():
                net_connect.enable() 
            
            config = net_connect.send_command("show running-config", read_timeout=60)
            file_path = f"/ansible/backups/{custom_name}_{today}.txt"
            with open(file_path, "w", encoding="utf-8") as file:
                file.write(config)
            print(f" 백업 성공: {file_path}")
            success_list.append(custom_name)
            return True
    except Exception as e:
        print(f"❌ 백업 실패 [{custom_name}]: {e}")
        failed_list.append({"name": custom_name, "error": str(e)})
        return False

# =========================================================
# 1단계: 다이렉트 접근 스위치 백업
# =========================================================
print("=========================================")
print("▶ 1단계: 다이렉트 대상 스위치 백업 시작")
print("=========================================")
for sw in direct_switches:
    backup_device(sw, sw['name'])

# =========================================================
# 2단계: 경유지(ns0250) 자체 백업
# =========================================================
print("\n=========================================")
print("▶ 2단계: 경유지(ns0250) 자체 백업 시작")
print("=========================================")
if bastion_info:
    backup_device(bastion_info, bastion_info['name'])

# =========================================================
# 3단계: 경유지 경유 내부망 스위치 점프 백업
# =========================================================
print("\n=========================================")
print("▶ 3단계: 경유지(ns0250) 진입 후 내부망 스위치 점프 백업")
print("=========================================")
if bastion_info and internal_switches:
    bastion_base = {
        'device_type': bastion_info['device_type'],
        'host': bastion_info['host'],
        'username': bastion_info['username'],
        'password': bastion_info['password'],
        'port': bastion_info['port'],
        'secret': bastion_info['password'],
    }

    for sw in internal_switches:
        net_connect = None
        try:
            print(f"[{sw['name']}] 연결을 위해 경유지 세션 수립 중...")
            net_connect = ConnectHandler(**bastion_base)
            
            # 경유지에서는 이미 2단계에서 enable을 검증했으므로 바로 점프 명령 실행
            if sw['port'] == 23 or 'telnet' in sw['device_type']:
                print(f"-> CLI 점프: telnet {sw['host']}")
                net_connect.write_channel(f"telnet {sw['host']}\n")
                time.sleep(2)
                
                output = net_connect.read_channel()
                if "verification" in output.lower() or "username" in output.lower() or "login" in output.lower():
                    net_connect.write_channel(f"{sw['username']}\n")
                    time.sleep(1)
            else:
                print(f"-> CLI 점프: ssh -l {sw['username']} {sw['host']}")
                net_connect.write_channel(f"ssh -l {sw['username']} {sw['host']}\n")
                time.sleep(2)
            
            # 패스워드 입력
            output = net_connect.read_channel()
            if "password" in output.lower():
                net_connect.write_channel(f"{sw['password']}\n")
                time.sleep(2)
            
            # 제어권 타겟 장비로 변경
            redispatch(net_connect, device_type=sw['device_type'])
            
            # 🔥 [핵심] 점프한 내부망 장비에서 관리자 모드(#) 진입 조치
            net_connect.secret = sw['password']
            net_connect.enable()
            
            print(f"-> {sw['name']} 설정 스크래핑 중...")
            config_result = net_connect.send_command("show running-config")
            
            file_path = f"/ansible/backups/{sw['name']}_{today}.txt"
            with open(file_path, "w", encoding="utf-8") as file:
                file.write(config_result)
            print(f" 백업 성공: {file_path}")
            success_list.append(sw['name'])

        except Exception as e:
            print(f"❌ 백업 실패 [{sw['name']}]: {e}")
            failed_list.append({"name": sw['name'], "error": str(e)})
        finally:
            if net_connect:
                net_connect.disconnect()

print("\n=========================================")
print("모든 인벤토리 시나리오 백업이 완료되었습니다.")
print("=========================================")

# =========================================================
# 4단계: 네이버 웍스 봇 웹훅 전송 및 로깅
# =========================================================
total_count = len(success_list) + len(failed_list)
report_lines = [
    "[시스코 스위치 백업 결과 리포트]",
    f"- 일자: {today}",
    f"- 총 대상: {total_count}대 (성공: {len(success_list)}대, 실패: {len(failed_list)}대)",
    "",
    f"[성공 목록 ({len(success_list)}대)]",
    ", ".join(success_list) if success_list else "없음",
    "",
    f"[실패 목록 ({len(failed_list)}대)]"
]

if failed_list:
    for item in failed_list:
        report_lines.append(f"- {item['name']}: {item['error']}")
else:
    report_lines.append("없음")

report_text = "\n".join(report_lines)

def send_webhook_notification(payload_text):
    if not webhook_url:
        print("\n[Webhook] WEBHOOK_URL 설정이 없어 전송을 건너땁니다.")
        return

    headers = {
        "Content-Type": "text/plain; charset=utf-8",
    }
    if webhook_token:
        headers["Authorization"] = webhook_token

    print("\n=========================================")
    print("▶ 4단계: 네이버 웍스 봇 웹훅 전송")
    print("=========================================")
    print(f"[Webhook] 전송 시도 ({webhook_url})...")
    try:
        data = payload_text.encode("utf-8")
        req = urllib.request.Request(webhook_url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            status_code = response.getcode()
            resp_body = response.read().decode("utf-8", errors="ignore")
            print(f" 백업 결과 웹훅 전송 성공! (HTTP Status: {status_code})")
            if resp_body:
                print(f"  응답 내용: {resp_body}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"❌ 웹훅 전송 실패 (HTTP Status: {e.code}): {e.reason}")
        if err_body:
            print(f"  에러 응답 내용: {err_body}")
    except Exception as e:
        print(f"❌ 웹훅 전송 실패 (네트워크/기타 오류): {e}")

send_webhook_notification(report_text)

