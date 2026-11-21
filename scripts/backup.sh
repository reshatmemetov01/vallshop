#!/usr/bin/env bash
# Автобэкап базы VallShop.
#
# Что делает:
#   1. Безопасный онлайн-снимок SQLite (sqlite backup API, не cp — чтобы не
#      словить повреждённый файл во время записи ботом/сайтом).
#   2. Сжимает в <repo>/backups/db-YYYYmmdd-HHMM.sqlite.gz.
#   3. Ротация: удаляет бэкапы старше BACKUP_KEEP_DAYS (по умолчанию 14).
#   4. ОФСАЙТ (важно после простоя ЦОД): если в .env заданы BOT_TOKEN и
#      BACKUP_CHAT_ID — отправляет копию в Telegram этому чату (sendDocument).
#
# Запуск вручную:  bash scripts/backup.sh
# По расписанию:   см. комментарий внизу (crontab, каждые 12 часов).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${BACKUP_DB:-$REPO/data/vallshop.db}"
OUT="${BACKUP_DIR:-$REPO/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
ENV_FILE="${BACKUP_ENV_FILE:-$REPO/.env}"
STAMP="$(date +%Y%m%d-%H%M)"

mkdir -p "$OUT"
log() { echo "[$(date '+%F %T')] $*"; }

if [ ! -f "$DB" ]; then
  log "ОШИБКА: база не найдена: $DB"
  exit 1
fi

# 1) Безопасный снимок через sqlite backup API (python3 есть почти везде).
SNAP="$OUT/db-$STAMP.sqlite"
python3 - "$DB" "$SNAP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)      # онлайн-бэкап: консистентный даже при активной записи
dst.close(); src.close()
PY

# 2) Сжатие.
gzip -f "$SNAP"
ARCHIVE="$SNAP.gz"
SIZE="$(du -h "$ARCHIVE" | cut -f1)"
log "Снимок готов: $ARCHIVE ($SIZE)"

# 3) Ротация по возрасту.
find "$OUT" -maxdepth 1 -name 'db-*.sqlite.gz' -type f -mtime "+$KEEP_DAYS" -print -delete \
  | while read -r f; do log "Удалён старый бэкап: $f"; done || true

# 4) Офсайт в Telegram (если настроено).
get_env() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | sed 's/^["'"'"']//; s/["'"'"']$//'
}
TOKEN="${BACKUP_BOT_TOKEN:-$(get_env BOT_TOKEN)}"
CHAT="${BACKUP_CHAT_ID:-$(get_env BACKUP_CHAT_ID)}"

if [ -n "${TOKEN:-}" ] && [ -n "${CHAT:-}" ]; then
  BYTES="$(stat -c%s "$ARCHIVE" 2>/dev/null || stat -f%z "$ARCHIVE")"
  if [ "$BYTES" -gt 49000000 ]; then
    log "Бэкап >49MB — лимит Telegram sendDocument. Офсайт пропущен, настройте rclone."
  else
    if curl -sS --max-time 120 \
        --form-string "chat_id=$CHAT" \
        --form-string "caption=🗄 VallShop backup $STAMP ($SIZE)" \
        -F "document=@$ARCHIVE" \
        "https://api.telegram.org/bot$TOKEN/sendDocument" >/dev/null; then
      log "Офсайт-копия отправлена в Telegram (chat $CHAT)."
    else
      log "ВНИМАНИЕ: не удалось отправить офсайт-копию в Telegram."
    fi
  fi
else
  log "Офсайт в Telegram не настроен (нет BOT_TOKEN/BACKUP_CHAT_ID в .env) — только локальная копия."
fi

log "Готово."

# ── Расписание (каждые 12 часов) ──────────────────────────────────────────────
# crontab -e  и добавить строку (в 06:00 и 18:00):
#   0 6,18 * * * /opt/vallshop/scripts/backup.sh >> /opt/vallshop/backups/cron.log 2>&1
# Или раз в сутки (03:30):
#   30 3 * * * /opt/vallshop/scripts/backup.sh >> /opt/vallshop/backups/cron.log 2>&1
