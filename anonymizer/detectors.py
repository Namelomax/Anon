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
        flags: Extra regex flags (``UNICODE`` is always applied).
        ignorecase: If True (default) ``re.IGNORECASE`` is added — right for
            keyword-anchored document detectors. Set False for case-sensitive
            patterns (e.g. Russian name detectors that rely on capitalization to
            tell a name from a common lowercase word like «условно»).
        strip: Characters trimmed from both ends of each match before reporting.
    """

    def __init__(
        self,
        label: str,
        pattern: str,
        *,
        group: int | str = 0,
        flags: int = 0,
        ignorecase: bool = True,
        strip: str = " \t.,;:!?)(»«\"'=|/\\_-",
    ) -> None:
        self.label = label
        self.group = group
        self._strip = strip
        if ignorecase:
            flags |= re.IGNORECASE
        self._re = re.compile(pattern, flags | re.UNICODE)

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
# Cyrillic local parts / domains / .рф are legal and appear in RU contracts.
# ВАЖНО: разделитель [ \t]?, а не \s? — иначе матч перепрыгивает перенос строки
# и заглатывает следующее слово («…edu.\nСогласовано»).
EMAIL = RegexDetector(
    "EMAIL",
    r"[A-Za-zА-Яа-яЁё0-9_%+\-]+(?:[ \t]?\.[ \t]?[A-Za-zА-Яа-яЁё0-9_%+\-]+)*"
    r"[ \t]?@[ \t]?[A-Za-zА-Яа-яЁё0-9\-]+(?:[ \t]?\.[ \t]?[A-Za-zА-Яа-яЁё0-9\-]+)*"
    r"[ \t]?\.[ \t]?[A-Za-zА-Яа-яЁё]{2,}",
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
    # Not followed by a word char/colon, or a "." that continues the number
    # (an extra octet). A plain sentence-ending "." must NOT block the match —
    # "...192.168.1.45." (very common phrasing) was leaking completely before
    # this fix because the old (?![\w.:]) rejected ANY trailing dot.
    (?![\w:])(?!\.\d)
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
      | \(\d{3,5}\)[ \t]*\d{2,3}(?:[ \t\-]\d{2,3}){1,2}          # (3952) 405-000 — городской с кодом
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

# The keyword must be a standalone word: without the boundaries, IGNORECASE
# makes "ИНН" match the "инн" inside words like "дл-инн-ое"/"стар-инн-ый" and
# then grab any nearby digit ("(5 и более слов)" -> a bogus INN "5").
INN = RegexDetector(
    "INN",
    r"(?<![А-Яа-яЁёA-Za-z])(?:ИНН(?![А-Яа-яЁёA-Za-z])|налогоплательщик\w*)"
    + _GAP + r"(" + _BLOB + r")",
    group=1,
)

SNILS_KW = RegexDetector(
    "SNILS",
    r"СНИЛС\w*" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

# SNILS canonical grouping (3-3-3 2) even without a keyword. Separators up to
# 3 chars: documents often have double spaces before the check digits.
SNILS_FMT = RegexDetector(
    "SNILS",
    r"(?<!\d)\d{3}[.\- ]{1,3}\d{3}[.\- ]{1,3}\d{3}[.\- ]{0,3}\d{2}(?!\d)",
)

# Word boundaries are required: with IGNORECASE a bare "ОМС" matches the "омс"
# inside words like "прОМСвязь", and then grabs any nearby digits as a policy
# number. "поо?лис" covers the common "полис"/"поолис" typo forms.
OMS_KW = RegexDetector(
    "OMS",
    r"(?<![А-Яа-яЁёA-Za-z])(?:поо?лис\w*|ОМС)(?![А-Яа-яЁёA-Za-z])"
    + _GAP + r"(" + _BLOB + r")",
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

# Military id: 2 UPPERCASE cyrillic letters + 6-7 digits, with messy separators.
# (?-i:...) keeps the letters uppercase-only despite the detector's global
# IGNORECASE — otherwise lowercase function words like "до"/"от 500 000" match
# and money gets mislabeled as a military id. Not preceded by a roman numeral
# (that's a birth-certificate series).
MILITARY_FMT = RegexDetector(
    "MILITARY_ID",
    r"(?<![А-ЯЁA-Z])(?<![IVXLC] )(?-i:[А-ЯЁ]{2})[ \t\-_()]*\d(?:[\d \t\-_()]{4,6}\d)(?!\d)",
)

# Military id by keyword, allowing a split "серия МЗ, номер 0045678".
# (?-i:...) keeps the series letters uppercase-only: without it, IGNORECASE
# lets the non-greedy gap latch onto a lowercase substring INSIDE "серия"
# itself (matching "ри" as the "series"), which produces a span starting
# mid-word — _apply_all_occurrences then refuses to splice a placeholder into
# the middle of a word, so the whole id (including the real digits) silently
# stays unmasked in the output.
MILITARY_KW = RegexDetector(
    "MILITARY_ID",
    r"воен\w*\s+билет\w*" + _GAP + r"((?-i:[А-ЯЁ]{2})\D{0,12}?\d{6,7})",
    group=1,
)


class MilitarySeriesDetector:
    """Detect a 2-letter military series + (often split) 6-7 digit number.

    The benchmark frequently writes military ids as a bare "серия XX … номер
    NNNNNNN" WITHOUT the «военный билет» keyword nearby, sometimes quoted
    (»АА«), with the series and number as separate spans. Case-sensitive: the
    series is two UPPERCASE Cyrillic letters (distinguishes from lowercase
    function words). A roman numeral before the letters means a birth
    certificate, not a military id, so it naturally doesn't match (roman = Latin
    I/V/X, not in the Cyrillic class).
    """

    _RE = re.compile(
        r"сери[ияю]\s*[№:\-]*\s*[«»\"]?\s*([А-ЯЁ]{2})\s*[«»\"]?"
        r"(?:.{0,20}?(?:номер|№)\s*[№:\-]*\s*[«»\"]?\s*(\d{6,7})\b)?",
        re.UNICODE,  # NOT IGNORECASE — series is uppercase
    )

    def find(self, text: str) -> list[Span]:
        spans: list[Span] = []
        for m in self._RE.finditer(text):
            for grp in (1, 2):
                if m.group(grp):
                    s, e = m.span(grp)
                    spans.append(Span(s, e, "MILITARY_ID", text[s:e], source="regex"))
        return spans


MILITARY_SERIES = MilitarySeriesDetector()

# Surname + initials (and reverse): "Носов Д.В.", "Д.В. Носов", "Стрельцов И. И."
# Very common in Russian documents (signatures, "Ответственный: ..."), often
# missed by NER. Distinctive enough to flag directly.
PERSON_INITIALS = RegexDetector(
    "PERSON",
    r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.(?!\w)"
    r"|(?<!\w)[А-ЯЁ]\.\s?[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]+",
    ignorecase=False,  # capitalization distinguishes a name from "т.д. Слово"
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
    # Case-sensitive: without this, IGNORECASE makes the «-вно/-вне» suffix match
    # common lowercase adverbs («условно», «оперативно», «всё равно», «вовне»).
    ignorecase=False,
)


class SeriesNumberDetector:
    """Detect "серия NN NN ... номер NNNNNN" runs and label by local context.

    Passport and driver-licence numbers share the exact 4+6-digit "серия/номер"
    shape; only nearby words tell them apart. This scans for the shape and picks
    the label from a context window, defaulting to PASSPORT.
    """

    _RE = re.compile(
        # Триггер: «серия»/«паспорт», либо «водительское удостоверение» без
        # слова «серия» перед номером — частая формулировка в кадровых
        # документах («Водительское удостоверение 66 14 887766»).
        r"(?:сери[ияю]|паспорт\w*|водительск\w*\s+удостоверени\w*)\s*[№:\-]*\s*(\d{2}[ \t]?\d{2})"
        # Номер: либо после слова «номер»/«№», либо просто следом за серией —
        # «паспорт 2518 445566» без ключевого слова (иначе 6 цифр утекали).
        r"(?:(?:\D{0,18}?(?:номер|№)\s*[№:\-]*\s*|[ \t]+)(\d{3}[ \t]?[-]?[ \t]?\d{3}|\d{6}))?",
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

# Сумма прописью в скобках между числом и валютой — стандарт договоров:
# «49 500 (сорок девять тысяч пятьсот) рублей 00 копеек».
_SPELLED = r"(?:\([^)\n]{3,90}\)\s*)?"

AMOUNT = RegexDetector(
    "AMOUNT",
    _AMOUNT_NUM + _SCALE + r"\s*" + _CUR + r"?"          # 4,7 миллиарда рублей
    + r"|" + _AMOUNT_NUM + _SPELLED + _CUR                # 500 рублей; 49 500 (…) рублей
    + r"(?:[,\s]*\d{2}\s*копе[а-я]*)?"                    # …, 00 копеек
    # Percentages: only financial ones — annual rates or precise decimals.
    # Plain integer percents ("точность 97%", "30—40% времени") are NOT money.
    + r"|" + r"\d[\d.,]*\s*%\s*годовых"                   # 18% годовых
    + r"|" + r"\d+,\d+\s*%",                              # 18,5% (decimal share)
)

# Contract/document reference numbers: "№ ЧЕБ-2026-01", "№ 47-П". Requires a
# letter or dash in the token so plain "№ 1" (protocol number) is not grabbed.
# The negative lookahead skips template placeholders ("Протокол № номер от дата")
# and bare trigger words so they aren't masked as a contract id.
CONTRACT = RegexDetector(
    "CONTRACT",
    r"№\s*(?!(?:номер|дата|договор\w*|приказ\w*|протокол\w*)(?![A-Za-zА-Яа-яЁё0-9\-/_]))"
    r"(?=[^\s]*[A-Za-zА-Яа-яЁё\-])[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-/_]{2,}",
)

_MONTH = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
DATE = RegexDetector(
    "DATE",
    # «12» июня 2026 года / 12 апреля 2026 г. / 1 апреля.
    # День может быть в кавычках («12», "31") — тогда между днём и месяцем стоит
    # закрывающая кавычка, из-за чего прежний паттерн «\d+ месяц» день не ловил и
    # он утекал. Кавычки вокруг дня теперь опциональны и входят в один спан;
    # хвост «года/г.» подхватывается и в форме «месяц год».
    r"(?<!\d)[«\"]?\d{1,2}[»\"]?\s+" + _MONTH + r"(?:\s+\d{4})?(?:\s*(?:года|г\.?))?"
    + r"|" + _MONTH + r"\s+\d{4}(?:\s*(?:года|г\.?))?"               # июнь 2026 (года)
    # 12.03.2015, в т.ч. с приклеенным «г.» без пробела («30.09.2024г.») — из-за
    # него \b на границе цифра/буква не срабатывал и дата не маскировалась.
    + r"|" + r"\b\d{1,2}\.\d{1,2}\.\d{2,4}(?:\s*г\.?)?(?![0-9])",
)

# Organization with a legal form + quoted name: ООО «Чебурашка-Логистика»,
# ГК «Форус», ООО «ТаможняДаётДобро». Safe (the legal form anchors it).
ORG_LEGAL = RegexDetector(
    "ORG",
    r"(?:ООО|ОАО|ЗАО|ПАО|АО|ГК|НКО|АНО|ИП)\s*[«\"][^»\"\n]{1,60}[»\"]",
)

# --- Реквизиты договоров (страница реквизитов сторон) ----------------------
# Форматно-детерминированные идентификаторы: правила здесь надёжнее ML.

# КПП: ровно 9 цифр после ключевого слова.
KPP = RegexDetector(
    "KPP",
    r"(?<![А-Яа-яЁёA-Za-z])КПП(?![А-Яа-яЁёA-Za-z])" + _GAP + r"(\d{9})(?!\d)",
    group=1,
)

# ОГРН — 13 цифр, ОГРНИП — 15 цифр (по ключевому слову).
OGRN = RegexDetector(
    "OGRN",
    r"(?<![А-Яа-яЁёA-Za-z])ОГРН(?:ИП)?(?![А-Яа-яЁёA-Za-z])" + _GAP + r"(\d{13}|\d{15})(?!\d)",
    group=1,
)

# БИК банка — 9 цифр (по ключевому слову).
BIK = RegexDetector(
    "BIK",
    r"(?<![А-Яа-яЁёA-Za-z])БИК(?![А-Яа-яЁёA-Za-z])" + _GAP + r"(\d{9})(?!\d)",
    group=1,
)

# ОКПО — 8 или 10 цифр (по ключевому слову).
OKPO = RegexDetector(
    "OKPO",
    r"(?<![А-Яа-яЁёA-Za-z])ОКПО(?![А-Яа-яЁёA-Za-z])" + _GAP + r"(\d{8}|\d{10})(?!\d)",
    group=1,
)

# Банковские счета: расчётный/корреспондентский/лицевой — 20 цифр, допускаем
# пробельную группировку. Ключевые слова: «р/с», «к/с», «расчётный счёт», «счёт №».
BANK_ACCOUNT_KW = RegexDetector(
    "BANK_ACCOUNT",
    r"(?:расч[её]тн\w*|корреспондентск\w*|лицев\w*)?\s*"
    r"(?:сч[её]т\w*|р\s*[/\\.]\s*с|к\s*[/\\.]\s*с)\s*[№:\-]*\s*"
    r"((?:\d[ \t]?){20})(?!\d)",
    group=1,
)

# Голые 20 цифр подряд — в деловом тексте это почти всегда номер счёта.
BANK_ACCOUNT_FMT = RegexDetector(
    "BANK_ACCOUNT",
    r"(?<!\d)\d{20}(?!\d)",
)

# Банки и филиалы. Название банка часто пишут БЕЗ кавычек-формы ООО«…»
# («Банка ВТБ (ПАО)»), а номер филиала — чисто цифрой после «№», которую
# CONTRACT-детектор пропускает (он требует букву). Ловим детерминированно:
#   1) «Филиал/Отделение №N [Банка] <Название> (ПАО/АО)» одним спаном;
#   2) одиночный «Филиал/Отделение №N» неизвестного банка.
# Список банков — редактируемый (можно дополнять прямо здесь).
_BANK_NAMES = (
    r"(?:ВТБ|Сбербанк\w*|Сбербанка|Сбер|Альфа[-\s]?[Бб]анк\w*|Тинькофф|Т[-\s]?Банк|"
    r"Газпромбанк\w*|ГПБ|«?Открытие»?|Райффайзен\w*|Совкомбанк\w*|Росбанк\w*|"
    r"Промсвязьбанк\w*|ПСБ|Россельхозбанк\w*|Уралсиб\w*|МКБ|Юникредит\w*|"
    r"Ак\s?Барс|Хоум\s?Кредит|Ситибанк\w*|Банк\s?Дом\.?РФ|Почта\s?Банк\w*)"
)
_BRANCH = r"(?:филиал\w*|отделени\w*|доп\.?\s*офис\w*)\s*№?\s*\d+"
BANK = RegexDetector(
    "ORG",
    r"(?:" + _BRANCH + r"\s+)?(?:банк[а-я]*\s+)?" + _BANK_NAMES + r"(?:\s*\((?:ПАО|АО|ООО|НКО)\))?"
    + r"|" + _BRANCH,
)

REQUISITES_DETECTORS: tuple[Detector, ...] = (
    KPP, OGRN, BIK, OKPO, BANK_ACCOUNT_KW, BANK_ACCOUNT_FMT, BANK,
)

# --- Внутренние коды учреждения (структура/КОСГУ/субконто/шифр/штамп) ------
# Формат этих кодов не стандартизован (в отличие от ОГРН/ОКПО) — цифры сами по
# себе могут быть как значимым внутренним идентификатором организации, так и
# безобидным номером страницы/пункта. Поэтому, в отличие от остальных
# детекторов этого файла, ADMIN_CODE не считается автоматически безопасным для
# маскировки формата — он единственный из "жёстких" детекторов помечен как
# ``_REVIEWABLE_LABELS`` в review.py: LLM смотрит на код вместе с контекстом и
# решает, оставить маску или вернуть как безобидное число.
ADMIN_CODE_STRUCT_KW = RegexDetector(
    "ADMIN_CODE",
    r"код\s+(?:структур\w*|учреждени\w*|подразделени\w*|бюджетополучател\w*)"
    + _GAP + r"(" + _BLOB + r")",
    group=1,
)

ADMIN_CODE_KOSGU = RegexDetector(
    "ADMIN_CODE",
    r"(?<![А-Яа-яЁёA-Za-z])КОСГУ(?![А-Яа-яЁёA-Za-z])" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

ADMIN_CODE_SUBCONTO = RegexDetector(
    "ADMIN_CODE",
    r"субконт\w*" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

ADMIN_CODE_SHIFR = RegexDetector(
    "ADMIN_CODE",
    r"(?<![А-Яа-яЁёA-Za-z])шифр\w*(?![А-Яа-яЁёA-Za-z])" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

ADMIN_CODE_REG_NUMBER = RegexDetector(
    "ADMIN_CODE",
    r"рег(?:истрационн\w*)?\.?\s*номер\w*" + _GAP + r"(" + _BLOB + r")",
    group=1,
)

# «Штамп ... (И-118):» — код в скобках после слова «штамп», может отделяться
# от ключевого слова длинным названием организации, поэтому окно шире _GAP.
ADMIN_CODE_STAMP = RegexDetector(
    "ADMIN_CODE",
    r"штамп\w*[^()\n]{0,80}\(([A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9\-./]{0,14})\)",
    group=1,
)

ADMIN_CODE_DETECTORS: tuple[Detector, ...] = (
    ADMIN_CODE_STRUCT_KW,
    ADMIN_CODE_KOSGU,
    ADMIN_CODE_SUBCONTO,
    ADMIN_CODE_SHIFR,
    ADMIN_CODE_REG_NUMBER,
    ADMIN_CODE_STAMP,
)

# File names with an extension: «Управленка_2026.xlsx», report.docx,
# Запись_Встреча_2026.mp4 (meeting recordings often embed names/orgs).
FILE = RegexDetector(
    "FILE",
    r"[«\"]?[A-Za-zА-Яа-яЁё0-9_\-]+\.(?:xlsx?|docx?|pdf|csv|txt|pptx?|zip|rar|jpg|jpeg|png"
    r"|mp[34]|wav|m4a|aac|ogg|avi|mkv|mov|webm)[»\"]?",
)

# AMOUNT included: на договорах LLM-слой стабильно пропускает суммы (отзыв
# заказчика — «нигде не убрал цены»), а денежные форматы детерминированы;
# ложные срабатывания дополнительно режет is_money_amount в engine.
CORPORATE_DETECTORS: tuple[Detector, ...] = (
    CONTRACT, DATE, ORG_LEGAL, FILE, AMOUNT, *REQUISITES_DETECTORS, *ADMIN_CODE_DETECTORS,
)


# Order matters only as a default registry; overlap resolution decides winners.
# City / settlement by a keyword abbreviation + a capitalized name — catches
# "г. Иркутск", "пгт. Листвянка", "с. Оёк" that NER sometimes misses in the
# address block of a contract. Keyword-anchored (dot required for 1-letter
# abbreviations) so it stays high-precision — "с Иваном" (preposition) won't
# match because bare "с" needs a following dot.
_CITY_KW = r"(?:г|гор|пгт|дер|пос|с)\.|(?:город|деревн\w+|село|посёлок|посел\w+|станиц\w+)"
# [ \t]* (not \s*) keeps the gap on the same line — a trailing "...2026г."
# from a date must not reach across a paragraph break into the next
# capitalized word. (?-i:[А-ЯЁ]) keeps the name's first letter case-sensitive
# despite the detector's global IGNORECASE — otherwise "20.01.2026г. по ..."
# matches the lowercase "по" as if it were a place name.
CITY = RegexDetector(
    "LOCATION",
    r"(?<![А-Яа-яЁёA-Za-z])(?:" + _CITY_KW + r")[ \t]*(?-i:[А-ЯЁ])[А-Яа-яё\-]+",
)

DEFAULT_DETECTORS: tuple[Detector, ...] = (
    EMAIL,
    URL,
    CITY,
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
    MILITARY_SERIES,
    PERSON_INITIALS,
    PERSON_PATRONYMIC,
    PHONE,
)

# Higher weight wins when spans overlap. Specific document/contact types beat
# the broad PHONE/CREDIT_CARD digit runs that can swallow them.
DEFAULT_PRIORITY: dict[str, int] = {
    # User-curated glossary terms are ground truth, not a guess — should win
    # over any NER/regex span at the same position.
    "CUSTOM_TERM": 95,
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
    "BANK_ACCOUNT": 82,
    "KPP": 80,
    "OGRN": 80,
    # Реквизиты по ключевому слову («БИК:», «ОКПО:») — надёжнее общих
    # цифровых детекторов: должны побеждать MILITARY_ID/PASSPORT на тех же цифрах
    # (иначе 9-значный БИК уходил под метку [MILITARY_ID]).
    "BIK": 85,
    "OKPO": 85,
    "CONTRACT": 65,
    "ORG": 62,
    # Общий класс для добора recall-проходом LLM (см. review.recall_spans),
    # когда конкретный тип не распознан.
    "SENSITIVE": 55,
    "ADMIN_CODE": 60,
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
    low = " ".join(text.split()).casefold()  # collapse nbsp/spaces
    if len(low) <= 1:
        return True
    return low in _ENTITY_STOPWORDS


_MONEY_CUES = (
    "руб", "рубл", "₽", "$", "€", "долл", "евро",
    "млн", "млрд", "тыс", "миллион", "миллиард", "тысяч", "копе", "цент",
)


def is_money_amount(text: str) -> bool:
    """True if an AMOUNT span actually looks monetary.

    The LLM over-tags AMOUNT (dates, deadlines, headcounts, "22 ТС", bare
    numbers). A real money sum carries a currency word/symbol or a scale word
    (млн/млрд/тыс…), or is an annual rate ("18% годовых"). Everything else is
    dropped so AMOUNT means money — dates are still caught by the DATE detector.
    """
    low = text.lower()
    if "%" in low and "годов" in low:
        return True
    return any(cue in low for cue in _MONEY_CUES)


def is_generic_entity(text: str, label: str) -> bool:
    """True for an ORG/LOCATION span that is a lowercase common-noun phrase.

    NER/LLM at low thresholds tag generic nouns as organizations/places
    ("новой системы", "казначейство", "финансового блока", "4 банками"). Real
    proper names carry a capital letter, a quote («…»), or a legal form, so a
    span with NONE of those is dropped. Orgs/places aren't personal data, so this
    is a safe precision win (it won't drop a real ФИО). Latin acronyms with a
    capital (e.g. "IT-инфраструктуры") survive — raise the GLiNER threshold for
    those.
    """
    if label not in ("ORG", "LOCATION"):
        return False
    t = text.strip()
    if "«" in t or "»" in t or '"' in t:
        return False
    return not any(ch.isupper() for ch in t)


# --- Precision noise filter ------------------------------------------------
# Низкопороговый GLiNER + LLM-слой массово размечают как сущности тайм-коды,
# метки дорожек («Спикер 4»), общие слова («человек», «команда», «компания»),
# служебные фразы («это всё равно», «там условно»). Это не ПДн. Удаляем такой
# шум детерминированно, до маскирования.

_TIMECODE_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")
_SPEAKER_RE = re.compile(r"^(?:спикер|speaker|участ\w*|трек|track)\s*[№#]?\s*\d+$", re.IGNORECASE)
_REPEAT_RE = re.compile(r"^(\w{1,6})(?:[\-\s]+\1){1,}$", re.IGNORECASE)

# Общие существительные / доменные термины (по ОСНОВАМ, чтобы ловить падежи),
# которые модели путают с сущностями. startswith-сопоставление токенов.
_NOISE_STEMS = (
    "человек", "люд", "команд", "компани", "клиент", "проект", "систем",
    "ассистент", "нейросет", "модел", "решени", "лаборатори", "пользовател",
    "ведущ", "смежник", "сотрудник", "персонал", "коллег", "ребят", "друзь",
    "заказчик", "исполнител", "искусственн", "интеллект", "данн", "документ",
    "протокол", "встреч", "вопрос", "задач", "инструмент", "процесс",
    "результат", "коммент", "отдел", "служб", "групп", "функционал",
    "сегодн", "завтра", "вчера", "штук", "вариант", "момент", "вещ",
)

# Одиночные общие слова с заглавной (точное совпадение), напр. «Нейросеть».
_COMMON_NOUN_EXACT = frozenset({
    "нейросеть", "ассистент", "компания", "команда", "система", "проект",
    "решение", "интеллект", "пользователь", "ведущий", "заказчик",
    "исполнитель", "документ", "протокол", "встреча", "вопрос", "задача",
})

_PREPOSITIONS = frozenset({
    "у", "в", "во", "на", "с", "со", "к", "ко", "о", "об", "от", "до", "из",
    "по", "за", "для", "при", "над", "под", "про", "без", "из-за", "через",
})

_INTERJECTIONS = frozenset({
    "так", "ну", "вот", "ага", "угу", "эээ", "ммм", "да", "нет", "ок", "окей",
    "блин", "короче", "значит", "типа", "это", "вообще", "просто", "ладно",
})


def _alpha_tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _is_noise_token(tok: str) -> bool:
    """Токен — служебное/общее слово (стоп-слово, предлог, междометие, общий
    термин по основе)."""
    if tok in _ENTITY_STOPWORDS or tok in _PREPOSITIONS or tok in _INTERJECTIONS:
        return True
    return any(tok.startswith(stem) for stem in _NOISE_STEMS)


def is_timecode(text: str) -> bool:
    """True, если строка — это тайм-код ЧЧ:ММ:СС / ММ:СС."""
    return bool(_TIMECODE_RE.match(text.strip()))


def is_noise_span(text: str, label: str) -> bool:
    """Детерминированный фильтр: True, если спан — мусор, а не ПДн.

    Применяется ко ВСЕМ меткам: тайм-коды режутся для любых лейблов (включая
    ошибочные INN/SNILS/LOCATION). Для PERSON/ORG/LOCATION дополнительно режутся
    метки дорожек, общие слова и фразы из служебных слов.
    """
    s = text.strip()
    if not s:
        return True
    if is_timecode(s):  # тайм-код — не ПДн в любой метке
        return True

    low = s.lower()
    toks = _alpha_tokens(s)

    if label == "PERSON":
        if _SPEAKER_RE.match(s):
            return True
        if _REPEAT_RE.match(s):  # «так-так-так»
            return True
        if not s[0].isupper():  # настоящее ФИО начинается с заглавной
            return True
        if len(toks) >= 4:  # слишком длинно для имени — фраза
            return True
        if toks and all(
            t in _ENTITY_STOPWORDS or t in _PREPOSITIONS or t in _INTERJECTIONS
            for t in toks
        ):
            return True
        # По токену, а не по сырой строке: «“Заказчик”» в кавычках из преамбулы
        # договора иначе проскакивал мимо списка и маскировался как PERSON.
        if len(toks) == 1 and toks[0] in _COMMON_NOUN_EXACT:
            return True
        return False

    if label in (
        "ORG", "LOCATION", "CITY", "REGION", "COUNTRY",
        "DISTRICT", "STREET", "HOUSE", "ADDRESS",
    ):
        if _SPEAKER_RE.match(s):
            return True
        if low in _COMMON_NOUN_EXACT:
            return True
        if toks and all(_is_noise_token(t) for t in toks):
            return True
        return False

    return False


def propagate_entity_aliases(text: str, spans: list[Span]) -> list[Span]:
    """Mask standalone mentions derived from already-detected entities.

    Two generic derivations (no word lists):
    * multi-word PERSON («Иванов Никита Петрович») -> each capitalized part
      (>=3 chars) is masked wherever it appears alone («Никита, добрый день»);
    * ORG with a quoted name (ООО «Ромашка») -> the inner name («Ромашка») is
      masked where it appears without the legal form («далее — Ромашка»).

    Fixes the "каталог поверхностей" gap of mask-all: the full form is caught,
    but its bare fragments elsewhere in the document leak. New spans inherit
    the source span's label; overlaps with existing spans are skipped.
    """
    existing = [(s.start, s.end) for s in spans]
    seen: set[tuple[str, str]] = set()
    extra: list[Span] = []

    for s in spans:
        aliases: list[str] = []
        if s.label == "PERSON":
            parts = [
                p for p in re.split(r"[\s,.]+", s.text)
                if len(p) >= 3 and p[0].isupper() and not p.endswith(".")
            ]
            if len(parts) >= 2:  # только у многословных ФИО есть что выделять
                aliases = parts
        elif s.label == "ORG":
            m = re.search(r"[«\"]([^»\"\n]{3,60})[»\"]", s.text)
            if m:
                aliases = [m.group(1).strip()]

        for alias in aliases:
            key = (s.label, alias.casefold())
            if key in seen or alias.casefold() == s.text.strip().casefold():
                continue
            seen.add(key)
            pattern = re.compile(
                r"(?<![А-Яа-яЁёA-Za-z0-9])" + re.escape(alias) + r"(?![А-Яа-яЁёA-Za-z0-9])"
            )
            for m2 in pattern.finditer(text):
                a, b = m2.start(), m2.end()
                if any(a < e and st < b for st, e in existing):
                    continue
                if any(a < e2 and st2 < b for st2, e2 in ((x.start, x.end) for x in extra)):
                    continue
                extra.append(Span(a, b, s.label, text[a:b], source="alias"))
    return extra


_DECLENSION_LABELS = frozenset({"PERSON", "LOCATION", "ORG"})

# Common Russian singular noun case endings, longest first (so a 3-char match
# is tried before a 1-char one that's merely a substring of it). Deliberately
# EXCLUDES "ов"/"ев"/"й": those are frequent endings of the NOMINATIVE itself
# (surnames like "Иванов", names like "Виталий") — stripping them would wrongly
# shrink a real base name into a different, shorter one.
_DECL_SUFFIXES = (
    "иями", "иях", "ами", "ями", "ах", "ях", "ом", "ем", "ём", "ой", "ей",
    "им", "ым", "у", "ю", "е", "и", "ы", "а", "я",
)


def _decl_stem(val: str) -> str:
    """Best-effort stem: strip the longest known case ending that still
    leaves >=4 chars; fall back to chopping one trailing char (old behaviour,
    right for a nominative that itself ends in a plain vowel like "Никита")."""
    for suf in _DECL_SUFFIXES:
        if val.endswith(suf) and len(val) - len(suf) >= 4:
            return val[: -len(suf)]
    return val[:-1]


def propagate_declensions(text: str, spans: list[Span]) -> list[Span]:
    """Find declined case-forms of already-detected single-word entities.

    NER/LLM may catch a value in WHATEVER case it happened to appear in first
    — not necessarily nominative (a transcript might say "с Мингосом" before
    it ever says bare "Мингос") — and miss the other case-forms elsewhere in
    the document. This derives a common stem via ``_decl_stem`` (strips a
    known case ending rather than blindly chopping one character, so an
    instrumental first-catch like "Мингосом" still yields the true stem
    "Мингос", not the useless "Мингосо") and searches for
    `<stem><known ending or nothing>`, returning matches as NEW spans (each
    keeps its own surface text, so the mapping stays reversible). Returns only
    spans not overlapping the existing ones.
    """
    existing = [(s.start, s.end) for s in spans]
    seen_stems: set[str] = set()
    extra: list[Span] = []
    suffix_alt = "|".join(sorted((re.escape(s) for s in _DECL_SUFFIXES), key=len, reverse=True))
    for s in spans:
        if s.label not in _DECLENSION_LABELS:
            continue
        val = s.text.strip()
        if " " in val or len(val) < 5 or not val[0].isupper():
            continue
        stem = _decl_stem(val)
        key = (s.label, stem.casefold())
        if key in seen_stems or len(stem) < 4:
            continue
        seen_stems.add(key)
        pattern = re.compile(
            r"(?<![А-Яа-яЁёA-Za-z])" + re.escape(stem) + r"(?:" + suffix_alt + r")?"
            r"(?![А-Яа-яЁёA-Za-z])"
        )
        for m in pattern.finditer(text):
            a, b = m.start(), m.end()
            if any(a < e and st < b for st, e in existing):
                continue
            if any(a < e2 and st2 < b for st2, e2 in ((x.start, x.end) for x in extra)):
                continue
            extra.append(Span(a, b, s.label, text[a:b], source="morph"))
    return extra
