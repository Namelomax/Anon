"""Streamlit UI for the anonymizer.

Run:
    streamlit run anonymizer/app.py

Two tabs:
* Анонимизация — upload .docx/.txt or paste text, choose detectors, get the
  redacted text + mapping, download both (and an anonymized .docx).
* Деанонимизация — paste/upload anonymized text + the mapping JSON, restore.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _ROOT)

from anonymizer.deanonymize import deanonymize, find_unknown_placeholders  # noqa: E402
from anonymizer.documents import anonymized_docx_bytes, read_text_from_bytes  # noqa: E402

st.set_page_config(page_title="Анонимизатор ПДн", page_icon="🛡️", layout="wide")


def run_worker(text: str, ner_backend: str, corporate: bool, use_llm: bool,
               llm_base_url: str, llm_model: str) -> dict:
    """Anonymize in a separate process (keeps PyTorch out of the Streamlit thread).

    Returns ``{anonymized_text, mapping, summary}``. Raises RuntimeError on failure.
    """
    ner = {"GLiNER": "gliner", "Natasha": "natasha"}.get(ner_backend, "none")
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / "in.txt"
        out = Path(d) / "out.json"
        inp.write_text(text, encoding="utf-8")
        worker_py = str(Path(__file__).resolve().parent / "worker.py")
        cmd = [sys.executable, worker_py,
               "--in", str(inp), "--out", str(out), "--ner", ner]
        if corporate:
            cmd.append("--corporate")
        if use_llm:
            cmd += ["--llm", "--llm-base-url", llm_base_url, "--llm-model", llm_model]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, cwd=_ROOT)
        if not out.exists():
            tail = (proc.stderr or proc.stdout or "нет вывода")[-2000:]
            raise RuntimeError(f"Воркер завершился с ошибкой:\n{tail}")
        return json.loads(out.read_text(encoding="utf-8"))


def _label_of(placeholder: str) -> str:
    return placeholder.strip("[]").rsplit("_", 1)[0]


def _mapping_markdown(mapping: dict) -> str:
    """Render the mapping as a Markdown table (no dynamic JS component)."""
    lines = ["| Плейсхолдер | Тип | Оригинал |", "|---|---|---|"]
    for ph, orig in mapping.items():
        safe = orig.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{ph}` | {_label_of(ph)} | {safe} |")
    return "\n".join(lines)


def _build_zip(stem: str, doc_name: str, doc_bytes: bytes, mapping_json: str) -> bytes:
    """Bundle the anonymized document + mapping into a single ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(doc_name, doc_bytes)
        z.writestr(f"{stem}.map.json", mapping_json)
    return buf.getvalue()


# --- Remote GPU backend (fixed; server.py on JupyterHub) -------------------
# Сервер не меняется — адрес и токен зашиты. Вся обработка (GLiNER + LLM +
# корпоративные данные) идёт на GPU-сервере; здесь — только интерфейс.
REMOTE_URL = "https://jh.interfonica.cloud/user/ts-bdfzametjv73gxxu/proxy/8000"
REMOTE_KEY = "1913de7b07f34d61a28792a88f613767"

st.sidebar.header("🌐 Бэкенд")
st.sidebar.success("GPU-сервер (GLiNER + LLM + корпоративные данные)")
st.sidebar.caption("Обработка на удалённом сервере. Локально — только интерфейс.")

st.sidebar.header("⚙️ Этапы обработки")
st.sidebar.caption("Можно отключить любой слой — например, оставить только GLiNER.")
stage_regex = st.sidebar.checkbox("Правила (regex)", value=True,
                                  help="Телефоны, email, ИНН, паспорта, даты…")
