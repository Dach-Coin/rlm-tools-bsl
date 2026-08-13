from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class SourceFormat(Enum):
    CF = "cf"
    EDT = "edt"
    UNKNOWN = "unknown"


@dataclass
class FormatInfo:
    primary_format: SourceFormat
    root_path: str
    bsl_file_count: int
    has_configuration_xml: bool
    metadata_categories_found: list[str]

    @property
    def format_label(self) -> str:
        return self.primary_format.value


@dataclass
class BslFileInfo:
    relative_path: str
    category: str | None
    object_name: str | None
    module_type: str | None
    form_name: str | None
    command_name: str | None
    is_form_module: bool


class SourceSupport(Enum):
    """Уровень поддержки дерева исходников."""

    SUPPORTED = "supported"
    FOREIGN_WITH_BSL = "foreign_with_bsl"
    FOREIGN_NO_BSL = "foreign_no_bsl"


UNSUPPORTED_FORMAT_SESSION_WARNING = (
    "== UNSUPPORTED SOURCE FORMAT ==\n"
    "These sources are NOT a Configurator dump (cf) and NOT a 1C:EDT project. "
    "Only cf and edt are supported.\n"
    "Metadata and object-name helpers are not reliable on this layout. "
    "Work from file paths and BSL text, treat empty metadata results as unsupported, "
    "and state that the answer is best-effort."
)

GENERIC_MODE_SESSION_WARNING = (
    "UNSUPPORTED SOURCE FORMAT: this directory is neither a Configurator dump (cf) "
    "nor a 1C:EDT project, and no .bsl file was found. "
    "Only generic file exploration is available in this session."
)

UNSUPPORTED_FORMAT_INDEX_WARNING = (
    "Формат исходников не распознан как cf (выгрузка Конфигуратора) или edt (1С:EDT). "
    "Индекс будет неполным: метаданные и навигация по объектам не гарантируются; "
    "будут доступны только данные, производные от текста BSL."
)

NO_BSL_INDEX_REFUSAL = (
    "Формат исходников не распознан как cf или edt, и ни одного .bsl-файла "
    "в дереве не найдено (либо дерево недоступно для чтения) — "
    "индекс строить не по чему."
)


METADATA_CATEGORIES: frozenset[str] = frozenset(
    {
        "CommonModules",
        "Documents",
        "Catalogs",
        "AccumulationRegisters",
        "InformationRegisters",
        "AccountingRegisters",
        "CalculationRegisters",
        "Reports",
        "DataProcessors",
        "Constants",
        "Enums",
        "ChartsOfAccounts",
        "ChartsOfCharacteristicTypes",
        "ChartsOfCalculationTypes",
        "CommonForms",
        "CommonCommands",
        "CommonTemplates",
        "HTTPServices",
        "WebServices",
        "BusinessProcesses",
        "Tasks",
        "ExchangePlans",
        "Roles",
        "DocumentJournals",
        "FilterCriteria",
        "SettingsStorages",
        "Subsystems",
        "XDTOPackages",
        "ExternalDataSources",
    }
)

MODULE_TYPE_MAP: dict[str, str] = {
    "Module.bsl": "Module",
    "ObjectModule.bsl": "ObjectModule",
    "ManagerModule.bsl": "ManagerModule",
    "RecordSetModule.bsl": "RecordSetModule",
    "CommandModule.bsl": "CommandModule",
    "ManagedApplicationModule.bsl": "ManagedApplicationModule",
    "OrdinaryApplicationModule.bsl": "OrdinaryApplicationModule",
    "SessionModule.bsl": "SessionModule",
    "ExternalConnectionModule.bsl": "ExternalConnectionModule",
    "ValueManagerModule.bsl": "ValueManagerModule",
}
_MODULE_TYPE_MAP_CASEFOLD: dict[str, str] = {
    name.casefold(): module_type for name, module_type in MODULE_TYPE_MAP.items()
}


def detect_format(base_path: str) -> FormatInfo:
    """Scans the top 2-3 levels of directory to quickly determine source format."""
    base = Path(base_path)
    bsl_file_count = 0
    has_configuration_xml = False
    has_ext_dir = False
    has_mdo_files = False
    categories_found: set[str] = set()

    for root, dirs, files in os.walk(base):
        # Compute current depth relative to base_path
        try:
            rel = Path(root).relative_to(base)
            depth = len(rel.parts)
        except ValueError:
            depth = 0

        # Limit walk depth: process files at all levels up to 4,
        # but don't descend beyond depth 3
        if depth >= 4:
            dirs.clear()
            continue
        if depth >= 3:
            dirs.clear()

        for fname in files:
            if fname.endswith(".bsl"):
                bsl_file_count += 1
            if fname == "Configuration.xml":
                has_configuration_xml = True
            if fname.endswith(".mdo"):
                has_mdo_files = True

        for dname in dirs:
            if dname == "Ext":
                has_ext_dir = True
            if dname in METADATA_CATEGORIES:
                categories_found.add(dname)

    # Determine format
    if has_configuration_xml and has_ext_dir:
        primary_format = SourceFormat.CF
    elif has_mdo_files and not has_ext_dir:
        primary_format = SourceFormat.EDT
    else:
        primary_format = SourceFormat.UNKNOWN

    return FormatInfo(
        primary_format=primary_format,
        root_path=str(base),
        bsl_file_count=bsl_file_count,
        has_configuration_xml=has_configuration_xml,
        metadata_categories_found=sorted(categories_found),
    )


