#!/usr/bin/env bash
# Выкладка бота на VPS одной командой:  bash deploy.sh
# Заливает только код. .env и ghostclash.db на сервере не трогает.
set -euo pipefail
cd "$(dirname "$0")"

KEY="${VPS_KEY:-$HOME/.ssh/id_ed25519_vps}"
HOST="${VPS_HOST:-root@171.22.31.138}"
DIR=/opt/mogged
FILES="bot.py stats.py database.py image_generator.py requirements.txt Dockerfile docker-compose.yml fonts images"

for f in bot.py stats.py database.py image_generator.py; do
  python -c "import ast,sys; ast.parse(open('$f', encoding='utf-8').read())" || { echo "Синтаксическая ошибка в $f"; exit 1; }
done

echo "→ заливаю код на $HOST:$DIR"
tar czf - $FILES | ssh -i "$KEY" "$HOST" "mkdir -p $DIR/backups && cd $DIR && tar xzf -"

echo "→ пересобираю и перезапускаю"
ssh -i "$KEY" "$HOST" "cd $DIR && docker compose up -d --build 2>&1 | tail -3 && sleep 6 && docker compose logs --tail 8 bot" < /dev/null
