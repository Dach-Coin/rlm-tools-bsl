from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class SourceFormat(Enum):
    CF = "cf"
    EDT = "edt"
    V8UNPACK = "v8unpack"
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
        "Sequences",
    }
)

V8UNPACK_CATEGORY_MAP: dict[str, str] = {
    "AccountingRegister": "AccountingRegisters",
    "AccumulationRegister": "AccumulationRegisters",
    "BusinessProcess": "BusinessProcesses",
    "CalculationRegister": "CalculationRegisters",
    "Catalog": "Catalogs",
    "ChartOfAccounts": "ChartsOfAccounts",
    "ChartOfCalculationTypes": "ChartsOfCalculationTypes",
    "ChartOfCharacteristicType": "ChartsOfCharacteristicTypes",
    "CommonCommand": "CommonCommands",
    "CommonForm": "CommonForms",
    "CommonModule": "CommonModules",
    "Constant": "Constants",
    "DataProcessor": "DataProcessors",
    "Document": "Documents",
    "DocumentJournal": "DocumentJournals",
    "Enum": "Enums",
    "ExchangePlan": "ExchangePlans",
    "ExternalDataSource": "ExternalDataSources",
    "FilterCriterion": "FilterCriteria",
    "HTTPService": "HTTPServices",
    "InformationRegister": "InformationRegisters",
    "Report": "Reports",
    "SettingsStorage": "SettingsStorages",
    "Task": "Tasks",
    "WebService": "WebServices",
    "Sequences": "Sequences",
}

V8UNPACK_ROOT_MODULES: dict[str, str] = {
    "configuration.802.bsl": "ManagedApplicationModule",
    "configuration.app.bsl": "OrdinaryApplicationModule",
    "configuration.seance.bsl": "SessionModule",
    "configuration.con.bsl": "ExternalConnectionModule",
}

_V8UNPACK_RECORD_SET_CATEGORIES = {
    "AccountingRegisters",
    "AccumulationRegisters",
    "CalculationRegisters",
    "InformationRegisters",
    "Sequences",
}

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
    has_v8unpack_bsl_structure = False
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
                if (
                    (rel.parts and rel.parts[0] in V8UNPACK_CATEGORY_MAP)
                    or (not rel.parts and fname.casefold() in V8UNPACK_ROOT_MODULES)
                ):
                    has_v8unpack_bsl_structure = True
            if fname == "Configuration.xml":
                has_configuration_xml = True
            if fname.endswith(".mdo"):
                has_mdo_files = True

        for dname in dirs:
            if dname == "Ext":
                has_ext_dir = True
            if dname in METADATA_CATEGORIES:
                categories_found.add(dname)
            canonical_category = V8UNPACK_CATEGORY_MAP.get(dname)
            if canonical_category:
                categories_found.add(canonical_category)

    # Determine format
    if has_configuration_xml and has_ext_dir:
        primary_format = SourceFormat.CF
    elif has_mdo_files and not has_ext_dir:
        primary_format = SourceFormat.EDT
    elif has_v8unpack_bsl_structure and _has_v8unpack_marker(base):
        primary_format = SourceFormat.V8UNPACK
    else:
        primary_format = SourceFormat.UNKNOWN

    return FormatInfo(
        primary_format=primary_format,
        root_path=str(base),
        bsl_file_count=bsl_file_count,
        has_configuration_xml=has_configuration_xml,
        metadata_categories_found=sorted(categories_found),
    )


def _has_v8unpack_marker(base: Path) -> bool:
    try:
        with (base / "Configuration.json").open(encoding="utf-8-sig") as stream:
            descriptor = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    marker = descriptor.get("v8unpack") if isinstance(descriptor, dict) else None
    return isinstance(marker, str) and bool(marker.strip())


def parse_bsl_path(file_path: str, base_path: str) -> BslFileInfo:
    """Universal parser for .bsl file paths."""
    fp = Path(file_path)
    bp = Path(base_path)

    # Compute relative path and normalize to forward slashes
    try:
        rel = fp.relative_to(bp)
    except ValueError:
        rel = fp

    relative_path = rel.as_posix()
    parts = relative_path.split("/")
    filename = parts[-1]
    filename_casefold = filename.casefold()

    category: str | None = None
    object_name: str | None = None
    form_name: str | None = None
    command_name: str | None = None
    module_type: str | None = None
    is_v8unpack = len(parts) >= 3 and parts[0] in V8UNPACK_CATEGORY_MAP

    if is_v8unpack:
        category = V8UNPACK_CATEGORY_MAP[parts[0]]
        object_name = parts[1]
    else:
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
    if (
        is_v8unpack
        and category == "CommonForms"
        and filename_casefold == "commonform.obj.bsl"
    ):
        form_name = object_name
    elif is_v8unpack:
        for i, part in enumerate(parts[2:-1], start=2):
            if (
                (part == "Form" or part.endswith("Form"))
                and i + 1 < len(parts) - 1
                and filename_casefold == f"{part}.obj.bsl".casefold()
            ):
                form_name = parts[i + 1]
                break
    elif "Forms" in parts:
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
    if (
        is_v8unpack
        and category == "CommonCommands"
        and filename_casefold == "commoncommand.obj.bsl"
    ):
        command_name = object_name
    elif is_v8unpack:
        for i, part in enumerate(parts[2:-1], start=2):
            if (
                part.endswith("Command")
                and i + 1 < len(parts) - 1
                and filename_casefold == f"{part}.obj.bsl".casefold()
            ):
                command_name = parts[i + 1]
                break
    elif "Commands" in parts:
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
    if len(parts) == 1:
        module_type = V8UNPACK_ROOT_MODULES.get(filename_casefold)
    if module_type is None and is_v8unpack:
        if form_name is not None:
            module_type = "Module"
        elif command_name is not None:
            module_type = "CommandModule"
        elif category == "CommonModules" and filename_casefold == "commonmodule.obj.bsl":
            module_type = "Module"
        elif filename_casefold.endswith(".mgr.bsl"):
            module_type = "ManagerModule"
        elif filename_casefold.endswith(".obj.bsl"):
            if category in _V8UNPACK_RECORD_SET_CATEGORIES:
                module_type = "RecordSetModule"
            elif category == "Constants":
                module_type = "ValueManagerModule"
            elif category in {"Enums", "FilterCriteria", "DocumentJournals"}:
                module_type = "ManagerModule"
            elif category in {"HTTPServices", "WebServices"}:
                module_type = "Module"
            else:
                module_type = "ObjectModule"
    if module_type is None:
        module_type = _MODULE_TYPE_MAP_CASEFOLD.get(filename_casefold)

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