stage_corporate = st.sidebar.checkbox("Корпоративные (суммы/договоры)", value=True)
stage_ner = st.sidebar.checkbox("GLiNER (ФИО, города, организации)", value=True)
ner_threshold = st.sidebar.slider(
    "Чувствительность GLiNER", min_value=0.20, max_value=0.70, value=0.45, step=0.05,
    disabled=not stage_ner,
    help="Ниже порог → больше находок (выше полнота, но и больше лишнего). "
         "Выше → строже (меньше ложных срабатываний).",
)
stage_llm = st.sidebar.checkbox("LLM (добивание сложных случаев)", value=True)
STAGES = {
    "regex": stage_regex,
    "corporate": stage_corporate,
    "ner": stage_ner,
    "llm": stage_llm,
}
if stage_ner:
    STAGES["ner_threshold"] = ner_threshold
if not any(STAGES.get(k) for k in ("regex", "corporate", "ner", "llm")):
    st.sidebar.error("Включите хотя бы один этап.")

st.title("🛡️ Анонимизатор персональных данных")

tab_anon, tab_deanon = st.tabs(["🔒 Анонимизация", "🔑 Деанонимизация"])

# --- Tab: Anonymize --------------------------------------------------------
with tab_anon:
    st.subheader("Исходный документ")
    col_in1, col_in2 = st.columns(2)
    uploaded = col_in1.file_uploader("Загрузить файл (.docx / .txt)", type=["docx", "txt"])
    pasted = col_in2.text_area("…или вставить текст", height=160, placeholder="Вставьте текст здесь")

    if st.button("🔒 Обезличить", type="primary"):
        try:
            if uploaded is not None:
                src_bytes = uploaded.getvalue()
                name = uploaded.name
                is_docx = name.lower().endswith(".docx")
                text = read_text_from_bytes(name, src_bytes)
            elif pasted.strip():
                src_bytes, name, is_docx, text = b"", "text.txt", False, pasted
            else:
                st.warning("Загрузите файл или вставьте текст.")
                st.stop()

            import time

            from anonymizer.remote_client import anonymize_remote

            t0 = time.time()
            with st.spinner("Обрабатываю на GPU-сервере…"):
                result = anonymize_remote(text, REMOTE_URL, REMOTE_KEY, stages=STAGES)
            elapsed = time.time() - t0

            stem = Path(name).stem
            mapping = result["mapping"]
            mapping_json = json.dumps(mapping, ensure_ascii=False, indent=2)
            # Anonymized document in the SAME format as the source.
            if is_docx:
                doc_bytes = anonymized_docx_bytes(src_bytes, mapping)
                doc_name = f"{stem}.anon.docx"
                doc_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            else:
                doc_bytes = result["anonymized_text"].encode("utf-8")
                doc_name = f"{stem}.anon.txt"
                doc_mime = "text/plain"
            zip_bytes = _build_zip(stem, doc_name, doc_bytes, mapping_json)

            st.session_state["anon_result"] = {
                "text": result["anonymized_text"],
                "mapping": mapping,
                "summary": result["summary"],
                "orig_len": len(text),
                "stem": stem,
                "doc_name": doc_name,
                "doc_bytes": doc_bytes,
                "doc_mime": doc_mime,
                "mapping_json": mapping_json,
                "zip": zip_bytes,
                "elapsed": elapsed,
            }
        except Exception as exc:  # show errors in UI instead of crashing
            st.error(f"Ошибка обработки: {exc}")
            st.exception(exc)

    res = st.session_state.get("anon_result")
    if res:
        elapsed = res.get("elapsed", 0.0)
        chars = res["orig_len"]
        speed = f" · {chars / elapsed:.0f} символов/с" if elapsed else ""
        c_t, c_e, c_s = st.columns(3)
        c_t.metric("⏱️ Время обработки", f"{elapsed:.1f} с")
        c_e.metric("Сущностей", len(res["mapping"]))
        c_s.metric("Символов", chars)
        st.success(
            f"Обработано за {elapsed:.1f} с{speed} · "
            f"найдено уникальных сущностей: {len(res['mapping'])}"
        )
        if res["summary"]:
            summary_str = " · ".join(f"**{k}**: {v}" for k, v in res["summary"].items())
            st.markdown("По типам: " + summary_str)

        st.subheader("📦 Скачать")
        st.download_button(
            "⬇️ Скачать ZIP (документ + маппинг)",
            res["zip"], file_name=f"{res['stem']}_anonymized.zip",
            mime="application/zip", type="primary",
        )
        c1, c2 = st.columns(2)
        c1.download_button(
            f"⬇️ Документ ({res['doc_name']})", res["doc_bytes"],
            file_name=res["doc_name"], mime=res["doc_mime"],
        )
        c2.download_button(
            "⬇️ Маппинг (.json)", res["mapping_json"],
            file_name=f"{res['stem']}.map.json", mime="application/json",
        )

        st.subheader("Обезличенный текст")
        st.text_area("Результат", res["text"], height=260, key="anon_out_text")

        st.subheader("Маппинг (ключ для восстановления — хранить отдельно!)")
        if res["mapping"]:
            with st.expander(f"Показать таблицу ({len(res['mapping'])} сущностей)", expanded=False):
                st.markdown(_mapping_markdown(res["mapping"]))

