#!/usr/bin/env bash
# Download a published release; never install directly from an untested main branch.
set -Eeuo pipefail

REPOSITORY="${RH_PREM_GITHUB_REPOSITORY:-Kinbas-validator/rh-prem-bot}"
RELEASE_TAG=latest
PACKAGE_DIR=""
INSTALL_ARGS=()

bootstrap_die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }
bootstrap_require_root() { [[ "$EUID" -eq 0 ]] || bootstrap_die 'Запустите через sudo bash.'; }

download_release() {
  local url="$1" output="$2"
  curl --silent --show-error --fail --location --proto '=https' --tlsv1.2 \
    --retry 3 --connect-timeout 20 --max-time 300 --max-filesize 16777216 \
    --output "$output" "$url"
}

bootstrap_main() {
  local arg url expected filename extra actual
  while (($#)); do
    arg="$1"; shift
    case "$arg" in
      -h|--help)
        printf '%s\n' 'Установка: curl -fsSL https://raw.githubusercontent.com/Kinbas-validator/rh-prem-bot/main/bootstrap.sh | sudo bash' \
          'Обновление: sudo rh-prem-bot update' 'Параметры: --version vX.Y.Z, --keep-config, --reconfigure'
        return 0 ;;
      --keep-config|--reconfigure) INSTALL_ARGS+=("$arg") ;;
      --version) (($#)) || bootstrap_die 'После --version нужен тег релиза.'; RELEASE_TAG="$1"; shift ;;
      *) bootstrap_die "Неизвестный параметр: $arg" ;;
    esac
  done
  bootstrap_require_root
  [[ "$REPOSITORY" =~ ^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$ ]] || bootstrap_die 'Некорректный репозиторий GitHub.'
  [[ "$RELEASE_TAG" == latest || "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || bootstrap_die 'Ожидается тег vX.Y.Z.'
  [[ -t 0 ]] || bootstrap_die 'Для настройки нужен интерактивный SSH-терминал.'
  for arg in curl sha256sum mktemp; do
    command -v "$arg" >/dev/null || bootstrap_die "Не найдена утилита $arg; установите её отдельно."
  done
  PACKAGE_DIR="$(mktemp -d /tmp/rh-prem-download.XXXXXX)"
  trap 'rm -rf -- "$PACKAGE_DIR"' EXIT
  if [[ "$RELEASE_TAG" == latest ]]; then
    url="https://github.com/$REPOSITORY/releases/latest/download"
  else
    url="https://github.com/$REPOSITORY/releases/download/$RELEASE_TAG"
  fi
  printf 'Скачиваю %s (%s) с GitHub…\n' "$REPOSITORY" "$RELEASE_TAG"
  download_release "$url/rh-prem-bot-installer.run" "$PACKAGE_DIR/rh-prem-bot-installer.run"
  download_release "$url/rh-prem-bot-installer.run.sha256" "$PACKAGE_DIR/checksum"
  read -r expected filename extra <"$PACKAGE_DIR/checksum"
  [[ "$expected" =~ ^[a-f0-9]{64}$ && "$filename" == rh-prem-bot-installer.run && -z "$extra" ]] || bootstrap_die 'Некорректный файл контрольной суммы.'
  actual="$(sha256sum "$PACKAGE_DIR/rh-prem-bot-installer.run")"
  [[ "${actual%% *}" == "$expected" ]] || bootstrap_die 'Контрольная сумма не совпала. Установка отменена; повторите загрузку.'
  printf 'Контрольная сумма SHA256 подтверждена.\n'
  bash "$PACKAGE_DIR/rh-prem-bot-installer.run" "${INSTALL_ARGS[@]}"
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  if [[ -t 0 || "${1:-}" == --help || "${1:-}" == -h ]]; then
    bootstrap_main "$@"
  elif { true </dev/tty; } 2>/dev/null; then
    # Only the function receives the terminal; Bash still reads this script from the pipe.
    bootstrap_main "$@" </dev/tty
  else
    bootstrap_die 'Запустите команду в интерактивном SSH-терминале.'
  fi
fi
