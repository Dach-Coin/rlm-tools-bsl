import json
import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from anthropic import Anthropic

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# 2048, а не прежние 1024: на замере с длинным контекстом (16 КБ, ранжирование
# списка имён) лимит 1024 обрывал ответ в 5 случаях из 6 — современные модели
# тратят часть бюджета на рассуждение, и на сам ответ его не остаётся. При 2048
# отказов 2 из 6, при 4096 — 0 из 6. Средняя цена растёт умеренно (920 → 1192
# токена), потому что на 1024 модель просто упирается в потолок; на коротких
# запросах потолок вообще не достигается и не стоит ничего.
DEFAULT_MAX_TOKENS = 2048

ENV_MAX_TOKENS = "RLM_LLM_MAX_TOKENS"
ENV_EXTRA_BODY = "RLM_LLM_EXTRA_BODY"

# Ключи, которыми владеет сборщик запроса. Пришедшие из RLM_LLM_EXTRA_BODY молча
# передрались бы с явными аргументами SDK, поэтому отбрасываются с предупреждением.
_EXTRA_BODY_RESERVED = frozenset({"model", "messages", "max_tokens", "stream"})

# finish_reason, которые сами по себе объясняют пустой content. Рекомендация про
# бюджет reasoning к ним НЕ относится (провайдер вправе прислать content_filter
# вместе с непустым reasoning_content — проверено на openai SDK).
_SELF_EXPLANATORY_FINISH_REASONS = frozenset({"content_filter", "tool_calls", "function_call"})

# Отличает «поля нет» от «поле есть и равно None»: message=[] — это кривая форма
# ответа, а не «нет сообщения», и на truthiness её проверять нельзя.
_MISSING = object()

# Хвост значения env в тексте предупреждения обрезается: переменная может нести
# килобайты, а лог должен остаться читаемым.
_ENV_ECHO_LIMIT = 80

# Тот же приём для finish_reason: значение приходит от провайдера и попадает в
# агент-facing маркер, поэтому длина ограничена (штатные значения — одно слово).
_FINISH_REASON_ECHO_LIMIT = 40


def _echo_env_value(raw: str) -> str:
    """Значение env для текста предупреждения: усечённое и в repr."""
    if len(raw) <= _ENV_ECHO_LIMIT:
        return repr(raw)
    return f"{raw[:_ENV_ECHO_LIMIT]!r}… ({len(raw)} символов)"


# ── Разбор конфигурации sub-LLM из окружения ──────────
#
# Резолверы возвращают ``(value, warning | None)`` и НИЧЕГО не логируют сами:
# куда девать текст — решает вызывающий. Это то, что позволяет провалидировать
# окружение в родительском процессе (где логгер живой) и переиспользовать те же
# функции в sandbox-воркере, который stderr уводит в devnull.
#
# КОНТРАКТ: ни один резолвер не имеет права выбросить исключение. Их зовут при
# старте сервера, и необработанное исключение означало бы не «настройка
# проигнорирована», а «сервер не поднялся» — отказ хуже того, от чего защищаемся.
# Отсюда финальный ``except Exception`` в каждом: перечисление ожидаемых типов
# нужно для точных сообщений, но контракт не может держаться на полноте списка.
# Живой пример промаха такого списка: глубоко вложенный JSON роняет ``json.loads``
# и ``json.dumps`` через ``RecursionError``, а он наследуется от ``RuntimeError``,
# не от ``ValueError``.


def _resolve_max_tokens(raw: str | None) -> tuple[int, str | None]:
    """``RLM_LLM_MAX_TOKENS`` → (лимит, предупреждение). Невалидное → дефолт."""
    if raw is None:
        return DEFAULT_MAX_TOKENS, None
    try:
        text = raw.strip()
        if not text:
            return DEFAULT_MAX_TOKENS, None
        try:
            value = int(text)
        except ValueError:
            return (
                DEFAULT_MAX_TOKENS,
                f"{ENV_MAX_TOKENS}={_echo_env_value(raw)}: ожидалось положительное целое; "
                f"используется значение по умолчанию {DEFAULT_MAX_TOKENS}",
            )
        if value <= 0:
            return (
                DEFAULT_MAX_TOKENS,
                f"{ENV_MAX_TOKENS}={_echo_env_value(raw)}: значение должно быть больше нуля; "
                f"используется значение по умолчанию {DEFAULT_MAX_TOKENS}",
            )
        return value, None
    except Exception as exc:  # noqa: BLE001 — бэкстоп тотальности, см. комментарий выше
        return (
            DEFAULT_MAX_TOKENS,
            f"{ENV_MAX_TOKENS}: проверить значение не удалось ({type(exc).__name__}); "
            f"используется значение по умолчанию {DEFAULT_MAX_TOKENS}",
        )


