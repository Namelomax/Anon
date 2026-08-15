"""LLM review layer: a final sanity pass over the detected spans.

This is the FOURTH and last layer, run after regex + NER + LLM detection have
produced a candidate span list (and after declension propagation), right
before placeholders are assigned. Every earlier layer is recall-oriented
("when in doubt, mask it"), which is exactly why obvious false positives slip
through — a common word capitalized at the start of a sentence ("День"), a
product/technology name (GPT, Telegram, Битрикс), a legal abbreviation (ФЗ,
НДА), or a speech-to-text artifact all get masked just like real PII.

This layer asks an LLM to look at each *distinct* detected value together with
a snippet of its surrounding context and asks it to report EXCEPTIONS ONLY —
candidates that need one of three actions. Anything not mentioned in the
model's answer is left masked exactly as detected; silence means "keep this
span masked". The three actions:

* ``keep=false`` — drop it: not PII, revert to plain text.
* ``trim`` — the candidate is PII only in *part* (a rank/title/honorific is
  stuck to a real name, e.g. "Капитан Яков" — only "Яков" is PII); the span
  shrinks to just that substring, the rest reverts to plain text.
* ``merge_with`` — this candidate and another one in the same batch are the
  *same real-world entity* under different wording (e.g. "Капитан Яков" and
  "Вайгус" turn out, by context, to be the same person's surname and
  callsign) — both get the same placeholder in the output instead of two.

Design contract, mirroring ``llm.py``:

* The reviewer can only DROP or SHRINK spans, or MERGE two of them under one
  placeholder — it never adds masking, invents entities, or rewrites text.
* Only "soft" labels prone to false positives are reviewed (PERSON, ORG,
  LOCATION and friends). Format-driven identifiers (EMAIL, PHONE, INN,
  SNILS, PASSPORT, CREDIT_CARD...) are never sent to the model and always
  stay masked — a regex match on those is essentially never wrong, and an
  LLM should not be given a chance to talk itself into unmasking real PII.
* Fail-safe: if the LLM is unreachable, times out, returns something that
  cannot be parsed, or simply omits a candidate, every affected span is kept
  masked, untrimmed and unmerged (unchanged behaviour) — every candidate
  defaults to "keep masked" and only an entry that survives the echo-text
  check below can change that.
* Every returned entry must echo the candidate's exact text back (``text``
  field) so its ``id`` can be verified against what was actually shown at
  that position; an entry whose echoed text doesn't match the candidate
  sitting at that id is ignored (span stays masked) and logged to stderr.
  This is the only positional safety net left once "one verdict per
  candidate" is gone — a short reply is now the *normal* case, not evidence
  of drift, so there is no separate check on how many entries came back.
* Repeat occurrences of the same value (same label, whitespace-collapsed,
  case-folded) are judged once, as a group, using the first occurrence's
  context — mirrors how ``mapping.assign_placeholders`` groups identical
  entities under one placeholder.
* ``merge_with`` is a batch-local id: the model can only merge candidates it
  was actually shown together in the same request. ``batch_size`` defaults
  high enough to fit a typical meeting transcript's reviewable entities in
  one call; if a document has more, merges across batch boundaries are
  simply not attempted (fails safe to "two separate placeholders").
* PERSON ``keep=false`` is additionally vetoed by a DETERMINISTIC morphology
  check (``detectors.can_unmask_person``) before it is applied — the prompt
  already tells the model that dropping PERSON is only for ordinary Russian
  words, but it does not reliably obey that (rare/unfamiliar surnames like
  "Вайгус" get dropped as if they were common words). The model's verdict is
  only TRUSTED once a Russian morphological dictionary confirms the value is
  actually an ordinary word (or, failing that, structurally impossible as a
  name — see the detector for the two-path rule); otherwise the drop is
  refused and the span stays masked, logged to stderr either way.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
from dataclasses import dataclass, field, replace

from . import http_pool, usage_log
from .detectors import can_unmask_person  # deterministic PERSON-unmask veto
from .llm import _extract_json_array  # reuse the same tolerant JSON-array scanner
from .spans import Span

# Only these labels are second-guessed. Structured/format-driven identifiers
# are deliberately excluded — see module docstring. ADMIN_CODE is the one
# exception to "format detectors are always trusted": unlike an INN/SNILS/
# passport number, a bare institutional code's format alone doesn't tell you
# whether it's a real identifying code (an org's internal structure/accounting
# code) or a harmless number the keyword-anchored detector over-caught — that
# needs the surrounding context, which is exactly what this layer supplies.
_REVIEWABLE_LABELS = frozenset({
    "PERSON", "ORG", "LOCATION", "CITY", "REGION", "COUNTRY",
    "DISTRICT", "STREET", "HOUSE", "ADDRESS",
    "FIRST_NAME", "LAST_NAME", "MIDDLE_NAME",
    "ADMIN_CODE",
    # SUBJECT (предмет договора) тоже вероятностный — его выдаёт LLM по смыслу,
    # а не по формату, поэтому ложные срабатывания («товар», «оборудование»)
    # некому было снимать. Ревьюится ТОЛЬКО в subject-режиме, где промпт знает
    # критерий «отраслевая номенклатура vs служебная лексика договора»; вне
    # этого режима метка не появляется вовсе (см. _build_review_prompt).
    "SUBJECT",
})

# Вероятностные источники спанов — ТОЛЬКО их имеет смысл пересматривать LLM-ревью
# (там бывают ложные срабатывания). Детерминированные источники (regex, alias,
# morph, glossary) высокоточны и на пересмотр не отдаются — см. _group_candidates.
_NER_LLM_SOURCES = frozenset({"gliner", "ner", "llm", "llm2", "llm-recall"})

def _build_review_prompt(subject: bool = False) -> str:
    """Промпт ревьюера. ``subject`` — включён ли режим предмета договора.

    В обычном режиме ревьюеру РАЗРЕШЕНО снимать маску с «названий продуктов»
    (GPT, Telegram, Битрикс) — это давняя и полезная эвристика против ложных
    ORG/PERSON. Но при включённой стадии `subject` наименование товара — ровно
    то, что заказчик просит СКРЫТЬ, а предмет договора сплошь и рядом приезжает
    из детектора под меткой ORG («Ритон», «МАР RD-5547-CE», «Siemens Somatom
    go.Now»). Без этой развилки два слоя работали друг против друга: детекция
    маскировала номенклатуру, а ревью её тут же раскрывало как «продукт».
    Поэтому в subject-режиме разрешение сужается до программ и ИТ-сервисов, а
    для самой метки SUBJECT даётся отдельный критерий.
    """
    product_rule = (
        "название программы или ИТ-сервиса (GPT, Telegram, Zoom, Битрикс, 1С)"
        if subject else
        "название программы/продукта (GPT, Telegram, Zoom, Битрикс, 1С)"
    )
    goods_guard = (
        "ВАЖНО ПРО НАЗВАНИЯ-БРЕНДЫ. В этом документе намеренно скрывается "
        "предмет договора, поэтому решай по РОЛИ значения в тексте, а не по "
        "тому, похоже ли оно на торговую марку:\n"
        "  keep=TRUE — если это вещь, которую по договору поставляют, продают, "
        "передают, монтируют или перечисляют в предмете/спецификации/"
        "комплектации: марка, модель, изделие, оборудование, материал "
        "(«сигнализация: Ритон», «GPS-навигатор МАР RD-5547-CE», «автомат "
        "Калашникова АК-74М», «томограф Somatom go.Now»). Такие значения "
        "keep=true ДАЖЕ ЕСЛИ они помечены типом ORG и выглядят как бренд: "
        "по ним восстанавливается отрасль заказчика.\n"
        "  keep=false — если это лишь ИНСТРУМЕНТ или сервис, которым стороны "
        "пользуются в работе и который не является предметом сделки "
        "(«учёт ведётся в Битрикс», «созвон в Zoom», «1С», «Telegram»).\n"
        if subject else ""
    )
    subject_rule = (
        "Тип SUBJECT — предмет договора. keep=true, если значение указывает на "
        "КОНКРЕТНУЮ номенклатуру: марка, модель, тип изделия, обозначение "
        "(«автомат Калашникова АК-74М», «станок ЧПУ Haas VF-2»), либо родовое "
        "слово с явной отраслевой спецификой («вооружение», «боеприпасы», "
        "«медицинское оборудование»). keep=false — только если детектор "
        "захватил служебную лексику договора: «товар», «продукция», «услуги», "
        "«работы», «поставка», «оборудование» без уточняющего определения, "
        "количество или единицу измерения.\n"
        if subject else ""
    )
    # str.format здесь применять НЕЛЬЗЯ: в шаблоне есть литеральный JSON
    # ({"id": …, "keep": …}), на котором format падает с KeyError.
    return (
        _REVIEW_PROMPT_TEMPLATE
        .replace("<<PRODUCT_RULE>>", product_rule)
        .replace("<<PERSON_PRODUCT>>", "ПО" if subject else "продуктов/ПО")
        .replace("<<ORG_PRODUCT>>", "ПО" if subject else "продукт/ПО")
        .replace("<<GOODS_GUARD>>", goods_guard)
        .replace("<<SUBJECT_RULE>>", subject_rule)
    )


_REVIEW_PROMPT_TEMPLATE = (
    "Ты — контролёр качества анонимизации персональных данных (ПДн) в русском "
    "тексте. Тебе присылают пронумерованный список кандидатов, которые детектор "
    "пометил как ПДн (ФИО, организация, локация и т.п.). Для каждого дано: id, "
    "тип, само значение и фрагмент окружающего текста, где значение выделено "
    "квадратными скобками: [вот_так]. Если значение встречается в документе "
    "несколько раз, тебе показаны НЕСКОЛЬКО таких фрагментов подряд, разделённых "
    "« | » — это разные места документа, а не один и тот же фрагмент. Суди по "
    "ВСЕМ показанным фрагментам сразу: если хотя бы в одном из них значение явно "
    "выглядит как настоящее ПДн (реальное обращение к человеку, название "
    "организации и т.п.), считай его keep=true (оставить замаскированным), даже "
    "если другие фрагменты выглядят более обыденно или неоднозначно — "
    "двусмысленность в одном месте не отменяет явную ПДн в другом.\n"
    "ФОРМАТ ОТВЕТА — ТОЛЬКО ИСКЛЮЧЕНИЯ. Не перечисляй всех кандидатов. Верни "
    "объект ТОЛЬКО для тех кандидатов, с которыми нужно что-то СДЕЛАТЬ: снять "
    "маску целиком (keep=false), скрыть только часть значения (trim) или "
    "объединить с другим кандидатом (merge_with). Кандидата, который должен "
    "остаться замаскированным БЕЗ ИЗМЕНЕНИЙ (keep=true без trim и merge_with), "
    "в ответ включать НЕ НУЖНО — ПРОСТО ПРОПУСТИ его. Отсутствие кандидата в "
    "твоём ответе означает «оставить замаскированным как есть» — это "
    "умолчание, а не ошибка, и большинство кандидатов в него попадут. Не "
    "перечисляй кандидатов «для полноты» — лишние объекты в ответе не нужны и "
    "тратят место впустую.\n"
    'Для каждого включённого кандидата верни объект {"id": <номер>, "text": '
    '<значение кандидата>, "keep": false (опционально при trim/merge_with), '
    '"trim": <опционально>, "merge_with": <опционально>}. Поле text ОБЯЗАТЕЛЬНО '
    "и должно ДОСЛОВНО повторять значение этого кандидата — по нему "
    "проверяется, что id указан верно. Если сомневаешься, нужно ли действие — "
    "НЕ включай кандидата в ответ: по умолчанию он останется замаскированным, "
    "это безопасно.\n"
    "- keep=false — это ОШИБКА детектора: обычное слово, местоимение, день "
    "недели, должность, <<PRODUCT_RULE>>, юридический термин или "
    "аббревиатура (ФЗ, НДА), обрывок "
    "слова — значение НЕ является ПДн и должно остаться в тексте как есть. "
    "Типичные ошибки в расшифровках встреч, которые нужно снимать (keep=false): "
    "слово из приветствия или вежливой фразы, помеченное как имя (в контексте "
    "«Добрый [День]» слово «День» — не имя); обозначение говорящего или дорожки "
    "(в контексте «[Спикер] 4:» слово «Спикер» — это роль, не имя; то же для "
    "«Участник», «Speaker», «Голос»); общие слова «Коллеги», «Друзья», "
    "«Ребята» как обращение к группе. Ориентируйся на КОНТЕКСТ: одно и то же "
    "слово может быть именем в одном месте и обычным словом в другом.\n"
    "ЛЮБОЕ настоящее имя, фамилия или обращение к человеку (даже краткое, "
    "уменьшительное или без фамилии — «Катя», «Рома», «Никита») — это keep=true, "
    "НИКОГДА не keep=false.\n"
    "Для типа PERSON снимать маску (keep=false) можно ТОЛЬКО с обычных слов "
    "русского языка, ошибочно помеченных как имя (день недели, приветствие, "
    "роль «Спикер»/«Участник», обращение «Коллеги»/«Друзья», местоимение, "
    "обычное существительное), либо с названий <<PERSON_PRODUCT>>. Если слово "
    "РЕДКОЕ, иностранное или незнакомое («Вайгус», «Смит», необычная фамилия "
    "или позывной) — это ПДн, keep=true ВСЕГДА: незнакомость слова — признак "
    "имени, а не ошибки. Если с этим значением здороваются, к нему обращаются "
    "или оно стоит в перечислении рядом с другим именем (капитан, "
    "представитель, участник команды) — это точно человек, keep=true.\n"
    "Настоящие ОРГАНИЗАЦИИ, компании, учреждения, вузы, институты, банки, "
    "госорганы, наименования сторон договора (в т.ч. длинные официальные, "
    "например «Федеральное государственное … университет», «ООО Ромашка», "
    "«Институт информационных технологий и анализа данных») — ВСЕГДА keep=true: "
    "это важные данные, их нужно скрывать. Для типа ORG ставь keep=false ТОЛЬКО "
    "если это слово-роль/отношение (Сторона, Стороны, Заказчик, Исполнитель, "
    "Подрядчик, студенты, сотрудники, участники), термин или аббревиатура "
    "документа (ТЗ, АКТ, МП, КОСГУ, приказ, раздел), либо <<ORG_PRODUCT>> — но "
    "НИКОГДА не настоящее название организации.\n"
    "<<GOODS_GUARD>>"
    "<<SUBJECT_RULE>>"
    "Для типа ADMIN_CODE кандидат — это число/шифр рядом со словом вроде «код "
    "структуры», «КОСГУ», «субконто», «шифр», «рег. номер», «штамп». Формат "
    "таких кодов не стандартизован, поэтому решай по смыслу: keep=true — если "
    "это внутренний идентификатор УЧРЕЖДЕНИЯ/ПОДРАЗДЕЛЕНИЯ/ДОКУМЕНТА, по "
    "которому его можно узнать или привязать к конкретной организации/записи "
    "(код структуры, код учреждения, бюджетная классификация, номер печати/"
    "штампа, субконто) — такие коды нужно скрывать, как и реквизиты. "
    "keep=false — только если детектор явно ошибся: захватил число, которое "
    "на самом деле не код (случайный счётчик, номер строки/пункта, дата, "
    "процент), т.е. само значение не выглядит как осмысленный шифр. Если не "
    "уверен — keep=true (эти коды почти всегда стоит скрывать).\n"
    "- keep=true без trim/merge_with — значение целиком ПДн, оставить как есть; "
    "ТАКИХ КАНДИДАТОВ В ОТВЕТ НЕ ВКЛЮЧАЙ (см. правило про исключения выше) — "
    "это умолчание для любого пропущенного id.\n"
    "- trim — если ПДн является лишь ЧАСТЬЮ значения (к имени приклеено "
    "звание/должность/обращение, например «Капитан Яков» — ПДн только «Яков», "
    "а «Капитан» должен вернуться в текст), укажи в trim ТОЧНУЮ подстроку "
    "значения (как она записана), которую нужно оставить скрытой.\n"
    "- merge_with — если по контексту очевидно, что этот кандидат и ДРУГОЙ "
    "кандидат из этого же списка — ОДНО И ТО ЖЕ реальное лицо или организация "
    "под разными именами (например «Капитан Яков» и «Вайгус» — фамилия и "
    "позывной одного человека), укажи id того другого кандидата в merge_with. "
    "ТИП кандидатов при этом может РАЗЛИЧАТЬСЯ: детектор нередко помечает "
    "человека как ORG по контексту (после слова «команда» — «команда … Капитан "
    "Яков») — если по смыслу это человек, смело используй trim и merge_with с "
    "кандидатом-PERSON. Учитывай, что расшифровка речи искажает имена и ставит "
    "лишние запятые: «Капитан Яков, Вайгус» — это может быть имя и фамилия "
    "ОДНОГО человека, разделённые запятой по ошибке распознавания.\n"
    "ВАЖНО: merge_with указывай у МЕНЕЕ формального/полного упоминания "
    "(позывной, короткое имя, кличка), а ссылаться нужно на id БОЛЕЕ полного/ "
    "официального упоминания (обычно то, что содержит фамилию или требует "
    "trim) — это оно станет отображаемым именем в итоговом документе. В "
    "документе оба получат один и тот же плейсхолдер.\n"
    "Если сомневаешься — НЕ включай кандидата в ответ (что эквивалентно "
    "keep=true без trim и merge_with): лучше оставить лишнее замаскированным, "
    "чем случайно раскрыть настоящие ПДн.\n"
    "Ответь ТОЛЬКО JSON-массивом объектов-исключений, без каких-либо пояснений. "
    "Если действий не требуется ни для одного кандидата — верни пустой массив []."
)

# Промпт обычного (не-subject) режима — поведение по умолчанию не изменилось.
_REVIEW_SYSTEM_PROMPT = _build_review_prompt(False)


@dataclass
class ReviewConfig:
    """Connection and batching settings for the LLM review layer.

    Attributes:
        base_url: OpenAI-compatible base (LM Studio / Ollama). Typically the
            same server used for the detection LLM layer.
        model: Model id as the server reports it.
        max_tokens: Output budget for the verdict JSON — an EXCEPTIONS-ONLY
            list of ``{id, text, keep?, trim?, merge_with?}`` objects, one per
            candidate that needs an action (dropped, trimmed or merged);
            candidates that stay masked as-is are omitted entirely, so the
            reply is normally a few hundred tokens regardless of document
            size (down from ~1626 completion tokens on a ~90-candidate
            transcript under the old one-verdict-per-candidate format). The
            budget is kept generous anyway as a ceiling, not a target.
            ВНИМАНИЕ: LM Studio отвергает запрос с HTTP 400, если
            ``prompt_tokens + max_tokens`` превышает длину загруженного
            контекста. При 16000 здесь модель должна быть загружена с
            контекстом не меньше 32k. Recall-проход эту цифру больше НЕ
            использует — у него отдельный, маленький бюджет
            ``recall_max_tokens`` (см. ниже) именно потому, что раньше он
            делил этот бюджет с ревью и переполнял контекст на каждом
            реальном документе.
        temperature: 0 for deterministic verdicts.
        timeout: Per-request seconds.
        api_key: Sent as Bearer; ignored by LM Studio/Ollama.
        extra_body: Merged into the request JSON (e.g. disable "thinking").
        context_chars: Characters of surrounding text kept on each side of a
            candidate value, to give the model enough to judge from.
        batch_size: Deprecated / ignored. The reviewer now sends the whole
            candidate list in a single request (see ``review_spans``); kept
            only so existing configs don't break.
        recall_max_tokens: Output budget for the recall pass (``_ask_recall``),
            deliberately SEPARATE from ``max_tokens`` above. Incident: recall's
            reply is a short list of missed values (a few hundred tokens at
            most, same shape as the short-numbers/adjacent sub-passes, which
            already hardcode 2000 for the same reason), yet recall used to
            share ``max_tokens`` (16000) as its OUTPUT reservation while also
            being the ONLY call site that sends the whole document as input —
            on a real transcript that produced ``16000 + 16769 = 32769``
            prompt+output tokens against a 32768-token context, a fixed
            HTTP 400 on EVERY real document (``recall`` never actually ran,
            only ever validated on toy inputs). ``max_tokens`` itself is left
            untouched — it is the main review pass's batched-candidate budget,
            a separate concern — and this field is scoped to recall only.
        recall_max_chars: Recall now chunks the already-placeholder-spliced
            document (see ``recall_spans``) instead of sending it whole — the
            other missing half of the same incident: even with its own output
            budget, recall was still the only layer with no input chunking at
            all (LLM detection chunks at ``LLMConfig.max_chars``, GLiNER at
            800 chars), so a large enough document alone can still overflow
            the context. Chunking recall is safe because placement of found
            values never depends on which chunk they came from: each returned
            value is located by an exact substring search over the ORIGINAL
            (un-chunked) text and only claims non-overlapping spots — see
            ``recall_spans``.
        recall_concurrency: Сколько recall-кусков одного документа слать
            ОДНОВРЕМЕННО (см. ``recall_spans``). Раньше куски шли строго
            последовательно: на реальном документе 73000 символов это 7
            чат-вызовов подряд по ~12 с каждый — почти 84 с только на recall,
            хотя пул ``http_pool.MAX_INFLIGHT`` держит 24 одновременных
            чат-слота, и recall-стадия ОДНОГО документа занимала из них ровно
            один. Значение НЕ поднято выше 4: этот пул общий на ВСЕ
            параллельно обрабатываемые документы (см. докстринг
            ``http_pool``, "24 x конкурентно"), и recall-стадия одного
            документа не должна забирать себе заметную долю пула, пока рядом
            обрабатываются другие документы — иначе ускорение одного
            документа обернётся задержкой всех остальных. ``<= 1`` сохраняет
            прежнее строго последовательное поведение без изменений.
    """

    base_url: str = "http://127.0.0.1:11433/v1"
    model: str = "qwen3.5:9b"
    max_tokens: int = 16000
    temperature: float = 0.0
    timeout: float = 300.0
    api_key: str = "not-needed"
    extra_body: dict = field(default_factory=dict)
    context_chars: int = 60
    batch_size: int = 35
    recall_max_tokens: int = 2000
    recall_max_chars: int = 12000
    recall_concurrency: int = 4
    # Включает дополнительный recall-проход (см. recall_spans): один вызов LLM по
    # уже-замаскированному тексту, чтобы ДОБРАТЬ ПДн, которые детекторы пропустили.
    # По умолчанию берётся из окружения ANONYMIZER_LLM_RECALL (1/true/yes/on) —
    # можно включить на сервере без правки кода. Лишний вызов + склонность к
    # перемаскировке; но перемаскировать безопаснее, чем пропустить.
    recall: bool = field(
        default_factory=lambda: os.getenv("ANONYMIZER_LLM_RECALL", "").strip().lower()
        in ("1", "true", "yes", "on")
    )
    # Режим предмета договора: ревьюер судит метку SUBJECT и перестаёт снимать
    # маску с товарной номенклатуры под предлогом «это продукт». Выставляется
    # сервером из стадии `subject` (server._compose); см. _build_review_prompt.
    subject: bool = False


@dataclass
class _Candidate:
    label: str
    text: str
    context: str


# --- Warning messages for AnonymizationResult.warnings -----------------------
# One template per fail-safe below (see review_spans/recall_spans docstrings
# for the "warnings" parameter contract). Worded for the END USER, not the
# developer — the stderr prints alongside these are what's for developers.
#
# Review and its two sub-stages (short numbers, adjacent-name re-check) can
# only make masking MORE permissive when the model actually answers; a failed
# call there leaves everything masked exactly as detected, so the consequence
# is "possibly over-masked, never under-masked" — safe, and the wording says
# so plainly. Recall is the opposite: it is the layer that ADDS masking for
# PII no earlier layer caught, so a failed recall call means that search never
# happened — a real coverage gap, and the wording must read as one.
_REVIEW_FAILED_MESSAGE = (
    "Не удалось выполнить проверку на лишние маски. Из-за этого документ "
    "может быть избыточно замаскирован (например, обычное слово осталось "
    "скрыто под плейсхолдером), но это безопасно: настоящие персональные "
    "данные при этом не раскрываются."
)
_REVIEW_SHORT_NUMBERS_FAILED_MESSAGE = (
    "Не удалось проверить, являются ли короткие числа (2-3 цифры) в "
    "документе значимыми идентификаторами — по умолчанию такие числа "
    "остаются НЕЗАМАСКИРОВАННЫМИ, чтобы ошибочная маска не разрушила "
    "нумерацию документа. Риск невелик: короткое голое число само по себе "
    "почти никогда не является персональными данными."
)
_REVIEW_ADJACENT_FAILED_MESSAGE = (
    "Дополнительная проверка спорных соседних имён не была выполнена — "
    "такие кандидаты остались замаскированными без изменений. Персональные "
    "данные при этом не раскрываются, документ может быть немного избыточно "
    "замаскирован."
)
_REVIEW_PERSON_GATE_UNAVAILABLE_MESSAGE = (
    "Словарная проверка фамилий и имён недоступна — для одного или нескольких "
    "кандидатов ФИО предлагалось снять маску, но эти решения НЕ были "
    "применены: без словаря "
    "нельзя надёжно отличить редкую фамилию от обычного слова, поэтому такие "
    "кандидаты остались замаскированными. Персональные данные при этом не "
    "раскрываются, документ может быть немного избыточно замаскирован."
)
_RECALL_FAILED_MESSAGE = (
    "Не была выполнена проверка на полноту — документ не был дополнительно "
    "проверен на персональные данные, пропущенные другими слоями. Часть "
    "персональных данных могла остаться незамаскированной."
)
# Частичный отказ recall'а: документ теперь режется на куски (см.
# ReviewConfig.recall_max_chars), и один сбойный кусок больше не обнуляет
# весь слой — но если сбойных кусков несколько, часть документа всё равно
# осталась непроверенной. В отличие от _RECALL_FAILED_MESSAGE (весь документ)
# здесь честно говорится "часть документа", без технических деталей (номер/
# границы куска — в stderr, не в клиентском предупреждении).
_RECALL_PARTIAL_MESSAGE = (
    "Проверка на полноту была выполнена только для ЧАСТИ документа — "
    "оставшаяся часть не была дополнительно проверена на персональные "
    "данные, пропущенные другими слоями. Персональные данные в этой части "
    "могли остаться незамаскированными."
)


def review_spans(
    text: str,
    spans: list[Span],
    config: "ReviewConfig | None" = None,
    warnings: list | None = None,
) -> list[Span]:
    """Ask an LLM to double-check spans: drop false positives, trim titles
    stuck to real names, and merge different wordings of the same entity.

    Groups spans by (label, whitespace-collapsed, case-folded value) so a
    value repeated many times (``mask_all_occurrences``) is judged once and
    dropped/kept/trimmed as a whole. Only labels in ``_REVIEWABLE_LABELS`` are
    ever sent to the model; everything else passes through untouched.

    CAUTION this grouping implies: judging a repeated value from only ONE
    occurrence's context means one bad call on an ambiguous mention (e.g.
    "Никита, добрый день" read as a generic greeting rather than someone's
    real name) silently drops EVERY occurrence of that value in the whole
    document — confirmed by reproducing it directly (a forced keep=false on
    one grouped candidate deleted all N spans sharing its key). Repetition
    alone is NOT proof a value is real PII, though — a mislabeled common word
    can repeat just as easily, and permanently trusting repeated values
    without review would leave those stuck masked with no way to correct them.
    So instead of special-casing repeat count, ``_group_candidates`` samples
    UP TO 3 spread-out occurrences (first/middle/last) and shows the model ALL
    of them for one value — richer, more representative evidence beats a
    single roll of the dice on whichever mention happened to come first.

    Returns the filtered/adjusted span list. On any error talking to the LLM
    (or if it returns unparsable output) the affected spans are left masked,
    untrimmed and unmerged — this layer can only make anonymization *more*
    permissive when it is confident, never less safe by default.

    ``warnings``, if given, is a list this function APPENDS to (never
    replaces) whenever one of its internal fail-safes actually fires — same
    contract as ``LLMDetector.warnings`` / ``RemoteGLiNERDetector.warnings``
    (see ``llm.py`` / ``gliner_remote.py``): a stage silently degrading must
    still be visible to the caller, not just logged to stderr. ``None`` (the
    default) means "don't collect warnings" — used by direct callers (tests,
    scripts) that don't need them.
    """
    if not spans:
        return spans
    cfg = config or ReviewConfig()

    # Короткие голые числа судим отдельным сфокусированным вопросом: жёсткий
    # порог по длине тут неуместен — «000» бывает куском госномера, «12» —
    # номером пункта, «777» — кодом подразделения, и различает их только
    # контекст. Отдельный вызов (а не общий батч) потому же, почему и
    # _ask_adjacent: на маленьком прицельном вопросе модель стабильна.
    drop_short = _judge_short_numbers(text, spans, cfg, warnings)
    if drop_short:
        spans = [s for s in spans if id(s) not in drop_short]
        if not spans:
            return spans

    # SUBJECT пересматриваем ТОЛЬКО в subject-режиме: критерий «номенклатура vs
    # служебная лексика» живёт в промпте этого режима (_build_review_prompt).
    # Без него ревьюер стабильно снимал маску с предмета договора («автомат
    # Калашникова АК-74М», «медицинское оборудование») — проверено на живой
    # модели. Fail-safe: не знаем критерия — не трогаем, маска остаётся.
    reviewable = _REVIEWABLE_LABELS if cfg.subject else _REVIEWABLE_LABELS - {"SUBJECT"}
    candidates = _group_candidates(text, spans, cfg.context_chars, reviewable)
    if not candidates:
        return spans

    keys = list(candidates.keys())
    items = list(candidates.items())

    # Single call: the model sees the WHOLE candidate list as one JSON-like
    # request. A small model is more self-consistent and better at spotting
    # obvious false positives (a role word, a product name, an abbreviation)
    # when it can compare every candidate against all the others at once than
    # when the list is split into independent batches. merge_with ids are then
    # global. Fail-safe: any error => empty verdicts => everything stays masked.
    verdicts: dict[int, dict] = {}
    try:
        raw = _ask(items, cfg)
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"[review] _ask упал: {exc}", file=sys.stderr)
        raw = {}
        if warnings is not None:
            warnings.append(
                {
                    "kind": "review_failed",
                    "message": _REVIEW_FAILED_MESSAGE,
                }
            )
    for idx, v in raw.items():
        if not (0 <= idx < len(items)):
            continue
        mw = v.get("merge_with")
        if isinstance(mw, int) and not (0 <= mw < len(items)):
            v = {k: vv for k, vv in v.items() if k != "merge_with"}
        verdicts[idx] = v

    keep = {k: True for k in keys}
    trimmed_text: dict[str, str | None] = {k: None for k in keys}
    parent = {k: k for k in keys}

    def _norm(s: str) -> str:
        return " ".join(s.split()).casefold()

    dropped_ids = 0
    # Кандидаты PERSON, которых модель попросила снять (keep=false), но
    # детерминированный морфологический шлюз (см. detectors.can_unmask_person)
    # отказал/разрешил — собираем для одного финального лога и (для отказов
    # из-за недоступности словаря) единственной записи в warnings, вместо
    # печати построчно внутри цикла. См. комментарий у can_unmask_person.
    _person_gate_refused: list[tuple[str, list]] = []
    _person_gate_allowed: list[tuple[str, list]] = []
    _person_gate_unavailable = False
    for idx, key in enumerate(keys):
        # ``verdicts`` is exceptions-only (see _build_review_prompt): a
        # candidate the model didn't mention at all simply has no entry here,
        # and ``keep[key]`` was already initialised to True above — the
        # correct, safe default. Nothing below this point can *unmask* a
        # candidate the model was silent about; it can only act on an entry
        # that was actually returned AND passes the echo-text check next.
        v = verdicts.get(idx)
        if not v:
            continue
        # Safety net: the model must echo back the exact candidate text for
        # this id. A small/fast model can lose count on a long batch (skip,
        # duplicate, or shift ids) — if what it claims to be judging doesn't
        # match what's actually at this id, its numbering has drifted, so we
        # ignore the verdict entirely rather than risk acting on the wrong
        # candidate (this is what caused real names to be dropped in testing:
        # the model's id/text pairing had silently desynced mid-batch). This
        # is now the ONLY positional safety net: under the old "one verdict
        # per candidate" format a short reply was itself suspicious and could
        # be flagged by comparing counts, but under exceptions-only a short
        # (or even empty) reply is the expected, normal case — so there is
        # deliberately no separate "len(verdicts) != len(candidates)" check.
        if _norm(v.get("text", "")) != _norm(candidates[key].text):
            dropped_ids += 1
            continue
        if v.get("keep") is False:
            # Детерминированный шлюз: модель регулярно "прощает" редкие/
            # незнакомые фамилии как обычные слова (канонический случай —
            # «Вайгус»), несмотря на явный запрет в промпте. Для PERSON
            # keep=false доверяем модели ТОЛЬКО если словарная проверка
            # подтверждает, что значение действительно обычное слово (или,
            # для неизвестных словарю значений, явный мусор распознавания —
            # см. detectors.can_unmask_person). Иначе — отказ, маска остаётся.
            if candidates[key].label == "PERSON":
                allowed, gate_verdicts = can_unmask_person(candidates[key].text)
                if not allowed:
                    if not gate_verdicts:  # словарь недоступен — см. can_unmask_person
                        _person_gate_unavailable = True
                    _person_gate_refused.append((candidates[key].text, gate_verdicts))
                    continue  # keep[key] остаётся True — безопасный дефолт
                _person_gate_allowed.append((candidates[key].text, gate_verdicts))
            keep[key] = False
            continue
        trim = v.get("trim")
        cand_text = candidates[key].text
        if isinstance(trim, str) and trim and trim != cand_text and trim in cand_text:
            trimmed_text[key] = trim
        mw = v.get("merge_with")
        if isinstance(mw, int) and 0 <= mw < len(keys):
            target = keys[mw]
            # Мерж разрешён при одинаковых метках И между PERSON<->ORG: детектор
            # часто помечает человека как ORG по контексту («команда … Капитан
            # Яков»), а ревьюер по смыслу видит, что это то же лицо, что и
            # кандидат-PERSON («Вайгус»). Жёсткое равенство меток блокировало
            # такие объединения, и звание+имя оставалось отдельным [ORG_N].
            _mergeable = {"PERSON", "ORG"}
            same_label = candidates[target].label == candidates[key].label
            cross_ok = (
                candidates[target].label in _mergeable
                and candidates[key].label in _mergeable
            )
            if target != key and (same_label or cross_ok):
                parent[key] = target

    if dropped_ids:
        import sys

        print(
            f"[review] discarded {dropped_ids} verdict(s): id/text mismatch "
            "(model's numbering drifted mid-batch) — affected candidates kept masked",
            file=sys.stderr,
        )

    if _person_gate_refused or _person_gate_allowed:
        import sys

        for text, gate_verdicts in _person_gate_refused:
            if not gate_verdicts:
                print(
                    f"[review] person-gate: kept masked, dictionary unavailable: {text!r}",
                    file=sys.stderr,
                )
                continue
            reasons = "; ".join(
                f"{w!r} — {why}" for w, ok, why in gate_verdicts if not ok
            )
            print(
                f"[review] person-gate: refused model's keep=false for {text!r} "
                f"({reasons}) — kept masked",
                file=sys.stderr,
            )
        for text, gate_verdicts in _person_gate_allowed:
            paths = (
                ", ".join(f"{w!r}={why}" for w, _ok, why in gate_verdicts)
                if gate_verdicts
                else "no Cyrillic capitalised words (out of scope)"
            )
            print(
                f"[review] person-gate: confirmed model's keep=false for {text!r} "
                f"via {paths} — unmasked",
                file=sys.stderr,
            )

    if _person_gate_unavailable and warnings is not None:
        warnings.append(
            {
                "kind": "review_person_gate_unavailable",
                "message": _REVIEW_PERSON_GATE_UNAVAILABLE_MESSAGE,
            }
        )

    # Страховка соседства: PERSON-кандидат, которого ревью сняло, но чей спан
    # стоит ВПЛОТНУЮ (пробел/запятая/тире, ≤3 символов) к сохранённому
    # PERSON-спану — подозрителен: такое соседство бывает и перечислением имён
    # («Капитан Яков, Вайгус» = фамилия и позывной; 9B-ревьюер в батче из ~90
    # кандидатов снимал «Вайгус» вопреки контексту), и ролью, приклеенной к
    # имени («Самозанятый Андрей Смирнов» — роль корректно снята). Слепо
    # доверять нельзя ни большому батчу, ни жёсткому правилу, поэтому спорные
    # случаи ЭСКАЛИРУЮТСЯ отдельным сфокусированным LLM-запросом: маленький
    # батч + прицельный вопрос «роль или имя?» — на нём модель стабильна.
    # Fail-safe: любой сбой/непарсируемый ответ → маска остаётся (безопасно).
    _suspects = _adjacent_dropped_persons(text, spans, candidates, keep)
    if _suspects:
        try:
            _decisions = _ask_adjacent(_suspects, cfg)
        except Exception as exc:  # noqa: BLE001
            import sys

            print(f"[review] _ask_adjacent упал: {exc}", file=sys.stderr)
            _decisions = {}
            if warnings is not None:
                warnings.append(
                    {
                        "kind": "review_adjacent_failed",
                        "message": _REVIEW_ADJACENT_FAILED_MESSAGE,
                    }
                )
        _restored, _confirmed = [], []
        for key, _cand_text, _ctx in _suspects:
            if _decisions.get(key, True):  # нет вердикта = маскируем
                keep[key] = True
                _restored.append(candidates[key].text)
            else:
                _confirmed.append(candidates[key].text)
        import sys

        if _restored:
            print(
                "[review] adjacency re-check kept masked (name/callsign): "
                + ", ".join(sorted(_restored)),
                file=sys.stderr,
            )
        if _confirmed:
            print(
                "[review] adjacency re-check confirmed unmask (role word): "
                + ", ".join(sorted(_confirmed)),
                file=sys.stderr,
            )

    def find_root(k: str) -> str:
        seen: set[str] = set()
        while parent[k] != k:
            if k in seen:  # cycle guard — shouldn't happen, fail safe anyway
                return k
            seen.add(k)
            k = parent[k]
        return k

    def own_text(k: str) -> str:
        return trimmed_text[k] or candidates[k].text

    clusters: dict[str, list[str]] = {}
    for k in keys:
        if not keep[k]:
            continue
        root = find_root(k)
        if not keep.get(root, True):
            root = k  # merge target was independently dropped — don't merge into it
        clusters.setdefault(root, []).append(k)

    merge_key_for: dict[str, str] = {}
    canonical_for: dict[str, str] = {}
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        mkey = f"{candidates[root].label}\x00__merged__\x00{root}"
        canon = own_text(root)
        for m in members:
            merge_key_for[m] = mkey
            canonical_for[m] = canon

    dropped_stems = _stems_of_dropped(candidates, keep)

    out: list[Span] = []
    for span in spans:
        key = _key_of(span)
        if key not in candidates:
            if _is_orphaned_derivative(span, dropped_stems):
                continue  # производная снятого кандидата — снимаем вместе с ним
            out.append(span)  # label not reviewable, untouched
            continue
        if not keep[key]:
            continue  # dropped: false positive, revert to plain text
        s = span
        new_text = trimmed_text.get(key)
        if new_text:
            offset = span.text.find(new_text)
            if offset >= 0:
                s = replace(
                    s,
                    start=span.start + offset,
                    end=span.start + offset + len(new_text),
                    text=new_text,
                )
        if key in merge_key_for:
            s = replace(s, merge_key=merge_key_for[key], canonical_text=canonical_for[key])
        out.append(s)
    return out


# --- HTTP ---------------------------------------------------------------
# Shared by all four call sites below (_ask_short_numbers, _ask_adjacent,
# _ask_recall, _ask): builds the request and POSTs it over the pooled,
# keep-alive connection (see http_pool.py) instead of a fresh
# urllib.request.urlopen() connection per call. Raises whatever
# http_pool.post_json raises (OSError/http_pool.PoolConnectionError on
# connection failure) or http_pool.PoolConnectionError for a non-2xx status —
# each call site applies its own usage_log bookkeeping and error wording
# around this, so the exception is intentionally left unwrapped here.
def _chat_completion(cfg: "ReviewConfig", payload: dict) -> dict:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.api_key}",
    }
    status, resp_body = http_pool.post_json(url, body, headers, cfg.timeout, pool="chat")
    if status != 200:
        # Mirrors the old urlopen behaviour, where a non-2xx status raised
        # HTTPError (a urllib.error.URLError subclass) and was caught by the
        # same except clause as any other connection failure.
        raise http_pool.PoolConnectionError(f"HTTP {status}: {resp_body[:200]!r}")
    return json.loads(resp_body)


_SHORT_NUMBER_SYSTEM_PROMPT = (
    "Ты — контролёр анонимизации русских документов. Каждый кандидат ниже — "
    "КОРОТКОЕ ЧИСЛО (2-3 цифры), которое детектор пометил как конфиденциальное. "
    "Кандидат выделен в контексте квадратными скобками: [вот_так].\n"
    "Реши по КОНТЕКСТУ, что это число значит:\n"
    "- mask=true — число само по себе является значимым идентификатором или "
    "конфиденциальным значением: код подразделения, серия документа, шифр, "
    "номер части/учреждения, внутренний код — то, по чему можно узнать "
    "человека или организацию.\n"
    "- mask=false — число НЕ является самостоятельным значением: номер пункта, "
    "раздела, статьи или приложения («п. 1.2», «Приложение N 3»); количество, "
    "срок или единица измерения («400 шт.», «12 мм», «3 рабочих дня»); "
    "порядковый номер в перечне; ЧАСТЬ более крупного значения, которое рядом "
    "записано целиком (кусок госномера «А 000 АА 00», пробега «22 000 км», "
    "суммы, телефона, серии-номера) — такое число маскировать НЕЛЬЗЯ, иначе "
    "оно разорвёт значение и остаток утечёт.\n"
    "ВАЖНО: по умолчанию отвечай mask=false. Короткое число почти всегда "
    "оказывается нумерацией или количеством, а замаскированное по ошибке оно "
    "подставится во ВСЕ свои вхождения и разрушит документ. Ставь mask=true "
    "только когда из контекста ЯВНО видно, что это идентификатор.\n"
    'Ответь ТОЛЬКО JSON-массивом объектов {"id": <номер>, "text": <значение '
    'кандидата ДОСЛОВНО>, "mask": true|false} для ВСЕХ кандидатов по порядку.'
)


def _short_number_spans(spans: list[Span]) -> list[Span]:
    from .detectors import is_short_number

    return [s for s in spans if is_short_number(s.text)]


def _judge_short_numbers(
    text: str, spans: list[Span], cfg: "ReviewConfig", warnings: list | None = None
) -> set[int]:
    """Спросить модель, какие короткие числа реально стоит маскировать.

    Возвращает множество ``id(span)`` спанов, которые НУЖНО СНЯТЬ. Умолчание
    здесь ОБРАТНОЕ обычному для этого слоя «сомневаешься — оставь маску»: нет
    ответа модели, ответ не распарсился, LLM недоступна — короткое число
    снимается. Так сделано намеренно: утечка голого двузначного числа
    ничтожна, а ошибочная маска размножается ``mask_all_occurrences`` по всем
    вхождениям и ломает документ (номер пункта «1» дал 127 подстановок и
    превратил нумерацию договора в «[PASSPORT_1].[PASSPORT_2]»), да ещё и
    разрывает более крупные значения, выпуская наружу их остаток.

    ``warnings`` — см. ``review_spans``: список, в который эта функция
    добавляет запись, если запрос к LLM реально не удался (сеть/HTTP), а не
    просто вернул нераспознанный JSON.
    """
    shorts = _short_number_spans(spans)
    if not shorts:
        return set()

    # По одному вопросу на РАЗЛИЧНОЕ значение: одинаковые числа всё равно
    # получат общий плейсхолдер, судить их порознь незачем.
    by_value: dict[str, list[Span]] = {}
    for s in shorts:
        by_value.setdefault(s.text.strip(), []).append(s)

    items: list[tuple[str, str]] = []
    for value, group in by_value.items():
        first = group[0]
        lo = max(0, first.start - 90)
        hi = min(len(text), first.end + 90)
        ctx = f"{text[lo:first.start]}[{first.text}]{text[first.end:hi]}"
        items.append((value, " ".join(ctx.split())))

    lines = [f'{i}. "{value}" — контекст: «{ctx}»'
             for i, (value, ctx) in enumerate(items)]
    try:
        verdicts = _ask_short_numbers(lines, items, cfg)
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"[short-num] LLM-запрос упал ({exc}) — короткие числа сняты",
              file=sys.stderr)
        verdicts = {}
        if warnings is not None:
            warnings.append(
                {
                    "kind": "review_short_numbers_failed",
                    "message": _REVIEW_SHORT_NUMBERS_FAILED_MESSAGE,
                }
            )

    drop: set[int] = set()
    kept_values: list[str] = []
    for value, group in by_value.items():
        if verdicts.get(value, False):  # нет вердикта = снимаем (см. docstring)
            kept_values.append(value)
            continue
        drop.update(id(s) for s in group)

    import sys

    print(
        f"[short-num] коротких чисел: {len(by_value)}; "
        f"модель оставила замаскированными: {kept_values or '—'}",
        file=sys.stderr,
    )
    return drop


def _ask_short_numbers(
    lines: list[str], items: list[tuple[str, str]], cfg: "ReviewConfig"
) -> dict[str, bool]:
    """value -> mask. Значения без совпавшего эхо-текста в результат не входят."""
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _SHORT_NUMBER_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": cfg.temperature,
        "max_tokens": 2000,
        # см. комментарий в review._ask
        "presence_penalty": 0,
        "frequency_penalty": 0,
        **cfg.extra_body,
    }
    t0 = time.time()
    try:
        data = _chat_completion(cfg, payload)
    except Exception as exc:  # noqa: BLE001
        usage_log.record_call(
            "review_short_numbers", model=cfg.model, seconds=time.time() - t0,
            chars=len("\n".join(lines)), ok=False, error=str(exc),
        )
        raise
    usage = data.get("usage") or {}
    usage_log.record_call(
        "review_short_numbers",
        model=cfg.model,
        prompt_tokens=usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("completion_tokens") or 0,
        seconds=time.time() - t0,
        chars=len("\n".join(lines)),
        ok=True,
    )
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    blob = _extract_json_array(content)
    if blob is None:
        return {}
    try:
        arr = json.loads(blob)
    except json.JSONDecodeError:
        return {}

    out: dict[str, bool] = {}
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        i, m, t = obj.get("id"), obj.get("mask"), obj.get("text")
        if not (isinstance(i, int) and isinstance(m, bool) and isinstance(t, str)):
            continue
        if not (0 <= i < len(items)):
            continue
        value, _ctx = items[i]
        # Тот же страховочный эхо-текст, что и в основном ревью.
        if t.strip() != value:
            continue
        out[value] = m
    return out


# Производные спаны, которые НЕ проходят ревью сами (см. _group_candidates), но
# целиком порождены другим спаном: падежные формы и алиасы.
_DERIVED_SOURCES = frozenset({"morph", "alias"})


def _stems_of_dropped(
    candidates: dict[str, "_Candidate"], keep: dict[str, bool]
) -> dict[str, list[str]]:
    """label -> основы кандидатов, с которых ревью сняло маску (casefold)."""
    from .detectors import _decl_stem

    out: dict[str, list[str]] = {}
    for key, cand in candidates.items():
        if keep.get(key, True):
            continue
        stem = _decl_stem(" ".join(cand.text.split())).casefold()
        if len(stem) >= 4:
            out.setdefault(cand.label, []).append(stem)
    return out


def _is_orphaned_derivative(span: Span, dropped_stems: dict[str, list[str]]) -> bool:
    """True, если спан — падежная форма/алиас значения, снятого ревью.

    ``propagate_declensions`` строит формы как ``<основа><окончание>`` от уже
    найденного значения, а ``_group_candidates`` их на пересмотр НЕ отдаёт
    (source не вероятностный). Из-за этого разрыва ревью снимало маску с
    исходного «Автомобиль», а порождённые им «Автомобиля»/«Автомобилю»
    оставались замаскированными: в договоре купли-продажи автомобиля обычное
    слово превращалось в [PERSON_8] в 18 местах, причём именительный падеж
    рядом стоял открытым. Совпадение по основе — и есть та связь «родитель →
    производная», которой не хватало: другого способа её узнать нет, спаны
    происхождение не хранят.
    """
    if span.source not in _DERIVED_SOURCES:
        return False
    value = " ".join(span.text.split()).casefold()
    return any(value.startswith(stem) for stem in dropped_stems.get(span.label, ()))


# Зазор между соседними именами: пусто, пробелы, запятая, точка, тире —
# но не больше 3 символов (иначе это уже не перечисление, а разные фразы).
_ADJ_GAP_RE = re.compile(r"^[\s,.;:—–\-]{0,3}$")

_ADJACENT_SYSTEM_PROMPT = (
    "Ты — контролёр анонимизации русских документов. Каждый кандидат ниже — "
    "слово или фраза, стоящая ВПЛОТНУЮ к уже подтверждённому имени человека "
    "(кандидат выделен в контексте квадратными скобками [вот_так]).\n"
    "Для каждого кандидата реши:\n"
    "- mask=true — кандидат САМ является частью именования человека: фамилия, "
    "имя, отчество, позывной, ник, псевдоним. Редкое, иностранное или "
    "незнакомое слово («Вайгус», «Смит», странный позывной) — это имя: "
    "mask=true. Незнакомость слова — признак имени, а не ошибки.\n"
    "- mask=false — кандидат является обычным словом русского языка: роль, "
    "статус, должность или обращение, приклеенное к имени («Самозанятый», "
    "«директор», «Капитан», «Исполнитель», «гражданин», «господин») — такое "
    "слово должно остаться читаемым в тексте.\n"
    "Если сомневаешься — mask=true (скрыть безопаснее, чем раскрыть).\n"
    'Ответь ТОЛЬКО JSON-массивом объектов {"id": <номер>, "text": <значение '
    "кандидата ДОСЛОВНО>, \"mask\": true|false} для ВСЕХ кандидатов по порядку."
)


def _adjacent_dropped_persons(
    text: str,
    spans: list[Span],
    candidates: dict[str, "_Candidate"],
    keep: dict[str, bool],
) -> list[tuple[str, str]]:
    """Найти PERSON-кандидатов, снятых ревью, но примыкающих к сохранённым.

    Возвращает [(key, текст, контекст-с-выделением)] для эскалации в
    сфокусированный LLM-запрос (см. ``_ask_adjacent``). Сосед должен быть
    сохранён (или вне ревью): если модель отбросила ОБА имени пары, они не
    «спасают» друг друга (оба могли быть настоящим мусором, «Так-так, Ну-ну»).
    """
    person_spans = [s for s in spans if s.label == "PERSON"]
    dropped_keys = [
        k for k, kept_flag in keep.items()
        if not kept_flag and k in candidates and candidates[k].label == "PERSON"
    ]
    if not dropped_keys:
        return []

    kept_ranges = [
        (s.start, s.end) for s in person_spans
        if keep.get(_key_of(s), True)  # свои спаны с keep=true ИЛИ вне ревью
    ]
    out: list[tuple[str, str, str]] = []
    for key in dropped_keys:
        hit: Span | None = None
        for s in person_spans:
            if _key_of(s) != key:
                continue
            for a, b in kept_ranges:
                if (s.start >= b and _ADJ_GAP_RE.match(text[b:s.start])) or (
                    s.end <= a and _ADJ_GAP_RE.match(text[s.end:a])
                ):
                    hit = s
                    break
            if hit:
                break
        if hit:
            lo = max(0, hit.start - 90)
            hi = min(len(text), hit.end + 90)
            ctx = f"{text[lo:hit.start]}[{hit.text}]{text[hit.end:hi]}"
            out.append((key, candidates[key].text, " ".join(ctx.split())))
    return out


def _ask_adjacent(
    suspects: list[tuple[str, str, str]], cfg: "ReviewConfig"
) -> dict[str, bool]:
    """Сфокусированный LLM-вопрос «роль или имя?» по спорным соседям.

    Возвращает key -> mask (True = оставить маску). Кандидаты, по которым
    модель не ответила или ответ не сошёлся по тексту, в результат не попадают
    (вызывающий код трактует отсутствие как mask=True — безопасное умолчание).
    """
    lines = [f'{idx}. "{cand_text}" — контекст: «{ctx}»'
             for idx, (_key, cand_text, ctx) in enumerate(suspects)]
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _ADJACENT_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": cfg.temperature,
        "max_tokens": 2000,
        # см. комментарий в review._ask
        "presence_penalty": 0,
        "frequency_penalty": 0,
        **cfg.extra_body,
    }
    t0 = time.time()
    try:
        data = _chat_completion(cfg, payload)
    except Exception as exc:  # noqa: BLE001
        usage_log.record_call(
            "review_adjacent", model=cfg.model, seconds=time.time() - t0,
            chars=len("\n".join(lines)), ok=False, error=str(exc),
        )
        raise
    usage = data.get("usage") or {}
    usage_log.record_call(
        "review_adjacent",
        model=cfg.model,
        prompt_tokens=usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("completion_tokens") or 0,
        seconds=time.time() - t0,
        chars=len("\n".join(lines)),
        ok=True,
    )
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    blob = _extract_json_array(content)
    if blob is None:
        return {}
    try:
        arr = json.loads(blob)
    except json.JSONDecodeError:
        return {}

    def _norm(s: str) -> str:
        return " ".join(s.split()).casefold()

    out: dict[str, bool] = {}
    for obj in arr if isinstance(arr, list) else []:
        if not isinstance(obj, dict):
            continue
        i, m, t = obj.get("id"), obj.get("mask"), obj.get("text")
        if not (isinstance(i, int) and isinstance(m, bool) and isinstance(t, str)):
            continue
        if not (0 <= i < len(suspects)):
            continue
        key, cand_text, _ctx = suspects[i]
        # Тот же страховочный эхо-текст, что и в основном ревью: вердикт
        # применяется только если модель дословно повторила значение.
        if _norm(t) != _norm(cand_text):
            continue
        out[key] = m
    return out


# --- Recall-проход: добор пропущенного через LLM -----------------------------
# Метки, которые LLM разрешено «дорисовать». Незнакомый тип → SENSITIVE.
_RECALL_LABELS = frozenset({
    "PERSON", "ORG", "LOCATION", "ADDRESS", "CITY", "REGION", "STREET", "HOUSE",
    "DATE", "PHONE", "EMAIL", "URL", "IP_ADDRESS", "AMOUNT",
    "INN", "SNILS", "OGRN", "OKPO", "KPP", "BIK", "BANK_ACCOUNT",
    "CONTRACT", "PASSPORT", "MILITARY_ID", "CREDIT_CARD", "ADMIN_CODE",
    "OMS", "DRIVER_LICENSE", "BIRTH_CERTIFICATE",
    # Предмет договора: recall-промпт его не запрашивает, но если модель всё же
    # вернёт этот тип, метка должна сохраниться, а не выродиться в SENSITIVE.
    "SUBJECT",
})

_RECALL_SYSTEM_PROMPT = (
    "Ты — контролёр ПОЛНОТЫ обезличивания. Ниже текст, где часть персональных и "
    "конфиденциальных данных УЖЕ заменена на плейсхолдеры вида [ТИП_НОМЕР] "
    "(например [PERSON_1], [ORG_2], [DATE_1], [BANK_ACCOUNT_1]). Найди значения, "
    "которые ОСТАЛИСЬ ВИДНЫ и ещё НЕ заменены плейсхолдером: ФИО и обращения к "
    "людям; организации, банки, госорганы, стороны договора; адреса; телефоны; "
    "e-mail; даты; номера и коды (счёт, ИНН, ОГРН, ОКПО, КПП, БИК, номер договора, "
    "номер филиала/отделения, паспорт, СНИЛС и т.п.); денежные суммы.\n"
    "ОСОБОЕ ВНИМАНИЕ — одиночные фамилии, имена и инициалы, оставшиеся рядом с "
    "уже замаскированным ФИО или САМИ ПО СЕБЕ: фамилия в скобках после "
    "плейсхолдера («[PERSON_1] (Бальмонт)», «([PERSON_2]) Иванов»); фамилия в "
    "подписи или её расшифровке («подпись: Петров», «/ Сидоров /»); повтор "
    "фамилии где-либо ещё; обращение по имени. Даже ОДНО слово-фамилия или имя "
    "рядом с плейсхолдером, в скобках или в подписи — это ПДн, ОБЯЗАТЕЛЬНО "
    "включи его. Название банка после «Банк:» / «Наименование банка:» — тоже "
    "включи.\n"
    "ВАЖНЫЕ НОМЕРА И КОДЫ: включай любой числовой ряд после слов ИНН, КПП, ОГРН, "
    "ОКПО, ОКТМО, ОКВЭД, БИК, счёт/р/с/к/с/л/с, СНИЛС, паспорт, табельный, шифр, "
    "код — ДАЖЕ если длина непривычная или в номере опечатка/лишние знаки; а "
    "также почтовый индекс (6 цифр в адресе, напр. «101000, г. Москва»), "
    "кадастровый номер, VIN, госномер авто. Числа-идентификаторы важнее "
    "перепроверки — лучше замаскировать.\n"
    "СЛИПШИЙСЯ И КРИВОЙ ТЕКСТ: в документе слова и числа могут идти БЕЗ пробелов "
    "или с ошибками распознавания («Москва101000», «годаг.», «ИвановИ.И.», "
    "«ООО«Ромашка»»). Всё равно найди и верни точную подстроку с ПДн, как она "
    "записана в тексте (даже слипшуюся), — такие места пропускать НЕЛЬЗЯ.\n"
    "НЕ трогай уже существующие плейсхолдеры [ТИП_НОМЕР]. НЕ включай обычные "
    "слова, роли (Заказчик, Исполнитель, Сторона) и термины. Если сомневаешься, "
    "конфиденциально ли значение — ЛУЧШЕ включи его (перемаскировать безопаснее, "
    "чем пропустить).\n"
    "Сами токены вида [ТИП_НОМЕР] (например [PERSON_1], [ORG_2], [DATE_3]) — это "
    "УЖЕ замаскированные значения, а не текст документа: никогда не включай их "
    "в ответ как найденное значение, сообщай только о том, что до сих пор "
    "видно обычным текстом.\n"
    "Верни ТОЛЬКО JSON-массив объектов {\"text\": \"<точная подстрока из текста>\", "
    "\"type\": \"<ТИП>\"}. Поле text должно ДОСЛОВНО (те же символы) совпадать с "
    "фрагментом текста. Если всё уже замаскировано — верни []. Без пояснений."
)


def _splice_placeholders(text: str, spans: list[Span], span_ph: dict[int, str]) -> str:
    """Собирает промежуточный анонимизированный текст (плейсхолдеры на местах)."""
    ordered = sorted(spans, key=lambda s: s.start)
    pieces: list[str] = []
    cursor = 0
    for s in ordered:
        if s.start < cursor:  # перекрытие — пропускаем (не должно случаться)
            continue
        pieces.append(text[cursor:s.start])
        pieces.append(span_ph.get(id(s), text[s.start:s.end]))
        cursor = s.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _occurrences(text: str, value: str) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        i = text.find(value, start)
        if i < 0:
            break
        out.append((i, i + len(value)))
        start = i + len(value)
    return out


def recall_spans(
    text: str,
    spans: list[Span],
    config: "ReviewConfig | None" = None,
    warnings: list | None = None,
) -> list[Span]:
    """LLM-проход на ПОЛНОТУ: показываем модели уже-замаскированный текст (с
    плейсхолдерами в контексте) и просим найти оставшиеся ВИДНЫМИ ПДн. Найденные
    значения размещаем ДЕТЕРМИНИРОВАННО — ищем точную подстроку в оригинале и
    заводим новый спан только на непересекающихся местах. Модель НЕ двигает
    существующие плейсхолдеры и не переписывает текст — только называет
    пропущенные значения; расстановку делает код (безопасно от галлюцинаций
    смещений).

    Документ (уже со вставленными плейсхолдерами) режется на куски не длиннее
    ``cfg.recall_max_chars`` (см. ``ReviewConfig.recall_max_chars``) — раньше
    recall был единственным слоем без входного чанкинга и слал весь документ
    одним сообщением, из-за чего на реальных документах ``prompt_tokens +
    max_tokens`` стабильно переполнял контекст модели (HTTP 400 на КАЖДОМ
    прогоне). Резка безопасна для расстановки: каждое найденное значение всё
    равно ищется точной подстрокой в ОРИГИНАЛЬНОМ (нечанкованном) тексте и
    занимает только непересекающееся место — из какого куска пришло значение,
    для этой логики не имеет значения.

    Fail-safe — ТЕПЕРЬ ПОКУСОЧНЫЙ: один сбойный кусок сам по себе не обнуляет
    весь recall — результаты остальных, успешных кусков всё равно
    возвращаются. ``warnings`` (см. ``review_spans`` про контракт списка)
    получает ровно одну из двух записей:
    * ``recall_failed`` — если провалились ВСЕ куски (в т.ч. случай одного
      куска — старое поведение не изменилось): поиск не состоялся вовсе.
    * ``recall_partial`` — если провалилась ЧАСТЬ кусков, а остальные
      отработали: часть документа проверку прошла, часть — нет.
    ``Cancelled`` (кооперативная отмена, см. ``llm.Cancelled``) — НЕ ошибка
    уровня "куска": она пробрасывается наружу как есть, а не превращается в
    предупреждение — вызывающий код должен узнать об отмене, а не получить
    частичный "успешный" результат.

    Куски отправляются ОДНОВРЕМЕННО (``cfg.recall_concurrency``, по умолчанию
    4 — см. ``ReviewConfig.recall_concurrency``), если их больше одного;
    иначе (в т.ч. при ``recall_concurrency <= 1``) — строго последовательно,
    как раньше. Результаты собираются по ИНДЕКСУ куска, а не по порядку
    завершения запроса: слияние (дедуп ``(value, type)`` по first-seen)
    обходит результаты в порядке кусков документа, поэтому итоговый список
    не зависит от того, какой из параллельных запросов ответил первым —
    тот же порядок, что и у строго последовательного прохода.
    """
    if not spans and not text.strip():
        return []
    cfg = config or ReviewConfig()

    from .chunking import chunk_text
    from .llm import Cancelled
    from .mapping import assign_placeholders

    ordered = sorted(spans, key=lambda s: s.start)
    _, span_ph = assign_placeholders(ordered)
    interim = _splice_placeholders(text, ordered, span_ph)

    chunks = chunk_text(interim, cfg.recall_max_chars)
    if not chunks:
        return []

    import sys

    found: list[tuple[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    ok_chunks = 0
    failed_chunks = 0

    if cfg.recall_concurrency > 1 and len(chunks) > 1:
        # Куски одного документа — параллельно, но их не больше
        # cfg.recall_concurrency сразу: чат-пул общий на ВСЕ одновременно
        # обрабатываемые документы (http_pool.MAX_INFLIGHT, 24 слота), и
        # recall-стадия одного документа не должна забирать себе заметную
        # долю пула — см. ReviewConfig.recall_concurrency.
        results: list[list[tuple[str, str]] | Exception] = [None] * len(chunks)  # type: ignore[list-item]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg.recall_concurrency
        ) as pool:
            future_to_index = {}
            for i, (_offset, chunk) in enumerate(chunks):
                # run_in_context, НЕ pool.submit напрямую — обычный submit
                # не копирует contextvars в поток-воркер, и usage_log
                # потеряет request_id (см. usage_log.run_in_context и
                # идентичный приём в llm.py / gliner_remote.py).
                future_to_index[usage_log.run_in_context(pool, _ask_recall, chunk, cfg)] = i
            for future in future_to_index:
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Cancelled:
                    # Пробрасываем как есть — см. докстринг recall_spans.
                    # Остальные future's с `with` уже дожидаются завершения
                    # (ThreadPoolExecutor.__exit__ ждёт весь пул), это не
                    # обрывает уже запущенные HTTP-запросы на середине.
                    raise
                except Exception as exc:  # noqa: BLE001
                    results[index] = exc
        # Слияние — СТРОГО по индексу куска (порядку в документе), а не по
        # порядку завершения запроса: см. докстринг recall_spans.
        for result in results:
            if isinstance(result, Exception):
                failed_chunks += 1
                print(f"[recall] LLM-запрос упал: {result}", file=sys.stderr)
                continue
            ok_chunks += 1
            for pair in result:
                if pair in seen_pairs:  # дубликат из другого куска — учли один раз
                    continue
                seen_pairs.add(pair)
                found.append(pair)
    else:
        for _offset, chunk in chunks:
            try:
                chunk_found = _ask_recall(chunk, cfg)
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                failed_chunks += 1
                print(f"[recall] LLM-запрос упал: {exc}", file=sys.stderr)
                continue
            ok_chunks += 1
            for pair in chunk_found:
                if pair in seen_pairs:  # дубликат из другого куска — учли один раз
                    continue
                seen_pairs.add(pair)
                found.append(pair)

    if ok_chunks == 0:
        if warnings is not None:
            warnings.append(
                {
                    "kind": "recall_failed",
                    "message": _RECALL_FAILED_MESSAGE,
                }
            )
        return []
    if failed_chunks:
        if warnings is not None:
            warnings.append(
                {
                    "kind": "recall_partial",
                    "message": _RECALL_PARTIAL_MESSAGE,
                }
            )

    taken = [(s.start, s.end) for s in spans]
    out: list[Span] = []
    for value, typ in found:
        val = value.strip()
        if len(val) < 3:
            continue
        if _PLACEHOLDER_LIKE.search(val):  # модель вернула плейсхолдер — игнор
            continue
        occ = _occurrences(text, val)
        # Слишком много вхождений — вероятно, это обычное слово; не перемаскировываем.
        if not occ or len(occ) > 40:
            continue
        label = typ if typ in _RECALL_LABELS else "SENSITIVE"
        for a, b in occ:
            if any(a < e and st < b for st, e in taken):
                continue
            out.append(Span(a, b, label, text[a:b], source="llm-recall"))
            taken.append((a, b))
    print(
        f"[recall] модель вернула {len(found)} кандидат(ов), добавлено спанов: {len(out)}"
        + (f" — примеры: {[v for v, _ in found[:8]]}" if found else ""),
        file=sys.stderr,
    )
    return out


import re as _re

_PLACEHOLDER_LIKE = _re.compile(r"\[[A-Z_]+_\d+\]")


def _ask_recall(interim_text: str, cfg: ReviewConfig) -> list[tuple[str, str]]:
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _RECALL_SYSTEM_PROMPT},
            {"role": "user", "content": interim_text},
        ],
        "temperature": cfg.temperature,
        # НЕ cfg.max_tokens — см. ReviewConfig.recall_max_tokens: recall
        # отвечает коротким списком пропущенных значений, а не полным
        # вердиктом по батчу кандидатов, и делить с ревью один общий бюджет
        # приводило к HTTP 400 (переполнение контекста) на каждом реальном
        # документе.
        "max_tokens": cfg.recall_max_tokens,
        # см. комментарий в review._ask
        "presence_penalty": 0,
        "frequency_penalty": 0,
        **cfg.extra_body,
    }
    t0 = time.time()
    try:
        data = _chat_completion(cfg, payload)
    except Exception as exc:  # noqa: BLE001
        usage_log.record_call(
            "review_recall", model=cfg.model, seconds=time.time() - t0,
            chars=len(interim_text), ok=False, error=str(exc),
        )
        raise
    usage = data.get("usage") or {}
    usage_log.record_call(
        "review_recall",
        model=cfg.model,
        prompt_tokens=usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("completion_tokens") or 0,
        seconds=time.time() - t0,
        chars=len(interim_text),
        ok=True,
    )
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    import sys

    print(f"[recall] сырой ответ LLM ({len(content)} симв.): {content[:600]!r}", file=sys.stderr)
    return _parse_recall(content)


def _parse_recall(content: str) -> list[tuple[str, str]]:
    blob = _extract_json_array(content)
    if blob is None:
        return []
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str]] = []
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        t, ty = obj.get("text"), obj.get("type")
        if isinstance(t, str) and t.strip():
            out.append((t, str(ty or "SENSITIVE").strip().upper()))
    return out


def _key_of(span: Span) -> str:
    return f"{span.label}\x00{' '.join(span.text.split()).casefold()}"


def _group_candidates(
    text: str,
    spans: list[Span],
    context_chars: int,
    reviewable: frozenset | set | None = None,
) -> dict[str, _Candidate]:
    """Group reviewable spans by value, sampling up to 3 spread-out occurrences
    (first/middle/last) per value instead of just the first.

    A value mentioned many times can look ambiguous in one spot and completely
    unambiguous in another ("Никита, добрый день" vs. "Я правильно говорю,
    Никита?") — judging from a single occurrence risks the model reading too
    much into whichever one happened to be first. Spreading the sample across
    the document (not just taking the first N, which could all cluster in one
    ambiguous passage) gives the model a fair cross-section of how the value is
    actually used before it decides.
    """
    if reviewable is None:
        reviewable = _REVIEWABLE_LABELS
    by_key: dict[str, list[Span]] = {}
    order: list[str] = []
    for span in spans:
        if span.label not in reviewable:
            continue
        # LLM-ревью нужно только для ВЕРОЯТНОСТНЫХ источников (GLiNER/LLM), где
        # реально бывают ложные срабатывания. Детерминированные спаны —
        # regex-детекторы (ФИО с инициалами/отчеством, ORG с правовой формой),
        # алиасы («Денисов» из «Денисов А.А.»), падежи (morph) и глоссарий —
        # высокоточны и НЕ отдаются на пересмотр: иначе модель иногда ошибочно
        # снимает реальное ПДн (подпись «Соколова М. В.», повтор фамилии
        # «(Денисов)»). ADMIN_CODE — исключение: его формат сам по себе не
        # доказывает чувствительность, поэтому его ревьюим независимо от источника.
        if span.source not in _NER_LLM_SOURCES and span.label != "ADMIN_CODE":
            continue
        key = _key_of(span)
        if key not in by_key:
            by_key[key] = []
            order.append(key)
        by_key[key].append(span)

    groups: dict[str, _Candidate] = {}
    for key in order:
        occurrences = by_key[key]
        if len(occurrences) <= 3:
            sample = occurrences
        else:
            last = len(occurrences) - 1
            sample = [occurrences[0], occurrences[last // 2], occurrences[last]]
        snippets = []
        for span in sample:
            start = max(0, span.start - context_chars)
            end = min(len(text), span.end + context_chars)
            ctx = f"{text[start:span.start]}[{span.text}]{text[span.end:end]}"
            snippets.append(" ".join(ctx.split()))
        first = occurrences[0]
        groups[key] = _Candidate(
            label=first.label, text=first.text, context=" | ".join(dict.fromkeys(snippets))
        )
    return groups


def _ask(batch: list[tuple[str, _Candidate]], cfg: ReviewConfig) -> dict[int, dict]:
    lines = [
        f'{idx}. [{cand.label}] "{cand.text}" — контекст: «{cand.context}»'
        for idx, (_, cand) in enumerate(batch)
    ]
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _build_review_prompt(cfg.subject)},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        # модели в Ollama могут иметь агрессивные штрафы за повторы по
        # умолчанию (Modelfile) — а наши протоколы требуют дословного эха
        # входного текста в ответе, штрафы за повтор им прямо мешают.
        "presence_penalty": 0,
        "frequency_penalty": 0,
        **cfg.extra_body,
    }
    t0 = time.time()
    try:
        data = _chat_completion(cfg, payload)
    except OSError as exc:
        usage_log.record_call(
            "review_list", model=cfg.model, seconds=time.time() - t0,
            chars=len("\n".join(lines)), ok=False, error=str(exc),
        )
        raise RuntimeError(
            f"Review LLM request to {cfg.base_url} failed: {exc}. "
            "Is LM Studio / Ollama running and the model loaded?"
        ) from exc
    usage = data.get("usage") or {}
    usage_log.record_call(
        "review_list",
        model=cfg.model,
        prompt_tokens=usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("completion_tokens") or 0,
        seconds=time.time() - t0,
        chars=len("\n".join(lines)),
        ok=True,
    )
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_verdicts(content)


def _parse_verdicts(content: str) -> dict[int, dict]:
    """Parse the review reply into ``{id: verdict}``.

    Under the exceptions-only protocol the model only returns entries for
    candidates it wants to act on, so ``keep`` is now OPTIONAL: an entry with
    ``trim``/``merge_with`` but no ``keep`` key is valid (and common), and is
    treated the same as an explicit ``keep=true`` — i.e. it does not drop the
    span, only trims/merges it. Only ``id`` and ``text`` (the echo-text guard
    checked by the caller) are mandatory; a malformed entry missing either is
    skipped entirely, which is safe because a missing id has no effect and
    the corresponding candidate simply stays masked (the caller's default).
    Backward-tolerant of the old verbose format too (every candidate present,
    each with an explicit boolean ``keep``) — that just means every entry is
    a no-op except the ones with ``keep=false``.
    """
    blob = _extract_json_array(content)
    if blob is None:
        return {}
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    out: dict[int, dict] = {}
    for obj in data if isinstance(data, list) else []:
        if not isinstance(obj, dict):
            continue
        i, t = obj.get("id"), obj.get("text")
        if not (isinstance(i, int) and isinstance(t, str)):
            continue
        verdict: dict = {"text": t}
        k = obj.get("keep")
        if isinstance(k, bool):
            verdict["keep"] = k
        trim = obj.get("trim")
        if isinstance(trim, str) and trim.strip():
            verdict["trim"] = trim.strip()
        mw = obj.get("merge_with")
        if isinstance(mw, int):
            verdict["merge_with"] = mw
        out[i] = verdict
    return out
