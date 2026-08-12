#!/usr/bin/env python3
"""Сбор ЗАПОЛНЕННЫХ образцов документов с spmag.ru — корпус фикстур для автотестов анонимизатора.

ЧТО ДЕЛАЕТ
  1. Берёт список страниц-бланков из sitemap.xml (2524 штуки, один запрос).
     Резервный путь — постраничный обход /blanks?page=N (127 страниц по 20).
  2. На каждой странице разбирает блоки <div class="blank-page__document">:
     заголовок файла + прямой адрес в /storage/files/.
  3. Классифицирует файл по заголовку: «Образец…» → sample, «Бланк…» → blank,
     нейтральный заголовок → unknown (таких большинство).
  4. Скачивает, извлекает текст (тем же кодом, что и рабочий конвейер) и
     считает, есть ли внутри НАСТОЯЩИЕ данные — ФИО, даты, суммы, ИНН/ОГРН —
     против заглушек «______» и «(Ф.И.О.)». Решение по тексту важнее заголовка:
     заголовки сайта врут в обе стороны.
  5. Пустые бланки удаляет с диска (если не задан --include-blanks), но строку
     в манифест пишет — повторный запуск их не перекачивает.
  6. manifest.jsonl — по строке на файл: страница, заголовок, тип, формат,
     sha256, размер, локальный путь, filled + признаки, по которым решено.

ЧТО ЗАМЕРЕНО (выборка 100 случайных страниц, август 2026)
  • страниц-бланков в sitemap 2524, файлов на странице 1–3;
  • по заголовкам: примерно 40 % «Образец», 30 % «Бланк», 30 % нейтральных;
  • после проверки содержимого заполненными оказываются ~40 % кандидатов —
    на весь каталог это порядка 1000–1200 файлов, поровну .doc и .docx;
  • на 26 отобранных файлах регексные детекторы анонимизатора нашли ПД в 23:
    PERSON 210, LOCATION 48, PHONE 12, INN 10, POSTAL_CODE 6, PASSPORT 5,
    MILITARY_ID 2, EMAIL 1, BIRTH_CERTIFICATE 1 — корпус рабочий.
  • полный проход при --delay 1.0 занимает около двух часов.

ИЗВЕСТНЫЕ ПРОБЕЛЫ ИЗВЛЕЧЕНИЯ ТЕКСТА (такие файлы решаются по заголовку)
  • .xls — нужен пакет xlrd, без него текста нет;
  • .pdf форм ФНС — pypdf не достаёт ничего (данные в AcroForm);
  • .doc — нужен antiword или LibreOffice; вызов antiword здесь идёт с
    «-m UTF-8.txt», иначе вся кириллица превращается в «?».

ПРО robots.txt
  robots.txt закрывает /download/file/ (кнопка «Скачать») для всех агентов.
  Скрипт туда НЕ ходит: прямой адрес /storage/files/... лежит в ссылке
  «Посмотреть» (docs.google.com/viewer?url=…) и под запрет не попадает.
  robots.txt скачивается на старте и проверяется через urllib.robotparser
  перед КАЖДЫМ запросом; плюс вшит жёсткий чёрный список префиксов.

ПРО АВТОРСКОЕ ПРАВО
  Материалы сайта защищены. Скачанное годится как локальные фикстуры; не
  выкладывайте файлы в открытый репозиторий. Каталог фикстур по умолчанию
  лежит в корне проекта и в git не попадает (.gitignore — белый список).
  Для CI держите в репозитории только манифест (URL + sha256) и подтягивайте
  файлы при прогоне.

ПРИМЕРЫ
  python anonymizer/tools/fetch_spmag_samples.py --limit 20 --dry-run
  python anonymizer/tools/fetch_spmag_samples.py --limit 50 --out ./spmag_fixtures
  python anonymizer/tools/fetch_spmag_samples.py --out ./spmag_fixtures      # всё
  python anonymizer/tools/fetch_spmag_samples.py --formats docx,rtf,odt      # без .doc
  python anonymizer/tools/fetch_spmag_samples.py --include-blanks            # и пустые

Зависимости: requests, beautifulsoup4 (есть regex-фолбэк, если bs4 нет).
Текст из документов извлекается через anonymizer.documents (тот же код, что и в
рабочем конвейере), для .doc — antiword -m UTF-8.txt / soffice, если найдутся.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from bs4 import BeautifulSoup  # type: ignore

    HAVE_BS4 = True
except ImportError:  # pragma: no cover
    HAVE_BS4 = False

BASE = "https://spmag.ru"
INDEX = BASE + "/blanks"
SITEMAP = BASE + "/sitemap.xml"
ROBOTS = BASE + "/robots.txt"
STORAGE_PREFIX = "/storage/files/"
# Жёсткий чёрный список — дублирует robots.txt, чтобы кнопка «Скачать» не
# дёрнулась даже при подмене/недоступности robots.txt.
FORBIDDEN_PREFIXES = ("/download/file/", "/file/", "/files/", "/away", "/search")

UA = "spmag-fixture-fetcher/2.0 (local autotest fixtures for a document anonymizer)"

log = logging.getLogger("spmag")

# --------------------------------------------------------------------- разбор

BLANK_PAGE_RE = re.compile(r"^/blanks/[a-z0-9\-]+$")
FILE_RE = re.compile(
    r"https?://spmag\.ru/storage/files/[^\s\"'<>\\)]+?"
    r"\.(?:docx?|xlsx?|xlsm|pdf|rtf|odt|txt|xml)",
    re.IGNORECASE,
)
EXT_RE = re.compile(r"\.([A-Za-z]{2,4})$")

# Заголовок «Образец …» → в файле есть данные; «Бланк …» → пустая форма.
SAMPLE_WORDS = ("образец", "образц", "пример", "заполн", "sample")
BLANK_WORDS = ("бланк", "пустой", "пустая", "незаполн", "шаблон", "форма для")

# Признаки пустой формы: прочерки/точки/тире под рукописный ввод…
PLACEHOLDER_RE = re.compile(r"_{3,}|\.{6,}|-{6,}|…{3,}")
# …и словесные заглушки в скобках: «(Ф.И.О.)», «(указать сумму)», «[вписать нужное]».
MARKER_RE = re.compile(
    r"\((?:Ф\.?\s?И\.?\s?О\.?|ФИО|адрес|подпись|нужное|дата[^)]{0,30}|"
    r"указать[^)]{0,40}|наименование[^)]{0,60}|выбрать[^)]{0,30}|вписать[^)]{0,30})\)"
    r"|\[(?:вписать[^\]]{0,30}|указать[^\]]{0,40}|число[^\]]{0,30})\]",
    re.IGNORECASE,
)
# Признаки НАСТОЯЩИХ данных — ровно то, что анонимизатор и должен находить.
DATE_RE = re.compile(r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}\b")
DATE_TEXT_RE = re.compile(
    r"\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|"
    r"ноябр|декабр)\w*\s+\d{4}",
    re.IGNORECASE,
)
# ИНН/ОГРН/СНИЛС/номер счёта: 10, 12, 13 или 20 цифр подряд.
REQUISITE_RE = re.compile(r"\b(?:\d{10}|\d{12}|\d{13}|\d{20})\b")
MONEY_RE = re.compile(r"\b\d{1,3}(?:[   ]\d{3})+\b")
# «Иванов Иван Иванович» в любом падеже (…овича, …евну) и «Иванов И. И.».
FIO_FULL_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]*"
    r"(?:ович|евич|ьевич|овна|евна|ична|инична)[а-яё]{0,3}\b"
)
FIO_SHORT_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.")

SUPPORTED_EXT = ("docx", "doc", "rtf", "odt", "pdf", "xlsx", "xlsm", "xls", "txt", "xml")


@dataclass
class Entry:
    page_url: str
    page_slug: str
    page_title: str
    file_title: str
    file_url: str
    kind: str  # по заголовку: sample | blank | unknown
    ext: str
    sha256: str = ""
    size: int = 0
    local_path: str = ""
    filled: bool | None = None  # есть ли в файле настоящие данные
    decided_by: str = ""  # content | title | dupe
    text_chars: int = 0
    pii: int = 0  # сколько найдено ФИО/дат/сумм/реквизитов
    placeholders: int = 0  # прочерки «____»
    markers: int = 0  # заглушки «(Ф.И.О.)», «[вписать нужное]»
    fio: int = 0
    dates: int = 0
    money: int = 0
    requisites: int = 0
    fetched_at: str = ""
    note: str = ""


@dataclass
class Stats:
    pages: int = 0
    files_seen: int = 0
    downloaded: int = 0
    kept: int = 0
    dropped_blank: int = 0
    dupes: int = 0
    errors: int = 0
    by_ext: dict = field(default_factory=dict)
    conflicts: int = 0


# ----------------------------------------------------------------------- сеть


class Throttle:
    """Пауза между запросами с джиттером — чтобы не долбить сайт ровным потоком."""

    def __init__(self, delay: float):
        self.delay = delay
        self._last = 0.0

    def wait(self) -> None:
        if self.delay <= 0:
            return
        need = self.delay * random.uniform(0.8, 1.3) - (time.monotonic() - self._last)
        if need > 0:
            time.sleep(need)
        self._last = time.monotonic()


class Fetcher:
    """requests-сессия + ретраи + троттлинг + проверка robots.txt на каждый запрос."""

    def __init__(self, delay: float, timeout: int):
        self.timeout = timeout
        self.throttle = Throttle(delay)
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
        retry = Retry(
            total=4,
            connect=3,
            read=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        self.s = s
        self.robots = self._load_robots()

    def _load_robots(self) -> urllib.robotparser.RobotFileParser | None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = self.s.get(ROBOTS, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("robots.txt не прочитан (%s) — работаю по чёрному списку префиксов", e)
            return None
        rp.parse(r.text.splitlines())
        log.info("robots.txt загружен")
        return rp

    def assert_allowed(self, url: str) -> None:
        path = urlparse(url).path
        for bad in FORBIDDEN_PREFIXES:
            if path.startswith(bad):
                raise PermissionError(f"{path} закрыт в robots.txt — файлы берём только из {STORAGE_PREFIX}")
        if self.robots is not None and not self.robots.can_fetch(UA, url):
            raise PermissionError(f"robots.txt запрещает {path}")

    def get(self, url: str, **kw) -> requests.Response:
        self.assert_allowed(url)
        self.throttle.wait()
        r = self.s.get(url, timeout=self.timeout, **kw)
        r.raise_for_status()
        return r

    def get_file(self, url: str) -> bytes:
        """Скачивание документа: только из /storage/files/ и ничего другого."""
        path = urlparse(url).path
        if not path.startswith(STORAGE_PREFIX):
            raise PermissionError(f"ожидался путь {STORAGE_PREFIX}*, получен {path}")
        return self.get(url).content


# ------------------------------------------------------------------ страницы

def iter_pages_sitemap(f: Fetcher) -> list[str]:
    xml = f.get(SITEMAP).text
    urls = [u for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml) if "/blanks/" in u]
    out, seen = [], set()
    for u in urls:
        if BLANK_PAGE_RE.match(urlparse(u).path) and u not in seen:
            seen.add(u)
            out.append(u)
    log.info("sitemap: страниц-бланков %d", len(out))
    return out


def iter_pages_index(f: Fetcher, max_pages: int | None) -> Iterator[str]:
    seen: set[str] = set()
    page, empty = 1, 0
    while True:
        if max_pages and page > max_pages:
            return
        url = INDEX if page == 1 else f"{INDEX}?page={page}"
        try:
            html = f.get(url).text
        except requests.HTTPError as e:
            log.warning("список, страница %d: %s — останавливаюсь", page, e)
            return
        links = [u for u in find_blank_links(html) if u not in seen]
        if not links:
            empty += 1
            if empty >= 2:
                log.info("страница %d пустая — конец списка", page)
                return
        else:
            empty = 0
        for u in links:
            seen.add(u)
            yield u
        log.info("список: страница %d, новых %d (всего %d)", page, len(links), len(seen))
        page += 1


def find_blank_links(html: str) -> list[str]:
    hrefs = re.findall(r'href="((?:https://spmag\.ru)?/blanks/[a-z0-9\-]+)"', html, re.IGNORECASE)
    out, seen = [], set()
    for h in hrefs:
        full = urljoin(BASE, h)
        if BLANK_PAGE_RE.match(urlparse(full).path) and full not in seen:
            seen.add(full)
            out.append(full)
    return out


# --------------------------------------------------------- разбор страницы

def direct_url(href: str, page_url: str) -> str:
    """Прямой адрес файла из ссылки «Посмотреть» или из обычной ссылки на /storage/."""
    if "docs.google.com/viewer" in href and "url=" in href:
        cand = unquote(href.split("url=", 1)[1].split("&", 1)[0])
    elif STORAGE_PREFIX in href:
        cand = urljoin(page_url, href)
    else:
        return ""
    p = urlparse(cand)
    if not p.path.startswith(STORAGE_PREFIX) or p.netloc not in ("spmag.ru", "www.spmag.ru"):
        return ""
    return cand


def extract_files_bs4(html: str, page_url: str) -> list[tuple[str, str]]:
    """(заголовок, прямой URL) — структурный разбор по блокам документов."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    blocks = soup.select("div.blank-page__document")
    for b in blocks:
        t = b.select_one(".blank-page__document-title")
        title = t.get_text(" ", strip=True) if t else ""
        url = ""
        for a in b.find_all("a", href=True):
            url = direct_url(a["href"], page_url)
            if url:
                break
        if not url or url in seen:
            continue  # блок без «Посмотреть» — прямого адреса нет, /download/ запрещён
        seen.add(url)
        out.append((title, url))
    if not blocks:  # вёрстка поменялась — общий обход ссылок
        for a in soup.find_all("a", href=True):
            url = direct_url(a["href"], page_url)
            if url and url not in seen:
                seen.add(url)
                out.append(("", url))
    return out


