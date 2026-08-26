# Cisco Switch Backup Scraper & Systemd Automation

시스코(Cisco) 네트워크 스위치의 설정(`show running-config`)을 자동 수집하고, 네이버 웍스(NAVER Works) 봇 등의 웹훅(Webhook)으로 결과를 알리는 **Docker & systemd 기반 네트워크 자동화 시스템**입니다.

Direct 접속, Bastion(경유지) 스위치를 이용한 점프 접속(SSH/Telnet) 등 다양한 네트워크 토폴로지를 지원합니다.

---

## ✨ 주요 기능

- **다양한 접속 토폴로지 지원 (`Netmiko`)**
  - **Direct 접속**: SSH(22) 및 Telnet(23) 직접 접속
  - **Bastion 점프 접속**: Bastion 장비 진입 후 내부망 스위치로 2차 점프 접속 (`redispatch`)
- **실시간 웹훅 알림 (Custom Webhook)**
  - 백업 완료 후 성공/실패 수량, 장비 목록, 에러 상세 원인을 커스텀 웹훅(HTTP POST `text/plain`)으로 전송
- **고정된 Docker 런타임**
  - `requirements.txt`의 고정 의존성을 Docker 이미지 빌드 시 설치해, 예약 실행 중 외부 패키지 설치를 하지 않음
- **systemd 스케줄링 자동화 (`systemd.sh`)**
  - 리눅스 `systemd` 서비스 및 타이머 스케줄러(매일 03:00 AM) 등록, 해제, 수동 즉시 실행 스크립트 제공
- **보안 중심 설계**
  - 스위치 비밀번호, IP 주소 및 API 토큰을 환경변수 및 `.gitignore` 처리하여 Public 저장소 안전 유지

---

## 📁 프로젝트 구조

```text
.
├── docker-compose.yml        # Docker 컨테이너 구성 파일
├── systemd.sh                # systemd 서비스/타이머 관리 스크립트 (install|uninstall|run)
├── ssh_config.example        # Bastion 점프용 SSH 설정 템플릿
├── .gitignore                # 백업 파일 및 실제 민감 설정 제외 규칙
└── project/
    ├── scraper.py            # 백업 스크래퍼 및 웹훅 전송 메인 로직
    └── devices.yml.example   # 스위치 인벤토리 및 웹훅 설정 템플릿
```

---

## 🚀 시작하기

### 1. 환경 설정 파일 준비

프로젝트의 `example` 템플릿 파일들을 복사하여 실제 설정 파일을 생성합니다.

```bash
# 1. 스위치 인벤토리 설정 파일 복사
cp project/devices.yml.example project/devices.yml

# 2. SSH 경유지 설정 파일 복사 (필요 시)
cp ssh_config.example ssh_config
```

### 2. `devices.yml` 설정 수정

`project/devices.yml` 파일에 백업 대상 스위치 정보를 입력합니다:

```yaml
# 1. 다이렉트(직접) 접속 스위치
direct_switches:
  - name: ns0278
    host: 211.210.44.182
    username: admin
    password: "YOUR_PASSWORD"
    port: 22
    device_type: cisco_ios

# 2. 경유지(Bastion) 스위치
bastion_switch:
  name: ns0250
  host: 211.210.44.186
  username: admin
  password: "YOUR_PASSWORD"
  port: 22
  device_type: cisco_ios

# 3. 경유지(ns0250)를 타고 들어갈 내부망 스위치
internal_switches:
  - name: ns0279
    host: 10.10.200.4
    username: admin
    password: "YOUR_PASSWORD"
    port: 23
    transport: telnet
    device_type: cisco_ios_telnet

# 4. (선택사항) 웹훅 URL 및 토큰 설정
webhook_url: "https://your-webhook-endpoint.com/api/v1/bots/xxx/plain"
webhook_token: "Bearer YOUR_BEARER_TOKEN"
```

> **Note**: `WEBHOOK_URL` 및 `WEBHOOK_TOKEN`은 환경변수(`export WEBHOOK_URL=...`)로 설정할 수도 있습니다.

