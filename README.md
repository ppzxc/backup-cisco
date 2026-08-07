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
- **Docker 기반 격리 환경**
  - 의존성 설치 없이 Docker Compose 컨테이너 환경에서 독립 실행
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
    device_type: cisco_ios_telnet

# 4. (선택사항) 웹훅 URL 및 토큰 설정
webhook_url: "https://your-webhook-endpoint.com/api/v1/bots/xxx/plain"
webhook_token: "Bearer YOUR_BEARER_TOKEN"
```

> **Note**: `WEBHOOK_URL` 및 `WEBHOOK_TOKEN`은 환경변수(`export WEBHOOK_URL=...`)로 설정할 수도 있습니다.

---

## ⚙️ 실행 방법

### 1. Docker Compose로 수동 실행 (테스트)

```bash
docker compose up --build
```
실행이 완료되면 백업 파일이 `backups/` 디렉터리에 `{장비명}_{YYYYMMDD}.txt` 형식으로 저장되고 웹훅이 전송됩니다.

---

### 2. `systemd` 자동화 스케줄 등록 (`systemd.sh`)

제공되는 `systemd.sh` 스크립트를 사용하여 리눅스 서버에 매일 새벽 03:00 정기 백업 스케줄을 등록할 수 있습니다.

#### A. 스케줄러 등록 및 활성화
```bash
sudo ./systemd.sh install
```

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
