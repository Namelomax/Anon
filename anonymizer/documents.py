"""Read documents, anonymize them as a whole, and write the results back.

Reads ``.docx`` (paragraphs + tables + headers/footers), ``.pdf``, ``.xlsx``,
``.xml``, ``.rtf``, ``.odt`` and plain text; presentations are excluded. The
whole document is anonymized in one pass so the same entity gets the same
placeholder everywhere (e.g. one person -> ``[PERSON_1]`` across all paragraphs).
Only ``.docx`` is rebuilt structure-preserving; other formats yield anonymized
plain text. Старый ``.doc`` — особый случай: писать бинарный Word 97 нечем, но
и отдавать его текстом нельзя (документ после этого не собрать), поэтому
обезличенная копия отдаётся как ``.docx`` — через LibreOffice с сохранением
разметки (``doc_to_docx_bytes``) или, если её на хосте нет, простым
документом из текста (``text_to_docx_bytes``).

Outputs:
* ``<name>.anon.txt``  — anonymized plain text
* ``<name>.map.json``  — placeholder -> original mapping (the deanonymize key)
* ``<name>.anon.docx`` — anonymized copy preserving paragraph/table structure
  (only for .docx inputs)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .engine import Anonymizer
from .mapping import Mapping, save_mapping


def read_text(path: str | Path) -> str:
    """Read a document (docx/pdf/xlsx/xml/rtf/odt/txt…) into one string."""
    path = Path(path)
    return read_text_from_bytes(path.name, path.read_bytes())


def _iter_container_paragraphs(container):
    """Yield paragraphs of a body/header/footer, including its tables."""
    for para in container.paragraphs:
        yield para
    for table in container.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    yield para


def _iter_docx_paragraphs(document):
    """Yield body paragraphs, table-cell paragraphs AND header/footer paragraphs.

    Headers/footers matter: в договорах реквизиты сторон и email часто вынесены
    в колонтитулы — раньше они не читались вовсе, поэтому не анонимизировались
    ни в тексте, ни в .docx-копии.
    """
    yield from _iter_container_paragraphs(document)
    for section in document.sections:
        for part in (
            section.header, section.footer,
            section.first_page_header, section.first_page_footer,
            section.even_page_header, section.even_page_footer,
        ):
            if part is None or getattr(part, "is_linked_to_previous", False):
                continue
            yield from _iter_container_paragraphs(part)


def _read_docx_text(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    lines = [para.text for para in _iter_docx_paragraphs(document)]
    return "\n".join(lines)


def anonymize_document(path: str | Path, anon: Anonymizer) -> tuple[str, Mapping]:
    """Read a document and anonymize its full text in one pass."""
    text = read_text(path)
    res = anon.anonymize(text)
    return res.anonymized_text, res.mapping


def _replacer(mapping: Mapping):
    """Build a function that swaps original values for their placeholders.

    Longest originals first so a value that contains a shorter one is replaced
    whole. Used to anonymize .docx paragraphs while reusing the document-wide
    mapping (consistent placeholders).
    """
    items = sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True)
    if not items:
        return lambda s: s
    pattern = re.compile("|".join(re.escape(orig) for _, orig in items))
    inverse = {orig: ph for ph, orig in items}

    def replace(s: str) -> str:
        return pattern.sub(lambda m: inverse[m.group(0)], s)

    return replace


def _rewrite_docx(document, mapping: Mapping) -> None:
    """In-place: replace original values with placeholders in every paragraph."""
    _rewrite_docx_paragraphs(document, _replacer(mapping))


def _rewrite_docx_paragraphs(document, replace) -> None:
    """In-place: прогнать каждый абзац через ``replace``.

    Structure is preserved; each paragraph is rewritten into a single run
    (intra-paragraph formatting is not retained — fine for a redacted artifact).
    Подстановка идёт по ЦЕЛОМУ абзацу: значение часто разорвано на несколько
    run'ов, и по отдельным run'ам его было бы не найти.
    """
    for para in _iter_docx_paragraphs(document):
        if not para.text:
            continue
        new_text = replace(para.text)
        if new_text == para.text:
            continue
        for run in list(para.runs):
            run.text = ""
        if para.runs:
            para.runs[0].text = new_text
        else:
            para.add_run(new_text)


def write_anonymized_docx(src: str | Path, dst: str | Path, mapping: Mapping) -> None:
    """Write an anonymized .docx copy to ``dst`` (path)."""
    import docx

    document = docx.Document(str(src))
    _rewrite_docx(document, mapping)
    document.save(str(dst))


# File formats we can extract text from. Presentations (.pptx/.ppt) are
# intentionally excluded (per requirement). Anonymization runs on the extracted
# text; the .docx path additionally rebuilds a structure-preserving copy, other
# formats are returned as anonymized .txt.
_TEXT_EXT = {".txt", ".csv", ".md", ".log", ".json"}
_UNSUPPORTED = {".pptx", ".ppt"}


def _decode(data: bytes) -> str:
    """Best-effort decode of a plain-text/byte blob (Russian docs are often cp1251)."""
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_docx_bytes(data: bytes) -> str:
    import io

    import docx

    document = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in _iter_docx_paragraphs(document))


def _read_pdf_bytes(data: bytes) -> str:
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _read_xlsx_bytes(data: bytes) -> str:
    import io

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append("\t".join(cells))
    return "\n".join(lines)


def _read_xml_bytes(data: bytes) -> str:
    from lxml import etree

    root = etree.fromstring(data)  # handles the encoding declaration itself
    return "\n".join(t.strip() for t in root.itertext() if t and t.strip())


def _read_odt_bytes(data: bytes) -> str:
    import io
    import zipfile

    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        content = z.read("content.xml")
    root = etree.fromstring(content)
    # One line per paragraph/heading element (tags end with "}p" / "}h").
    lines: list[str] = []
    for el in root.iter():
        tag = etree.QName(el).localname if isinstance(el.tag, str) else ""
        if tag in ("p", "h"):
            txt = "".join(el.itertext()).strip()
            if txt:
                lines.append(txt)
    return "\n".join(lines)


def _read_rtf_bytes(data: bytes) -> str:
    """Minimal RTF -> text: enough to extract content for anonymization."""
    text = data.decode("latin-1", errors="ignore")
    text = re.sub(r"\\'([0-9a-fA-F]{2})",
                  lambda m: bytes([int(m.group(1), 16)]).decode("cp1251", "ignore"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 0x10000), text)
    text = re.sub(r"\\(?:par|line|pard|sect)\b", "\n", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)  # drop remaining control words
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_xls_bytes(data: bytes) -> str:
    """Старый Excel 97-2003 (.xls) через xlrd (openpyxl читает только .xlsx).

    xlrd 2.x поддерживает именно .xls; ставится `pip install xlrd`.
    """
    import xlrd

    book = xlrd.open_workbook(file_contents=data)
    lines: list[str] = []
    for sh in book.sheets():
        for r in range(sh.nrows):
            cells = [str(c.value).strip() for c in sh.row(r) if str(c.value).strip()]
            if cells:
                lines.append("\t".join(cells))
    return "\n".join(lines)


def _read_doc_olefile(data: bytes) -> str:
    """Чистый Python-извлекатель текста из бинарного .doc (Word 97-2003).

    Разбираем OLE-контейнер: FIB → таблица кусков (piece table, Clx) в потоке
    0Table/1Table → куски текста в WordDocument (cp1251 для 8-бит или UTF-16LE).
    Работает БЕЗ системных утилит — нужен только пакет ``olefile``. По сути это
    «нативная конвертация .doc → текст» на сервере.
    """
    import io
    import struct

    import olefile

    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        wd = ole.openstream("WordDocument").read()
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        table_name = "1Table" if (flags & 0x0200) else "0Table"
        if not ole.exists(table_name):
            table_name = "0Table" if table_name == "1Table" else "1Table"
        fc_clx = struct.unpack_from("<i", wd, 0x1A2)[0]
        lcb_clx = struct.unpack_from("<i", wd, 0x1A6)[0]
        clx = ole.openstream(table_name).read()[fc_clx:fc_clx + lcb_clx]
    finally:
        ole.close()

    # Найти Pcdt (маркер 0x02) в Clx, пропустив Prc-блоки (0x01 + 2-байт длина).
    i, plc = 0, None
    while i < len(clx):
        if clx[i] == 0x01:
            cb = struct.unpack_from("<H", clx, i + 1)[0]
            i += 3 + cb
        elif clx[i] == 0x02:
            lcb = struct.unpack_from("<I", clx, i + 1)[0]
            plc = clx[i + 5:i + 5 + lcb]
            break
        else:
            break
    if not plc:
        raise ValueError("piece table (.doc) не найдена")

    n = (len(plc) - 4) // 12
    cps = [struct.unpack_from("<I", plc, k * 4)[0] for k in range(n + 1)]
    parts: list[str] = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, (n + 1) * 4 + k * 8 + 2)[0]
        count = cps[k + 1] - cps[k]
        if fc & 0x40000000:  # 8-битная кодировка (cp1251 для русских документов)
            base = (fc & 0x3FFFFFFF) // 2
            parts.append(wd[base:base + count].decode("cp1251", "replace"))
        else:  # UTF-16LE
            base = fc & 0x3FFFFFFF
            parts.append(wd[base:base + count * 2].decode("utf-16-le", "replace"))

    text = "".join(parts)
    # Спецсимволы Word → обычный текст. ВАЖНО: карту применяем ПЕРВОЙ (через
    # _map.get с fallback), иначе управляющие символы (0x0D — конец абзаца!)
    # отсекаются условием >=0x20 раньше, чем превратятся в '\n', слова слипаются
    # («года»+«г.» → «годаг»), и маскирование по границам слова ломается.
    _map = {0x0D: "\n", 0x07: "\n", 0x0B: "\n", 0x0C: "\n", 0x1E: "-", 0xA0: " "}
    return "".join(
        _map.get(ord(c), c if (ord(c) >= 0x20 or c in "\n\t") else "") for c in text
    )


def _read_doc_bytes(data: bytes) -> str:
    """Старый Word 97-2003 (.doc) → текст.

    Сначала системная утилита, если она есть (лучшая точность): antiword →
    catdoc → LibreOffice. Если их нет (типичный JupyterHub без root) — чистый
    Python-разбор через ``_read_doc_olefile`` (пакет olefile), т.е. .doc
    обрабатывается автоматически без установки системных пакетов.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tf:
        tf.write(data)
        path = tf.name
    try:
        if shutil.which("antiword"):
            r = subprocess.run(["antiword", path], capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        if shutil.which("catdoc"):
            r = subprocess.run(["catdoc", "-d", "utf-8", path], capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode("utf-8", "replace")
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if soffice:
            outdir = tempfile.mkdtemp()
            subprocess.run(
                [soffice, "--headless", "--convert-to", "txt:Text", "--outdir", outdir, path],
                capture_output=True, timeout=120,
            )
            for f in os.listdir(outdir):
                if f.endswith(".txt"):
                    with open(os.path.join(outdir, f), encoding="utf-8", errors="replace") as fh:
                        return fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    # Системных утилит нет — резервный чистый-Python разбор .doc.
    return _read_doc_olefile(data)


def _find_soffice() -> str | None:
    """Путь к LibreOffice (soffice/libreoffice) или None, если её нет."""
    import shutil

    return shutil.which("soffice") or shutil.which("libreoffice")


def _libreoffice_convert(
    data: bytes, src_suffix: str, convert_to: str, out_ext: str
) -> bytes | None:
    """Пересобрать документ в другой формат через LibreOffice.

    ``None``, если LibreOffice на хосте нет или конвертация не дала файла.
    Это единственный способ сохранить разметку форматов, которые Python
    записать не умеет (.doc, .xls, .rtf): их поднимают до формата, который мы
    правим (.docx/.xlsx), а при необходимости конвертируют обратно.

    Каждому запуску даётся свой профиль (``-env:UserInstallation``): общий
    профиль в $HOME блокируется первым же процессом, и два одновременных
    запроса дерутся за него — второй падает или висит до таймаута.
    """
    import os
    import subprocess
    import tempfile

    soffice = _find_soffice()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory() as workdir:
        src = os.path.join(workdir, f"src{src_suffix}")
        with open(src, "wb") as fh:
            fh.write(data)
        outdir = os.path.join(workdir, "out")
        os.makedirs(outdir, exist_ok=True)
        profile = Path(workdir, "profile").as_uri()
        try:
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation={profile}",
                    "--headless",
                    "--convert-to",
                    convert_to,
                    "--outdir",
                    outdir,
                    src,
                ],
                capture_output=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None  # конвертера нет смысла чинить на лету
        for fname in os.listdir(outdir):
            if fname.lower().endswith(out_ext):
                with open(os.path.join(outdir, fname), "rb") as fh:
                    return fh.read()
    return None


def doc_to_docx_bytes(data: bytes, suffix: str = ".doc") -> bytes | None:
    """Старый .doc → .docx. ``None``, если LibreOffice на хосте нет.

    Обратно в бинарный .doc не пишет ни python-docx, ни какая-либо другая
    чистая Python-библиотека, поэтому обезличенная копия старого Word'а может
    быть только .docx — с разметкой (через эту конвертацию) или без неё
    (``text_to_docx_bytes``).
    """
    return _libreoffice_convert(data, suffix, "docx:MS Word 2007 XML", ".docx")


def xls_to_xlsx_bytes(data: bytes) -> bytes | None:
    """Старый .xls → .xlsx. ``None``, если LibreOffice на хосте нет.

    То же, что с .doc: xlrd только читает, записи .xls в экосистеме Python
    нет, поэтому таблица отдаётся как .xlsx.
    """
    return _libreoffice_convert(data, ".xls", "xlsx:Calc MS Excel 2007 XML", ".xlsx")


def docx_to_rtf_bytes(data: bytes) -> bytes | None:
    """.docx → .rtf, обратный шаг для RTF-документов.

    RTF правится не напрямую (значения в нём разбиты управляющими словами и
    экранированы посимвольно — надёжной подстановки не получается), а кругом
    rtf → docx → правка → rtf. ``None``, если LibreOffice на хосте нет.
    """
    return _libreoffice_convert(data, ".docx", "rtf:Rich Text Format", ".rtf")


def text_to_docx_bytes(text: str) -> bytes:
    """Собрать простой .docx из готового текста — по абзацу на строку.

    Запасной путь для форматов, чью разметку взять неоткуда (старый .doc на
    хосте без LibreOffice, см. ``doc_to_docx_bytes``). Оформление оригинала не
    восстанавливается, но пользователь получает документ Word, который можно
    открыть, править и отправить на восстановление обратно .docx-путём, а не
    .txt, из которого документ уже не собрать.
    """
    import io

    import docx

    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def read_text_from_bytes(name: str, data: bytes) -> str:
    """Extract plain text from in-memory document bytes (no temp file).

    Supported: .docx .doc .pdf .xlsx/.xlsm .xls .xml .rtf .odt and plain text.
    Presentations (.pptx/.ppt) are rejected. Unknown extensions fall back to a
    best-effort text decode so nothing silently returns empty.
    """
    ext = Path(name).suffix.lower()
    if ext in _UNSUPPORTED:
        raise ValueError(f"Формат {ext} не поддерживается (презентации исключены).")
    if ext == ".docx":
        return _read_docx_bytes(data)
    if ext == ".doc":
        return _read_doc_bytes(data)
    if ext == ".pdf":
        return _read_pdf_bytes(data)
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx_bytes(data)
    if ext == ".xls":
        return _read_xls_bytes(data)
    if ext == ".xml":
        return _read_xml_bytes(data)
    if ext == ".odt":
        return _read_odt_bytes(data)
    if ext == ".rtf":
        return _read_rtf_bytes(data)
    return _decode(data)


def anonymized_docx_bytes(src_data: bytes, mapping: Mapping) -> bytes:
    """Return an anonymized .docx as bytes, built from source .docx bytes."""
    import io

    import docx

    document = docx.Document(io.BytesIO(src_data))
    _rewrite_docx(document, mapping)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def deanonymized_docx_bytes(src_data: bytes, mapping: Mapping) -> bytes:
    """Restore originals in a .docx (replace placeholders -> values), return bytes."""
    from .deanonymize import deanonymize

    return _rewritten_docx(src_data, lambda s: deanonymize(s, mapping))


def _rewritten_docx(src_data: bytes, replace) -> bytes:
    """Прогнать абзацы .docx через ``replace`` и вернуть байты копии."""
    import io

    import docx

    document = docx.Document(io.BytesIO(src_data))
    _rewrite_docx_paragraphs(document, replace)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


# --- Пересборка документа в исходном формате -------------------------------
# Общее правило: пользователь должен получить обратно ТОТ ЖЕ формат, который
# загрузил. Обезличивание — это подстановка строк, поэтому для каждого формата
# нужен способ пройти по его текстовым узлам и переписать их, сохранив всё
# остальное. Способы делятся на три уровня:
#
#   "original"  — формат правится напрямую (.docx, .xlsx/.xlsm, .odt, .xml, а
#                 также простой текст, который сам по себе и есть документ);
#   "converted" — формат Python'ом не пишется, поэтому LibreOffice поднимает
#                 его до правимого (.doc→.docx, .xls→.xlsx, .rtf→.docx→.rtf);
#   "text"      — ни того, ни другого нет: документ собирается заново из
#                 обезличенного текста, разметка теряется.
#
# .pdf сознательно остаётся текстом: переписать текст в PDF, не развалив
# вёрстку, умеет только PyMuPDF под AGPL-3, а круг pdf→docx→pdf через
# LibreOffice превращает страницу в мешанину текстовых блоков — это хуже
# честного .txt.

_MIME_BY_EXT = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
    ".xml": "application/xml",
    ".json": "application/json",
    ".csv": "text/csv",
    ".md": "text/markdown",
}


