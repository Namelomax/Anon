#!/usr/bin/env bash
# Запуск анонимайзера одной командой. Полный конвейер (regex + GLiNER + LLM +
# review + second-pass + recall) включён В САМОМ server.py по умолчанию —
# никакие флаги не нужны.
#
#   bash anonymizer/run.sh                     # порт 8000, GPU 0, всё включено
#   bash anonymizer/run.sh --port 8500         # доп. аргументы пробрасываются
#   bash anonymizer/run.sh --no-review         # что-то отключить
#   CUDA_VISIBLE_DEVICES=1 bash anonymizer/run.sh   # другая видеокарта
#
# Чтобы ОТКЛЮЧИТЬ стадии: --no-llm --no-review --no-second-pass --no-recall
#   --no-corporate --ner none ; включить reasoning LLM: --think.

set -euo pipefail

# Видеокарта по умолчанию — 0, можно переопределить извне.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"

# Абсолютный путь к каталогу скрипта — работает из любого cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python "$HERE/server.py" "$@"
