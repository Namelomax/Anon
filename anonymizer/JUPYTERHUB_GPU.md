# Запуск на JupyterHub с GPU (RTX 3090 + 3070)

Чтобы получить «< 1 минуты на 50 страниц», **весь пайплайн должен выполняться на
сервере с GPU** — иначе GLiNER остаётся на CPU (~56 с) и упирается в него. Удалённо
(из локального ПК через proxy-URL) ускоряется только LLM, а GLiNER и сетевые
задержки тянут время вверх. Поэтому: код + GLiNER (CUDA) + Ollama — всё на хабе.

Найдено при проверке твоего хаба:
- Ollama (OpenAI-совместимый) локально: `http://127.0.0.1:11433/v1`, модель **`qwen3.5:9b`**.
- На 3090 скорость ~**70 ток/с** (≈×2 от локального AMD). Reasoning отключается
  параметром **`reasoning_effort=none`** (флаг `--llm-no-think`).
- 3090 занята моделью Ollama (~15 ГБ), 3070 свободна → **GLiNER ставим на 3070**,
  Ollama на 3090 — слои считаются параллельно.

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
curl -s http://127.0.0.1:11433/v1/models   # увидеть qwen3.5:9b
```

## 4. Обезличить документ (одна команда)

```bash
CUDA_VISIBLE_DEVICES=1 \
python anonymizer/anonymize_document.py /path/to/doc.docx \
  --gliner --device cuda --corporate \
  --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model qwen3.5:9b --llm-no-think \
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
  --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model qwen3.5:9b --llm-no-think
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
  --llm --llm-base-url http://127.0.0.1:11433/v1 --llm-model qwen3.5:9b --llm-no-think
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