def mime_for(ext: str) -> str:
    """MIME-тип по расширению; всё незнакомое — обычный текст."""
    return _MIME_BY_EXT.get(ext, "text/plain")


def is_plain_text_ext(ext: str) -> bool:
    """Формат, у которого документ и есть его текст (.txt/.csv/.md/.json…).

    Для таких клиент может подставлять значения прямо у себя; для всех
    остальных документ собирает сервер (см. ``rebuild_document``).
    """
    return ext in _TEXT_EXT


def masking_replacer(mapping: Mapping):
    """Подстановка «оригинал → плейсхолдер» (обезличивание)."""
    return _replacer(mapping)


def restoring_replacer(mapping: Mapping):
    """Подстановка «плейсхолдер → оригинал» (восстановление)."""
    from .deanonymize import deanonymize

    return lambda text: deanonymize(text, mapping)


def _rewrite_xml_nodes(data: bytes, replace) -> bytes:
    """Переписать текстовые узлы XML-документа, сохранив разметку.

    Для произвольного .xml подстановка идёт поузлово: структуры «абзаца», в
    которой значение могло бы разъехаться по соседним тегам, здесь нет.
    Атрибуты не трогаются — их не читает и извлекатель текста
    (``_read_xml_bytes``), так что в mapping они и не попадают.
    """
    from lxml import etree

    root = etree.fromstring(data)
    for el in root.iter():
        if el.text:
            new = replace(el.text)
            if new != el.text:
                el.text = new
        if el.tail:
            new = replace(el.tail)
            if new != el.tail:
                el.tail = new
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True)


