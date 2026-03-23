#!/bin/bash
# =============================================================================
# Recovery Script - Universal Telegram Bot (3 modes)
# Автоматическое восстановление после перезагрузки сервера
# =============================================================================

set -e

LOG_FILE="/var/log/bot_recovery.log"
BOT_DIR="/app/telegram_bot"
VENV="/root/.venv"
SUPERVISOR_CONF="/etc/supervisor/conf.d/telegram_bot.conf"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $1" | tee -a "$LOG_FILE"
}

log "=== Начало восстановления ==="

# 1. Проверка виртуального окружения
if [ ! -d "$VENV" ]; then
    log "Создаю виртуальное окружение..."
    python3 -m venv "$VENV"
fi
log "venv: OK"

# 2. Установка зависимостей
log "Установка зависимостей..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$BOT_DIR/requirements.txt" 2>&1 | tee -a "$LOG_FILE"
log "Зависимости: OK"

# 3. Установка ffmpeg
if ! command -v ffmpeg &> /dev/null; then
    log "Установка ffmpeg..."
    apt-get update -qq && apt-get install -y -qq ffmpeg 2>&1 | tee -a "$LOG_FILE"
fi
log "ffmpeg: OK"

# 4. Проверка .env
if [ ! -f "$BOT_DIR/.env" ]; then
    log "ОШИБКА: .env не найден!"
    exit 1
fi
if ! grep -q "TELEGRAM_BOT_TOKEN=" "$BOT_DIR/.env"; then
    log "ОШИБКА: TELEGRAM_BOT_TOKEN не задан!"
    exit 1
fi
log ".env: OK"

# 5. Supervisor конфигурация
if [ ! -f "$SUPERVISOR_CONF" ]; then
    log "Создаю конфигурацию supervisor..."
    cat > "$SUPERVISOR_CONF" << 'CONF'
[program:telegram_bot]
command=/root/.venv/bin/python /app/telegram_bot/bot.py
directory=/app/telegram_bot
autostart=true
autorestart=true
startsecs=5
startretries=10
stderr_logfile=/var/log/supervisor/telegram_bot.err.log
stdout_logfile=/var/log/supervisor/telegram_bot.out.log
environment=HOME="/root",PATH="/root/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CONF
fi
log "Supervisor conf: OK"

# 6. Перезапуск
log "Перезагрузка supervisor..."
supervisorctl reread 2>&1 | tee -a "$LOG_FILE"
supervisorctl update 2>&1 | tee -a "$LOG_FILE"
supervisorctl restart telegram_bot 2>&1 | tee -a "$LOG_FILE"

# 7. Проверка
sleep 5
STATUS=$(supervisorctl status telegram_bot 2>&1)
log "Статус: $STATUS"

if echo "$STATUS" | grep -q "RUNNING"; then
    log "Бот успешно запущен!"
else
    log "ОШИБКА: Бот не запустился!"
    tail -n 20 /var/log/supervisor/telegram_bot.err.log 2>/dev/null | tee -a "$LOG_FILE"
    exit 1
fi

log "=== Восстановление завершено ==="