def _resolve_extra_body(raw: str | None) -> tuple[dict | None, str | None]:
    """``RLM_LLM_EXTRA_BODY`` → (объект, предупреждение). Непригодное → ``None``.

    Разбора JSON и проверки типа корня НЕДОСТАТОЧНО: значение может разобраться и
    всё равно быть неотправляемым. ``httpx`` сериализует тело как
    ``json_dumps(..., ensure_ascii=False, allow_nan=False).encode("utf-8")``,
    поэтому, например, ``1e999`` разбирается в ``inf`` и роняет вызов ДО HTTP —
    уже после того, как квота LLM-вызовов списана. Значит нужна контрольная
    сериализация, буква в букву повторяющая httpx.

    ``ensure_ascii=False`` здесь обязателен и это не описка: с ``True`` одиночный
    surrogate экранируется обратно в ASCII и проверку проходит, а httpx на нём
    падает ``UnicodeEncodeError``. Конвенция проекта ``ensure_ascii=True``
    относится к ВЫВОДУ данных; тут мы не выводим, а моделируем чужой сериализатор.
    """
    if raw is None:
        return None, None
    try:
        text = raw.strip()
        if not text:
            return None, None

        try:
            parsed = json.loads(text)
        except Exception as exc:  # noqa: BLE001 — сюда же RecursionError на глубокой вложенности
            return None, (
                f"{ENV_EXTRA_BODY}: не удалось разобрать JSON ({type(exc).__name__}); параметр проигнорирован"
            )

        if not isinstance(parsed, dict):
            return None, (
                f"{ENV_EXTRA_BODY}: корнем должен быть JSON-объект, получен {type(parsed).__name__}; "
                "параметр проигнорирован"
            )

        dropped = sorted(key for key in parsed if str(key).lower() in _EXTRA_BODY_RESERVED)
        if dropped:
            parsed = {key: value for key, value in parsed.items() if str(key).lower() not in _EXTRA_BODY_RESERVED}

        try:
            json.dumps(parsed, allow_nan=False, ensure_ascii=False).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 — ValueError/UnicodeEncodeError/RecursionError/…
            return None, (
                f"{ENV_EXTRA_BODY}: значение нельзя сериализовать для отправки ({type(exc).__name__}); "
                "параметр проигнорирован"
            )

        warning = None
        if dropped:
            warning = (
                f"{ENV_EXTRA_BODY}: ключи {dropped} задаются самим запросом и отброшены; "
                "остальные поля переданы провайдеру"
            )
        return (parsed or None), warning
    except Exception as exc:  # noqa: BLE001 — бэкстоп тотальности
        return None, (
            f"{ENV_EXTRA_BODY}: проверить значение не удалось ({type(exc).__name__}); параметр проигнорирован"
        )


def validate_llm_env() -> list[str]:
    """Проверить env-настройки sub-LLM. Возвращает предупреждения (пусто = чисто).

    Зовётся из ``server.main()``: провайдер создаётся лениво в sandbox-воркере, а
    тот не настраивает logging и пишет stderr в devnull — предупреждение оттуда
    не увидел бы никто. Тотальна по тем же причинам, что и резолверы.
    """
    warnings: list[str] = []
    for env_name, resolver in ((ENV_MAX_TOKENS, _resolve_max_tokens), (ENV_EXTRA_BODY, _resolve_extra_body)):
        try:
            _value, warning = resolver(os.environ.get(env_name))
        except Exception as exc:  # noqa: BLE001 — бэкстоп тотальности
            warnings.append(f"{env_name}: проверить значение не удалось ({type(exc).__name__}); значение не применено")
            continue
        if warning:
            warnings.append(warning)
    return warnings