# --- Tab: Deanonymize ------------------------------------------------------
with tab_deanon:
    st.subheader("Восстановление по маппингу (без ИИ)")

    last = st.session_state.get("anon_result")
    use_last = False
    if last and last.get("mapping"):
        use_last = st.checkbox(
            f"Использовать маппинг последнего документа "
            f"(«{last['stem']}», {len(last['mapping'])} сущностей)",
            value=True,
            help="Маппинг подставится автоматически — загрузите только изменённый документ.",
        )

    col_a, col_b = st.columns(2)
    anon_file = col_a.file_uploader(
        "Изменённый документ (.txt / .docx)", type=["txt", "docx"], key="de_doc"
    )
    map_file = col_b.file_uploader(
        "Маппинг (.json)", type=["json"], key="de_map", disabled=use_last
    )
    anon_pasted = st.text_area("…или вставить обезличенный текст", height=140, key="de_paste")

    if st.button("🔑 Восстановить", type="primary"):
        # 1) Mapping: last document's, or an uploaded file
        if use_last:
            mapping = last["mapping"]
        elif map_file is not None:
            try:
                mapping = json.loads(map_file.getvalue().decode("utf-8"))
            except json.JSONDecodeError as exc:
                st.error(f"Некорректный JSON маппинга: {exc}")
                st.stop()
        else:
            st.warning("Загрузите маппинг (.json) или включите «использовать последний».")
            st.stop()

        # 2) Document: .docx (restore into .docx), .txt, or pasted text
        is_docx = anon_file is not None and anon_file.name.lower().endswith(".docx")
        if anon_file is not None:
            if is_docx:
                anon_text = read_text_from_bytes(anon_file.name, anon_file.getvalue())
            else:
                anon_text = anon_file.getvalue().decode("utf-8")
        elif anon_pasted.strip():
            anon_text = anon_pasted
        else:
            st.warning("Загрузите изменённый документ или вставьте текст.")
            st.stop()

        restored = deanonymize(anon_text, mapping)
        leftover = find_unknown_placeholders(restored, mapping)

        st.subheader("Восстановленный текст")
        st.text_area("Результат", restored, height=260, key="de_out")
        if leftover:
            st.warning(f"Плейсхолдеры без значения в маппинге: {sorted(set(leftover))}")
        else:
            st.success("Все плейсхолдеры восстановлены.")

        c1, c2 = st.columns(2)
        c1.download_button(
            "⬇️ Восстановленный текст (.txt)", restored, file_name="restored.txt",
            mime="text/plain",
        )
        if is_docx:
            from anonymizer.documents import deanonymized_docx_bytes

            c2.download_button(
                "⬇️ Восстановленный .docx",
                deanonymized_docx_bytes(anon_file.getvalue(), mapping),
                file_name="restored.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
