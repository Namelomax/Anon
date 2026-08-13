"""Anonymization engine: text + detectors -> redacted text + reversible mapping.

Layers: regex/NER/LLM detection -> optional LLM second-pass leak check ->
optional LLM review -> placeholder assignment -> masking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .detectors import (
    CORPORATE_DETECTORS,
    DEFAULT_DETECTORS,
    DEFAULT_PRIORITY,
    Detector,
    is_generic_entity,
    is_money_amount,
    is_non_pii,
    is_noise_span,
    is_short_number,
    is_stopword_entity,
    propagate_declensions,
    propagate_entity_aliases,
    run_detectors,
)
from .detectors import _SOFT_LABELS

# Job titles / known software terms should stay unmasked under ANY soft
# (NER/LLM) label — e.g. a product name can arrive as LOCATION, not just ORG.
# Kept intentionally small + generic; document-specific brands/slang are the
# review layer's job (no hardcoded per-document value lists).
_TITLE_FILTER_LABELS = _SOFT_LABELS
from .canonicalize import canonicalize_entities
from .mapping import Mapping, assign_placeholders, find_placeholder_spans
from .spans import Span, rebalance_quotes, resolve_overlaps


# Родовые слова, которые детекторы принимают за названия организаций и имена.
# Реальный случай: в протоколе появлялись «Заказчик — организация заказчика» и
# «Заявку в организацию учреждение направлено» — маскировались сами слова
# «заказчик» и «учреждение».
_GENERIC_ENTITY_WORDS: frozenset[str] = frozenset(
    {
        "заказчик", "исполнитель", "подрядчик", "поставщик", "клиент",
        "участник", "участники", "представитель", "представители",
        "сотрудник", "сотрудники", "коллега", "коллеги", "спикер",
        "директор", "руководитель", "начальник", "специалист", "аналитик",
        "команда", "команды", "сторона", "стороны", "пользователь",
        "организация", "организации", "учреждение", "учреждения",
        "компания", "компании", "фирма", "предприятие", "отдел",
        "департамент", "служба", "офис", "филиал", "подразделение",
        "город", "область", "район", "адрес", "чат", "чате", "чата",
        "протокол", "договор", "проект", "система", "программа", "документ",
    }
)


def _is_generic_entity(span: Span) -> bool:
    """True — под маской оказалось родовое слово, а не название."""
    if span.label not in ("ORG", "PERSON", "LOCATION"):
        return False
    cleaned = span.text.strip().strip("«»\"'()[].,;:!?—–-").lower().replace("ё", "е")
    words = [w for w in cleaned.split() if w]
    if not words:
        return True
    # Падежные формы отсекаем по началу слова: «заказчика», «учреждению».
    return all(
        any(w == g or (len(w) > 4 and w.startswith(g[:5])) for g in _GENERIC_ENTITY_WORDS)
        for w in words
    )


@dataclass(frozen=True)
class AnonymizationResult:
    """Result of anonymizing one text.

    Attributes:
        text: The original, unmodified input.
        anonymized_text: Text with sensitive spans replaced by placeholders.
        mapping: ``placeholder -> original`` dict needed to restore the text.
        spans: The non-overlapping spans that were redacted, in order.
    """

    text: str
    anonymized_text: str
    mapping: Mapping
    spans: tuple[Span, ...] = field(default_factory=tuple)
    # How many ``[LABEL_123]``-shaped tokens were already present in the input
    # (before any detection ran). Non-zero almost always means the document was
    # anonymized before — re-uploading an already-anonymized file. Surfaced so
    # a caller/UI can warn instead of the mapping silently filling with junk.
    preexisting_placeholders: int = 0
    # Post-anonymization leak scan (see verify.scan_residual_pii): PII-looking
    # fragments still present in ``anonymized_text`` (long digit runs, emails).
    # A checklist for the human, not a hard error — turns silent misses visible.
    warnings: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def summary(self) -> dict[str, int]:
        """Count of redacted spans per label."""
        counts: dict[str, int] = {}
        for span in self.spans:
            counts[span.label] = counts.get(span.label, 0) + 1
        return counts


class Anonymizer:
    """Configurable, reusable anonymizer.

    Args:
        detectors: Detector objects to run. Defaults to the regex set for
            structured Russian PII. Add an NER detector here for names/addresses.
        priority: ``label -> weight`` map used to resolve overlapping spans.
    """

    def __init__(
        self,
        detectors: Iterable[Detector] | None = None,
        *,
        priority: dict[str, int] | None = None,
        mask_all_occurrences: bool = True,
        review_config=None,
        second_pass_detectors: Iterable[Detector] | None = None,
    ) -> None:
        self._detectors = tuple(detectors) if detectors is not None else DEFAULT_DETECTORS
        self._priority = priority if priority is not None else DEFAULT_PRIORITY
        self._mask_all = mask_all_occurrences
        # Optional 4th layer: an LLM double-checks the surviving spans and
        # reverts obvious false positives (see review.py). None = skip it.
        self._review_config = review_config
        # Optional leak check: after the first masking pass these detectors
        # (typically just the LLM detector) re-scan the INTERIM anonymized text.
        # Whatever they still find is real PII the first pass missed — a bare
        # first name next to a masked full name, a standalone surname, an org
        # without a legal form. Found values are located back in the original
        # text and join the span list BEFORE the review layer, so the reviewer
        # can also merge e.g. "Никита" with "[Никита Иванов]" into one
        # placeholder. Model-driven recall, no hardcoded word lists.
        self._second_pass_detectors = (
            tuple(second_pass_detectors) if second_pass_detectors else ()
        )

    def anonymize(self, text: str) -> AnonymizationResult:
        """Detect sensitive spans and replace them with reversible placeholders.

        Once a value is identified as sensitive it is masked at *every* exact
        occurrence (when ``mask_all_occurrences``), so a repeat the detectors
        missed (common in multi-paragraph documents) can't leak.
        """
        # Guard against re-anonymizing an already-anonymized document: existing
        # "[LABEL_123]" tokens must never be treated as detectable entities —
        # otherwise GLiNER/regex "re-discover" them (they look like capitalized
        # identifiers) and wrap them again, producing garbage like
        # "[[PERSON_1]]" and a mapping full of broken placeholder fragments.
        protected = find_placeholder_spans(text)

        # Короткие голые числа («12», «000») разбирает LLM-ревью по контексту
        # (см. review._judge_short_numbers). Если ревью не подключено, спросить
        # некого — снимаем их детерминированно, как и раньше: ошибочная маска
        # на номере пункта размножается по всем вхождениям и рушит документ,
        # тогда как утечка голого двузначного числа пренебрежима.
        adjudicate_short = self._review_config is not None

        def passes_filters(s: Span) -> bool:
            return (
                _has_alnum(s.text)
                and not (not adjudicate_short and is_short_number(s.text))
                and not (s.label in _TITLE_FILTER_LABELS and is_non_pii(s.text))
                and not is_stopword_entity(s.text, s.label)
                and not is_noise_span(s.text, s.label)
                and not is_generic_entity(s.text, s.label)
                # AMOUNT без денежного маркера отсекаем, КРОМЕ сумм, пойманных
                # по ключевому слову («Цена: N», source="price_kw") — там слово
                # уже доказывает денежный смысл, число валюты рядом не требует.
                and not (
                    s.label == "AMOUNT"
                    and s.source != "price_kw"
                    and not is_money_amount(s.text)
                )
                and not _overlaps_any(s, protected)
            )

        raw = run_detectors(text, self._detectors)
        # Выравниваем непарные «ёлочки» ДО фильтров: обрезанная тримом кавычка
        # («12» сентября…, Технопарка «Сколково) оставляла в тексте сироту-«/».
        raw = [rebalance_quotes(text, s) for s in raw]
        raw = [s for s in raw if passes_filters(s)]
        spans = resolve_overlaps(raw, priority=self._priority)
        # Производные упоминания: голые части многословных ФИО («Никита» при
        # пойманном «Иванов Никита») и имена ORG без правовой формы («Ромашка»
        # при «ООО "Ромашка"»). До склонений — чтобы алиасы получили и падежи.
        aliases = propagate_entity_aliases(text, spans)
        if aliases:
            aliases = [a for a in aliases if passes_filters(a)]
            if aliases:
                spans = resolve_overlaps(spans + aliases, priority=self._priority)
        # Mask declined case-forms of detected entities (e.g. "Лентой" given "Лента").
        # ОБЯЗАТЕЛЬНО через passes_filters: раньше morph-спаны шли в обход всех
        # фильтров — один плохой seed («Заказчика» от NER) размножался по
        # документу немаскируемым фильтрами «Заказчик»/«Заказчику».
        extra = propagate_declensions(text, spans)
        if extra:
            extra = [e for e in extra if passes_filters(e)]
            spans = resolve_overlaps(spans + extra, priority=self._priority)
        # Leak check (see __init__): re-scan the interim-anonymized text with
        # the second-pass detectors; anything still readable there is a miss.
        # Runs BEFORE review so the new candidates are judged (and can be
        # merged with existing ones) in the same review call.
        if self._second_pass_detectors:
            leaked = self._find_leaked_spans(text, spans, protected)
            leaked = [rebalance_quotes(text, s) for s in leaked]
            leaked = [s for s in leaked if passes_filters(s)]
            if leaked:
                spans = resolve_overlaps(spans + leaked, priority=self._priority)
        # 4th layer: LLM double-checks the surviving spans against their
        # context and reverts obvious false positives (see review.py). Runs
        # last, after all detectors, so it judges the final candidate set.
        # Recall-проход (опционально, review_config.recall): показываем LLM уже
        # замаскированный текст и добираем ПДн, которые детекторы пропустили.
        # Both are single, multi-second LLM calls with NO data dependency
        # between them: recall reasons over the spans as DETECTED (before
        # review's verdicts), so its result never needs review's output —
        # they run CONCURRENTLY via _review_and_recall (see its docstring for
        # the fail-safe/determinism contract). New recall candidates then go
        # through the same filters and overlap resolution as before.
        # Populated (only) inside the review/recall branch below with warnings
        # about a stage that couldn't do its job (see review.py's
        # review_spans/recall_spans ``warnings`` param and
        # _review_and_recall's docstring) — merged into the result's
        # ``warnings`` alongside scan_residual_pii's and the detectors' own,
        # so a failed review/recall pass is visible to the caller exactly
        # like a failed GLiNER/LLM chunk is, not just printed to stderr.
        stage_warnings: list[dict] = []
        if self._review_config is not None:
            detected = spans
            if getattr(self._review_config, "recall", False):
                spans, recalled = self._review_and_recall(text, detected, stage_warnings)
            else:
                from .review import review_spans

                spans = review_spans(text, detected, self._review_config, stage_warnings)
                recalled = []
            if recalled:
                recalled = [rebalance_quotes(text, s) for s in recalled]
                recalled = [
                    s for s in recalled
                    if passes_filters(s) and not _overlaps_any(s, protected)
                ]
                if recalled:
                    spans = resolve_overlaps(spans + recalled, priority=self._priority)
        # Родовые слова («заказчик», «учреждение», «организация») маскировать
        # бессмысленно: они ничего не раскрывают, зато после обратной подстановки
        # ломают падежи — в протоколе появляется «организация учреждение» и
        # «Заявку в организацию учреждение направлено». Фильтр стоит ЗДЕСЬ, а не
        # в конкретном детекторе, потому что помечает их не только GLiNER, но и
        # LLM-слой; на выходе движка источник уже неважен.
        spans = [s for s in spans if not _is_generic_entity(s)]
        # Схлопываем падежные/меточные варианты одной ORG/LOCATION-сущности в
        # один плейсхолдер («Форус»/«Форуса», «Телеграме» как ORG и LOCATION).
        spans = canonicalize_entities(spans)
        mapping, span_placeholders = assign_placeholders(spans)

        if self._mask_all and mapping:
            anonymized, spans = _apply_all_occurrences(text, spans, span_placeholders)
            # Плейсхолдер мог не попасть в итоговый текст (его поверхность
            # перекрыта более длинной или дубль другой сущности, как
            # «КиберКубок 2025» рядом с «КиберКубок 2025» в кавычках) —
            # мёртвые строки из маппинга убираем, чтобы не путать пользователя.
            mapping = {ph: v for ph, v in mapping.items() if ph in anonymized}
        else:
            anonymized = _apply(text, spans, span_placeholders)

        from .verify import scan_residual_pii

        warnings: list[dict] = list(scan_residual_pii(anonymized))
        # Detectors that could not fully analyze their input (e.g. the LLM
        # detector giving up on a chunk after a degenerate reply, see llm.py)
        # surface it here — a chunk skipped by the LLM is PII that may still
        # be unmasked, and that must not be silent. Not all detectors track
        # this, hence getattr with a default instead of a base-class method.
        # Deduplicated by identity: build_anonymizer reuses the SAME LLM
        # detector instance for both the main pass and the second pass, so
        # iterating both lists as-is would double-count its warnings.
        seen_detectors: dict[int, object] = {}
        for det in (*self._detectors, *self._second_pass_detectors):
            seen_detectors[id(det)] = det
        for det in seen_detectors.values():
            warnings.extend(getattr(det, "warnings", None) or [])
        # Review/recall stage failures (see above) — same visibility contract
        # as the detector warnings just above.
        warnings.extend(stage_warnings)

        return AnonymizationResult(
            text=text,
            anonymized_text=anonymized,
            mapping=mapping,
            spans=tuple(spans),
            preexisting_placeholders=len(protected),
            warnings=tuple(warnings),
        )

    def _review_and_recall(
        self, text: str, detected: list[Span], warnings: list[dict]
    ) -> tuple[list[Span], list[Span]]:
        """Run ``review_spans`` and ``recall_spans`` CONCURRENTLY.

        Both are single, multi-second LLM calls that used to run back to
        back (~29s of a ~50s document, measured in production). They have no
        data dependency once recall is given the PRE-review ``detected``
        span list instead of review's result: recall hunts over the SAME
        masking (built from ``detected``) regardless of what review decides,
        so the two calls can overlap.

        Semantic consequence of feeding recall the pre-review masking:
        recall no longer sees values review just unmasked (e.g. a product
        name), so it can't re-flag them — the two layers used to be able to
        fight over exactly that value. Losing that fight is intentional:
        review is the layer that has the final say on what is NOT personal
        data. Recall's ability to find genuinely missed PII is unaffected,
        because the text it reasons over is masked identically either way
        (from ``detected``, not from review's output) EXCEPT inside spans
        that review itself trims or drops — there, and only there, the two
        orderings can disagree.

        Agreement with the accepted tradeoff (per the task spec, restated
        here for the record): sequentially, recall used to get a free
        second opinion on review's own drops/trims, because it saw the
        exact same text review had just unmasked. That is a real, if
        narrow, safety net for a review *mistake* (review dropping
        something a detector had genuinely caught as PII) — not the same
        thing as recall's actual job of catching what NO detector ever
        caught. Removing that accidental backstop is consistent with "review
        has final say over what is/isn't PII" and does not change recall's
        ability to catch a genuinely undetected value anywhere else in the
        document (identical masking there in both orderings) — so this is
        accepted as specified, not flagged as a MISS regression.

        ``usage_log.run_in_context`` (never a bare ``pool.submit``) carries
        the per-document ``request_id`` contextvar into each worker thread —
        without it the two calls' usage-log entries would silently stop
        being grouped under this document's ``request_total`` line (see
        usage_log.py's threading-trap docstring).

        Fail-safe, unchanged from the sequential version and independent of
        which call finishes first: if review raises, ``spans`` fall back to
        ``detected`` (masked, unreviewed, exactly as before review existed).
        If recall raises or finds nothing, ``recalled`` is empty and the
        document proceeds with review's result alone. Each future's
        exception is caught on its own, so one failing can never take the
        other down.

        Determinism: the merge in ``anonymize`` always concatenates
        review's result THEN recall's result, in that fixed order — set
        by this method's ``return``, not by which future happens to
        complete first — so the resulting span list (and therefore
        placeholder numbering) is identical regardless of completion order.

        ``warnings`` is APPENDED to (never replaced/read) with an entry for
        each of the two ``except`` branches below AND for whatever
        review_spans/recall_spans append to their OWN ``warnings`` lists
        internally (see review.py) — this method's own except blocks are a
        backstop for the case where one of them raises past its own internal
        fail-safes entirely, not the normal path. Two separate lists
        (``review_warnings``/``recall_warnings``) are used while the futures
        are in flight rather than one shared list, so the two worker threads
        never mutate the same list concurrently; they're merged into
        ``warnings`` in the same fixed review-then-recall order as the spans,
        once both futures have completed.
        """
        from concurrent.futures import ThreadPoolExecutor

        from . import usage_log
        from .review import (
            _RECALL_FAILED_MESSAGE,
            _REVIEW_FAILED_MESSAGE,
            recall_spans,
            review_spans,
        )

        review_warnings: list[dict] = []
        recall_warnings: list[dict] = []

        with ThreadPoolExecutor(max_workers=2) as pool:
            review_future = usage_log.run_in_context(
                pool, review_spans, text, detected, self._review_config, review_warnings
            )
            recall_future = usage_log.run_in_context(
                pool, recall_spans, text, detected, self._review_config, recall_warnings
            )

            try:
                spans = review_future.result()
            except Exception as exc:  # noqa: BLE001
                import sys

                print(f"[engine] review_spans упал: {exc}", file=sys.stderr)
                spans = detected
                review_warnings.append(
                    {
                        "kind": "review_failed",
                        "message": _REVIEW_FAILED_MESSAGE.format(exc=exc),
                    }
                )

            try:
                recalled = recall_future.result()
            except Exception as exc:  # noqa: BLE001
                import sys

                print(f"[engine] recall_spans упал: {exc}", file=sys.stderr)
                recalled = []
                recall_warnings.append(
                    {
                        "kind": "recall_failed",
                        "message": _RECALL_FAILED_MESSAGE.format(exc=exc),
                    }
                )

        warnings.extend(review_warnings)
        warnings.extend(recall_warnings)

        return spans, list(recalled or [])

    def _find_leaked_spans(
        self, text: str, spans: list[Span], protected: list[tuple[int, int]]
    ) -> list[Span]:
        """Mask the text with the current spans, re-scan the result, and map
        every value the second-pass detectors still see back onto the ORIGINAL
        text as new spans.

        The interim mask hides everything already caught, so the detector's
        full attention lands on what slipped through. Detections that overlap
        a placeholder token are skipped (the model occasionally tags the
        ``[PERSON_1]`` tokens themselves); the rest are located verbatim in the
        original text — a value we cannot find verbatim is never masked.
        """
        if spans:
            _, span_placeholders = assign_placeholders(spans)
            interim, _ = _apply_all_occurrences(text, spans, span_placeholders)
        else:
            interim = text

        hits = run_detectors(interim, self._second_pass_detectors)
        placeholder_ranges = find_placeholder_spans(interim)

        surfaces: set[tuple[str, str]] = set()
        for hit in hits:
            if _overlaps_any(hit, placeholder_ranges):
                continue
            value = hit.text.strip()
            if value:
                surfaces.add((hit.label, value))

        taken = [(s.start, s.end) for s in spans] + list(protected)
        leaked: list[Span] = []
        for label, value in surfaces:
            for a, b in _find_occurrences(text, value):
                if any(a < e and st < b for st, e in taken):
                    continue
                leaked.append(Span(a, b, label, text[a:b], source="llm2"))
                taken.append((a, b))
        return leaked


def _overlaps_any(span: Span, ranges: list[tuple[int, int]]) -> bool:
    return any(span.start < e and st < span.end for st, e in ranges)


def _find_occurrences(text: str, value: str) -> list[tuple[int, int]]:
    """Every non-overlapping exact occurrence of ``value`` in ``text``."""
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        idx = text.find(value, start)
        if idx < 0:
            break
        out.append((idx, idx + len(value)))
        start = idx + len(value)
    return out


def _has_alnum(text: str) -> bool:
    """True if the span contains at least one letter or digit (not pure symbols)."""
    return any(ch.isalnum() for ch in text)


def _label_of(placeholder: str) -> str:
    """`[PERSON_1]` -> `PERSON`."""
    return placeholder.strip("[]").rsplit("_", 1)[0]


_WORD_CH = "0-9A-Za-zА-Яа-яЁё"


def _bounded(orig: str) -> str:
    """Escape ``orig`` and add word boundaries so it only matches as a whole token.

    Without this, a short value (e.g. a name "Ян" or a mis-tagged pronoun) would
    be substituted *inside* other words ("сегодн[Я]", "п[ОН]имаю"). Boundaries
    are added only on the alnum side(s), so values bordered by punctuation
    (emails, "+7…", "№ ЧЕБ-…") still match.
    """
    esc = re.escape(orig)
    pre = rf"(?<![{_WORD_CH}])" if orig[:1].isalnum() else ""
    post = rf"(?![{_WORD_CH}])" if orig[-1:].isalnum() else ""
    return pre + esc + post


def _apply_all_occurrences(
    text: str, spans: list[Span], span_placeholders: dict[int, str]
) -> tuple[str, tuple[Span, ...]]:
    """Replace every exact occurrence of each span's own surface text with its placeholder.

    Built from ``spans`` (not the ``mapping`` dict) so a merged entity — one
    that the review layer decided is the same real-world person/org under
    *different* wordings (e.g. "Капитан Яков" and "Вайгус", see review.py) —
    is masked correctly: each surface form is matched by its own exact text,
    but both point at the same placeholder (``span_placeholders`` already
    reflects the merge via ``Span.merge_key``). This also still catches
    repeats of any surface form the detectors missed elsewhere in the document.

    Originals are matched longest-first so a value containing a shorter one wins.
    Matching is whole-token (word-boundary aware) so a placeholder is never
    spliced into the middle of an unrelated word. Returns the anonymized text and
    the spans of all replaced occurrences.
    """
    surface_to_ph: dict[str, str] = {}
    for span in spans:
        surface_to_ph.setdefault(span.text, span_placeholders[id(span)])

    items = sorted(surface_to_ph.items(), key=lambda kv: len(kv[0]), reverse=True)
    pattern = re.compile("|".join(_bounded(orig) for orig, _ in items))

    result: list[Span] = []
    for m in pattern.finditer(text):
        ph = surface_to_ph[m.group(0)]
        result.append(Span(m.start(), m.end(), _label_of(ph), m.group(0)))
    anonymized = pattern.sub(lambda m: surface_to_ph[m.group(0)], text)
    result.sort(key=lambda s: s.start)
    return anonymized, tuple(result)


def _apply(text: str, spans: list[Span], span_placeholders: dict[int, str]) -> str:
    """Splice placeholders into the text at each (sorted, disjoint) span."""
    if not spans:
        return text
    pieces: list[str] = []
    cursor = 0
    for span in spans:
        pieces.append(text[cursor : span.start])
        pieces.append(span_placeholders[id(span)])
        cursor = span.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def build_anonymizer(
    *,
    use_regex: bool = True,
    use_ner: bool = True,
    ner_backend: str = "natasha",
    include_org: bool = False,
    gliner_config=None,
    corporate: bool = False,
    use_llm: bool = False,
    llm_config=None,
    use_review: bool = False,
    review_config=None,
    use_second_pass: bool = False,
    custom_terms: str | Path | Iterable | None = "auto",
) -> Anonymizer:
    """Construct an anonymizer from the layered detectors.

    Layers (cheap to expensive): regex -> NER -> local LLM -> LLM review. Each
    detection layer is independently switchable; each only adds spans, overlap
    resolution merges. The review layer runs last and can only remove spans.

    Args:
        use_regex: Include the deterministic regex detectors (contacts + RU
            document numbers). Turn off to test NER/LLM in isolation.
        use_ner: Append an NER detector (names/locations). Loads heavy models.
        ner_backend: ``"natasha"`` (fast, CPU) or ``"gliner"`` (multilingual,
            higher recall on Cyrillic / Latin names / streets).
        include_org: Natasha-only — also redact organizations.
        gliner_config: Optional :class:`anonymizer.gliner_ner.GLiNERConfig`.
        corporate: Add business detectors (AMOUNT/CONTRACT/DATE/requisites/
            ADMIN_CODE) for business documents. Off by default (not part of the
            PII taxonomy). These are regex-based but controlled separately
            from ``use_regex``. ADMIN_CODE (structure/КОСГУ/subconto/stamp
            codes) is additionally reviewed by the LLM layer if enabled — see
            ``review.py`` — since its format alone doesn't prove sensitivity.
        use_llm: Append the local-LLM gap-filler. Requires LM Studio / Ollama.
        llm_config: Optional :class:`anonymizer.llm.LLMConfig`.
        use_review: Add the LLM review layer (see ``review.py``): double-checks
            the final span list against context and reverts obvious detector
            mistakes (common words, product names, legal abbreviations...)
            before placeholders are assigned. Requires LM Studio / Ollama.
        review_config: Optional :class:`anonymizer.review.ReviewConfig`.
        custom_terms: User glossary of always-mask terms (see ``glossary.py``)
            — abbreviations/nicknames no NER/LLM would recognize as sensitive
            on their own. ``"auto"`` (default) loads
            ``anonymizer/custom_terms.txt`` if that file exists, silently
            skipped if it doesn't. Pass ``None`` to disable, a path to use a
            different file, or an iterable of
            :class:`anonymizer.glossary.GlossaryEntry` directly. Always
            trusted — bypasses the noise filters and the LLM review layer,
            since a human curated the list rather than a model scoring it.

    Returns:
        A configured :class:`Anonymizer`.
    """
    detectors: list[Detector] = list(DEFAULT_DETECTORS) if use_regex else []
    second_pass: list[Detector] = []
    if corporate:
        detectors.extend(CORPORATE_DETECTORS)
    if custom_terms is not None:
        from .glossary import DEFAULT_GLOSSARY_PATH, GlossaryDetector, load_glossary

        if custom_terms == "auto":
            entries = load_glossary(DEFAULT_GLOSSARY_PATH)
        elif isinstance(custom_terms, (str, Path)):
            entries = load_glossary(custom_terms)
        else:
            entries = tuple(custom_terms)
        if entries:
            detectors.append(GlossaryDetector(entries))
    if use_ner:
        if ner_backend == "gliner":
            from .gliner_ner import GLiNERDetector

            detectors.append(GLiNERDetector(gliner_config))
        elif ner_backend == "natasha":
            from .ner import NatashaDetector

            detectors.append(NatashaDetector(include_org=include_org))
        else:
            raise ValueError(f"Unknown ner_backend: {ner_backend!r}")
    if use_llm:
        from dataclasses import replace

        from .llm import LLMConfig, LLMDetector

        cfg = llm_config or LLMConfig()
        if corporate:  # the LLM (not regex) handles organizations and money sums
            cfg = replace(cfg, allowed_labels=cfg.allowed_labels | {"ORG", "AMOUNT"})
        llm_detector = LLMDetector(cfg)
        detectors.append(llm_detector)
        if use_second_pass:  # leak check re-uses the same LLM detector
            second_pass.append(llm_detector)

    review_cfg = None
    if use_review:
        from .review import ReviewConfig

        review_cfg = review_config or ReviewConfig()
    return Anonymizer(
        detectors, review_config=review_cfg, second_pass_detectors=second_pass
    )


_default = Anonymizer()


def anonymize(text: str) -> AnonymizationResult:
    """Anonymize text with the default regex-only engine (no NER, no model load)."""
    return _default.anonymize(text)
