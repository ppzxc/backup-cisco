#!/usr/bin/env bash
set -e

SERVICE_NAME="backup-cisco"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TIMER_FILE="/etc/systemd/system/${SERVICE_NAME}.timer"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DOCKER_BIN="$(which docker || echo "/usr/bin/docker")"

check_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "❌ 이 작업은 root 권한이 필요합니다. sudo를 사용해 주세요."
        echo "예: sudo ./systemd.sh $1"
        exit 1
    fi
}

install_systemd() {
    check_root "install"
    echo "========================================="
    echo "▶ systemd 서비스 및 타이머 등록 중..."
    echo "========================================="
    
    if ! getent group "$SERVICE_NAME" >/dev/null; then
        groupadd --system "$SERVICE_NAME"
    fi
    if ! id -u "$SERVICE_NAME" >/dev/null 2>&1; then
        useradd --system --gid "$SERVICE_NAME" --home-dir "$PROJECT_DIR" \
            --shell /usr/sbin/nologin "$SERVICE_NAME"
    fi
    if getent group docker >/dev/null; then
        usermod -aG docker "$SERVICE_NAME"
    else
        echo "❌ docker 그룹을 찾을 수 없습니다. Docker 설치 상태를 확인하세요."
        exit 1
    fi
    if [ ! -f "$PROJECT_DIR/project/devices.yml" ]; then
        echo "❌ project/devices.yml 파일이 필요합니다. example 파일을 복사해 설정하세요."
        exit 1
    fi

    install -d -o "$SERVICE_NAME" -g "$SERVICE_NAME" -m 0700 "$PROJECT_DIR/backups"
    chown root:"$SERVICE_NAME" "$PROJECT_DIR/project/devices.yml"
    chmod 0640 "$PROJECT_DIR/project/devices.yml"
    BACKUP_UID="$(id -u "$SERVICE_NAME")"
    BACKUP_GID="$(id -g "$SERVICE_NAME")"

    # 1. Service 파일 생성
    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Cisco Switch Backup Docker Service
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=${SERVICE_NAME}
Group=${SERVICE_NAME}
WorkingDirectory=${PROJECT_DIR}
Environment=BACKUP_UID=${BACKUP_UID}
Environment=BACKUP_GID=${BACKUP_GID}
ExecStart=${DOCKER_BIN} compose run --rm backup-cisco

[Install]
WantedBy=multi-user.target
EOF

    # 2. Timer 파일 생성 (매일 03:00)
    cat <<EOF > "$TIMER_FILE"
[Unit]
Description=Run Cisco Switch Backup Daily at 3 AM

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

    # 3. 검증된 런타임 이미지를 미리 빌드하고 systemd 적용
    ${DOCKER_BIN} compose build backup-cisco
    systemctl daemon-reload
    systemctl enable --now "${SERVICE_NAME}.timer"
    
    echo " 백업 systemd 타이머가 등록 및 활성화되었습니다!"
    echo ""
    echo "등록된 타이머 상태:"
    systemctl list-timers --all | grep "${SERVICE_NAME}" || true
}

uninstall_systemd() {
    check_root "uninstall"
    echo "========================================="
    echo "▶ systemd 서비스 및 타이머 제거 중..."
    echo "========================================="
    
    systemctl disable --now "${SERVICE_NAME}.timer" 2>/dev/null || true
    systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true
    
    rm -f "$SERVICE_FILE" "$TIMER_FILE"
    systemctl daemon-reload
    
    echo " systemd 서비스 및 타이머가 성공적으로 제거되었습니다."
}

run_systemd() {
    check_root "run"
    echo "========================================="
    echo "▶ systemd 백업 서비스 수동 즉시 실행 중..."
    echo "========================================="
    
    systemctl start "${SERVICE_NAME}.service"
    
    echo " 실행 완료. 최근 실행 로그:"
    journalctl -u "${SERVICE_NAME}.service" -n 20 --no-pager
}

usage() {
    echo "사용법: $0 {install|uninstall|run}"
    echo "  install   : systemd 서비스 및 타이머(매일 03:00) 등록/활성화"
    echo "  uninstall : systemd 서비스 및 타이머 해제/삭제"
    echo "  run       : systemd 서비스를 통해 수동 즉시 실행 및 로그 확인"
    exit 1
}

case "$1" in
    install)
        install_systemd
        ;;
    uninstall)
        uninstall_systemd
        ;;
    run)
        run_systemd
        ;;
    *)
        usage
        ;;
esac
