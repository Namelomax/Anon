# Развёртывание бэкенда на 130.17.10.40 (режим «только API»)

Вычислений на хосте не остаётся: GLiNER и LLM живут на `oui.interfonica.cloud`,
сервер нужен ради публичного адреса и HTTPS. Ни torch, ни GPU, ни весов моделей.

Схема: **браузер → Vercel → этот сервер → oui.interfonica.cloud**.
Devtunnel и его обрыв на 101 секунде из схемы уходят совсем.

## Что учтено про этот конкретный хост

| факт | следствие |
|---|---|
| порт 8000 занят чужим `uvicorn main:app` | наш слушает **8010** |
| свободно 1.8 ГиБ памяти из 3.8 | без torch процесс ~150 МБ вместо ~2 ГБ |
| свободно 12 ГБ диска из 40 | зависимости ~30 МБ вместо ~2.5 ГБ |
| есть пользователи `deploy`, `root` и другие | ключ только в `/etc/anonymizer.env` (600), не в аргументах — иначе виден в `ps aux` |
| Python 3.12 | подходит, коду нужен 3.10+ |

## Что уже есть на хосте (проверено снаружи)

```
порт  80: nginx/1.24.0 (Ubuntu), на голый IP отвечает 404
порт 443: сертификат Let's Encrypt, CN=arena.cpcore.ru
порт 8000: открыт наружу (чужой uvicorn)
порт 5678: открыт наружу (порт n8n по умолчанию)
```

Домен, значит, **cpcore.ru**. nginx разводит сайты по `server_name`, поэтому
`arena.cpcore.ru` и n8n нашему блоку не мешают. Сертификат для arena выпущен
Let's Encrypt — то есть certbot на хосте почти наверняка уже стоит.

Ниже примером взят поддомен `anon.cpcore.ru` — подставьте свой.

## Шаг 0. Сначала запушить код (иначе клонировать нечего)

На сервере код берётся из `https://github.com/Namelomax/Anon.git`, а режима
`--ner remote` в закоммиченной версии **нет**: `gliner_remote.py` не добавлен в
git, правки `server.py` и `llm.py` не зафиксированы. Без этого шага сервер
склонирует старую версию, которая полезет за моделями на локальную машину.

Запушить нужно: `anonymizer/gliner_remote.py`, `anonymizer/server.py`,
`anonymizer/llm.py`, папку `deploy/` и правки в `anonymizer/web/` (последние —
для Vercel).

## Шаги

### 1. Поддомен

A-запись `anon.cpcore.ru` → `130.17.10.40`. Проверить, что разошлось:

```bash
dig +short anon.cpcore.ru      # должно вернуть 130.17.10.40
```

Certbot не выпустит сертификат, пока запись не видна снаружи.

### 2. Код и окружение

```bash
cd ~
git clone https://github.com/Namelomax/Anon.git privacy-filter
cd privacy-filter
python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r deploy/requirements-api.txt
```

### 3. Ключ

```bash
sudo install -m 600 -o root -g root deploy/anonymizer.env.example /etc/anonymizer.env
sudo nano /etc/anonymizer.env     # вписать НОВЫЙ ключ
```

Старый ключ (`879808fe…`) утёк в переписку — на сервер класть уже нечего,
нужен свежий у Ильи.

### 4. Сервис

```bash
sudo cp deploy/anonymizer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now anonymizer
curl -s localhost:8010/health | head -c 200
```

Ожидается `{"status": "ok", "ner": "remote", ...}`.

### 5. nginx и сертификат

```bash
sudo cp deploy/nginx-anonymizer.conf /etc/nginx/sites-available/anonymizer
sudo sed -i 's/ПОДДОМЕН/anon.cpcore.ru/' /etc/nginx/sites-available/anonymizer
sudo ln -s /etc/nginx/sites-available/anonymizer /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d anon.cpcore.ru
```

`nginx -t` перед перезагрузкой обязателен: битый конфиг уронил бы и
`arena.cpcore.ru`, и всё остальное на этом nginx.

Порт 8010 наружу **не открывать** — снаружи виден только nginx на 80/443.
Firewall на хосте пропускает всё (8000 и 5678 торчат в интернет), так что
защита здесь ровно одна: сервис слушает `127.0.0.1`.

### 6. Vercel

Переменные окружения проекта (Production, не только Preview!), затем передеплой:

```
ANONYMIZER_BACKEND_URL = https://anon.cpcore.ru
ANONYMIZER_BACKEND_KEY = (пусто)
```

Правка переменной без передеплоя ничего не даёт — существующий деплой держит
старое значение.

## Проверка

```bash
curl -s https://anon.cpcore.ru/health

# полный путь: постановка задачи и опрос
JOB=$(curl -s -X POST https://anon.cpcore.ru/jobs/anonymize-file \
  -H 'Content-Type: application/json' \
  -d "{\"filename\":\"t.txt\",\"file_base64\":\"$(printf 'Иванов Иван, ИНН 7736050003' | base64 -w0)\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

curl -s https://anon.cpcore.ru/jobs/$JOB
```

`/health` должен показать `"ner": "remote"` — если там `gliner`, значит сервис
поднялся не с тем флагом и попытается грузить torch.

Submit обязан вернуться меньше чем за секунду.

## Чего этот сервер НЕ решает

Лимит Vercel в **300 секунд** остаётся: роут опрашивает задачу на своей стороне
и не может ждать дольше. По замеру 13.4 мс на символ это упирается примерно в
**20 тысяч символов** документа. Дальше нужен опрос из браузера — серверная
часть для него уже готова, меняются только `route.ts` и `page.tsx`.

Отдельный предел со стороны модели: стадия `recall` шлёт весь документ одним
промптом, а у `gemma-4-31b` окно 64 000 токенов — это около **165 тысяч
символов**. При переполнении vLLM вернёт 400, то есть откажет громко, а не
молча обрежет.
