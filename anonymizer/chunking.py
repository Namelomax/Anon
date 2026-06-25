"""Offset-preserving text chunking, shared by the GLiNER and LLM detectors.

Both models degrade on very long inputs (GLiNER truncates past its context
window; the LLM loses recall and may overflow its output budget). We split the
text into chunks no longer than ``max_chars`` and run the model per chunk,
shifting span offsets back to the original text.

Consecutive lines are grouped together up to ``max_chars`` (rather than one
chunk per line) to keep the number of model calls low. Because the original text
is line-joined with ``"\\n"``, a group of consecutive lines joined by ``"\\n"``
is exactly ``text[offset : offset + len(chunk)]`` — so local offsets map back by
simple addition.
"""

from __future__ import annotations


def chunk_text(text: str, max_chars: int, *, group: bool = True) -> list[tuple[int, str]]:
    """Split ``text`` into ``(offset, chunk)`` pieces of at most ~``max_chars``.

    Args:
        max_chars: Target maximum chunk size in characters.
        group: If True (LLM), pack consecutive lines together up to ``max_chars``
            to minimize model calls. If False (GLiNER), keep each line separate
            (over-long lines are split at spaces) — small focused inputs give
            span-based NER higher recall.

    Offsets are exact: ``text[offset : offset + len(chunk)] == chunk``.
    """
    if not group:
        return _per_line(text, max_chars)

    out: list[tuple[int, str]] = []
    pos = 0
    buf: list[str] = []
    buf_start = 0
    buf_len = 0
    for line in text.split("\n"):
        line_span = len(line) + 1  # +1 for the "\n" that joined it
        if buf and buf_len + len(line) > max_chars:
            out.append((buf_start, "\n".join(buf)))
            buf, buf_len = [], 0
        if not buf:
            buf_start = pos
        buf.append(line)
        buf_len += line_span
        pos += line_span
    if buf:
        out.append((buf_start, "\n".join(buf)))
    return [(off, chunk) for off, chunk in out if chunk.strip()]


def _per_line(text: str, max_chars: int) -> list[tuple[int, str]]:
    """One chunk per line; over-long lines split at the nearest preceding space."""
    out: list[tuple[int, str]] = []
    pos = 0
    for line in text.split("\n"):
        if len(line) <= max_chars:
            if line.strip():
                out.append((pos, line))
        else:
            i = 0
            while i < len(line):
                end = min(i + max_chars, len(line))
                if end < len(line):
                    sp = line.rfind(" ", i, end)
                    if sp > i:
                        end = sp
                if line[i:end].strip():
                    out.append((pos + i, line[i:end]))
                i = end
        pos += len(line) + 1
    return out
