from __future__ import annotations
import json
import logging
import xml.etree.ElementTree as ET
from rlm_tools_bsl.format_detector import METADATA_CATEGORIES

logger = logging.getLogger(__name__)


# Namespace maps for 1C metadata XML
# CF format (Platform Export / Конфигуратор)
_NS_CF = {
    "md": "http://v8.1c.ru/8.3/MDClasses",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xr": "http://v8.1c.ru/8.3/xcf/readable",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
}

# MDO format (EDT / 1C:DT)
_NS_MDO = {
    "mdclass": "http://g5.1c.ru/v8/dt/metadata/mdclass",
    "mdext": "http://g5.1c.ru/v8/dt/metadata/mdclass/extension",
    "core": "http://g5.1c.ru/v8/dt/mcore",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}

_MDO_NS_URI = "http://g5.1c.ru/v8/dt/metadata/mdclass"
_CF_NS_URI = "http://v8.1c.ru/8.3/MDClasses"

# Aliases for metadata categories: singular -> plural, Russian -> English
_CATEGORY_ALIASES: dict[str, str] = {
    # English singular -> plural
    "informationregister": "informationregisters",
    "accumulationregister": "accumulationregisters",
    "accountingregister": "accountingregisters",
    "calculationregister": "calculationregisters",
    "document": "documents",
    "catalog": "catalogs",
    "report": "reports",
    "dataprocessor": "dataprocessors",
    "commonmodule": "commonmodules",
    "constant": "constants",
    # Russian aliases
    "регистрсведений": "informationregisters",
    "регистрнакопления": "accumulationregisters",
    "регистрбухгалтерии": "accountingregisters",
    "регистррасчета": "calculationregisters",
    "документ": "documents",
    "справочник": "catalogs",
    "отчет": "reports",
    "обработка": "dataprocessors",
    "общиймодуль": "commonmodules",
    "константа": "constants",
    # XDTO packages
    "xdtopackage": "xdtopackages",
    "пакетxdto": "xdtopackages",
    # External data sources
    "externaldatasource": "externaldatasources",
    "внешнийисточникданных": "externaldatasources",
}


def _normalize_category(meta_type: str) -> str:
    """Normalize a metadata category name to the canonical folder form."""
    key = meta_type.lower().replace(" ", "").replace("_", "")
    resolved = _CATEGORY_ALIASES.get(key)
    if resolved:
        return resolved
    # Fallback: if it doesn't end with 's', try adding 's'
    if not key.endswith("s"):
        candidate = key + "s"
        if candidate in {c.lower() for c in METADATA_CATEGORIES}:
            return candidate
    return key


def _xml_find_text(element, tag: str, ns: dict) -> str:
    """Find text of a child element, return '' if not found."""
    el = element.find(tag, ns)
    return el.text.strip() if el is not None and el.text else ""


def _xml_direct_text(element, child_name: str) -> str:
    """Find direct child by local name, return text."""
    for ch in element:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == child_name:
            return ch.text.strip() if ch.text else ""
    return ""


# --- CF format helpers ---


def _cf_find_synonym(props, ns: dict = _NS_CF) -> str:
    """Extract ru synonym from CF Properties element."""
    syn = props.find("md:Synonym", ns)
    if syn is None:
        return ""
    for item in syn.findall("v8:item", ns):
        lang = _xml_find_text(item, "v8:lang", ns)
        if lang == "ru":
            return _xml_find_text(item, "v8:content", ns)
    return ""


def _cf_parse_type(props, ns: dict = _NS_CF) -> str:
    """Extract type string from CF <Type> element."""
    type_el = props.find("md:Type", ns)
    if type_el is None:
        return ""
    types = []
    for t in type_el.findall("v8:Type", ns):
        if t.text:
            types.append(t.text.strip())
    return ", ".join(types)


_XS_TYPE_MAP: dict[str, str] = {
    "xs:string": "String",
    "xs:decimal": "Number",
    "xs:boolean": "Boolean",
    "xs:dateTime": "DateTime",
    "xs:base64Binary": "ValueStorage",
}


def _strip_ns_prefix(t: str) -> str:
    """Strip XML namespace prefix: 'cfg:CatalogRef.X' -> 'CatalogRef.X'."""
    if ":" in t:
        return t.split(":", 1)[1]
    return t


def normalize_type_string(raw: str) -> str:
    """Normalize 1C type string to JSON array.

    Input: comma-separated types from parse_metadata_xml, e.g.
        "cfg:CatalogRef.X, xs:string, d4p1:DocumentRef.Y"
    Output: JSON array string, e.g.
        '["CatalogRef.X", "String", "DocumentRef.Y"]'
    """
    if not raw or not raw.strip():
        return "[]"
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    normalized = []
    for p in parts:
        mapped = _XS_TYPE_MAP.get(p)
        if mapped:
            normalized.append(mapped)
        else:
            normalized.append(_strip_ns_prefix(p))
    return json.dumps(normalized, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Reference type canonicalization (v1.9.0)
# ---------------------------------------------------------------------------

# Map: 1C type-form suffix -> canonical metadata category (singular)
_REF_TYPE_SUFFIXES: dict[str, str] = {
    "Ref": "",
    "Object": "",
    "Manager": "",
    "List": "",
    "Selection": "",
    "RecordSet": "",
    "RecordKey": "",
    "RecordManager": "",
    "Record": "",
    "MetadataObject": "",
}

# Map: 1C type-form prefix -> canonical metadata category
_REF_CANONICAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("CatalogRef.", "Catalog."),
    ("CatalogObject.", "Catalog."),
    ("CatalogManager.", "Catalog."),
    ("CatalogList.", "Catalog."),
    ("CatalogSelection.", "Catalog."),
    ("DocumentRef.", "Document."),
    ("DocumentObject.", "Document."),
    ("DocumentManager.", "Document."),
    ("DocumentList.", "Document."),
    ("DocumentSelection.", "Document."),
    ("EnumRef.", "Enum."),
    ("EnumManager.", "Enum."),
    ("EnumList.", "Enum."),
    ("InformationRegisterRecordSet.", "InformationRegister."),
    ("InformationRegisterRecordKey.", "InformationRegister."),
    ("InformationRegisterManager.", "InformationRegister."),
    ("InformationRegisterRecordManager.", "InformationRegister."),
    ("InformationRegisterList.", "InformationRegister."),
    ("InformationRegisterSelection.", "InformationRegister."),
    ("AccumulationRegisterRecordSet.", "AccumulationRegister."),
    ("AccumulationRegisterRecordKey.", "AccumulationRegister."),
    ("AccumulationRegisterManager.", "AccumulationRegister."),
    ("AccumulationRegisterList.", "AccumulationRegister."),
    ("AccountingRegisterRecordSet.", "AccountingRegister."),
    ("AccountingRegisterRecordKey.", "AccountingRegister."),
    ("AccountingRegisterManager.", "AccountingRegister."),
    ("AccountingRegisterList.", "AccountingRegister."),
    ("CalculationRegisterRecordSet.", "CalculationRegister."),
    ("CalculationRegisterRecordKey.", "CalculationRegister."),
    ("CalculationRegisterManager.", "CalculationRegister."),
    ("CalculationRegisterList.", "CalculationRegister."),
    ("ChartOfAccountsRef.", "ChartOfAccounts."),
    ("ChartOfAccountsObject.", "ChartOfAccounts."),
    ("ChartOfAccountsManager.", "ChartOfAccounts."),
    ("ChartOfAccountsList.", "ChartOfAccounts."),
    ("ChartOfAccountsSelection.", "ChartOfAccounts."),
    ("ChartOfCharacteristicTypesRef.", "ChartOfCharacteristicTypes."),
    ("ChartOfCharacteristicTypesObject.", "ChartOfCharacteristicTypes."),
    ("ChartOfCharacteristicTypesManager.", "ChartOfCharacteristicTypes."),
    ("ChartOfCharacteristicTypesList.", "ChartOfCharacteristicTypes."),
    ("ChartOfCharacteristicTypesSelection.", "ChartOfCharacteristicTypes."),
    ("ChartOfCalculationTypesRef.", "ChartOfCalculationTypes."),
    ("ChartOfCalculationTypesObject.", "ChartOfCalculationTypes."),
    ("ChartOfCalculationTypesManager.", "ChartOfCalculationTypes."),
    ("ChartOfCalculationTypesList.", "ChartOfCalculationTypes."),
    ("ExchangePlanRef.", "ExchangePlan."),
    ("ExchangePlanObject.", "ExchangePlan."),
    ("ExchangePlanManager.", "ExchangePlan."),
    ("ExchangePlanList.", "ExchangePlan."),
    ("ExchangePlanSelection.", "ExchangePlan."),
    ("BusinessProcessRef.", "BusinessProcess."),
    ("BusinessProcessObject.", "BusinessProcess."),
    ("BusinessProcessManager.", "BusinessProcess."),
    ("BusinessProcessList.", "BusinessProcess."),
    ("TaskRef.", "Task."),
    ("TaskObject.", "Task."),
    ("TaskManager.", "Task."),
    ("TaskList.", "Task."),
    ("ConstantManager.", "Constant."),
    ("ConstantValueManager.", "Constant."),
    ("DefinedType.", "DefinedType."),
)

_CANONICAL_REF_HEADS = (
    "Catalog",
    "Document",
    "Enum",
    "InformationRegister",
    "AccumulationRegister",
    "AccountingRegister",
    "CalculationRegister",
    "ChartOfAccounts",
    "ChartOfCharacteristicTypes",
    "ChartOfCalculationTypes",
    "ExchangePlan",
    "BusinessProcess",
    "Task",
    "Constant",
    "DefinedType",
    "Subsystem",
    "Role",
    "FunctionalOption",
    "EventSubscription",
    "ScheduledJob",
    "CommonModule",
    "CommonForm",
    "CommonCommand",
    "Report",
    "DataProcessor",
    "DocumentJournal",
)
_CANONICAL_REF_HEAD_BY_CASEFOLD = {head.casefold(): head for head in _CANONICAL_REF_HEADS}


