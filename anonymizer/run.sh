#!/usr/bin/env bash
# Запуск анонимайзера одной командой — корпоративный режим, полный конвейер.
#
# Использование:
#   bash anonymizer/run.sh                # порт 8000, GPU 0
#   bash anonymizer/run.sh --port 8500    # любые доп. флаги пробрасываются
#   CUDA_VISIBLE_DEVICES=1 bash anonymizer/run.sh   # другая видеокарта
#
# Все "лишние" флаги из старой команды убраны: они и так равны дефолтам
# server.py (--port 8000, --device cuda, --ner gliner,
# --llm-base-url http://127.0.0.1:11433/v1, --llm-model qwen3.5:9b).
# Остались только те, что реально меняют поведение.

set -euo pipefail

# Видеокарта по умолчанию — 0, но можно переопределить извне.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"

# Абсолютный путь к каталогу этого скрипта — работает из любого cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python "$HERE/server.py" \
  --corporate \
  --llm \
  --review \
  --second-pass \
  --llm-recall \
  --llm-no-think \
  --review-no-think \
  "$@"
