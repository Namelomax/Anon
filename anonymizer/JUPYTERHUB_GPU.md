# Запуск на JupyterHub с GPU (RTX 3090 + 3070)

Чтобы получить «< 1 минуты на 50 страниц», **весь пайплайн должен выполняться на
сервере с GPU** — иначе GLiNER остаётся на CPU (~56 с) и упирается в него. Удалённо
(из локального ПК через proxy-URL) ускоряется только LLM, а GLiNER и сетевые
задержки тянут время вверх. Поэтому: код + GLiNER (CUDA) + Ollama — всё на хабе.

Найдено при проверке твоего хаба:
- Ollama (OpenAI-совместимый) локально: `http://127.0.0.1:11433/v1`.
- Reasoning отключается параметром **`reasoning_effort=none`** (флаг `--llm-no-think`).

> **Актуальная конфигурация (июль 2026): одна RTX 3080, 10 ГБ.** Двух карт больше
> нет, поэтому `CUDA_VISIBLE_DEVICES=1` в примерах ниже неактуален — GLiNER и
> Ollama делят одну карту. Из-за этого критично не раздувать контекст модели,
> см. §3.1. Замер по слоям на этой машине: GLiNER ~0.4 с на договор (не узкое
> место ни на CPU, ни на GPU), всё остальное время — вызовы LLM.

> **Модель: `qwen3.5:9b` (август 2026).** Раньше стояла `gemma4:12b` — в Q4 это
> 6.86 ГБ весов, и на 10-гигабайтной карте, где ещё сидит GLiNER, она грузилась
> лишь частично: в логе `load_tensors: offloaded 34/49 layers to GPU`,
> `CPU_Mapped model buffer size = 2754.97 MiB`. Последствия измеримые: генерация
> 18 ток/с вместо ожидаемых 25+, а prompt eval при промахе кэша проваливался с
> ~1000 до 27 ток/с, из-за чего отдельные вызовы занимали 22 секунды вместо
> двух. 9B помещается в карту целиком.
>
> **Как проверить, что модель влезла:** в логе загрузки строка
> `load_tensors: offloaded N/M layers to GPU` — N должно равняться M, а строки
> `CPU_Mapped model buffer size` быть не должно вовсе. Если слои всё ещё уходят
> на CPU — уменьшай `OLLAMA_CONTEXT_LENGTH` (§3.1) или сними GLiNER с карты:
> `server.py --device cpu` (он считает ~0.4 с и на CPU, зато освобождает ~1 ГБ).

### Смена модели одной переменной

Модель и эндпоинт берутся из окружения, так что менять их в двух местах
(детекция и review) больше не нужно — иначе легко забыть `--review-model` и
получить в VRAM сразу две модели, что на этой карте гарантирует выгрузку слоёв
на CPU:

```bash
export ANONYMIZER_LLM_MODEL=qwen3.5:9b
export ANONYMIZER_LLM_BASE_URL=http://127.0.0.1:11433/v1
```

Имя модели должно совпадать РОВНО с тем, что отдаёт сервер:
`curl -s http://127.0.0.1:11433/v1/models | python -m json.tool | grep '"id"'`.

## 1. Загрузить код на хаб

Загрузи папку `anonymizer/` (и при желании `pyproject.toml`) через интерфейс
JupyterHub, либо `git clone`, если есть репозиторий.

## 2. Установить зависимости (в терминале хаба)

```bash
pip install torch gliner natasha python-docx
python -c "import torch; print('CUDA:', torch.cuda.is_available())"   # должно быть True
```
Если `CUDA: False` — поставить сборку под нужную CUDA, напр.:
`pip install torch --index-url https://download.pytorch.org/whl/cu124`

## 3. Поднять Ollama (твой скрипт)

```bash
bash ~/start-ollama.sh
curl -s http://127.0.0.1:11433/v1/models   # увидеть gemma4:12b
```

### 3.1. Размер контекста — от него зависит, влезет ли модель в GPU

**Самая частая причина «почему так медленно».** Если контекст выставлен с запасом
(по умолчанию бывает 32768 и больше), KV-кэш съедает столько VRAM, что модель
перестаёт помещаться целиком, и llama.cpp молча выгружает часть слоёв на
процессор. В логе загрузки это видно так:

```
load_tensors: offloaded 26/34 layers to GPU     <- 8 слоёв ушли на CPU
```

После этого генерация падает примерно до 23 ток/с, и обработка одного договора
занимает минуты. Ошибки при этом нет — всё «работает», просто втрое дольше.

Считается объём KV-кэша по параметрам из того же лога:

```
на токен = n_layer × (n_embd_k_gqa + n_embd_v_gqa) × 2 байта
пример: 32 × (1024 + 1024) × 2 = 128 КиБ на токен
32768 токенов × 128 КиБ = 4 ГиБ только под кэш
```

