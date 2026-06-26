"""Regex detectors for structured Russian PII.

These cover the format-deterministic entity types where rules beat ML:
contacts (email, url, phone, ip) and Russian identity documents (passport,
inn, snils, oms, credit card, driver license, military id, birth certificate).

Document-number detectors are *keyword-anchored*: they look for a trigger word
("паспорт", "ИНН", "СНИЛС"...) and capture only the number that follows, so the
trigger word itself stays in the text. This keeps precision high — a bare
4-digit run is not flagged as a passport unless the context says so.

Names and addresses are intentionally NOT handled here; they are fuzzy and
belong to the NER detector added later.
"""

from __future__ import annotations

import re
from typing import Iterable, Protocol

from .spans import Span


class Detector(Protocol):
    """Anything that can find spans in text."""

    def find(self, text: str) -> list[Span]: ...


class RegexDetector:
    """Detector backed by a single compiled regular expression.

    Args:
        label: Entity label assigned to every match.
        pattern: Regex source string.
        group: Capture group whose span is reported. ``0`` is the whole match;
            use a named/numbered group to redact only part of a keyword-anchored
            match (e.g. the digits after "ИНН").
        flags: Extra regex flags (``IGNORECASE | UNICODE`` are always applied).
        strip: Characters trimmed from both ends of each match before reporting.
    """

    def __init__(
        self,
        label: str,
        pattern: str,
        *,
        group: int | str = 0,
        flags: int = 0,
        strip: str = " \t.,;:!?)(»«\"'=|/\\_-",
    ) -> None:
        self.label = label
        self.group = group
        self._strip = strip
        self._re = re.compile(pattern, flags | re.IGNORECASE | re.UNICODE)

    def find(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for m in self._re.finditer(text):
            start, end = m.span(self.group)
            if start < 0:  # group did not participate in the match
                continue
            start, end = _trim(text, start, end, self._strip)
            if end <= start:
                continue
            spans.append(Span(start, end, self.label, text[start:end], source="regex"))
        return spans


def _trim(text: str, start: int, end: int, chars: str) -> tuple[int, int]:
    while start < end and text[start] in chars:
        start += 1
    while end > start and text[end - 1] in chars:
        end -= 1
    return start, end


# --- Patterns -------------------------------------------------------------

# Keyword fragments reused across document detectors.
_SEP = r"[ \t.\-=№:]*"  # noisy separators seen in the benchmark (space, dash, =)

# One noisy inter-digit separator, and a separator-tolerant digit run ("blob").
# The benchmark deliberately injects ". : ; | \ / _" and spaces between digits.
_DIGSEP = r"[ \t.\-:;=|/\\_]"
_BLOB = r"\d(?:" + _DIGSEP + r"*\d)*"

# Email tolerant of spaces around "@" and "." (benchmark: "n . makarov@aol . com").
EMAIL = RegexDetector(
    "EMAIL",
    r"[A-Za-z0-9_%+\-]+(?:\s?\.\s?[A-Za-z0-9_%+\-]+)*"
    r"\s?@\s?[A-Za-z0-9\-]+(?:\s?\.\s?[A-Za-z0-9\-]+)*\s?\.\s?[A-Za-z]{2,}",
)

# Common TLDs used to recognize bare domains (no http/www), e.g. "lamoda.ru/login".
_TLD = (
    r"ru|рф|com|org|net|io|info|biz|edu|gov|me|app|dev|co|tv|pro|online|site|"
    r"shop|store|ua|by|kz|de|uk|us|fr|cn"
)
URL = RegexDetector(
    "URL",
    r"https?://[^\s,;()<>«»\"']+"
    r"|www\s?\.\s?[A-Za-z0-9\-]+(?:\s?\.\s?[A-Za-z0-9\-]+)+"
    # bare domain with a known TLD + optional path/query (spaces around dots ok)
    r"|(?<![@\w])[A-Za-z0-9\-]+(?:\s?\.\s?[A-Za-z0-9\-]+)*\s?\.\s?(?:" + _TLD + r")"
    r"(?:\s?/[^\s,;«»()<>\"']*)?(?![A-Za-z0-9])",
)

IP_ADDRESS = RegexDetector(
    "IP_ADDRESS",
    r"""
    (?<![\w.:])
    (?:
        (?:\d{1,3}\.){3}\d{1,3}                       # IPv4
      | (?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}    # IPv6 (>=4 groups; not HH:MM:SS)
    )
    (?![\w.:])
    """,
    flags=re.VERBOSE,
)

CREDIT_CARD = RegexDetector(
    "CREDIT_CARD",
    r"(?<!\d)\d{4}(?:[ \t\-]+\d{4}){3}(?!\d)",
)

# RU phone. Deliberately NOT matching bare space-separated digit runs (those
# are usually document numbers); a phone must be signalled by a "+", a leading
# 8/7 country code, parentheses, or dash-grouping.
PHONE = RegexDetector(
    "PHONE",
    r"""
    (?<![\w+])
    (?:
        \+\d{1,3}[ \t\-]*\(?\d{1,4}\)?(?:[ \t\-]*\d{2,4}){2,5}   # +7..., +81 96 2008 6712
      | [78][ \t\-]*\(?\d{3,4}\)?(?:[ \t\-]*\d{2,4}){2,4}        # 8(812)4095642, 8-998-237-66-62
      | \(?\d{3}\)?-\d{2,3}-\d{2}-\d{2}                          # 861-296-11-12 (dash-locked)
    )
    (?![\w])
    """,
    flags=re.VERBOSE,
)

# --- Keyword-anchored Russian documents -----------------------------------
#
# Pattern: trigger word, then up to a short non-digit gap (lets a few words sit
# between the keyword and the number, e.g. "полис единого образца 5678..."),
# then a separator-tolerant digit blob. We capture only the blob.

_GAP = r"\D{0,25}?"  # short non-greedy gap of non-digit chars after a keyword

INN = RegexDetector(
    "INN",
    r"(?:ИНН|налогоплательщик\w*)" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

SNILS_KW = RegexDetector(
    "SNILS",
    r"СНИЛС\w*" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

# SNILS canonical grouping (3-3-3 2) even without a keyword.
SNILS_FMT = RegexDetector(
    "SNILS",
    r"(?<!\d)\d{3}[.\- ]\d{3}[.\- ]\d{3}[.\- ]?\d{2}(?!\d)",
)

OMS_KW = RegexDetector(
    "OMS",
    r"(?:по{1,2}лис|ОМС)" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

# Roman series + 2 cyrillic letters + 6 digits: "II - АВ 123456", "II+АВ+123456"
BIRTH_CERTIFICATE = RegexDetector(
    "BIRTH_CERTIFICATE",
    r"[IVXLC]{1,4}[ \t\-+]*[А-ЯЁ]{2}[ \t\-+]*\d{6}",
)

# Birth certificate by keyword + roman/letters/number, more separator-tolerant.
BIRTH_CERTIFICATE_KW = RegexDetector(
    "BIRTH_CERTIFICATE",
    r"свидетельств\w*\s+о\s+рождени\w*" + _GAP
    + r"([IVXLC]{1,4}" + _DIGSEP + r"*[А-ЯЁ]{1,2}" + _DIGSEP + r"*\d{6})",
    group=1,
)


class BirthCertificateDetector:
    """Detect birth-certificate series and (often split) number.

    Russian birth-certificate series is a roman numeral + 2 cyrillic letters
    ("II - КЗ"), and the 6-digit number is frequently a separate span after the
    word "номер": "серия III - АГ, номер 112233" / "I - МЮ, а номер — 012345".
    The roman+cyrillic shape is distinctive enough to flag on its own; the number
    is captured when it follows the series within a short window.
    """

    _SERIES = r"[IVX]{1,4}\s?[\-+/]?\s?[А-ЯЁ]{2}"
    _RE = re.compile(
        r"(?<![0-9A-Za-zА-Яа-яЁё])(" + _SERIES + r")"
        r"(?:.{0,25}?(?:[Нн]омер|№)\s*[—:\-]*\s*(\d{6}))?",
        re.UNICODE,
    )

    def find(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for m in self._RE.finditer(text):
            for grp in (1, 2):
                if m.group(grp):
                    s, e = m.span(grp)
                    spans.append(
                        Span(s, e, "BIRTH_CERTIFICATE", text[s:e], source="regex")
                    )
        return spans


BIRTH_CERTIFICATE_SPLIT = BirthCertificateDetector()

# Military id: 2 cyrillic letters + 6-7 digits, with messy separators. Not
# preceded by a roman numeral (that's a birth certificate series).
MILITARY_FMT = RegexDetector(
    "MILITARY_ID",
    r"(?<![А-ЯЁA-Z])(?<![IVXLC] )[А-ЯЁ]{2}[ \t\-_()]*\d(?:[\d \t\-_()]{4,6}\d)(?!\d)",
)

# Military id by keyword, allowing a split "серия МЗ, номер 0045678".
MILITARY_KW = RegexDetector(
    "MILITARY_ID",
    r"воен\w*\s+билет\w*" + _GAP + r"([А-ЯЁ]{2}\D{0,12}?\d{6,7})",
    group=1,
)

# Surname + initials (and reverse): "Носов Д.В.", "Д.В. Носов", "Стрельцов И. И."
# Very common in Russian documents (signatures, "Ответственный: ..."), often
# missed by NER. Distinctive enough to flag directly.
PERSON_INITIALS = RegexDetector(
    "PERSON",
    r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.(?!\w)"
    r"|(?<!\w)[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]+",
)

# Full name anchored on a Russian patronymic, in ANY grammatical case — catches
# declined full names that NER/LLM sometimes miss ("Дроновой Екатериной
# Сергеевной", "Громовой Оксане Владимировне"). The patronymic suffix
# (-ович/-евич/-овна/-евна + case endings) makes it high-precision; we grab the
# 1-2 preceding capitalized words (surname + given name).
_PATRONYMIC = r"[А-ЯЁ][а-яё]+(?:вич(?:[ауеё]|ем)?|вн[аеоуы]й?)"
PERSON_PATRONYMIC = RegexDetector(
    "PERSON",
    r"(?:[А-ЯЁ][а-яё]+\s+){1,2}" + _PATRONYMIC + r"(?![а-яё])",
)


class SeriesNumberDetector:
    """Detect "серия NN NN ... номер NNNNNN" runs and label by local context.

    Passport and driver-licence numbers share the exact 4+6-digit "серия/номер"
    shape; only nearby words tell them apart. This scans for the shape and picks
    the label from a context window, defaulting to PASSPORT.
    """

    _RE = re.compile(
        r"(?:сери[ияю]|паспорт\w*)\s*[№:\-]*\s*(\d{2}[ \t]?\d{2})"
        r"(?:\D{0,18}?(?:номер|№)\s*[№:\-]*\s*(\d{3}[ \t]?[-]?[ \t]?\d{3}|\d{6}))?",
        re.IGNORECASE | re.UNICODE,
    )
    _DRIVER = re.compile(r"водительс|удостоверени|\bВУ\b|\bправ", re.IGNORECASE | re.UNICODE)

    def find(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for m in self._RE.finditer(text):
            start = m.start(1)
            end = m.end(2) if m.group(2) else m.end(1)
            window = text[max(0, start - 60) : end + 20]
            label = "DRIVER_LICENSE" if self._DRIVER.search(window) else "PASSPORT"
            s, e = _trim(text, start, end, " \t.,;:!?)(»«\"'=|/\\_-")
            if e > s:
                spans.append(Span(s, e, label, text[s:e], source="regex"))
        return spans


SERIES_NUMBER = SeriesNumberDetector()


# --- Corporate / business entities (optional layer) -----------------------
# Not part of the PII benchmark taxonomy; enable for business documents where
# money, contract numbers and event dates are sensitive (see anonymizer.skill).

_AMOUNT_NUM = r"\d[\d.,  ]*"
_SCALE = r"(?:тысяч[а-я]*|тыс\.?|миллиард[а-я]*|млрд\.?|миллион[а-я]*|млн\.?)"
_CUR = r"(?:рубл[а-я]*|руб\.?|₽|долл[а-я]*|\$|евро)"

AMOUNT = RegexDetector(
    "AMOUNT",
    _AMOUNT_NUM + _SCALE + r"\s*" + _CUR + r"?"          # 4,7 миллиарда рублей
    + r"|" + _AMOUNT_NUM + r"\s*" + _CUR                  # 500 рублей
    + r"|" + r"\d[\d.,]*\s*%(?:\s*годовых)?"              # 18,5% годовых
    + r"|" + _AMOUNT_NUM + r"(?:человек|сотрудник[а-я]*|чел\.?)",  # 1 200 человек
)

# Contract/document reference numbers: "№ ЧЕБ-2026-01", "№ 47-П". Requires a
# letter or dash in the token so plain "№ 1" (protocol number) is not grabbed.
CONTRACT = RegexDetector(
    "CONTRACT",
    r"№\s*(?=[^\s]*[A-Za-zА-Яа-яЁё\-])[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-/_]{2,}",
)

_MONTH = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
DATE = RegexDetector(
    "DATE",
    r"\b\d{1,2}\s+" + _MONTH + r"(?:\s+\d{4})?(?:\s*(?:года|г\.?))?"  # 1 апреля 2026 года
    + r"|" + _MONTH + r"\s+\d{4}"                                     # июнь 2026
    + r"|" + r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b",                        # 12.03.2015
)

# Organization with a legal form + quoted name: ООО «Чебурашка-Логистика»,
# ГК «Форус», ООО «ТаможняДаётДобро». Safe (the legal form anchors it).
ORG_LEGAL = RegexDetector(
    "ORG",
    r"(?:ООО|ОАО|ЗАО|ПАО|АО|ГК|НКО|АНО|ИП)\s*[«\"][^»\"\n]{1,60}[»\"]",
)

# File names with an extension: «Управленка_2026.xlsx», report.docx
FILE = RegexDetector(
    "FILE",
    r"[«\"]?[A-Za-zА-Яа-яЁё0-9_\-]+\.(?:xlsx?|docx?|pdf|csv|txt|pptx?|zip|rar|jpg|png)[»\"]?",
)

CORPORATE_DETECTORS: tuple[Detector, ...] = (AMOUNT, CONTRACT, DATE, ORG_LEGAL, FILE)


# Order matters only as a default registry; overlap resolution decides winners.
DEFAULT_DETECTORS: tuple[Detector, ...] = (
    EMAIL,
    URL,
    IP_ADDRESS,
    CREDIT_CARD,
    INN,
    SNILS_KW,
    SNILS_FMT,
    OMS_KW,
    SERIES_NUMBER,
    BIRTH_CERTIFICATE,
    BIRTH_CERTIFICATE_KW,
    BIRTH_CERTIFICATE_SPLIT,
    MILITARY_FMT,
    MILITARY_KW,
    PERSON_INITIALS,
    PERSON_PATRONYMIC,
    PHONE,
)

# Higher weight wins when spans overlap. Specific document/contact types beat
# the broad PHONE/CREDIT_CARD digit runs that can swallow them.
DEFAULT_PRIORITY: dict[str, int] = {
    "EMAIL": 90,
    "URL": 90,
    "IP_ADDRESS": 90,
    "PASSPORT": 80,
    "SNILS": 80,
    "INN": 80,
    "OMS": 80,
    "DRIVER_LICENSE": 80,
    "MILITARY_ID": 80,
    "BIRTH_CERTIFICATE": 80,
    "CREDIT_CARD": 70,
    "CONTRACT": 65,
    "ORG": 62,
    "FILE": 58,
    "AMOUNT": 55,
    "DATE": 45,
    "PHONE": 40,
    # NER labels (added later) sit between contacts and structured docs.
    "FIRST_NAME": 60,
    "LAST_NAME": 60,
    "MIDDLE_NAME": 60,
    "COUNTRY": 50,
    "REGION": 50,
    "DISTRICT": 50,
    "CITY": 50,
    "STREET": 50,
    "HOUSE": 50,
}


def run_detectors(text: str, detectors: Iterable[Detector]) -> list[Span]:
    """Collect spans from every detector into one flat list (may overlap)."""
    spans: list[Span] = []
    for detector in detectors:
        spans.extend(detector.find(text))
    return spans


# --- Job-title / generic-word filter --------------------------------------
# NER (especially GLiNER at a low threshold) tends to label job titles as
# PERSON/ORG. Titles should stay in the text — they give the reader context and
# are not personal data. We drop a PERSON/ORG span when it consists only of
# role/modifier words (and contains at least one role headword).

_ROLE_STEMS = (
    "директор", "бухгалтер", "менеджер", "архитектор", "аналитик", "начальник",
    "руководител", "специалист", "инженер", "заместител", "помощни", "секретар",
    "кладовщик", "логист", "экономист", "юрист", "кассир", "оператор",
    "программист", "разработчик", "консультант", "продав", "водител", "технолог",
    "маркетолог", "снабжен", "диспетчер", "бригадир", "мастер", "отдел", "служб",
    "департамент", "директора", "сотрудник", "персонал",
)
_MODIFIER_STEMS = (
    "генеральн", "финансов", "главн", "старш", "младш", "ведущ", "исполнительн",
    "коммерческ", "техническ", "региональн", "функциональн", "системн", "ит",
    "it", "по", "продаж", "закуп", "логистик", "кадр", "склад", "и", "новой",
    "новый", "управленческ",
)
_TITLE_STEMS = _ROLE_STEMS + _MODIFIER_STEMS

# Standalone non-PII words (table headers etc.) NER sometimes mislabels.
_GENERIC_WORDS = frozenset({
    "должность", "должности", "этап", "срок", "сроки", "описание", "участник",
    "участники", "ответственный", "заказчик", "исполнитель", "тема", "повестка",
})

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")


# Software / methodology terms to preserve (per anonymizer.skill): product names
# and standard abbreviations carry meaning and are not personal/commercial data.
_SOFTWARE_MARKERS = (
    "1с", "1c", "excel", "word", "outlook", "foxpro", "контур", "диадок",
    "sap", "oracle", "битрикс", "sku", "ebitda", "гтд", "эдо", "edi", "erp",
    "crm", "bi", "sql", "1с:erp",
)


def is_software(text: str) -> bool:
    """True if ``text`` names a software product / standard term to keep."""
    low = text.strip().lower()
    return any(m in low for m in _SOFTWARE_MARKERS)


def is_job_title(text: str) -> bool:
    """True if ``text`` is a job title / generic word that should not be masked."""
    low = text.strip().lower()
    if low in _GENERIC_WORDS:
        return True
    tokens = _WORD_RE.findall(low)
    if not tokens:
        return False
    if not all(any(tok.startswith(s) for s in _TITLE_STEMS) for tok in tokens):
        return False
    return any(tok.startswith(s) for tok in tokens for s in _ROLE_STEMS)


def is_non_pii(text: str) -> bool:
    """True if a PERSON/ORG span should be left unmasked (title or software)."""
    return is_job_title(text) or is_software(text)


# NER/LLM labels are "soft": models sometimes tag pronouns or function words
# ("я", "он", "это") as PERSON/LOCATION/ORG. Masking those is catastrophic with
# mask_all_occurrences (every "я" inside "сегодня", "друзья" gets replaced), so
# we drop such spans before masking.
_SOFT_LABELS = frozenset({
    "PERSON", "ORG", "LOCATION", "CITY", "REGION", "COUNTRY", "DISTRICT",
    "STREET", "HOUSE", "ADDRESS", "FIRST_NAME", "LAST_NAME", "MIDDLE_NAME",
})

# Russian pronouns / function words that must never be treated as entities.
_ENTITY_STOPWORDS = frozenset({
    "я", "ты", "он", "она", "оно", "они", "мы", "вы",
    "меня", "тебя", "его", "её", "ее", "нас", "вас", "их",
    "мне", "тебе", "ему", "ей", "нам", "вам", "им", "них", "нему", "нею", "ней",
    "мной", "мною", "тобой", "тобою", "нами", "вами", "ими",
    "себя", "себе", "собой", "собою",
    "это", "этот", "эта", "эти", "тот", "та", "те", "то", "той", "том", "этом",
    "вот", "тут", "там", "здесь", "тогда", "сейчас", "теперь",
    "да", "нет", "ну", "же", "бы", "ли", "не", "ни", "уж",
    "и", "а", "но", "или", "что", "чтобы", "как", "так", "чем",
    "кто", "где", "когда", "куда", "зачем", "почему",
    "мой", "твой", "наш", "ваш", "свой", "весь", "вся", "всё", "все",
    "ага", "угу", "вообще", "просто", "значит",
})


def is_stopword_entity(text: str, label: str) -> bool:
    """True if a soft (NER/LLM) span is a pronoun/function word, not real PII."""
    if label not in _SOFT_LABELS:
        return False
    low = text.strip().lower()
    if len(low) <= 1:
        return True
    return low in _ENTITY_STOPWORDS


_DECLENSION_LABELS = frozenset({"PERSON", "LOCATION", "ORG"})


def propagate_declensions(text: str, spans: list[Span]) -> list[Span]:
    """Find declined case-forms of already-detected single-word entities.

    NER/LLM may catch "Лента"/"Москва" (nominative) but miss "Лентой"/"Москве"
    (oblique cases), especially on large chunks. For each detected single-word
    PERSON/LOCATION/ORG, this scans for `<stem><russian-ending>` occurrences and
    returns them as NEW spans (each keeps its own surface text, so the mapping
    stays reversible). Returns only spans not overlapping the existing ones.
    """
    existing = [(s.start, s.end) for s in spans]
    seen_stems: set[str] = set()
    extra: list[Span] = []
    for s in spans:
        if s.label not in _DECLENSION_LABELS:
            continue
        val = s.text.strip()
        if " " in val or len(val) < 5 or not val[0].isupper():
            continue
        stem = val[:-1]
        key = (s.label, stem.casefold())
        if key in seen_stems or len(stem) < 4:
            continue
        seen_stems.add(key)
        pattern = re.compile(
            r"(?<![А-Яа-яЁёA-Za-z])" + re.escape(stem) + r"[а-яё]{0,3}(?![А-Яа-яЁёA-Za-z])"
        )
        for m in pattern.finditer(text):
            a, b = m.start(), m.end()
            if any(a < e and st < b for st, e in existing):
                continue
            if any(a < e2 and st2 < b for st2, e2 in ((x.start, x.end) for x in extra)):
                continue
            extra.append(Span(a, b, s.label, text[a:b], source="morph"))
    return extra
