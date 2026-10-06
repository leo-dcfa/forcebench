"""Rebuild the vendored data used by the ``scratch_def`` grader.

Not imported at runtime. Run it when a new Salesforce release ships (see scratch-def-README.md):

    uv run python src/forcebench/data/scratch-def-build.py \
        --schemas <dir of the unpacked @salesforce/schemas npm package> \
        --features-doc <get_document_content JSON of sfdx_dev_scratch_orgs_def_file_config_values.htm> \
        --wsdl <metadata WSDL downloaded from a scratch org> \
        --md-types <`sf org list metadata-types --json` output from the same org> \
        --retrieved YYYY-MM-DD

Outputs (next to this script):
    scratch-def.schema.json           official scratch org definition JSON schema (verbatim)
    scratch-sfdx-project.schema.json  official sfdx-project.json JSON schema (verbatim)
    scratch-features.json             scratch org features from the "Scratch Org Features" docs
    scratch-settings-catalog.json     Metadata API *Settings types -> fields, from the WSDL
"""

import argparse
import html
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


HERE = Path(__file__).resolve().parent
XSD = "{http://www.w3.org/2001/XMLSchema}"
FEATURES_URL = (
    "https://developer.salesforce.com/docs/atlas.en-us.sfdx_dev.meta/sfdx_dev/"
    "sfdx_dev_scratch_orgs_def_file_config_values.htm"
)

# Features used in the "Scratch Org Features" page's own sample definition files but missing
# from its index. They are documented usage, so they are accepted.
EXAMPLE_ONLY_FEATURES = [
    "AnalyticsQueryService",
    "ContextService",
    "CustomerCommunityPlus",
    "FeatureParameterLicensing",
    "IndustriesSalesExcellenceAddOn",
    "IndustriesServiceExcellenceAddOn",
    "PartnerCommunity",
    "SalesforceConfiguratorEngine",
    "Scrt2Conversation",
]

# Features that must fail even though an older source lists them.
RETIRED_FEATURES = {
    "MultiCurrency": (
        "no longer a scratch org feature (removed from the Scratch Org Features docs; "
        "Salesforce Help 000395964: remove it and use currencySettings.enableMultiCurrency)"
    ),
    "Functions": "Salesforce Functions was retired; the feature is no longer available",
}

