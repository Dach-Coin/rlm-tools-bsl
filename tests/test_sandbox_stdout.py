"""v1.29.0 этап 2: bounded stdout capture и закрытие inline stdout-гонки.

Детерминированные тесты вместо случайных sleep (§18.2 плана): перекрытие
управляется threading.Barrier/инжектированными callables, а инвариант после
фикса — ГЛОБАЛЬНАЯ сериализация inline-exec (окна выполнения не пересекаются),
поэтому смешивание маркеров исключено конструктивно.
"""

import io
import sys
import threading
import time

import pytest

from rlm_tools_bsl.sandbox import BoundedTextCapture, Sandbox, TRUNCATION_MARKER


# ---------------------------------------------------------------------------
# BoundedTextCapture unit
# ---------------------------------------------------------------------------


def test_capture_simple():
    cap = BoundedTextCapture(100)
    cap.write("hello")
    cap.write(" world")
    assert cap.getvalue() == "hello world"
    assert cap.truncated is False


def test_capture_unicode_and_empty_writes():
    cap = BoundedTextCapture(100)
    cap.write("")
    cap.write("кириллица ✓ emoji 🎉")
    cap.write("")
    assert cap.getvalue() == "кириллица ✓ emoji 🎉"
    assert cap.truncated is False


def test_capture_exact_limit_not_truncated():
    cap = BoundedTextCapture(5)
    cap.write("abcde")
    assert cap.getvalue() == "abcde"
    assert cap.truncated is False


def test_capture_limit_plus_one():
    cap = BoundedTextCapture(5)
    cap.write("abcdef")
    assert cap.getvalue() == "abcde"
    assert cap.truncated is True


def test_capture_stops_accumulating_after_limit():
    cap = BoundedTextCapture(10)
    cap.write("0123456789")
    # Дальше данные не копятся — память не растёт (§3.4).
    for _ in range(1000):
        cap.write("x" * 1000)
    assert cap.getvalue() == "0123456789"
    assert cap.truncated is True
    assert len(cap._parts) == 1


def test_capture_flush_noop():
    cap = BoundedTextCapture(10)
    cap.write("a")
    cap.flush()
    assert cap.getvalue() == "a"


# ---------------------------------------------------------------------------
# Sandbox.execute + bounded capture (маркер байт-в-байт)
# ---------------------------------------------------------------------------


def test_execute_simple_and_flush(tmp_path):
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    r = sb.execute("print('a')\nprint('b', flush=True)\nprint('в кириллице')")
    assert r.error is None
    assert r.stdout == "a\nb\nв кириллице\n"


def test_execute_output_below_at_and_above_limit(tmp_path):
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=10)
    assert sb.execute("print('12345')").stdout == "12345\n"
    # print добавляет \n: 9 chars + \n = ровно лимит 10
    r = sb.execute("print('123456789')")
    assert r.stdout == "123456789\n"
    assert TRUNCATION_MARKER not in r.stdout
    r = sb.execute("print('12345678901')")  # 11 + \n > 10
    assert r.stdout == "1234567890" + TRUNCATION_MARKER

    sb2 = Sandbox(base_path=str(tmp_path), max_output_chars=50)
    r = sb2.execute("print('a' * 100000)")
    assert r.stdout == "a" * 50 + TRUNCATION_MARKER
    assert len(r.stdout) == 50 + len(TRUNCATION_MARKER)


def test_execute_partial_stdout_on_exception(tmp_path):
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    r = sb.execute("print('before-error')\nraise ValueError('boom')")
    assert "before-error" in r.stdout
    assert r.error is not None and "ValueError" in r.error


def test_namespace_persists_between_executes(tmp_path):
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    sb.execute("x = 41")
    r = sb.execute("x += 1\nprint(x)")
    assert r.stdout.strip() == "42"


# ---------------------------------------------------------------------------
# stdout-гонка: детерминированные тесты сериализации/изоляции
# ---------------------------------------------------------------------------


