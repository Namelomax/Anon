# anonymizer — обратимая анонимизация PII (русский язык)

Ядро пайплайна обезличивания: находит чувствительные данные, заменяет их на
уникальные плейсхолдеры (`[LABEL_N]`), сохраняет JSON-маппинг и умеет
**восстанавливать** исходный текст обратно по маппингу без ИИ.

## Быстрый старт

```python
from anonymizer import anonymize, deanonymize, save_mapping, load_mapping

text = "ИНН 7711222333, тел. +7 (812) 987 6543, почта ivan@example.ru"

res = anonymize(text)
print(res.anonymized_text)   # ИНН [INN_1], тел. [PHONE_1], почта [EMAIL_1]
print(res.mapping)           # {"[INN_1]": "7711222333", ...}
print(res.summary)           # {"INN": 1, "PHONE": 1, "EMAIL": 1}

save_mapping(res.mapping, "doc.map.json")

# ...позже, восстановление (детерминированно, без модели):
mapping = load_mapping("doc.map.json")
original = deanonymize(res.anonymized_text, mapping)
assert original == text
```

## Архитектура

```
text ──▶ detectors ──▶ resolve_overlaps ──▶ assign_placeholders ──▶ anonymized text + mapping
                                                                              │
                                                            deanonymize(text, mapping) ──▶ original
```

- `spans.py` — тип `Span` и разрешение пересечений по приоритету/длине.
- `detectors.py` — регэксп-детекторы структурированных данных (документы, контакты)
  и протокол `Detector`. NER-детектор для ФИО/адресов подключается сюда же.
- `mapping.py` — присвоение уникальных плейсхолдеров (одинаковые значения →
  один плейсхолдер), загрузка/сохранение JSON.
- `engine.py` — `Anonymizer` / `anonymize()`.
- `deanonymize.py` — обратная подстановка одним проходом (без коллизий
  `[X_1]` / `[X_10]`).

## Что детектируется сейчас (регэкспы)

Контакты: `EMAIL`, `URL`, `IP_ADDRESS`, `PHONE`, `CREDIT_CARD`.
Документы РФ (по ключевому слову + номер): `INN`, `SNILS`, `OMS`, `PASSPORT`,
`DRIVER_LICENSE`, `MILITARY_ID`, `BIRTH_CERTIFICATE`.

ФИО и адреса (`FIRST_NAME`, `CITY`, `STREET`…) пока **не** детектируются — это
следующая фаза (NER-детектор).

## Подключение нового детектора

Любой объект с методом `find(text) -> list[Span]` совместим с движком:

```python
from anonymizer.engine import Anonymizer
from anonymizer.detectors import DEFAULT_DETECTORS

class MyNER:
    def find(self, text): ...  # вернуть list[Span] с метками FIRST_NAME и т.п.

anon = Anonymizer([*DEFAULT_DETECTORS, MyNER()])
```

## Гибрид с NER (ФИО / адреса)

NER подключается отдельно (Natasha грузится тяжело — только по запросу):

```python
from anonymizer import build_anonymizer
anon = build_anonymizer(use_ner=True)        # регэкспы + Natasha
res = anon.anonymize("Ефимов Данил из Казани, ИНН 7711222333")
# Ефимов Данил → [PERSON_1], Казани → [LOCATION_1], 7711222333 → [INN_1]
```

`anonymize()` (модульная функция) — только регэкспы, без загрузки модели.

## Слой LLM (добивание пропусков, локально)

Третий слой детекции поверх регэкспов и NER. Ловит то, что первые два не могут:
числа прописью («СНИЛС семь восемь девять…»), ID без подсказывающих слов, ФИО
латиницей/в необычном порядке, улицы и дома. Всё **локально** — данные не уходят.

```python
from anonymizer import build_anonymizer
from anonymizer.llm import LLMConfig

anon = build_anonymizer(
    use_ner=True,
    use_llm=True,
    llm_config=LLMConfig(base_url="http://127.0.0.1:1234/v1", model="gemma4:12b"),
)
res = anon.anonymize("Reginald Blackwell, СНИЛС семь восемь девять - два три семь.")
# оба попадут в маппинг и обезличатся
```