> **업그레이드 주의**: 기존 `internal_switches`의 각 항목에는 반드시 `transport: ssh` 또는 `transport: telnet`을 추가해야 합니다. 누락하면 안전을 위해 백업 실행이 시작되지 않습니다.

---

## ⚙️ 실행 방법

### 1. Docker Compose로 수동 실행 (테스트 및 단발성 실행)

```bash
# 기본 정기 백업 실행 (show running-config)
BACKUP_UID=$(id -u) BACKUP_GID=$(id -g) docker compose run --rm backup-cisco

# 단발성 진단 정보 수집 (show tech-support)
BACKUP_UID=$(id -u) BACKUP_GID=$(id -g) docker compose run --rm backup-cisco --tech-support

# 특정 장비 1대만 지정하여 수집 (예: ns0278)
BACKUP_UID=$(id -u) BACKUP_GID=$(id -g) docker compose run --rm backup-cisco --target ns0278
BACKUP_UID=$(id -u) BACKUP_GID=$(id -g) docker compose run --rm backup-cisco --tech-support --target ns0278
```
최초 실행 또는 의존성 변경 뒤에는 먼저 `docker compose build backup-cisco`를 실행합니다. 
- 백업 파일: `backups/{장비명}_{YYYYMMDDTHHMMSS}.txt`
- Tech-Support 파일: `backups/{장비명}_tech_{YYYYMMDDTHHMMSS}.txt`

같은 날 재실행해도 기존 파일을 덮어쓰지 않으며, 90일이 지난 백업은 다음 실행 중 정리됩니다.

---

### 2. `systemd` 자동화 스케줄 등록 (`systemd.sh`)

제공되는 `systemd.sh` 스크립트를 사용하여 리눅스 서버에 매일 새벽 03:00 정기 백업 스케줄을 등록할 수 있습니다.

#### A. 스케줄러 등록 및 활성화
```bash
sudo ./systemd.sh install
```

설치 과정은 전용 `backup-cisco` 시스템 계정을 만들고 Docker 그룹에 추가합니다. 또한 실제 `project/devices.yml`을 `root:backup-cisco`, `0640`으로, `backups/`를 서비스 계정 전용 `0700`으로 설정합니다. 이 권한은 실제 자격 증명이 있는 인벤토리를 보호하므로 설치 후에는 root로 수정하세요.

#### B. 백업 수동 즉시 실행 및 로그 확인
```bash
sudo ./systemd.sh run
```

#### C. 스케줄러 등록 해제
```bash
sudo ./systemd.sh uninstall
```

---

## 🔔 웹훅 결과 메시지 예시 (Naver Works Bot / Custom Text)

```text
[시스코 스위치 백업 결과 리포트]
- 일자: 20260807
- 총 대상: 10대 (성공: 10대, 실패: 0대)

[성공 목록 (10대)]
ns0278, ns0341, ns0065, ns0263, ns0347, ns0348, ns0250, ns0279, ns0281, ns0282

[실패 목록 (0대)]
없음
```

---

## 🔒 보안 (Security)

- `backups/*.txt` (백업 결과물) 및 `project/devices.yml` (실제 계정 및 IP)은 `.gitignore`에 등록되어 있어 Git 커밋 및 Public 저장소 업로드 시 유출되지 않습니다.
- 실제 `devices.yml`에는 평문 자격 증명이 들어가므로 systemd 설치가 설정하는 `root:backup-cisco`, `0640` 권한을 유지해야 합니다.
- 백업 파일은 서비스 계정만 읽을 수 있는 `0700` 디렉터리에 `0600`으로 작성됩니다.
- 결과 알림과 로그에는 대상 이름과 실패 단계만 기록하며, 비밀번호·토큰·설정 본문·원문 예외는 포함하지 않습니다.

---

## 테스트

네트워크 장비 없이 인벤토리 검증, 산출물 보존, CLI 오류 처리, 보고서 마스킹을 확인합니다.

```bash
BACKUP_UID=$(id -u) BACKUP_GID=$(id -g) docker compose run --rm --entrypoint python3 backup-cisco -m unittest discover -s tests -v
```