def _run_parallel_executes(sandboxes_and_codes, start_barrier):
    results = [None] * len(sandboxes_and_codes)
    errors = []

    def _run(i, sb, code):
        try:
            start_barrier.wait(timeout=10)
            results[i] = sb.execute(code)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_run, args=(i, sb, code)) for i, (sb, code) in enumerate(sandboxes_and_codes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


def test_two_sandboxes_no_marker_mixing(tmp_path):
    """Два Sandbox, старты синхронизированы barrier: каждый результат содержит
    ТОЛЬКО свои маркеры, глобальный sys.stdout восстановлен."""
    original_stdout = sys.stdout
    sentinel = io.StringIO()
    sys.stdout = sentinel
    try:
        sb_a = Sandbox(base_path=str(tmp_path), max_output_chars=100_000)
        sb_b = Sandbox(base_path=str(tmp_path), max_output_chars=100_000)
        code_a = "print('A-start')\n" + "\n".join(f"print('A-{i}')" for i in range(100)) + "\nprint('A-end')"
        code_b = "print('B-start')\n" + "\n".join(f"print('B-{i}')" for i in range(100)) + "\nprint('B-end')"
        barrier = threading.Barrier(2)
        for _round in range(5):  # повторяем порядок входа/выхода несколько раз
            res_a, res_b = _run_parallel_executes([(sb_a, code_a), (sb_b, code_b)], barrier)
            assert res_a.error is None and res_b.error is None
            assert "B-" not in res_a.stdout and res_a.stdout.count("A-start") == 1
            assert "A-" not in res_b.stdout and res_b.stdout.count("B-start") == 1
            assert res_a.stdout.count("\n") == 102
            assert res_b.stdout.count("\n") == 102
        # Глобальный stdout равен исходному объекту-подмене и пуст.
        assert sys.stdout is sentinel
        assert sentinel.getvalue() == ""
    finally:
        sys.stdout = original_stdout


def test_inline_executes_are_globally_serialized(tmp_path):
    """Окна exec двух Sandbox не пересекаются: замер monotonic-интервалов через
    инжектированные callables (управляемые события вместо случайного sleep)."""
    sb_a = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    sb_b = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    windows = {}

    def _make_probe(tag):
        def probe():
            start = time.monotonic()
            time.sleep(0.15)
            windows[tag] = (start, time.monotonic())
            return tag

        return probe

    sb_a._namespace["probe"] = _make_probe("A")
    sb_b._namespace["probe"] = _make_probe("B")
    barrier = threading.Barrier(2)
    _run_parallel_executes([(sb_a, "print(probe())"), (sb_b, "print(probe())")], barrier)
    (a0, a1), (b0, b1) = windows["A"], windows["B"]
    assert a1 <= b0 or b1 <= a0, f"exec windows overlap: A={windows['A']} B={windows['B']}"


def test_five_sandboxes_parallel_tags_isolated(tmp_path):
    sandboxes = [Sandbox(base_path=str(tmp_path), max_output_chars=100_000) for _ in range(5)]
    codes = ["\n".join(f"print('S{n}-{i}')" for i in range(50)) for n in range(5)]
    barrier = threading.Barrier(5)
    results = _run_parallel_executes(list(zip(sandboxes, codes)), barrier)
    for n, res in enumerate(results):
        assert res.error is None
        own = f"S{n}-"
        assert res.stdout.count(own) == 50
        for other in range(5):
            if other != n:
                assert f"S{other}-" not in res.stdout


def test_same_sandbox_sequential_namespace_changes(tmp_path):
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    barrier = threading.Barrier(2)
    _run_parallel_executes([(sb, "y = 1"), (sb, "z = 2")], barrier)
    variables = sb.list_variables()
    assert "y" in variables and "z" in variables


def test_global_stdout_restored_after_error(tmp_path):
    original = sys.stdout
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    sb.execute("print('x')\nraise RuntimeError('boom')")
    assert sys.stdout is original


def test_defensive_slice_for_foreign_capture(tmp_path):
    """Нестандартный capture без truncated-флага: post-hoc срез остаётся
    последним defensive-рубежом (§14.2)."""
    sb = Sandbox(
        base_path=str(tmp_path),
        max_output_chars=5,
        output_capture_factory=io.StringIO,
    )
    r = sb.execute("print('abcdefghij')")
    assert r.stdout == "abcde" + TRUNCATION_MARKER


@pytest.mark.parametrize("code,expected", [("print('привет')", "привет\n"), ("print('🎉'*3)", "🎉🎉🎉\n")])
def test_unicode_stdout_roundtrip(tmp_path, code, expected):
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=100)
    assert sb.execute(code).stdout == expected


def test_error_traceback_uses_sandbox_filename_not_string(tmp_path):
    """Код агента компилируется с именем '<rlm-sandbox>', а не дефолтным '<string>'.
    В process-режиме дефолт коллизил с multiprocessing '<string>'-bootstrap worker-а,
    и traceback-кадр кода агента эхом показывал команду spawn_main worker-а (косметика,
    не утечка). Отдельное имя убирает коллизию во ВСЕХ режимах."""
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    r = sb.execute("raise ValueError('boom')")
    assert r.error is not None and "ValueError: boom" in r.error
    assert 'File "<rlm-sandbox>", line 1, in <module>' in r.error
    assert '"<string>"' not in r.error
    assert "spawn_main" not in r.error


def test_syntaxerror_caught_with_sandbox_filename(tmp_path):
    """compile() внутри try: SyntaxError ловится как и раньше (не всплывает наружу),
    а кадр несёт '<rlm-sandbox>'."""
    sb = Sandbox(base_path=str(tmp_path), max_output_chars=1000)
    r = sb.execute("def broken(:\n    pass")
    assert r.error is not None and "SyntaxError" in r.error
    assert "<rlm-sandbox>" in r.error