При модели ~5.4 ГиБ и карте на 10 ГиБ (RTX 3080, которая ещё тянет дисплей)
4 ГиБ кэша — это ровно та капля, из-за которой слои уезжают на CPU.

**Рабочее значение — 16384.** Нижняя граница считается так: самый длинный промпт
конвейера ~3900 токенов плюс `max_tokens = 8000` на ответ ≈ 11 900 токенов.
При 8192 длинный ответ слоя review обрезался бы на середине, поэтому 16384 —
минимум с запасом, а не просто «покрасивее».

```bash
export OLLAMA_CONTEXT_LENGTH=16384   # KV-кэш ~2 ГиБ вместо 4 ГиБ
export OLLAMA_MAX_LOADED_MODELS=1    # чтобы две модели не висели в VRAM разом
# export OLLAMA_KV_CACHE_TYPE=q8_0   # ещё вдвое режет кэш, если не хватит
```

Проверить, что помогло — в логе загрузки модели должно стать
`offloaded 34/34 layers to GPU`, а `tg` в `print_timing` заметно вырасти
относительно 23 ток/с.

## 4. Обезличить документ (одна команда)

```bash
CUDA_VISIBLE_DEVICES=1 \
python anonymizer/anonymize_document.py /path/to/doc.docx \
  --gliner --device cuda --corporate \
  --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model gemma4:12b --llm-no-think \
  --out-dir out
```
- `CUDA_VISIBLE_DEVICES=1` → GLiNER на свободную 3070 (Ollama держит модель на 3090).
- Локальный Ollama (`11433`) — без proxy и без ключа (ключ нужен только снаружи).
- На выходе: `out/doc.anon.docx`, `out/doc.map.json`, `out/doc.anon.txt`.

## 5. (Опционально) Чистый замер по слоям

```bash
# извлечь текст
python -c "from anonymizer.documents import read_text; open('doc.txt','w',encoding='utf-8').write(read_text('/path/to/doc.docx'))"

# только GLiNER на GPU (без LLM)
time CUDA_VISIBLE_DEVICES=1 python anonymizer/worker.py --in doc.txt --out g.json \
  --ner gliner --device cuda --corporate

# полный пайплайн на GPU
time CUDA_VISIBLE_DEVICES=1 python anonymizer/worker.py --in doc.txt --out f.json \
  --ner gliner --device cuda --corporate \
  --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model gemma4:12b --llm-no-think
```

Ожидаемо на этом железе для 50 страниц: GLiNER на 3070 ~5–10 с, LLM на 3090 (без
сетевого прокси) заметно быстрее локального → суммарно **под минуту**.

## Режим «тонкий клиент»: бэкенд на хабе, UI/бенчмарк локально

Это для демо заказчику: весь пайплайн (GLiNER на CUDA + LLM) крутится на GPU-сервере,
а на ноутбуке — только интерфейс, который к нему подключается.

### Шаг 1. Поднять бэкенд НА ХАБЕ

```bash
CUDA_VISIBLE_DEVICES=1 python anonymizer/server.py --port 8000 \
  --device cuda --corporate \
  --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model gemma4:12b --llm-no-think
```
Сервис поднимется на `127.0.0.1:8000`; снаружи он доступен через JupyterHub-proxy:
`https://jh.interfonica.cloud/user/<id>/proxy/8000` (с Bearer-токеном, как у Ollama).
Проверка: `GET …/proxy/8000/health` → `{"status":"ok", ...}`.

### Шаг 2а. Бенчмарк ЛОКАЛЬНО через удалённый бэкенд

```bash
python anonymizer/eval_benchmark.py --csv pii_benchmark/test.csv \
  --remote-url "https://jh.interfonica.cloud/user/<id>/proxy/8000" \
  --remote-key "<OLLAMA_API_KEY>" --sample 200 --seed 42
```
GLiNER+LLM считаются на сервере; локально только подсчёт метрик.

### Шаг 2б. UI ЛОКАЛЬНО через удалённый бэкенд

```bash
streamlit run anonymizer/app.py
```
В сайдбаре включить **«Удалённый бэкенд (GPU-сервер)»**, вставить URL
(`…/proxy/8000`) и токен. Документы обрабатываются на сервере, локально — только
показ результата и сборка `.docx`/ZIP.

## Доступ к LLM СНАРУЖИ (если нужно из локального ПК)

Из локальной машины тот же Ollama доступен через JupyterHub-proxy с Bearer-ключом:
```
base_url = https://jh.interfonica.cloud/user/<...>/proxy/11434/v1
header   = Authorization: Bearer <OLLAMA_API_KEY>
```
В нашем CLI: `--llm-base-url <это> --llm-api-key <KEY> --llm-no-think`.
Но GLiNER при этом останется на локальном CPU (≈56 с) + добавятся сетевые задержки —
для «< 1 мин» этот режим не подходит, только полный запуск на хабе (п. 4).
