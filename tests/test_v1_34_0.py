"""v1.34.0 — «честная форма ответа».

Тесты сгруппированы по задачам релиза. Общий принцип: проверяется не «ключ есть»,
а СМЫСЛ контракта — что именно ответ обещает и чего он НЕ обещает.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------
def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


def _make_bsl(root, *, extension_paths=None, private_io=False, **kw):
    """Прямая фабрика. ``private_io=True`` эмулирует production-песочницу:
    именно она передаёт приватные status-aware каналы."""
    sink: dict = {}
    helpers, resolve_safe = make_helpers(str(root), _private_io=sink if private_io else None)
    extra = {}
    if private_io:
        extra["grep_status_fn"] = sink["grep_with_status"]
        extra["catalog_scan_fn"] = sink["scan_bsl_catalog_status"]
    return make_bsl_helpers(
        base_path=str(root),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(root)),
        extension_paths=extension_paths,
        **extra,
        **kw,
    )


@pytest.fixture
def cf(tmp_path):
    """Минимальная MAIN-конфигурация без расширений."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "CommonModules" / "ОбщийМодуль" / "Ext" / "Module.bsl",
        "Процедура ЦелеваяПроцедура() Экспорт\n    Возврат;\nКонецПроцедуры\n",
    )
    _write(
        root / "Documents" / "ТестДок" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    )
    return root


# ---------------------------------------------------------------------------
# Задача 1 — форма ответа git_search / safe_grep + оси охвата
# ---------------------------------------------------------------------------
class TestGrepEnvelope:
    def test_safe_grep_returns_dict_with_results_on_every_path(self, cf):
        bsl = _make_bsl(cf)
        hit = bsl["safe_grep"]("ЦелеваяПроцедура", max_files=50)
        miss = bsl["safe_grep"]("ЗаведомоНетТакого", max_files=50)
        for res in (hit, miss):
            assert isinstance(res, dict)
            assert isinstance(res["results"], list)
            assert res["returned"] == len(res["results"])
        assert hit["results"], "предусловие: совпадение должно быть"
        assert miss["results"] == []
        # `error` у safe_grep НЕТ — его аргументные ошибки остаются ValueError.
        assert "error" not in hit
        with pytest.raises(ValueError):
            bsl["safe_grep"]("(")

    def test_scanned_files_never_exceeds_candidates(self, cf):
        bsl = _make_bsl(cf)
        wide = bsl["safe_grep"]("Процедура", max_files=50)
        assert wide["scanned_files"] <= wide["candidates_total"]
        narrow = bsl["safe_grep"]("Процедура", max_files=1)
        assert narrow["candidates_total"] > 1, "предусловие: кандидатов больше одного"
        # Срез max_files меньше числа кандидатов ⇒ СТРОГО меньше.
        assert narrow["scanned_files"] < narrow["candidates_total"]

    def test_early_stop_does_not_inflate_scanned_files(self, tmp_path):
        """Ранняя остановка: `scanned_files` — ПОПЫТКИ, а не длина списка путей.

        Проверки `scanned_files <= candidates_total` для этого мало — она проходит
        и на завышенном числе.
        """
        root = tmp_path / "big"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        for i in range(300):
            _write(
                root / "CommonModules" / f"Мод{i:03d}" / "Ext" / "Module.bsl",
                "Процедура Общая() Экспорт\n    ЦелеваяСтрока = 1;\nКонецПроцедуры\n",
            )
        bsl = _make_bsl(root)
        res = bsl["safe_grep"]("ЦелеваяСтрока", max_files=300, _result_cap=5)
        assert res["candidates_total"] == 300
        assert res["truncated"] is True
        # Батч Python-ветки — 64 файла; читается ОДИН насыщенный батч, а не все 300.
        assert res["scanned_files"] <= 64, res["scanned_files"]

    def test_read_failure_is_counted_and_not_cached_away(self, cf, monkeypatch):
        """Стабильный отказ чтения виден как `failed_files`, и ВТОРОЙ вызов не
        превращает его в cache-hit с нулём."""
        bsl = _make_bsl(cf, private_io=True)
        import pathlib

        real_read = pathlib.Path.read_text

        def _boom(self, *a, **kw):
            if self.name == "Module.bsl" and "ОбщийМодуль" in str(self):
                raise PermissionError("denied")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "read_text", _boom)
        first = bsl["safe_grep"]("ЦелеваяПроцедура", name_hint="ОбщийМодуль", max_files=5)
        second = bsl["safe_grep"]("ЦелеваяПроцедура", name_hint="ОбщийМодуль", max_files=5)
        assert first["failed_files"] == 1, first
        assert second["failed_files"] == 1, "неуспешный проход не имеет права закешироваться"

    def test_read_status_complete_distinguishes_legacy_callback(self, cf):
        """`read_status_complete` — ДОКАЗУЕМОСТЬ статуса, а не инверсия failed_files.

        Legacy-фабрика без приватного канала не может исключить молча проглоченный
        отказ, поэтому честно отдаёт False; production-канал на той же читаемой
        фикстуре даёт True И нулевой failed_files.
        """
        legacy = _make_bsl(cf)
        prod = _make_bsl(cf, private_io=True)
        # Регекс-паттерн: git-fast-path не задействован, реально исполняется base-попытка.
        r_legacy = legacy["safe_grep"]("Целевая.роцедура", max_files=50)
        r_prod = prod["safe_grep"]("Целевая.роцедура", max_files=50)
        assert r_legacy["results"], "предусловие: строки найдены обеими фабриками"
        assert r_legacy["read_status_complete"] is False
        assert r_legacy["failed_files"] == 0, "выдуманных отказов быть не должно"
        assert r_prod["read_status_complete"] is True
        assert r_prod["failed_files"] == 0

    def test_zero_base_attempts_keep_status_complete(self, cf):
        """Ноль base-попыток тоже оставляет True — флаг не свойство фабрики навсегда."""
        bsl = _make_bsl(cf)
        res = bsl["safe_grep"]("что-угодно", name_hint="ЗаведомоНетМодуля", max_files=5)
        assert res["candidates_total"] == 0
        assert res["scanned_files"] == 0
        assert res["read_status_complete"] is True

    def test_catalog_enumeration_failure_is_visible(self, cf, monkeypatch):
        """Ошибка перечисления каталога не даёт `catalog_complete=True`."""
        import rlm_tools_bsl.helpers as helpers_mod

        real_scandir = os.scandir
        target = str((cf / "CommonModules").resolve())

        def _boom(path=".", *a, **kw):
            if str(path) == target:
                raise OSError("denied")
            return real_scandir(path, *a, **kw)

        monkeypatch.setattr(helpers_mod.os, "scandir", _boom)
        bsl = _make_bsl(cf, private_io=True, idx_reader=_DummyReader())
        res = bsl["safe_grep"]("Процедура", max_files=50)
        assert res["catalog_complete"] is False
        assert res["catalog_errors"] > 0
        # Известные соседние файлы остаются в выдаче.
        assert any("ТестДок" in r["file"] for r in res["results"]), res["results"]


class _DummyReader:
    """Минимальный reader-стаб: заставляет live-каталог идти каноном `walk`.

    Все методы отдают `None`/пусто, то есть «таблицы нет» — helper-пути честно
    уходят в свои live-ветки, а сам факт наличия reader переключает канон.
    """

    has_fts = False
    has_calls = False

    def __getattr__(self, name):
        def _none(*a, **kw):
            return None

        return _none


# ---------------------------------------------------------------------------
# Задача 2 — find_definition
# ---------------------------------------------------------------------------
class TestFindDefinition:
    def test_no_index_no_hint_scans_live(self, cf):
        bsl = _make_bsl(cf)
        res = bsl["find_definition"]("ЦелеваяПроцедура")
        assert "error" not in res, res
        assert res["_meta"]["source"] == "live"
        assert res["_meta"]["index_used"] is False
        # Старая семантика поля сохранена: live-ветка НИКОГДА не ставит slow_fallback.
        assert res["_meta"]["slow_fallback"] is False
        assert res["total"] == len(res["definitions"]) == 1
        assert res["definitions"][0]["file"].endswith("Module.bsl")

    def test_missing_name_is_empty_not_error(self, cf):
        bsl = _make_bsl(cf)
        res = bsl["find_definition"]("ЗаведомоОтсутствующийМетод")
        assert "error" not in res
        assert res["definitions"] == [] and res["total"] == 0

    def test_partial_is_exactly_not_total_exact(self, cf):
        bsl = _make_bsl(cf)
        for call in (
            lambda: bsl["find_definition"]("ЦелеваяПроцедура"),
            lambda: bsl["find_definition"]("ЦелеваяПроцедура", "ОбщийМодуль"),
            lambda: bsl["find_definition"]("ОбработкаПроведения", "Документ.ТестДок"),
        ):
            res = call()
            assert res["partial"] is not res["_meta"]["total_exact"], res

    def test_object_hint_is_exact_not_substring(self, tmp_path):
        """Объявленное СУЖЕНИЕ recall: живая ветка сравнивает имя объекта ТОЧНО.

        Раньше она шла через substring-matcher и возвращала ещё и однокоренные
        объекты, расходясь с индексной веткой.
        """
        root = tmp_path / "cf"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        body = "Процедура Общая() Экспорт\nКонецПроцедуры\n"
        _write(root / "Documents" / "Заказ" / "Ext" / "ObjectModule.bsl", body)
        _write(root / "Documents" / "ЗаказПоставщику" / "Ext" / "ObjectModule.bsl", body)
        bsl = _make_bsl(root)
        res = bsl["find_definition"]("Общая", "Заказ")
        files = [d["file"] for d in res["definitions"]]
        assert any("/Заказ/" in f.replace("\\", "/") for f in files), files
        assert not any("ЗаказПоставщику" in f for f in files), files

    def test_hint_branches_honour_limit(self, tmp_path):
        """Обе живые hint-ветки соблюдают публичный cap, включая limit=0."""
        root = tmp_path / "cf"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        body = "Процедура Общая() Экспорт\nКонецПроцедуры\n"
        _write(root / "Documents" / "Док" / "Ext" / "ObjectModule.bsl", body)
        _write(root / "Documents" / "Док" / "Ext" / "ManagerModule.bsl", body)
        bsl = _make_bsl(root)
        one = bsl["find_definition"]("Общая", "Док", 1)
        assert len(one["definitions"]) == 1
        assert one["total"] >= 2, one
        assert one["truncated"] is True
        zero = bsl["find_definition"]("Общая", "Док", 0)
        assert zero["definitions"] == []
        assert zero["total"] == one["total"], "census не зависит от размера страницы"
        # rel_hint-ветка: тот же cap.
        rel_zero = bsl["find_definition"]("Общая", "Documents/Док/Ext/ObjectModule.bsl", 0)
        assert rel_zero["definitions"] == []

    def test_unparseable_module_forbids_exact_total(self, cf, monkeypatch):
        """Нечитаемый модуль запрещает точность, даже когда ни один потолок не достигнут."""
        bsl = _make_bsl(cf)
        import rlm_tools_bsl.bsl_helpers as mod

        def _boom(*a, **kw):
            raise OSError("denied")

        monkeypatch.setattr(mod, "_merge_proc_continuations_with_mask", _boom)
        res = bsl["find_definition"]("ЦелеваяПроцедура", "ОбщийМодуль")
        assert res["_meta"]["failed_parse_files"] > 0
        assert res["_meta"]["total_exact"] is False
        assert res["partial"] is True
        # Отказ — НЕ страница, которую можно дочитать.
        assert res["truncated"] is False

    def test_async_declaration_is_found_live(self, tmp_path):
        root = tmp_path / "cf"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        _write(
            root / "CommonModules" / "Асинхронный" / "Ext" / "Module.bsl",
            "Асинх Функция ЖдатьРезультат(\n    Параметр\n) Экспорт\n    Возврат 1;\nКонецФункции\n",
        )
        bsl = _make_bsl(root)
        res = bsl["find_definition"]("ЖдатьРезультат")
        assert res["total"] == 1, res


def test_declaration_grammar_keeps_capture_groups():
    """Общими сделаны АЛЬТЕРНАТИВЫ, а не готовая capturing-композиция: группы 1..4
    у `procedure_def` заморожены, иначе сдвинулась бы нумерация у всех потребителей."""
    import re

    from rlm_tools_bsl.bsl_knowledge import BSL_PATTERNS, bsl_declaration_search_pattern

    m = re.match(BSL_PATTERNS["procedure_def"], "Асинх Функция Тест(А, Б=5) Экспорт", re.IGNORECASE)
    assert m.groups() == ("Функция", "Тест", "А, Б=5", "Экспорт")
    m2 = re.match(BSL_PATTERNS["procedure_def"], "Процедура Простая()", re.IGNORECASE)
    assert m2.group(1) == "Процедура" and m2.group(2) == "Простая"

    # Поисковый префикс: async знает, границу слова уважает, скобку НЕ требует
    # (grep построчный, а многострочная сигнатура закрывается ниже).
    pat = bsl_declaration_search_pattern("Тест")
    assert re.search(pat, "   Асинх Функция Тест(")
    assert re.search(pat, "Procedure Тест(a,")
    assert not re.search(pat, "Процедура ТестДоп(")


# ---------------------------------------------------------------------------
# Задача 3 — count_only и limit
# ---------------------------------------------------------------------------
class TestCounting:
    def test_find_by_type_count_matches_list(self, cf):
        bsl = _make_bsl(cf)
        listed = bsl["find_by_type"]("Documents", limit=10**9)
        counted = bsl["find_by_type"]("Documents", count_only=True)
        assert counted["total"] == len(listed)
        # Строка ответа — МОДУЛЬ; объектов может быть меньше.
        assert counted["unique_objects"] <= counted["total"]
        assert counted["source"] in {"index", "index+live", "live"}
        assert isinstance(counted["extensions_included"], bool)
        assert counted["partial"] is not counted["total_exact"]

    def test_find_by_type_count_ignores_limit(self, cf):
        bsl = _make_bsl(cf)
        assert (
            bsl["find_by_type"]("Documents", count_only=True, limit=1)["total"]
            == bsl["find_by_type"]("Documents", count_only=True, limit=10**9)["total"]
        )

    def test_find_by_type_accepts_both_names_but_not_both_at_once(self, cf):
        bsl = _make_bsl(cf)
        assert bsl["find_by_type"](category="Documents") == bsl["find_by_type"]("Documents")
        with pytest.raises(ValueError):
            bsl["find_by_type"]("Documents", category="Documents")
        with pytest.raises(TypeError):
            bsl["find_by_type"]()
        # Явная пустая строка — прежний валидный вызов, а не «аргумент пропущен».
        assert bsl["find_by_type"]("") == []
        assert bsl["find_by_type"](category="") == []
        empty_count = bsl["find_by_type"]("", count_only=True)
        assert empty_count["total"] == 0 and empty_count["unique_objects"] == 0

    def test_limit_zero_returns_empty_list_for_both(self, cf):
        bsl = _make_bsl(cf)
        assert bsl["find_by_type"]("Documents", limit=0) == []
        assert bsl["find_module"]("", limit=0) == []
        # count_only при limit=0 всё равно считает.
        assert bsl["find_by_type"]("Documents", limit=0, count_only=True)["total"] >= 1

    def test_find_module_default_is_unchanged(self, cf):
        bsl = _make_bsl(cf)
        assert bsl["find_module"]("Общий") == bsl["find_module"]("Общий", limit=50)


# ---------------------------------------------------------------------------
# Задача 4 — пагинация get_overrides
# ---------------------------------------------------------------------------
_CFE_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000002">\n'
    "    <Properties><Name>МоёРасш</Name>"
    "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)


@pytest.fixture
def cf_with_overrides(tmp_path):
    """MAIN + соседнее CFE с НЕСКОЛЬКИМИ перехватами — иначе тест пагинации вакуумен."""
    base = tmp_path / "src" / "cf"
    ext = tmp_path / "src" / "cfe" / "Расш"
    _write(base / "Configuration.xml", _CF_DESCRIPTOR)
    _write(ext / "Configuration.xml", _CFE_DESCRIPTOR)
    for i in range(5):
        _write(
            ext / "Documents" / f"Док{i}" / "Ext" / "ObjectModule.bsl",
            f'&Вместо("Метод{i}")\nПроцедура Расш_Метод{i}()\nКонецПроцедуры\n',
        )
    return base, ext


def test_overrides_pagination_has_no_gaps_or_duplicates(cf_with_overrides):
    base, ext = cf_with_overrides
    bsl = _make_bsl(base, extension_paths=[str(ext)])
    full = bsl["get_overrides"](limit=10**9)
    assert full["total"] >= 5, "предусловие: перехватов должно быть несколько"
    assert full["has_more"] is False and full["truncated"] is False

    page = bsl["get_overrides"](limit=2)
    assert page["offset"] == 0 and page["returned"] == 2
    assert page["has_more"] is True
    assert page["truncated"] is True, "список неполон ОТНОСИТЕЛЬНО total"

    glued: list = []
    offset = 0
    while True:
        chunk = bsl["get_overrides"](limit=2, offset=offset)
        glued.extend(chunk["overrides"])
        if not chunk["has_more"]:
            # ПОСЛЕДНЯЯ страница: has_more уже False, а truncated обязан остаться
            # True — именно их расхождение и есть смысл разделения ключей.
            assert chunk["truncated"] is True, chunk
            break
        offset += chunk["returned"]
    assert glued == full["overrides"], "склейка страниц равна полному набору"

    # Агрегаты считаются по ПОЛНОМУ набору и от страницы не зависят.
    tail = bsl["get_overrides"](limit=2, offset=4)
    for key in ("by_annotation", "by_object_top", "by_extension_top", "unique_objects", "unique_methods", "total"):
        assert tail[key] == full[key], key