# Теги внутри абзаца ODF, которые несут только текст и его оформление: такой
# абзац можно безопасно схлопнуть в один текстовый узел. Всё остальное
# (draw:frame с картинкой, сноска, вложенная таблица) схлопывать нельзя.
_ODT_INLINE_TAGS = frozenset(
    {
        "span", "a", "s", "tab", "line-break", "bookmark", "bookmark-start",
        "bookmark-end", "soft-page-break", "sequence", "reference-mark",
        "reference-mark-start", "reference-mark-end", "date", "time",
        "page-number", "page-count", "title", "subject", "author-name",
        "author-initials", "file-name", "chapter", "sender-firstname",
        "sender-lastname", "sender-company", "variable-set", "variable-get",
    }
)


def _rewrite_odt_paragraphs(data: bytes, replace) -> bytes:
    """Переписать абзацы content.xml/styles.xml внутри .odt, сохранив всё
    остальное содержимое архива.

    Подстановка идёт по ЦЕЛОМУ абзацу, а не по отдельным текстовым узлам: в
    ODF значение часто разорвано на несколько ``text:span`` (правка, смена
    шрифта), и поузловая замена такое значение просто не нашла бы — в
    выдаваемом документе оно осталось бы открытым. Абзац при этом схлопывается
    в один текстовый узел: внутреннее оформление теряется, как и в .docx-пути
    (``_rewrite_docx``), сам документ — нет.
    """
    import io
    import zipfile

    from lxml import etree

    def local(el) -> str:
        return etree.QName(el).localname if isinstance(el.tag, str) else ""

    def rewrite_part(xml_bytes: bytes) -> bytes:
        root = etree.fromstring(xml_bytes)
        # Список собирается ЗАРАНЕЕ: ниже абзацы перестраиваются, а менять
        # дерево во время root.iter() нельзя — итератор теряет место и молча
        # пропускает следующие элементы (проверено: второй абзац оставался
        # необезличенным).
        paragraphs = [el for el in root.iter() if local(el) in ("p", "h")]
        for el in paragraphs:
            full = "".join(el.itertext())
            if not full:
                continue
            new = replace(full)
            if new == full:
                continue
            if all(local(child) in _ODT_INLINE_TAGS for child in el.iter() if child is not el):
                # Внутри только текстовая разметка — схлопываем абзац в один
                # узел, как в .docx-пути.
                for child in list(el):
                    el.remove(child)
                el.text = new
            else:
                # В абзаце есть рамка, картинка, сноска или вложенная
                # таблица: схлопывание уничтожило бы их вместе с содержимым.
                # Правим поузлово — значение, разорванное по span'ам, здесь
                # может не найтись, но документ остаётся целым.
                for node in el.iter():
                    if node.text:
                        node.text = replace(node.text)
                    if node.tail and node is not el:
                        node.tail = replace(node.tail)
        return etree.tostring(root, encoding="UTF-8", xml_declaration=True)

    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        names = src.namelist()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            # "mimetype" обязан лежать первым и без сжатия — иначе ODF-файл не
            # опознаётся (требование стандарта, а не прихоть LibreOffice).
            if "mimetype" in names:
                out.writestr(
                    zipfile.ZipInfo("mimetype"), src.read("mimetype"), zipfile.ZIP_STORED
                )
            for name in names:
                if name == "mimetype":
                    continue
                payload = src.read(name)
                if name in ("content.xml", "styles.xml"):
                    payload = rewrite_part(payload)
                out.writestr(name, payload)
    return buf.getvalue()