def _resolved_max_tokens() -> int:
    """Лимит из env с логированием предупреждения (для фабрик провайдеров)."""
    value, warning = _resolve_max_tokens(os.environ.get(ENV_MAX_TOKENS))
    if warning:
        logger.warning("%s", warning)
    return value


def _resolved_extra_body() -> dict | None:
    """``extra_body`` из env с логированием предупреждения (OpenAI-совместимый путь)."""
    value, warning = _resolve_extra_body(os.environ.get(ENV_EXTRA_BODY))
    if warning:
        logger.warning("%s", warning)
    return value


# ── Разбор ответа OpenAI-совместимого провайдера ───────
#
# openai SDK строит объекты ответа НЕ валидируя (``construct_type``), чтобы
# изменения API не роняли клиент. Значит любое поле может прийти любого типа, и
# ни одно нельзя использовать без проверки: иначе ветвление некорректно, а
# контракт ``-> str`` нарушается. Проверено на живом клиенте, что без этих
# проверок ``choices``-словарь даёт ``KeyError``, ``choices=[0]`` и ``message=[]``
# — ``AttributeError``, а ``content`` списком уезжает наружу из функции,
# обещающей строку.


def _finish_reason_for_display(value) -> str | None:
    """``finish_reason`` для текста маркера: без управляющих символов и усечённый.

    Значение приходит от провайдера и уезжает в агент-facing строку. Без чистки
    сломанный или недобросовестный endpoint вписал бы в маркер перевод строки и
    подделал вид второго маркера (``...finish_reason=stop\\n[REFUSAL] ...``), а
    заодно раздул бы ответ на произвольную длину. На ветвление НЕ влияет — оно
    идёт по исходному значению, приведённому к нижнему регистру.
    """
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value).strip()
    if not cleaned:
        return None
    if len(cleaned) > _FINISH_REASON_ECHO_LIMIT:
        cleaned = cleaned[:_FINISH_REASON_ECHO_LIMIT] + "…"
    return cleaned


def _empty_content_marker(response, choice, message) -> str:
    """Маркер для ответа с пустым ``content`` — текст зависит от ПРИЧИНЫ.

    Рекомендация «поднять лимит / отключить thinking» верна только когда бюджет
    действительно ушёл в reasoning. Провайдер вправе прислать ``content_filter``
    вместе с непустым ``reasoning_content``, поэтому наличие reasoning — не
    равноправное условие, а последний fallback: явная причина всегда бьёт
    эвристику.
    """
    refusal = getattr(message, "refusal", None)
    if isinstance(refusal, str) and refusal.strip():
        # Модель ОТВЕТИЛА, просто отказом. Превратить это в «пусто» — потерять ответ.
        return f"[REFUSAL] {refusal.strip()}"

    raw_finish_reason = getattr(choice, "finish_reason", None)
    finish_reason = _finish_reason_for_display(raw_finish_reason)
    # Ветвление — по регистронезависимому значению. Провайдер, вернувший
    # "CONTENT_FILTER", иначе получил бы reasoning-совет по ложному условию, а
    # "LENGTH" — наоборот, потерял бы верный. Фильтр зарезервированных ключей
    # extra_body уже регистронезависим; здесь та же логика.
    reason_key = raw_finish_reason.strip().lower() if isinstance(raw_finish_reason, str) else ""

    reasoning = getattr(message, "reasoning_content", None)
    reasoning_len = len(reasoning) if isinstance(reasoning, str) and reasoning.strip() else 0

    usage = getattr(response, "usage", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int):
        completion_tokens = None

    if reason_key in _SELF_EXPLANATORY_FINISH_REASONS:
        blame_reasoning = False
    elif reason_key == "length":
        blame_reasoning = True
    else:
        blame_reasoning = reasoning_len > 0

    # В тексте только finish_reason, число completion-токенов и ДЛИНА reasoning —
    # ни промпта, ни ответа, ни самого reasoning_content, ни ключа.
    details = []
    if finish_reason:
        details.append(f"finish_reason={finish_reason}")
    if completion_tokens is not None:
        details.append(f"completion_tokens={completion_tokens}")
    if reasoning_len:
        details.append(f"reasoning_content={reasoning_len} симв.")
    detail = ", ".join(details) if details else "метаданные ответа отсутствуют"

    if blame_reasoning:
        # Адресовано АДМИНИСТРАТОРУ: агент env-переменные задать не может, и совет
        # «поднимите лимит» в его адрес был бы тупиком.
        return (
            f"[EMPTY] LLM вернула пустой content: {detail}. Бюджет ушёл в reasoning; "
            f"администратору сервера — поднять {ENV_MAX_TOKENS} или отключить thinking "
            f"через {ENV_EXTRA_BODY}."
        )
    return f"[EMPTY] LLM вернула пустой content: {detail}."