_CF_NS = "http://v8.1c.ru/8.3/MDClasses"
_EDT_NS = "http://g5.1c.ru/v8/dt/metadata/mdclass"


def _is_cf_descriptor(path: Path) -> bool:
    """Сигнатура CF: корень {MDClasses}MetaDataObject, первый дочерний — Configuration.

    Ранний выход: читаются только два первых start-события iterparse, то есть
    префикс файла. Многомегабайтный боевой Configuration.xml целиком не читается.

    LookupError ловится наравне с ParseError: незнакомая кодировка в XML-декларации
    (`encoding="x-invalid"`) — это НЕ наш дескриптор, а не повод уронить гейт
    необработанным исключением. Гейт обязан быть тотальным: он стоит на входе
    сессии и построения индекса, и любой нечитаемый файл для него значит «нет».
    """
    try:
        with open(path, "rb") as fh:
            events = ET.iterparse(fh, events=("start",))
            _, root = next(events)
            if root.tag != f"{{{_CF_NS}}}MetaDataObject":
                return False
            _, child = next(events)
            return child.tag == f"{{{_CF_NS}}}Configuration"
    except (OSError, ET.ParseError, LookupError, StopIteration):
        return False


def _is_edt_descriptor(path: Path) -> bool:
    """Сигнатура EDT: корень {mdclass}Configuration. Читается только префикс файла.

    Набор перехватываемых исключений — тот же, что у CF (см. `_is_cf_descriptor`).
    """
    try:
        with open(path, "rb") as fh:
            _, root = next(ET.iterparse(fh, events=("start",)))
            return root.tag == f"{{{_EDT_NS}}}Configuration"
    except (OSError, ET.ParseError, LookupError, StopIteration):
        return False


def _subdirs(path: Path) -> list[Path]:
    """Видимые подкаталоги одним scandir; недоступный каталог — пусто."""
    try:
        with os.scandir(path) as it:
            return [Path(entry.path) for entry in it if entry.is_dir() and not entry.name.startswith(".")]
    except OSError:
        return []


def _candidate_config_roots(base: Path):
    """Корень, прямые дети и один уровень обертки (BFS).

    Контракт скорости: листинг каталогов — только уровни 0 и 1. Кандидаты
    уровня 2 проверяются двумя точечными open() без листинга их содержимого:
    os.walk листил бы тысячи объектных каталогов чужого дерева (~800 мс).
    """
    yield base
    level1 = _subdirs(base)
    yield from level1
    for child in level1:
        yield from _subdirs(child)


def has_our_format_descriptor(base_path: str) -> bool:
    """Есть ли валидный дескриптор CF/EDT в поддерживаемой раскладке."""
    base = Path(base_path)
    if not base.is_dir():
        return False
    for root in _candidate_config_roots(base):
        if _is_cf_descriptor(root / "Configuration.xml"):
            return True
        if _is_edt_descriptor(root / "Configuration" / "Configuration.mdo"):
            return True
    return False


def probe_bsl(base_path: str) -> str:
    """Возвращает found/none/unknown и выходит на первом индексируемом .bsl."""
    unreadable = False

    def _on_error(_exc: OSError) -> None:
        nonlocal unreadable
        unreadable = True

    base = Path(base_path)
    if not base.is_dir():
        return "unknown"

    for _root, _dirs, files in os.walk(base, onerror=_on_error):
        for fname in files:
            if os.path.normcase(fname).endswith(".bsl"):
                return "found"
    return "unknown" if unreadable else "none"


def classify_source(base_path: str) -> SourceSupport:
    """Классифицирует дерево только по живому диску."""
    if has_our_format_descriptor(base_path):
        return SourceSupport.SUPPORTED
    return SourceSupport.FOREIGN_NO_BSL if probe_bsl(base_path) == "none" else SourceSupport.FOREIGN_WITH_BSL