# ---------------------------------------------------------------------------
# Задача 5 — totals функциональных опций без limit
# ---------------------------------------------------------------------------
def test_functional_options_totals_without_limit(cf):
    bsl = _make_bsl(cf)
    res = bsl["find_functional_options"]("ТестДок")
    assert res["xml_total"] == len(res["xml_options"])
    assert res["code_total"] == len(res["code_options"])
    assert res["total"] == res["xml_total"] + res["code_total"]
    # Пагинации в этой ветке по-прежнему НЕТ — этим ветки и различаются.
    assert "returned" not in res and "has_more" not in res


# ---------------------------------------------------------------------------
# Задача 6 — owner
# ---------------------------------------------------------------------------
class TestOwner:
    def test_main_session_without_extensions_is_all_main(self, cf, monkeypatch):
        import rlm_tools_bsl.extension_detector as ed

        calls: list = []
        real = ed.detect_extension_context
        monkeypatch.setattr(ed, "detect_extension_context", lambda p: (calls.append(p), real(p))[1])
        bsl = _make_bsl(cf, current_config_role="main", current_config_name="Тест", current_config_root=str(cf))
        for row in bsl["find_module"]("Общий"):
            assert row["owner"] == "main"
        for row in bsl["find_by_type"]("Documents"):
            assert row["owner"] == "main"
        assert bsl["get_module_outline"]("CommonModules/ОбщийМодуль/Ext/Module.bsl")["owner"] == "main"
        assert not calls, "production передал роль — detector повторяться не имеет права"

    def test_owner_is_always_present(self, cf):
        bsl = _make_bsl(cf)
        rows = bsl["find_module"]("Общий")
        assert rows and all("owner" in r for r in rows), "условный ключ дал бы KeyError"

    def test_outside_path_is_access_error_even_without_extensions(self, cf, tmp_path):
        """Объявленное boundary-исключение. Параметризуется по ОБЕИМ сессиям —
        второй случай и есть тот, где гейт на `_ext_paths_raw` открыл бы границу молча."""
        outside = tmp_path / "outside" / "X.bsl"
        _write(outside, "Процедура Ч() Экспорт\nКонецПроцедуры\n")
        for ext_paths in (None, []):
            bsl = _make_bsl(cf, extension_paths=ext_paths)
            with pytest.raises(PermissionError):
                bsl["get_module_outline"]("../outside/X.bsl", no_live=True)
            with pytest.raises(PermissionError):
                bsl["get_module_outline"](str(outside.resolve()))

    def test_extension_named_main_is_still_distinguishable(self, tmp_path):
        """Ради этого случая owner и получил префикс: расширение, названное `main`,
        иначе дало бы метку, неотличимую от базы."""
        base = tmp_path / "src" / "cf"
        ext = tmp_path / "src" / "cfe" / "Папка"
        _write(base / "Configuration.xml", _CF_DESCRIPTOR)
        _write(base / "CommonModules" / "Мод" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
        _write(ext / "CommonModules" / "ЕхтМод" / "Ext" / "Module.bsl", "Процедура Е() Экспорт\nКонецПроцедуры\n")
        bsl = _make_bsl(
            base,
            extension_paths=[str(ext)],
            current_config_role="main",
            current_config_root=str(base),
            extension_name_by_root={str(ext): "main"},
        )
        owners = {r["object_name"]: r["owner"] for r in bsl["find_module"]("Мод")}
        assert owners.get("Мод") == "main"
        assert owners.get("ЕхтМод") == "extension:main"
        assert owners["ЕхтМод"].startswith("extension:")

    def test_extension_name_comes_from_metadata_not_basename(self, tmp_path):
        base = tmp_path / "src" / "cf"
        ext = tmp_path / "src" / "cfe" / "ПапкаНаДиске"
        _write(base / "Configuration.xml", _CF_DESCRIPTOR)
        _write(base / "CommonModules" / "Мод" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
        _write(ext / "CommonModules" / "ЕхтМод" / "Ext" / "Module.bsl", "Процедура Е() Экспорт\nКонецПроцедуры\n")
        bsl = _make_bsl(
            base,
            extension_paths=[str(ext)],
            current_config_role="main",
            current_config_root=str(base),
            extension_name_by_root={str(ext): "РеальноеИмя"},
        )
        ext_rows = [r for r in bsl["find_module"]("ЕхтМод")]
        assert ext_rows and ext_rows[0]["owner"] == "extension:РеальноеИмя"

    def test_standalone_extension_marks_its_own_paths(self, tmp_path):
        """Сессия ПРЯМО на расширении с `extension_paths=[]`: обычный относительный
        путь текущего root — extension, и `_extension_paths_set` тут пуст by design."""
        ext = tmp_path / "cfe" / "МоёРасширение"
        _write(ext / "Configuration.xml", _CF_DESCRIPTOR)
        _write(ext / "CommonModules" / "Мод" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
        bsl = _make_bsl(
            ext,
            extension_paths=[],
            current_config_role="extension",
            current_config_name="МоёРасширение",
            current_config_root=str(ext),
        )
        rows = bsl["find_module"]("Мод")
        assert rows and rows[0]["owner"] == "extension:МоёРасширение"


# ---------------------------------------------------------------------------
# Задача 0 — топология корней
# ---------------------------------------------------------------------------
class TestRootTopology:
    def test_overlapping_roots_are_rejected_early(self, cf, tmp_path):
        inside = cf / "Внутри"
        _write(inside / "Configuration.xml", _CF_DESCRIPTOR)
        with pytest.raises(ValueError):
            _make_bsl(cf, extension_paths=[str(inside)])
        with pytest.raises(ValueError):
            _make_bsl(cf, extension_paths=[str(cf)])
        with pytest.raises(ValueError):
            _make_bsl(inside, extension_paths=[str(cf)])

    def test_two_overlapping_extension_roots_are_rejected(self, tmp_path):
        base = tmp_path / "cf"
        ext = tmp_path / "cfe" / "A"
        nested = ext / "B"
        _write(base / "Configuration.xml", _CF_DESCRIPTOR)
        _write(ext / "Configuration.xml", _CF_DESCRIPTOR)
        _write(nested / "Configuration.xml", _CF_DESCRIPTOR)
        with pytest.raises(ValueError):
            _make_bsl(base, extension_paths=[str(ext), str(nested)])

    def test_sibling_roots_stay_valid(self, tmp_path):
        base = tmp_path / "src" / "cf"
        ext = tmp_path / "src" / "cfe" / "A"
        _write(base / "Configuration.xml", _CF_DESCRIPTOR)
        _write(ext / "Configuration.xml", _CF_DESCRIPTOR)
        assert _make_bsl(base, extension_paths=[str(ext)])  # не бросает

    def test_generic_mode_skips_topology_validation(self, cf):
        """Документированный generic-режим provenance/totals не использует и
        сохраняет прежнее право принимать эти поля без BSL-валидации."""
        from rlm_tools_bsl.sandbox import Sandbox

        inside = cf / "Внутри"
        _write(inside / "Configuration.xml", _CF_DESCRIPTOR)
        # BSL-путь активен → отказ.
        with pytest.raises(ValueError):
            Sandbox(base_path=str(cf), format_info=detect_format(str(cf)), extension_paths=[str(inside)])
        # generic → создаётся как раньше.
        Sandbox(
            base_path=str(cf),
            format_info=detect_format(str(cf)),
            extension_paths=[str(inside)],
            enable_bsl_helpers=False,
        )


# ---------------------------------------------------------------------------
# Задача 9 — мост предупреждений воркера (валидация на РОДИТЕЛЕ)
# ---------------------------------------------------------------------------
class TestLogRecordProtocol:
    @staticmethod
    def _validate(payload):
        from rlm_tools_bsl.sandbox_process import ProcessSandboxBackend

        return ProcessSandboxBackend._validate_log_records(payload, "execute_result")

    def test_absent_key_is_empty_not_error(self):
        assert self._validate({}) == []

    def test_explicit_null_is_a_violation(self):
        from rlm_tools_bsl._sandbox_protocol import SandboxProtocolError

        with pytest.raises(SandboxProtocolError):
            self._validate({"log_records": None})

    @pytest.mark.parametrize(
        "bad",
        [
            {"log_records": "not a list"},
            {"log_records": [1]},
            {"log_records": ["x"] * 21},
            {"log_records": ["x" * 301]},
        ],
    )
    def test_shape_violations(self, bad):
        from rlm_tools_bsl._sandbox_protocol import SandboxProtocolError

        with pytest.raises(SandboxProtocolError):
            self._validate(bad)

    @pytest.mark.parametrize("ch", ["\n", "\r", "\t", "\x00", "\x1b", "\x85", " ", " ", "‮"])
    def test_any_raw_non_printable_is_rejected(self, ch):
        """Проверка по `str.isprintable()`, а не по одному CR/LF-check: иначе
        скомпрометированный worker подделал бы строку лога через ESC/bidi."""
        from rlm_tools_bsl._sandbox_protocol import SandboxProtocolError

        with pytest.raises(SandboxProtocolError):
            self._validate({"log_records": [f"a{ch}b"]})

    @pytest.mark.parametrize("ch", ["\n", "\r", "\t", "\x00", "\x1b", "\x85", " ", " ", "‮"])
    def test_worker_sanitizer_makes_them_pass(self, ch):
        from rlm_tools_bsl._sandbox_protocol import LOG_RECORD_MAX_CHARS, sanitize_log_record

        cleaned = sanitize_log_record(f"a{ch}b")
        assert cleaned.isprintable()
        assert len(cleaned) <= LOG_RECORD_MAX_CHARS
        assert self._validate({"log_records": [cleaned]}) == [cleaned]

    def test_escape_happens_before_truncation(self):
        from rlm_tools_bsl._sandbox_protocol import LOG_RECORD_MAX_CHARS, sanitize_log_record

        cleaned = sanitize_log_record("\x1b" * 200)
        assert len(cleaned) <= LOG_RECORD_MAX_CHARS
        assert cleaned.isprintable()

    def test_printable_unicode_survives_verbatim(self):
        from rlm_tools_bsl._sandbox_protocol import sanitize_log_record

        assert sanitize_log_record("arg-guard: лимит 5 — ок") == "arg-guard: лимит 5 — ок"

    def test_inline_backend_has_no_startup_records(self, cf):
        from rlm_tools_bsl.sandbox import Sandbox
        from rlm_tools_bsl.sandbox_backend import InlineSandboxBackend

        backend = InlineSandboxBackend(Sandbox(base_path=str(cf), format_info=detect_format(str(cf))), None)
        assert backend.startup_log_records == []


def test_backend_result_log_records_default_is_empty():
    """Поле обязано иметь default_factory: результат конструируют и inline-backend,
    и аварийные ветки, и они об этом поле знать не должны."""
    from rlm_tools_bsl.sandbox_backend import BackendExecutionResult

    assert BackendExecutionResult(stdout="", error=None, variables=[]).log_records == []


# ---------------------------------------------------------------------------
# Задача 1 — git-специфика (ignore/binary/per-file cap)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not shutil.which("git"), reason="git недоступен")
class TestGitBranch:
    @staticmethod
    def _repo(root):
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)

    def test_gitignored_module_is_still_found(self, tmp_path):
        """Trusted точный список файлов не должен зависеть от `.gitignore`:
        раньше совпадение в игнорируемом `.bsl` терялось БЕЗ СЛЕДА — git молчал
        тем же rc=0, а Python эти пути после успешного вызова уже не читал."""
        root = tmp_path / "repo"
        self._repo(root)
        _write(root / ".gitignore", "Ignored/\n")
        _write(
            root / "Ignored" / "CommonModules" / "Скрытый" / "Ext" / "Module.bsl",
            "Процедура П() Экспорт\n    ИскомыйЛитерал = 1;\nКонецПроцедуры\n",
        )
        bsl = _make_bsl(root)
        res = bsl["safe_grep"]("ИскомыйЛитерал", max_files=50)
        assert res["results"], "совпадение в .gitignore'd .bsl терялось до v1.34.0"

    def test_binary_marked_text_module_is_still_found(self, tmp_path):
        """Независимый от .gitignore контроль: `-I` молча пропускал валидный
        текстовый `.bsl`, помеченный `binary` через .gitattributes."""
        root = tmp_path / "repo"
        self._repo(root)
        _write(root / ".gitattributes", "*.bsl binary\n")
        _write(
            root / "CommonModules" / "Помеченный" / "Ext" / "Module.bsl",
            "Процедура П() Экспорт\n    ДругойЛитерал = 1;\nКонецПроцедуры\n",
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        bsl = _make_bsl(root)
        res = bsl["safe_grep"]("ДругойЛитерал", max_files=50)
        assert res["results"], "binary-classified текстовый .bsl терялся до v1.34.0"
        # Общий git_search семантику НЕ меняет.
        general = bsl["git_search"]("ДругойЛитерал")
        assert general["error"] is None
        assert general["results"] == [], "общая ветка сохраняет прежний -I"

    def test_per_file_cap_is_now_visible(self, tmp_path):
        """51 совпадение в ОДНОМ файле при общем max_results=200 возвращалось как
        50 строк БЕЗ признака усечения."""
        root = tmp_path / "repo"
        self._repo(root)
        body = "\n".join(f"    ЧастыйТокен{i} = {i};" for i in range(60))
        _write(
            root / "CommonModules" / "Много" / "Ext" / "Module.bsl", f"Процедура П() Экспорт\n{body}\nКонецПроцедуры\n"
        )
        bsl = _make_bsl(root)
        res = bsl["git_search"]("ЧастыйТокен", max_results=200)
        assert res["error"] is None
        assert res["returned"] == 50, res["returned"]
        assert res["truncated"] is True
        assert all("_truncated" not in r for r in res["results"])

    def test_git_failure_does_not_double_count_attempts(self, tmp_path, monkeypatch):
        """На отказе git те же пути реально проходит Python-ветка — засчитав их
        «по факту попытки» дважды, мы получили бы завышенный охват."""
        import rlm_tools_bsl.bsl_index as idx

        root = tmp_path / "repo"
        self._repo(root)
        _write(
            root / "CommonModules" / "Мод" / "Ext" / "Module.bsl",
            "Процедура П() Экспорт\n    Литерал = 1;\nКонецПроцедуры\n",
        )
        bsl = _make_bsl(root)
        monkeypatch.setattr(idx, "_git_grep", lambda *a, **k: None)
        res = bsl["safe_grep"]("Литерал", max_files=50)
        assert res["results"], "Python-ветка обязана отработать те же файлы"
        assert res["scanned_files"] == res["candidates_total"] == 1, res


# ---------------------------------------------------------------------------
# Задача 0/6 — провенанс UNTRUSTED пути: все формы одного файла дают один owner
# ---------------------------------------------------------------------------
@pytest.fixture
def cf_ext(tmp_path):
    """MAIN-база и соседнее CFE рядом — топология sibling-корней."""
    base = tmp_path / "src" / "cf"
    ext = tmp_path / "src" / "cfe" / "ПапкаНаДиске"
    _write(base / "Configuration.xml", _CF_DESCRIPTOR)
    _write(base / "CommonModules" / "Мод" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
    _write(
        ext / "CommonModules" / "ЕхтМод" / "Ext" / "Module.bsl",
        "Процедура ЕхтПроцедура() Экспорт\nКонецПроцедуры\n",
    )
    return base, ext


def _bsl_with_ext(base, ext):
    return _make_bsl(
        base,
        extension_paths=[str(ext)],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(base),
        extension_name_by_root={str(ext): "РеальноеИмя"},
    )


class TestUntrustedPathOwner:
    """Owner untrusted-аргумента считается по CONTAINMENT, а не по тексту.

    Текстовый классификатор работает по компонентным префиксам ДОВЕРЕННОЙ строки
    каталога; на другой форме ТОГО ЖЕ файла он вернул бы `main` — уверенно неверная
    метка ровно там, где провенанс и важен.
    """

    @staticmethod
    def _forms(base, ext):
        rel = "../cfe/ПапкаНаДиске/CommonModules/ЕхтМод/Ext/Module.bsl"
        return [
            rel,
            str((ext / "CommonModules" / "ЕхтМод" / "Ext" / "Module.bsl").resolve()),
            "CommonModules/../" + rel,
        ]

    def test_outline_owner_is_identical_for_every_allowed_form(self, cf_ext):
        base, ext = cf_ext
        bsl = _bsl_with_ext(base, ext)
        owners = {bsl["get_module_outline"](form, no_live=True)["owner"] for form in self._forms(base, ext)}
        assert owners == {"extension:РеальноеИмя"}, owners

    def test_definition_hint_owner_and_coverage_follow_containment(self, cf_ext):
        base, ext = cf_ext
        bsl = _bsl_with_ext(base, ext)
        for form in self._forms(base, ext):
            res = bsl["find_definition"]("ЕхтПроцедура", form)
            assert res["definitions"], form
            assert res["definitions"][0]["owner"] == "extension:РеальноеИмя", form
            # Охват обязан двигаться вместе с меткой: иначе ext-счётчики молча
            # сообщали бы «расширения не смотрели» на CFE-модуле.
            assert res["_meta"]["extensions_included"] is True, form
            assert res["_meta"]["scanned_extension_modules"] == 1, form

    def test_main_module_keeps_main_owner_in_every_form(self, cf_ext):
        base, ext = cf_ext
        rel = "CommonModules/Мод/Ext/Module.bsl"
        bsl = _bsl_with_ext(base, ext)
        for form in (rel, str((base / rel).resolve()), "CommonModules/../" + rel):
            assert bsl["get_module_outline"](form, no_live=True)["owner"] == "main", form
            res = bsl["find_definition"]("П", form)
            assert res["definitions"][0]["owner"] == "main", form
            assert res["_meta"]["extensions_included"] is False, form


def test_find_definition_rows_always_carry_owner(cf_ext):
    """Ключ обещан планом, CHANGELOG и `full_analysis_prompt`; условный ключ дал бы
    `KeyError` ровно у агента, который следовал документации."""
    base, ext = cf_ext
    bsl = _bsl_with_ext(base, ext)
    for name, expected in (("П", "main"), ("ЕхтПроцедура", "extension:РеальноеИмя")):
        rows = bsl["find_definition"](name)["definitions"]
        assert rows, name
        assert all(r.get("owner") == expected for r in rows), (name, rows)


# ---------------------------------------------------------------------------
# Задача 0 — wrapper-вход: роль EXTENSION относится к current_config_root,
# а НЕ ко всему base_path
# ---------------------------------------------------------------------------
def test_wrapper_extension_root_does_not_paint_the_whole_base(tmp_path):
    """`resolve_config_root` на wrapper-входе оставляет container, а CFE лежит в его
    дочернем каталоге. Строки container-а обязаны остаться `main`."""
    container = tmp_path / "wrap"
    inner = container / "МоёРасширение"
    _write(container / "CommonModules" / "ВнеCFE" / "Ext" / "Module.bsl", "Процедура В() Экспорт\nКонецПроцедуры\n")
    _write(inner / "Configuration.xml", _CF_DESCRIPTOR)
    _write(inner / "CommonModules" / "ВнутриCFE" / "Ext" / "Module.bsl", "Процедура И() Экспорт\nКонецПроцедуры\n")
    bsl = _make_bsl(
        container,
        extension_paths=[],
        current_config_role="extension",
        current_config_name="МоёРасширение",
        current_config_root=str(inner),
    )
    owners = {r["object_name"]: r["owner"] for r in bsl["find_by_type"]("CommonModules")}
    assert owners.get("ВнутриCFE") == "extension:МоёРасширение", owners
    assert owners.get("ВнеCFE") == "main", owners


# ---------------------------------------------------------------------------
# Задача 0 — detector-alias: второй путь к УЖЕ учтённому дереву не считается дважды
# ---------------------------------------------------------------------------
def test_alias_pointing_inside_current_root_is_filtered_out(tmp_path):
    from rlm_tools_bsl.extension_detector import ExtensionInfo, filter_alias_extension_infos

    current = tmp_path / "cf"
    _write(current / "Configuration.xml", _CF_DESCRIPTOR)
    sibling = tmp_path / "cfe" / "Сосед"
    _write(sibling / "Configuration.xml", _CF_DESCRIPTOR)

    from rlm_tools_bsl.extension_detector import ConfigRole

    def _info(path, name):
        return ExtensionInfo(path=str(path), role=ConfigRole.EXTENSION, name=name, source_format="edt")

    alias_inside = _info(current / "Подкаталог", "Алиас")
    kept = filter_alias_extension_infos(str(current), [alias_inside, _info(sibling, "Сосед")])
    assert [i.name for i in kept] == ["Сосед"], [i.name for i in kept]
    # Сам current root тоже не является своим соседом.
    assert filter_alias_extension_infos(str(current), [_info(current, "Я")]) == []


# ---------------------------------------------------------------------------
# Задача 7 — тест-карта: объявленные в docs/HELPERS.md источники не протухли
# ---------------------------------------------------------------------------
def _coverage_map_from_docs():
    """Разбор таблицы «Карта охвата» — колонка `source`, значения в бэктиках."""
    import pathlib
    import re as _re

    text = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    block = text.split("## Карта охвата", 1)
    assert len(block) == 2, "раздел «Карта охвата» исчез из docs/HELPERS.md"
    table = {}
    for line in block[1].splitlines():
        if not line.startswith("| `"):
            continue
        # Внутри ячеек альтернативы разделены ЭКРАНИРОВАННЫМ `\|` — наивный split
        # разрезал бы ячейку и оставил от неё первое значение (тест стал бы вакуумным).
        cells = [c.strip().replace("\x00", "|") for c in line.replace("\\|", "\x00").strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        helper = cells[0].strip("`").split("(")[0]
        values = set(_re.findall(r"`([a-z+_]+)`", cells[1]))
        if values:
            table.setdefault(helper, set()).update(values)
    return table


def test_coverage_map_matches_reality(cf_ext):
    """Карта нужна, чтобы следующий релиз работал ПО КАРТЕ. Проверяется направление,
    которое реально протухает: фактически отданный `source` обязан быть объявлен."""
    base, ext = cf_ext
    _write(base / "Documents" / "Док" / "Ext" / "ObjectModule.bsl", "Процедура О()\nКонецПроцедуры\n")
    table = _coverage_map_from_docs()
    assert table, "таблица не разобралась — тест стал вакуумным"
    bsl = _bsl_with_ext(base, ext)

    def _source(res):
        return res.get("source") or res["_meta"]["source"]

    actual = {
        "find_definition": _source(bsl["find_definition"]("П")),
        "find_code_usages": _source(bsl["find_code_usages"]("Документ.Док")),
        "find_references_to_object": _source(bsl["find_references_to_object"]("Документ.Док")),
        "search_objects": _source(bsl["search_objects"]("Док", count_only=True)),
        "find_by_type": _source(bsl["find_by_type"]("Documents", count_only=True)),
        "get_overrides": _source(bsl["get_overrides"]()),
    }
    for helper, value in actual.items():
        assert helper in table, f"{helper} исчез из карты охвата"
        assert value in table[helper], f"{helper}: source={value!r} не объявлен в карте {sorted(table[helper])}"
    # `safe_grep` в карте помечен как чисто живой и собственного `source` не несёт —
    # ассертим именно это, иначе строка таблицы ничем не подтверждена.
    assert "source" not in bsl["safe_grep"]("Процедура")


# ---------------------------------------------------------------------------
# Задача 7 — доказуемость поколения индекса у find_code_usages
# ---------------------------------------------------------------------------
class _UsagesReader(_DummyReader):
    """Reader с ЖИВОЙ таблицей `metadata_code_usages` и управляемым bracket."""

    def __init__(self, caps):
        self._caps = caps

    def get_build_capabilities(self):
        return dict(self._caps) if self._caps is not None else None

    def find_code_usages(self, *a, **kw):
        return [{"path": "CommonModules/ОбщийМодуль/Ext/Module.bsl", "line": 1, "kind": "call", "snippet": "x"}]

    def count_code_usages(self, *a, **kw):
        return {"total": 1, "by_kind": {"call": 1}}


def _caps(**kw):
    base = {
        "bsl_count": 1,
        "modules_count": 1,
        "build_in_progress": None,
        "data_version": 7,
        "base_path": "",
        "built_at": "",
    }
    base.update(kw)
    return base


class TestCodeUsagesProof:
    def test_stable_domain_incompleteness_keeps_index_rows(self, cf):
        """Один нечитаемый модуль на сборке не имеет права обменять доказанные
        строки индекса на grep по 40 файлам — публикуется НИЖНЯЯ оценка."""
        bsl = _make_bsl(cf, idx_reader=_UsagesReader(_caps(bsl_count=2, modules_count=1)))
        res = bsl["find_code_usages"]("Документ.ТестДок")
        assert res["_meta"]["source"] == "index"
        assert res["usages"] and res["usages"][0]["owner"] == "main"
        assert res["partial"] is True
        assert res["_meta"]["index_proof"] == "incomplete"

    def test_transient_generation_falls_back_and_says_why(self, cf):
        """Недоказуемо само ПОКОЛЕНИЕ → живой путь. Причина в hint обязана совпадать
        с фактической: «таблицы нет» на существующей таблице — тупиковый совет."""
        bsl = _make_bsl(cf, idx_reader=_UsagesReader(_caps(build_in_progress="1")))
        res = bsl["find_code_usages"]("Документ.ТестДок")
        assert res["_meta"]["source"] == "live"
        assert res["partial"] is True
        assert res["_meta"]["index_proof"] == "transient"
        assert "table missing" not in res["_meta"]["hint"]

    def test_missing_table_keeps_the_rebuild_advice(self, cf):
        bsl = _make_bsl(cf, idx_reader=_DummyReader())
        res = bsl["find_code_usages"]("Документ.ТестДок")
        assert res["_meta"]["source"] == "live"
        assert "table missing" in res["_meta"]["hint"]


# ---------------------------------------------------------------------------
# Ревью № 2 — находки, подтверждённые на исполнении
# ---------------------------------------------------------------------------
class TestSpecialLiveRouteCoverage:
    """Special-маршруты live-walker'а (ExchangePlans / DefinedTypes) обязаны
    считать кандидатов так же, как общий `metadata_xml`-маршрут.

    Без этого битый `Content.xml` уходил в domain-parser пустым результатом, и
    ответ публиковал `candidates_total=0` / `failed_files=0` — то есть ДОКАЗЫВАЛ
    полноту домена, который не просматривал. На standalone-CFE это ещё и
    поднимало `extensions_included=True` по правилу «пустой полный обход».
    """

    @staticmethod
    def _standalone_ext(tmp_path):
        root = tmp_path / "src" / "cfe" / "Расш"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        _write(root / "CommonModules" / "М" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
        return root

    @staticmethod
    def _bsl(root):
        return _make_bsl(
            root,
            current_config_role="extension",
            current_config_name="Расш",
            current_config_root=str(root),
        )

    def test_malformed_exchange_plan_is_a_proven_failure_not_silence(self, tmp_path):
        root = self._standalone_ext(tmp_path)
        _write(root / "ExchangePlans" / "П" / "Ext" / "Content.xml", "<ExchangePlanContent><item")
        res = self._bsl(root)["find_references_to_object"]("Document.Цель", kinds=["exchange_plan_content"])
        meta = res["_meta"]
        assert meta["candidates_total"] == 1, meta
        assert meta["failed_files"] == 1, meta
        assert meta["scanned_files"] == 1, meta
        # Кандидат есть и он не разобран — «полный пустой обход» больше не
        # доказывается, значит и охват расширения не заявляется.
        assert meta["extensions_included"] is False, meta

    def test_wellformed_exchange_plan_without_the_ref_is_a_success(self, tmp_path):
        """Пустой состав на доказанно well-formed XML — валидное «ссылок нет»."""
        root = self._standalone_ext(tmp_path)
        _write(
            root / "ExchangePlans" / "П" / "Ext" / "Content.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n<ExchangePlanContent/>\n',
        )
        meta = self._bsl(root)["find_references_to_object"]("Document.Цель", kinds=["exchange_plan_content"])["_meta"]
        assert meta["candidates_total"] == 1 and meta["failed_files"] == 0, meta
        assert meta["extensions_included"] is True, meta

    def test_malformed_defined_type_is_a_proven_failure(self, tmp_path):
        root = self._standalone_ext(tmp_path)
        _write(root / "DefinedTypes" / "Т.xml", "<MetaDataObject><DefinedType")
        meta = self._bsl(root)["find_references_to_object"]("Document.Цель", kinds=["defined_type_content"])["_meta"]
        assert meta["candidates_total"] == 1, meta
        assert meta["failed_files"] == 1, meta
        assert meta["extensions_included"] is False, meta

    def test_irrelevant_kind_does_not_account_the_special_route(self, tmp_path):
        """Файл, из которого запрошенный вид испустить невозможно, кандидатом НЕ
        является — иначе счётчики росли бы от чужого домена.

        `DefinedTypes` общий `metadata_xml`-обход не посещает вовсе, поэтому здесь
        виден именно вклад special-ветки."""
        root = self._standalone_ext(tmp_path)
        _write(root / "DefinedTypes" / "Т.xml", "<MetaDataObject><DefinedType")
        meta = self._bsl(root)["find_references_to_object"]("Document.Цель", kinds=["exchange_plan_content"])["_meta"]
        assert meta["candidates_total"] == 0, meta
        assert meta["failed_files"] == 0, meta

    def test_one_file_is_one_candidate_across_routes(self, tmp_path):
        """`ExchangePlans/<П>/Ext/Content.xml` читают ДВА маршрута — общий
        `metadata_xml` (категория есть в `scan_categories`) и special-ветка состава.
        Счётчики — перепись ФАЙЛОВ, поэтому файл обязан остаться ОДНИМ кандидатом,
        иначе `scanned_files <= candidates_total` перестал бы что-либо значить."""
        root = self._standalone_ext(tmp_path)
        _write(root / "ExchangePlans" / "П" / "Ext" / "Content.xml", "<ExchangePlanContent><item")
        meta = self._bsl(root)["find_references_to_object"]("Document.Цель")["_meta"]
        assert meta["candidates_total"] == 1, meta
        assert meta["scanned_files"] == 1, meta
        assert meta["failed_files"] == 1, meta


class TestCountExactnessIsRouteProven:
    """`total_exact` не имеет права зависеть от ИСТОРИИ сессии.

    Без ридера live-каталог перечисляется тем же кешем `glob`, что и main-loader,
    а `pathlib.glob` молча глотает `PermissionError` на подкаталоге и структурно
    отдаёт `errors=0`. Прогрев такого каталога любым живым хелпером превращал
    честный `partial=True` в «точный» ответ на НЕИЗМЕННОЙ ФС.
    """

    def test_glob_warmup_does_not_turn_partial_into_exact(self, cf):
        bsl = _make_bsl(cf)  # без idx_reader → канон `glob`
        before = bsl["find_by_type"]("CommonModules", count_only=True)
        bsl["find_definition"]("ЦелеваяПроцедура")  # греет live-каталог
        after = bsl["find_by_type"]("CommonModules", count_only=True)
        assert before["total"] == after["total"]
        assert before["total_exact"] is False and after["total_exact"] is False
        assert before["partial"] is True and after["partial"] is True
        assert after["_meta"]["current_root_complete"] is False

    def test_find_definition_does_not_prove_exactness_on_glob_either(self, cf):
        """Тот же дефект в СОСЕДНЕМ хелпере: production-обвязка (`private_io`) даёт
        `read_status_complete=True`, и на glob-каноне живой `find_definition`
        объявлял итог доказанным. Причина публикуется отдельным токеном —
        `catalog_complete` остаётся про УВИДЕННЫЕ отказы и значения не меняет."""
        bsl = _make_bsl(cf, private_io=True)  # без idx_reader → канон `glob`
        for res in (
            bsl["find_definition"]("ЦелеваяПроцедура"),
            bsl["find_definition"]("ЦелеваяПроцедура", "ОбщийМодуль"),
        ):
            meta = res["_meta"]
            assert res["definitions"], meta
            assert meta["total_exact"] is False, meta
            assert "catalog_unproven" in meta["reasons"], meta
            assert meta["catalog_complete"] is True, meta
            assert meta["unique"] is False, meta

    def test_explicit_path_hint_stays_exact(self, cf):
        """Явный `rel_path` каталога не перечисляет вовсе — доказуемость обхода к
        нему не относится, иначе честность выродилась бы в «всегда partial»."""
        bsl = _make_bsl(cf, private_io=True)
        res = bsl["find_definition"]("ЦелеваяПроцедура", "CommonModules/ОбщийМодуль/Ext/Module.bsl")
        assert res["definitions"]
        assert res["_meta"]["total_exact"] is True, res["_meta"]

    def test_walk_canon_still_proves_exactness(self, cf):
        """Регресс-гард на обратную сторону: канон `walk` доказательством ОСТАЁТСЯ,
        иначе честность выродилась бы в «всегда partial»."""
        bsl = _make_bsl(cf, private_io=True, idx_reader=_DummyReader())
        bsl["find_definition"]("ЦелеваяПроцедура")
        res = bsl["find_by_type"]("CommonModules", count_only=True)
        assert res["total_exact"] is True and res["partial"] is False
        assert res["_meta"]["current_root_complete"] is True
        assert bsl["find_definition"]("ЦелеваяПроцедура")["_meta"]["total_exact"] is True


def test_normalized_path_between_sibling_roots_gets_the_real_owner(tmp_path):
    """`../cfe/ExtA/../ExtB/…` совпадает с компонентным префиксом ExtA, а читается
    из ExtB. Текстовый маршрут отдавал owner СОСЕДНЕГО расширения.

    `validate_root_topology` запрещает ВЛОЖЕННЫЕ корни, но такую нормализацию
    между соседними корнями не ловит, а `_ext_resolve_safe` путь не отвергает:
    его итог лежит внутри разрешённого ExtB.
    """
    base = tmp_path / "src" / "cf"
    ext_a = tmp_path / "src" / "cfe" / "ExtA"
    ext_b = tmp_path / "src" / "cfe" / "ExtB"
    _write(base / "Configuration.xml", _CF_DESCRIPTOR)
    _write(ext_a / "CommonModules" / "МодA" / "Ext" / "Module.bsl", "Процедура ПA() Экспорт\nКонецПроцедуры\n")
    _write(ext_b / "CommonModules" / "МодB" / "Ext" / "Module.bsl", "Процедура ПB() Экспорт\nКонецПроцедуры\n")
    bsl = _make_bsl(
        base,
        extension_paths=[str(ext_a), str(ext_b)],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(base),
        extension_name_by_root={str(ext_a): "ИмяA", str(ext_b): "ИмяB"},
    )
    crossed = "../cfe/ExtA/../ExtB/CommonModules/МодB/Ext/Module.bsl"
    direct = "../cfe/ExtB/CommonModules/МодB/Ext/Module.bsl"
    assert bsl["get_module_outline"](crossed, no_live=True)["owner"] == "extension:ИмяB"
    assert (
        bsl["get_module_outline"](crossed, no_live=True)["owner"]
        == (bsl["get_module_outline"](direct, no_live=True)["owner"])
    )
    res = bsl["find_definition"]("ПB", crossed)
    assert res["definitions"] and res["definitions"][0]["owner"] == "extension:ИмяB", res["definitions"]
    assert res["_meta"]["extensions_included"] is True


class TestRoleSampleIsOrderIndependent:
    """Sample `matched_objects` / `rights_by_object` — top-k, а не «первые k».

    У обоих ролевых SQL нет `ORDER BY`, поэтому удержание первых k пар делало бы
    выдачу зависимой от порядка вставки: две логически ОДИНАКОВЫЕ базы отдавали бы
    разные объекты. Сортировка в `result()` упорядочивает лишь удержанный префикс.
    """

    @staticmethod
    def _build(pairs):
        from rlm_tools_bsl.bsl_index import _RoleGroupBuilder

        b = _RoleGroupBuilder(1)
        for obj, right in pairs:
            b.add("Роль", obj, right, "Roles/Роль/Ext/Rights.xml")
        return b.result()[0]

    def test_insertion_order_does_not_change_the_sample(self):
        forward = self._build([("Document.A", "Read"), ("Document.B", "Read")])
        backward = self._build([("Document.B", "Read"), ("Document.A", "Read")])
        assert forward["matched_objects"] == ["Document.A"], forward
        assert forward["matched_objects"] == backward["matched_objects"]
        assert forward["rights_by_object"] == backward["rights_by_object"]
        assert forward["details_truncated"] is backward["details_truncated"] is True

    def test_eviction_keeps_the_bound_and_full_rights_union(self):
        from rlm_tools_bsl.bsl_index import _RoleGroupBuilder

        b = _RoleGroupBuilder(2)
        for obj in ("Document.Z", "Document.Y", "Document.X", "Document.W"):
            b.add("Роль", obj, "Read", "f")
        row = b.result()[0]
        assert row["matched_objects"] == ["Document.W", "Document.X"], row
        assert len(row["matched_objects"]) == 2
        assert row["details_truncated"] is True
        # Legacy-поля не трогаются: `rights` остаётся ПОЛНЫМ union, `object` —
        # прежним query-or-first-match.
        assert row["rights"] == ["Read"]
        assert row["object"] == "Document.Z"

    def test_repeated_evicted_pair_does_not_corrupt_the_sample(self):
        from rlm_tools_bsl.bsl_index import _RoleGroupBuilder

        b = _RoleGroupBuilder(1)
        for obj in ("Document.B", "Document.A", "Document.B"):
            b.add("Роль", obj, "Read", "f")
        row = b.result()[0]
        assert row["matched_objects"] == ["Document.A"], row

    def test_zero_limit_keeps_the_sample_empty_and_flags_truncation(self):
        from rlm_tools_bsl.bsl_index import _RoleGroupBuilder

        b = _RoleGroupBuilder(0)
        b.add("Роль", "Document.A", "Read", "f")
        row = b.result()[0]
        assert row["matched_objects"] == [] and row["details_truncated"] is True


def test_get_roles_exact_streams_the_cursor():
    """`include_members=True` включает `get_object_profile(sections=["roles"])`, а
    `LIKE '<ref>.%'` тянет ВСЕ member-гранты объекта. `fetchall()` держал бы в
    памяти `O(role_rights)` ДО применения cap — заявленная граница
    `O(roles * details_limit)` относится только к сериализации."""
    import inspect

    from rlm_tools_bsl.bsl_index import IndexReader

    src = inspect.getsource(IndexReader.get_roles_exact)
    assert ".fetchall()" not in src, "get_roles_exact снова материализует весь результат"
    assert "for r in cursor:" in src


class TestInitOkLogFitting:
    """OPTIONAL-логи ужимаются под РЕАЛЬНЫЙ frame-budget у ОБОИХ frame-type.

    Минимально разрешённый IPC cap — 256 KiB, а `detected_prefixes` ничем не
    ограничены: `init_ok`, который сам бы поместился, падал `SandboxProtocolError`
    из-за ДИАГНОСТИКИ — старт сессии терял обязательную часть ради необязательной.
    """

    @staticmethod
    def _msg(payload):
        from rlm_tools_bsl._sandbox_protocol import make_message

        return make_message("init_ok", "r1", 1, payload)

    def test_oversized_logs_are_dropped_not_the_frame(self):
        from rlm_tools_bsl._sandbox_protocol import SandboxProtocolError, encode_frame
        from rlm_tools_bsl.sandbox_worker import _fit_optional_log_records_to_frame

        cap = 4096
        # Кириллица в UTF-8 — 2 байта: baseline держим ASCII, чтобы он сам
        # ГАРАНТИРОВАННО помещался, а логи выносили frame за cap.
        payload = {"detected_prefixes": ["p" * 100 for _ in range(20)], "log_records": ["ы" * 300] * 20}
        msg = self._msg(payload)
        with pytest.raises(SandboxProtocolError):
            encode_frame(msg, cap)
        _fit_optional_log_records_to_frame(msg, cap)
        encode_frame(msg, cap)  # baseline помещается — обязательная часть цела
        assert msg["payload"]["detected_prefixes"] == payload["detected_prefixes"]
        assert len(msg["payload"].get("log_records", [])) < 20

    def test_worker_sends_init_ok_through_the_fitter(self):
        import inspect

        from rlm_tools_bsl import sandbox_worker

        src = inspect.getsource(sandbox_worker)
        head = src.split('make_message(\n            "init_ok"', 1)
        assert len(head) == 2, "init_ok больше не собирается через make_message"
        assert "_fit_optional_log_records_to_frame(init_ok" in head[1], "init_ok уходит в _send мимо общего fitter'а"


def test_code_usages_doc_does_not_deny_extension_recall():
    """Живой фолбэк идёт по CFE-aware каталогу и принимает ext-строки, помечая их
    `owner`. Безоговорочное «не ловит код расширений» в публичной доке отправляло
    бы агента искать несуществующее ограничение."""
    import pathlib

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    line = next(ln for ln in doc.splitlines() if ln.startswith("- `find_code_usages("))
    assert "и код расширений" not in line, line
    assert "extensions_included" in line, line

    from rlm_tools_bsl.bsl_helpers import make_bsl_helpers as _mk

    assert "extensions are not in the index" not in (_mk.__doc__ or "")


# ---------------------------------------------------------------------------
# Ревью № 3 — находки, подтверждённые на исполнении
# ---------------------------------------------------------------------------
class _StaleGenerationReader(_DummyReader):
    """v15-ридер, у которого ИДЁТ пересборка, а в `methods` лежит УДАЛЁННЫЙ метод."""

    def get_build_capabilities(self):
        return _caps(build_in_progress="1")

    def get_definitions(self, *a, **kw):
        return {"rows": [], "total": 0, "truncated": False, "slow_fallback": False}

    def get_methods_by_path(self, path):
        return [
            {
                "name": "УдалённыйМетод",
                "type": "Процедура",
                "line": 1,
                "end_line": 2,
                "is_export": True,
                "params": "",
            }
        ]

    def get_overrides_for_path(self, path):
        return None


class TestLivePathDoesNotReuseRejectedIndex:
    """Живая ветка `find_definition` обязана ЧИТАТЬ ФАЙЛ.

    В неё попадают ровно тогда, когда поколение индекса уже ОТВЕРГНУТО
    (`proof == "transient"`) либо индекса нет вовсе. Публичный `extract_procedures`
    предпочитает `get_methods_by_path` и лишь ДОПОЛНЯЕТ его живыми методами,
    никогда не убирая устаревшие, — то есть возвращал метод, которого в файле УЖЕ
    НЕТ, под меткой `source="live"`, `total_exact=True`, `partial=False`.
    """

    def test_stale_index_row_is_not_published_as_exact_live(self, cf):
        bsl = _make_bsl(cf, private_io=True, idx_reader=_StaleGenerationReader())
        res = bsl["find_definition"]("УдалённыйМетод", "CommonModules/ОбщийМодуль/Ext/Module.bsl")
        assert res["_meta"]["source"] == "live", res["_meta"]
        assert res["definitions"] == [], res["definitions"]
        assert res["total"] == 0

    def test_real_method_from_the_file_is_still_found(self, cf):
        """Обратная сторона: живой парсер обязан находить то, что в файле ЕСТЬ."""
        bsl = _make_bsl(cf, private_io=True, idx_reader=_StaleGenerationReader())
        for hint in ("CommonModules/ОбщийМодуль/Ext/Module.bsl", "ОбщийМодуль", ""):
            res = bsl["find_definition"]("ЦелеваяПроцедура", hint)
            assert res["definitions"], (hint, res["_meta"])
            row = res["definitions"][0]
            assert row["is_export"] is True and isinstance(row["params"], list), row


class TestMetadataRouteCandidacy:
    """Кандидат общего `metadata_xml`-маршрута определяется КОРНЕМ XML.

    Layout 2 идёт `rglob`, поэтому в маршрут прилетает ВЕСЬ XML под деревом
    объекта: формы, макеты, `Ext/Predefined.xml`, состав планов обмена. Ни один из
    них метаданными объекта не является, `parse_metadata_xml` штатно отдаёт по ним
    error-mapping, и учёт их кандидатами превращал ЗДОРОВУЮ конфигурацию в «3
    отказа из 4» — перепись охвата, флагманская метрика честности релиза,
    показывала бы на боевой базе тысячи выдуманных отказов.
    """

    @staticmethod
    def _healthy_cf(tmp_path):
        root = tmp_path / "cf"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        _write(
            root / "Documents" / "Док" / "Ext" / "Document.xml",
            '<?xml version="1.0"?>\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
            "<Document><Properties><Name>Док</Name></Properties></Document></MetaDataObject>\n",
        )
        _write(
            root / "Documents" / "Док" / "Forms" / "ФормаСписка" / "Ext" / "Form.xml",
            '<?xml version="1.0"?>\n<Form xmlns="http://v8.1c.ru/8.3/xcf/logform"><Items/></Form>\n',
        )
        _write(
            root / "Documents" / "Док" / "Templates" / "Макет" / "Ext" / "Template.xml",
            '<?xml version="1.0"?>\n<Template xmlns="http://v8.1c.ru/8.3/xcf/logform"/>\n',
        )
        _write(root / "Documents" / "Док" / "Ext" / "Predefined.xml", '<?xml version="1.0"?>\n<PredefinedData/>\n')
        return root

    def test_healthy_config_reports_no_failures(self, tmp_path):
        root = self._healthy_cf(tmp_path)
        meta = _make_bsl(root)["find_references_to_object"]("Document.Цель")["_meta"]
        assert meta["failed_files"] == 0, meta
        assert meta["candidates_total"] == 1, meta
        assert meta["scanned_files"] == 1, meta

    def test_broken_metadata_object_is_still_a_proven_failure(self, tmp_path):
        """Сужение не имеет права проглотить НАСТОЯЩИЙ отказ: битый XML корнем не
        классифицируется, поэтому остаётся кандидатом и доказанным отказом."""
        root = self._healthy_cf(tmp_path)
        _write(root / "Documents" / "Битый" / "Ext" / "Document.xml", "<MetaDataObject><Document")
        meta = _make_bsl(root)["find_references_to_object"]("Document.Цель")["_meta"]
        assert meta["candidates_total"] == 2, meta
        assert meta["failed_files"] == 1, meta

    def test_valid_exchange_plan_content_is_success_under_default_kinds(self, tmp_path):
        """`ExchangePlans/<П>/Ext/Content.xml` читают ДВА маршрута. При `kinds=None`
        общий маршрут прежде успевал заклеймить корректный файл отказом."""
        root = self._healthy_cf(tmp_path)
        _write(
            root / "ExchangePlans" / "П" / "Ext" / "Content.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ExchangePlanContent xmlns="http://v8.1c.ru/8.3/xcf/extrnprops">\n'
            "  <Item><Metadata>Document.Цель</Metadata><AutoRecord>Allow</AutoRecord></Item>\n"
            "</ExchangePlanContent>\n",
        )
        res = _make_bsl(root)["find_references_to_object"]("Document.Цель")
        meta = res["_meta"]
        assert meta["failed_files"] == 0, meta
        # Ссылка ПРИ ЭТОМ найдена — сужение переписи не сузило recall.
        assert [r["kind"] for r in res["references"]] == ["exchange_plan_content"], res["references"]


def test_default_kinds_still_names_unsupported_kinds(cf):
    """`kinds=None` — значение ПО УМОЛЧАНИЮ и означает ВСЕ виды. Пустой список
    прятал ограничение именно в том вызове, которым хелпер и вызывают: пустой
    ответ по `role_rights` читался бы как доказанное «ссылок нет»."""
    bsl = _make_bsl(cf)
    default = bsl["find_references_to_object"]("Document.Цель")["_meta"]["unsupported_kinds"]
    assert set(default) == {
        "choice_parameter_link",
        "event_subscription_source",
        "functional_option_content",
        "link_by_type",
        "predefined_characteristic_type",
        "role_rights",
    }, default
    # Явный запрос по-прежнему сужается до запрошенного.
    narrow = bsl["find_references_to_object"]("Document.Цель", kinds=["role_rights", "owner"])
    assert narrow["_meta"]["unsupported_kinds"] == ["role_rights"]


class TestStandaloneCfeIndexCoverage:
    """`extensions_included` — «учтён ли ФАКТИЧЕСКИ хотя бы один extension-source»
    (docs/HELPERS.md), а НЕ «домен доказанно полон»: полнота живёт на ОРТОГОНАЛЬНОЙ
    оси `index_coverage`. Индекс относится к ТЕКУЩЕМУ root, поэтому в
    EXTENSION-сессии отданные строки пришли ИЗ расширения, и жёсткий `False` про
    них врал — причём три хелпера отвечали на один вопрос по-разному.
    """

    @staticmethod
    def _bsl(cf, reader):
        return _make_bsl(
            cf,
            idx_reader=reader,
            current_config_role="extension",
            current_config_name="МоёРасш",
            current_config_root=str(cf),
        )

    def test_count_route_does_not_deny_its_own_root(self, cf):
        class _CountReader(_DummyReader):
            def get_build_capabilities(self):
                return _caps(has_synonyms=True)

            def count_objects(self, query, current_prefix=None):
                return {"total": 3, "current_root": None}

        res = self._bsl(cf, _CountReader())["search_objects"]("Расх", count_only=True)
        assert res["total_extensions"] == 3, res
        assert res["extensions_included"] is True, res
        # Недоказанность полноты по-прежнему выражена ОТДЕЛЬНО и не размыта.
        assert res["total_exact"] is False, res

    def test_main_session_still_does_not_claim_extensions(self, cf):
        """Обратная сторона: в MAIN-сессии индекс соседние CFE не покрывает."""

        class _CountReader(_DummyReader):
            def get_build_capabilities(self):
                return _caps(has_synonyms=True)

            def count_objects(self, query, current_prefix=None):
                return {"total": 3, "current_root": None}

        res = _make_bsl(cf, idx_reader=_CountReader())["search_objects"]("Расх", count_only=True)
        assert res["extensions_included"] is False, res


def test_git_search_error_shape_is_documented_as_a_dict():
    """Доки показывали СТАРУЮ list-форму рядом с верным описанием словаря. Агент,
    следовавший примеру и читавший `res[0]["error"]`, получал `KeyError: 0` — ровно
    то падение, которое релиз и чинит."""
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("docs/HELPERS.md", "docs/full_analysis_prompt.md", "src/rlm_tools_bsl/bsl_helpers.py"):
        text = repo.joinpath(rel).read_text(encoding="utf-8")
        assert '[{"error"' not in text, rel
        assert "[{error}]" not in text, rel


def test_indexed_standalone_cfe_reference_route_owns_its_rows(tmp_path, monkeypatch):
    """Индексный маршрут `find_references_to_object` отдавал строки ТЕКУЩЕГО
    расширения и одновременно сообщал `extensions_included=False`.

    Проверяется на НАСТОЯЩЕМ индексе: стаб здесь доказывал бы только сам себя.
    """
    from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

    root = tmp_path / "src" / "cfe"
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
    (tmp_path / "idx").mkdir(parents=True, exist_ok=True)
    _write(
        root / "Configuration.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        '  <Configuration uuid="00000000-0000-0000-0000-000000000002">\n'
        "    <Properties><Name>МоёРасш</Name>"
        "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose></Properties>\n"
        "  </Configuration>\n</MetaDataObject>\n",
    )
    _write(
        root / "Documents" / "Расходная" / "Ext" / "Document.xml",
        '<?xml version="1.0"?>\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core">\n'
        "<Document><Properties><Name>Расходная</Name></Properties>\n"
        "<ChildObjects><Attribute><Properties><Name>Контрагент</Name>"
        "<Type><v8:Type>CatalogRef.Цель</v8:Type></Type></Properties></Attribute></ChildObjects>\n"
        "</Document></MetaDataObject>\n",
    )
    _write(root / "Documents" / "Расходная" / "Ext" / "ObjectModule.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
    IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
    reader = IndexReader(get_index_db_path(str(root)))
    try:
        bsl = _make_bsl(
            root,
            idx_reader=reader,
            current_config_role="extension",
            current_config_name="МоёРасш",
            current_config_root=str(root),
        )
        res = bsl["find_references_to_object"]("Catalog.Цель")
        assert res["_meta"]["source"] == "index", res["_meta"]
        assert res["references"], res
        assert res["_meta"]["extensions_included"] is True, res["_meta"]
        # Ось ПОЛНОТЫ — ортогональная и остаётся консервативной.
        assert res["_meta"]["index_coverage"] == "build_unproven", res["_meta"]
        # `partial` на здоровом индексном маршруте НЕ трогается (отклонение A).
        assert res["partial"] is False, res
    finally:
        reader.close() if hasattr(reader, "close") else None


# ---------------------------------------------------------------------------
# Ревью № 4 — обе половины контракта `extensions_included`
# ---------------------------------------------------------------------------
class TestAccountedNeedsProofNotJustAnAnswer:
    """«Учтён» = кандидат УСПЕШНО ОБРАБОТАН **либо** полный обход ДОКАЗАЛ, что
    кандидатов нет (docs/HELPERS.md). Обе половины существенны.

    Ревью № 3 починило первую (жёсткий `False` при роли EXTENSION врал, ОТДАВАЯ
    строки этого расширения) и промахнулось по второй: «COUNT просто выполнился»
    доказательством не является. Полный обход OPTIONAL index-домена
    (synonyms/metadata) v15 доказать не умеет вовсе — это и есть
    `index_coverage="build_unproven"`, — поэтому пустой ответ там означает «не
    нашли», а не «доказано, что нет».
    """

    @staticmethod
    def _ext_bsl(cf, reader):
        return _make_bsl(
            cf,
            idx_reader=reader,
            current_config_role="extension",
            current_config_name="МоёРасш",
            current_config_root=str(cf),
        )

    class _OptionalReader(_DummyReader):
        """Здоровый v15 с ВКЛЮЧЁННЫМИ optional-доменами и управляемым числом строк."""

        def __init__(self, total, refs):
            self._total = total
            self._refs = refs

        def get_build_capabilities(self):
            return _caps(has_synonyms=True, has_metadata=True)

        def count_objects(self, query, current_prefix=None):
            return {"total": self._total, "current_root": None}

        def find_metadata_references(self, *a, **kw):
            return list(self._refs)

    def test_empty_optional_index_does_not_claim_the_extension(self, cf):
        bsl = self._ext_bsl(cf, self._OptionalReader(0, []))
        res = bsl["search_objects"]("НетТакого", count_only=True)
        assert res["total_extensions"] == 0, res
        assert res["extensions_included"] is False, res
        refs = bsl["find_references_to_object"]("Catalog.НетТакого")
        assert refs["references"] == []
        assert refs["_meta"]["extensions_included"] is False, refs["_meta"]

    def test_nonempty_optional_index_still_claims_it(self, cf):
        """Обратная сторона: строки ЕСТЬ — значит кандидат обработан, флаг обязан
        подняться, иначе вернулся бы дефект ревью № 3."""
        res = self._ext_bsl(cf, self._OptionalReader(3, []))["search_objects"]("Расх", count_only=True)
        assert res["total_extensions"] == 3 and res["extensions_included"] is True, res

    def test_proven_core_domain_may_claim_it_while_empty(self, cf):
        """У `find_code_usages` домен ДРУГОЙ — core-BSL, и его полнота доказуема:
        `index_proof == "ok"` означает `modules_count == bsl_count`. Там пустой
        ответ — это вторая половина контракта, и охват им доказывается."""

        class _Usages(_DummyReader):
            def __init__(self, caps):
                self._c = caps

            def get_build_capabilities(self):
                return dict(self._c)

            def find_code_usages(self, *a, **kw):
                return []

            def count_code_usages(self, *a, **kw):
                return {"total": 0, "by_kind": {}}

        proven = self._ext_bsl(cf, _Usages(_caps()))["find_code_usages"]("Документ.ТестДок")
        assert proven["_meta"]["source"] == "index", proven["_meta"]
        assert proven["_meta"]["extensions_included"] is True, proven["_meta"]

        # Доказанная НЕполнота набора второй половиной контракта не является.
        unproven = self._ext_bsl(cf, _Usages(_caps(bsl_count=2, modules_count=1)))["find_code_usages"](
            "Документ.ТестДок"
        )
        assert unproven["_meta"]["index_proof"] == "incomplete", unproven["_meta"]
        assert unproven["_meta"]["extensions_included"] is False, unproven["_meta"]


def test_agent_facing_texts_do_not_deny_extension_recall():
    """Рецепт и полный prompt — это то, что агент ЧИТАЕТ и по чему действует.
    Безоговорочное «только основная конфигурация» отправляло бы его искать
    несуществующее ограничение ровно там, где живой фолбэк расширения ловит."""
    import pathlib

    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    recipe = build_helper_metadata_snapshot()["find_code_usages"].get("recipe") or ""
    assert "main config only" not in recipe, recipe
    assert "extensions_included" in recipe, recipe

    prompt = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "full_analysis_prompt.md")
    text = prompt.read_text(encoding="utf-8")
    assert "код расширений\n   (только основная конфигурация)" not in text
    assert "код расширений — вне охвата v1.14.0" not in text


# ---------------------------------------------------------------------------
# Ревью № 5 — находки, подтверждённые на исполнении
# ---------------------------------------------------------------------------
class TestExtDefinitionPassIsPrefiltered:
    """Домешивание CFE в индексный `find_definition` не имеет права разбирать
    ВСЕ модули расширений на КАЖДЫЙ вызов.

    Без hint кандидатом становится каждый BSL-модуль расширения, и каждый
    разбирался целиком — с маской комментариев и литералов, без мемоизации, даже
    когда индекс уже дал точный ответ. Замер: 300 модулей по ~400 строк — 3,0 с
    на первом вызове и по 0,57 с на каждом следующем.
    """

    @staticmethod
    def _fixture(tmp_path, n=12):
        root = tmp_path / "src" / "cf"
        ext = tmp_path / "src" / "cfe" / "Расш"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        _write(root / "CommonModules" / "Осн" / "Ext" / "Module.bsl", "Процедура Цель() Экспорт\nКонецПроцедуры\n")
        for i in range(n):
            _write(
                ext / "CommonModules" / f"М{i}" / "Ext" / "Module.bsl",
                f"Процедура Вспомогательная{i}()\nКонецПроцедуры\n",
            )
        # Модуль расширения, который РЕАЛЬНО объявляет искомое.
        _write(ext / "CommonModules" / "Носитель" / "Ext" / "Module.bsl", "Процедура Цель(А) Экспорт\nКонецПроцедуры\n")
        # И модуль, где имя встречается ТОЛЬКО в комментарии.
        _write(
            ext / "CommonModules" / "Обманка" / "Ext" / "Module.bsl",
            "// Процедура Цель() Экспорт\nПроцедура Другая()\nКонецПроцедуры\n",
        )
        return root, ext

    class _Reader(_DummyReader):
        def get_build_capabilities(self):
            return _caps()

        def get_definitions(self, *a, **kw):
            return {
                "rows": [
                    {
                        "rel_path": "CommonModules/Осн/Ext/Module.bsl",
                        "line": 1,
                        "end_line": 2,
                        "type": "Процедура",
                        "is_export": True,
                        "params": [],
                        "category": "CommonModules",
                        "object_name": "Осн",
                        "module_type": "Module",
                    }
                ],
                "total": 1,
                "truncated": False,
                "slow_fallback": False,
            }

    def _bsl(self, root, ext):
        return _make_bsl(
            root,
            extension_paths=[str(ext)],
            idx_reader=self._Reader(),
            current_config_role="main",
            current_config_name="Тест",
            current_config_root=str(root),
        )

    def test_modules_without_the_name_are_never_fully_parsed(self, tmp_path, monkeypatch):
        import rlm_tools_bsl.bsl_helpers as bh

        root, ext = self._fixture(tmp_path)
        calls: list[int] = []
        real = bh.mask_comments_and_strings
        monkeypatch.setattr(bh, "mask_comments_and_strings", lambda lines: (calls.append(1), real(lines))[1])
        bsl = self._bsl(root, ext)
        bsl["find_definition"]("Цель")
        # Разбираются ТОЛЬКО модули, где имя встречается: Носитель и Обманка.
        # 12 модулей-пустышек не должны стоить ни одной маски.
        assert len(calls) <= 2, len(calls)

    def test_recall_is_unchanged(self, tmp_path):
        """Строгая над-аппроксимация: реальное объявление найдено, а имя из
        комментария в выдачу не попало."""
        root, ext = self._fixture(tmp_path)
        res = self._bsl(root, ext)["find_definition"]("Цель")
        owners = sorted(d["file"].replace("\\", "/") for d in res["definitions"])
        assert any("Носитель" in f for f in owners), owners
        assert not any("Обманка" in f for f in owners), owners
        assert any(f.startswith("CommonModules/Осн") for f in owners), owners

    def test_unreadable_module_is_still_a_proven_failure(self, tmp_path, monkeypatch):
        """Префильтр не имеет права превратить отказ чтения в «имени нет»."""
        import rlm_tools_bsl.bsl_helpers as bh

        root, ext = self._fixture(tmp_path, n=1)
        target = "Обманка"
        real_read = bh.Path.read_text

        def boom(self, *a, **kw):
            if target in str(self):
                raise OSError("denied")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(bh.Path, "read_text", boom)
        res = self._bsl(root, ext)["find_definition"]("Цель")
        assert res["_meta"]["failed_extension_files"] >= 1, res["_meta"]
        assert res["_meta"]["total_exact"] is False, res["_meta"]


class TestExtensionsIncludedEdgeSymmetry:
    """Ось охвата обязана вести себя одинаково на ОБОИХ краях."""

    @staticmethod
    def _ext_bsl(cf, reader):
        return _make_bsl(
            cf,
            idx_reader=reader,
            current_config_role="extension",
            current_config_name="Расш",
            current_config_root=str(cf),
        )

    def test_limit_zero_page_does_not_deny_existing_rows(self, cf):
        """`limit=0` — валидное значение: индекс честно отдаёт `LIMIT 0`, и
        доказательством служит НАЛИЧИЕ строк, а не размер отданной страницы."""

        class _R(_DummyReader):
            def get_build_capabilities(self):
                return _caps(has_metadata=True)

            def count_metadata_references(self, *a, **kw):
                return {"total": 4, "by_kind": {"attribute_type": 4}}

            def find_metadata_references(self, canonical, kinds=None, limit=1000):
                row = {
                    "used_in": "Document.Р.Контрагент",
                    "path": "Documents/Р/Ext/Document.xml",
                    "line": 5,
                    "ref_kind": "attribute_type",
                }
                return [row] * min(4, max(0, limit))

        res = self._ext_bsl(cf, _R())["find_references_to_object"]("Catalog.Цель", limit=0)
        assert res["total"] == 4 and res["references"] == []
        assert res["_meta"]["extensions_included"] is True, res["_meta"]

    def test_empty_answer_on_incomplete_domain_does_not_claim_coverage(self, cf):
        """Пустой ответ доказывает охват ТОЛЬКО на полном домене. `find_definition`
        и `find_code_usages` обязаны отвечать на одном состоянии ОДИНАКОВО."""

        class _R(_DummyReader):
            def get_build_capabilities(self):
                return _caps(bsl_count=2, modules_count=1)

            def get_definitions(self, *a, **kw):
                return {"rows": [], "total": 0, "truncated": False, "slow_fallback": False}

            def find_code_usages(self, *a, **kw):
                return []

            def count_code_usages(self, *a, **kw):
                return {"total": 0, "by_kind": {}}

        bsl = self._ext_bsl(cf, _R())
        d = bsl["find_definition"]("НетТакогоМетода")
        u = bsl["find_code_usages"]("Документ.Х")
        assert d["_meta"]["extensions_included"] is False, d["_meta"]
        assert d["_meta"]["extensions_included"] == u["_meta"]["extensions_included"]

    def test_proven_domain_still_claims_coverage_while_empty(self, cf):
        """Обратная сторона: на ДОКАЗАННО полном домене пустой ответ охват
        подтверждает, иначе честность выродилась бы в «всегда False»."""

        class _R(_DummyReader):
            def get_build_capabilities(self):
                return _caps()

            def get_definitions(self, *a, **kw):
                return {"rows": [], "total": 0, "truncated": False, "slow_fallback": False}

        res = self._ext_bsl(cf, _R())["find_definition"]("НетТакогоМетода")
        assert res["_meta"]["extensions_included"] is True, res["_meta"]


def test_functional_options_hint_names_the_actual_reason(cf, monkeypatch):
    """Совет «укажи object_name» агенту, который его УЖЕ указал, — тупик."""
    import rlm_tools_bsl.bsl_helpers as bh

    real = bh.Path.read_text

    def boom(self, *a, **kw):
        if str(self).endswith(".bsl"):
            raise OSError("denied")
        return real(self, *a, **kw)

    monkeypatch.setattr(bh.Path, "read_text", boom)
    res = _make_bsl(cf, private_io=True)["find_functional_options"]("ТестДок")
    meta = res.get("_meta")
    if meta is None:
        pytest.skip("на этой фикстуре неполнота не воспроизвелась")
    assert "укажи object_name" not in meta["hint"], meta
    assert "code-скан" in meta["hint"], meta


def test_documented_return_shape_matches_reality():
    """Дока и agent-facing `sig` обещают строке ровно то, что она несёт."""
    import pathlib

    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    line = next(ln for ln in doc.splitlines() if ln.startswith("- `find_definition("))
    shape = line.split("Возвращает", 1)[1]
    assert "owner" in shape, shape
    assert "partial" in shape, shape
    assert "не доходит (у воркер-процесса нет своих обработчиков" not in doc

    snap = build_helper_metadata_snapshot()
    assert "owner" in snap["find_module"]["sig"], snap["find_module"]["sig"]


# ---------------------------------------------------------------------------
# Ревью № 6 — находки, подтверждённые на исполнении
# ---------------------------------------------------------------------------
def test_builder_path_contract_is_pinned_per_candidate_class():
    """Рамка релиза: builder-путь не тронут.

    Прежний тест сравнивал НОВУЮ функцию с ней же — детерминированную перестановку
    или подмену строк такой тест пропустил бы. Здесь закреплён САМ КОНТРАКТ
    builder-ветки (`_status is None`) поимённо по классам кандидата, плюс
    равенство обеих веток. Порядок категорий из проверки исключён явным
    `categories=`: `_SYNONYM_CATEGORIES` — `frozenset`, и его обход зависит от
    рандомизации хешей процесса, то есть был недетерминирован и ДО релиза.
    """
    import tempfile

    from rlm_tools_bsl.bsl_index import _collect_object_synonyms, _iter_metadata_xml_files

    root = __import__("pathlib").Path(tempfile.mkdtemp()) / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    ns = 'xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:v8="http://v8.1c.ru/8.1/data/core"'

    def _doc(name, syn):
        body = (
            f"<Synonym><v8:item><v8:lang>ru</v8:lang><v8:content>{syn}</v8:content></v8:item></Synonym>" if syn else ""
        )
        _write(
            root / "Documents" / name / "Ext" / "Document.xml",
            f'<?xml version="1.0"?>\n<MetaDataObject {ns}><Document><Properties>'
            f"<Name>{name}</Name>{body}</Properties></Document></MetaDataObject>\n",
        )

    _doc("ССиноним", "Синоним А")
    _doc("БезСинонима", "")
    _write(root / "Documents" / "Битый" / "Ext" / "Document.xml", "<MetaDataObject><Document")
    _write(root / "Documents" / "Чужой" / "Ext" / "Document.xml", '<?xml version="1.0"?>\n<Form/>\n')

    cats = frozenset({"Documents"})
    rows = _collect_object_synonyms(str(root), categories=cats)
    # СТРОКУ даёт ровно объект с <Synonym>; остальные три класса — ни строки.
    assert [r[0] for r in rows] == ["ССиноним"], rows
    assert rows[0][1] == "Documents" and rows[0][3].endswith("Documents/ССиноним/Ext/Document.xml"), rows[0]
    assert rows[0][2].endswith("Синоним А"), rows[0]
    # Все четыре объекта — кандидаты discovery.
    locs = _iter_metadata_xml_files(str(root), categories=cats)
    assert sorted(o for _c, o, _r in locs) == ["БезСинонима", "Битый", "ССиноним", "Чужой"], locs

    # Обе ветки обязаны отдавать ОДНО И ТО ЖЕ: sink влияет только на статус.
    st: dict = {}
    assert _collect_object_synonyms(str(root), categories=cats, _status=st) == rows
    assert _iter_metadata_xml_files(str(root), categories=cats, _status={}) == locs
    # Битый XML — доказанный отказ, объект без <Synonym> и чужой корень — успехи.
    assert st == {"traversal_failures": 0, "candidates": 4, "ok": 3, "failed": 1}, st


class TestOutlineLiveBranchParsesLive:
    """Живая ветка `get_module_outline` обязана читать ФАЙЛ.

    В неё попадают, когда индекс НЕПРИГОДЕН (нет таблицы `regions`, модуля нет в
    индексе, строки модуля пусты), но она звала `extract_procedures`, который
    предпочитает `get_methods_by_path` и лишь ДОПОЛНЯЕТ его живыми методами.
    На старом или отставшем индексе ответ нёс УДАЛЁННЫЙ из файла метод рядом с
    реальным — при `index_used=False`.
    """

    class _OldIndex(_DummyReader):
        def get_build_capabilities(self):
            return _caps()

        def get_outline_data(self, path):
            return None  # таблицы `regions` нет — маршрут уходит в live

        def get_methods_by_path(self, path):
            return [
                {
                    "name": "УдалённыйМетод",
                    "type": "Процедура",
                    "line": 1,
                    "end_line": 2,
                    "is_export": True,
                    "params": "",
                }
            ]

        def get_overrides_for_path(self, path):
            return None

    @staticmethod
    def _names(outline: dict) -> list[str]:
        out = [m.get("name") for m in outline.get("orphan_methods", [])]
        for grp in outline.get("outline", []):
            out += [m.get("name") for m in grp.get("methods", [])]
        return out

    def test_stale_index_method_is_not_published(self, cf):
        bsl = _make_bsl(cf, private_io=True, idx_reader=self._OldIndex())
        res = bsl["get_module_outline"]("CommonModules/ОбщийМодуль/Ext/Module.bsl")
        assert res["_meta"]["index_used"] is False, res["_meta"]
        names = self._names(res)
        assert "УдалённыйМетод" not in names, names
        # Реальный метод файла на месте — сужение не уронило recall.
        assert "ЦелеваяПроцедура" in names, names
        assert res["totals"]["methods"] == 1, res["totals"]


class TestCodeUsagesKindOnLiveRoute:
    """Живой grep вид обращения не определяет и метит всё `unknown`. Молча
    проглоченный `kind` заставлял агента читать `unknown`-строку как строку
    запрошенного вида."""

    def test_live_route_declares_the_filter_inapplicable(self, cf):
        bsl = _make_bsl(cf, private_io=True)
        res = bsl["find_code_usages"]("Документ.ТестДок", kind="manager")
        assert res["_meta"]["source"] == "live"
        assert res["_meta"]["kind_filter_applied"] is False, res["_meta"]
        assert "kind" in res["_meta"]["hint"], res["_meta"]["hint"]
        assert all(u["kind"] == "unknown" for u in res["usages"])

    def test_live_route_without_kind_is_not_flagged(self, cf):
        res = _make_bsl(cf, private_io=True)["find_code_usages"]("Документ.ТестДок")
        assert res["_meta"]["kind_filter_applied"] is True, res["_meta"]

    def test_index_route_applies_the_filter(self, cf):
        class _R(_DummyReader):
            def get_build_capabilities(self):
                return _caps()

            def find_code_usages(self, canonical, kind=None, limit=1000):
                assert kind == "manager", "фильтр обязан доехать до ридера"
                return []

            def count_code_usages(self, *a, **kw):
                return {"total": 0, "by_kind": {}}

        res = _make_bsl(cf, idx_reader=_R())["find_code_usages"]("Документ.ТестДок", kind="manager")
        assert res["_meta"]["source"] == "index"
        assert res["_meta"]["kind_filter_applied"] is True, res["_meta"]


def test_functional_options_code_scope_is_declared(tmp_path):
    """`code_total=0` означает «нет в модулях ЭТОГО объекта», а не «нет во всей
    конфигурации»: кандидаты сужены `name_hint`-ом. Домен обязан быть назван."""
    root = tmp_path / "cf"
    _write(root / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        root / "Documents" / "Цель" / "Ext" / "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\nКонецПроцедуры\n",
    )
    _write(
        root / "CommonModules" / "Независимый" / "Ext" / "Module.bsl",
        'Процедура П() Экспорт\n    Если ПолучитьФункциональнуюОпцию("Скрытая") Тогда\n    КонецЕсли;\nКонецПроцедуры\n',
    )
    bsl = _make_bsl(root, private_io=True)
    # Несуженный скан ВИДИТ вызов — значит сужение реально, а не пустой каталог.
    assert any(
        "Независимый" in r["file"] for r in bsl["safe_grep"]("ПолучитьФункциональнуюОпцию", max_files=100)["results"]
    )
    # ГЛАВНОЕ: домен назван на ПОЛНОМ (успешном) ответе — там, где `code_total=0`
    # и читается как «во всей конфигурации вызовов нет». Ветка неполноты здесь
    # не участвует: `partial` не выставлен.
    res = bsl["find_functional_options"]("Цель")
    assert res["code_total"] == 0, res
    assert res.get("partial") is None, res
    assert res["_meta"]["code_scope"] == "object_modules", res["_meta"]
    # Обратная сторона: без code-скана описывать нечего, и замороженный legacy-набор
    # ключей обязан остаться прежним (его стережёт test_arg_guards).
    assert "_meta" not in bsl["find_functional_options"]("Цель", include_code=False)

    # Домен обязан быть назван МАШИННО там, где `_meta` публикуется: пустой обзор
    # с каталогом больше бюджета даёт причину, а значит и `_meta`.
    for i in range(25):
        _write(root / "CommonModules" / f"Ш{i}" / "Ext" / "Module.bsl", "Процедура Ш() Экспорт\nКонецПроцедуры\n")
    overview = _make_bsl(root, private_io=True)["find_functional_options"]("")
    assert overview["_meta"]["code_scope"] == "overview_budget", overview["_meta"]

    import pathlib

    doc = pathlib.Path(__file__).resolve().parents[1].joinpath("docs", "HELPERS.md").read_text(encoding="utf-8")
    line = next(ln for ln in doc.splitlines() if ln.startswith("- `find_functional_options("))
    assert "code_scope" in line, "домен code-корзины не объявлен в доке"
    assert "весь текущий файловый BSL-каталог сессии, а не список" not in line, "осталось прежнее «весь каталог»"


# ---------------------------------------------------------------------------
# Ревью № 8 — находки внешнего ревьюера
# ---------------------------------------------------------------------------
_SYNONYM_OBJ = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Catalog uuid="u"><Properties><Name>{name}</Name>'
    '<Synonym><v8:item xmlns:v8="http://v8.1c.ru/8.1/data/core">'
    "<v8:lang>ru</v8:lang><v8:content>{name}Синоним</v8:content></v8:item></Synonym>"
    "</Properties></Catalog>\n</MetaDataObject>\n"
)


def _cf_with_ext_synonyms(tmp_path):
    """MAIN + nearby CFE, у ОБОИХ есть объект метаданных с синонимом."""
    main = tmp_path / "cf"
    ext = tmp_path / "cfe" / "РасшТест"
    _write(main / "Configuration.xml", _CF_DESCRIPTOR)
    _write(main / "Catalogs" / "ГлавныйОбъект" / "ГлавныйОбъект.xml", _SYNONYM_OBJ.format(name="ГлавныйОбъект"))
    _write(main / "CommonModules" / "ОМ" / "Ext" / "Module.bsl", "Процедура П() Экспорт\nКонецПроцедуры\n")
    _write(ext / "Configuration.xml", _CF_DESCRIPTOR.replace("<Name>Тест</Name>", "<Name>РасшТест</Name>"))
    _write(ext / "Catalogs" / "ОбъектРасширения" / "ОбъектРасширения.xml", _SYNONYM_OBJ.format(name="ОбъектРасширения"))
    return main, ext


def test_synonym_count_never_claims_exactness_without_reading_main(tmp_path):
    """``search_objects(count_only=True)`` БЕЗ индекса не смеет объявлять итог точным.

    MAIN-домен синонимов считает ТОЛЬКО индексный COUNT — живого сканера
    ``object_synonyms`` основной конфигурации в проекте нет вовсе. Значит без
    ридера ``total`` описывает ОДНИ соседние расширения, и ``total_exact=True``
    был бы заявлением о полноте домена, в который ни разу не заглянули. Ровно
    такой ответ и выдавался: ``index_coverage == "not_used"`` истинно только при
    ``idx_reader is None``, а остальные слагаемые формулы про main молчали.
    """
    main, ext = _cf_with_ext_synonyms(tmp_path)
    bsl = _make_bsl(main, extension_paths=[str(ext)])

    res = bsl["search_objects"]("", count_only=True)
    assert res["source"] == "live", res
    # ГЛАВНОЕ: непросмотренный main запрещает заявлять точность.
    assert res["total_exact"] is False, res
    assert res["partial"] is True, res
    # Причина видна машинно, а не только общим `partial`.
    assert res["_meta"]["current_root_accounted"] is False, res["_meta"]
    assert res["_meta"]["index_coverage"] == "not_used", res["_meta"]
    # Инвариант count↔list при этом остаётся целым.
    assert res["total"] == len(bsl["search_objects"]("", limit=10**9)), res


def test_synonym_count_exactness_is_unreachable_while_v15_cannot_prove_it(tmp_path):
    """Обратная сторона: С ридером точность тоже не заявляется — но по ДРУГОЙ причине.

    Две половины (``current_root_accounted`` и ``index_coverage == "not_used"``)
    в v15 взаимоисключающи, и тест фиксирует это НАМЕРЕННО: если будущий бамп
    схемы научится доказывать optional-домен, тест упадёт и заставит осознанно
    переписать контракт, а не тихо разрешить точность.
    """
    main, ext = _cf_with_ext_synonyms(tmp_path)

    class _Reader(_DummyReader):
        def count_objects(self, query, current_prefix=None):
            return {"total": 5, "current_root": None}

    res = _make_bsl(main, extension_paths=[str(ext)], idx_reader=_Reader())["search_objects"]("", count_only=True)
    assert res["_meta"]["current_root_accounted"] is True, res["_meta"]
    assert res["_meta"]["index_coverage"] != "not_used", res["_meta"]
    assert res["total_exact"] is False, res


@pytest.mark.parametrize("value", [float("inf"), float("-inf")])
def test_numeric_guard_survives_infinity(cf, value):
    """ТОТАЛЬНЫЙ гард обязан пережить вход, ради которого и существует.

    ``int(float("inf"))`` бросает ``OverflowError``, поэтому ``_coerce_bound``
    падал САМ — и на новых публичных ``limit``/``offset`` этого релиза, и на уже
    существовавших. Соседний ``_normalize_role_details_limit`` этот дефект знал и
    обходил, то есть два нормализатора одного проекта расходились.
    """
    bsl = _make_bsl(cf)
    assert isinstance(bsl["find_module"]("", limit=value), list)
    assert isinstance(bsl["find_by_type"]("Documents", limit=value), list)
    assert isinstance(bsl["get_overrides"](limit=value, offset=value), dict)
    # Конечные значения не задеты: 0 остаётся ВАЛИДНЫМ, а не «мусором».
    assert bsl["find_module"]("", limit=0) == []


def test_agent_facing_contract_of_find_roles_and_find_definition_is_current():
    """Инвариант 8 плана: меняешь контракт — правишь ТЕКСТ, который агент и читает.

    Обе точки отстали от кода: ``find_roles`` не сообщала ни ``details_limit``, ни
    того, что это BROAD substring, ни ключей детализации; ``find_definition`` не
    называла ``owner``, хотя ``docs/HELPERS.md`` его обещает.

    Разделение каналов НАМЕРЕННОЕ и проверяется явно: ``sig`` уезжает агенту на
    КАЖДОМ старте и лежит в бюджете ``rlm_start`` (запас на slim-payload — десятки
    символов), поэтому в нём остаётся минимум — параметр и предупреждение о типе
    совпадения. Полный контракт деталей живёт в ``recipe``, который НЕ
    бюджетируется и отдаётся по запросу ``rlm_help(helpers=['find_roles'])``.
    Это ровно то разрешение конфликта инвариантов 6 и 8, которое план и
    предписывает: «длинные пояснения живут в recipe (не в бюджете)».
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    snap = build_helper_metadata_snapshot()
    roles_sig = snap["find_roles"]["sig"]
    # sig: сам факт параметра + предупреждение, что запрос НЕ точный.
    assert "details_limit" in roles_sig, roles_sig
    assert "match" in roles_sig and "case_sensitive" in roles_sig, roles_sig
    assert "BROAD" in roles_sig, "sig не предупреждает, что это НЕ точный запрос"

    # recipe: полный контракт детализации, включая ловушку «список полон».
    roles_recipe = snap["find_roles"]["recipe"] or ""
    for token in ("matched_objects", "rights_by_object", "details_truncated", "substring"):
        assert token in roles_recipe, f"{token!r} не доехал до agent-facing recipe"
    assert "get_object_profile" in roles_recipe, "recipe не называет точный маршрут"

    # find_definition: owner дешёвый и помещается прямо в sig.
    assert "owner" in snap["find_definition"]["sig"], snap["find_definition"]["sig"]


def test_empty_kinds_list_keeps_meaning_all_kinds(cf):
    """`kinds=[]` == `kinds=None` == ВСЕ виды — контракт СОХРАНЁН, а не изменён.

    Проверять его надо именно теперь. До релиза обе ветки читали аргумент прямо:
    индексный ридер через `if kinds:`, живой обход через
    `kinds_set = set(kinds) if kinds else None`, — то есть пустой список у обеих
    означал «все виды», и расхождения не было.

    Этот релиз добавил capability-слой (`_requested_live_kinds`,
    `_unsupported_ref_kinds`, `_route_relevant`), который строит из аргумента
    `frozenset`. На пустом списке такой frozenset пуст, поэтому БЕЗ нормализации
    `_effective_ref_kinds` живой маршрут перестал бы считать какой-либо файл
    релевантным кандидатом, а `unsupported_kinds` вернулся бы пустым — то есть
    новый слой ВНЁС бы расхождение там, где его никогда не было. Нормализация
    ровно это и предотвращает; тест стережёт её от случайного удаления.
    """
    bsl = _make_bsl(cf)
    empty = bsl["find_references_to_object"]("Документ.ТестДок", kinds=[])
    none_ = bsl["find_references_to_object"]("Документ.ТестДок", kinds=None)

    # Полное равенство ответов: и строки, и обе оси охвата, и счётчики переписи.
    assert empty == none_, (empty, none_)
    # И то, и другое — «все виды», поэтому ограничение живого обхода названо
    # поимённо, а не спрятано пустым списком.
    assert empty["_meta"]["unsupported_kinds"], empty["_meta"]
    assert "role_rights" in empty["_meta"]["unsupported_kinds"], empty["_meta"]


# ---------------------------------------------------------------------------
# Ревью № 10 (Codex, финальное) — физическая identity источников
# ---------------------------------------------------------------------------
def _make_junction(link, target) -> bool:
    """Windows junction: прав НЕ требует (в отличие от symlink). True — создан."""
    if os.name != "nt":
        return False
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


def _try_symlink(link, target, *, directory: bool) -> bool:
    try:
        os.symlink(str(target), str(link), target_is_directory=directory)
    except (OSError, NotImplementedError, AttributeError):
        return False
    return True


def _drop_reparse_points(root) -> None:
    """Каталог-редирект удаляется как ССЫЛКА: rmtree иначе уйдёт в цель."""
    for p in sorted(root.rglob("*"), reverse=True):
        try:
            if p.is_dir() and (getattr(p.lstat(), "st_reparse_tag", 0) or p.is_symlink()):
                os.rmdir(p)
        except OSError:
            pass


@pytest.fixture
def cf_ext_pair(tmp_path):
    """MAIN + соседнее CFE с ОДНИМ общим модулем: задвоение видно сразу."""
    base = tmp_path / "src" / "cf"
    ext = tmp_path / "src" / "cfe" / "Расш"
    _write(base / "Configuration.xml", _CF_DESCRIPTOR)
    _write(
        base / "CommonModules" / "СекретныйГлавный" / "Ext" / "Module.bsl",
        "Процедура Главная() Экспорт\nКонецПроцедуры\n",
    )
    _write(ext / "Configuration.xml", _CFE_DESCRIPTOR)
    _write(
        ext / "CommonModules" / "РасшМодуль" / "Ext" / "Module.bsl",
        "Процедура РасшПроцедура() Экспорт\nКонецПроцедуры\n",
    )
    try:
        yield base, ext
    finally:
        _drop_reparse_points(tmp_path)


class TestOneFileOneRow:
    """Один ФИЗИЧЕСКИЙ модуль — ровно одна строка выдачи.

    Дефект ДОрелизный: проверено исполнением на 1a04038 — junction внутри CFE
    давал две строки. os.walk отсекает POSIX-симлинк на каталог сам, но Windows
    junction симлинком не является (os.path.islink -> False, st_reparse_tag =
    0xA0000003), поэтому спуск шёл и по нему тоже.
    """

    @staticmethod
    def _rows(base, ext, name="РасшМодуль"):
        res = _make_bsl(base, extension_paths=[str(ext)])["find_module"](name, limit=100)
        return res["results"] if isinstance(res, dict) else res

    def test_junction_inside_extension_does_not_double_the_module(self, cf_ext_pair):
        base, ext = cf_ext_pair
        if not _make_junction(ext / "CommonModulesLink", ext / "CommonModules"):
            pytest.skip("junction создать не удалось (не Windows либо mklink недоступен)")
        rows = self._rows(base, ext)
        assert len(rows) == 1, [r["path"] for r in rows]
        # Победил ОБЫЧНЫЙ физический путь, а не маршрут через ссылку.
        assert "CommonModulesLink" not in rows[0]["path"], rows[0]["path"]

    def test_symlinked_bsl_inside_extension_does_not_double_the_module(self, cf_ext_pair):
        """Симлинк на .bsl внутри того же root разрешается в ту же цель.

        Без гарда это ДВЕ строки с ОДИНАКОВЫМ путём — задвоение, которое агент
        от двух разных модулей отличить не может вовсе.
        """
        base, ext = cf_ext_pair
        real = ext / "CommonModules" / "РасшМодуль" / "Ext" / "Module.bsl"
        (ext / "Alias").mkdir(parents=True, exist_ok=True)
        if not _try_symlink(ext / "Alias" / "Module.bsl", real, directory=False):
            pytest.skip("symlink на файл требует прав (SeCreateSymbolicLinkPrivilege)")
        rows = self._rows(base, ext)
        assert len(rows) == 1, [r["path"] for r in rows]

    def test_junction_out_of_the_extension_stays_excluded(self, cf_ext_pair):
        """Сосед фикса: цель ВНЕ ext_root по-прежнему исключается целиком.

        Ради этого ветка редиректов и появилась — иначе main-модуль получил бы
        ext-owner. Расширение при этом не должно поредеть.
        """
        base, ext = cf_ext_pair
        if not _make_junction(ext / "Чужое", base / "CommonModules"):
            pytest.skip("junction создать не удалось (не Windows либо mklink недоступен)")
        res = _make_bsl(base, extension_paths=[str(ext)])["find_by_type"]("CommonModule", limit=100)
        rows = res["results"] if isinstance(res, dict) else res
        paths = [r["path"] for r in rows]
        assert not [p for p in paths if "Чужое" in p], paths
        assert sum(1 for r in rows if r["owner"] == "extension:МоёРасш") == 1, paths
        assert sum(1 for r in rows if r["owner"] == "main") == 1, paths


class TestIndexIdentityIsProven:
    """Индекс, построенный от ДРУГОГО корня, доказательством не является.

    На штатном серверном маршруте недостижимо (get_index_db_path — md5
    нормализованного base_path), но достижимо при legacy/direct embedding и при
    подменённом файле БД. Проверяется на НАСТОЯЩИХ индексах: стаб доказывал бы
    здесь только сам себя.
    """

    # Синоним и подчинённый справочник нужны ОПТИОНАЛЬНЫМ доменам: без них
    # `object_synonyms` и `metadata_references` пусты, и тесты чужой базы прошли бы
    # вакуумно — «чужой строки нет», потому что её нет ни у кого.
    _DOC_XML = (
        '<?xml version="1.0"?>\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        "<Document><Properties><Name>{name}</Name>\n"
        '<Synonym><v8:item xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<v8:lang>ru</v8:lang><v8:content>Синоним {name}</v8:content></v8:item></Synonym>\n"
        "</Properties></Document></MetaDataObject>\n"
    )

    _CAT_XML = (
        '<?xml version="1.0"?>\n<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
        "<Catalog><Properties><Name>{name}</Name>\n"
        '<Synonym><v8:item xmlns:v8="http://v8.1c.ru/8.1/data/core">'
        "<v8:lang>ru</v8:lang><v8:content>Синоним {name}</v8:content></v8:item></Synonym>\n"
        "</Properties></Catalog></MetaDataObject>\n"
    )

    _OWNED_CAT_XML = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses"\n'
        '                xmlns:xr="http://v8.1c.ru/8.3/xcf/readable"\n'
        '                xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <Catalog><Properties><Name>{name}</Name>\n"
        '    <Owners><xr:Item xsi:type="xr:MDObjectRef">{owner}</xr:Item></Owners>\n'
        "  </Properties></Catalog></MetaDataObject>\n"
    )

    @classmethod
    def _config(cls, root, name, uuid, doc, tag):
        _write(
            root / "Configuration.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
            f'  <Configuration uuid="{uuid}">\n'
            f"    <Properties><Name>{name}</Name></Properties>\n"
            "  </Configuration>\n</MetaDataObject>\n",
        )
        _write(root / "Documents" / doc / "Ext" / "Document.xml", cls._DOC_XML.format(name=doc))
        _write(
            root / "Documents" / doc / "Ext" / "ObjectModule.bsl",
            f"Процедура Метод_{doc}() Экспорт\nКонецПроцедуры\n",
        )
        _write(root / "Catalogs" / f"Владелец{tag}" / "Ext" / "Catalog.xml", cls._CAT_XML.format(name=f"Владелец{tag}"))
        _write(
            root / "Catalogs" / f"Подчинённый{tag}" / "Ext" / "Catalog.xml",
            cls._OWNED_CAT_XML.format(name=f"Подчинённый{tag}", owner=f"Catalog.Владелец{tag}"),
        )

    @staticmethod
    def _build(root):
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

        IndexBuilder().build(str(root), build_calls=False, build_metadata=True)
        return IndexReader(get_index_db_path(str(root)))

    @pytest.fixture
    def two_configs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / "idx"))
        (tmp_path / "idx").mkdir(parents=True, exist_ok=True)
        a = tmp_path / "src" / "confA"
        b = tmp_path / "src" / "confB"
        self._config(a, "КонфА", "00000000-0000-0000-0000-0000000000aa", "ДокументА", "А")
        self._config(b, "КонфБ", "00000000-0000-0000-0000-0000000000bb", "ДокументБ", "Б")
        return a, b

    def test_index_from_another_base_is_not_trusted(self, two_configs):
        a, b = two_configs
        reader = self._build(b)
        try:
            bsl = _make_bsl(a, idx_reader=reader)
            cnt = bsl["find_by_type"]("Document", count_only=True)
            # Точность НЕ заявляется: домен текущей базы не читался вовсе.
            assert cnt["total_exact"] is False, cnt
            assert cnt["partial"] is True, cnt
            res = bsl["find_by_type"]("Document", limit=100)
            rows = res["results"] if isinstance(res, dict) else res
            names = [r.get("object_name") for r in rows]
            # Чужой объект в выдачу не попадает, СВОЙ — попадает (уход на live).
            assert "ДокументБ" not in names, names
            assert "ДокументА" in names, names
            assert bsl["find_definition"]("Метод_ДокументБ")["total"] == 0
        finally:
            reader.close()

    @pytest.mark.parametrize("spelling", ["as_is", "upper", "trailing_sep", "dot_dot"])
    def test_own_index_stays_trusted_across_path_spellings(self, two_configs, spelling):
        """Сверка НОРМАЛИЗОВАННАЯ: сырое сравнение строк развалилось бы на регистре,
        завершающем разделителе и `..`, отправив здоровый индекс в live."""
        if spelling == "upper" and os.path.normcase("A") != os.path.normcase("a"):
            # На регистрозависимой ФС путь в верхнем регистре — ДРУГОЙ путь, а не иное
            # написание того же. `normcase` там регистр не фолдит, поэтому «здоровый
            # индекс» в этом написании обязан считаться чужим — сверка работает верно,
            # проверять на этой платформе нечего. Условие взято от САМОГО normcase, а не
            # от `os.name`: оно и есть предикат, по которому сверка судит.
            pytest.skip("регистрозависимая ФС: верхний регистр — другой путь, а не то же написание")
        a, _b = two_configs
        reader = self._build(a)
        try:
            session = {
                "as_is": str(a),
                "upper": str(a).upper(),
                "trailing_sep": str(a) + os.sep,
                "dot_dot": str(a.parent / "нет-такого" / ".." / a.name),
            }[spelling]
            bsl = _make_bsl(session, idx_reader=reader)
            cnt = bsl["find_by_type"]("Document", count_only=True)
            assert cnt["total_exact"] is True, (spelling, cnt)
            assert bsl["find_definition"]("Метод_ДокументА")["_meta"]["source"] == "index", spelling
        finally:
            reader.close()

    def test_real_capabilities_always_state_the_base(self, two_configs):
        """ПРЕДПОСЫЛКА сверки: пустой base_path у боевого ридера недостижим.

        Сверка идентичности судит только ЗАЯВЛЕННУЮ базу, а «не заявлена» пропускает.
        Это корректно ровно потому, что get_build_capabilities отдаёт None, если
        base_path пуст. Если предпосылка когда-нибудь отвалится, упадёт этот тест,
        а не молча ослабнет гард.
        """
        a, _b = two_configs
        reader = self._build(a)
        try:
            caps = reader.get_build_capabilities()
            assert caps is not None and caps["base_path"], caps
        finally:
            reader.close()

    def test_synonym_search_does_not_publish_rows_of_another_base(self, two_configs):
        """Optional-домен синонимов: чужие строки не доезжают ни списком, ни счётом.

        Строки чужой базы не «недоказуемы», а НЕВЕРНЫ: они называют объекты,
        которых в открытой конфигурации нет, и ведут на несуществующие файлы.
        Гейт обязан стоять в ОБЕИХ ветках — паритет `count_only(q)['total'] ==
        len(search_objects(q, limit=10**9))` задокументирован в самом хелпере.
        """
        a, b = two_configs
        reader = self._build(b)
        try:
            bsl = _make_bsl(a, idx_reader=reader)
            names = [r["object_name"] for r in bsl["search_objects"]("", limit=100)]
            assert "ДокументБ" not in names and "ВладелецБ" not in names, names
            cnt = bsl["search_objects"]("", count_only=True)
            assert cnt["source"] != "index", cnt
            assert cnt["total"] == 0 and cnt["partial"] is True, cnt
            assert cnt["_meta"]["current_root_accounted"] is False, cnt["_meta"]
            assert cnt["total"] == len(bsl["search_objects"]("", limit=10**9)), cnt
        finally:
            reader.close()

    def test_own_synonym_rows_survive(self, two_configs):
        """Обратная сторона: СВОЙ индекс синонимов по-прежнему отвечает из индекса."""
        a, _b = two_configs
        reader = self._build(a)
        try:
            bsl = _make_bsl(a, idx_reader=reader)
            names = [r["object_name"] for r in bsl["search_objects"]("", limit=100)]
            assert "ДокументА" in names and "ВладелецА" in names, names
            cnt = bsl["search_objects"]("", count_only=True)
            assert cnt["source"] == "index" and cnt["total"] == len(names), cnt
        finally:
            reader.close()

    def test_metadata_references_do_not_come_from_another_base(self, two_configs):
        """Перепись ссылок на чужом индексе уходит в live, а не выдаёт чужие строки.

        Проверяется и то, что фолбэк СОДЕРЖАТЕЛЕН: ссылку своей конфигурации он
        находит. Пустой ответ доказательством фикса не был бы — он получается и
        при полностью сломанном маршруте.
        """
        a, b = two_configs
        foreign = self._build(b)
        try:
            bsl = _make_bsl(a, idx_reader=foreign)
            alien = bsl["find_references_to_object"]("Catalog.ВладелецБ")
            assert alien["total"] == 0 and alien["references"] == [], alien
            assert alien["_meta"]["source"] == "live", alien["_meta"]
            own = bsl["find_references_to_object"]("Catalog.ВладелецА")
            assert own["_meta"]["source"] == "live", own["_meta"]
            assert own["total"] == 1 and own["partial"] is True, own
            assert own["references"][0]["kind"] == "owner", own["references"]
            assert "ПодчинённыйА" in own["references"][0]["path"], own["references"]
        finally:
            foreign.close()
        native = self._build(a)
        try:
            bsl = _make_bsl(a, idx_reader=native)
            own = bsl["find_references_to_object"]("Catalog.ВладелецА")
            # Обратная сторона: СВОЙ индекс маршрут не теряет.
            assert own["_meta"]["source"] == "index" and own["total"] == 1, own
        finally:
            native.close()

    @staticmethod
    def _break_built_at(root):
        """Портит ТОЛЬКО `built_at`: `base_path` и строки индекса остаются читаемыми."""
        import sqlite3

        from rlm_tools_bsl.bsl_index import get_index_db_path

        con = sqlite3.connect(get_index_db_path(str(root)))
        try:
            con.execute("UPDATE index_meta SET value='не-число' WHERE key='built_at'")
            con.commit()
        finally:
            con.close()

    def test_foreign_base_is_caught_even_when_capabilities_are_unreadable(self, two_configs):
        """Идентичность спрашивается ОТДЕЛЬНО от capability-переписи.

        `get_build_capabilities` отдаёт `None` на ЛЮБОЙ негодности меты (нечитаемый
        `built_at`, отсутствующий `bsl_count`), хотя сам `base_path` при этом
        читается прекрасно. Гейт по снимку пропускал бы чужой индекс ровно в том
        состоянии, ради которого сверка и заводилась, — у подменённой БД.
        """
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

        a, b = two_configs
        IndexBuilder().build(str(b), build_calls=False, build_metadata=True)
        self._break_built_at(b)
        reader = IndexReader(get_index_db_path(str(b)))
        try:
            # Предпосылка находки: перепись недоступна, а база — заявлена.
            assert reader.get_build_capabilities() is None
            assert reader.get_declared_base_path(), "узкое чтение обязано видеть базу"
            bsl = _make_bsl(a, idx_reader=reader)
            assert [r["object_name"] for r in bsl["search_objects"]("", limit=100)] == []
            cnt = bsl["search_objects"]("", count_only=True)
            assert cnt["total"] == 0 and cnt["source"] != "index", cnt
            ref = bsl["find_references_to_object"]("Catalog.ВладелецБ")
            assert ref["total"] == 0 and ref["_meta"]["source"] == "live", ref
        finally:
            reader.close()

    def test_own_base_with_unreadable_capabilities_still_answers(self, two_configs):
        """Обратная сторона: недоступная перепись — НЕ повод отвергать СВОЙ индекс.

        Недоступные capabilities о принадлежности базе не говорят ничего. Сузить
        ответ по ним значило бы выключить индекс из-за испорченного ключа меты,
        никак не связанного с тем, чью конфигурацию индекс описывает.
        """
        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

        a, _b = two_configs
        IndexBuilder().build(str(a), build_calls=False, build_metadata=True)
        self._break_built_at(a)
        reader = IndexReader(get_index_db_path(str(a)))
        try:
            assert reader.get_build_capabilities() is None
            bsl = _make_bsl(a, idx_reader=reader)
            names = [r["object_name"] for r in bsl["search_objects"]("", limit=100)]
            assert "ДокументА" in names, names
            cnt = bsl["search_objects"]("", count_only=True)
            assert cnt["source"] == "index" and cnt["total"] == len(names), cnt
            assert cnt["_meta"]["index_coverage"] == "unavailable", cnt["_meta"]
        finally:
            reader.close()

    _LEGACY_ROW = {"object_name": "Чужой", "category": "Catalog", "synonym": "Ч", "file": "Catalogs/Ч/Ext/Catalog.xml"}
    _LEGACY_REF = {"used_in": "Catalog.X.Owners", "path": "Catalogs/X/Ext/Catalog.xml", "line": 1, "ref_kind": "owner"}

    class _LegacyStrict:
        """Прокси образца 1.33 с БЕЛЫМ СПИСКОМ методов: неизвестное → `AttributeError`."""

        has_fts = False
        has_calls = False

        def __init__(self, declared_base, outer):
            self._base = str(declared_base)
            self._outer = outer

        def get_build_capabilities(self):
            return _caps(has_synonyms=True, has_metadata=True, base_path=self._base, built_at="0")

        def search_objects(self, query, limit):
            return [dict(self._outer._LEGACY_ROW)]

        def count_objects(self, query, current_prefix=None):
            return {"total": 1, "current_root": None}

        def count_metadata_references(self, *a, **kw):
            return {"total": 1, "by_kind": {"owner": 1}}

        def find_metadata_references(self, *a, **kw):
            return [dict(self._outer._LEGACY_REF)]

    class _LegacyLenient(_DummyReader):
        """Адаптер, у которого НЕИЗВЕСТНЫЙ метод не падает, а отдаёт `None`.

        Вторая форма того же класса: `AttributeError` не поднимается вовсе, поэтому
        `try/except` её не ловит — узкое чтение просто возвращает `None`.
        """

        def __init__(self, declared_base, outer):
            self._base = str(declared_base)
            self._outer = outer

        def get_build_capabilities(self):
            return _caps(has_synonyms=True, has_metadata=True, base_path=self._base, built_at="0")

        def search_objects(self, query, limit):
            return [dict(self._outer._LEGACY_ROW)]

        def count_objects(self, query, current_prefix=None):
            return {"total": 1, "current_root": None}

        def count_metadata_references(self, *a, **kw):
            return {"total": 1, "by_kind": {"owner": 1}}

        def find_metadata_references(self, *a, **kw):
            return [dict(self._outer._LEGACY_REF)]

    def _legacy(self, kind, declared_base):
        cls = self._LegacyStrict if kind == "raises" else self._LegacyLenient
        reader = cls(declared_base, self)
        # Предпосылка обеих форм: узкого метода фактически нет.
        try:
            assert reader.get_declared_base_path() is None, "стаб не должен знать узкий метод"
        except AttributeError:
            pass
        assert reader.get_build_capabilities()["base_path"], "старый API базу заявляет"
        return reader

    @pytest.mark.parametrize("kind", ["raises", "returns_none"])
    def test_legacy_reader_without_the_narrow_method_keeps_its_foreign_gate(self, two_configs, kind):
        """`idx_reader` — duck-typed параметр: адаптер может знать только СТАРЫЙ API.

        Узкое чтение базы ему недоступно (в одной форме падает `AttributeError`, в
        другой молча отдаёт `None`), и без compatibility-фолбэка такой ридер ПОТЕРЯЛ БЫ
        гейт, который у него уже был.
        """
        a, b = two_configs
        bsl = _make_bsl(a, idx_reader=self._legacy(kind, b))
        assert [r["object_name"] for r in bsl["search_objects"]("", limit=100)] == []
        cnt = bsl["search_objects"]("", count_only=True)
        assert cnt["total"] == 0 and cnt["source"] != "index", cnt
        ref = bsl["find_references_to_object"]("Catalog.ВладелецБ")
        assert ref["total"] == 0 and ref["_meta"]["source"] == "live", ref

    @pytest.mark.parametrize("kind", ["raises", "returns_none"])
    def test_legacy_reader_declaring_the_OWN_base_is_not_rejected(self, two_configs, kind):
        """Обратная сторона: фолбэк не превращается в отказ от любого старого ридера."""
        a, _b = two_configs
        bsl = _make_bsl(a, idx_reader=self._legacy(kind, a))
        assert [r["object_name"] for r in bsl["search_objects"]("", limit=100)] == ["Чужой"]
        cnt = bsl["search_objects"]("", count_only=True)
        assert cnt["total"] == 1 and cnt["source"] == "index", cnt
        ref = bsl["find_references_to_object"]("Catalog.ВладелецА")
        assert ref["total"] == 1 and ref["_meta"]["source"] == "index", ref

    def test_unproven_generation_of_the_OWN_base_still_answers(self, two_configs):
        """Гейт стоит на ЧУЖОЙ базе, а НЕ на `index_coverage == "unavailable"`.

        У «unavailable» три причины, и две из них (идущая пересборка, смена
        поколения между снимками) относятся к ПРАВИЛЬНОЙ базе: строки там
        недоказуемы, но не ложны. Живого сканера синонимов основной конфигурации в
        проекте нет, поэтому отказ от них превратил бы `search_objects` в пустой
        ответ на каждой пересборке индекса. Тест фиксирует это решение: маркер
        сборки ставится на СВОЙ индекс, покрытие честно падает в `unavailable`,
        а строки остаются.
        """
        import sqlite3

        from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader, get_index_db_path

        a, _b = two_configs
        IndexBuilder().build(str(a), build_calls=False, build_metadata=True)
        db = get_index_db_path(str(a))
        con = sqlite3.connect(db)
        try:
            con.execute("INSERT OR REPLACE INTO index_meta(key, value) VALUES('build_in_progress', '1')")
            con.commit()
        finally:
            con.close()
        reader = IndexReader(db)
        try:
            bsl = _make_bsl(a, idx_reader=reader)
            cnt = bsl["search_objects"]("", count_only=True)
            assert cnt["_meta"]["index_coverage"] == "unavailable", cnt["_meta"]
            assert cnt["source"] == "index" and cnt["total"] > 0, cnt
            assert [r["object_name"] for r in bsl["search_objects"]("", limit=100)], "строки своей базы пропали"
        finally:
            reader.close()


class TestCoverageLegendSection:
    """`rlm_help(section='coverage')` — единая легенда осей охвата (v1.34.0).

    Релиз добавил СЕМЬ машинных осей (`source`, `owner`, `extensions_included`,
    `total_exact`, `partial`, `index_coverage`, `truncated`/`has_more`) поверх
    замороженного legacy-`scope`. Секция — единственное место, где они описаны
    агенту целиком; тесты стерегут её от расхождения с фактическим контрактом.
    """

    @staticmethod
    def _text():
        from rlm_tools_bsl.bsl_knowledge import _get_section

        return _get_section("coverage")

    def test_section_is_listed_and_reachable(self):
        import json as _json

        from rlm_tools_bsl.bsl_knowledge import list_sections
        from rlm_tools_bsl.server import _rlm_help_dispatch

        assert "coverage" in list_sections()
        res = _json.loads(_rlm_help_dispatch(section="coverage"))
        assert res["mode"] == "section", res
        assert res["result"]["section"] == "coverage"
        assert "== COVERAGE" in res["result"]["text"]

    def test_names_every_index_coverage_value_the_code_can_produce(self):
        """Значения берутся ИЗ КОДА, а не переписываются в тест руками.

        `_INDEX_COVERAGE_VALUES` — локальная константа фабрики, импортировать её
        нельзя, поэтому читаем исходник самой фабрики: добавится шестое значение —
        тест упадёт, и легенда не разъедется с ответами молча.
        """
        import ast
        import inspect

        from rlm_tools_bsl.bsl_helpers import make_bsl_helpers

        tree = ast.parse(inspect.getsource(make_bsl_helpers).lstrip())
        values = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_INDEX_COVERAGE_VALUES" for t in node.targets
            ):
                values = [e.value for e in node.value.elts]
        assert values, "константа _INDEX_COVERAGE_VALUES не найдена в фабрике"
        text = self._text()
        for value in values:
            assert value in text, f"легенда не называет значение index_coverage={value!r}"

    def test_legend_and_the_contract_doc_agree_on_not_used(self):
        """Смысловой разъезд легенды и `docs/HELPERS.md` — то, чего AST-тест НЕ ловит.

        Он сверяет только НАЗВАНИЯ значений, поэтому доку и легенду можно было
        развести по смыслу молча: в доке `not_used` был подписан как «live-маршрут»,
        а это неверно — без ридера штатно получается `source='unavailable'` ВМЕСТЕ с
        `index_coverage='not_used'` (проверено исполнением, см. класс ниже по файлу).
        Тест держит оба текста на одном утверждении.
        """
        from pathlib import Path

        doc = Path(__file__).resolve().parents[1] / "docs" / "HELPERS.md"
        doc_text = doc.read_text(encoding="utf-8")
        legend = self._text()
        assert "`not_used` (live-маршрут)" not in doc_text, "дока снова подписала not_used как live-маршрут"
        for text, where in ((legend, "легенда"), (doc_text, "docs/HELPERS.md")):
            near = text[text.index("not_used") : text.index("not_used") + 400]
            assert "source" in near, f"{where}: рядом с not_used не сказано, где читать фактический маршрут"

    def test_truncated_is_not_tied_to_a_total_that_may_not_exist(self):
        """`git_search` намеренно НЕ отдаёт `total`: при упоре в лимит итог неизвестен.

        Значит определять `truncated` через «неполна относительно total» нельзя —
        достижимый случай (`max_results=1`, либо 51+ совпадение в одном файле) даёт
        `truncated=True` вообще без `total`. Предпосылка проверяется у самого
        контракта хелпера, а не переписывается в тест.
        """
        from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

        # Предпосылка — из ФАКТИЧЕСКОГО agent-facing контракта, а не из копии в тесте:
        # в объявленной форме ответа git_search ключа total нет, а truncated есть.
        sig = build_helper_metadata_snapshot()["git_search"]["sig"]
        shape = sig[sig.index("->") :]
        assert "truncated" in shape and "total" not in shape, shape
        legend = self._text()
        assert "total может и НЕ БЫТЬ" in legend, legend
        assert "git_search" in legend, "легенда не называет хелпер, у которого total нет"

    def test_carries_the_three_anti_rules(self):
        """Ровно те три вывода, на которых агент ошибается чаще всего.

        Каждый из них — не риторика, а поведение, доказанное тестами этого файла:
        source='index' на неполном/чужом домене, partial=False на индексной ветке
        `find_references_to_object`, extensions_included=False при недоказанном
        охвате.
        """
        text = self._text()
        for rule in (
            "source='index' НЕ означает «полный»",
            "partial=False НЕ всегда означает «полный»",
            "extensions_included=False НЕ означает «расширений нет»",
        ):
            assert rule in text, f"в легенде нет анти-правила: {rule}"

    def test_names_every_axis_and_calls_scope_legacy(self):
        text = self._text()
        for axis in ("source", "owner", "extensions_included", "total_exact", "partial", "index_coverage"):
            assert axis in text, axis
        assert "truncated" in text and "has_more" in text
        assert "legacy" in text and "scope" in text

    @pytest.mark.strategy_mode_slim
    def test_slim_points_to_the_section_but_does_not_inline_it(self, cf):
        """Discoverability против бюджета: в старте — только имя секции.

        Фикс, о котором агент не узнал, — мёртвый фикс, поэтому указатель в slim
        обязателен. Но текст легенды в стартовый payload не попадает: он длиннее
        всего запаса slim-гарда, и место ему — в on-demand секции.
        """
        from rlm_tools_bsl.bsl_knowledge import get_strategy
        from rlm_tools_bsl.format_detector import detect_format

        strategy = get_strategy("high", detect_format(str(cf)), registry={}, idx_stats=None, query="")
        assert "'coverage')" in strategy, "slim не упоминает секцию"
        assert "== COVERAGE: как читать" not in strategy, "полная легенда инлайнится в стартовую стратегию"

    def test_full_mode_does_not_send_the_agent_to_a_tool_it_has_no_access_to(self, cf, monkeypatch):
        """`rlm_help` РЕГИСТРИРУЕТСЯ ТОЛЬКО В SLIM (server.py, gate по режиму).

        Значит указатель `rlm_help(section='coverage')` в full-стратегии был бы
        мёртвым: агент прочитал бы совет, которого не может выполнить. В full режим
        сам себя описывает целиком, поэтому выжимка ИНЛАЙНИТСЯ — и проверяется
        именно она, а не наличие строки-указателя.

        Пре-существующее ограничение full (НЕ трогаем в этом релизе): в шапке
        остаётся `rlm_help(topic='проведение')` — такой же недостижимый указатель,
        появившийся задолго до v1.34.0. Тест фиксирует, что РЕЛИЗ новых не добавил.
        """
        from rlm_tools_bsl.bsl_knowledge import get_strategy
        from rlm_tools_bsl.format_detector import detect_format

        monkeypatch.setenv("RLM_STRATEGY_MODE", "full")
        strategy = get_strategy("high", detect_format(str(cf)), registry={}, idx_stats=None, query="")
        assert "rlm_help(section=" not in strategy, "full ведёт к незарегистрированному инструменту"
        assert "== COVERAGE (кратко) ==" in strategy, "в full выжимка обязана быть ВСТРОЕННОЙ"
        for rule in ("НЕ означает «полный»", "extensions_included=False НЕ означает"):
            assert rule in strategy, rule

    def test_help_tool_is_registered_only_in_slim(self):
        """Предпосылка предыдущего теста, проверенная ИСПОЛНЕНИЕМ, а не по комментарию.

        Гейт стоит на import-time (`if get_strategy_mode() == "slim"`), поэтому режим
        проверяется в ОТДЕЛЬНЫХ процессах: monkeypatch внутри уже импортированного
        модуля его не переключит.
        """
        import subprocess
        import sys

        # Спрашиваем РЕЕСТР ИНСТРУМЕНТОВ FastMCP, а не имя в модуле: агент видит именно
        # его. Атрибут модуля сверяется рядом — если API реестра сменится, разъезд
        # двух признаков уронит тест, а не пройдёт молча.
        probe = (
            "import rlm_tools_bsl.server as s;"
            "tools = getattr(getattr(s.mcp, '_tool_manager', None), '_tools', None);"
            "assert isinstance(tools, dict), 'реестр инструментов FastMCP недоступен';"
            "reg = 'rlm_help' in tools;"
            "assert reg == hasattr(s, 'rlm_help'), 'реестр и модуль разошлись';"
            "print(reg)"
        )
        out = {}
        for mode in ("slim", "full"):
            env = dict(os.environ, RLM_STRATEGY_MODE=mode)
            res = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=180)
            assert res.returncode == 0, res.stderr[-400:]
            out[mode] = res.stdout.strip()
        assert out["slim"] == "True", out
        assert out["full"] == "False", out


# ---------------------------------------------------------------------------
# Точечный e2e-прогон 2026-09-02 (tmp/e2e/1.34.0/01_SINGLE_PASS_PROTOCOL.md) —
# три находки на боевых конфигурациях, каждая подтверждена исполнением
# ---------------------------------------------------------------------------
class TestSaturationNudgeMirrorsTheGuard:
    """Нудж усечения обязан переживать тот же вход, что и гард хелпера.

    `_note_saturation` объявляет себя зеркалом `_coerce_bound` («Зеркалим…»), но
    `inf`-ветку, добавленную релизом в гард, не получил: `int(float('inf'))` бросал
    `OverflowError` — и падал ПОСЛЕ уже успешно выполненного хелпера, унося весь
    `rlm_execute` вместе с готовым ответом. Тесты релиза этого не ловили: они гоняют
    `limit=inf` через ПРЯМУЮ фабрику, где `Sandbox` (и нудж) не участвует.
    """

    @staticmethod
    def _sandbox(root):
        from rlm_tools_bsl.sandbox import Sandbox

        return Sandbox(base_path=str(root), format_info=detect_format(str(root)))

    @pytest.fixture
    def many_modules(self, tmp_path):
        root = tmp_path / "cf"
        _write(root / "Configuration.xml", _CF_DESCRIPTOR)
        for i in range(60):  # > дефолта 50 у find_module/find_by_type
            _write(
                root / "CommonModules" / f"Мод{i:02d}" / "Ext" / "Module.bsl",
                f"Процедура Ц{i}() Экспорт\nКонецПроцедуры\n",
            )
        return root

    @pytest.mark.parametrize("call", ["find_module('Мод', limit={v})", "find_by_type('CommonModules', limit={v})"])
    # `10**400` — не педантизм: `math.isinf` на БОЛЬШОМ int сам бросает OverflowError,
    # поэтому наивное зеркало (`math.isinf(limit)` без проверки типа) меняет одно падение
    # на другое — причём на входе, который до правки отрабатывал штатно.
    @pytest.mark.parametrize("v", ["float('inf')", "float('-inf')", "10**400", "-10**400"])
    def test_extreme_numeric_limit_does_not_kill_the_execute(self, many_modules, call, v):
        res = self._sandbox(many_modules).execute("r = " + call.format(v=v))
        assert res.error is None, res.error

    def test_the_nudge_still_fires_and_uses_the_effective_limit(self, many_modules):
        """Обратная сторона: сигнал не потерян, и он про ФАКТИЧЕСКИЙ предел.

        При `limit=inf` хелпер ограничился дефолтом 50 (`_coerce_bound`), значит 60
        модулей дают ровно 50 строк — усечение реально и обязано быть названо.
        """
        sb = self._sandbox(many_modules)
        inf_run = sb.execute("r = find_module('Мод', limit=float('inf'))")
        assert inf_run.error is None
        assert any(h["id"] == "list_truncated:find_module" for h in (inf_run.efficiency_hints or []))
        # А запас выше фактического числа строк нуджа не даёт — ложного позитива нет.
        wide = self._sandbox(many_modules).execute("r = find_module('Мод', limit=1000)")
        assert wide.error is None
        assert not [h for h in (wide.efficiency_hints or []) if h["id"].startswith("list_truncated")]


def test_functional_options_hint_does_not_deny_a_complete_answer(cf):
    """`_meta` публикуется на ЛЮБОМ ответе, где code-скан выполнялся, а `_fo_hint()`
    не имел ветки «причин нет» и утверждал «code-скан неполон (охват неполон)» —
    прямо против соседних осей ТОГО ЖЕ `_meta`."""
    bsl = _make_bsl(cf, private_io=True)
    res = bsl["find_functional_options"]("ТестДок")
    meta = res["_meta"]
    # предпосылка: ответ действительно полон
    assert meta["reason"] is None and "partial" not in res, meta
    assert meta["failed_files"] == 0 and meta["catalog_complete"] and meta["read_status_complete"], meta
    assert "неполон" not in meta["hint"], meta["hint"]
    assert meta["code_scope"] in meta["hint"], meta["hint"]


class TestExtPrefilterCachesItsResult:
    """Префильтр ext-модулей обязан платить чтение ОДИН раз за сессию.

    Тела уезжают в общий LRU `_EXT_FILE_CACHE_MAX`; на боевой конфигурации (155 CFE,
    1610 модулей, 29 МБ) кеш вытеснялся полностью, и КАЖДЫЙ `find_definition` без hint
    заново читал весь набор — 9.6–10.5 с на вызов, а батч выбивал дедлайн воркера.
    """

    @staticmethod
    def _lru_cap() -> int:
        import pathlib as _pl
        import re as _re

        src = _pl.Path(__file__).resolve().parents[1] / "src" / "rlm_tools_bsl" / "bsl_helpers.py"
        m = _re.search(r"_EXT_FILE_CACHE_MAX\s*=\s*(\d+)", src.read_text(encoding="utf-8"))
        assert m, "константа ёмкости LRU исчезла — тест перестал быть осмысленным"
        return int(m.group(1))

    @pytest.fixture
    def main_with_big_ext(self, tmp_path):
        # Кандидатов СТРОГО больше ёмкости LRU: иначе тест вакуумен — тела поместились бы
        # в кеш и повторное чтение не воспроизвелось бы даже на дефектной версии.
        n = self._lru_cap() + 5
        base = tmp_path / "src" / "cf"
        ext = tmp_path / "src" / "cfe" / "Расш"
        _write(base / "Configuration.xml", _CF_DESCRIPTOR)
        _write(base / "CommonModules" / "Осн" / "Ext" / "Module.bsl", "Процедура Цель() Экспорт\nКонецПроцедуры\n")
        _write(ext / "Configuration.xml", _CFE_DESCRIPTOR)
        for i in range(n):
            _write(ext / "CommonModules" / f"М{i:04d}" / "Ext" / "Module.bsl", f"Процедура В{i}()\nКонецПроцедуры\n")
        _write(ext / "CommonModules" / "Носитель" / "Ext" / "Module.bsl", "Процедура Цель(А) Экспорт\nКонецПроцедуры\n")
        _write(
            ext / "CommonModules" / "Обманка" / "Ext" / "Module.bsl",
            "// Процедура Цель() Экспорт\nПроцедура Другая()\nКонецПроцедуры\n",
        )
        # МЕЖСТРОЧНАЯ ловушка: `BSL_DECL_PREFIX_RE` кончается на `\s+`, а `\s` включает
        # перевод строки. Регекс, запущенный по СКЛЕЕННОМУ тексту, начинает совпадение
        # на строке с одним ключевым словом, перескакивает границу и принимает за имя
        # первое слово СЛЕДУЮЩЕЙ строки — настоящее объявление этой строки теряется
        # молча, хотя полный (построчный) разбор его находит.
        _write(
            ext / "CommonModules" / "Межстрочный" / "Ext" / "Module.bsl",
            "Процедура\nФункция ЦельМежстрочная() Экспорт\nКонецФункции\n",
        )
        return base, ext, n

    class _Reader(_DummyReader):
        def get_build_capabilities(self):
            return _caps()

        def get_definitions(self, *a, **kw):
            return {"rows": [], "total": 0, "truncated": False, "slow_fallback": False}

    def _bsl(self, base, ext):
        return _make_bsl(
            base,
            extension_paths=[str(ext)],
            idx_reader=self._Reader(),
            current_config_role="main",
            current_config_name="Тест",
            current_config_root=str(base),
        )

    def test_second_call_reads_nothing_from_disk(self, main_with_big_ext, monkeypatch):
        import pathlib as _pl

        base, ext, n = main_with_big_ext
        bsl = self._bsl(base, ext)
        reads = {"n": 0}
        real = _pl.Path.read_text

        def counting(self, *a, **kw):
            if str(self).endswith(".bsl"):
                reads["n"] += 1
            return real(self, *a, **kw)

        monkeypatch.setattr(_pl.Path, "read_text", counting)
        bsl["find_definition"]("Цель")
        first = reads["n"]
        reads["n"] = 0
        bsl["find_definition"]("ДругоеИмя")
        assert first >= n, f"первый вызов обязан прочитать набор целиком: {first} < {n}"
        assert reads["n"] == 0, f"повторный вызов снова читает {reads['n']} модулей — кеш результата не работает"

    def test_recall_is_unchanged_by_the_prefilter(self, main_with_big_ext):
        """Префильтр по ИМЕНАМ ОБЪЯВЛЕНИЙ остаётся над-аппроксимацией: реальное
        объявление найдено, а имя из комментария в выдачу не попало."""
        base, ext, _n = main_with_big_ext
        rows = self._bsl(base, ext)["find_definition"]("Цель")["definitions"]
        files = sorted(r["file"].replace("\\", "/") for r in rows)
        assert any("Носитель" in f for f in files), files
        assert not any("Обманка" in f for f in files), files

    def test_declaration_after_a_bare_keyword_line_is_not_swallowed(self, main_with_big_ext):
        r"""Совпадение префильтра не имеет права перескочить границу строки.

        `\s+` в грамматике объявления включает перевод строки: по склеенному тексту
        строка с одним `Процедура` перед `Функция Цель()` дала бы имя `Функция`, а
        настоящее `Цель` в набор не попадало — `find_definition` молча терял бы
        строку, которую полный разбор находит.
        """
        base, ext, _n = main_with_big_ext
        bsl = self._bsl(base, ext)
        res = bsl["find_definition"]("ЦельМежстрочная")
        files = [r["file"].replace("\\", "/") for r in res["definitions"]]
        assert res["total"] == 1, (res["total"], files)
        assert any("Межстрочный" in f for f in files), files
        # И подменённое имя в набор не попало: по нему не находится ничего.
        assert bsl["find_definition"]("Функция")["total"] == 0
