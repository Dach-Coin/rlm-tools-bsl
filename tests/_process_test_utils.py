"""Общие утилиты process-mode тестов v1.29.0 (spawn-safe, без monkeypatch-магии)."""

import os
import time

CM_BSL = """\
Процедура ЗаполнитьТабличнуюЧасть(ДокументОбъект, ИмяТабличнойЧасти) Экспорт
    ВычислитьИтоги(ДокументОбъект[ИмяТабличнойЧасти]);
КонецПроцедуры

Функция ПолучитьДатуСеанса() Экспорт
    Возврат ТекущаяДатаСеанса();
КонецФункции

Процедура ВычислитьИтоги(ТаблицаЗначений)
    Результат = Новый Массив;
КонецПроцедуры
"""

DOC_BSL = """\
Процедура ОбработкаЗаполнения(ДанныеЗаполнения, СтандартнаяОбработка) Экспорт
    МойМодуль.ЗаполнитьТабличнуюЧасть(ЭтотОбъект, "Товары");
КонецПроцедуры
"""


def make_cf_project(root) -> str:
    """Маленький детерминированный CF-проект, пригодный для spawn на Win/Linux."""
    cm = root / "CommonModules" / "МойМодуль" / "Ext"
    cm.mkdir(parents=True, exist_ok=True)
    (cm / "Module.bsl").write_text(CM_BSL, encoding="utf-8-sig")
    doc = root / "Documents" / "ТестовыйДокумент" / "Ext"
    doc.mkdir(parents=True, exist_ok=True)
    (doc / "ObjectModule.bsl").write_text(DOC_BSL, encoding="utf-8-sig")
    (root / "Configuration.xml").write_text("<Configuration/>", encoding="utf-8")
    return str(root)


def pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def wait_until(predicate, timeout=10.0, interval=0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
