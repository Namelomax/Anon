"""Read documents, anonymize them as a whole, and write the results back.

Supports ``.docx`` (paragraphs + tables) and plain ``.txt``. The whole document
is anonymized in one pass so the same entity gets the same placeholder
everywhere (e.g. one person -> ``[PERSON_1]`` across all paragraphs).

Outputs:
* ``<name>.anon.txt``  — anonymized plain text
* ``<name>.map.json``  — placeholder -> original mapping (the deanonymize key)
* ``<name>.anon.docx`` — anonymized copy preserving paragraph/table structure
  (only for .docx inputs)
"""

from __future__ import annotations

import re
from pathlib import Path

from .engine import Anonymizer
from .mapping import Mapping, save_mapping


def read_text(path: str | Path) -> str:
    """Read a .docx or .txt file into a single string (newline-joined)."""
    path = Path(path)
    if path.suffix.lower() == ".docx":
        return _read_docx_text(path)
    return path.read_text(encoding="utf-8")


def _iter_docx_paragraphs(document):
    """Yield body paragraphs and all table-cell paragraphs."""
    for para in document.paragraphs:
        yield para
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    yield para


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
    """In-place: replace original values with placeholders in every paragraph.

    Structure is preserved; each paragraph is rewritten into a single run
    (intra-paragraph formatting is not retained — fine for a redacted artifact).
    """
    replace = _replacer(mapping)
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


def read_text_from_bytes(name: str, data: bytes) -> str:
    """Read .docx/.txt content from in-memory bytes (no temp file)."""
    if name.lower().endswith(".docx"):
        import io

        import docx

        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in _iter_docx_paragraphs(document))
    return data.decode("utf-8")


def anonymized_docx_bytes(src_data: bytes, mapping: Mapping) -> bytes:
    """Return an anonymized .docx as bytes, built from source .docx bytes."""
    import io

    import docx

    document = docx.Document(io.BytesIO(src_data))
    _rewrite_docx(document, mapping)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


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