# Mirrors @salesforce/core scratchOrgFeatureDeprecation.ts (the CLI warns and drops or remaps
# these).
CLI_DEPRECATED_FEATURES = {
    "ExpandedSourceTracking": "deprecated: source tracking is always on in scratch orgs",
    "ListCustomSettingCreation": "deprecated by Salesforce CLI",
    "AppNavCapabilities": "deprecated by Salesforce CLI",
    "CMTRecordManagedDeletion": "deprecated by Salesforce CLI",
    "EditInSubtab": "deprecated by Salesforce CLI",
    "OldNewRecordFlowConsole": "deprecated by Salesforce CLI",
    "OldNewRecordFlowStd": "deprecated by Salesforce CLI",
    "DesktopLayoutStandardOff": "deprecated by Salesforce CLI",
    "SplitViewOnStandardOff": "deprecated by Salesforce CLI",
    "PopOutUtilities": "deprecated by Salesforce CLI",
    "SalesWave": "renamed: use DevelopmentWave",
    "ServiceWave": "renamed: use DevelopmentWave",
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def build_features(doc_json: Path, schema: dict, retrieved: str) -> dict:
    doc = json.loads(doc_json.read_text())
    content = doc["content"]
    items = re.findall(
        r'<li class="link ulchildlink">\s*<strong><a href="[^"]*#([^"]+)">(.*?)</a></strong>',
        content,
        re.S,
    )
    sections: dict[str, str] = {}
    parts = re.split(r'<a name="(so_[^"]+)"><!-- --></a>', content)
    for i in range(1, len(parts), 2):
        sections[parts[i]] = _text(parts[i + 1])

    features: dict[str, dict] = {}
    for anchor, raw in items:
        title = _text(raw)
        name = re.split(r"[\s:(]", title, maxsplit=1)[0]
        entry: dict = {"quantity": "<value>" in title, "source": "docs"}
        body = sections.get(anchor, "")
        m = re.search(r"Supported Quantities\s*([\d,]+)\s*[\u2013-]\s*([\d,]+)", body)
        if m:
            entry["min"] = int(m.group(1).replace(",", ""))
            entry["max"] = int(m.group(2).replace(",", ""))
        else:
            m = re.search(r"Supported Quantities\s*Maximum:\s*([\d,]+)", body)
            if m:
                entry["max"] = int(m.group(1).replace(",", ""))
        if "(Developer Preview)" in title or "(Beta)" in title or "(Pilot)" in title:
            entry["note"] = title[len(name) :].strip()
        features[name] = entry
    for name in EXAMPLE_ONLY_FEATURES:
        features.setdefault(name, {"quantity": False, "source": "docs example"})
    # The official JSON schema's feature list (older, smaller; used by VS Code completion).
    for item in schema["definitions"]["features"]["items"]["anyOf"]:
        names = item.get("enum") or []
        if "pattern" in item:
            names = [item["title"].split(":", 1)[0]]
        for name in names:
            if name in RETIRED_FEATURES:
                continue
            if name.lower() not in {k.lower() for k in features}:
                features[name] = {"quantity": "pattern" in item, "source": "schema"}
            elif "pattern" in item:
                key = next(k for k in features if k.lower() == name.lower())
                features[key]["quantity"] = True
    return {
        "_provenance": {
            "source": FEATURES_URL,
            "release": doc.get("version", {}).get("version_text")
            or "Summer '26 (API version 67.0)",
            "retrieved": retrieved,
            "notes": (
                "Index of the Scratch Org Features page, plus features used in that page's "
                "sample definitions, plus the feature list of the official scratch org "
                "definition JSON schema (@salesforce/schemas). quantity=true means the docs "
                "list the feature as Name:<value>; min/max come from 'Supported Quantities'."
            ),
        },
        "features": dict(sorted(features.items(), key=lambda kv: kv[0].lower())),
        "retired": RETIRED_FEATURES,
        "deprecated": CLI_DEPRECATED_FEATURES,
    }


def build_settings(wsdl: Path, md_types: Path, retrieved: str) -> dict:
    # The metadata WSDL the maintainer downloaded from their own scratch org.
    root = ET.parse(wsdl).getroot()  # noqa: S314
    schema = root.find(f".//{XSD}schema")
    if schema is None:
        raise ValueError(f"{wsdl}: no XML schema in the WSDL")
    complex_types = {c.get("name"): c for c in schema.findall(f"{XSD}complexType")}
    simple_types = {s.get("name"): s for s in schema.findall(f"{XSD}simpleType")}
    header = (wsdl.read_text()[:400]).split("Metadata API version", 1)
    api_version = header[1].split()[0] if len(header) > 1 else "?"

    # Stand-alone metadata types whose names end in "Settings" (e.g. LeadConvertSettings) are
    # not members of the Settings type and cannot be used as scratch org settings.
    described = json.loads(md_types.read_text())["result"]["metadataObjects"]
    standalone = {o["xmlName"] for o in described if o["xmlName"] != "Settings"}

    def base_of(ct: ET.Element) -> str | None:
        ext = ct.find(f".//{XSD}extension")
        return ext.get("base").split(":", 1)[1] if ext is not None else None

    settings = sorted(
        n
        for n, ct in complex_types.items()
        if n.endswith("Settings") and base_of(ct) == "Metadata" and n not in standalone
    )

    types: dict[str, dict] = {}
    enums: dict[str, list[str]] = {}

    def type_ref(xsd_type: str) -> str:
        prefix, _, name = xsd_type.partition(":")
        if prefix == "xsd":
            return name
        if name in simple_types:
            values = [e.get("value") for e in simple_types[name].iter(f"{XSD}enumeration")]
            if values:
                enums[name] = values
                return f"enum:{name}"
            restriction = simple_types[name].find(f"{XSD}restriction")
            return restriction.get("base").split(":", 1)[1] if restriction is not None else "string"
        if name in complex_types:
            add(name)
            return name
        return "string"

    def fields(name: str) -> dict[str, list]:
        ct = complex_types[name]
        out: dict[str, list] = {}
        ext = ct.find(f".//{XSD}extension")
        container = ext if ext is not None else ct
        if ext is not None:
            out.update(fields(ext.get("base").split(":", 1)[1]))
        seq = container.find(f"{XSD}sequence")
        for el in seq.findall(f"{XSD}element") if seq is not None else []:
            out[el.get("name")] = [type_ref(el.get("type")), el.get("maxOccurs") == "unbounded"]
        return out

    def add(name: str) -> None:
        if name in types:
            return
        types[name] = {}  # guard against recursion
        types[name] = fields(name)

    for name in settings:
        add(name)
    # objectSettings.sharingModel values are CustomObject sharing models; the definition's
    # `language` must be a Salesforce language code.
    type_ref("tns:SharingModel")
    type_ref("tns:Language")
    return {
        "_provenance": {
            "source": "Metadata API WSDL (/services/wsdl/metadata) of a scratch org",
            "api_version": api_version,
            "retrieved": retrieved,
            "notes": (
                "settings: complexTypes extending Metadata whose name ends in 'Settings', minus "
                "stand-alone metadata types returned by describeMetadata. types: field -> "
                "[type, repeated] for every type reachable from a settings type (inherited "
                "fields flattened). enums: xsd enumerations."
            ),
        },
        "api_version": api_version,
        "settings": settings,
        "types": dict(sorted(types.items())),
        "enums": dict(sorted(enums.items())),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--schemas", type=Path, required=True)
    ap.add_argument("--features-doc", type=Path, required=True)
    ap.add_argument("--wsdl", type=Path, required=True)
    ap.add_argument("--md-types", type=Path, required=True)
    ap.add_argument("--retrieved", required=True)
    args = ap.parse_args()

    shutil.copyfile(
        args.schemas / "project-scratch-def.schema.json", HERE / "scratch-def.schema.json"
    )
    shutil.copyfile(
        args.schemas / "sfdx-project.schema.json", HERE / "scratch-sfdx-project.schema.json"
    )
    schema = json.loads((args.schemas / "project-scratch-def.schema.json").read_text())
    feats = build_features(args.features_doc, schema, args.retrieved)
    (HERE / "scratch-features.json").write_text(json.dumps(feats, indent=1) + "\n")
    cat = build_settings(args.wsdl, args.md_types, args.retrieved)
    (HERE / "scratch-settings-catalog.json").write_text(
        json.dumps(cat, separators=(",", ":")) + "\n"
    )
    print(
        f"features: {len(feats['features'])}, settings types: {len(cat['settings'])}, "
        f"types: {len(cat['types'])}, enums: {len(cat['enums'])}, API {cat['api_version']}"
    )


if __name__ == "__main__":
    main()