**Контракт безопасности:** LLM только *находит* подстроки; офсеты, плейсхолдеры и
маппинг делает код. Что нельзя локализовать дословно в тексте (галлюцинация) —
отбрасывается. Обратимость остаётся детерминированной.

### Скорость и «thinking»

Qwen3.5 — рассуждающая модель: на коротком тексте уходит ~2,5 мин (reasoning).
Модуль это переживает (парсит ответ и из `reasoning_content`), но для практики
рассуждения стоит отключить:

- **Ollama** (будущее использование): `LLMConfig(base_url="http://127.0.0.1:11434/v1",
  extra_body={"think": False})`.
- **LM Studio**: отключить reasoning в настройках модели или взять non-reasoning
  модель (instruct без thinking). Параметры API (`/no_think`,
  `chat_template_kwargs`) в текущей сборке не отключают рассуждения.
- Слой лучше включать как «режим максимального покрытия», а не на каждый документ.

`extra_body` уходит как есть в тело запроса — туда же любые серверные флаги.

## Веб-интерфейс (Streamlit)

```bash
pip install streamlit
streamlit run anonymizer/app.py
```

Две вкладки:
- **Анонимизация** — загрузка `.docx`/`.txt` (или вставка текста), выбор движка
  (GLiNER/Natasha/нет), галки «корпоративные данные» и «LLM-слой»; на выходе —
  обезличенный текст, таблица маппинга и кнопки скачивания (`.txt`, `.json`,
  обезличенный `.docx`).
- **Деанонимизация** — загрузка обезличенного текста + маппинга `.json` →
  восстановление исходного текста (без ИИ).

Настройки движка — в левом сайдбаре. LLM требует запущенного LM Studio / Ollama.

## Анонимизация документа из консоли

```bash
python anonymizer/anonymize_document.py doc.docx --gliner --corporate [--llm]
```

## Тесты

```bash
python anonymizer/tests/test_engine.py      # ядро (без зависимостей)
python anonymizer/tests/test_ner.py         # NER (пропустится без natasha)
python anonymizer/tests/test_llm.py         # LLM-парсер/локатор (без сервера)
```

## Оценка на бенчмарке

```bash
python anonymizer/eval_benchmark.py                 # полный прогон (~2 мин)
python anonymizer/eval_benchmark.py --limit 300     # быстрая проверка
python anonymizer/eval_benchmark.py --no-ner        # только регэкспы
```

Методология как у redmadrobot: span-level overlap matching, слияние смежных
спанов одной категории, грубые категории.

**Текущий результат (все 2841 строки):**

| Срез | P | R | F1 |
|------|---|---|----|
| **PERSON+LOCATION** (общий лидерборд) | 0.863 | 0.610 | **0.715** |
| ALL (micro) | 0.887 | 0.622 | **0.731** |

PERSON+LOCATION ≈ уровень лучшей модели лидерборда (GLiNER 0.731) и упирается в
потолок Natasha. По документам после тюнинга разделителей: EMAIL 0.98, INN 0.90,
OMS 0.84, SNILS 0.78, PASSPORT 0.76, MILITARY 0.72.

### Разбор ошибок

```bash
python anonymizer/eval_benchmark.py --no-ner --errors SNILS,DRIVER_LICENSE --show 15
```

Показывает пропущенные (FN) и лишние (FP) спаны с контекстом по категориям.

**Почему не 100% (принципиальный потолок):** числа прописью («СНИЛС семь восемь
девять…»), безконтекстные голые числа (неотличимы от любых других), шум разметки
(часть «лишних» срабатываний фактически корректна), и статистическая природа NER
(латиница, нестандартный порядок ФИО). Для анонимизации это смягчается тем, что
важна полнота (пропуск = утечка), а не точность метки.