def _mark_if_truncated(content: str, choice) -> str:
    """Пометить НЕПОЛНЫЙ ответ, оборванный лимитом токенов.

    ``finish_reason == "length"`` при НЕПУСТОМ content — это доказанный обрыв: модель
    не закончила, её оборвал потолок. Отличие от пустого content в том, что здесь
    диагностировать нечего по виду ответа: обрывок выглядит как нормальный ответ, и
    ни агент, ни человек не отличат его от полного. На части провайдеров это самый
    частый режим отказа — модель тратит бюджет на рассуждение прямо внутри content.

    Предупреждение дописывается ХВОСТОМ и с новой строки, а не префиксом: путь с
    непустым content — обычный, успешный, и его текст часто разбирают програмно.
    Префикс сломал бы первый элемент разбора и любые проверки ``startswith``, тогда
    как хвост оставляет тело нетронутым до разделителя — оно восстанавливается
    ``answer.rpartition("\\n\\n")``. Полной прозрачности всё же нет: наивный
    ``answer.split(", ")`` утащит маркер в ПОСЛЕДНИЙ элемент, поэтому рецепт с
    отделением хвоста описан в ``docs/LLM_QUERY.md``, а не обойдён молчанием.

    Только OpenAI-совместимый путь: у Anthropic своя схема завершения
    (``stop_reason="max_tokens"``), там обрыв не отмечается — граница релиза.
    """
    finish_reason = getattr(choice, "finish_reason", None)
    if not isinstance(finish_reason, str) or finish_reason.strip().lower() != "length":
        return content
    return (
        f"{content}\n\n[TRUNCATED] Ответ оборван лимитом токенов (finish_reason=length) "
        f"и НЕПОЛОН. Администратору сервера — поднять {ENV_MAX_TOKENS}."
    )