def canonicalize_type_ref(type_str: str, *, fold_case: bool = True) -> str:
    """Convert 1C reference type form to canonical metadata reference.

    Examples:
        "cfg:CatalogRef.Контрагенты"   -> "Catalog.Контрагенты"
        "DocumentObject.Заказ"          -> "Document.Заказ"
        "document.Заказ"                -> "Document.Заказ"
        "EnumRef.СтавкиНДС"             -> "Enum.СтавкиНДС"
        "DefinedType.Сумма"             -> "DefinedType.Сумма"
        "xs:string"                     -> "" (not a metadata reference)
        "String"                        -> "" (primitive type)
    """
    if not type_str:
        return ""
    s = _strip_ns_prefix(type_str.strip())
    if not s or "." not in s:
        return ""
    for prefix, canonical in _REF_CANONICAL_PREFIXES:
        prefix_matches = s[: len(prefix)].casefold() == prefix.casefold() if fold_case else s.startswith(prefix)
        if prefix_matches:
            return canonical + s[len(prefix) :]
    # Already canonical form like "Catalog.X"?
    head, suffix = s.split(".", 1)
    canonical_head = _CANONICAL_REF_HEAD_BY_CASEFOLD.get(head.casefold()) if fold_case else head
    if not fold_case and canonical_head not in _CANONICAL_REF_HEADS:
        canonical_head = ""
    if canonical_head:
        return f"{canonical_head}.{suffix}"
    return ""


# ---------------------------------------------------------------------------
# Russian/English metadata FORMS for reverse code-usage extraction (v1.14.0)
#
# Single source of truth for the code-derived `metadata_code_usages` table:
#   - ``plural`` (RU/EN)   — collection manager accessor in code:
#                            ``Документы.X`` / ``Documents.X`` -> usage_kind 'manager'
#   - ``singular`` (RU/EN) — metadata path inside a query string literal:
#                            ``Документ.X`` / ``Документ.X.Товары`` -> usage_kind 'query'
#   - ``reftypes`` (RU)    — string-literal reference type prefixes:
#                            ``"ДокументСсылка.X"`` -> usage_kind 'ref_type'
#                            (EN reftypes like ``DocumentRef.`` are covered by
#                            canonicalize_type_ref — do NOT duplicate them here).
#
# IMPORTANT: Russian plural/reftype forms are irregular (РегистрСведений ->
# РегистрыСведений, Обработка -> Обработки) — they are NOT derived by a rule and
# must be listed explicitly. Only categories reachable via a manager collection
# in code are included; metadata-only categories (Subsystem, Role, …) are NOT
# code-addressable and are intentionally out of this table.
_RU_META_FORMS: dict[str, dict] = {
    "Catalog.": {
        "ru_singular": "Справочник",
        "ru_plural": "Справочники",
        "en_singular": "Catalog",
        "en_plural": "Catalogs",
        "reftypes": ["СправочникСсылка", "СправочникОбъект", "СправочникМенеджер"],
    },
    "Document.": {
        "ru_singular": "Документ",
        "ru_plural": "Документы",
        "en_singular": "Document",
        "en_plural": "Documents",
        "reftypes": ["ДокументСсылка", "ДокументОбъект", "ДокументМенеджер"],
    },
    "Enum.": {
        "ru_singular": "Перечисление",
        "ru_plural": "Перечисления",
        "en_singular": "Enum",
        "en_plural": "Enums",
        "reftypes": ["ПеречислениеСсылка", "ПеречислениеМенеджер"],
    },
    "InformationRegister.": {
        "ru_singular": "РегистрСведений",
        "ru_plural": "РегистрыСведений",
        "en_singular": "InformationRegister",
        "en_plural": "InformationRegisters",
        "reftypes": [
            "РегистрСведенийЗапись",
            "РегистрСведенийКлючЗаписи",
            "РегистрСведенийМенеджер",
            "РегистрСведенийНаборЗаписей",
        ],
    },
    "AccumulationRegister.": {
        "ru_singular": "РегистрНакопления",
        "ru_plural": "РегистрыНакопления",
        "en_singular": "AccumulationRegister",
        "en_plural": "AccumulationRegisters",
        "reftypes": [
            "РегистрНакопленияЗапись",
            "РегистрНакопленияКлючЗаписи",
            "РегистрНакопленияМенеджер",
            "РегистрНакопленияНаборЗаписей",
        ],
    },
    "AccountingRegister.": {
        "ru_singular": "РегистрБухгалтерии",
        "ru_plural": "РегистрыБухгалтерии",
        "en_singular": "AccountingRegister",
        "en_plural": "AccountingRegisters",
        "reftypes": [
            "РегистрБухгалтерииЗапись",
            "РегистрБухгалтерииКлючЗаписи",
            "РегистрБухгалтерииМенеджер",
            "РегистрБухгалтерииНаборЗаписей",
        ],
    },
    "CalculationRegister.": {
        "ru_singular": "РегистрРасчета",
        "ru_plural": "РегистрыРасчета",
        "en_singular": "CalculationRegister",
        "en_plural": "CalculationRegisters",
        "reftypes": [
            "РегистрРасчетаЗапись",
            "РегистрРасчетаКлючЗаписи",
            "РегистрРасчетаМенеджер",
            "РегистрРасчетаНаборЗаписей",
        ],
    },
    "ChartOfCharacteristicTypes.": {
        "ru_singular": "ПланВидовХарактеристик",
        "ru_plural": "ПланыВидовХарактеристик",
        "en_singular": "ChartOfCharacteristicTypes",
        "en_plural": "ChartsOfCharacteristicTypes",
        "reftypes": [
            "ПланВидовХарактеристикСсылка",
            "ПланВидовХарактеристикОбъект",
            "ПланВидовХарактеристикМенеджер",
        ],
    },
    "ChartOfAccounts.": {
        "ru_singular": "ПланСчетов",
        "ru_plural": "ПланыСчетов",
        "en_singular": "ChartOfAccounts",
        "en_plural": "ChartsOfAccounts",
        "reftypes": ["ПланСчетовСсылка", "ПланСчетовОбъект", "ПланСчетовМенеджер"],
    },
    "ChartOfCalculationTypes.": {
        "ru_singular": "ПланВидовРасчета",
        "ru_plural": "ПланыВидовРасчета",
        "en_singular": "ChartOfCalculationTypes",
        "en_plural": "ChartsOfCalculationTypes",
        "reftypes": [
            "ПланВидовРасчетаСсылка",
            "ПланВидовРасчетаОбъект",
            "ПланВидовРасчетаМенеджер",
        ],
    },
    "ExchangePlan.": {
        "ru_singular": "ПланОбмена",
        "ru_plural": "ПланыОбмена",
        "en_singular": "ExchangePlan",
        "en_plural": "ExchangePlans",
        "reftypes": ["ПланОбменаСсылка", "ПланОбменаОбъект", "ПланОбменаМенеджер"],
    },
    "BusinessProcess.": {
        "ru_singular": "БизнесПроцесс",
        "ru_plural": "БизнесПроцессы",
        "en_singular": "BusinessProcess",
        "en_plural": "BusinessProcesses",
        "reftypes": ["БизнесПроцессСсылка", "БизнесПроцессОбъект", "БизнесПроцессМенеджер"],
    },
    "Task.": {
        "ru_singular": "Задача",
        "ru_plural": "Задачи",
        "en_singular": "Task",
        "en_plural": "Tasks",
        "reftypes": ["ЗадачаСсылка", "ЗадачаОбъект", "ЗадачаМенеджер"],
    },
    "Report.": {
        "ru_singular": "Отчет",
        "ru_plural": "Отчеты",
        "en_singular": "Report",
        "en_plural": "Reports",
        "reftypes": [],
    },
    "DataProcessor.": {
        "ru_singular": "Обработка",
        "ru_plural": "Обработки",
        "en_singular": "DataProcessor",
        "en_plural": "DataProcessors",
        "reftypes": [],
    },
    "Constant.": {
        "ru_singular": "Константа",
        "ru_plural": "Константы",
        "en_singular": "Constant",
        "en_plural": "Constants",
        "reftypes": [],
    },
}


def _build_code_collection_map() -> dict[str, str]:
    """lower(collection accessor) -> canonical prefix, for 'manager' and 'query'.

    Plural forms (Документы/Documents) drive manager-collection detection;
    singular forms (Документ/Document) drive query-path detection. Both share
    the same lookup table because plural and singular tokens never collide.
    """
    out: dict[str, str] = {}
    for canonical, forms in _RU_META_FORMS.items():
        for key in ("ru_plural", "en_plural", "ru_singular", "en_singular"):
            tok = forms.get(key)
            if tok:
                out[tok.lower()] = canonical
    return out


def _build_manager_collection_map() -> dict[str, str]:
    """lower(plural accessor) -> canonical prefix (manager usages only)."""
    out: dict[str, str] = {}
    for canonical, forms in _RU_META_FORMS.items():
        for key in ("ru_plural", "en_plural"):
            tok = forms.get(key)
            if tok:
                out[tok.lower()] = canonical
    return out


def _build_query_collection_map() -> dict[str, str]:
    """lower(singular accessor) -> canonical prefix (query-path usages only)."""
    out: dict[str, str] = {}
    for canonical, forms in _RU_META_FORMS.items():
        for key in ("ru_singular", "en_singular"):
            tok = forms.get(key)
            if tok:
                out[tok.lower()] = canonical
    return out


def _build_ru_reftype_map() -> dict[str, str]:
    """lower(RU reftype prefix incl. trailing dot) -> canonical prefix."""
    out: dict[str, str] = {}
    for canonical, forms in _RU_META_FORMS.items():
        for rt in forms.get("reftypes", []):
            out[rt.lower() + "."] = canonical
    return out


# Derived lookup tables (built once at import).
_CODE_MANAGER_COLLECTIONS: dict[str, str] = _build_manager_collection_map()
_CODE_QUERY_COLLECTIONS: dict[str, str] = _build_query_collection_map()
_RU_REFTYPE_TO_CANONICAL: dict[str, str] = _build_ru_reftype_map()


