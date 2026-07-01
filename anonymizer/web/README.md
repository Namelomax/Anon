# Анонимизатор — веб-интерфейс (Next.js, для Vercel)

Тестовая версия для заказчика: загружаешь документ (`.docx` / `.txt`) →
получаешь обезличенную версию + `mapping` (ключ восстановления). ZIP, отдельный
документ, отдельный JSON и превью прямо на странице.

## Архитектура

```
Браузер ──> Next.js (Vercel)         ──>  Python backend (server.py)
            /api/anonymize  (прокси)       /anonymize-file
            + Bearer-токен (server-side)   regex + GLiNER + LLM
```

Vercel не может запускать GLiNER/PyTorch и не видит твой `127.0.0.1`. Поэтому
фронт — тонкий: вся обработка идёт на Python-бэкенде (`../server.py`). URL
бэкенда задаётся переменной окружения, токен подставляется на сервере и в
браузер не попадает.

| Сценарий | `ANONYMIZER_BACKEND_URL` | Где запущен фронт |
|---|---|---|
| Локальный тест | `http://127.0.0.1:8000` | локально (`npm run dev`) |
| Демо заказчику | URL JupyterHub-прокси | Vercel |

> Деплой на Vercel **с локальным бэкендом не работает напрямую** — облако не
> видит твой компьютер. Для локальных тестов запускай фронт локально
> (`npm run dev`). Деплой на Vercel указывай на публичный бэкенд (Jupyter).

## 1. Локальный тест (LM Studio + GLiNER)

**Шаг 1. LM Studio** — загрузи `qwen/qwen3.5-9b`, включи сервер на
`http://127.0.0.1:1234`.

**Шаг 2. Python-бэкенд** (из каталога `privacy-filter-main`, т.е. на уровень
выше `anonymizer/`):

```bash
python anonymizer/server.py --port 8000 \
  --ner gliner --device cuda \
  --corporate \
  --llm --llm-base-url http://127.0.0.1:1234/v1 \
  --llm-model qwen/qwen3.5-9b --llm-no-think
```

- нет GPU → `--device cpu`;
- проверить без LLM/GLiNER (только regex) → убери `--llm` и поставь `--ner none`;
- здоровье: `curl http://127.0.0.1:8000/health`.

**Шаг 3. Фронт:**

```bash
cd anonymizer/web
cp .env.local.example .env.local   # ANONYMIZER_BACKEND_URL=http://127.0.0.1:8000
npm install
npm run dev                        # http://localhost:3000
```

## 2. Деплой на Vercel

1. В Vercel создай проект, **Root Directory = `anonymizer/web`**.
2. Environment Variables:
   - `ANONYMIZER_BACKEND_URL` = URL твоего Jupyter-бэкенда (например
     `https://<hub>/user/<id>/proxy/8000`);
   - `ANONYMIZER_BACKEND_KEY` = Bearer-токен (если бэкенд за JupyterHub-прокси).
3. Deploy. Framework Next.js определится автоматически.

Бэкенд на Jupyter поднимается тем же `server.py` (см. `../JUPYTERHUB_GPU.md`),
LLM указывает на локальный Ollama/LM Studio на той машине.

## Вкладки

- **Анонимизация** — загрузка документа, выбор этапов, превью + скачивание
  (документ / `mapping.json` / ZIP).
- **Деанонимизация** — восстановление по `mapping` (без ИИ). По умолчанию берёт
  **последний** обезличенный документ и его `mapping` (галочка «Использовать
  последний документ»). Сними галочку — можно загрузить свой обезличенный файл
  (`.docx`/`.txt`) и свой `mapping.json`.

## API бэкенда

`POST /anonymize-file` (JSON):

```json
{ "filename": "doc.docx", "file_base64": "...", "regex": true, "ner": true, "llm": true }
```

Ответ: `anonymized_text`, `mapping`, `summary`, `spans`, `document_base64`,
`document_name`, `document_mime`. Флаги этапов опциональны (по умолчанию — как
запущен сервер).

`POST /deanonymize-file` (JSON):

```json
{ "filename": "doc.anon.docx", "file_base64": "...", "mapping": { "[PERSON_1]": "Иванов И.И." } }
```

Ответ: `restored_text`, `leftover` (плейсхолдеры без значения), `document_base64`,
`document_name`, `document_mime`. Восстановление детерминированное, без ИИ.

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `ANONYMIZER_BACKEND_URL` | URL Python-бэкенда (без хвостового `/`) |
| `ANONYMIZER_BACKEND_KEY` | Bearer-токен бэкенда (опц., только на сервере) |