def extract_files_regex(html: str, page_url: str) -> list[tuple[str, str]]:
    """Фолбэк без bs4: URL по regex, заголовок — из ближайшего document-title слева."""
    decoded = unquote(html)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in FILE_RE.finditer(decoded):
        url = m.group(0)
        if url in seen:
            continue
        seen.add(url)
        window = decoded[max(0, m.start() - 3000) : m.start()]
        titles = re.findall(
            r'blank-page__document-title[^>]*>\s*(.*?)\s*</div>', window, re.DOTALL
        )
        title = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", titles[-1])).strip() if titles else ""
        out.append((title, url))
    return out


def extract_files(html: str, page_url: str) -> list[tuple[str, str]]:
    if HAVE_BS4:
        res = extract_files_bs4(html, page_url)
        if res:
            return res
    return extract_files_regex(html, page_url)


def page_title(html: str) -> str:
    for pat in (r"(?is)<h1[^>]*>(.*?)</h1>", r"(?is)<title[^>]*>(.*?)</title>"):
        m = re.search(pat, html)
        if m:
            txt = re.sub(r"(?s)<[^>]+>", " ", m.group(1))
            return re.sub(r"\s+", " ", unescape(txt)).strip()
    return ""


def unescape(s: str) -> str:
    import html as _html

    return _html.unescape(s)


