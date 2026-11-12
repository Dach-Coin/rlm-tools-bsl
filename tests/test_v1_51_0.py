"""v1.51.0 — холодная сборка без простоя; ответ о том объекте, о котором спросили.

Разделы: прогрев на окно вперёд (``_WarmAhead``) и где он включён; сборщики описаний и порции BSL прогревают свои
файлы; пул в начале сборки, обход BSL — задачей пула; старый файл индекса — чтением подряд; без фонового прогрева
координатора; опись снимка после пула; строка роли ``find_roles`` — член запрошенного объекта (issue #45); голое имя у
четырёх хелперов одной категории (issue #46).
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

import pytest

import rlm_tools_bsl.bsl_index as BI
from rlm_tools_bsl import _index_workers as IW

_WAIT = 30.0  # предохранитель ожиданий на событиях: сломанный код не вешает прогон


def _files(root: Path, count: int, prefix: str = "f") -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(count):
        path = root / f"{prefix}{i:04d}.xml"
        path.write_text("<x/>", encoding="utf-8")
        out.append(path)
    return out


def _warm_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("rlm-warm") and t.is_alive()]


def _failing_warm_start(monkeypatch, exc_type: type[BaseException]) -> list[threading.Thread]:
    """``Thread.start`` потоков ``rlm-warm*``: первый запускается, каждый следующий — ``exc_type`` (отказ ОС «can't
    start new thread» либо прерывание). Возвращает запущенные потоки прогрева."""
    real_start = threading.Thread.start
    started: list[threading.Thread] = []

    def start(thread):
        if thread.name.startswith("rlm-warm"):
            if started:
                raise exc_type("can't start new thread") if exc_type is RuntimeError else exc_type()
            started.append(thread)
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    return started


def _record_warms(monkeypatch) -> list:
    """Все экземпляры ``_WarmAhead`` — чтобы тест снял их потоки сам, если код их бросил (прогон на коде до правки)."""
    owners: list = []
    real_init = BI._WarmAhead.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        owners.append(self)

    monkeypatch.setattr(BI._WarmAhead, "__init__", init)
    return owners


def _stop_warms(owners) -> None:
    for warm in owners:
        with contextlib.suppress(RuntimeError):  # join незапущенного потока у кода до правки
            warm.close()


def _woken_at(i: int, window: int, count: int) -> bool:
    """После ``advance(i)`` прогрев гарантированно разбужен: окно сдвинулось на порцию ``_WARM_WAKE_BATCH`` (позиции
    15, 31, … от начала) либо до конца списка осталось не больше окна."""
    return i % BI._WARM_WAKE_BATCH == BI._WARM_WAKE_BATCH - 1 or i + window >= count - 1


class _OpenSpy:
    """Шпион на ``os.open``/``os.close`` (делегирует настоящим). У потоков прогрева ``rlm-warm*`` записывает каждую
    ПОПЫТКУ открытия (индекс файла в списке — до настоящего ``open``: попытка открыть каталог или отсутствующий файл
    тоже видна) и проверяет окно в момент попытки; дескрипторы прогрева — до закрытия. Открытия других потоков не
    записываются."""

    def __init__(self, monkeypatch, paths, *, position=None, window=None, block=None, fail=()):
        self._open, self._close = os.open, os.close
        self.index = {os.fspath(p): i for i, p in enumerate(paths)}
        self.cond = threading.Condition()
        self.opened: list[int] = []  # попытки открыть, по порядку
        self.foreign: list[str] = []
        self.violations: list[tuple[int, int]] = []
        self.fds: set[int] = set()
        self.position = position  # [int]: позиция потребителя в тесте — ставится ДО advance
        self.window = window
        self.block = block or {}  # индекс -> (entered: Event, release: Event)
        self.fail = set(fail)
        monkeypatch.setattr(os, "open", self._spy_open)
        monkeypatch.setattr(os, "close", self._spy_close)

    @staticmethod
    def _warm_thread() -> bool:
        return threading.current_thread().name.startswith("rlm-warm")

    def _spy_open(self, path, flags, *args, **kwargs):
        if not self._warm_thread():
            return self._open(path, flags, *args, **kwargs)
        idx = self.index.get(os.fspath(path))
        with self.cond:
            if idx is None:
                self.foreign.append(os.fspath(path))
            else:
                if self.position is not None and idx > self.position[0] + self.window:
                    self.violations.append((idx, self.position[0]))
                self.opened.append(idx)
            self.cond.notify_all()
        if idx in self.block:
            entered, release = self.block[idx]
            entered.set()
            release.wait(_WAIT)
        if idx in self.fail:
            raise PermissionError(13, "denied by test", os.fspath(path))
        fd = self._open(path, flags, *args, **kwargs)
        with self.cond:
            self.fds.add(fd)
        return fd

    def _spy_close(self, fd):
        if self._warm_thread():
            with self.cond:
                self.fds.discard(fd)
        return self._close(fd)

    def wait_opened(self, wanted) -> bool:
        wanted = set(wanted)
        with self.cond:
            return self.cond.wait_for(lambda: wanted <= set(self.opened), timeout=_WAIT)


# ═══════════════ Задача 1. Прогрев на окно вперёд ═══════════════


class TestWarmAhead:
    def test_window_bounds_every_open_and_every_file_is_warmed(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "w", 200)
        window = BI._WARM_WINDOW
        pos = [-1]
        spy = _OpenSpy(monkeypatch, files, position=pos, window=window)
        with BI._large_sequential_warm():
            with BI._WarmAhead(files) as warm:
                # Потребитель ещё не начал: прогрев открывает окно и останавливается на его границе.
                assert spy.wait_opened(range(0, window))
                assert max(spy.opened) == window - 1
                for i in range(len(files)):
                    pos[0] = i  # позиция теста — раньше, чем её увидит прогрев
                    warm.advance(i)
                    # Элемент, у которого стоит потребитель, прогрев открывает (он не дальше окна последнего пробуждения).
                    assert spy.wait_opened({i})
                    if _woken_at(i, window, len(files)):
                        # Окно сдвинулось на порцию (или дошло до конца списка) — прогрев разбужен и доходит до границы.
                        assert spy.wait_opened(range(0, min(i + window, len(files) - 1) + 1))
        assert spy.violations == []
        assert sorted(spy.opened) == list(range(len(files)))  # каждый файл — ровно один раз
        assert spy.fds == set()
        assert _warm_threads() == []

    def test_one_thread_warms_in_list_order_and_iteration_keeps_order(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "o", 90)
        spy = _OpenSpy(monkeypatch, files)
        with BI._large_sequential_warm():
            warm = BI._WarmAhead(files, threads=1)
            with warm:
                got = []
                for item in warm:
                    got.append(item)
                    i = len(got) - 1
                    assert spy.wait_opened({i})  # потребитель не обгоняет прогрев — пропусков нет
                    if _woken_at(i, BI._WARM_WINDOW, len(files)):
                        assert spy.wait_opened(range(0, min(i + BI._WARM_WINDOW, len(files) - 1) + 1))
        assert got == files
        # Один поток берёт пути строго по порядку списка.
        assert spy.opened == list(range(len(files)))

    def test_items_the_consumer_passed_are_not_warmed(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "b", 200)
        entered, release = threading.Event(), threading.Event()
        spy = _OpenSpy(monkeypatch, files, block={2: (entered, release)})
        with BI._large_sequential_warm():
            with BI._WarmAhead(files, threads=1) as warm:
                assert entered.wait(_WAIT)  # единственный поток прогрева застрял в open() файла 2
                warm.advance(50)  # потребитель ушёл вперёд сам
                release.set()
                assert spy.wait_opened(range(50, 50 + BI._WARM_WINDOW + 1))
        # Пройденное потребителем не открывается: прогрев продолжает с его позиции.
        assert sorted(spy.opened) == [0, 1, 2, *range(50, 50 + BI._WARM_WINDOW + 1)]

    def test_lazy_paths_of_item_are_all_warmed(self, tmp_path, monkeypatch):
        dirs = []
        paths = []
        for i in range(20):
            d = tmp_path / "items" / f"d{i:02d}"
            d.mkdir(parents=True)
            for name in ("a.xml", "b.xml"):
                (d / name).write_text("<x/>", encoding="utf-8")
                paths.append(d / name)
            dirs.append(d)
        spy = _OpenSpy(monkeypatch, paths)
        with BI._large_sequential_warm():
            with BI._WarmAhead(dirs, lambda d: (d / "a.xml", None, d / "b.xml")) as warm:
                assert spy.wait_opened(range(len(paths)))  # 20 элементов — в окне ещё до начала перебора
                assert list(warm) == dirs
        assert sorted(spy.opened) == list(range(len(paths)))

    def test_directory_and_missing_path_are_skipped(self, tmp_path, monkeypatch):
        f0, f1 = _files(tmp_path / "s", 2)
        folder = tmp_path / "s" / "folder.xml"
        folder.mkdir()
        missing = tmp_path / "s" / "missing.xml"
        spy = _OpenSpy(monkeypatch, [f0, folder, missing, f1])
        with BI._large_sequential_warm():
            with BI._WarmAhead([f0, folder, missing, f1], threads=1) as warm:
                assert spy.wait_opened({0, 3})  # один поток идёт по порядку: каталог и отсутствующий уже пройдены
                assert list(warm) == [f0, folder, missing, f1]
        assert sorted(spy.opened) == [0, 3]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO — только POSIX")
    def test_fifo_is_never_opened(self, tmp_path, monkeypatch):
        f0, f1 = _files(tmp_path / "p", 2)
        fifo = tmp_path / "p" / "stream.xml"
        os.mkfifo(fifo)
        link = tmp_path / "p" / "fifo_link.xml"
        os.symlink(fifo, link)
        spy = _OpenSpy(monkeypatch, [f0, fifo, link, f1])
        try:
            with BI._large_sequential_warm():
                with BI._WarmAhead([f0, fifo, link, f1], threads=1):
                    assert spy.wait_opened({0, 3})
            assert sorted(spy.opened) == [0, 3]
        finally:
            # Сломанный прогрев висел бы в open() на FIFO без писателя: писатель его отпускает.
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def test_consumer_error_stops_warm_and_waits_for_threads(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "e", 100)
        entered, release = threading.Event(), threading.Event()
        spy = _OpenSpy(monkeypatch, files, block={10: (entered, release)})
        with BI._large_sequential_warm():
            with pytest.raises(RuntimeError, match="consumer failed"):
                with BI._WarmAhead(files) as warm:
                    warm.advance(3)
                    assert entered.wait(_WAIT)  # поток прогрева сейчас внутри open() файла 10
                    # Отпустить открытие чуть позже: выход из менеджера обязан его ДОЖДАТЬСЯ, а не бросить поток.
                    threading.Timer(0.2, release.set).start()
                    raise RuntimeError("consumer failed")
        assert release.is_set()
        assert 10 in spy.opened
        assert _warm_threads() == []
        assert spy.fds == set()  # ни один файл прогрева не остался открытым

    def test_outside_worker_without_flag_no_threads_and_no_opens(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "n", 50)
        spy = _OpenSpy(monkeypatch, files)
        assert not IW.in_index_worker()
        with BI._WarmAhead(files) as warm:
            assert _warm_threads() == []
            assert list(warm) == files
        assert spy.opened == []

    def test_worker_and_flag_enable_warm(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "k", 30)
        spy = _OpenSpy(monkeypatch, files)
        monkeypatch.setattr(IW, "_IN_INDEX_WORKER", True)
        with BI._WarmAhead(files) as warm:
            assert spy.wait_opened(range(len(files)))
            list(warm)
        monkeypatch.setattr(IW, "_IN_INDEX_WORKER", False)
        spy.opened.clear()
        with BI._large_sequential_warm():
            with BI._WarmAhead(files) as warm:
                assert spy.wait_opened(range(len(files)))
                list(warm)
        assert _warm_threads() == []

    def test_flag_of_one_thread_is_not_seen_by_another(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "t", 30)
        spy = _OpenSpy(monkeypatch, files)
        seen: dict[str, object] = {}

        def other() -> None:
            with BI._WarmAhead(files) as warm:
                seen["threads"] = _warm_threads()
                seen["items"] = list(warm)

        with BI._large_sequential_warm():
            thread = threading.Thread(target=other, name="other-operation")
            thread.start()
            thread.join(_WAIT)
        assert seen == {"threads": [], "items": files}
        assert spy.opened == []
        # Признак снят на выходе: та же операция в этом потоке после неё — без прогрева.
        with BI._WarmAhead(files) as warm:
            assert list(warm) == files
        assert spy.opened == []

    def test_single_file_list_starts_no_threads(self, tmp_path, monkeypatch):
        (only,) = _files(tmp_path / "1", 1)
        spy = _OpenSpy(monkeypatch, [only])
        with BI._large_sequential_warm():
            with BI._WarmAhead([only]) as warm:
                assert _warm_threads() == []
                assert list(warm) == [only]
        assert spy.opened == []

    def test_warm_errors_never_reach_the_consumer(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "x", 40)
        spy = _OpenSpy(monkeypatch, files, fail={5, 6})

        def paths_of(path):
            if path == files[7]:
                raise ValueError("bad item")
            return (path,)

        with BI._large_sequential_warm():
            # Один поток: сбой на элементе не должен оборвать прогрев остального списка.
            with BI._WarmAhead(files, paths_of, threads=1) as warm:
                assert spy.wait_opened(set(range(len(files))) - {7})
                assert list(warm) == files
        assert 7 not in spy.opened
        assert spy.fds == set()
        assert _warm_threads() == []

    # Ревью кода 1.51.0: поток прогрева не запустился — `__exit__` после сбоя `__enter__` не зовётся, и прежний код
    # ронял задачу (а с ней сборку) и оставлял уже запущенный поток ждать позиции навсегда.
    def test_thread_start_failure_keeps_the_task_and_leaves_no_threads(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "r", 100)
        spy = _OpenSpy(monkeypatch, files)
        owners = _record_warms(monkeypatch)
        started = _failing_warm_start(monkeypatch, RuntimeError)
        try:
            with BI._large_sequential_warm():
                with BI._WarmAhead(files) as warm:
                    assert len(started) == 1  # второй поток — отказ ОС: прогрев снят, задача идёт без него
                    got = list(warm)
        finally:
            _stop_warms(owners)
        assert got == files
        assert _warm_threads() == []
        assert spy.fds == set()

    def test_interrupt_during_thread_start_stops_the_started_threads(self, tmp_path, monkeypatch):
        files = _files(tmp_path / "i", 100)
        owners = _record_warms(monkeypatch)
        started = _failing_warm_start(monkeypatch, KeyboardInterrupt)
        try:
            with BI._large_sequential_warm():
                with pytest.raises(KeyboardInterrupt):
                    with BI._WarmAhead(files):
                        pytest.fail("вход в менеджер не завершается: прерывание идёт дальше")
            assert len(started) == 1
            # Запущенный поток остановлен и дождался ещё внутри `__enter__`.
            assert _warm_threads() == []
        finally:
            _stop_warms(owners)

    def test_interrupt_after_the_thread_started_waits_for_it(self, tmp_path, monkeypatch):
        """Прерывание внутри ``Thread.start`` уже после запуска потока: ``start()`` не вернулся, но поток жив и
        открывает файл — выход из ``__enter__`` обязан его дождаться."""
        files = _files(tmp_path / "a", 100)
        entered, release = threading.Event(), threading.Event()
        spy = _OpenSpy(monkeypatch, files, block={0: (entered, release)})
        owners = _record_warms(monkeypatch)
        real_start = threading.Thread.start

        def start(thread):
            real_start(thread)
            if thread.name.startswith("rlm-warm"):
                assert entered.wait(_WAIT)  # поток запущен и сейчас открывает файл 0
                threading.Timer(0.2, release.set).start()  # отпустить открытие позже: выход обязан его дождаться
                raise KeyboardInterrupt

        monkeypatch.setattr(threading.Thread, "start", start)
        try:
            with BI._large_sequential_warm():
                with pytest.raises(KeyboardInterrupt):
                    with BI._WarmAhead(files):
                        pytest.fail("вход в менеджер не завершается: прерывание идёт дальше")
            assert release.is_set()  # выход дождался открытия, начатого потоком
            assert _warm_threads() == []
            assert spy.fds == set()
        finally:
            release.set()
            _stop_warms(owners)

    def test_build_survives_a_warm_thread_start_failure(self, tmp_path, monkeypatch):
        """Крупная выгрузка последовательно: отказ запуска потоков прогрева — та же сборка, без брошенных потоков."""
        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        expected = T48._content(T48._build_with(monkeypatch, root, tmp_path / "ok", "1"))
        owners = _record_warms(monkeypatch)
        started = _failing_warm_start(monkeypatch, RuntimeError)
        try:
            db = T48._build_with(monkeypatch, root, tmp_path / "fail", "1")
        finally:
            _stop_warms(owners)
        assert started  # прогрев шёл: первый поток запущен, каждый следующий — отказ ОС
        assert T48._content(db) == expected
        assert _warm_threads() == []


# ═══════════════ Задача 2. Сборщики описаний прогревают свои файлы ═══════════════


def _collectors_files(fmt: str, *, twins: bool = True) -> dict[str, str]:
    """Мир 1.50.0 (макеты СКД объектов и общие, подписки, ФО, перечисления, подсистемы, пакет XDTO, права, форма
    критерия) и то, чего в нём нет: форма справочника и общая форма, предопределённые, план обмена, общая команда,
    команды объекта (CF ``Commands/*.xml``, EDT ``<Имя>/<Имя>.command``), пакет XDTO EDT с ``Package.xdto``.

    ``twins`` — двойники описателей верхнего уровня (тот же текст под именем ``<Имя>_2``) для разделов прогрева; мирам
    сравнения ответов они не нужны: два описателя одного объекта отвечают по порядку листинга ФС."""
    import test_v1_50_0 as F

    files = {rel: text for rel, text in F._world50_files(fmt).items() if not rel.startswith("../")}
    proc = "Процедура П() Экспорт\nКонецПроцедуры\n"
    posting = (
        "Процедура ОбработкаПроведения(Отказ, Режим)\n"
        "\tДвижения.Рег.Записывать = Истина;\n"
        "\tЗапись = Движения.Рег.Добавить();\n"
        "\tЗадание.Выполнить();\n"
        "КонецПроцедуры\n"
    )
    files["Documents/ДокДвижения/" + ("Ext/ObjectModule.bsl" if fmt == "cf" else "ObjectModule.bsl")] = posting
    if fmt == "cf":
        files["Catalogs/Спр/Ext/ObjectModule.bsl"] = proc
        files["Catalogs/Спр/Forms/ФормаЭлемента/Ext/Form.xml"] = F._cf_list_form("Catalog.Спр")
        files["CommonForms/ОбщФорма.xml"] = F._cf_md("CommonForm", "ОбщФорма")
        files["CommonForms/ОбщФорма/Ext/Form.xml"] = F._cf_list_form("Catalog.Спр")
        files["Catalogs/Спр/Ext/Predefined.xml"] = '<?xml version="1.0" encoding="UTF-8"?>\n<PredefinedData/>'
        files["ExchangePlans/Обмен.xml"] = F._cf_md("ExchangePlan", "Обмен")
        files["ExchangePlans/Обмен/Ext/Content.xml"] = '<?xml version="1.0" encoding="UTF-8"?>\n<ExchangePlanContent/>'
        files["CommonCommands/ОбщКоманда.xml"] = F._cf_md("CommonCommand", "ОбщКоманда")
        files["Catalogs/Спр/Commands/Команда2.xml"] = F._cf_md("Command", "Команда2")
        files["Catalogs/Спр/Commands/Команда1/Ext/CommandModule.bsl"] = proc
    else:
        files["Catalogs/Спр/Forms/ФормаЭлемента/Form.form"] = F._edt_list_form("Catalog.Спр")
        files["CommonForms/ОбщФорма/ОбщФорма.mdo"] = F._edt_md("CommonForm", "ОбщФорма")
        files["CommonForms/ОбщФорма/Form.form"] = F._edt_list_form("Catalog.Спр")
        files["ExchangePlans/Обмен/Обмен.mdo"] = F._edt_md("ExchangePlan", "Обмен")
        files["CommonCommands/ОбщКоманда/ОбщКоманда.mdo"] = F._edt_md("CommonCommand", "ОбщКоманда")
        files["Catalogs/Спр/Commands/Команда1/Команда1.command"] = F._edt_md("Command", "Команда1")
        files["XDTOPackages/Пакет/Package.xdto"] = (
            '<package xmlns="http://v8.1c.ru/8.1/xdto" targetNamespace="urn:test"/>'
        )
    # Двойник описателя каждого объекта верхнего уровня (CF `Кат/Имя.xml`, EDT `Кат/Имя/Имя.mdo`): ни один раздел
    # сборщика не состоит из одного файла — список из одного файла прогрев не греет (читатель открыл бы его сам сразу).
    for rel, text in list(files.items()) if twins else ():
        parts = rel.split("/")
        if fmt == "cf" and len(parts) == 2 and parts[1].endswith(".xml"):
            files[f"{parts[0]}/{parts[1][:-4]}_2.xml"] = text
        elif fmt == "edt" and len(parts) == 3 and parts[2] == f"{parts[1]}.mdo" and parts[0] != "Configuration":
            files[f"{parts[0]}/{parts[1]}_2/{parts[1]}_2.mdo"] = text
    return files


def _collectors_root(tmp_path: Path, fmt: str) -> Path:
    import test_v1_50_0 as F

    root = tmp_path / f"collectors_{fmt}" / "cf"
    F._write_tree(root, _collectors_files(fmt))
    return root


_COLLECTORS = {
    "metadata": lambda bp: BI._collect_metadata_tables(bp),
    "forms": lambda bp: BI._collect_form_elements(bp),
    "template_queries": lambda bp: BI._collect_template_queries(bp),
    "roles": lambda bp: BI._rights_batch_data(bp, BI._rights_file_items(bp)),
    "synonyms": lambda bp: BI._collect_object_synonyms(bp),
}


class _ReadSpy:
    """Чтения сборщика и открытия прогрева под корнем ``base``. Открытие разбором (``open``/``io.open`` вне потоков
    ``rlm-warm*``) ждёт, пока этот файл откроет прогрев (``os.open`` потока ``rlm-warm*``): файл, которого прогрев так
    и не открыл, — промах (после первого промаха ожидания нет). ``wait_warm=False`` — только запись."""

    def __init__(self, monkeypatch, base: Path, *, wait_warm: bool = True, timeout: float = 3.0) -> None:
        import builtins
        import io
        import pathlib

        self._base = os.path.abspath(base)
        self.base = os.path.normcase(self._base)
        self.cond = threading.Condition()
        self.warm: list[str] = []
        self._warm_set: set[str] = set()
        self.read: list[str] = []
        self.misses: list[str] = []
        self._wait = wait_warm
        self._timeout = timeout
        real_os_open, real_open, real_io_open = os.open, builtins.open, io.open

        def os_open(path, flags, *a, **k):
            if threading.current_thread().name.startswith("rlm-warm"):
                n = self._norm(path)
                if n is not None:
                    with self.cond:
                        self.warm.append(n)
                        self._warm_set.add(n)
                        self.cond.notify_all()
            return real_os_open(path, flags, *a, **k)

        def consumer(real):
            def spy(file, *a, **k):
                if not isinstance(file, int) and not threading.current_thread().name.startswith("rlm-warm"):
                    n = self._norm(file)
                    if n is not None:
                        self._consumed(n)
                return real(file, *a, **k)

            return spy

        monkeypatch.setattr(os, "open", os_open)
        monkeypatch.setattr(builtins, "open", consumer(real_open))
        monkeypatch.setattr(io, "open", consumer(real_io_open))
        # Python 3.10: `Path.open`/`read_text` открывают через `pathlib._NormalAccessor.open` — ссылку на `io.open`,
        # взятую при импорте `pathlib` (с 3.11 — `io.open` в момент вызова): подмена `io.open` ее не видит.
        accessor = getattr(pathlib, "_NormalAccessor", None)
        if accessor is not None:
            monkeypatch.setattr(accessor, "open", staticmethod(consumer(real_io_open)))

    def _norm(self, path) -> str | None:
        try:
            n = os.path.normcase(os.path.abspath(os.fspath(path)))
        except TypeError:
            return None
        return n if n.startswith(self.base) else None

    def _consumed(self, n: str) -> None:
        if not os.path.isfile(n):
            return  # попытка открыть каталог или несуществующий путь — не чтение файла (прогрев их не открывает)
        with self.cond:
            self.read.append(n)
            if not self._wait:
                return
            if self.misses:
                if n not in self._warm_set:
                    self.misses.append(n)
                return
            if not self.cond.wait_for(lambda: n in self._warm_set, timeout=self._timeout):
                self.misses.append(n)

    def rel(self, paths) -> list[str]:
        """Пути относительно корня, как в дереве фикстуры (``/``, исходный регистр — по ``self.names``)."""
        names = self.names
        return sorted({names.get(p, os.path.relpath(p, self.base).replace(os.sep, "/")) for p in paths})

    @property
    def names(self) -> dict[str, str]:
        out = {}
        for top, _dirs, files in os.walk(self._base):
            for name in files:
                full = os.path.join(top, name)
                out[os.path.normcase(full)] = os.path.relpath(full, self._base).replace(os.sep, "/")
        return out


class TestCollectorsWarm:
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_collector_output_with_warm_equals_without(self, tmp_path, monkeypatch, fmt):
        root = str(_collectors_root(tmp_path, fmt))
        for name, collect in _COLLECTORS.items():
            plain = collect(root)  # координатор, малая выгрузка: без прогрева, свои пулы потоков
            # Признак крупной выгрузки — только у кода релиза: на HEAD тот же вызов без него (тождество — на обеих).
            with getattr(BI, "_large_sequential_warm", contextlib.nullcontext)():
                sequential = collect(root)
            monkeypatch.setattr(IW, "_IN_INDEX_WORKER", True)
            try:
                worker = collect(root)
            finally:
                monkeypatch.setattr(IW, "_IN_INDEX_WORKER", False)
            assert plain == sequential == worker, name
            assert plain, name

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    @pytest.mark.parametrize("name", list(_COLLECTORS))
    def test_worker_collector_warms_exactly_the_files_it_reads(self, tmp_path, monkeypatch, fmt, name):
        root = _collectors_root(tmp_path, fmt)
        monkeypatch.setattr(IW, "_IN_INDEX_WORKER", True)
        spy = _ReadSpy(monkeypatch, root)
        _COLLECTORS[name](str(root))
        assert spy.read, name
        assert spy.rel(spy.misses) == []  # каждый прочитанный файл прогрев открыл раньше разбора
        assert spy.rel(spy.warm) == spy.rel(spy.read)  # и прогрев не открывает того, что разбор не читает
        assert _warm_threads() == []

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_template_queries_warm_descriptors_and_contents(self, tmp_path, monkeypatch, fmt):
        root = _collectors_root(tmp_path, fmt)
        monkeypatch.setattr(IW, "_IN_INDEX_WORKER", True)
        spy = _ReadSpy(monkeypatch, root)
        BI._collect_template_queries(str(root))
        if fmt == "cf":
            wanted = {
                # описатели макетов, которые читает обнаружение (тип макета), — у владельцев и общие
                "Reports/ОтчетСКД/Templates/ОсновнаяСхема.xml",
                "Reports/ОтчетСКД/Templates/Печать.xml",
                "Documents/Док/Templates/СхемаДок.xml",
                "CommonTemplates/ОбщСКД.xml",
                "CommonTemplates/ОбщТабл.xml",
                # содержимое макетов СКД
                "Reports/ОтчетСКД/Templates/ОсновнаяСхема/Ext/Template.xml",
                "Documents/Док/Templates/СхемаДок/Ext/Template.xml",
                "DataProcessors/ОбрСКД/Templates/Схема/Ext/Template.xml",
                "CommonTemplates/ОбщСКД/Ext/Template.xml",
            }
        else:
            wanted = {
                "Reports/ОтчетСКД/Templates/ОсновнаяСхема/Template.dcs",
                "Documents/Док/Templates/СхемаДок/Template.dcs",
                "DataProcessors/ОбрСКД/Templates/Схема/Template.dcs",
                "CommonTemplates/ОбщСКД/Template.dcs",
            }
        assert wanted <= set(spy.rel(spy.warm))
        assert spy.rel(spy.misses) == []
        # Содержимое табличного макета разбор не читает — и прогрев его не открывает.
        assert not [p for p in spy.rel(spy.warm) if "Печать/" in p or "ОбщТабл/" in p]

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_sequential_large_export_warms_tasks_without_own_pool(self, tmp_path, monkeypatch, fmt):
        root = _collectors_root(tmp_path, fmt)
        assert not IW.in_index_worker()
        for name in ("metadata", "template_queries", "roles"):
            with monkeypatch.context() as m:
                spy = _ReadSpy(m, root)
                with BI._large_sequential_warm():
                    _COLLECTORS[name](str(root))
                assert spy.read and spy.rel(spy.misses) == [], name
        for name in ("forms", "synonyms"):
            with monkeypatch.context() as m:
                spy = _ReadSpy(m, root, wait_warm=False)
                with BI._large_sequential_warm():
                    _COLLECTORS[name](str(root))
                # Вне воркера формы и синонимы читают своим пулом потоков — прогрев им не нужен.
                assert spy.read and spy.warm == [], name
        assert _warm_threads() == []

    def test_template_discovery_is_unchanged(self, tmp_path):
        """Обнаружение макетов СКД (его зовут и опись снимка, и git-путь) — прежний выход: литерал на мире 1.50.0."""
        for fmt, wanted in (
            (
                "cf",
                [
                    ("CommonTemplates", "ОбщСКД", "ОбщСКД", "CommonTemplates/ОбщСКД/Ext/Template.xml"),
                    (
                        "DataProcessors",
                        "ОбрСКД",
                        "БезЗапросов",
                        "DataProcessors/ОбрСКД/Templates/БезЗапросов/Ext/Template.xml",
                    ),
                    ("DataProcessors", "ОбрСКД", "Схема", "DataProcessors/ОбрСКД/Templates/Схема/Ext/Template.xml"),
                    ("Documents", "Док", "СхемаДок", "Documents/Док/Templates/СхемаДок/Ext/Template.xml"),
                    (
                        "Reports",
                        "ОтчетСКД",
                        "ОсновнаяСхема",
                        "Reports/ОтчетСКД/Templates/ОсновнаяСхема/Ext/Template.xml",
                    ),
                ],
            ),
            (
                "edt",
                [
                    ("CommonTemplates", "ОбщСКД", "ОбщСКД", "CommonTemplates/ОбщСКД/Template.dcs"),
                    (
                        "DataProcessors",
                        "ОбрСКД",
                        "БезЗапросов",
                        "DataProcessors/ОбрСКД/Templates/БезЗапросов/Template.dcs",
                    ),
                    ("DataProcessors", "ОбрСКД", "Схема", "DataProcessors/ОбрСКД/Templates/Схема/Template.dcs"),
                    ("Documents", "Док", "СхемаДок", "Documents/Док/Templates/СхемаДок/Template.dcs"),
                    ("Reports", "ОтчетСКД", "ОсновнаяСхема", "Reports/ОтчетСКД/Templates/ОсновнаяСхема/Template.dcs"),
                ],
            ),
        ):
            root = _collectors_root(tmp_path, fmt)
            got = [(c, o, n, p.relative_to(root).as_posix()) for c, o, n, p in BI._template_query_files(root)]
            assert got == wanted, fmt


# ═══════════════ Задача 3. Порция BSL прогревает свои модули ═══════════════


def _bsl_batch(root: Path) -> list[Path]:
    """Модули мира задачи 2 в порядке обнаружения сборки, плюс непрочитанное: несуществующий файл и каталог `.bsl`."""
    paths = sorted(root.rglob("*.bsl"))
    (root / "Catalogs" / "Каталог.bsl").mkdir(parents=True, exist_ok=True)
    return [
        paths[0],
        root / "CommonModules" / "Нет" / "Ext" / "Module.bsl",
        root / "Catalogs" / "Каталог.bsl",
        *paths[1:],
    ]


class TestBslBatchWarm:
    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_batch_result_with_warm_equals_without(self, tmp_path, monkeypatch, fmt):
        root = _collectors_root(tmp_path, fmt)
        batch = _bsl_batch(root)
        plain = BI._parse_bsl_batch(batch, str(root), True)
        with getattr(BI, "_large_sequential_warm", contextlib.nullcontext)():
            sequential = BI._parse_bsl_batch(batch, str(root), True)
        monkeypatch.setattr(IW, "_IN_INDEX_WORKER", True)
        worker = BI._parse_bsl_batch(batch, str(root), True)
        assert plain == sequential == worker
        # Непрочитанные (нет файла, каталог) в результат не попадают — как прежде; вызовы и движения на месте.
        assert len(plain) == len(batch) - 2
        assert any(r.raw_calls for r in plain) and any(r.movements for r in plain)

    @pytest.mark.parametrize("mode", ["worker", "sequential"])
    def test_batch_modules_are_opened_by_the_warm_first(self, tmp_path, monkeypatch, mode):
        root = _collectors_root(tmp_path, "cf")
        batch = _bsl_batch(root)
        if mode == "worker":
            monkeypatch.setattr(IW, "_IN_INDEX_WORKER", True)
        flag = BI._large_sequential_warm() if mode == "sequential" else contextlib.nullcontext()
        spy = _ReadSpy(monkeypatch, root)
        with flag:
            BI._parse_bsl_batch(batch, str(root), True)
        assert spy.read and spy.rel(spy.misses) == []
        assert spy.rel(spy.warm) == spy.rel(spy.read)  # прогрев открывает ровно модули порции, которые читает разбор
        assert _warm_threads() == []

    def test_batch_without_warm_outside_worker_and_flag(self, tmp_path, monkeypatch):
        root = _collectors_root(tmp_path, "edt")
        spy = _ReadSpy(monkeypatch, root, wait_warm=False)
        BI._parse_bsl_batch(_bsl_batch(root), str(root), True)  # малая выгрузка, координатор: без прогрева
        assert spy.read and spy.warm == []

    def test_git_delta_in_processes_uses_the_same_batches(self, tmp_path, monkeypatch):
        root = _collectors_root(tmp_path, "cf")
        paths = sorted(root.rglob("*.bsl"))
        expected = BI._parse_bsl_batch(paths, str(root), True)
        monkeypatch.setattr(BI, "_PARALLEL_MIN_BSL_FILES", 0)
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        monkeypatch.setenv("RLM_INDEX_WORKERS", "2")
        assert BI._parse_bsl_files(paths, str(root), True) == expected


# ═══════════════ Задача 4. Сборка: пул в начале, обход BSL — первой задачей ═══════════════


def _large_root(tmp_path: Path, fmt: str = "cf", count: int = 300) -> Path:
    """Мир задачи 2 и ещё ``count`` общих модулей: выгрузка крупнее порога параллельного разбора (256 ``.bsl``)."""
    import test_v1_50_0 as F

    files = _collectors_files(fmt)
    for i in range(count):
        name = f"Общий{i:03d}"
        body = f"Процедура П{i}() Экспорт\n\tЗадание.Выполнить();\nКонецПроцедуры\n"
        if fmt == "cf":
            files[f"CommonModules/{name}.xml"] = F._cf_md("CommonModule", name, props="<Server>true</Server>")
            files[f"CommonModules/{name}/Ext/Module.bsl"] = body
        else:
            files[f"CommonModules/{name}/{name}.mdo"] = F._edt_md("CommonModule", name, "<server>true</server>")
            files[f"CommonModules/{name}/Module.bsl"] = body
    root = tmp_path / f"large_{fmt}" / "cf"
    F._write_tree(root, files)
    assert BI._large_export(str(root))
    return root


def _submitted(monkeypatch) -> list[str]:
    """Имена функций задач в порядке постановки в пул сборки."""
    order: list[str] = []
    real = IW.IndexTaskPool.submit

    def submit(self, fn, *args):
        order.append(fn.__name__)
        return real(self, fn, *args)

    monkeypatch.setattr(IW.IndexTaskPool, "submit", submit)
    return order


class TestPoolBeforeDiscovery:
    def test_large_export_discovers_bsl_as_the_first_pool_task(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        calls: list[str] = []
        real_discover = BI._discover_bsl

        def discover(base_path):
            calls.append(base_path)
            return real_discover(base_path)

        discover.__name__ = "_discover_bsl"
        monkeypatch.setattr(BI, "_discover_bsl", discover)
        rglobs: list[str] = []
        real_rglob = Path.rglob

        def rglob(self, pattern, *a, **k):
            if pattern == "*.bsl":
                rglobs.append(pattern)
            return real_rglob(self, pattern, *a, **k)

        monkeypatch.setattr(Path, "rglob", rglob)
        order = _submitted(monkeypatch)
        # Последовательно: шпионы видят вызовы в этом процессе.
        T48._build_with(monkeypatch, root, tmp_path / "idx", "1")
        assert calls == [str(root)]  # ровно один обход — задачей пула
        assert order[0] == "_discover_bsl"
        first_batch = order.index("_parse_bsl_batch")
        descriptors = {
            "_collect_metadata_tables",
            "_collect_form_elements",
            "_collect_template_queries",
            "_rights_batch_data",
            "_collect_object_synonyms",
            "_collect_file_paths",
            "_parse_configuration_meta",
        }
        assert descriptors <= set(order[1:first_batch])  # задачи описаний — до первой порции BSL
        # Полного обхода в координаторе нет: только ограниченный счёт `_large_export` и обход задачи.
        assert len(rglobs) == 2

    @pytest.mark.parametrize("fmt", ["cf", "edt"])
    def test_large_export_content_in_processes_equals_sequential(self, tmp_path, monkeypatch, fmt):
        import test_v1_48_0 as T48

        root = _large_root(tmp_path, fmt)
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        sequential = T48._build_with(monkeypatch, root, tmp_path / "seq", "1")
        parallel = T48._build_with(monkeypatch, root, tmp_path / "par", "2")
        assert T48._content(sequential) == T48._content(parallel)

    def test_discovery_failure_keeps_the_previous_index(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        db = T48._build_with(monkeypatch, root, tmp_path / "idx", "1")
        before = T48._content(db)

        def broken(_base_path):
            raise OSError("discovery failed")

        broken.__name__ = "_discover_bsl"
        monkeypatch.setattr(BI, "_discover_bsl", broken)
        with pytest.raises(OSError, match="discovery failed"):
            BI.IndexBuilder().build(str(root))
        assert not BI.index_incomplete(db)  # build_in_progress не ставился — ошибка до изменений БД
        assert T48._content(db) == before
        assert not list(db.parent.glob(".bsl_index.spool-*"))

    def test_failed_process_start_builds_the_same_large_index_sequentially(self, tmp_path, monkeypatch, caplog):
        import logging

        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        sequential = T48._build_with(monkeypatch, root, tmp_path / "seq", "1")

        def refuse(*_a, **_kw):
            raise OSError("CreatePipe refused")

        monkeypatch.setattr(IW, "ProcessPoolExecutor", refuse)
        with caplog.at_level(logging.WARNING, logger="rlm_tools_bsl"):
            fallback = T48._build_with(monkeypatch, root, tmp_path / "fallback", "4")
        assert caplog.text.count("cannot start worker processes") == 1
        assert T48._content(sequential) == T48._content(fallback)

    def test_small_export_discovers_before_the_pool(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root = _collectors_root(tmp_path, "cf")
        assert not BI._large_export(str(root))
        events: list[str] = []
        real_rglob = Path.rglob

        def rglob(self, pattern, *a, **k):
            if pattern == "*.bsl":
                events.append("rglob")
            return real_rglob(self, pattern, *a, **k)

        class Pool(IW.IndexTaskPool):
            def __init__(self, *a, **k):
                events.append("pool")
                super().__init__(*a, **k)

        monkeypatch.setattr(Path, "rglob", rglob)
        monkeypatch.setattr(BI, "IndexTaskPool", Pool)
        order = _submitted(monkeypatch)
        T48._build_with(monkeypatch, root, tmp_path / "idx", "auto")
        # Прежний порядок: счёт `_large_export`, обход в координаторе, затем пул (один процесс — последовательно).
        assert events == ["rglob", "rglob", "pool"]
        assert "_discover_bsl" not in order


# ═══════════════ Задача 5. Пересборка на месте: старый файл индекса — чтением подряд ═══════════════


class _DbReadSpy:
    """События сборки по порядку: чтение файла индекса подряд (``open(<db>, 'rb')`` — сколько байт прочитано) и
    очистка схемы (``_drop_all_objects_except_meta``)."""

    def __init__(self, monkeypatch, db_path: Path, *, fail: bool = False) -> None:
        import builtins
        import io

        self.events: list[tuple[str, int]] = []
        target = os.path.normcase(os.path.abspath(db_path))
        real_open, real_io_open = builtins.open, io.open
        spy = self

        def opener(real):
            def wrapped(file, mode="r", *a, **k):
                hit = not isinstance(file, int) and os.path.normcase(os.path.abspath(os.fspath(file))) == target
                if hit and "r" in mode and "+" not in mode and "w" not in mode:
                    if fail:
                        raise PermissionError(13, "denied by test", os.fspath(file))
                    fh = real(file, mode, *a, **k)
                    return _CountingReader(fh, spy)
                return real(file, mode, *a, **k)

            return wrapped

        monkeypatch.setattr(builtins, "open", opener(real_open))
        monkeypatch.setattr(io, "open", opener(real_io_open))
        real_drop = BI.IndexBuilder._drop_all_objects_except_meta

        def drop(builder, conn):
            spy.events.append(("drop", 0))
            return real_drop(builder, conn)

        monkeypatch.setattr(BI.IndexBuilder, "_drop_all_objects_except_meta", drop)

    def read_bytes(self) -> int:
        return sum(n for kind, n in self.events if kind == "read")


class _CountingReader:
    def __init__(self, fh, spy: _DbReadSpy) -> None:
        self._fh, self._spy = fh, spy
        spy.events.append(("open", 0))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def readinto(self, buf):
        n = self._fh.readinto(buf)
        if n:
            self._spy.events.append(("read", n))
        return n

    def read(self, *a):
        data = self._fh.read(*a)
        if data:
            self._spy.events.append(("read", len(data)))
        return data

    def close(self):
        self._fh.close()


# Порог чтения подряд в тестах: больше свежего файла SQLite (одна страница после `journal_mode=WAL`), меньше
# индекса фикстуры.
_PREREAD_TEST_MIN = 64 * 1024


class TestOldDbPreRead:
    def _built(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root = _collectors_root(tmp_path, "cf")
        db = T48._build_with(monkeypatch, root, tmp_path / "idx", "1")
        return root, db

    def test_inplace_rebuild_reads_the_old_file_before_the_first_drop(self, tmp_path, monkeypatch):
        root, db = self._built(tmp_path, monkeypatch)
        monkeypatch.setattr(BI, "_DB_PREREAD_MIN_BYTES", _PREREAD_TEST_MIN, raising=False)  # не 64 МБ
        size = db.stat().st_size
        assert size >= _PREREAD_TEST_MIN
        spy = _DbReadSpy(monkeypatch, db)
        BI.IndexBuilder().build(str(root))
        kinds = [kind for kind, _n in spy.events]
        assert "drop" in kinds and "open" in kinds
        assert kinds.index("open") < kinds.index("drop")
        # Весь файл прочитан подряд — до очистки схемы.
        before_drop = sum(n for kind, n in spy.events[: kinds.index("drop")] if kind == "read")
        assert before_drop >= size

    def test_small_file_and_fresh_build_are_not_read(self, tmp_path, monkeypatch):
        root, db = self._built(tmp_path, monkeypatch)
        spy = _DbReadSpy(monkeypatch, db)
        BI.IndexBuilder().build(str(root))  # файл фикстуры меньше порога (64 МБ)
        assert [kind for kind, _n in spy.events] == ["drop"]
        monkeypatch.setattr(BI, "_DB_PREREAD_MIN_BYTES", _PREREAD_TEST_MIN, raising=False)
        fresh_idx = tmp_path / "fresh_idx"
        monkeypatch.setenv("RLM_INDEX_DIR", str(fresh_idx))
        fresh_db = BI.get_index_db_path(str(root))
        fresh_spy = _DbReadSpy(monkeypatch, fresh_db)
        BI.IndexBuilder().build(str(root))  # свежая сборка: файл только что создан — пропуск по размеру
        assert [kind for kind, _n in fresh_spy.events] == ["drop"]

    def test_open_error_does_not_fail_the_build(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root, db = self._built(tmp_path, monkeypatch)
        expected = T48._content(db)
        monkeypatch.setattr(BI, "_DB_PREREAD_MIN_BYTES", _PREREAD_TEST_MIN, raising=False)
        with monkeypatch.context() as m:
            _DbReadSpy(m, db, fail=True)
            BI.IndexBuilder().build(str(root))
        assert T48._content(db) == expected

    def test_inplace_rebuild_content_equals_fresh_build(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root, db = self._built(tmp_path, monkeypatch)
        monkeypatch.setattr(BI, "_DB_PREREAD_MIN_BYTES", _PREREAD_TEST_MIN, raising=False)
        BI.IndexBuilder().build(str(root))  # на месте, поверх прежнего файла
        fresh = T48._build_with(monkeypatch, root, tmp_path / "fresh", "1")
        assert T48._content(db) == T48._content(fresh)


# ═══════════════ Задача 6. Без фонового прогрева координатора ═══════════════


_NEW_PROC = "\nПроцедура Новая() Экспорт\nКонецПроцедуры\n"


def _without_meta(content: dict) -> dict:
    return {table: rows for table, rows in content.items() if table != "index_meta"}


class TestNoCoordinatorPrefetch:
    @pytest.mark.parametrize("workers", ["1", "2"])
    def test_large_build_starts_no_coordinator_prefetch(self, tmp_path, monkeypatch, workers):
        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        monkeypatch.setattr(IW, "available_cpus", lambda: 4)
        with monkeypatch.context() as m:
            starts = T48._ThreadStarts(m)
            T48._build_with(m, root, tmp_path / f"idx{workers}", workers)
        assert starts.with_prefix("rlm-prefetch") == []

    def test_large_full_scan_starts_no_coordinator_prefetch(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        T48._build_with(monkeypatch, root, tmp_path / "idx", "1")
        module = root / "CommonModules" / "Общий000" / "Ext" / "Module.bsl"
        module.write_text(module.read_text(encoding="utf-8") + _NEW_PROC, "utf-8")
        with monkeypatch.context() as m:
            starts = T48._ThreadStarts(m)
            res = BI.IndexBuilder().update(str(root))
        assert res["git_fast_path"] is False and res["changed"] == 1, res
        assert starts.with_prefix("rlm-prefetch") == []

    def test_large_build_content_equals_small_build(self, tmp_path, monkeypatch):
        """Сборка той же фикстуры как крупной выгрузки (пул в начале, обход задачей, прогрев в задачах) и как малой."""
        import test_v1_48_0 as T48

        root = _collectors_root(tmp_path, "cf")
        small = T48._build_with(monkeypatch, root, tmp_path / "small", "1")
        monkeypatch.setattr(BI, "_large_export", lambda _base_path: True)
        large = T48._build_with(monkeypatch, root, tmp_path / "large", "1")
        assert T48._content(small) == T48._content(large)

    def test_full_scan_content_equals_fresh_build(self, tmp_path, monkeypatch):
        import test_v1_48_0 as T48

        root = _large_root(tmp_path)
        db = T48._build_with(monkeypatch, root, tmp_path / "idx", "1")
        module = root / "CommonModules" / "Общий001" / "Ext" / "Module.bsl"
        module.write_text(module.read_text(encoding="utf-8") + _NEW_PROC, "utf-8")
        descriptor = root / "Catalogs" / "Спр.xml"
        descriptor.write_text(
            descriptor.read_text(encoding="utf-8").replace("Справочник тест", "Справочник два"), "utf-8"
        )
        BI.IndexBuilder().update(str(root))
        fresh = T48._build_with(monkeypatch, root, tmp_path / "fresh", "1")
        assert _without_meta(T48._content(db)) == _without_meta(T48._content(fresh))


# ═══════════════ Задача 8. find_roles: строка роли — член запрошенного объекта (issue #45) ═══════════════

# Роли мира задачи 8: права (сырые имена объектов прав) в порядке файла — от него зависит «первый объект потока».
_ROLES8: dict[str, dict[str, list[str]]] = {
    # (а) только члены документа и однокоренной чужой справочник (он первым в файле)
    "ЧленыДокумента": {
        "Catalog.ПродажаПрисоединенныеФайлы": ["Read"],
        "Catalog.ПродажаПрисоединенныеФайлы.TabularSection.Подписи.Attribute.Верна": ["View", "Edit"],
        "Document.Продажа.Attribute.Б": ["View", "Edit"],
        "Document.Продажа.Attribute.А": ["View"],
    },
    # (б) омоним другой головы и член документа
    "Омоним": {"Catalog.Продажа": ["Read"], "Document.Продажа.Attribute.А": ["View"]},
    # (в) голое имя: член справочника (другой головы), чужой однокоренной — первым
    "ЧленыСправочника": {"Catalog.ПродажаПрисоединенныеФайлы": ["Read"], "Catalog.Продажа.Attribute.Код": ["View"]},
    # (г) только сам документ
    "СамДокумент": {"Document.Продажа": ["Read", "Update"]},
    # (г) сам документ, его член и чужой однокоренной (первым в файле)
    "ДокументИЧужие": {
        "Catalog.ПродажаПрисоединенныеФайлы": ["Read"],
        "Document.Продажа": ["Read"],
        "Document.Продажа.Attribute.А": ["View"],
    },
    # (г2) голое имя: точные объекты двух голов и член первого из них, чужих нет
    "ДваТочных": {
        "AccumulationRegister.План": ["Read", "Update"],
        "AccumulationRegister.План.Dimension.Изм": ["View"],
        "Report.План": ["Use", "View"],
    },
    # (д) только однокоренной
    "Однокоренные": {"Catalog.ПродажаПрисоединенныеФайлы": ["Read", "Update"]},
}


def _roles8_files(fmt: str) -> dict[str, str]:
    """Мир задачи 2 и объекты/роли задачи 8: документ `Продажа`, омоним-справочник `Продажа`, однокоренной
    `ПродажаПрисоединенныеФайлы`, регистр накопления и отчёт `План`."""
    import test_v1_50_0 as F

    files = _collectors_files(fmt)
    cf = fmt == "cf"
    for category, kind, name in (
        ("Documents", "Document", "Продажа"),
        ("Catalogs", "Catalog", "Продажа"),
        ("Catalogs", "Catalog", "ПродажаПрисоединенныеФайлы"),
        ("AccumulationRegisters", "AccumulationRegister", "План"),
        ("Reports", "Report", "План"),
    ):
        if cf:
            files[f"{category}/{name}.xml"] = F._cf_md(kind, name)
        else:
            files[f"{category}/{name}/{name}.mdo"] = F._edt_md(kind, name)
    for role, objects in _ROLES8.items():
        if cf:
            files[f"Roles/{role}.xml"] = F._cf_md("Role", role)
            files[f"Roles/{role}/Ext/Rights.xml"] = F._rights(objects)
        else:
            files[f"Roles/{role}/{role}.mdo"] = F._edt_md("Role", role)
            files[f"Roles/{role}/Rights.rights"] = F._rights(objects)
    return files


def _roles8_world(tmp_path: Path, fmt: str):
    """``(root, bsl_index, bsl_live, reader)`` — хелперы на индексе и без индекса на одном дереве."""
    import test_v1_50_0 as F

    root = tmp_path / f"roles8_{fmt}" / "cf"
    F._write_tree(root, _roles8_files(fmt))
    db = F._build_index(root, tmp_path / f"roles8_{fmt}_idx")
    reader = BI.IndexReader(db)
    return root, F._bsl_for(root, reader), F._bsl_for(root, None), reader


def _role_row(res: dict, role: str) -> dict:
    rows = [r for r in res["roles"] if r["role_name"] == role and r.get("granted_via") != "set_for_new_objects"]
    assert len(rows) == 1, res
    return rows[0]


def _rights_rel(fmt: str, role: str) -> str:
    return f"Roles/{role}/Ext/Rights.xml" if fmt == "cf" else f"Roles/{role}/Rights.rights"


@pytest.fixture(scope="module", params=["cf", "edt"])
def roles8(request, tmp_path_factory):
    """``(fmt, bsl_index, bsl_live, reader)`` мира задачи 8."""
    _root, idx, live, reader = _roles8_world(tmp_path_factory.mktemp("roles8"), request.param)
    yield request.param, idx, live, reader
    reader.close()


class TestRoleRowMembers:
    def test_member_only_row_is_a_member_of_the_document(self, roles8):
        """(а) Роль с правами только на реквизиты документа и на однокоренной чужой справочник: строка — первый по имени
        реквизит документа с его правами; ограниченная выборка — только члены документа (прежде — чужой справочник
        и ни одного члена документа)."""
        _fmt, idx, live, _reader = roles8
        for bsl in (idx, live):
            row = _role_row(bsl["find_roles"]("Документ.Продажа", details_limit=2), "ЧленыДокумента")
            assert row["object"] == "Document.Продажа.Attribute.А" and row["rights"] == ["View"], row
            assert row["rights_by_object"] == [
                {"object": "Document.Продажа.Attribute.А", "rights": ["View"]},
                {"object": "Document.Продажа.Attribute.Б", "rights": ["Edit"]},
            ], row
            assert row["details_truncated"] is True

    def test_reference_prefers_a_member_to_a_homonym_of_another_head(self, roles8):
        """(б) Ссылка `Документ.Продажа`: член документа — раньше омонима `Catalog.Продажа` (прежде — омоним)."""
        _fmt, idx, live, _reader = roles8
        for bsl in (idx, live):
            row = _role_row(bsl["find_roles"]("Документ.Продажа"), "Омоним")
            assert row["object"] == "Document.Продажа.Attribute.А" and row["rights"] == ["View"], row
            assert row["matched_objects"] == ["Document.Продажа.Attribute.А", "Catalog.Продажа"], row

    def test_bare_name_takes_a_member_of_any_head(self, roles8):
        """(в) Голое имя: член объекта любой головы; точный объект (омоним) — прежний выбор."""
        _fmt, idx, live, _reader = roles8
        for bsl in (idx, live):
            res = bsl["find_roles"]("Продажа")
            row = _role_row(res, "ЧленыСправочника")
            assert row["object"] == "Catalog.Продажа.Attribute.Код" and row["rights"] == ["View"], row
            assert _role_row(res, "ЧленыДокумента")["object"] == "Document.Продажа.Attribute.А"
            assert _role_row(res, "Омоним")["object"] == "Catalog.Продажа"

    def test_role_with_the_exact_object(self, roles8):
        """(г) Роль с правом на сам документ: без чужих совпадений — ответ HEAD; с чужими — `object` и `rights` HEAD, а
        выборка начинается с документа и его членов (прежде — с однокоренного справочника)."""
        fmt, idx, live, _reader = roles8
        for bsl in (idx, live):
            assert _role_row(bsl["find_roles"]("Документ.Продажа"), "СамДокумент") == {
                "role_name": "СамДокумент",
                "object": "Document.Продажа",
                "rights": ["Read", "Update"],
                "file": _rights_rel(fmt, "СамДокумент"),
                "matched_objects": ["Document.Продажа"],
                "rights_by_object": [{"object": "Document.Продажа", "rights": ["Read", "Update"]}],
                "details_truncated": False,
                "granted_via": "explicit",
                "excluded_rights": [],
            }
            row = _role_row(bsl["find_roles"]("Документ.Продажа", details_limit=2), "ДокументИЧужие")
            assert row["object"] == "Document.Продажа" and row["rights"] == ["Read"], row
            assert row["matched_objects"] == ["Document.Продажа", "Document.Продажа.Attribute.А"], row

    def test_bare_name_puts_exact_objects_of_all_heads_before_members(self, roles8):
        """(г2) Голое имя, точные объекты двух голов и член первого: выборка — N минимальных пар ярусного ключа, оба
        точных раньше члена (прежде член вытеснял отчёт); `object` и `rights` — как прежде."""
        _fmt, idx, live, _reader = roles8
        for bsl in (idx, live):
            row = _role_row(bsl["find_roles"]("План", details_limit=3), "ДваТочных")
            assert row["object"] == "AccumulationRegister.План" and row["rights"] == ["Read", "Update"], row
            assert row["rights_by_object"] == [
                {"object": "AccumulationRegister.План", "rights": ["Read", "Update"]},
                {"object": "Report.План", "rights": ["Use"]},
            ], row
            assert row["details_truncated"] is True

    def test_same_root_only_row_answers_as_before(self, roles8):
        """(д) Роль только с однокоренными совпадениями — ответ HEAD."""
        fmt, idx, live, _reader = roles8
        for bsl in (idx, live):
            assert _role_row(bsl["find_roles"]("Документ.Продажа"), "Однокоренные") == {
                "role_name": "Однокоренные",
                "object": "Catalog.ПродажаПрисоединенныеФайлы",
                "rights": ["Read", "Update"],
                "file": _rights_rel(fmt, "Однокоренные"),
                "matched_objects": ["Catalog.ПродажаПрисоединенныеФайлы"],
                "rights_by_object": [{"object": "Catalog.ПродажаПрисоединенныеФайлы", "rights": ["Read", "Update"]}],
                "details_truncated": False,
                "granted_via": "explicit",
                "excluded_rights": [],
            }

    @pytest.mark.parametrize(
        "query,limit",
        [("Документ.Продажа", 20), ("Документ.Продажа", 2), ("Продажа", 20), ("Продажа", 1), ("План", 3)],
    )
    def test_live_branch_rows_equal_index_rows(self, roles8, query, limit):
        """(е) Индексная и живая ветки — один построитель: те же строки ролей."""
        _fmt, idx, live, _reader = roles8

        def explicit(res):
            return sorted(
                (r for r in res["roles"] if r.get("granted_via") != "set_for_new_objects"), key=lambda r: r["role_name"]
            )

        assert explicit(idx["find_roles"](query, details_limit=limit)) == explicit(
            live["find_roles"](query, details_limit=limit)
        )

    def test_get_roles_exact_answers_as_before(self, roles8):
        """(ж) Без `primary_name` (`get_roles_exact`) — прежний ответ (эталон HEAD). Порядок строк — порядок первой
        строки роли в `role_rights` (порядок обнаружения файлов прав: на NTFS — по имени, на ext4 — иной), как у HEAD
        на той же ФС; содержимое строк от ФС не зависит."""
        fmt, _idx, _live, reader = roles8
        table_order = list(
            dict.fromkeys(r[0] for r in reader._conn.execute("SELECT role_name FROM role_rights ORDER BY rowid"))
        )

        def in_table_order(rows):
            return sorted(rows, key=lambda r: table_order.index(r["role_name"]))

        def row(role, obj, rights, matched, by_object):
            return {
                "role_name": role,
                "object": obj,
                "rights": rights,
                "file": _rights_rel(fmt, role),
                "matched_objects": matched,
                "rights_by_object": [{"object": o, "rights": r} for o, r in by_object],
                "details_truncated": False,
            }

        doc, attr_a, attr_b = "Document.Продажа", "Document.Продажа.Attribute.А", "Document.Продажа.Attribute.Б"
        assert reader.get_roles_exact(doc, include_members=True) == in_table_order(
            [
                row("ДокументИЧужие", doc, ["Read", "View"], [doc, attr_a], [(doc, ["Read"]), (attr_a, ["View"])]),
                row("Омоним", attr_a, ["View"], [attr_a], [(attr_a, ["View"])]),
                row("СамДокумент", doc, ["Read", "Update"], [doc], [(doc, ["Read", "Update"])]),
                row(
                    "ЧленыДокумента",
                    attr_b,
                    ["View", "Edit"],
                    [attr_a, attr_b],
                    [(attr_a, ["View"]), (attr_b, ["Edit", "View"])],
                ),
            ]
        )
        assert reader.get_roles_exact(doc) == in_table_order(
            [
                row("ДокументИЧужие", doc, ["Read"], [doc], [(doc, ["Read"])]),
                row("СамДокумент", doc, ["Read", "Update"], [doc], [(doc, ["Read", "Update"])]),
            ]
        )

    def test_sample_is_independent_of_row_order(self):
        """(з) Вытесняющий накопитель с ярусом: порядок строк потока выборку не меняет."""
        import random

        rows = [
            (role, obj, right, "f")
            for role in ("ЧленыДокумента", "Омоним", "ДокументИЧужие")
            for obj, rights in _ROLES8[role].items()
            for right in rights
        ]

        def sample(order):
            builder = BI._RoleGroupBuilder(2, primary_name="Продажа", primary_head="Document")
            for r in order:
                builder.add(*r)
            return sorted(
                (r["role_name"], r["object"], r["rights"], r["matched_objects"], r["rights_by_object"])
                for r in builder.result()
            )

        expected = sample(rows)
        for seed in range(5):
            shuffled = list(rows)
            random.Random(seed).shuffle(shuffled)
            assert sample(shuffled) == expected, seed
        assert [s[1] for s in expected] == [
            "Document.Продажа",
            "Document.Продажа.Attribute.А",
            "Document.Продажа.Attribute.А",
        ]

    def test_one_member_candidate_is_kept(self):
        """(и) У роли с правами на сотни членов объекта в накопителе — один кандидат-член (минимальный по имени):
        память — ``O(roles × details_limit)``."""
        import random

        names = [f"Document.Продажа.Attribute.Р{i:03d}" for i in range(300)]
        random.Random(7).shuffle(names)
        builder = BI._RoleGroupBuilder(5, primary_name="Продажа", primary_head="Document")
        builder.add("Р", "Catalog.ПродажаПрисоединенныеФайлы", "Read", "f")
        for name in names:
            builder.add("Р", name, "View", "f")
            builder.add("Р", name, "Edit", "f")
        members = [o for o in builder._groups["Р"]["candidates"] if o.startswith("Document.Продажа.")]
        assert members == ["Document.Продажа.Attribute.Р000"]
        (row,) = builder.result()
        assert row["object"] == "Document.Продажа.Attribute.Р000" and row["rights"] == ["View", "Edit"], row
        assert row["matched_objects"][0] == "Document.Продажа.Attribute.Р000"


# ═══════════════ Задача 9. Голое имя без объекта категории — отказ, как у ссылки (issue #46) ═══════════════


def _bare9_files(fmt: str) -> dict[str, str]:
    """Мир задачи 2 и то, что нужно матрице задачи 9: отчёт `КнигаПокупок`, регистр `Пустой` (без писателей и
    объявивших), план обмена `Обмен` с составом, документ `ДокТаблица` (таблица механизма модуля менеджера
    `ДвиженияСерийТоваров` — регистра с таким именем нет), документ `ДокПоле` (поле набора `Движения.Сумма`), вложенная
    подсистема мира 1.50.0 (`Продажи/Подчиненная`); расширение `Расш1` рядом: регистр `РасшРег`, определяемый тип
    `РасшТип`, справочник `РасшСпр`."""
    import test_v1_50_0 as F

    cf = fmt == "cf"
    # Без двойников описателей: у двух описателей одного объекта ответ решал бы порядок листинга ФС (NTFS — по имени,
    # ext4 — иной), и эталон HEAD не был бы одним на платформы.
    files = _collectors_files(fmt, twins=False)

    def obj(category: str, kind: str, name: str, *, cf_props: str = "", edt: str = "", prefix: str = "") -> None:
        if cf:
            files[f"{prefix}{category}/{name}.xml"] = F._cf_md(kind, name, props=cf_props)
        else:
            files[f"{prefix}{category}/{name}/{name}.mdo"] = F._edt_md(kind, name, edt)

    def module(category: str, name: str, file: str, text: str) -> None:
        files[f"{category}/{name}/" + (f"Ext/{file}" if cf else file)] = text

    obj("Reports", "Report", "КнигаПокупок")
    obj("AccumulationRegisters", "AccumulationRegister", "Пустой")
    if cf:
        files["ExchangePlans/Обмен/Ext/Content.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<ExchangePlanContent><Item><Metadata>Catalog.Спр</Metadata>'
            "<AutoRecord>Allow</AutoRecord></Item></ExchangePlanContent>"
        )
    else:
        files["ExchangePlans/Обмен/Обмен.mdo"] = F._edt_md(
            "ExchangePlan", "Обмен", "<content><mdObject>Catalog.Спр</mdObject><autoRecord>Allow</autoRecord></content>"
        )
    obj("Documents", "Document", "ДокТаблица")
    module(
        "Documents",
        "ДокТаблица",
        "ManagerModule.bsl",
        'Функция ТекстЗапросаТаблицаДвиженияСерийТоваров(Запрос)\n\tВозврат "";\nКонецФункции\n',
    )
    obj("Documents", "Document", "ДокПоле")
    module(
        "Documents",
        "ДокПоле",
        "ObjectModule.bsl",
        "Процедура ОбработкаПроведения(Отказ, Режим)\n\tДля Каждого Движения Из Таблица Цикл\n"
        "\t\tДвижения.Сумма = 0;\n\tКонецЦикла;\nКонецПроцедуры\n",
    )
    ext = "../cfe/Расш1/"
    if cf:
        files[ext + "Configuration.xml"] = F.CF_EXT_DESCRIPTOR
    else:
        files[ext + "Configuration/Configuration.mdo"] = F.EDT_EXT_DESCRIPTOR
    obj("AccumulationRegisters", "AccumulationRegister", "РасшРег", prefix=ext)
    obj(
        "DefinedTypes",
        "DefinedType",
        "РасшТип",
        cf_props="<Type><v8:Type>xs:string</v8:Type></Type>",
        edt="<type><types>String</types></type>",
        prefix=ext,
    )
    obj("Catalogs", "Catalog", "РасшСпр", prefix=ext)
    return files


def _bare9_world(tmp_path: Path, fmt: str) -> dict:
    """Сессии мира задачи 9: основная с индексом и без (расширение рядом), расширение без основной рядом и с ней."""
    import test_v1_50_0 as F

    base = tmp_path / f"bare9_{fmt}"
    root = base / "cf"
    F._write_tree(root, _bare9_files(fmt))
    ext = base / "cfe" / "Расш1"
    db = F._build_index(root, base / "idx")
    reader = BI.IndexReader(db)
    main_kw = dict(
        extension_paths=[str(ext)],
        current_config_role="main",
        current_config_name="Тест",
        current_config_root=str(root),
        extension_name_by_root={str(ext): "Расш1"},
    )
    ext_kw = dict(current_config_role="extension", current_config_name="Расш1", current_config_root=str(ext))
    return {
        "fmt": fmt,
        "root": root,
        "ext": ext,
        "reader": reader,
        "index": F._bsl_for(root, reader, idx_zero_callers_authoritative=True, **main_kw),
        "live": F._bsl_for(root, None, **main_kw),
        "ext_alone": F._bsl_for(ext, None, **ext_kw),
        "ext_with_main": F._bsl_for(ext, None, main_config_root=str(root), **ext_kw),
    }


def _answer9(bsl, helper: str, arg) -> dict:
    """Ответ публичного вызова: ``{"ok": ответ}`` либо ``{"error": "<тип>: <текст>"}`` (форма эталона HEAD)."""
    try:
        if helper == "get_object_profile_flow":
            return {"ok": bsl["get_object_profile"](arg, include_flow=True)}
        return {"ok": bsl[helper](arg)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


_TIME_KEYS9 = frozenset({"elapsed", "elapsed_ms", "duration", "total_elapsed_ms"})  # поля времени — вне эталона


def _digest9(entry: dict) -> str:
    import hashlib
    import json

    def norm(obj):
        if isinstance(obj, dict):
            return {k: norm(v) for k, v in obj.items() if k not in _TIME_KEYS9}
        if isinstance(obj, list):
            return [norm(v) for v in obj]
        return obj

    value = ("ERR " + entry["error"]) if "error" in entry else norm(entry["ok"])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# Вид хелпера одной категории: (родительный падеж, «что это», «по каким отвечает», пример формы) — как в тексте отказа.
_KIND9 = {
    "find_register_writers": (
        "регистра",
        "регистр",
        "по регистрам",
        "'РегистрНакопления.X' или путь 'AccumulationRegisters/X'",
    ),
    "find_register_movements": ("документа", "документ", "по документам", "'Документ.X' или путь 'Documents/X'"),
    "find_exchange_plan_content": (
        "плана обмена",
        "план обмена",
        "по планам обмена",
        "'ПланОбмена.X' или путь 'ExchangePlans/X'",
    ),
    "find_defined_types": (
        "определяемого типа",
        "определяемый тип",
        "по определяемым типам",
        "'ОпределяемыйТип.X' или путь 'DefinedTypes/X'",
    ),
}
_REGISTER_DIRS9 = ("AccumulationRegisters", "InformationRegisters", "AccountingRegisters", "CalculationRegisters")


def _descriptor9(fmt: str, category: str, name: str, ext: bool = False) -> str:
    """Путь описателя объекта от корня сессии основной (у объекта расширения — через `../cfe/Расш1/`)."""
    prefix = "../cfe/Расш1/" if ext else ""
    if category == "Subsystems" and name == "Подчиненная":
        rel = "Subsystems/Продажи/Subsystems/Подчиненная" + (".xml" if fmt == "cf" else "/Подчиненная.mdo")
    else:
        rel = f"{category}/{name}" + (".xml" if fmt == "cf" else f"/{name}.mdo")
    return prefix + rel


def _expected9(fmt: str) -> dict[str, tuple[str, str]]:
    """Ожидаемые отказы релиза: ключ матрицы -> ("prefix" | "exact", текст). Всё прочее — ответ HEAD."""
    out: dict[str, tuple[str, str]] = {}

    def missing(session: str, helper: str, arg: str, where: str) -> None:
        genitive = _KIND9[helper][0]
        out[f"{session}|{helper}|{arg}"] = (
            "prefix",
            f"ValueError: {helper}: {genitive} с именем {arg!r} нет — {where}; проверьте имя: ",
        )

    def other(session: str, helper: str, arg: str, category: str, *, ext: bool = False, main_only: bool = False):
        _genitive, what, only, example = _KIND9[helper]
        if main_only:
            route = (
                f"объект основной конфигурации ({category}/{arg}); в этом расширении его нет — анализ: rlm_start на "
                "основной конфигурации"
            )
        else:
            route = f"описание объекта — parse_object_xml({_descriptor9(fmt, category, arg, ext)!r})"
            if category in _REGISTER_DIRS9 and helper != "find_register_writers":
                route += f"; кто пишет в регистр — find_register_writers({arg!r})"
        out[f"{session}|{helper}|{arg}"] = (
            "exact",
            f"ValueError: {helper}: {arg!r} — не {what}, а объект категории {category}. Хелпер отвечает только {only} "
            f"(голое имя, {example}); {route}.",
        )

    main_where = "ни в основной конфигурации, ни в соседних расширениях"
    for session in ("index", "live"):
        missing(session, "find_register_writers", "НетТакогоРегистра", main_where)
        other(session, "find_register_writers", "КнигаПокупок", "Reports")
        other(session, "find_register_writers", "Док", "Documents")
        other(session, "find_register_writers", "Подчиненная", "Subsystems")
        missing(session, "find_register_movements", "НетТакогоДокумента", main_where)
        missing(session, "find_register_movements", "НДС", main_where)
        other(session, "find_register_movements", "Рег", "AccumulationRegisters")
        other(session, "find_register_movements", "КнигаПокупок", "Reports")
        other(session, "find_register_movements", "Подчиненная", "Subsystems")
        missing(session, "find_exchange_plan_content", "НетТакогоОбъекта", main_where)
        other(session, "find_exchange_plan_content", "Спр", "Catalogs")
        other(session, "find_exchange_plan_content", "КнигаПокупок", "Reports")
        other(session, "find_exchange_plan_content", "Подчиненная", "Subsystems")
        missing(session, "find_defined_types", "НетТакогоОбъекта", main_where)
        other(session, "find_defined_types", "Спр", "Catalogs")
        other(session, "find_defined_types", "Подчиненная", "Subsystems")
        other(session, "find_defined_types", "РасшСпр", "Catalogs", ext=True)
        other(session, "find_defined_types", "КнигаПокупок", "Reports")
        other(session, "find_defined_types", "Рег", "AccumulationRegisters")
    # Без индекса писатели — только `Движения.X` модулей объектов (таблицы механизмов модуля менеджера видит индекс):
    # ответ пуст, а регистра с таким именем нет.
    missing("live", "find_register_writers", "ДвиженияСерийТоваров", main_where)
    # Фрагмент: с индексом — точное имя, пустой ответ; без индекса `Движения.Ре` находит `Движения.Рег` — ответ прежний.
    missing("index", "find_register_writers", "Ре", main_where)
    # С индексом фрагмент документа — пустой ответ (без индекса — ответ по подстроке, прежний).
    missing("index", "find_register_movements", "ДокДв", main_where)
    ext_where = "ни в этом расширении, ни в основной конфигурации"
    missing("ext_with_main", "find_defined_types", "НетТакогоОбъекта", ext_where)
    other("ext_with_main", "find_defined_types", "КнигаПокупок", "Reports", main_only=True)
    other("ext_with_main", "find_register_writers", "КнигаПокупок", "Reports", main_only=True)
    other("ext_with_main", "find_exchange_plan_content", "Спр", "Catalogs", main_only=True)
    other("ext_with_main", "find_register_movements", "Рег", "AccumulationRegisters", main_only=True)
    return out


@pytest.fixture(scope="module", params=["cf", "edt"])
def bare9(request, tmp_path_factory):
    world = _bare9_world(tmp_path_factory.mktemp("bare9"), request.param)
    yield world
    world["reader"].close()


class TestBareNameCategoryRefusal:
    def test_bare_name_refusals(self, bare9):
        """(а, б, в — первая часть, з2, и) Голое имя без объекта категории хелпера при пустом ответе — отказ: имени нет
        ни в одной категории — «<вида> с именем … нет»; объект другой категории — прежний отказ категории с маршрутом
        `parse_object_xml` к его описателю (у объекта только основной в сессии на расширении — маршрут `rlm_start`)."""
        expected = _expected9(bare9["fmt"])
        for key, (mode, text) in expected.items():
            session, helper, arg = key.split("|")
            got = _answer9(bare9[session], helper, arg)
            assert "error" in got, (key, got)
            if mode == "exact":
                assert got["error"] == text, key
            else:
                assert got["error"].startswith(text), (key, got["error"])

    def test_every_other_call_answers_as_head(self, bare9):
        """(в — вторая часть, г, д, е, ж, з, к, л) Все прочие вызовы матрицы — ответ HEAD: существующий объект, непустой
        ответ на имя без объекта (таблица механизма, поле набора, подстрока плана обмена), объект только расширения,
        ссылки, не-ссылки иных форм, сессия на расширении без основной, композиты и `find_enum_values`."""
        from _head_answers_v1_51_0 import _HEAD9

        head = _HEAD9[bare9["fmt"]]
        expected = _expected9(bare9["fmt"])
        assert set(expected) <= set(head)
        changed = []
        for key, digest in head.items():
            if key in expected:
                continue
            session, helper, arg = key.split("|")
            if _digest9(_answer9(bare9[session], helper, arg)) != digest:
                changed.append(key)
        assert changed == []

    def test_explicit_answers_that_stay(self, bare9):
        """(в, г, д) Литералы ключевых ответов, которые не меняются: подстрока плана обмена, таблица механизма модуля
        менеджера (индекс) и поле набора, подсказка регистра без писателей и объявивших."""
        idx, live = bare9["index"], bare9["live"]
        for bsl in (idx, live):
            assert bsl["find_exchange_plan_content"]("Обм") == [{"ref": "Catalog.Спр", "auto_record": True}]
            sum_writers = bsl["find_register_writers"]("Сумма")
            assert [w["document"] for w in sum_writers["writers"]] == ["ДокПоле"], sum_writers
            empty = bsl["find_register_writers"]("Пустой")
            assert empty["writers"] == [] and empty["declared_documents"] == []
            assert empty["hint"].endswith("документы-регистраторы его не пишут."), empty["hint"]
        table = idx["find_register_writers"]("ДвиженияСерийТоваров")
        assert [(w["document"], w["source"]) for w in table["writers"]] == [("ДокТаблица", "manager_table")], table

    def test_internal_calls_skip_the_refusal(self, bare9):
        """(к) Композиты зовут хелпер функцией замыкания: `analyze_document_flow` на голом имени без документа —
        прежний ответ с пустыми движениями, а не отказ."""
        for session in ("index", "live"):
            flow = bare9[session]["analyze_document_flow"]("НетТакогоДокумента")
            assert flow["register_movements"]["code_registers"] == [], flow

    def test_refusal_routes_are_executable(self, bare9):
        """(м) Маршрут отказа исполним: `search_objects(...)`/похожие на диске находят объекты; `parse_object_xml(...)`
        читает описатель — объекта верхнего уровня, ВЛОЖЕННОЙ подсистемы и объекта соседнего расширения, с индексом и
        без него."""
        import re

        for session in ("index", "live"):
            bsl = bare9[session]
            text = _answer9(bsl, "find_register_writers", "Пуст")["error"]  # фрагмент имени регистра `Пустой`
            search = re.search(r"search_objects\('([^']*)'\)", text)
            if search:
                assert bsl["search_objects"](search.group(1)), text
            else:
                similar = re.search(r"похожие на диске — (.+)$", text)
                assert similar, text
                for ref in similar.group(1).split(", "):
                    assert bsl["parse_object_xml"](ref).get("name"), ref
            for helper, arg in (
                ("find_defined_types", "Спр"),
                ("find_defined_types", "Подчиненная"),
                ("find_defined_types", "РасшСпр"),
                ("find_register_movements", "Рег"),
            ):
                text = _answer9(bsl, helper, arg)["error"]
                path = re.search(r"parse_object_xml\('([^']*)'\)", text)
                assert path, text
                parsed = bsl["parse_object_xml"](path.group(1))
                assert parsed.get("name") == arg, (text, parsed)