def _rewrite_xlsx_cells(data: bytes, replace, keep_vba: bool = False) -> bytes:
    """Переписать значения ячеек книги Excel, сохранив саму книгу.

    Нестроковые значения тоже проходят через подстановку — через ``str()``,
    ровно как их видит извлекатель текста (``_read_xlsx_bytes``). Иначе
    телефон или счёт, записанные в ячейку ЧИСЛОМ, оказались бы замаскированы в
    предпросмотре и остались открытыми в самом файле — то есть утечка, которую
    пользователь не увидел бы.

    Формулы пропускаются: подстановка внутри ``=СЦЕПИТЬ(...)`` сломала бы
    расчёт, а текст формулы в mapping и не попадает.
    """
    import io

    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), keep_vba=keep_vba)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if value is None:
                    continue
                if isinstance(value, str) and value.startswith("="):
                    continue
                original = value if isinstance(value, str) else str(value)
                new = replace(original)
                if new != original:
                    cell.value = new
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def text_to_xlsx_bytes(text: str) -> bytes:
    """Собрать простую книгу Excel из текста: строка — ряд, табуляция — ячейка.

    Запасной путь для .xls на хосте без LibreOffice; разбор совпадает с тем,
    как таблицу читает ``_read_xls_bytes``.
    """
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for line in text.split("\n"):
        ws.append(line.split("\t"))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@dataclass(frozen=True)
