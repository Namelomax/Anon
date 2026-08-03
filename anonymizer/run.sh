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

# torch везёт СВОЙ cuDNN. Если в LD_LIBRARY_PATH лежит системный другой версии,
# torch подхватывает его и падает на несовместимости ("compiled against (9,20,0)
# but found runtime (9,13,1)"), после чего GLiNER молча откатывается на CPU и
# документ обрабатывается втрое дольше. Выкидываем из пути только записи с
# cudnn — остальные пути CUDA (сам рантайм, драйвер) не трогаем.
# Отключить фильтр: ANONYMIZER_KEEP_LD_PATH=1
if [ -n "${LD_LIBRARY_PATH:-}" ] && [ -z "${ANONYMIZER_KEEP_LD_PATH:-}" ]; then
    _clean_ld=$(printf '%s' "$LD_LIBRARY_PATH" | tr ':' '\n' | grep -v -i 'cudnn' | paste -sd: -)
    if [ "$_clean_ld" != "$LD_LIBRARY_PATH" ]; then
        echo "[run.sh] убрал cudnn из LD_LIBRARY_PATH (torch использует свой)" >&2
        export LD_LIBRARY_PATH="$_clean_ld"
    fi
fi

# Абсолютный путь к каталогу скрипта — работает из любого cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python "$HERE/server.py" "$@"
