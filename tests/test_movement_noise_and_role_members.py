"""v1.27.0 quality fixes — register-movement noise scrub (Fix 1) and roles
include_members (Fix 2).

Fix 1: ``Движения.Записать()`` (the commit-all recordset method) and its siblings
must never be indexed / returned as a register. Covered on three layers:
  * build extractor ``_extract_movements`` (lookahead-before-capture + stop-set);
  * centralized read-time scrub in ``IndexReader.get_register_movements`` /
    ``get_register_writers`` — cleans PRE-EXISTING indexes with no rebuild, via a
    REAL SQLite table (not mocked rows), so the None-vs-[] contract is exercised;
  * live (no-index) paths ``find_register_movements`` / ``find_register_writers``.

Fix 2: ``get_object_profile(...).sections.roles`` must count member-level grants
(``Document.X.Command.Y``) the same way ``find_roles`` does, without over-matching
prefix homonyms (``Document.XYZ``).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import (
    IndexBuilder,
    IndexReader,
    _extract_movements,
)
from rlm_tools_bsl.format_detector import BslFileInfo, detect_format
from rlm_tools_bsl.helpers import make_helpers


# ── shared fixtures ─────────────────────────────────────────────────────────


def _doc_object_info(rel: str = "Documents/Тест/Ext/ObjectModule.bsl") -> BslFileInfo:
    return BslFileInfo(
        relative_path=rel,
        category="Documents",
        object_name="Тест",
        module_type="ObjectModule",
        form_name=None,
        command_name=None,
        is_form_module=False,
    )


def _build_index(tmpdir: str, doc_name: str = "РеализацияТоваров") -> str:
    """Minimal buildable config with one Document (empty modules → no auto movements)."""
    doc = os.path.join(tmpdir, "Documents", doc_name, "Ext")
    os.makedirs(doc)
    with open(os.path.join(doc, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write("// doc\n")
    with open(os.path.join(doc, "ManagerModule.bsl"), "w", encoding="utf-8") as f:
        f.write("// mgr\n")
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    return str(IndexBuilder().build(tmpdir, build_calls=False, build_metadata=True))


def _reader_with_movements(tmpdir: str, rows: list[tuple], doc_name: str = "РеализацияТоваров") -> IndexReader:
    db_path = _build_index(tmpdir, doc_name=doc_name)
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO register_movements (document_name, register_name, source, file) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return IndexReader(db_path)


def _bsl_with_reader(tmpdir: str, reader: IndexReader) -> dict:
    helpers, resolve_safe = make_helpers(tmpdir)
    return make_bsl_helpers(
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(tmpdir),
        idx_reader=reader,
    )


# ── Fix 1a: build extractor (strict set — catches truncation artefacts) ──────


def test_extract_movements_rejects_bsl_methods_strict_set():
    content = (
        "Движения.Записать();\n"
        "Движения.Записать ();\n"  # space before '(' — \s* in lookahead must still reject
        "Движения.ТоварыНаСкладах.Добавить();\n"
        "Движения.Продажи.Очистить();\n"
    )
    info = _doc_object_info()
    rows = _extract_movements(content, info, info.relative_path)
    names = {name for (name, src, _f) in rows if src == "code"}
    # STRICT equality (Codex #4): also fails on a truncated 'Записат'/'Записа' leak.
    assert names == {"ТоварыНаСкладах", "Продажи"}


# ── Fix 1b: centralized read-time scrub via REAL SQLite + IndexReader ────────


def test_reader_movements_drops_code_noise_keeps_real_and_nonsource():
    with tempfile.TemporaryDirectory() as tmp:
        reader = _reader_with_movements(
            tmp,
            [
                ("Док1", "Записать", "code", "om.bsl"),
                ("Док1", "ТоварыНаСкладах", "code", "om.bsl"),
                ("Док1", "МеханизмУУ", "erp_mechanism", "mm.bsl"),
            ],
        )
        res = reader.get_register_movements("Док1")
        pairs = {(r["register_name"], r["source"]) for r in res}
        assert ("ТоварыНаСкладах", "code") in pairs
        assert ("МеханизмУУ", "erp_mechanism") in pairs  # non-code untouched
        assert ("Записать", "code") not in pairs
        assert all(r["register_name"] != "Записать" for r in res)


def test_reader_movements_all_noise_returns_empty_not_none():
    with tempfile.TemporaryDirectory() as tmp:
        reader = _reader_with_movements(
            tmp,
            [
                ("Док2", "Записать", "code", "om.bsl"),
                ("Док2", "Очистить", "code", "om.bsl"),
            ],
        )
        res = reader.get_register_movements("Док2")
        # [] = "table present, all rows were noise" (authoritative); None = empty/missing table.
        assert res is not None
        assert res == []


def test_reader_movements_nonsource_noise_name_is_kept():
    with tempfile.TemporaryDirectory() as tmp:
        # A register literally named 'Записать' but discovered by a quoted-name regex
        # (erp_mechanism) is a real register, NOT the collection method — keep it.
        reader = _reader_with_movements(tmp, [("Док3", "Записать", "erp_mechanism", "mm.bsl")])
        res = reader.get_register_movements("Док3")
        assert {(r["register_name"], r["source"]) for r in res} == {("Записать", "erp_mechanism")}


def test_reader_writers_noise_name_drops_code_rows():
    with tempfile.TemporaryDirectory() as tmp:
        reader = _reader_with_movements(
            tmp,
            [
                ("ДокА", "Записать", "code", "a.bsl"),
                ("ДокБ", "Записать", "erp_mechanism", "b.bsl"),  # non-code stays
            ],
        )
        writers = reader.get_register_writers("Записать")
        docs = {(w["document_name"], w["source"]) for w in writers}
        assert ("ДокА", "code") not in docs
        assert ("ДокБ", "erp_mechanism") in docs


def test_reader_writers_real_name_unaffected():
    with tempfile.TemporaryDirectory() as tmp:
        reader = _reader_with_movements(tmp, [("ДокА", "ТоварыНаСкладах", "code", "a.bsl")])
        writers = reader.get_register_writers("ТоварыНаСкладах")
        assert {(w["document_name"], w["source"]) for w in writers} == {("ДокА", "code")}


def test_reader_movements_none_on_empty_table():
    with tempfile.TemporaryDirectory() as tmp:
        # No rows injected → empty register_movements table → None (capability contract).
        db_path = _build_index(tmp)
        reader = IndexReader(db_path)
        assert reader.get_register_movements("РеализацияТоваров") is None


# ── Fix 1b downstream: get_object_profile registers section is also clean ────


def test_get_object_profile_registers_section_clean_of_code_noise():
    with tempfile.TemporaryDirectory() as tmp:
        reader = _reader_with_movements(
            tmp,
            [
                ("РеализацияТоваров", "Записать", "code", "om.bsl"),
                ("РеализацияТоваров", "Продажи", "code", "om.bsl"),
                ("РеализацияТоваров", "МеханизмУУ", "erp_mechanism", "mm.bsl"),
            ],
        )
        bsl = _bsl_with_reader(tmp, reader)
        prof = bsl["get_object_profile"]("РеализацияТоваров", sections=["registers"])
        section = prof["sections"]["registers"]
        items = section["items"]  # [{register, source}]
        assert not any(i["register"] == "Записать" and i["source"] == "code" for i in items)
        names = {i["register"] for i in items}
        assert "Продажи" in names and "МеханизмУУ" in names
        # summary.code_registers is a COUNT (Codex #12) and must exclude the scrubbed row.
        assert section["summary"]["code_registers"] == 1


# ── Fix 1c: live (no-index) paths ───────────────────────────────────────────

_LIVE_DOC_BODY = (
    "Процедура ОбработкаПроведения(Отказ) Экспорт\n"
    "    Движения.Записать();\n"  # method call — must be ignored
    "    Движения.ТоварыНаСкладах.Записать = Истина;\n"  # real register write
    "    Движения.ТоварыНаСкладах.Очистить();\n"
    "КонецПроцедуры\n"
)


def _bsl_no_index(tmpdir: str) -> dict:
    doc = os.path.join(tmpdir, "Documents", "МойДок", "Ext")
    os.makedirs(doc)
    with open(os.path.join(doc, "ObjectModule.bsl"), "w", encoding="utf-8") as f:
        f.write(_LIVE_DOC_BODY)
    with open(os.path.join(tmpdir, "Configuration.xml"), "w") as f:
        f.write("<Configuration/>")
    helpers, resolve_safe = make_helpers(tmpdir)
    return make_bsl_helpers(  # no idx_reader → filesystem live path
        base_path=tmpdir,
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(tmpdir),
    )


def test_find_register_movements_live_path_drops_noise():
    with tempfile.TemporaryDirectory() as tmp:
        bsl = _bsl_no_index(tmp)
        res = bsl["find_register_movements"]("МойДок")
        names = {r["name"] for r in res["code_registers"]}
        assert "Записать" not in names
        assert names == {"ТоварыНаСкладах"}


def test_find_register_writers_noise_name_live_path_empty():
    with tempfile.TemporaryDirectory() as tmp:
        bsl = _bsl_no_index(tmp)
        assert bsl["find_register_writers"]("Записать")["total_writers"] == 0
        # sanity: the real register is still found on the same live path
        assert bsl["find_register_writers"]("ТоварыНаСкладах")["total_writers"] >= 1


# ── Fix 2: roles include_members (member grants counted, homonyms excluded) ──


def test_object_profile_roles_counts_member_grants_without_homonyms():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _build_index(tmp, doc_name="РеализацияТоваров")
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
            [
                ("РольОсновная", "Document.РеализацияТоваров", "Read", "r1.xml"),
                ("РольЧлен", "Document.РеализацияТоваров.Command.СоздатьНаОсновании", "Use", "r2.xml"),
                ("РольОмоним", "Document.РеализацияТоваровДоп", "Read", "r3.xml"),  # prefix homonym
            ],
        )
        conn.commit()
        conn.close()
        reader = IndexReader(db_path)

        # Reader-level: the fix is include_members=True. Default (object-only) undercounts.
        assert len(reader.get_roles_exact("Document.РеализацияТоваров")) == 1
        assert len(reader.get_roles_exact("Document.РеализацияТоваров", include_members=True)) == 2

        # Profile aggregate now matches (2 roles), and the anchored WHERE excludes the homonym.
        bsl = _bsl_with_reader(tmp, reader)
        prof = bsl["get_object_profile"]("РеализацияТоваров", sections=["roles"])
        section = prof["sections"]["roles"]
        assert section["summary"]["roles"] == 2
        assert {i["role_name"] for i in section["items"]} == {"РольОсновная", "РольЧлен"}


def test_get_roles_exact_escapes_like_wildcard_underscore():
    """A literal '_' in the ref (legal 1C identifier char) must NOT act as a LIKE wildcard
    in the include_members branch — else a near-homonym's member grant is over-counted."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = _build_index(tmp)  # any valid index; role_rights queried directly by ref
        conn = sqlite3.connect(db_path)
        conn.executemany(
            "INSERT INTO role_rights (role_name, object_name, right_name, file) VALUES (?, ?, ?, ?)",
            [
                ("РольОсн", "Document.тст_Смета", "Read", "r1.xml"),
                ("РольЧлен", "Document.тст_Смета.Command.Провести", "Use", "r2.xml"),
                # If '_' were a wildcard, 'тстАСмета' would match 'тст_Смета' → this must stay excluded.
                ("РольЧужая", "Document.тстАСмета.Command.Икс", "Use", "r3.xml"),
            ],
        )
        conn.commit()
        conn.close()
        reader = IndexReader(db_path)
        names = {r["role_name"] for r in reader.get_roles_exact("Document.тст_Смета", include_members=True)}
        assert names == {"РольОсн", "РольЧлен"}