class PreparedDocument:
    """Во что превращён загруженный файл, чтобы его можно было переписать.

    ``working_data`` — документ, по которому идут и извлечение текста, и
    подстановка: сам файл, если формат правится напрямую, результат
    конвертации, если нет, и ``None``, если переписывать нечего (останется
    текст). ``output_extension`` — то, что в итоге получит пользователь.
    """

    extension: str
    working_extension: str
    working_data: bytes | None
    output_extension: str
    source: str  # "original" | "converted" | "text"

    def degraded(self) -> "PreparedDocument":
        """То же, но без правимого оригинала.

        Нужен, когда конвертация формально удалась, а результат не читается:
        падать из-за этого незачем — документ просто собирается из текста.
        Обратный шаг конвертации (.docx → .rtf) при этом тоже отпадает.
        """
        out = ".docx" if self.output_extension == ".rtf" else self.output_extension
        return PreparedDocument(self.extension, "", None, out, "text")


# Форматы, которые правятся напрямую, без конвертации.
_DIRECT_REWRITE = (".docx", ".xlsx", ".xlsm", ".odt", ".xml")


def prepare_document(name: str, data: bytes) -> PreparedDocument:
    """Решить, как этот файл будет переписан и в каком формате отдан.

    Единственное место, где живёт политика форматов: и обезличивание, и
    восстановление ходят сюда, чтобы не разъезжаться.
    """
    ext = Path(name).suffix.lower()
    if ext in _DIRECT_REWRITE:
        return PreparedDocument(ext, ext, data, ext, "original")
    if ext in _TEXT_EXT:
        # Простой текст сам по себе и есть документ: переписывать отдельно
        # нечего, обезличенный текст — уже готовый файл того же типа.
        return PreparedDocument(ext, "", None, ext, "original")
    if ext == ".doc":
        converted = doc_to_docx_bytes(data)
        if converted is not None:
            return PreparedDocument(ext, ".docx", converted, ".docx", "converted")
        return PreparedDocument(ext, "", None, ".docx", "text")
    if ext == ".xls":
        converted = xls_to_xlsx_bytes(data)
        if converted is not None:
            return PreparedDocument(ext, ".xlsx", converted, ".xlsx", "converted")
        return PreparedDocument(ext, "", None, ".xlsx", "text")
    if ext == ".rtf":
        converted = _libreoffice_convert(data, ".rtf", "docx:MS Word 2007 XML", ".docx")
        if converted is not None:
            return PreparedDocument(ext, ".docx", converted, ".rtf", "converted")
        return PreparedDocument(ext, "", None, ".docx", "text")
    # .pdf и всё незнакомое — только текст (см. комментарий к блоку выше).
    return PreparedDocument(ext, "", None, ".txt", "text")