def _cf_parse_attributes(parent, ns: dict = _NS_CF) -> list[dict]:
    """Parse CF <Attribute> elements under parent."""
    attrs = []
    for attr_el in parent.findall("md:Attribute", ns):
        props = attr_el.find("md:Properties", ns)
        if props is None:
            continue
        attrs.append(
            {
                "name": _xml_find_text(props, "md:Name", ns),
                "synonym": _cf_find_synonym(props, ns),
                "type": _cf_parse_type(props, ns),
            }
        )
    return attrs


def _ref_each(raw: str):
    """Iterate canonical refs found in a comma-separated raw type string."""
    if not raw:
        return
    for part in raw.split(","):
        canon = canonicalize_type_ref(part)
        if canon:
            yield canon


def _parse_cf_xml(root) -> dict:
    """Parse CF-format metadata XML (Platform Export / Конфигуратор)."""
    ns = _NS_CF

    meta_el = None
    for child in root:
        if child.find("md:Properties", ns) is not None:
            meta_el = child
            break
        if child.find("{http://v8.1c.ru/8.3/MDClasses}Properties") is not None:
            meta_el = child
            break
    if meta_el is None:
        for child in root:
            for sub in child:
                sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                if sub_tag == "Properties":
                    meta_el = child
                    break
            if meta_el is not None:
                break

    if meta_el is None:
        return {"error": "Could not find metadata object in XML"}

    meta_tag = meta_el.tag.split("}")[-1] if "}" in meta_el.tag else meta_el.tag

    props = meta_el.find("md:Properties", ns)
    if props is None:
        for ch in meta_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break

    result: dict = {
        "object_type": meta_tag,
        "name": _xml_find_text(props, "md:Name", ns) if props is not None else "",
        "synonym": _cf_find_synonym(props, ns) if props is not None else "",
    }

    references: list[dict] = []

    # In CF format, Attribute/TabularSection/Dimension/Resource elements
    # can be either direct children of meta_el OR inside <ChildObjects>.
    child_objects = meta_el.find("md:ChildObjects", ns)
    search_el = child_objects if child_objects is not None else meta_el

    attributes = _cf_parse_attributes(search_el, ns)
    if attributes:
        result["attributes"] = attributes
        for attr in attributes:
            for canon in _ref_each(attr.get("type", "")):
                references.append(
                    {
                        "ref_object": canon,
                        "ref_kind": "attribute_type",
                        "used_in_suffix": f"Attribute.{attr.get('name', '')}.Type",
                    }
                )

    tab_sections = []
    for ts_el in search_el.findall("md:TabularSection", ns):
        ts_props = ts_el.find("md:Properties", ns)
        ts_name = _xml_find_text(ts_props, "md:Name", ns) if ts_props is not None else ""
        ts_synonym = _cf_find_synonym(ts_props, ns) if ts_props is not None else ""
        # TS attributes live in <ChildObjects> (CF), not directly under <TabularSection>
        ts_child_objects = ts_el.find("md:ChildObjects", ns)
        ts_search_el = ts_child_objects if ts_child_objects is not None else ts_el
        ts_attrs = _cf_parse_attributes(ts_search_el, ns)
        tab_sections.append({"name": ts_name, "synonym": ts_synonym, "attributes": ts_attrs})
        for ts_attr in ts_attrs:
            for canon in _ref_each(ts_attr.get("type", "")):
                references.append(
                    {
                        "ref_object": canon,
                        "ref_kind": "attribute_type",
                        "used_in_suffix": f"TabularSection.{ts_name}.Attribute.{ts_attr.get('name', '')}.Type",
                    }
                )
    if tab_sections:
        result["tabular_sections"] = tab_sections

    dimensions = []
    for dim_el in search_el.findall("md:Dimension", ns):
        dim_props = dim_el.find("md:Properties", ns)
        if dim_props is not None:
            dim_name = _xml_find_text(dim_props, "md:Name", ns)
            dim_type = _cf_parse_type(dim_props, ns)
            dimensions.append(
                {
                    "name": dim_name,
                    "synonym": _cf_find_synonym(dim_props, ns),
                    "type": dim_type,
                }
            )
            for canon in _ref_each(dim_type):
                references.append(
                    {
                        "ref_object": canon,
                        "ref_kind": "attribute_type",
                        "used_in_suffix": f"Dimension.{dim_name}.Type",
                    }
                )
    if dimensions:
        result["dimensions"] = dimensions

    resources = []
    for res_el in search_el.findall("md:Resource", ns):
        res_props = res_el.find("md:Properties", ns)
        if res_props is not None:
            res_name = _xml_find_text(res_props, "md:Name", ns)
            res_type = _cf_parse_type(res_props, ns)
            resources.append(
                {
                    "name": res_name,
                    "synonym": _cf_find_synonym(res_props, ns),
                    "type": res_type,
                }
            )
            for canon in _ref_each(res_type):
                references.append(
                    {
                        "ref_object": canon,
                        "ref_kind": "attribute_type",
                        "used_in_suffix": f"Resource.{res_name}.Type",
                    }
                )
    if resources:
        result["resources"] = resources

    if props is not None:
        content_el = props.find("md:Content", ns)
        if content_el is not None:
            items = []
            for item in content_el.findall("xr:Item", ns):
                if item.text:
                    items.append(item.text.strip())
            if items:
                result["content"] = items
                # Subsystem content references
                if meta_tag == "Subsystem":
                    for ref in items:
                        canon = canonicalize_type_ref(ref)
                        if canon:
                            references.append(
                                {
                                    "ref_object": canon,
                                    "ref_kind": "subsystem_content",
                                    "used_in_suffix": "Content",
                                }
                            )

        # Owners (Catalog/ChartOfCharacteristicTypes).
        # Real CF stores owners as <xr:Item xsi:type="xr:MDObjectRef">Catalog.X</xr:Item>;
        # synthetic CF samples sometimes use <v8:Type>CatalogRef.X</v8:Type>. Handle both.
        owners_el = props.find("md:Owners", ns)
        if owners_el is not None:
            for t in owners_el.findall("v8:Type", ns):
                if t.text:
                    canon = canonicalize_type_ref(t.text.strip())
                    if canon:
                        references.append({"ref_object": canon, "ref_kind": "owner", "used_in_suffix": "Owners"})
            for item in owners_el.findall("xr:Item", ns):
                if item.text:
                    canon = canonicalize_type_ref(item.text.strip())
                    if canon:
                        references.append({"ref_object": canon, "ref_kind": "owner", "used_in_suffix": "Owners"})

        # BasedOn (Document)
        based_on_el = props.find("md:BasedOn", ns)
        if based_on_el is not None:
            for t in based_on_el.findall("v8:Type", ns):
                if t.text:
                    canon = canonicalize_type_ref(t.text.strip())
                    if canon:
                        references.append({"ref_object": canon, "ref_kind": "based_on", "used_in_suffix": "BasedOn"})
            # Sometimes BasedOn contains direct text ref (Item names)
            for item in based_on_el.findall("xr:Item", ns):
                if item.text:
                    canon = canonicalize_type_ref(item.text.strip())
                    if canon:
                        references.append({"ref_object": canon, "ref_kind": "based_on", "used_in_suffix": "BasedOn"})

        # Default forms (DefaultObjectForm/DefaultListForm/DefaultChoiceForm)
        for form_kind, tag in (
            ("default_object_form", "md:DefaultObjectForm"),
            ("default_list_form", "md:DefaultListForm"),
            ("main_form", "md:MainForm"),
            ("list_form", "md:ListForm"),
        ):
            form_text = _xml_find_text(props, tag, ns)
            if form_text:
                # Form ref like "Catalog.X.Form.ФормаЭлемента" — treat the whole thing as a ref but
                # reduce to just the object reference (Catalog.X) so it points to that object.
                # The form-level ref here means: this object designates form Y.
                # For find_references_to_object we only want the object as ref, not the form name.
                # But we also want a separate reference if the form belongs to ANOTHER object.
                parts = form_text.split(".")
                if len(parts) >= 2:
                    ref_obj = ".".join(parts[:2])  # "Catalog.X"
                    canon = canonicalize_type_ref(ref_obj)
                    if canon:
                        references.append(
                            {
                                "ref_object": canon,
                                "ref_kind": form_kind,
                                "used_in_suffix": f"{tag.split(':')[-1]}={form_text}",
                            }
                        )

        # ChartsOfCharacteristicTypes Type list
        type_el = props.find("md:Type", ns)
        if type_el is not None and meta_tag in ("ChartOfCharacteristicTypes", "Constant"):
            for t in type_el.findall("v8:Type", ns):
                if t.text:
                    canon = canonicalize_type_ref(t.text.strip())
                    if canon:
                        references.append(
                            {
                                "ref_object": canon,
                                "ref_kind": "characteristic_type"
                                if meta_tag == "ChartOfCharacteristicTypes"
                                else "attribute_type",
                                "used_in_suffix": "Type",
                            }
                        )

        # Document.Posting (Allow / Deny / UseSelectively).
        # CF stores it as <Posting>Allow</Posting> in Properties; check namespaced and bare tags.
        if meta_tag == "Document":
            posting_text = _xml_find_text(props, "md:Posting", ns)
            if not posting_text:
                for ch in props:
                    local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
                    if local.lower() == "posting" and ch.text:
                        posting_text = ch.text.strip()
                        break
            if posting_text:
                result["posting"] = posting_text

    if references:
        result["references"] = references

    return result


# --- MDO format helpers ---


def _mdo_find_synonym(element) -> str:
    """Extract ru synonym from MDO element. MDO uses <synonym><key>ru</key><value>...</value></synonym>."""
    for syn in element:
        local = syn.tag.split("}")[-1] if "}" in syn.tag else syn.tag
        if local != "synonym":
            continue
        key = ""
        value = ""
        for ch in syn:
            ch_local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if ch_local == "key" and ch.text:
                key = ch.text.strip()
            elif ch_local == "value" and ch.text:
                value = ch.text.strip()
        if key == "ru":
            return value
    return ""


