"""LLM review layer: a final sanity pass over the detected spans.

This is the FOURTH and last layer, run after regex + NER + LLM detection have
produced a candidate span list (and after declension propagation), right
before placeholders are assigned. Every earlier layer is recall-oriented
("when in doubt, mask it"), which is exactly why obvious false positives slip
through — a common word capitalized at the start of a sentence ("День"), a
product/technology name (GPT, Telegram, Битрикс), a legal abbreviation (ФЗ,
НДА), or a speech-to-text artifact all get masked just like real PII.

This layer asks an LLM to look at each *distinct* detected value together with
a snippet of its surrounding context and returns one of three verdicts per
candidate:

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
* Fail-safe: if the LLM is unreachable, times out, or returns something that
  cannot be parsed, every affected span is kept masked, untrimmed and
  unmerged (unchanged behaviour).
* Repeat occurrences of the same value (same label, whitespace-collapsed,
  case-folded) are judged once, as a group, using the first occurrence's
  context — mirrors how ``mapping.assign_placeholders`` groups identical
  entities under one placeholder.
* ``merge_with`` is a batch-local id: the model can only merge candidates it
  was actually shown together in the same request. ``batch_size`` defaults
  high enough to fit a typical meeting transcript's reviewable entities in
  one call; if a document has more, merges across batch boundaries are
  simply not attempted (fails safe to "two separate placeholders").
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace

from .llm import _extract_json_array  # reuse the same tolerant JSON-array scanner
from .spans import Span

# Only these labels are second-guessed. Structured/format-driven identifiers
# are deliberately excluded — see module docstring.
_REVIEWABLE_LABELS = frozenset({
    "PERSON", "ORG", "LOCATION", "CITY", "REGION", "COUNTRY",
    "DISTRICT", "STREET", "HOUSE", "ADDRESS",
    "FIRST_NAME", "LAST_NAME", "MIDDLE_NAME",
})

_REVIEW_SYSTEM_PROMPT = (
    "Ты — контролёр качества анонимизации персональных данных (ПДн) в русском "
    "тексте. Тебе присылают пронумерованный список кандидатов, которые детектор "
    "пометил как ПДн (ФИО, организация, локация и т.п.). Для каждого дано: id, "
    "тип, само значение и фрагмент окружающего текста, где значение выделено "
    "квадратными скобками: [вот_так].\n"
    "Для КАЖДОГО кандидата, СТРОГО по порядку и БЕЗ ПРОПУСКОВ, верни объект "
    '{"id": <номер>, "text": <значение кандидата>, "keep": true|false, '
    '"trim": <опционально>, "merge_with": <опционально>}. Поле text должно '
    "ДОСЛОВНО повторять значение этого кандидата — по нему проверяется, что "
    "нумерация не сбилась. Если не уверен в решении — не пропускай кандидата: "
    "верни его с keep=true.\n"
    "- keep=false — это ОШИБКА детектора: обычное слово, местоимение, день "
    "недели, должность, название программы/продукта (GPT, Telegram, Zoom, "
    "Битрикс, 1С), юридический термин или аббревиатура (ФЗ, НДА), обрывок "
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
    "Настоящие ОРГАНИЗАЦИИ, компании, учреждения, вузы, институты, банки, "
    "госорганы, наименования сторон договора (в т.ч. длинные официальные, "
    "например «Федеральное государственное … университет», «ООО Ромашка», "
    "«Институт информационных технологий и анализа данных») — ВСЕГДА keep=true: "
    "это важные данные, их нужно скрывать. Для типа ORG ставь keep=false ТОЛЬКО "
    "если это слово-роль/отношение (Сторона, Стороны, Заказчик, Исполнитель, "
    "Подрядчик, студенты, сотрудники, участники), термин или аббревиатура "
    "документа (ТЗ, АКТ, МП, КОСГУ, приказ, раздел), либо продукт/ПО — но "
    "НИКОГДА не настоящее название организации.\n"
    "- keep=true без trim/merge_with — значение целиком ПДн, оставить как есть.\n"
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
    "Если сомневаешься — верни keep=true без trim и merge_with: лучше скрыть "
    "лишнее, чем случайно раскрыть настоящие ПДн.\n"
    "Ответь ТОЛЬКО JSON-массивом объектов для ВСЕХ кандидатов по порядку, без "
    "каких-либо пояснений."
)


@dataclass
class ReviewConfig:
    """Connection and batching settings for the LLM review layer.

    Attributes:
        base_url: OpenAI-compatible base (LM Studio / Ollama). Typically the
            same server used for the detection LLM layer.
        model: Model id as the server reports it.
        max_tokens: Output budget for the verdict JSON (small — just a list
            of ``{id, keep, trim?, merge_with?}`` objects).
        temperature: 0 for deterministic verdicts.
        timeout: Per-request seconds.
        api_key: Sent as Bearer; ignored by LM Studio/Ollama.
        extra_body: Merged into the request JSON (e.g. disable "thinking").
        context_chars: Characters of surrounding text kept on each side of a
            candidate value, to give the model enough to judge from.
        batch_size: Deprecated / ignored. The reviewer now sends the whole
            candidate list in a single request (see ``review_spans``); kept
            only so existing configs don't break.
    """

    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen3.5:9b"
    max_tokens: int = 8000
    temperature: float = 0.0
    timeout: float = 300.0
    api_key: str = "not-needed"
    extra_body: dict = field(default_factory=dict)
    context_chars: int = 60
    batch_size: int = 35


@dataclass
class _Candidate:
    label: str
    text: str
    context: str


def review_spans(text: str, spans: list[Span], config: "ReviewConfig | None" = None) -> list[Span]:
    """Ask an LLM to double-check spans: drop false positives, trim titles
    stuck to real names, and merge different wordings of the same entity.

    Groups spans by (label, whitespace-collapsed, case-folded value) so a
    value repeated many times (``mask_all_occurrences``) is judged once and
    dropped/kept/trimmed as a whole. Only labels in ``_REVIEWABLE_LABELS`` are
    ever sent to the model; everything else passes through untouched.

    Returns the filtered/adjusted span list. On any error talking to the LLM
    (or if it returns unparsable output) the affected spans are left masked,
    untrimmed and unmerged — this layer can only make anonymization *more*
    permissive when it is confident, never less safe by default.
    """
    if not spans:
        return spans
    cfg = config or ReviewConfig()

    candidates = _group_candidates(text, spans, cfg.context_chars)
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
    except Exception:
        raw = {}
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
    for idx, key in enumerate(keys):
        v = verdicts.get(idx)
        if not v:
            continue
        # Safety net: the model must echo back the exact candidate text for
        # this id. A small/fast model can lose count on a long batch (skip,
        # duplicate, or shift ids) — if what it claims to be judging doesn't
        # match what's actually at this id, its numbering has drifted, so we
        # ignore the verdict entirely rather than risk acting on the wrong
        # candidate (this is what caused real names to be dropped in testing:
        # the model's id/text pairing had silently desynced mid-batch).
        if _norm(v.get("text", "")) != _norm(candidates[key].text):
            dropped_ids += 1
            continue
        if v.get("keep") is False:
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

    out: list[Span] = []
    for span in spans:
        key = _key_of(span)
        if key not in candidates:
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


def _key_of(span: Span) -> str:
    return f"{span.label}\x00{' '.join(span.text.split()).casefold()}"


def _group_candidates(text: str, spans: list[Span], context_chars: int) -> dict[str, _Candidate]:
    groups: dict[str, _Candidate] = {}
    for span in spans:
        if span.label not in _REVIEWABLE_LABELS:
            continue
        key = _key_of(span)
        if key in groups:
            continue  # first occurrence's context is enough
        start = max(0, span.start - context_chars)
        end = min(len(text), span.end + context_chars)
        ctx = f"{text[start:span.start]}[{span.text}]{text[span.end:end]}"
        groups[key] = _Candidate(label=span.label, text=span.text, context=" ".join(ctx.split()))
    return groups


def _ask(batch: list[tuple[str, _Candidate]], cfg: ReviewConfig) -> dict[int, dict]:
    lines = [
        f'{idx}. [{cand.label}] "{cand.text}" — контекст: «{cand.context}»'
        for idx, (_, cand) in enumerate(batch)
    ]
    payload = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        **cfg.extra_body,
    }
    req = urllib.request.Request(
        cfg.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            data = json.load(resp)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Review LLM request to {cfg.base_url} failed: {exc}. "
            "Is LM Studio / Ollama running and the model loaded?"
        ) from exc
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_verdicts(content)


def _parse_verdicts(content: str) -> dict[int, dict]:
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
        i, k, t = obj.get("id"), obj.get("keep"), obj.get("text")
        if not (isinstance(i, int) and isinstance(k, bool) and isinstance(t, str)):
            continue
        verdict: dict = {"keep": k, "text": t}
        trim = obj.get("trim")
        if isinstance(trim, str) and trim.strip():
            verdict["trim"] = trim.strip()
        mw = obj.get("merge_with")
        if isinstance(mw, int):
            verdict["merge_with"] = mw
        out[i] = verdict
    return out
