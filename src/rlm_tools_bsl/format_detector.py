from __future__ import annotations

import os
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
