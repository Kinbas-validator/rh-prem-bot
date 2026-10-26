#!/usr/bin/env bash
# RH_PREM_BOT_MANAGED_INSTALLER
set -Eeuo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_ROOT=/opt/rh-prem-bot
CONFIG_DIR=/etc/rh-prem-bot
STATE_DIR=/var/lib/rh-prem-bot
SERVICE_USER=rh-prem-bot
UNIT=/etc/systemd/system/rh-prem-bot.service
CONTROL=/usr/local/bin/rh-prem-bot
SERVICE=rh-prem-bot.service
INSTALL_LOCK=/run/lock/rh-prem-bot-install.lock
RECONFIGURE=0
KEEP_CONFIG=0
SUCCESS=0
ACTIVATED=0
DB_BACKED_UP=0
STOPPED_OLD=0
WAS_ACTIVE=0
WAS_ENABLED=0
WORK_DIR=""
RELEASE=""
OLD_CURRENT=""

die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
say() { printf '\n→ %s\n' "$*"; }

require_platform() {
  [[ -r /etc/os-release ]] || die 'Нужна Ubuntu 22.04 или новее.'
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ "$ID" == ubuntu && "${VERSION_ID%%.*}" -ge 22 ]] || die 'Нужна Ubuntu 22.04 или новее.'
  if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null; then
    die 'systemd не запущен на этом сервере.'
  fi
}

require_root() {
  if [[ "$EUID" -ne 0 ]]; then
    command -v sudo >/dev/null || die 'Запустите от root: bash install.sh'
    exec sudo bash "$SOURCE_DIR/install.sh" "$@"
  fi
}

service_python() {
  runuser -u "$SERVICE_USER" -- "$RELEASE/.venv/bin/python" -m rh_prem_bot "$@"
}

write_unit() {
  cat <<EOF
# RH_PREM_BOT_MANAGED_SERVICE
[Unit]
Description=Lighter Robinhood RWA Premium Telegram Monitor
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_ROOT/current
ExecStart=$APP_ROOT/current/.venv/bin/python -m rh_prem_bot --env-file $CONFIG_DIR/bot.env run
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
TimeoutStopSec=20
CPUAccounting=true
CPUWeight=20
CPUQuota=50%
MemoryAccounting=true
MemoryHigh=192M
MemoryMax=256M
MemorySwapMax=64M
TasksMax=64
IOWeight=20
Nice=10
OOMScoreAdjust=500
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$STATE_DIR

[Install]
WantedBy=multi-user.target
EOF
}

write_control() {
  cat <<EOF
#!/usr/bin/env bash
# RH_PREM_BOT_MANAGED_CONTROL
set -euo pipefail
case "\${1:-status}" in
  help|--help|-h) printf '%s\n' 'rh-prem-bot status|logs|start|stop|restart|health|setup|update'; exit 0 ;;
esac
if [[ "\$EUID" -ne 0 ]]; then exec sudo "\$0" "\$@"; fi
case "\${1:-status}" in
  status) exec systemctl status $SERVICE --no-pager ;;
  logs) exec journalctl -u $SERVICE -f ;;
  start|stop|restart) exec systemctl "\$1" $SERVICE ;;
  health) cd '$APP_ROOT/current'; exec runuser -u '$SERVICE_USER' -- '$APP_ROOT/current/.venv/bin/python' -m rh_prem_bot --env-file '$CONFIG_DIR/bot.env' health ;;
  setup) exec bash '$APP_ROOT/current/install.sh' --reconfigure ;;
  update) exec bash '$APP_ROOT/current/bootstrap.sh' --keep-config ;;
  *) printf '%s\n' 'Команды: status, logs, start, stop, restart, health, setup, update' >&2; exit 2 ;;
esac
EOF
}

restore_file() {
  local name="$1" target="$2"
  if [[ -f "$WORK_DIR/backup-$name" ]]; then
    cp -p -- "$WORK_DIR/backup-$name" "$target"
  else
    rm -f -- "$target"
  fi
}