def rebuild_document(prepared: PreparedDocument, replace, text: str) -> tuple[bytes, str, str]:
    """``(байты, расширение, source)`` итогового документа.

    ``replace`` — подстановка над строкой (в одну сторону при обезличивании, в
    другую при восстановлении), ``text`` — уже преобразованный плоский текст,
    из которого собирается документ, когда переписывать нечего.
    """
    if prepared.working_data is None:
        if prepared.output_extension == ".docx":
            return text_to_docx_bytes(text), ".docx", prepared.source
        if prepared.output_extension == ".xlsx":
            return text_to_xlsx_bytes(text), ".xlsx", prepared.source
        return text.encode("utf-8"), prepared.output_extension, prepared.source

    work_ext = prepared.working_extension
    if work_ext == ".docx":
        rewritten = _rewritten_docx(prepared.working_data, replace)
    elif work_ext in (".xlsx", ".xlsm"):
        rewritten = _rewrite_xlsx_cells(
            prepared.working_data, replace, keep_vba=(work_ext == ".xlsm")
        )
    elif work_ext == ".odt":
        rewritten = _rewrite_odt_paragraphs(prepared.working_data, replace)
    else:  # .xml
        rewritten = _rewrite_xml_nodes(prepared.working_data, replace)

    if prepared.output_extension == work_ext:
        return rewritten, work_ext, prepared.source
    # Остался обратный шаг конвертации (сейчас только .docx → .rtf). Если он
    # не удался, отдаём то, что уже переписано: формат не тот, зато документ
    # на руках и обезличен.
    back = docx_to_rtf_bytes(rewritten)
    if back is None:
        return rewritten, work_ext, prepared.source
    return back, prepared.output_extension, prepared.source


def anonymize_to_files(
    path: str | Path, anon: Anonymizer, out_dir: str | Path | None = None
) -> dict[str, Path]:
    """Anonymize a document and write .anon.txt, .map.json, (and .anon.docx).

    Returns a dict of the written paths plus the in-memory result counts.
    """
    path = Path(path)
    out_dir = Path(out_dir) if out_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem

    anon_text, mapping = anonymize_document(path, anon)

    txt_path = out_dir / f"{stem}.anon.txt"
    map_path = out_dir / f"{stem}.map.json"
    txt_path.write_text(anon_text, encoding="utf-8")
    save_mapping(mapping, map_path)

    written = {"text": txt_path, "mapping": map_path}
    if path.suffix.lower() == ".docx":
        docx_path = out_dir / f"{stem}.anon.docx"
        write_anonymized_docx(path, docx_path, mapping)
        written["docx"] = docx_path
    return written