def classify_title(file_title: str, page_ttl: str) -> str:
    """sample | blank | unknown. Заголовок файла точнее заголовка страницы."""
    for text in (file_title.lower(), page_ttl.lower()):
        if not text:
            continue
        i_s = min((text.find(w) for w in SAMPLE_WORDS if w in text), default=-1)
        i_b = min((text.find(w) for w in BLANK_WORDS if w in text), default=-1)
        if i_s >= 0 and i_b < 0:
            return "sample"
        if i_b >= 0 and i_s < 0:
            return "blank"
        if i_s >= 0 and i_b >= 0:
            return "sample" if i_s < i_b else "blank"  # что стоит первым, то и главное
    return "unknown"


# ------------------------------------------------- заполнен или пустой бланк

def _extract_text_repo(name: str, data: bytes) -> str:
    from anonymizer.documents import read_text_from_bytes

    return read_text_from_bytes(name, data)


def _extract_doc_text(data: bytes) -> str:
    """.doc → текст. antiword БЕЗ -m UTF-8.txt отдаёт «?» вместо кириллицы, поэтому
    маппинг задаём явно; затем LibreOffice; затем общий путь из anonymizer.documents."""
    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        if shutil.which("antiword"):
            r = subprocess.run(
                ["antiword", "-m", "UTF-8.txt", path], capture_output=True, timeout=60
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            outdir = tempfile.mkdtemp()
            subprocess.run(
                [soffice, "--headless", "--convert-to", "txt:Text", "--outdir", outdir, path],
                capture_output=True,
                timeout=180,
            )
            for f in os.listdir(outdir):
                if f.endswith(".txt"):
                    return Path(outdir, f).read_text(encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        log.debug(".doc: внешний конвертер не сработал: %s", e)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return _extract_text_repo("x.doc", data)


def extract_text(ext: str, data: bytes) -> tuple[str, str]:
    """(текст, причина неудачи). ext — расширение без точки.

    Имя для read_text_from_bytes собираем полное: Path(".doc").suffix пустой, и
    без «sample» разбор ушёл бы в общий декодер и вернул мусор из байтов файла.
    Известные пробелы: .xls требует xlrd (без него текста нет), из .pdf с ФНС
    формами pypdf ничего не достаёт — такие файлы решаются по заголовку.
    """
    ext = ext.lower().lstrip(".")
    name = f"sample.{ext}" if ext else "sample.bin"
    try:
        if ext == "doc":
            return _extract_doc_text(data), ""
        return _extract_text_repo(name, data), ""
    except Exception as e:  # извлечение текста — вспомогательный шаг, падать нельзя
        log.debug("текст не извлёкся из %s: %s", name, e)
        return "", f"{type(e).__name__}: {e}"


def content_features(text: str) -> dict[str, int]:
    """Сколько в тексте настоящих данных и сколько заглушек под ручной ввод."""
    f = {
        "chars": len(text),
        "placeholders": len(PLACEHOLDER_RE.findall(text)),
        "markers": len(MARKER_RE.findall(text)),
        "fio": len(FIO_FULL_RE.findall(text)) + len(FIO_SHORT_RE.findall(text)),
        "dates": len(DATE_RE.findall(text)) + len(DATE_TEXT_RE.findall(text)),
        "money": len(MONEY_RE.findall(text)),
        "requisites": len(REQUISITE_RE.findall(text)),
    }
    f["pii"] = f["fio"] + f["dates"] + f["money"] + f["requisites"]
    return f


def classify_content(f: dict[str, int]) -> bool | None:
    """True — есть настоящие данные, False — пустая форма, None — не берусь судить.

    Пороги подобраны на выборке из 54 файлов (40 случайных страниц): заголовок
    сайта врёт в обе стороны — «Образец договора дарения» оказывается пустым
    бланком с прочерками, а «Договор о целевом обучении (образец)» — реально
    заполненным, но с типографскими линиями-заглушками. Поэтому решает
    количество настоящих ПД, а прочерки — только тай-брейк для слабых случаев.
    """
    if f["chars"] < 120:
        return None  # текст не извлёкся (сканы .pdf, .xls без xlrd) — судить не по чему
    if f["pii"] >= 4:
        return True
    if f["pii"] >= 2 and f["placeholders"] <= 3 and f["markers"] == 0:
        return True
    if f["placeholders"] + f["markers"] >= 6:
        return False
    if f["pii"] == 0:
        return False
    return None


# ------------------------------------------------------------- имена файлов

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slugify(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    out = []
    for ch in text:
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        else:
            out.append("-")
    s = re.sub(r"-+", "-", "".join(out)).strip("-")
    return s[:maxlen].strip("-") or "doc"


def target_name(entry: Entry, digest: str) -> str:
    base = slugify(entry.file_title or entry.page_slug, 48)
    return f"{slugify(entry.page_slug, 32)}__{base}__{digest[:8]}.{entry.ext or 'bin'}"


def target_path(files_dir: Path, entry: Entry, digest: str) -> Path:
    """Имя файла, подрезанное под лимит длины пути Windows (MAX_PATH = 260)."""
    name = target_name(entry, digest)
    budget = 250 - len(str(files_dir)) - 1
    if len(name) > budget:
        tail = f"__{digest[:8]}.{entry.ext or 'bin'}"
        head = name[: max(8, budget - len(tail))].rstrip("-_")
        name = head + tail
    return files_dir / name


MAGIC = {
    "docx": (b"PK",), "xlsx": (b"PK",), "xlsm": (b"PK",), "odt": (b"PK",),
    "doc": (b"\xd0\xcf\x11\xe0",), "xls": (b"\xd0\xcf\x11\xe0",),
    "pdf": (b"%PDF",), "rtf": (b"{\\rtf",),
}


def magic_ok(ext: str, blob: bytes) -> bool:
    sigs = MAGIC.get(ext)
    return True if not sigs else any(blob.startswith(s) for s in sigs)


# ---------------------------------------------------------------- манифест

def load_manifest(path: Path) -> tuple[int, dict[str, str], set[str]]:
    rows, by_hash, done = 0, {}, set()
    if not path.exists():
        return rows, by_hash, done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows += 1
        done.add(d.get("file_url", ""))
        if d.get("sha256") and d.get("local_path"):
            by_hash[d["sha256"]] = d["local_path"]
    return rows, by_hash, done


# ------------------------------------------------------------- основной ход

def collect_entries(f: Fetcher, page_urls: Iterable[str], st: Stats,
                    dump_html: str | None = None) -> list[Entry]:
    entries: list[Entry] = []
    page_urls = list(page_urls)
    total = len(page_urls)
    for i, purl in enumerate(page_urls, 1):
        try:
            html = f.get(purl).text
        except (requests.RequestException, PermissionError) as e:
            log.warning("пропуск %s: %s", purl, e)
            st.errors += 1
            continue
        st.pages += 1
        if dump_html and i == 1:
            Path(dump_html).write_text(html, encoding="utf-8")
            log.info("сырой HTML сохранён в %s", dump_html)
        ptitle = page_title(html)
        slug = urlparse(purl).path.rstrip("/").rsplit("/", 1)[-1]
        files = extract_files(html, purl)
        for ftitle, furl in files:
            m = EXT_RE.search(urlparse(furl).path)
            entries.append(
                Entry(
                    page_url=purl, page_slug=slug, page_title=ptitle,
                    file_title=ftitle or ptitle, file_url=furl,
                    kind=classify_title(ftitle, ptitle),
                    ext=(m.group(1).lower() if m else ""),
                )
            )
        st.files_seen += len(files)
        log.info("[%d/%d] %s — файлов %d", i, total, slug, len(files))
    return entries


def handle_one(f: Fetcher, e: Entry, files_dir: Path, known: dict[str, str],
               keep_blanks: bool, st: Stats) -> None:
    try:
        blob = f.get_file(e.file_url)
    except (requests.RequestException, PermissionError) as err:
        e.note = f"download failed: {err}"
        st.errors += 1
        log.warning("не скачался %s: %s", e.file_url, err)
        return
    if len(blob) < 256:
        e.note = f"too small ({len(blob)} B)"
        st.errors += 1
        return
    if not magic_ok(e.ext, blob):
        e.note = "magic mismatch"
        st.errors += 1
        log.warning("не похоже на %s: %s", e.ext, e.file_url)
        return

    st.downloaded += 1
    e.sha256 = hashlib.sha256(blob).hexdigest()
    e.size = len(blob)
    e.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    if e.sha256 in known:  # тот же файл прилинкован с другой страницы
        e.local_path = known[e.sha256]
        e.filled, e.decided_by, e.note = True, "dupe", "duplicate of " + known[e.sha256]
        st.dupes += 1
        return

    text, extract_err = extract_text(e.ext, blob)
    feats = content_features(text)
    e.text_chars = feats["chars"]
    e.pii, e.placeholders, e.markers = feats["pii"], feats["placeholders"], feats["markers"]
    e.fio, e.dates, e.money, e.requisites = (
        feats["fio"], feats["dates"], feats["money"], feats["requisites"],
    )
    verdict = classify_content(feats)
    if verdict is None:
        e.filled = e.kind == "sample"
        e.decided_by = "title"
        if feats["chars"] < 120:
            e.note = "текст не извлёкся (" + (extract_err or "пусто") + ") — решение по заголовку"
    else:
        e.filled = verdict
        e.decided_by = "content"
        if e.kind in ("sample", "blank") and verdict != (e.kind == "sample"):
            st.conflicts += 1
            log.debug("заголовок и содержимое расходятся: %s (%s)", e.file_title, e.file_url)

    if not e.filled and not keep_blanks:
        st.dropped_blank += 1
        return

    path = target_path(files_dir, e, e.sha256)
    tmp = files_dir / (e.sha256[:16] + ".part")  # короткое имя: MAX_PATH
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    e.local_path = str(path.relative_to(files_dir.parent)).replace("\\", "/")
    known[e.sha256] = e.local_path
    st.kept += 1
    st.by_ext[e.ext] = st.by_ext.get(e.ext, 0) + 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):  # русские логи в консоли Windows
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", default="./spmag_fixtures", help="каталог для файлов и манифеста")
    ap.add_argument("--source", choices=("sitemap", "index"), default="sitemap",
                    help="откуда брать список страниц (sitemap — один запрос)")
    ap.add_argument("--limit", type=int, default=None, help="ограничить число страниц-бланков")
    ap.add_argument("--pages", type=int, default=None, help="страниц списка в режиме --source index")
    ap.add_argument("--max-files", type=int, default=None, help="ограничить число скачиваний")
    ap.add_argument("--formats", default="docx,doc,rtf,odt",
                    help=f"расширения через запятую (поддерживаются: {','.join(SUPPORTED_EXT)})")
    ap.add_argument("--kinds", default="sample,unknown",
                    help="какие типы по заголовку качать: sample,blank,unknown")
    ap.add_argument("--include-blanks", action="store_true",
                    help="оставлять на диске и пустые формы")
    ap.add_argument("--delay", type=float, default=1.0, help="пауза между запросами, с")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="только составить список, не качать")
    ap.add_argument("--dump-html", default=None, help="сохранить HTML первой страницы")
    ap.add_argument("--shuffle", action="store_true", help="перемешать страницы (для выборки)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not HAVE_BS4:
        log.info("bs4 нет — regex-разбор (pip install beautifulsoup4 точнее)")

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # для anonymizer.documents

    outdir = Path(args.out).resolve()
    files_dir = outdir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = outdir / "manifest.jsonl"
    formats = {x.strip().lower().lstrip(".") for x in args.formats.split(",") if x.strip()}
    kinds = {x.strip().lower() for x in args.kinds.split(",") if x.strip()}

    prev, known, done_urls = load_manifest(manifest_path)
    if prev:
        log.info("продолжаю: в манифесте %d записей, файлов на диске %d", prev, len(known))

    f = Fetcher(args.delay, args.timeout)
    st = Stats()

    if args.source == "sitemap":
        pages = iter_pages_sitemap(f)
    else:
        pages = list(iter_pages_index(f, args.pages))
    if args.shuffle:
        random.Random(args.seed).shuffle(pages)
    if args.limit:
        pages = pages[: args.limit]
    log.info("страниц к обходу: %d", len(pages))

    entries = collect_entries(f, pages, st, args.dump_html)
    log.info("файлов найдено: %d", len(entries))

    todo = [
        e for e in entries
        if e.kind in kinds and e.ext in formats and e.file_url not in done_urls
    ]
    seen_urls: set[str] = set()
    todo = [e for e in todo if not (e.file_url in seen_urls or seen_urls.add(e.file_url))]
    if args.max_files:
        todo = todo[: args.max_files]

    by_kind = {k: sum(1 for e in entries if e.kind == k) for k in ("sample", "blank", "unknown")}
    log.info("по заголовкам: образцов %d, бланков %d, неясных %d",
             by_kind["sample"], by_kind["blank"], by_kind["unknown"])
    log.info("к скачиванию: %d", len(todo))

    if args.dry_run:
        for e in todo[:40]:
            log.info("  [%-7s] %-5s %s", e.kind, e.ext, e.file_title[:70])
        if len(todo) > 40:
            log.info("  … и ещё %d", len(todo) - 40)
        return 0

    with manifest_path.open("a", encoding="utf-8") as mf:
        for i, e in enumerate(todo, 1):
            handle_one(f, e, files_dir, known, args.include_blanks, st)
            mf.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
            mf.flush()
            if i % 25 == 0:
                log.info("обработано %d/%d, оставлено %d", i, len(todo), st.kept)

    log.info("——— итог ———")
    log.info("страниц обойдено      : %d", st.pages)
    log.info("файлов скачано        : %d", st.downloaded)
    log.info("оставлено заполненных : %d", st.kept)
    log.info("отброшено пустых форм : %d", st.dropped_blank)
    log.info("дубликатов по sha256  : %d", st.dupes)
    log.info("ошибок                : %d", st.errors)
    log.info("расхождений заголовок/содержимое: %d", st.conflicts)
    log.info("по форматам           : %s", st.by_ext or "—")
    log.info("манифест: %s", manifest_path)
    log.info("файлы   : %s", files_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.warning("прервано — манифест дописан, повторный запуск продолжит с места остановки")
        sys.exit(130)