cleanup() {
  local code=$? rollback_ok=1
  trap - EXIT INT TERM
  set +e
  if [[ "$SUCCESS" -ne 1 ]]; then
    if [[ "$ACTIVATED" -eq 1 ]]; then
      if ! systemctl stop "$SERVICE" >/dev/null 2>&1; then
        # Never overwrite SQLite or executable files while the new service may still be running.
        rollback_ok=0
      else
        if [[ "$DB_BACKED_UP" -eq 1 ]]; then
          rm -f -- "$STATE_DIR/bot.sqlite3-wal" "$STATE_DIR/bot.sqlite3-shm"
          install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$WORK_DIR/backup-db" "$STATE_DIR/bot.sqlite3" || rollback_ok=0
        fi
        restore_file env "$CONFIG_DIR/bot.env" || rollback_ok=0
        restore_file unit "$UNIT" || rollback_ok=0
        restore_file control "$CONTROL" || rollback_ok=0
        if [[ -n "$OLD_CURRENT" ]]; then
          ln -sfnT -- "$OLD_CURRENT" "$APP_ROOT/current" || rollback_ok=0
        else
          rm -f -- "$APP_ROOT/current" || rollback_ok=0
        fi
        systemctl daemon-reload || rollback_ok=0
        if [[ "$WAS_ENABLED" -eq 1 ]]; then
          systemctl enable "$SERVICE" >/dev/null 2>&1 || rollback_ok=0
        else
          systemctl disable "$SERVICE" >/dev/null 2>&1
        fi
      fi
    fi
    if [[ "$rollback_ok" -eq 1 && "$WAS_ACTIVE" -eq 1 && ( "$STOPPED_OLD" -eq 1 || "$ACTIVATED" -eq 1 ) ]]; then
      systemctl reset-failed "$SERVICE" >/dev/null 2>&1
      systemctl start "$SERVICE" || rollback_ok=0
    fi
    if [[ "$rollback_ok" -eq 1 && -n "$RELEASE" && "$RELEASE" == "$APP_ROOT/releases/"* ]]; then
      rm -rf -- "$RELEASE"
    fi
    if [[ "$rollback_ok" -eq 1 ]]; then
      printf '\nУстановка не завершена. Предыдущие настройки и сервис сохранены/восстановлены.\n' >&2
    else
      printf '\nОткат требует проверки: systemctl status %s. Резервные файлы: %s\n' "$SERVICE" "$WORK_DIR" >&2
    fi
  fi
  if [[ "$rollback_ok" -eq 1 && -n "$WORK_DIR" ]]; then rm -rf -- "$WORK_DIR"; fi
  exit "$code"
}

install_runtime() {
  say 'Устанавливаю изолированное окружение (системные пакеты не меняются)'
  if [[ ! -x "$APP_ROOT/tools/uv" ]]; then
    curl --silent --show-error --proto '=https' --tlsv1.2 --fail --location --retry 3 --connect-timeout 20 \
      --output "$WORK_DIR/uv-install.sh" https://astral.sh/uv/install.sh
    UV_UNMANAGED_INSTALL="$APP_ROOT/tools" sh "$WORK_DIR/uv-install.sh"
  fi
  say 'Устанавливаю Python 3.12 и зависимости бота'
  UV_PYTHON_INSTALL_DIR="$APP_ROOT/python" UV_CACHE_DIR="$APP_ROOT/cache" UV_PROJECT_ENVIRONMENT="$RELEASE/.venv" \
    UV_CONCURRENT_BUILDS=1 UV_CONCURRENT_INSTALLS=1 UV_CONCURRENT_DOWNLOADS=2 \
    nice -n 10 "$APP_ROOT/tools/uv" sync --project "$RELEASE" --frozen --no-dev --no-editable \
    --python 3.12 --managed-python
}