def _mdo_parse_type(element) -> str:
    """Extract type string from MDO element. MDO uses <type><types>...</types></type>."""
    for ch in element:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "type":
            continue
        types = []
        for t in ch:
            t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
            if t_local == "types" and t.text:
                types.append(t.text.strip())
        if types:
            return ", ".join(types)
    return ""


def _mdo_parse_attributes(parent) -> list[dict]:
    """Parse MDO <attributes> elements under parent."""
    attrs = []
    for ch in parent:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "attributes":
            continue
        attrs.append(
            {
                "name": _xml_direct_text(ch, "name"),
                "synonym": _mdo_find_synonym(ch),
                "type": _mdo_parse_type(ch),
            }
        )
    return attrs


def _parse_mdo_xml(root) -> dict:
    """Parse MDO-format metadata XML (EDT / 1C:DT)."""
    meta_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    result: dict = {
        "object_type": meta_tag,
        "name": _xml_direct_text(root, "name"),
        "synonym": _mdo_find_synonym(root),
    }

    references: list[dict] = []

    attributes = _mdo_parse_attributes(root)
    if attributes:
        result["attributes"] = attributes
        for attr in attributes:
            for canon in _ref_each(attr.get("type", "")):
                references.append(
                    {
                        "ref_object": canon,
                        "ref_kind": "attribute_type",
                        "used_in_suffix": f"Attribute.{attr.get('name', '')}.Type",
                    }
                )

    # Tabular sections: <tabularSections>
    tab_sections = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "tabularSections":
            continue
        ts_attrs = _mdo_parse_attributes(ch)
        ts_name = _xml_direct_text(ch, "name")
        tab_sections.append(
            {
                "name": ts_name,
                "synonym": _mdo_find_synonym(ch),
                "attributes": ts_attrs,
            }
        )
        for ts_attr in ts_attrs:
            for canon in _ref_each(ts_attr.get("type", "")):
                references.append(
                    {
                        "ref_object": canon,
                        "ref_kind": "attribute_type",
                        "used_in_suffix": f"TabularSection.{ts_name}.Attribute.{ts_attr.get('name', '')}.Type",
                    }
                )
    if tab_sections:
        result["tabular_sections"] = tab_sections

    # Dimensions: <dimensions>
    dimensions = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "dimensions":
            continue
        dim_name = _xml_direct_text(ch, "name")
        dim_type = _mdo_parse_type(ch)
        dimensions.append(
            {
                "name": dim_name,
                "synonym": _mdo_find_synonym(ch),
                "type": dim_type,
            }
        )
        for canon in _ref_each(dim_type):
            references.append(
                {
                    "ref_object": canon,
                    "ref_kind": "attribute_type",
                    "used_in_suffix": f"Dimension.{dim_name}.Type",
                }
            )
    if dimensions:
        result["dimensions"] = dimensions

    # Resources: <resources>
    resources = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "resources":
            continue
        res_name = _xml_direct_text(ch, "name")
        res_type = _mdo_parse_type(ch)
        resources.append(
            {
                "name": res_name,
                "synonym": _mdo_find_synonym(ch),
                "type": res_type,
            }
        )
        for canon in _ref_each(res_type):
            references.append(
                {
                    "ref_object": canon,
                    "ref_kind": "attribute_type",
                    "used_in_suffix": f"Resource.{res_name}.Type",
                }
            )
    if resources:
        result["resources"] = resources

    # Subsystem content: <content> direct children with text
    content_items = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == "content" and ch.text:
            content_items.append(ch.text.strip())
    if content_items:
        result["content"] = content_items
        if meta_tag == "Subsystem":
            for ref in content_items:
                canon = canonicalize_type_ref(ref)
                if canon:
                    references.append(
                        {
                            "ref_object": canon,
                            "ref_kind": "subsystem_content",
                            "used_in_suffix": "Content",
                        }
                    )

    # Forms, commands, templates — list names
    forms = []
    commands = []
    templates = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == "forms" and ch.text:
            forms.append(ch.text.strip())
        elif local == "commands" and ch.text:
            commands.append(ch.text.strip())
        elif local == "templates" and ch.text:
            templates.append(ch.text.strip())
    if forms:
        result["forms"] = forms
    if commands:
        result["commands"] = commands
    if templates:
        result["templates"] = templates

    # MDO direct refs: owners, basedOn, mainForm/listForm/defaultObjectForm/defaultListForm
    for tag, ref_kind in (
        ("owners", "owner"),
        ("basedOn", "based_on"),
    ):
        for ch in root:
            local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if local == tag and ch.text:
                canon = canonicalize_type_ref(ch.text.strip())
                if canon:
                    references.append(
                        {
                            "ref_object": canon,
                            "ref_kind": ref_kind,
                            "used_in_suffix": tag.capitalize(),
                        }
                    )

    for tag, ref_kind in (
        ("defaultObjectForm", "default_object_form"),
        ("defaultListForm", "default_list_form"),
        ("mainForm", "main_form"),
        ("listForm", "list_form"),
    ):
        text = _xml_direct_text(root, tag)
        if text:
            parts = text.split(".")
            if len(parts) >= 2:
                ref_obj = ".".join(parts[:2])
                canon = canonicalize_type_ref(ref_obj)
                if canon:
                    references.append(
                        {
                            "ref_object": canon,
                            "ref_kind": ref_kind,
                            "used_in_suffix": f"{tag}={text}",
                        }
                    )

    # ChartOfCharacteristicTypes <type><types>...</types></type>
    if meta_tag in ("ChartOfCharacteristicTypes", "Constant"):
        for ch in root:
            local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if local != "type":
                continue
            for t in ch:
                t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                if t_local == "types" and t.text:
                    canon = canonicalize_type_ref(t.text.strip())
                    if canon:
                        references.append(
                            {
                                "ref_object": canon,
                                "ref_kind": "characteristic_type"
                                if meta_tag == "ChartOfCharacteristicTypes"
                                else "attribute_type",
                                "used_in_suffix": "Type",
                            }
                        )

    # Document.posting (Allow / Deny / UseSelectively).
    # EDT хранит признак проводимости как:
    #   - атрибут на корневом элементе: <Document posting="Allow" ...>
    #   - дочерний тег <posting>Allow</posting>
    # Поле <realTimePosting> здесь НЕ учитывается — это отдельная семантика
    # (оперативное проведение): документ может быть posting=Allow и
    # realTimePosting=Deny одновременно. Их нельзя путать.
    # Отсутствие <posting> в EDT = default Allow (документ проводимый).
    if meta_tag == "Document":
        posting_text = root.attrib.get("posting", "").strip()
        if not posting_text:
            posting_text = _xml_direct_text(root, "posting")
        if posting_text:
            result["posting"] = posting_text

    if references:
        result["references"] = references

    return result


def parse_metadata_xml(xml_content: str) -> dict | None:
    """Parse 1C metadata XML and extract structure: name, synonym, attributes,
    tabular sections, subsystem content, dimensions, resources, etc.
    Auto-detects format: CF (Platform Export) or MDO (EDT/1C:DT).

    Returns None on empty/whitespace content or XML parse error (контракт
    зеркалит сиблинг parse_form_xml). Ловится ТОЛЬКО ET.ParseError —
    не-parse структурные ошибки _parse_cf_xml/_parse_mdo_xml пробрасываются
    к callsite-обёрткам. Verbatim-текст ParseError логируется на debug,
    т.к. callsite после перехода на None теряет str(exc)."""
    if not xml_content or not xml_content.strip():
        return None
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        logger.debug("parse_metadata_xml: ParseError: %s", exc)
        return None

    # Detect format by root tag namespace
    root_ns = ""
    if "}" in root.tag:
        root_ns = root.tag.split("}")[0].lstrip("{")

    if root_ns == _MDO_NS_URI or _MDO_NS_URI in root_ns:
        # MDO format — root IS the metadata object
        return _parse_mdo_xml(root)
    else:
        # CF format — root is <MetaDataObject> wrapper
        return _parse_cf_xml(root)


# --- EventSubscription XML parsers ---


def _parse_cf_event_subscription(xml_content: str) -> dict | None:
    """Parse CF-format EventSubscription XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    sub_el = root.find("md:EventSubscription", ns)
    if sub_el is None:
        # Try without namespace
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "EventSubscription":
                sub_el = child
                break
    if sub_el is None:
        return None

    props = sub_el.find("md:Properties", ns)
    if props is None:
        for ch in sub_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    synonym = _cf_find_synonym(props, ns)
    event = _xml_find_text(props, "md:Event", ns)
    handler = _xml_find_text(props, "md:Handler", ns)

    # Source types: <Source><v8:Type>cfg:DocumentObject.Name</v8:Type>...</Source>
    source_types: list[str] = []
    source_el = props.find("md:Source", ns)
    if source_el is not None:
        for type_el in source_el.findall("v8:Type", ns):
            if type_el.text:
                raw = type_el.text.strip()
                # Strip cfg: prefix
                if raw.startswith("cfg:"):
                    raw = raw[4:]
                source_types.append(raw)

    return {
        "name": name,
        "synonym": synonym,
        "source_types": source_types,
        "event": event,
        "handler": handler,
    }


def _parse_mdo_event_subscription(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format EventSubscription XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "EventSubscription":
        return None

    name = _xml_direct_text(root, "name")
    synonym = _mdo_find_synonym(root)
    event = _xml_direct_text(root, "event")
    handler = _xml_direct_text(root, "handler")

    # Source types: <source><types>DocumentObject.Name</types>...</source>
    source_types: list[str] = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == "source":
            for t in ch:
                t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                if t_local == "types" and t.text:
                    source_types.append(t.text.strip())
            break

    return {
        "name": name,
        "synonym": synonym,
        "source_types": source_types,
        "event": event,
        "handler": handler,
    }


def parse_event_subscription_xml(xml_content: str) -> dict | None:
    """Parse EventSubscription XML, auto-detecting CF or EDT format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_event_subscription(xml_content)
    return _parse_cf_event_subscription(xml_content)


