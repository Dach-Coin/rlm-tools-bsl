"""Один писатель ``server.log`` под Windows-службой (v1.48.0).

До 1.48.0 у файла было два писателя: служба отдавала ребёнку открытый дескриптор ``server.log`` как
stdout/stderr, а ребёнок вешал ещё и свой ``RotatingFileHandler`` на тот же файл. Позиция общего дескриптора
отставала от конца файла — строки с временем затирались, а ротация по размеру падала с ``PermissionError``
(файл держала служба) и после порога печатала ``--- Logging error ---`` на каждую запись.

Теперь владелец файла и его ротации — служба: ребёнок пишет записи с временем в stderr и ``server.log`` не
открывает, служба вычитывает объединённые stdout/stderr ребёнка через PIPE (``ChildOutputPump``) и вместе со
своими событиями watchdog отправляет их в один потокобезопасный ``ServiceLogSink``. Строка без времени в начале
(строки uvicorn, трассировка аварии до настройки логирования, продолжение многострочной записи) получает время
приёма — каждая строка журнала датирована, и ``log_retention`` режет по любой из них.

Модуль не зависит от pywin32: формат, сборку строк и ротацию проверяют тесты на обеих ОС.
"""

from __future__ import annotations

import codecs
import datetime
import logging
import re
import threading
from logging.handlers import RotatingFileHandler

# Формат записи сервера — один на файловый обработчик обычного HTTP-запуска и на stderr ребёнка службы.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

# Порция чтения PIPE и предел незавершённой строки: поток без перевода строки не копится в памяти без конца.
READ_CHUNK_BYTES = 64 * 1024
PARTIAL_LINE_MAX_CHARS = 64 * 1024
# Сколько ждать EOF вывода ребёнка после его выхода: дольше вывод держит только потомок ребёнка.
FINISH_TIMEOUT_SECONDS = 3.0

# Время в начале строки — то же правило, что у log_retention (``[watchdog `` перед временем допустим).
_LEADING_TS_RE = re.compile(r"^(?:\[watchdog )?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")


def _now_text() -> str:
    return datetime.datetime.now().strftime(LOG_DATEFMT)


class ServiceLogSink:
    """Единственный писатель ``server.log``: ``RotatingFileHandler`` под одним замком на строки ребёнка и
    события watchdog. Обработчик не подвешен ни к одному логгеру — строки пишутся напрямую, без повторной
    обработки. После ``close()`` поздние строки молча отбрасываются: поток чтения, который пережил остановку
    (вывод держал потомок ребёнка), не пишет в закрытый файл."""

    def __init__(
        self,
        path,
        *,
        max_bytes: int = LOG_MAX_BYTES,
        backup_count: int = LOG_BACKUP_COUNT,
        clock=_now_text,
    ) -> None:
        self._handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False

    def write_line(self, line: str) -> None:
        """Строка вывода ребёнка (без перевода строки): со временем в начале — как есть, иначе — со временем
        приёма."""
        if not _LEADING_TS_RE.match(line):
            line = f"{self._clock()} {line}"
        self._emit(line)

    def watchdog(self, msg: str, *args) -> None:
        """Событие службы — формат прежнего ``_log_watchdog``: ``[watchdog <время>] <текст>``."""
        text = msg % args if args else msg
        self._emit(f"[watchdog {self._clock()}] {text}")

    def _emit(self, line: str) -> None:
        record = logging.makeLogRecord({"msg": line, "levelno": logging.INFO, "levelname": "INFO"})
        with self._lock:
            if self._closed:
                return
            # Сбой записи (полный диск) стоит строки, а не службы: emit сам гасит ошибку через handleError.
            self._handler.emit(record)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handler.close()


class ChildOutputPump:
    """Поток, непрерывно вычитывающий объединённые stdout/stderr ребёнка в sink.

    Чтение не зависит от интервала проверки здоровья: заполненный PIPE остановил бы сервер на записи. Порции —
    фиксированного размера, UTF-8 декодируется инкрементально (символ на стыке порций не ломается), ``\\r\\n``
    сводится к ``\\n``, незавершённая строка ограничена ``partial_max`` символов и дописывается на EOF."""

    def __init__(
        self,
        stream,
        sink: ServiceLogSink,
        *,
        chunk_bytes: int = READ_CHUNK_BYTES,
        partial_max: int = PARTIAL_LINE_MAX_CHARS,
    ) -> None:
        self._stream = stream
        self._sink = sink
        self._chunk_bytes = chunk_bytes
        self._partial_max = partial_max
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._thread = threading.Thread(target=self._run, name="rlm-service-log-pump", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> bytes:
        read1 = getattr(self._stream, "read1", None)
        return read1(self._chunk_bytes) if read1 is not None else self._stream.read(self._chunk_bytes)

    def _run(self) -> None:
        try:
            while True:
                data = self._read()
                if not data:
                    break
                self._feed(self._decoder.decode(data))
        except (OSError, ValueError):
            pass  # PIPE закрыт под чтением — хвост дописывается ниже
        finally:
            try:
                self._feed(self._decoder.decode(b"", final=True))
            except Exception:
                pass
            if self._pending:
                self._sink.write_line(self._pending)
                self._pending = ""

    def _feed(self, text: str) -> None:
        if not text:
            return
        parts = (self._pending + text).split("\n")
        self._pending = parts.pop()
        for line in parts:
            self._sink.write_line(line[:-1] if line.endswith("\r") else line)
        while len(self._pending) > self._partial_max:
            self._sink.write_line(self._pending[: self._partial_max])
            self._pending = self._pending[self._partial_max :]

    def finish(self, timeout: float = FINISH_TIMEOUT_SECONDS) -> bool:
        """Дождаться EOF после выхода ребёнка (не дольше ``timeout``) и закрыть поток. ``False`` — вывод ещё
        держит потомок ребёнка: поток чтения — демон и дочитает сам, а закрывать поток нельзя (``close``
        буферизованного читателя ждёт замок, который держит висящий ``read1``)."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            return False
        try:
            self._stream.close()
        except Exception:
            pass
        return True


def start_output_pump(proc, sink: ServiceLogSink) -> ChildOutputPump | None:
    """Запустить чтение вывода ``proc`` (``Popen`` со ``stdout=PIPE``) в sink; нет потока вывода — ``None``."""
    stream = getattr(proc, "stdout", None)
    if stream is None:
        return None
    pump = ChildOutputPump(stream, sink)
    pump.start()
    return pump