prune_old_releases() {
  local directory previous="$OLD_CURRENT"
  if [[ -n "$previous" && "$previous" != /* ]]; then previous="$APP_ROOT/$previous"; fi
  for directory in "$APP_ROOT"/releases/release-*; do
    [[ -d "$directory" && ! -L "$directory" ]] || continue
    if [[ "$directory" != "$RELEASE" && "$directory" != "$previous" ]]; then
      rm -rf -- "$directory"
    fi
  done
}

wait_healthy() {
  local attempt unit_pid
  for attempt in {1..45}; do
    unit_pid="$(systemctl show --property=MainPID --value "$SERVICE")"
    if systemctl is-active --quiet "$SERVICE" && \
      service_python --env-file "$CONFIG_DIR/bot.env" health >"$WORK_DIR/health.json" 2>/dev/null && \
      "$RELEASE/.venv/bin/python" -c \
        'import json,sys; h=json.load(open(sys.argv[2])); sys.exit(0 if sys.argv[1].isdigit() and int(sys.argv[1])>0 and h.get("pid")==int(sys.argv[1]) else 1)' \
        "$unit_pid" "$WORK_DIR/health.json"; then
      return 0
    fi
    printf '\rОжидание подключения: %d/45' "$attempt"
    sleep 2
  done
  printf 'Сервис не подтвердил подключение ко всем рынкам. Диагностика: journalctl -u %s\n' "$SERVICE" >&2
  return 1
}

main() {
  local arg item answer
  for arg in "$@"; do
    case "$arg" in
      --help|-h) printf '%s\n' 'Установка: sudo bash install.sh' 'Изменить настройки: sudo bash install.sh --reconfigure' 'Обновление с сохранением настроек: sudo bash install.sh --keep-config'; return 0 ;;
      --reconfigure) RECONFIGURE=1 ;;
      --keep-config) KEEP_CONFIG=1 ;;
      *) die "Неизвестный параметр: $arg" ;;
    esac
  done
  require_root "$@"
  require_platform
  umask 022
  [[ "$RECONFIGURE" -eq 0 || "$KEEP_CONFIG" -eq 0 ]] || die '--reconfigure и --keep-config несовместимы.'
  [[ -t 0 ]] || die 'Запустите установщик в интерактивном SSH-терминале.'
  for item in curl xz runuser useradd flock sha256sum nice; do
    command -v "$item" >/dev/null || die "Не найдена утилита $item. Установите её отдельно и повторите запуск; APT автоматически не вызывается."
  done
  exec 9>"$INSTALL_LOCK"
  flock -n 9 || die 'Другой установщик уже работает.'
  for item in pyproject.toml uv.lock .env.example src install.sh bootstrap.sh; do
    [[ -e "$SOURCE_DIR/$item" ]] || die "В папке проекта отсутствует $item. Распакуйте весь архив."
  done
  if [[ -e "$UNIT" ]] && ! grep -Fq 'rh_prem_bot' "$UNIT"; then
    die "Файл $UNIT принадлежит другому приложению."
  fi
  if [[ -e "$CONTROL" ]] && ! grep -Fq 'RH_PREM_BOT_MANAGED_CONTROL' "$CONTROL"; then
    die "Команда $CONTROL уже существует и не принадлежит установщику."
  fi
  [[ ! -e "$APP_ROOT/current" || -L "$APP_ROOT/current" ]] || die "$APP_ROOT/current должен быть ссылкой версии."
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  install -d -m 755 "$APP_ROOT" "$APP_ROOT/releases"
  install -d -o root -g "$SERVICE_USER" -m 750 "$CONFIG_DIR"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$STATE_DIR"
  WORK_DIR="$(mktemp -d "$STATE_DIR/.install.XXXXXX")"
  chown "root:$SERVICE_USER" "$WORK_DIR"
  chmod 750 "$WORK_DIR"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$WORK_DIR/wizard"
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  systemctl is-active --quiet "$SERVICE" && WAS_ACTIVE=1
  systemctl is-enabled --quiet "$SERVICE" && WAS_ENABLED=1
  [[ ! -L "$APP_ROOT/current" ]] || OLD_CURRENT="$(readlink "$APP_ROOT/current")"
  for item in env unit control; do
    case "$item" in env) arg="$CONFIG_DIR/bot.env" ;; unit) arg="$UNIT" ;; control) arg="$CONTROL" ;; esac
    if [[ -f "$arg" ]]; then cp -p -- "$arg" "$WORK_DIR/backup-$item"; fi
  done
  # Backups containing credentials remain root-only, even though the wizard runs as the service user.
  if [[ -f "$WORK_DIR/backup-env" ]]; then chown root:root "$WORK_DIR/backup-env"; chmod 600 "$WORK_DIR/backup-env"; fi
  RELEASE="$(mktemp -d "$APP_ROOT/releases/release-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
  chmod 755 "$RELEASE"
  for item in src pyproject.toml uv.lock .env.example install.sh bootstrap.sh; do cp -a -- "$SOURCE_DIR/$item" "$RELEASE/"; done
  chown -R root:root "$RELEASE"
  chmod -R a+rX "$RELEASE"
  install_runtime
  if [[ -f "$CONFIG_DIR/bot.env" && "$RECONFIGURE" -eq 0 && "$KEEP_CONFIG" -eq 0 ]]; then
    read -r -p 'Найдена конфигурация. Сохранить её? [Д/н]: ' answer
    case "$answer" in н|Н|n|N|no|нет) RECONFIGURE=1 ;; esac
  fi
  cd -- "$RELEASE"
  if [[ ! -f "$CONFIG_DIR/bot.env" || "$RECONFIGURE" -eq 1 ]]; then
    if [[ "$WAS_ACTIVE" -eq 1 ]]; then
      STOPPED_OLD=1
      systemctl stop "$SERVICE"
    fi
    say 'Настраиваю Telegram и мониторинг'
    service_python --env-file "$WORK_DIR/wizard/new.env" setup --advanced --service-mode
    arg="$WORK_DIR/wizard/new.env"
  else
    arg="$CONFIG_DIR/bot.env"
  fi
  say 'Проверяю конфигурацию, Telegram и доступ к бирже'
  runuser -u "$SERVICE_USER" -- "$RELEASE/.venv/bin/python" -c \
    'import sys; from rh_prem_bot.config import Settings; Settings.from_env(sys.argv[1]).validate(telegram=True)' "$arg"
  service_python --env-file "$arg" doctor
  write_unit >"$WORK_DIR/new.service"
  write_control >"$WORK_DIR/new-control"
  if [[ "$WAS_ACTIVE" -eq 1 && "$STOPPED_OLD" -eq 0 ]]; then
    STOPPED_OLD=1
    systemctl stop "$SERVICE"
  fi
  if [[ -f "$STATE_DIR/bot.sqlite3" ]]; then
    "$RELEASE/.venv/bin/python" -c \
      'import sqlite3,sys; a=sqlite3.connect(sys.argv[1]); b=sqlite3.connect(sys.argv[2]); a.backup(b); b.close(); a.close()' \
      "$STATE_DIR/bot.sqlite3" "$WORK_DIR/backup-db"
    chmod 600 "$WORK_DIR/backup-db"
    DB_BACKED_UP=1
  fi
  ACTIVATED=1
  if [[ "$arg" == "$WORK_DIR/wizard/new.env" ]]; then
    install -o root -g "$SERVICE_USER" -m 640 "$arg" "$CONFIG_DIR/bot.env"
    if [[ "$DB_BACKED_UP" -eq 1 ]]; then
      runuser -u "$SERVICE_USER" -- "$RELEASE/.venv/bin/python" -c \
        'import sys; from rh_prem_bot.store import Store; s=Store(sys.argv[1]); s.set("thresholds",{}); s.close()' "$STATE_DIR/bot.sqlite3"
    fi
  fi
  install -m 644 "$WORK_DIR/new.service" "$UNIT"
  install -m 755 "$WORK_DIR/new-control" "$CONTROL"
  ln -sfnT -- "$RELEASE" "$APP_ROOT/current"
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE" >/dev/null 2>&1 || true
  systemctl enable "$SERVICE"
  systemctl restart "$SERVICE"
  say 'Проверяю работу сервиса (до 90 секунд)'
  wait_healthy
  SUCCESS=1
  prune_old_releases || printf 'Не удалось очистить старые версии в %s/releases.\n' "$APP_ROOT" >&2
  printf '\nГотово! Бот установлен и работает как сервис. Можно закрывать SSH.\n'
  printf 'Статус: sudo rh-prem-bot status\nЛоги: sudo rh-prem-bot logs\nПерезапуск: sudo rh-prem-bot restart\nОбновление: sudo rh-prem-bot update\nНастроить заново: sudo rh-prem-bot setup\n'
  printf 'Конфигурация: %s/bot.env\nБаза: %s/bot.sqlite3\n' "$CONFIG_DIR" "$STATE_DIR"
  printf 'Откройте Telegram-бота и отправьте /start. Автоматические сигналы — после прогрева.\n'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