# --- ScheduledJob XML parsers ---


def _parse_cf_scheduled_job(xml_content: str) -> dict | None:
    """Parse CF-format ScheduledJob XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    job_el = root.find("md:ScheduledJob", ns)
    if job_el is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "ScheduledJob":
                job_el = child
                break
    if job_el is None:
        return None

    props = job_el.find("md:Properties", ns)
    if props is None:
        for ch in job_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    synonym = _cf_find_synonym(props, ns)
    method_name = _xml_find_text(props, "md:MethodName", ns)
    use_text = _xml_find_text(props, "md:Use", ns)
    predefined_text = _xml_find_text(props, "md:Predefined", ns)
    restart_count = _xml_find_text(props, "md:RestartCountOnFailure", ns)
    restart_interval = _xml_find_text(props, "md:RestartIntervalOnFailure", ns)

    return {
        "name": name,
        "synonym": synonym,
        "method_name": method_name,
        "use": use_text.lower() == "true" if use_text else True,
        "predefined": predefined_text.lower() == "true" if predefined_text else False,
        "restart_on_failure": {
            "count": int(restart_count) if restart_count else 0,
            "interval": int(restart_interval) if restart_interval else 0,
        },
    }


def _parse_mdo_scheduled_job(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format ScheduledJob XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "ScheduledJob":
        return None

    name = _xml_direct_text(root, "name")
    synonym = _mdo_find_synonym(root)
    method_name = _xml_direct_text(root, "methodName")
    predefined_text = _xml_direct_text(root, "predefined")
    restart_count = _xml_direct_text(root, "restartCountOnFailure")
    restart_interval = _xml_direct_text(root, "restartIntervalOnFailure")

    return {
        "name": name,
        "synonym": synonym,
        "method_name": method_name,
        "use": True,  # EDT format doesn't have explicit <use> — defaults to true
        "predefined": predefined_text.lower() == "true" if predefined_text else False,
        "restart_on_failure": {
            "count": int(restart_count) if restart_count else 0,
            "interval": int(restart_interval) if restart_interval else 0,
        },
    }


def parse_scheduled_job_xml(xml_content: str) -> dict | None:
    """Parse ScheduledJob XML, auto-detecting CF or EDT format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_scheduled_job(xml_content)
    return _parse_cf_scheduled_job(xml_content)


# --- Enum XML parsers ---


def _parse_cf_enum(xml_content: str) -> dict | None:
    """Parse CF-format Enum XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    enum_el = root.find("md:Enum", ns)
    if enum_el is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "Enum":
                enum_el = child
                break
    if enum_el is None:
        return None

    props = enum_el.find("md:Properties", ns)
    if props is None:
        for ch in enum_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    synonym = _cf_find_synonym(props, ns)

    # Enum values live in ChildObjects
    child_objects = enum_el.find("md:ChildObjects", ns)
    search_el = child_objects if child_objects is not None else enum_el

    values: list[dict] = []
    for ev_el in search_el.findall("md:EnumValue", ns):
        ev_props = ev_el.find("md:Properties", ns)
        if ev_props is None:
            for ch in ev_el:
                if ch.tag.endswith("Properties"):
                    ev_props = ch
                    break
        if ev_props is None:
            continue
        values.append(
            {
                "name": _xml_find_text(ev_props, "md:Name", ns),
                "synonym": _cf_find_synonym(ev_props, ns),
            }
        )

    return {"name": name, "synonym": synonym, "values": values}


def _parse_mdo_enum(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format Enum XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "Enum":
        return None

    name = _xml_direct_text(root, "name")
    synonym = _mdo_find_synonym(root)

    values: list[dict] = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == "enumValues":
            val_name = _xml_direct_text(ch, "name")
            val_synonym = _mdo_find_synonym(ch)
            values.append({"name": val_name, "synonym": val_synonym})

    return {"name": name, "synonym": synonym, "values": values}


def parse_enum_xml(xml_content: str) -> dict | None:
    """Parse Enum XML, auto-detecting CF or EDT format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_enum(xml_content)
    return _parse_cf_enum(xml_content)


# --- Predefined Items parsers ---


def _parse_cf_predefined(root) -> list[dict] | None:
    """Parse CF-format Predefined.xml or PredefinedData section."""
    # Find PredefinedData container — could be root or child
    predef_el = root.find(".//{http://v8.1c.ru/8.3/MDClasses}PredefinedData")
    if predef_el is None:
        predef_el = root.find(".//PredefinedData")
    if predef_el is None:
        # Try root itself (if the whole file IS PredefinedData)
        if root.tag.endswith("PredefinedData"):
            predef_el = root
    if predef_el is None:
        return None

    items: list[dict] = []
    for item_el in predef_el:
        tag = item_el.tag.split("}")[-1] if "}" in item_el.tag else item_el.tag
        if tag != "Item":
            continue
        name = ""
        synonym = ""
        code = ""
        is_folder = False
        types: list[str] = []
        for ch in item_el:
            ch_tag = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if ch_tag == "Name" and ch.text:
                name = ch.text.strip()
            elif ch_tag == "Description" and ch.text:
                synonym = ch.text.strip()
            elif ch_tag == "Code" and ch.text:
                code = ch.text.strip()
            elif ch_tag == "IsFolder" and ch.text:
                is_folder = ch.text.strip().lower() == "true"
            elif ch_tag == "Type":
                for t in ch:
                    t_tag = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                    if t_tag == "Type" and t.text:
                        raw = t.text.strip()
                        types.append(_strip_ns_prefix(raw))
        if name:
            items.append(
                {
                    "name": name,
                    "synonym": synonym,
                    "code": code,
                    "types": types,
                    "is_folder": is_folder,
                }
            )
    return items if items else None


def _parse_mdo_predefined(root) -> list[dict] | None:
    """Parse EDT/MDO-format <predefined> section."""
    predef_el = None
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == "predefined":
            predef_el = ch
            break
    if predef_el is None:
        return None

    items: list[dict] = []
    for item_el in predef_el:
        local = item_el.tag.split("}")[-1] if "}" in item_el.tag else item_el.tag
        if local != "items":
            continue
        name = ""
        synonym = ""
        code = ""
        is_folder = False
        types: list[str] = []
        for ch in item_el:
            ch_tag = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if ch_tag == "name" and ch.text:
                name = ch.text.strip()
            elif ch_tag == "description" and ch.text:
                synonym = ch.text.strip()
            elif ch_tag == "code" and ch.text:
                code = ch.text.strip()
            elif ch_tag == "isFolder" and ch.text:
                is_folder = ch.text.strip().lower() == "true"
            elif ch_tag == "type":
                for t in ch:
                    t_tag = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                    if t_tag == "types" and t.text:
                        types.append(t.text.strip())
        if name:
            items.append(
                {
                    "name": name,
                    "synonym": synonym,
                    "code": code,
                    "types": types,
                    "is_folder": is_folder,
                }
            )
    return items if items else None


def parse_predefined_items(xml_content: str) -> list[dict] | None:
    """Parse predefined items from Predefined.xml (CF) or .mdo (EDT).

    Returns: list of dicts {name, synonym, code, types: list[str], is_folder: bool}
    or None if no predefined items found.
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    # Detect format by namespace
    root_ns = ""
    if "}" in root.tag:
        root_ns = root.tag.split("}")[0].lstrip("{")

    if _MDO_NS_URI in root_ns:
        return _parse_mdo_predefined(root)
    return _parse_cf_predefined(root)


# --- FunctionalOption XML parsers ---


def _parse_cf_functional_option(xml_content: str) -> dict | None:
    """Parse CF-format FunctionalOption XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    fo_el = root.find("md:FunctionalOption", ns)
    if fo_el is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "FunctionalOption":
                fo_el = child
                break
    if fo_el is None:
        return None

    props = fo_el.find("md:Properties", ns)
    if props is None:
        for ch in fo_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    synonym = _cf_find_synonym(props, ns)
    location = _xml_find_text(props, "md:Location", ns)

    # Content: <Content><xr:Object>...</xr:Object>...</Content>
    content: list[str] = []
    content_el = props.find("md:Content", ns)
    if content_el is not None:
        for obj_el in content_el.findall("xr:Object", ns):
            if obj_el.text:
                content.append(obj_el.text.strip())

    return {"name": name, "synonym": synonym, "location": location, "content": content}