def _openai_answer(response) -> str:
    """Ответ OpenAI-совместимого провайдера → строка. Никогда не бросает по форме."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)):
        return f"[ERROR] LLM вернула ответ неожидаемой формы: тип choices — {type(choices).__name__}"
    if not choices:
        return ""  # законно пустой ответ — сообщать нечего

    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        return f"[ERROR] LLM вернула ответ неожидаемой формы: тип choices[0] — {type(choice).__name__}"

    # Сентинел, а не truthiness: ``message=[]`` ложный, но это кривая форма, а не
    # «сообщения нет».
    content = getattr(message, "content", _MISSING)
    if content is _MISSING:
        return f"[ERROR] LLM вернула ответ неожидаемой формы: тип message — {type(message).__name__}"

    # Проверка типа ДО проверки на непустоту: иначе ложные значения не того типа
    # (``[]``, ``{}``, ``0``, ``false``) её обходят и уезжают в диагностику пустого
    # ответа — структурная ошибка провайдера подменяется правдоподобной причиной.
    # Имя типа без значения: в content может лежать нагрузка любого размера.
    if content is not None and not isinstance(content, str):
        return f"[ERROR] LLM вернула content неожидаемого типа: {type(content).__name__}"
    if content:
        return _mark_if_truncated(content, choice)

    return _empty_content_marker(response, choice, message)


# ── Anthropic provider (existing) ─────────────────────


def get_client() -> Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable is required for llm_query()")
    return Anthropic(api_key=api_key)


def make_llm_query(
    client: Anthropic | None = None,
    model: str | None = None,
    *,
    max_tokens: int | None = None,
):
    _client = client or get_client()
    _model = model or os.environ.get("RLM_SUB_MODEL", DEFAULT_MODEL)
    # ``RLM_LLM_MAX_TOKENS`` действует на ОБА провайдера. ``RLM_LLM_EXTRA_BODY`` —
    # только на OpenAI-совместимый: он про расширения именно того протокола.
    _max_tokens = max_tokens if max_tokens is not None else _resolved_max_tokens()

    def llm_query(prompt: str, context: str = "") -> str:
        if not prompt:
            raise ValueError("prompt cannot be empty")

        messages = []
        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}\n\nQuestion: {prompt}"})
        else:
            messages.append({"role": "user", "content": prompt})

        response = _client.messages.create(
            model=_model,
            max_tokens=_max_tokens,
            messages=messages,
        )
        if not response.content:
            return ""
        first = response.content[0]
        return getattr(first, "text", str(first))

    return llm_query


# ── OpenAI-compatible provider (new) ──────────────────


def _make_openai_query(
    base_url: str,
    api_key: str,
    model: str,
    *,
    max_tokens: int | None = None,
    extra_body: dict | None = None,
):
    """Фабрика OpenAI-совместимого ``llm_query``.

    ``max_tokens``/``extra_body`` — keyword-only, ``None`` означает «взять из
    окружения» (поэтому позиционные вызовы с тремя аргументами не ломаются).
    """
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError(
            "openai package is required for RLM_LLM_BASE_URL support. "
            "Install it: pip install rlm-tools-bsl[openai]  "
            "or: pip install openai"
        )

    _client = OpenAI(base_url=base_url, api_key=api_key or "no-key-required")
    _model = model
    _max_tokens = max_tokens if max_tokens is not None else _resolved_max_tokens()
    _extra_body = extra_body if extra_body is not None else _resolved_extra_body()

    def llm_query(prompt: str, context: str = "") -> str:
        if not prompt:
            raise ValueError("prompt cannot be empty")

        messages = []
        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}\n\nQuestion: {prompt}"})
        else:
            messages.append({"role": "user", "content": prompt})

        request_options = {"model": _model, "messages": messages, "max_tokens": _max_tokens}
        if _extra_body:
            request_options["extra_body"] = _extra_body

        response = _client.chat.completions.create(**request_options)
        return _openai_answer(response)

    return llm_query


# ── Unified factory ───────────────────────────────────


def get_llm_query_fn():
    """Auto-detect LLM provider from environment variables.

    Priority:
      1. RLM_LLM_BASE_URL + RLM_LLM_API_KEY + RLM_LLM_MODEL -> OpenAI-compatible
      2. ANTHROPIC_API_KEY -> Anthropic
      3. None -> llm_query unavailable
    """
    base_url = os.environ.get("RLM_LLM_BASE_URL")
    if base_url:
        model = os.environ.get("RLM_LLM_MODEL")
        if not model:
            logger.warning("RLM_LLM_BASE_URL is set but RLM_LLM_MODEL is missing; llm_query will not be available")
            return None
        api_key = os.environ.get("RLM_LLM_API_KEY", "")
        try:
            return _make_openai_query(base_url, api_key, model)
        except ImportError as e:
            logger.warning(str(e))
            return None

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return make_llm_query()
        except Exception as e:
            logger.warning(f"Could not create Anthropic llm_query: {e}")
            return None

    logger.info(
        "No LLM provider configured; set RLM_LLM_BASE_URL+RLM_LLM_MODEL or ANTHROPIC_API_KEY to enable llm_query"
    )
    return None


# ── Warmup (background pre-import) ────────────────────


_openai_warmup_done = False
_openai_warmup_lock = threading.Lock()


def warmup_openai_import():
    """Pre-cache openai in sys.modules. Safe to call multiple times."""
    global _openai_warmup_done
    if _openai_warmup_done:
        return
    with _openai_warmup_lock:
        if _openai_warmup_done:
            return
        try:
            import openai  # noqa: F401
        except ImportError:
            pass
        _openai_warmup_done = True


# ── Batched execution (unchanged) ─────────────────────


def make_llm_query_batched(llm_query_fn, max_workers: int = 8):
    def llm_query_batched(prompts: list[str], context: str = "") -> list[str]:
        if not prompts:
            return []

        results: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_idx = {executor.submit(llm_query_fn, prompt, context): i for i, prompt in enumerate(prompts)}
            for future in as_completed(future_to_idx):
                i = future_to_idx[future]
                try:
                    results[i] = future.result()
                except Exception as e:
                    results[i] = f"[ERROR] {type(e).__name__}: {e}"
        return [results[i] for i in range(len(prompts))]

    return llm_query_batched
