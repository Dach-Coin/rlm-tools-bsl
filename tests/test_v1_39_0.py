"""v1.39.0 — «страница объявлена у обоих соседей, знак равенства значит одно».

Релиз read-time: схема SQLite и ``BUILDER_VERSION`` не тронуты, пересборка не нужна.
Три задачи — единый смысл ``=`` в подписи ``get_overrides``, страница и признак
усечения у ``find_common_modules``, арг-гард ``limit`` у ``find_templates``.

Фикстуры здесь СВОИ и намеренно локальные: приватные ``_props_fixture`` /
``_helpers_for`` из ``test_v1_38_0.py`` не импортируются (каталог ``tests`` не
package), а выносить их в ``conftest.py`` ради двенадцати точечных тестов значит
рефакторить старый тестовый контур с риском, несоразмерным задаче.
"""

from __future__ import annotations

import inspect
import math
import re

import pytest

from rlm_tools_bsl.bsl_helpers import make_bsl_helpers
from rlm_tools_bsl.bsl_index import IndexBuilder, IndexReader
from rlm_tools_bsl.format_detector import detect_format
from rlm_tools_bsl.helpers import make_helpers

CF_DESCRIPTOR = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">\n'
    '  <Configuration uuid="00000000-0000-0000-0000-000000000001">\n'
    "    <Properties><Name>Тест</Name></Properties>\n"
    "  </Configuration>\n"
    "</MetaDataObject>\n"
)

# Имена НАМЕРЕННО без `_` и `%`: это граница обещания о совпадении страниц
# (индексная ветка фильтрует через LIKE без ESCAPE, живая — питоновской
# подстрокой). `_` в имени покрасил бы тест по ДРУГОЙ, пре-существующей причине,
# которую этот релиз не чинит.
_CM_NAMES = ("alpha", "Zeta", "аБВ", "АбЯ", "Альфа")


def _cf_common_module(name: str, *, privileged: bool = False) -> str:
    return (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" '
        'xmlns:v8="http://v8.1c.ru/8.1/data/core"><CommonModule><Properties>'
        f"<Name>{name}</Name><Global>false</Global><Server>true</Server>"
        "<ServerCall>false</ServerCall>"
        f"<Privileged>{'true' if privileged else 'false'}</Privileged>"
        "<ExternalConnection>false</ExternalConnection>"
        "<ClientManagedApplication>false</ClientManagedApplication>"
        "<ClientOrdinaryApplication>false</ClientOrdinaryApplication>"
        "<ReturnValuesReuse>DontUse</ReturnValuesReuse>"
        "</Properties></CommonModule></MetaDataObject>"
    )


def _cf_template(name: str) -> str:
    return (
        '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
        f"<Template><Properties><Name>{name}</Name>"
        "<TemplateType>SpreadsheetDocument</TemplateType></Properties></Template></MetaDataObject>"
    )