def _parse_mdo_functional_option(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format FunctionalOption XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "FunctionalOption":
        return None

    name = _xml_direct_text(root, "name")
    synonym = _mdo_find_synonym(root)
    location = _xml_direct_text(root, "location")

    content: list[str] = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local == "content" and ch.text:
            content.append(ch.text.strip())

    return {"name": name, "synonym": synonym, "location": location, "content": content}


def parse_functional_option_xml(xml_content: str) -> dict | None:
    """Parse FunctionalOption XML, auto-detecting CF or EDT format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_functional_option(xml_content)
    return _parse_cf_functional_option(xml_content)


# --- Rights XML parser ---

# --- HTTPService XML parsers ---


def _parse_cf_http_service(xml_content: str) -> dict | None:
    """Parse CF-format HTTPService XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    svc_el = root.find("md:HTTPService", ns)
    if svc_el is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "HTTPService":
                svc_el = child
                break
    if svc_el is None:
        return None

    props = svc_el.find("md:Properties", ns)
    if props is None:
        for ch in svc_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    root_url = _xml_find_text(props, "md:RootURL", ns)

    templates: list[dict] = []
    child_objects = svc_el.find("md:ChildObjects", ns)
    if child_objects is None:
        for ch in svc_el:
            if ch.tag.endswith("ChildObjects"):
                child_objects = ch
                break
    if child_objects is not None:
        for tmpl_el in child_objects:
            tag = tmpl_el.tag.split("}")[-1] if "}" in tmpl_el.tag else tmpl_el.tag
            if tag != "URLTemplate":
                continue
            tmpl_props = tmpl_el.find("md:Properties", ns)
            if tmpl_props is None:
                for ch in tmpl_el:
                    if ch.tag.endswith("Properties"):
                        tmpl_props = ch
                        break
            if tmpl_props is None:
                continue
            tmpl_name = _xml_find_text(tmpl_props, "md:Name", ns)
            tmpl_template = _xml_find_text(tmpl_props, "md:Template", ns)

            methods: list[dict] = []
            tmpl_children = tmpl_el.find("md:ChildObjects", ns)
            if tmpl_children is None:
                for ch in tmpl_el:
                    if ch.tag.endswith("ChildObjects"):
                        tmpl_children = ch
                        break
            if tmpl_children is not None:
                for method_el in tmpl_children:
                    m_tag = method_el.tag.split("}")[-1] if "}" in method_el.tag else method_el.tag
                    if m_tag != "Method":
                        continue
                    m_props = method_el.find("md:Properties", ns)
                    if m_props is None:
                        for ch in method_el:
                            if ch.tag.endswith("Properties"):
                                m_props = ch
                                break
                    if m_props is None:
                        continue
                    methods.append(
                        {
                            "name": _xml_find_text(m_props, "md:Name", ns),
                            "http_method": _xml_find_text(m_props, "md:HTTPMethod", ns),
                            "handler": _xml_find_text(m_props, "md:Handler", ns),
                        }
                    )

            templates.append(
                {
                    "name": tmpl_name,
                    "template": tmpl_template,
                    "methods": methods,
                }
            )

    return {"name": name, "root_url": root_url, "templates": templates}


def _parse_mdo_http_service(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format HTTPService XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "HTTPService":
        return None

    name = _xml_direct_text(root, "name")
    root_url = _xml_direct_text(root, "rootURL")

    templates: list[dict] = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "urlTemplates":
            continue
        tmpl_name = _xml_direct_text(ch, "name")
        tmpl_template = _xml_direct_text(ch, "template")
        methods: list[dict] = []
        for m in ch:
            m_local = m.tag.split("}")[-1] if "}" in m.tag else m.tag
            if m_local != "methods":
                continue
            methods.append(
                {
                    "name": _xml_direct_text(m, "name"),
                    "http_method": _xml_direct_text(m, "httpMethod"),
                    "handler": _xml_direct_text(m, "handler"),
                }
            )
        templates.append(
            {
                "name": tmpl_name,
                "template": tmpl_template,
                "methods": methods,
            }
        )

    return {"name": name, "root_url": root_url, "templates": templates}


def parse_http_service_xml(xml_content: str) -> dict | None:
    """Parse HTTPService XML, auto-detecting CF or EDT format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_http_service(xml_content)
    return _parse_cf_http_service(xml_content)


# --- WebService XML parsers ---


def _parse_cf_web_service(xml_content: str) -> dict | None:
    """Parse CF-format WebService XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    svc_el = root.find("md:WebService", ns)
    if svc_el is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "WebService":
                svc_el = child
                break
    if svc_el is None:
        return None

    props = svc_el.find("md:Properties", ns)
    if props is None:
        for ch in svc_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    namespace = _xml_find_text(props, "md:Namespace", ns)

    operations: list[dict] = []
    child_objects = svc_el.find("md:ChildObjects", ns)
    if child_objects is None:
        for ch in svc_el:
            if ch.tag.endswith("ChildObjects"):
                child_objects = ch
                break
    if child_objects is not None:
        for op_el in child_objects:
            tag = op_el.tag.split("}")[-1] if "}" in op_el.tag else op_el.tag
            if tag != "Operation":
                continue
            op_props = op_el.find("md:Properties", ns)
            if op_props is None:
                for ch in op_el:
                    if ch.tag.endswith("Properties"):
                        op_props = ch
                        break
            if op_props is None:
                continue

            op_name = _xml_find_text(op_props, "md:Name", ns)
            return_type = _xml_find_text(op_props, "md:XDTOReturningValueType", ns)
            procedure_name = _xml_find_text(op_props, "md:ProcedureName", ns)

            params: list[str] = []
            op_children = op_el.find("md:ChildObjects", ns)
            if op_children is None:
                for ch in op_el:
                    if ch.tag.endswith("ChildObjects"):
                        op_children = ch
                        break
            if op_children is not None:
                for param_el in op_children:
                    p_tag = param_el.tag.split("}")[-1] if "}" in param_el.tag else param_el.tag
                    if p_tag != "Parameter":
                        continue
                    p_props = param_el.find("md:Properties", ns)
                    if p_props is None:
                        for ch in param_el:
                            if ch.tag.endswith("Properties"):
                                p_props = ch
                                break
                    if p_props is not None:
                        p_name = _xml_find_text(p_props, "md:Name", ns)
                        if p_name:
                            params.append(p_name)

            operations.append(
                {
                    "name": op_name,
                    "return_type": return_type,
                    "procedure_name": procedure_name,
                    "params": params,
                }
            )

    return {"name": name, "namespace": namespace, "operations": operations}


def _parse_mdo_web_service(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format WebService XML."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "WebService":
        return None

    name = _xml_direct_text(root, "name")
    namespace = _xml_direct_text(root, "namespace")

    operations: list[dict] = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "operations":
            continue
        op_name = _xml_direct_text(ch, "name")
        procedure_name = _xml_direct_text(ch, "procedureName")

        # Return type: compound <xdtoReturningValueType><name>string</name><nsUri>...</nsUri>
        return_type = ""
        for sub in ch:
            sub_local = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
            if sub_local == "xdtoReturningValueType":
                type_name = _xml_direct_text(sub, "name")
                ns_uri = _xml_direct_text(sub, "nsUri")
                if ns_uri == "http://www.w3.org/2001/XMLSchema" and type_name:
                    return_type = f"xs:{type_name}"
                elif type_name:
                    return_type = type_name
                break

        params: list[str] = []
        for sub in ch:
            sub_local = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
            if sub_local == "parameters":
                p_name = _xml_direct_text(sub, "name")
                if p_name:
                    params.append(p_name)

        operations.append(
            {
                "name": op_name,
                "return_type": return_type,
                "procedure_name": procedure_name,
                "params": params,
            }
        )

    return {"name": name, "namespace": namespace, "operations": operations}


def parse_web_service_xml(xml_content: str) -> dict | None:
    """Parse WebService XML, auto-detecting CF or EDT format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_web_service(xml_content)
    return _parse_cf_web_service(xml_content)


# --- XDTOPackage XML parsers ---

_NS_XDTO = "http://v8.1c.ru/8.1/xdto"


def _parse_cf_xdto_package(xml_content: str) -> dict | None:
    """Parse CF-format XDTOPackage XML (metadata only, types are binary)."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    ns = _NS_CF
    pkg_el = root.find("md:XDTOPackage", ns)
    if pkg_el is None:
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "XDTOPackage":
                pkg_el = child
                break
    if pkg_el is None:
        return None

    props = pkg_el.find("md:Properties", ns)
    if props is None:
        for ch in pkg_el:
            if ch.tag.endswith("Properties"):
                props = ch
                break
    if props is None:
        return None

    name = _xml_find_text(props, "md:Name", ns)
    namespace = _xml_find_text(props, "md:Namespace", ns)

    return {"name": name, "namespace": namespace, "types": []}


def _parse_mdo_xdto_package(xml_content: str) -> dict | None:
    """Parse EDT/MDO-format XDTOPackage .mdo (metadata only)."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    if root_tag != "XDTOPackage":
        return None

    name = _xml_direct_text(root, "name")
    namespace = _xml_direct_text(root, "namespace")

    return {"name": name, "namespace": namespace, "types": []}


def parse_xdto_types(xdto_content: str) -> list[dict]:
    """Parse .xdto file (Package.xdto) — extract objectType/valueType with properties.
    Works only for EDT format (CF stores types as binary Package.bin)."""
    try:
        root = ET.fromstring(xdto_content)
    except ET.ParseError:
        return []

    types: list[dict] = []

    for child in root:
        local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if local not in ("objectType", "valueType"):
            continue
        type_name = child.get("name", "")
        properties: list[dict] = []
        for prop in child:
            p_local = prop.tag.split("}")[-1] if "}" in prop.tag else prop.tag
            if p_local == "property":
                properties.append(
                    {
                        "name": prop.get("name", ""),
                        "type": prop.get("type", ""),
                    }
                )
        types.append(
            {
                "name": type_name,
                "kind": local,
                "properties": properties,
            }
        )

    return types


def parse_xdto_package_xml(xml_content: str, xdto_content: str = "") -> dict | None:
    """Parse XDTOPackage, auto-detecting CF or EDT.
    If xdto_content provided, parse types from it."""
    if _MDO_NS_URI in xml_content:
        result = _parse_mdo_xdto_package(xml_content)
    else:
        result = _parse_cf_xdto_package(xml_content)
    if result and xdto_content:
        result["types"] = parse_xdto_types(xdto_content)
    return result


# --- ExchangePlan content parsers ---

_NS_EP_CF = "http://v8.1c.ru/8.3/xcf/extrnprops"


def _parse_cf_exchange_plan_content(xml_content: str) -> list[dict]:
    """Parse CF ExchangePlans/<Name>/Ext/Content.xml."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    ns = {"ep": _NS_EP_CF}
    items: list[dict] = []

    # Try with namespace
    for item_el in root.findall("ep:Item", ns):
        metadata = ""
        auto_record = False
        meta_el = item_el.find("ep:Metadata", ns)
        if meta_el is not None and meta_el.text:
            metadata = meta_el.text.strip()
        ar_el = item_el.find("ep:AutoRecord", ns)
        if ar_el is not None and ar_el.text:
            auto_record = ar_el.text.strip() == "Allow"
        if metadata:
            items.append({"ref": metadata, "auto_record": auto_record})

    # Fallback: no namespace
    if not items:
        for item_el in root.findall("Item"):
            metadata = ""
            auto_record = False
            meta_el = item_el.find("Metadata")
            if meta_el is not None and meta_el.text:
                metadata = meta_el.text.strip()
            ar_el = item_el.find("AutoRecord")
            if ar_el is not None and ar_el.text:
                auto_record = ar_el.text.strip() == "Allow"
            if metadata:
                items.append({"ref": metadata, "auto_record": auto_record})

    return items


def _parse_mdo_exchange_plan_content(mdo_content: str) -> list[dict]:
    """Extract <content> elements from EDT ExchangePlan .mdo."""
    try:
        root = ET.fromstring(mdo_content)
    except ET.ParseError:
        return []

    items: list[dict] = []
    for ch in root:
        local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
        if local != "content":
            continue
        md_object = _xml_direct_text(ch, "mdObject")
        auto_record_text = _xml_direct_text(ch, "autoRecord")
        auto_record = auto_record_text == "Allow"
        if md_object:
            items.append({"ref": md_object, "auto_record": auto_record})

    return items


def parse_exchange_plan_content(xml_content: str) -> list[dict]:
    """Parse exchange plan content, auto-detecting format."""
    if _MDO_NS_URI in xml_content:
        return _parse_mdo_exchange_plan_content(xml_content)
    if _NS_EP_CF in xml_content or "ExchangePlanContent" in xml_content:
        return _parse_cf_exchange_plan_content(xml_content)
    return []


# ---------------------------------------------------------------------------
# DefinedType / ChartOfCharacteristicTypes / Command parameter parsers (v1.9.0)
# ---------------------------------------------------------------------------


def parse_defined_type(xml_content: str) -> dict | None:
    """Parse DefinedType XML / .mdo. Returns {name, types: list[str], path: not set}.

    Auto-detects CF or EDT format by namespace.
    Each type is canonicalized via canonicalize_type_ref where applicable;
    primitives ("xs:string", "String") are kept as-is in the raw list (filtered
    when written into metadata_references).
    """
    if not xml_content:
        return None
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_ns = ""
    if "}" in root.tag:
        root_ns = root.tag.split("}")[0].lstrip("{")

    name = ""
    types: list[str] = []

    if _MDO_NS_URI in root_ns:
        # EDT: root IS DefinedType
        root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if root_tag != "DefinedType":
            return None
        name = _xml_direct_text(root, "name")
        for ch in root:
            local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if local != "type":
                continue
            for t in ch:
                t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                if t_local == "types" and t.text:
                    types.append(t.text.strip())
    else:
        # CF: <MetaDataObject><DefinedType><Properties><Name>X<Type>...
        ns = _NS_CF
        dt_el = root.find("md:DefinedType", ns)
        if dt_el is None:
            for child in root:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "DefinedType":
                    dt_el = child
                    break
        if dt_el is None:
            return None
        props = dt_el.find("md:Properties", ns)
        if props is None:
            for ch in dt_el:
                if ch.tag.endswith("Properties"):
                    props = ch
                    break
        if props is None:
            return None
        name = _xml_find_text(props, "md:Name", ns)
        type_el = props.find("md:Type", ns)
        if type_el is not None:
            for t in type_el.findall("v8:Type", ns):
                if t.text:
                    types.append(_strip_ns_prefix(t.text.strip()))

    if not name:
        return None
    return {"name": name, "types": types}


def parse_pvh_characteristics(xml_content: str) -> dict | None:
    """Parse ChartOfCharacteristicTypes XML / .mdo for the Type list.

    Returns {pvh_name, types: list[str]}.
    """
    if not xml_content:
        return None
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    root_ns = ""
    if "}" in root.tag:
        root_ns = root.tag.split("}")[0].lstrip("{")

    name = ""
    types: list[str] = []

    if _MDO_NS_URI in root_ns:
        root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if root_tag != "ChartOfCharacteristicTypes":
            return None
        name = _xml_direct_text(root, "name")
        for ch in root:
            local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if local != "type":
                continue
            for t in ch:
                t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                if t_local == "types" and t.text:
                    types.append(t.text.strip())
    else:
        ns = _NS_CF
        pvh_el = root.find("md:ChartOfCharacteristicTypes", ns)
        if pvh_el is None:
            for child in root:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag == "ChartOfCharacteristicTypes":
                    pvh_el = child
                    break
        if pvh_el is None:
            return None
        props = pvh_el.find("md:Properties", ns)
        if props is None:
            for ch in pvh_el:
                if ch.tag.endswith("Properties"):
                    props = ch
                    break
        if props is None:
            return None
        name = _xml_find_text(props, "md:Name", ns)
        type_el = props.find("md:Type", ns)
        if type_el is not None:
            for t in type_el.findall("v8:Type", ns):
                if t.text:
                    types.append(_strip_ns_prefix(t.text.strip()))

    if not name:
        return None
    return {"pvh_name": name, "types": types}


def parse_command_parameter_type(xml_content: str) -> list[dict]:
    """Parse Command XML / .command for CommandParameterType refs.

    Returns list of dicts: [{ref_object, command_name}].
    """
    if not xml_content:
        return []
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    root_ns = ""
    if "}" in root.tag:
        root_ns = root.tag.split("}")[0].lstrip("{")

    results: list[dict] = []

    if _MDO_NS_URI in root_ns:
        root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        # EDT top-level commands are <CommonCommand>; object-nested are <Command>.
        if root_tag not in ("Command", "CommonCommand"):
            return []
        cmd_name = _xml_direct_text(root, "name")
        # commandParameterType / parameterType
        for ch in root:
            local = ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag
            if local not in ("commandParameterType", "parameterType"):
                continue
            for t in ch:
                t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                if t_local == "types" and t.text:
                    canon = canonicalize_type_ref(t.text.strip())
                    if canon:
                        results.append({"ref_object": canon, "command_name": cmd_name})
    else:
        ns = _NS_CF
        # Real CF wraps top-level commands in <CommonCommand>; object-nested in <Command>.
        cmd_el = None
        for tag_name in ("md:CommonCommand", "md:Command"):
            cmd_el = root.find(tag_name, ns)
            if cmd_el is not None:
                break
        if cmd_el is None:
            for child in root:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if tag in ("CommonCommand", "Command"):
                    cmd_el = child
                    break
        if cmd_el is None:
            return []
        props = cmd_el.find("md:Properties", ns)
        if props is None:
            for ch in cmd_el:
                if ch.tag.endswith("Properties"):
                    props = ch
                    break
        if props is None:
            return []
        cmd_name = _xml_find_text(props, "md:Name", ns)
        for tag_name in ("md:CommandParameterType", "md:ParameterType"):
            type_el = props.find(tag_name, ns)
            if type_el is None:
                continue
            # Object refs: <v8:Type>cfg:CatalogRef.X</v8:Type>
            for t in type_el.findall("v8:Type", ns):
                if t.text:
                    canon = canonicalize_type_ref(t.text.strip())
                    if canon:
                        results.append({"ref_object": canon, "command_name": cmd_name})
            # DefinedType refs: <v8:TypeSet>cfg:DefinedType.X</v8:TypeSet>
            for ts in type_el.findall("v8:TypeSet", ns):
                if ts.text:
                    canon = canonicalize_type_ref(ts.text.strip())
                    if canon:
                        results.append({"ref_object": canon, "command_name": cmd_name})

    return results


_NS_RIGHTS_VERSIONS = [
    "http://v8.1c.ru/8.2/roles",
    "http://v8.1c.ru/8.3/roles",
]


def parse_rights_xml(xml_content: str, object_filter: str = "") -> list[dict]:
    """Parse Rights XML (same format for CF and EDT).
    Returns only rights with <value>true</value>.
    Filters by object_filter substring if provided.
    Supports both 8.2 and 8.3 namespace versions."""
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return []

    # Detect namespace from root tag
    root_ns = ""
    if "}" in root.tag:
        root_ns = root.tag.split("}")[0].lstrip("{")

    # Try detected namespace, then known versions, then no namespace
    ns_candidates = []
    if root_ns:
        ns_candidates.append({"r": root_ns})
    for ns_uri in _NS_RIGHTS_VERSIONS:
        ns_candidates.append({"r": ns_uri})

    results: list[dict] = []

    for ns in ns_candidates:
        obj_elements = root.findall("r:object", ns)
        if not obj_elements:
            continue

        for obj_el in obj_elements:
            obj_name_el = obj_el.find("r:name", ns)
            if obj_name_el is None or not obj_name_el.text:
                continue
            obj_name = obj_name_el.text.strip()

            if object_filter and object_filter not in obj_name:
                continue

            granted: list[str] = []
            for right_el in obj_el.findall("r:right", ns):
                right_name_el = right_el.find("r:name", ns)
                right_value_el = right_el.find("r:value", ns)
                if right_name_el is None or right_value_el is None:
                    continue
                if right_value_el.text and right_value_el.text.strip().lower() == "true":
                    granted.append(right_name_el.text.strip())

            if granted:
                results.append({"object": obj_name, "rights": granted})

        break  # Found working namespace, stop trying

    return results


# ---------------------------------------------------------------------------
# Form XML parser (v1.6.0) — handlers, commands, attributes
# ---------------------------------------------------------------------------

_FORM_NS_EDT = "http://g5.1c.ru/v8/dt/form"
_FORM_NS_CF = "http://v8.1c.ru/8.3/xcf/logform"


def _find_children(parent, local_name: str) -> list:
    """Find all direct children by local tag name (namespace-agnostic)."""
    return [ch for ch in parent if (ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag) == local_name]


def _find_child(parent, local_name: str):
    """Find first direct child by local tag name (namespace-agnostic)."""
    for ch in parent:
        if (ch.tag.split("}")[-1] if "}" in ch.tag else ch.tag) == local_name:
            return ch
    return None


def _parse_form_edt(root) -> dict:
    """Parse EDT Form.form XML → handlers, commands, attributes.

    Uses namespace-agnostic child lookup because EDT forms may declare
    the form namespace as default (no prefix) or as ``form:``.
    """
    handlers: list[dict] = []
    commands: list[dict] = []
    attributes: list[dict] = []

    xsi_ns = "http://www.w3.org/2001/XMLSchema-instance"

    # --- Form-level handlers ---
    for h_el in _find_children(root, "handlers"):
        event = _xml_direct_text(h_el, "event")
        handler = _xml_direct_text(h_el, "name")
        if event and handler:
            handlers.append(
                {
                    "element": "",
                    "event": event,
                    "handler": handler,
                    "element_type": "",
                    "data_path": "",
                    "scope": "form",
                }
            )

    # --- ExtInfo handlers ---
    ext_info = _find_child(root, "extInfo")
    if ext_info is not None:
        for h_el in _find_children(ext_info, "handlers"):
            event = _xml_direct_text(h_el, "event")
            handler = _xml_direct_text(h_el, "name")
            if event and handler:
                handlers.append(
                    {
                        "element": "",
                        "event": event,
                        "handler": handler,
                        "element_type": "",
                        "data_path": "",
                        "scope": "ext_info",
                    }
                )

    # --- Elements (stack-based) ---
    stack = list(_find_children(root, "items"))
    while stack:
        item = stack.pop()
        # Control type from <type> child (InputField, CheckBoxField, etc.)
        # Fallback to xsi:type minus namespace prefix (FormField, FormGroup, Table)
        elem_type = _xml_direct_text(item, "type")
        if not elem_type:
            xsi_type = item.get(f"{{{xsi_ns}}}type", "")
            elem_type = xsi_type.split(":")[-1] if ":" in xsi_type else xsi_type
        elem_name = _xml_direct_text(item, "name")
        # data_path
        dp_el = _find_child(item, "dataPath")
        data_path = ""
        if dp_el is not None:
            data_path = _xml_direct_text(dp_el, "segments")

        # element handlers
        for h_el in _find_children(item, "handlers"):
            event = _xml_direct_text(h_el, "event")
            handler = _xml_direct_text(h_el, "name")
            if event and handler:
                handlers.append(
                    {
                        "element": elem_name,
                        "event": event,
                        "handler": handler,
                        "element_type": elem_type,
                        "data_path": data_path,
                        "scope": "element",
                    }
                )

        # push nested items (groups, tables)
        stack.extend(_find_children(item, "items"))

    # --- Commands (EDT: <formCommands>) ---
    for cmd_el in _find_children(root, "formCommands"):
        cmd_name = _xml_direct_text(cmd_el, "name")
        action_el = _find_child(cmd_el, "action")
        action = ""
        if action_el is not None:
            handler_el = _find_child(action_el, "handler")
            if handler_el is not None:
                action = _xml_direct_text(handler_el, "name")
        if cmd_name:
            commands.append({"name": cmd_name, "action": action})

    # --- Attributes ---
    for attr_el in _find_children(root, "attributes"):
        attr_name = _xml_direct_text(attr_el, "name")
        if not attr_name:
            continue
        # main flag
        main_val = _xml_direct_text(attr_el, "main")
        is_main = main_val.lower() == "true" if main_val else False
        # types
        vt_el = _find_child(attr_el, "valueType")
        types: list[str] = []
        if vt_el is not None:
            for t_el in vt_el:
                local = t_el.tag.split("}")[-1] if "}" in t_el.tag else t_el.tag
                if local == "types" and t_el.text:
                    types.append(t_el.text.strip())
        # DynamicList extInfo
        main_table = ""
        query_text = ""
        ext = _find_child(attr_el, "extInfo")
        if ext is not None:
            main_table = _xml_direct_text(ext, "mainTable")
            qt = _xml_direct_text(ext, "queryText")
            if qt:
                query_text = qt[:512]

        attr_dict: dict = {"name": attr_name, "types": types, "main": is_main}
        if main_table:
            attr_dict["main_table"] = main_table
        if query_text:
            attr_dict["query_text"] = query_text
        attributes.append(attr_dict)

    return {"handlers": handlers, "commands": commands, "attributes": attributes}


def _parse_form_cf(root) -> dict:
    """Parse CF Ext/Form.xml → handlers, commands, attributes."""
    handlers: list[dict] = []
    commands: list[dict] = []
    attributes: list[dict] = []

    # --- Form-level and ext_info events ---
    # CF puts both form-level and ext_info events in the same root <Events>.
    # Classify by event name: object lifecycle events → ext_info, rest → form.
    _EXT_INFO_EVENTS = frozenset(
        {
            "AfterWrite",
            "AfterWriteAtServer",
            "BeforeWrite",
            "BeforeWriteAtServer",
            "OnReadAtServer",
            "BeforeClose",
            "OnClose",
            "AfterWriteError",
            "BeforeRecordBreak",
        }
    )
    events_el = root.find("{%s}Events" % _FORM_NS_CF)
    if events_el is not None:
        for ev in events_el:
            local = ev.tag.split("}")[-1] if "}" in ev.tag else ev.tag
            if local == "Event":
                event_name = ev.get("name", "")
                handler = ev.text.strip() if ev.text else ""
                if event_name and handler:
                    scope = "ext_info" if event_name in _EXT_INFO_EVENTS else "form"
                    handlers.append(
                        {
                            "element": "",
                            "event": event_name,
                            "handler": handler,
                            "element_type": "",
                            "data_path": "",
                            "scope": scope,
                        }
                    )

    # --- Elements (stack-based via ChildItems) ---
    stack = list(root.findall("{%s}ChildItems" % _FORM_NS_CF))
    # Also check for ChildItems one level deeper (some CF forms have wrapper)
    while stack:
        container = stack.pop()
        for child in container:
            local_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local_tag == "ChildItems":
                stack.append(child)
                continue
            # This is an element
            elem_name = child.get("name", "")
            elem_type = local_tag  # InputField, Table, UsualGroup, etc.
            # data_path
            dp_el = child.find("{%s}DataPath" % _FORM_NS_CF)
            data_path = ""
            if dp_el is not None:
                data_path = dp_el.text.strip() if dp_el.text else ""

            # element events
            ev_el = child.find("{%s}Events" % _FORM_NS_CF)
            if ev_el is not None:
                for ev in ev_el:
                    ev_local = ev.tag.split("}")[-1] if "}" in ev.tag else ev.tag
                    if ev_local == "Event":
                        event_name = ev.get("name", "")
                        handler = ev.text.strip() if ev.text else ""
                        if event_name and handler:
                            handlers.append(
                                {
                                    "element": elem_name,
                                    "event": event_name,
                                    "handler": handler,
                                    "element_type": elem_type,
                                    "data_path": data_path,
                                    "scope": "element",
                                }
                            )

            # push nested ChildItems (groups, tables)
            for sub_ci in child.findall("{%s}ChildItems" % _FORM_NS_CF):
                stack.append(sub_ci)

    # --- Commands ---
    cmds_el = root.find("{%s}Commands" % _FORM_NS_CF)
    if cmds_el is not None:
        for cmd in cmds_el:
            local_tag = cmd.tag.split("}")[-1] if "}" in cmd.tag else cmd.tag
            if local_tag == "Command":
                cmd_name = cmd.get("name", "")
                action_el = cmd.find("{%s}Action" % _FORM_NS_CF)
                action = action_el.text.strip() if action_el is not None and action_el.text else ""
                if cmd_name:
                    commands.append({"name": cmd_name, "action": action})

    # --- Attributes ---
    attrs_el = root.find("{%s}Attributes" % _FORM_NS_CF)
    if attrs_el is not None:
        for attr in attrs_el:
            local_tag = attr.tag.split("}")[-1] if "}" in attr.tag else attr.tag
            if local_tag != "Attribute":
                continue
            attr_name = attr.get("name", "")
            if not attr_name:
                continue
            # main: реальные формы Конфигуратора пишут <MainAttribute>, не <Main>
            # (замер: 331 вхождение <MainAttribute> и НОЛЬ <Main> на 400 живых CF-формах,
            # из-за чего attribute_is_main=1 не встречался в индексе ни разу).
            # <Main> оставлен как запасной вариант — стоит один поиск.
            is_main = False
            for tag in ("MainAttribute", "Main"):
                el = attr.find("{%s}%s" % (_FORM_NS_CF, tag))
                if el is not None and el.text:
                    is_main = el.text.strip().lower() == "true"
                    break
            # type
            type_el = attr.find("{%s}Type" % _FORM_NS_CF)
            types: list[str] = []
            if type_el is not None:
                for t in type_el:
                    t_local = t.tag.split("}")[-1] if "}" in t.tag else t.tag
                    if t_local == "Type" and t.text:
                        types.append(t.text.strip())
            # DynamicList settings
            main_table = ""
            query_text = ""
            xsi_ns = "http://www.w3.org/2001/XMLSchema-instance"
            settings_el = attr.find("{%s}Settings" % _FORM_NS_CF)
            if settings_el is not None:
                xsi_type = settings_el.get(f"{{{xsi_ns}}}type", "")
                if "DynamicList" in xsi_type:
                    mt_el = settings_el.find("{%s}MainTable" % _FORM_NS_CF)
                    if mt_el is not None and mt_el.text:
                        main_table = mt_el.text.strip()
                    qt_el = settings_el.find("{%s}QueryText" % _FORM_NS_CF)
                    if qt_el is not None and qt_el.text:
                        query_text = qt_el.text.strip()[:512]

            attr_dict: dict = {"name": attr_name, "types": types, "main": is_main}
            if main_table:
                attr_dict["main_table"] = main_table
            if query_text:
                attr_dict["query_text"] = query_text
            attributes.append(attr_dict)

    return {"handlers": handlers, "commands": commands, "attributes": attributes}


def parse_form_xml(xml_content: str) -> dict | None:
    """Parse a 1C form XML (CF or EDT) → handlers, commands, attributes.

    Auto-detects format by namespace.
    Returns None on parse error.
    """
    if not xml_content or not xml_content.strip():
        return None
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    if _FORM_NS_EDT in xml_content:
        return _parse_form_edt(root)
    if _FORM_NS_CF in xml_content:
        return _parse_form_cf(root)
    return None