def parse_bsl_path(file_path: str, base_path: str) -> BslFileInfo:
    """Universal parser for .bsl file paths."""
    # Быстрый путь: строковый префикс вместо Path.relative_to. Функция зовется на
    # КАЖДЫЙ модуль — и билдером при сборке, и живым каталогом хелперов, — а два
    # объекта Path нужны здесь исключительно ради относительного пути (замер: 23 461
    # вызов = 2.4 с только внутри relative_to). Откат на прежнюю ветку — при любом
    # нетривиальном пути (несовпадение регистра, `//`, `/./`, `/../`), поэтому
    # семантика не меняется, меняется только цена типичного случая.
    relative_path: str | None = None
    if base_path:
        # Обратный слэш — разделитель ТОЛЬКО на Windows. На POSIX это допустимый
        # символ имени файла, и `PurePosixPath` его разделителем не считает: слепой
        # replace превратил бы `Name\Ext\Module.bsl` в несуществующий путь из трёх
        # сегментов, а билдер сохранил бы такой `rel_path` в индекс.
        norm_fp = file_path.replace("\\", "/") if os.name == "nt" else file_path
        norm_bp = (base_path.replace("\\", "/") if os.name == "nt" else base_path).rstrip("/")
        # База, которую `Path.relative_to` НЕ принимает как якорь, и потому старая
        # ветка отдавала полный путь: голый диск (`C:` — он drive-RELATIVE, а не
        # корень) и UNC-хост без шары (`//server`). Быстрый префикс тут срезал бы
        # лишнее и разошёлся бы со старым поведением.
        drive_relative = norm_bp.endswith(":")
        unc_hostonly = norm_bp.startswith("//") and norm_bp.count("/") < 3
        if not drive_relative and not unc_hostonly and norm_fp.startswith(norm_bp + "/"):
            tail = norm_fp[len(norm_bp) + 1 :]
            # `tail.startswith("/")` — это двойной разделитель РОВНО на границе базы
            # (`D:/a/b//X`): внутри tail его уже не видно, а Path такой путь схлопывает.
            # `tail.endswith("/")` — хвостовой разделитель, который Path тоже схлопывает
            # (иначе появился бы пустой сегмент и поехал бы разбор `parts`).
            if (
                tail
                and not tail.startswith(("/", "./", "../"))
                and not tail.endswith("/")
                and "//" not in tail
                and "/./" not in tail
                and "/../" not in tail
            ):
                relative_path = tail
    if relative_path is None:
        fp = Path(file_path)
        bp = Path(base_path)

        # Compute relative path and normalize to forward slashes
        try:
            rel = fp.relative_to(bp)
        except ValueError:
            rel = fp

        relative_path = rel.as_posix()
    parts = relative_path.split("/")

    category: str | None = None
    object_name: str | None = None
    form_name: str | None = None
    command_name: str | None = None
    module_type: str | None = None

    # Find category in parts
    for i, part in enumerate(parts):
        if part in METADATA_CATEGORIES:
            category = part
            # Next part is the object name (if it exists and is not a known subdir)
            if i + 1 < len(parts) - 1:  # not the last part (last part is the filename)
                object_name = parts[i + 1]
            break

    # Detect CF-style path: presence of "Ext" directory
    # In CF paths, Ext appears after the object name folder
    # e.g. CommonModules/MyModule/Ext/Module.bsl
    # We already extracted object_name from the part after category; keep it as-is.

    # Extract form_name: part after "Forms" in the path
    if "Forms" in parts:
        forms_index = parts.index("Forms")
        if forms_index + 1 < len(parts) - 1:
            # part after "Forms" and before the filename
            form_name = parts[forms_index + 1]
        elif forms_index + 1 == len(parts) - 1:
            # The next part might be the filename itself if it's a form module
            # In EDT style: Forms/MyForm.bsl  -> form_name = "MyForm" (strip extension)
            candidate = parts[forms_index + 1]
            if candidate.casefold().endswith(".bsl"):
                form_name = candidate[:-4]
            else:
                form_name = candidate

    # Extract command_name: part after "Commands" in the path
    if "Commands" in parts:
        commands_index = parts.index("Commands")
        if commands_index + 1 < len(parts) - 1:
            command_name = parts[commands_index + 1]
        elif commands_index + 1 == len(parts) - 1:
            candidate = parts[commands_index + 1]
            if candidate.casefold().endswith(".bsl"):
                command_name = candidate[:-4]
            else:
                command_name = candidate

    # Get filename and look up module type
    filename = parts[-1]
    module_type = _MODULE_TYPE_MAP_CASEFOLD.get(filename.casefold())

    # is_form_module: True when this .bsl belongs to a form
    is_form_module = form_name is not None

    return BslFileInfo(
        relative_path=relative_path,
        category=category,
        object_name=object_name,
        module_type=module_type,
        form_name=form_name,
        command_name=command_name,
        is_form_module=is_form_module,
    )