def _seed(tmp_path, *, common_modules=(), templates=()):
    """Минимальное дерево CF: якорный .bsl + описатели общих модулей и макетов.

    Якорь обязателен: на дереве без единого модуля сборка идёт путём пустого
    репозитория и метаданные не собирает вовсе — фикстура была бы вакуумной.
    """
    anchor = tmp_path / "CommonModules" / "Якорь" / "Ext"
    anchor.mkdir(parents=True)
    (anchor / "Module.bsl").write_text("Процедура Я() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    cm_dir = tmp_path / "CommonModules"
    for name in common_modules:
        (cm_dir / f"{name}.xml").write_text(_cf_common_module(name), encoding="utf-8")
    if templates:
        doc = tmp_path / "Documents" / "Заказ" / "Ext"
        doc.mkdir(parents=True)
        (doc / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
        tdir = tmp_path / "Documents" / "Заказ" / "Templates"
        tdir.mkdir(parents=True)
        for name in templates:
            (tdir / f"{name}.xml").write_text(_cf_template(name), encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    return tmp_path


def _bsl_for(base_path, idx_reader=None):
    helpers, resolve_safe = make_helpers(str(base_path))
    return make_bsl_helpers(
        base_path=str(base_path),
        resolve_safe=resolve_safe,
        read_file_fn=helpers["read_file"],
        grep_fn=helpers["grep"],
        glob_files_fn=helpers["glob_files"],
        format_info=detect_format(str(base_path)),
        idx_reader=idx_reader,
    )


def _helpers_for(tmp_path, monkeypatch, *, with_index: bool):
    monkeypatch.setenv("RLM_INDEX_DIR", str(tmp_path / ".idx"))
    reader = None
    if with_index:
        db = IndexBuilder().build(str(tmp_path), build_calls=False, build_metadata=True)
        reader = IndexReader(db)
    return _bsl_for(tmp_path, reader), reader


# Входы арг-гарда: конвенция v1.30.0 обязана восстановить документированный
# дефолт на КАЖДОМ, не уронив вызов.
#
# `-0.5` стоит отдельным входом, а не «ещё одним отрицательным»: у него дробная
# часть, а `int()` усекает К НУЛЮ, поэтому проверка минимума ПОСЛЕ усечения его не
# видела вовсе — `-0.5` превращался в валидный 0, ридер получал limit=1, наружу
# уходила ПУСТАЯ страница, и предупреждения в логе не было тоже. Целое `-1` этот
# случай не покрывает: оно отсекается и старым порядком проверок.
_BAD_LIMITS = [None, "abc", [1], -1, -0.5, True, math.nan, math.inf]
_BAD_LIMIT_IDS = ["none", "str", "list", "negative", "negative_fraction", "bool", "nan", "inf"]


# ───────────────── Задача 1: один знак равенства = один смысл ─────────────────


def test_get_overrides_sig_uses_one_meaning_for_the_equals_sign():
    """В подписи `=` обязан означать ОДНО — что лежит в значении.

    До релиза `by_*` объявляли ТИП значения (`dict{имя:N}`), а `unique_*` —
    ЕДИНИЦУ счёта (`ИМЕНА`/`ПАРЫ`). Единообразное прочтение подписи давало
    `len()` на `int` — ровно та потеря вызова, которую поймал приёмо-сдаточный
    e2e v1.38.0. Плюс ключ `unique_extensions` ответ нёс, а подпись умалчивала.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    sig = build_helper_metadata_snapshot()["get_overrides"]["sig"]
    assert "unique_methods=ИМЕНА" not in sig, sig
    assert "unique_object_methods=ПАРЫ" not in sig, sig
    for key in ("unique_objects", "unique_methods", "unique_object_methods", "unique_extensions"):
        assert key in sig, f"{key} не назван в подписи: {sig}"


def test_every_unique_key_named_in_the_sig_is_a_number(tmp_path, monkeypatch):
    """Привязка подписи к ФАКТУ: имена берутся ИЗ САМОЙ подписи.

    Тест, написанный от списка литералов, разъехался бы с подписью ровно так же,
    как разъехалась она сама.
    """
    from rlm_tools_bsl.bsl_helpers import build_helper_metadata_snapshot

    sig = build_helper_metadata_snapshot()["get_overrides"]["sig"]
    keys = sorted(set(re.findall(r"unique_[a-z_]+", sig)))
    assert keys, sig
    _seed(tmp_path, common_modules=("alpha",))
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        payload = bsl["get_overrides"]()
        for key in keys:
            assert key in payload, f"{key} назван подписью, но ответа не несёт"
            assert isinstance(payload[key], int) and not isinstance(payload[key], bool), (
                f"{key} = {payload[key]!r}: подпись обещает ЧИСЛО, len() по нему упал бы"
            )
    finally:
        reader.close()


# ──────────── Задача 2: find_common_modules — страница и усечение ────────────


def test_find_common_modules_pages_like_find_templates(tmp_path, monkeypatch):
    """Страница + ПОЛНЫЙ счёт, как у соседа из того же релиза.

    До v1.39.0 `LIMIT` не ставился вовсе: `flag='DontUse'` отдавал 3 835 строк
    одним ответом на 1.16 МБ сериализации.
    """
    _seed(tmp_path, common_modules=_CM_NAMES)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        page = bsl["find_common_modules"](flag="DontUse", limit=2)
        assert len(page["modules"]) == 2, page
        assert page["total"] == 5, page
        assert page["truncated"] is True, page
        assert page["source"] == "index" and page["partial"] is False, page
        # Ровно `limit` совпадений усечением НЕ объявляются.
        exact = bsl["find_common_modules"](flag="DontUse", limit=5)
        assert exact["total"] == 5 and exact["truncated"] is False, exact
        # Счёт идёт ТЕМ ЖЕ фильтром, что и выдача.
        one = bsl["find_common_modules"](name="Zeta")
        assert one["total"] == 1 and one["truncated"] is False, one
    finally:
        reader.close()


@pytest.mark.parametrize("with_index", [True, False], ids=["index", "live"])
def test_common_modules_live_page_matches_index_page(tmp_path, monkeypatch, with_index):
    """СОСТАВ страницы обязан совпасть у обеих веток.

    `sorted(Path)` на Windows сравнивает приведённые к нижнему регистру строки, а
    SQLite `ORDER BY module_name` — байты. Пока `limit` не было, порядок был
    ненаблюдаем; со страницей он решает, какие именно две строки увидит агент, —
    и одна конфигурация до и после сборки индекса отвечала бы РАЗНЫМИ двумя.
    """
    _seed(tmp_path, common_modules=_CM_NAMES)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=with_index)
    try:
        res = bsl["find_common_modules"](flag="DontUse", limit=2)
        assert res["source"] == ("index" if with_index else "live"), res
        assert res["total"] == 5, res
        assert [m["module_name"] for m in res["modules"]] == sorted(_CM_NAMES)[:2], res
        assert res["truncated"] is True, res
    finally:
        if reader is not None:
            reader.close()


@pytest.mark.parametrize("bad", _BAD_LIMITS, ids=_BAD_LIMIT_IDS)
def test_find_common_modules_limit_restores_documented_default(tmp_path, monkeypatch, bad):
    """Конвенция v1.30.0: некорректный `limit` не роняет вызов, а даёт дефолт 200.

    Точный дефолт доказывается НЕ маленькой фикстурой (где 5 строк одинаково
    выглядят и при `limit=200`, и при «без ограничения»), а spy-ридером: каждому
    случаю передан именно `limit=200`.
    """
    _seed(tmp_path, common_modules=_CM_NAMES)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        seen: list[int] = []
        real = reader.get_common_module_props

        def spy(name: str = "", flag: str = "", limit: int = 200):
            seen.append(limit)
            return real(name=name, flag=flag, limit=limit)

        monkeypatch.setattr(reader, "get_common_module_props", spy, raising=False)
        res = bsl["find_common_modules"](flag="DontUse", limit=bad)
        assert seen == [200], f"{bad!r} -> ридеру ушло {seen}"
        assert len(res["modules"]) == 5 and res["total"] == 5, res
        assert res["truncated"] is False, res
    finally:
        reader.close()


def test_common_modules_reader_without_limit_still_pages(tmp_path, monkeypatch):
    """Ридер СТАРОЙ сигнатуры сохраняет индексный маршрут, а страницу режет хелпер.

    Без этой ветки такой адаптер молча проваливался бы в живой XML-скан — чинили
    бы страницу ценой источника.
    """
    _seed(tmp_path, common_modules=_CM_NAMES)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        full = reader.get_common_module_props(flag="DontUse", limit=99)["modules"]
        assert len(full) == 5, full

        def legacy(name: str = "", flag: str = ""):
            return list(full)

        monkeypatch.setattr(reader, "get_common_module_props", legacy, raising=False)
        res = bsl["find_common_modules"](flag="DontUse", limit=2)
        assert res["source"] == "index", res
        assert len(res["modules"]) == 2, res
        # Голый список полон по построению — значит `total` здесь ТОЧЕН.
        assert res["total"] == 5 and res["truncated"] is True, res
    finally:
        reader.close()


@pytest.mark.parametrize("exc", [ValueError, TypeError], ids=["value_error", "type_error"])
def test_opaque_reader_still_gets_the_requested_limit(tmp_path, monkeypatch, exc):
    """Непрозрачный для интроспекции ридер, который `limit` ПРИНИМАЕТ.

    Стадии разведены намеренно: `TypeError` от `signature()` означает «объект
    непрозрачен», а от `bind()` — «параметра нет». Один `except TypeError` на оба
    вопроса увёл бы первый случай в `legacy`, и ридер, принимающий `limit`, получил
    бы вызов без него — то есть запрошенный `limit` был бы проигнорирован, а ответ
    выдан за полный. Какое из двух исключений выберет конкретная версия Python,
    тест решать не должен, поэтому параметризованы ОБА.

    Ридер намеренно возвращает на строку больше запрошенного: публичная граница
    обязана срезать её всё равно.
    """
    _seed(tmp_path, common_modules=_CM_NAMES)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        real = reader.get_common_module_props
        seen: list[int] = []

        def opaque(name: str = "", flag: str = "", limit: int = 200):
            seen.append(limit)
            page = real(name=name, flag=flag, limit=limit + 1)
            return page

        monkeypatch.setattr(reader, "get_common_module_props", opaque, raising=False)
        real_signature = inspect.signature

        def blind(obj, *a, **kw):
            if obj is reader.get_common_module_props:
                raise exc("сигнатура недоступна")
            return real_signature(obj, *a, **kw)

        monkeypatch.setattr(inspect, "signature", blind)
        res = bsl["find_common_modules"](flag="DontUse", limit=2)
        assert seen == [2], f"вызов ушёл БЕЗ limit: {seen}"
        assert len(res["modules"]) == 2, res
        assert res["source"] == "index" and res["total"] == 5, res
    finally:
        reader.close()


def test_internal_type_error_calls_the_reader_exactly_once(tmp_path, monkeypatch):
    """Гард против соблазна вернуть повтор по пойманному исключению.

    Ридер, который `limit` ПРИНИМАЕТ и бросил разовый внутренний `TypeError`,
    обязан дать ОДИН вызов и живой скан. С повтором он дал бы ДВА обращения к
    чужому объекту И другой результат (`source='index'`).
    """
    _seed(tmp_path, common_modules=_CM_NAMES)
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        calls: list[int] = []

        def boom(name: str = "", flag: str = "", limit: int = 200):
            calls.append(limit)
            raise TypeError("внутренний отказ ридера, а не отсутствие параметра")

        monkeypatch.setattr(reader, "get_common_module_props", boom, raising=False)
        res = bsl["find_common_modules"](flag="DontUse", limit=2)
        assert len(calls) == 1, calls
        assert res["source"] == "live" and res["partial"] is True, res
        assert res["total"] == 5 and len(res["modules"]) == 2, res
    finally:
        reader.close()


def test_live_branch_without_common_modules_keeps_every_promised_key(tmp_path, monkeypatch):
    """Гард раннего выхода: ключ `truncated` обещан подписью — значит он есть ВСЕГДА.

    Поддерживаемая конфигурация без каталога `CommonModules` и без индекса
    возвращает `out` ДО всякой нарезки. Если дописывать ключ по месту вычисления,
    агент получил бы `KeyError` там, где ответ честно пуст.
    """
    doc = tmp_path / "Documents" / "Заказ" / "Ext"
    doc.mkdir(parents=True)
    (doc / "ObjectModule.bsl").write_text("Процедура П() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (tmp_path / "Configuration.xml").write_text(CF_DESCRIPTOR, encoding="utf-8")
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=False)
    assert reader is None
    res = bsl["find_common_modules"]()
    assert res["truncated"] is False, res
    assert res["total"] == 0 and res["modules"] == [], res
    assert res["source"] == "live" and res["partial"] is True, res


# ─────────────── Задача 3: find_templates — арг-гард `limit` ───────────────


@pytest.mark.parametrize("bad", _BAD_LIMITS, ids=_BAD_LIMIT_IDS)
def test_find_templates_limit_restores_documented_default(tmp_path, monkeypatch, bad):
    """Прежний `max(1, int(limit))` не исполнял конвенцию НИ НА ОДНОМ из этих входов.

    Пять из них (`None`, строка, список, `NaN`, `inf`) РОНЯЛИ весь `rlm_execute`
    вместе с уже выполненной частью батча, а `-1`, `True` и `-0.5` молча отдавали
    одну строку вместо документированного дефолта 200.
    """
    _seed(tmp_path, templates=[f"Макет{i}" for i in range(5)])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        seen: list[int] = []
        real = reader.get_templates

        def spy(owner: str = "", name: str = "", template_type: str = "", limit: int = 200):
            seen.append(limit)
            return real(owner=owner, name=name, template_type=template_type, limit=limit)

        monkeypatch.setattr(reader, "get_templates", spy, raising=False)
        res = bsl["find_templates"](limit=bad)
        assert seen == [200], f"{bad!r} -> ридеру ушло {seen}"
        assert len(res["templates"]) == 5 and res["total"] == 5, res
        assert res["truncated"] is False, res
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("helper", "key", "kwargs"),
    [
        ("find_common_modules", "modules", {"flag": "DontUse"}),
        ("find_templates", "templates", {}),
    ],
    ids=["common_modules", "templates"],
)
def test_zero_limit_keeps_the_full_total(tmp_path, monkeypatch, helper, key, kwargs):
    """Гард блокера 3.1.1: `LIMIT 0` НЕ имеет права обнулить оконный счётчик.

    `COUNT(*) OVER ()` приезжает КОЛОНКОЙ строки: нет строк — нет и счёта, а
    формула `rows[0]['match_total'] if rows else 0` превращает это в честный на вид
    ноль. Ноль совпадений и ноль запрошенных строк — РАЗНЫЕ ответы.

    Ридер проверяется НАПРЯМУЮ: хелперный срез замаскировал бы забытый `[:limit]`
    внутри ридера.
    """
    _seed(tmp_path, common_modules=_CM_NAMES, templates=[f"Макет{i}" for i in range(5)])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        res = bsl[helper](limit=0, **kwargs)
        assert res[key] == [], res
        assert res["total"] == 5, res
        assert res["truncated"] is True, res
        direct = (
            reader.get_common_module_props(flag="DontUse", limit=0)
            if helper == "find_common_modules"
            else reader.get_templates(limit=0)
        )
        assert direct[key] == [], direct
        assert direct["total"] == 5, direct
    finally:
        reader.close()


@pytest.mark.parametrize("legacy_adapter", [False, True], ids=["reader", "list_adapter"])
def test_zero_limit_returns_empty_page(tmp_path, monkeypatch, legacy_adapter):
    """`limit=0` отдаёт РОВНО ноль строк у обоих хелперов.

    Сегодня `find_templates` отдаёт ОДНУ: `max(1, int(limit))` не пускает ноль в
    SQL. Вторая половина — поддерживаемый list-адаптер, который аргумент
    игнорирует и возвращает непустой список: публичный хелпер обязан срезать его
    до нуля сам, а не полагаться на чужую дисциплину.
    """
    _seed(tmp_path, common_modules=_CM_NAMES, templates=[f"Макет{i}" for i in range(5)])
    bsl, reader = _helpers_for(tmp_path, monkeypatch, with_index=True)
    try:
        if legacy_adapter:
            cm_rows = reader.get_common_module_props(flag="DontUse", limit=99)["modules"]
            tpl_rows = reader.get_templates(limit=99)["templates"]
            monkeypatch.setattr(
                reader, "get_common_module_props", lambda name="", flag="": list(cm_rows), raising=False
            )
            monkeypatch.setattr(reader, "get_templates", lambda **_kw: list(tpl_rows), raising=False)
        assert bsl["find_common_modules"](flag="DontUse", limit=0)["modules"] == []
        assert bsl["find_templates"](limit=0)["templates"] == []
    finally:
        reader.close()
