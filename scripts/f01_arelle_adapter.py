"""Arelle worker for the isolated Original Filing Evidence pipeline."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import logging
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "2.47.5"
EDGAR_COMMIT = "4891c4c9889c77af247428ceff4fa7cc52d534ea"
SEC_PLUGIN_FILES = {
    "__init__.py": "2296586f945ffd95ab37d3d4147c4e45f42ce4e5f397f61143e053208adefbf1",
    "text2num.py": "6c9b26354320a2fb34fe380cdc71ff7f8d1cc712811e6b10c661e512f0383409",
}
DIM = "http://xbrl.org/2006/xbrldi"
XHTML = "http://www.w3.org/1999/xhtml"
ARCS = (
    "http://www.xbrl.org/2003/arcrole/parent-child",
    "http://www.xbrl.org/2003/arcrole/summation-item",
    "https://xbrl.org/2023/arcrole/summation-item",
)


def prohibit_network(event, _args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        raise OSError("ARELLE_OFFLINE_NETWORK_DENIED")


class FilingLogs(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.issues = []

    def emit(self, record):
        self.issues.append(
            {"code": str(getattr(record, "messageCode", "")), "reason": record.getMessage()}
        )


def qname(value):
    return value.clarkNotation if value is not None else None


def date_text(value):
    return value.isoformat() if value is not None else None


def context_data(context):
    if context is None:
        return None
    from lxml import etree

    scheme, entity = context.entityIdentifier
    dimensions = []
    for axis, dim in sorted(context.qnameDims.items(), key=lambda item: qname(item[0])):
        item = {"axis": qname(axis), "container": dim.contextElement}
        if dim.isExplicit:
            item["member"] = qname(dim.memberQname)
        else:
            item["typed_xml"] = etree.tostring(dim.typedMember, encoding="unicode")
        dimensions.append(item)
    dimension_elements = {f"{{{DIM}}}explicitMember", f"{{{DIM}}}typedMember"}
    return {
        "entity": entity,
        "scheme": scheme,
        "dimensions": dimensions,
        "instant": date_text(context.instantDate) if context.isInstantPeriod else None,
        "startDate": date_text(context.startDatetime.date()) if context.isStartEndPeriod else None,
        "endDate": date_text(context.endDate) if context.isStartEndPeriod else None,
        "opaque_context": any(
            child.tag not in dimension_elements
            for node in (context.segment, context.scenario)
            if node is not None
            for child in node
        ),
    }


def relationships(model):
    result = defaultdict(list)
    for arcrole in ARCS:
        for rel in model.relationshipSet(arcrole).modelRelationships:
            parent = qname(getattr(rel.fromModelObject, "qname", None))
            child = qname(getattr(rel.toModelObject, "qname", None))
            if parent is None or child is None:
                continue
            entry = {
                "arcrole": arcrole,
                "linkrole": rel.linkrole,
                "from": parent,
                "to": child,
                "order": str(rel.order),
                "weight": rel.get("weight"),
                "source_document": rel.modelDocument.uri,
                "source_line": rel.sourceline,
            }
            for concept in {parent, child}:
                result[concept].append(entry)
    return result


def fact_data(fact, ordinal, arcs):
    concept = qname(fact.qname)
    unit = fact.unit
    value = None
    if fact.concept is not None and fact.isNumeric and fact.unit is not None and not fact.isNil:
        try:
            # Arelle's value applies iXBRL transforms, scale and sign without a float round trip.
            parsed = Decimal(fact.value)
            if parsed.is_finite():
                value = str(parsed)
        except (InvalidOperation, TypeError, ValueError):
            pass
    table = next(fact.iterancestors(f"{{{XHTML}}}table"), None)
    captions = (
        []
        if table is None
        else [
            " ".join("".join(node.itertext()).split())
            for node in table.findall(f"{{{XHTML}}}caption")
        ]
    )
    return {
        "concept": concept,
        "raw_text": getattr(fact, "rawValue", fact.textValue),
        "value": value,
        "scale": fact.get("scale", "0"),
        "sign": fact.get("sign", ""),
        "decimals": fact.decimals,
        "format": qname(getattr(fact, "format", None)),
        "context_id": fact.contextID,
        "context": context_data(fact.context),
        "unit_id": fact.unitID,
        "unit": {
            "numerator": [qname(measure) for measure in unit.measures[0]],
            "denominator": [qname(measure) for measure in unit.measures[1]],
        }
        if unit is not None
        else None,
        "fact_id": fact.get("id"),
        "document_ordinal": ordinal,
        "source_document": fact.modelDocument.uri,
        "source_line": fact.sourceline,
        "table_id": table.get("id") if table is not None else None,
        "table_captions": captions,
        "relationships": arcs.get(concept, []),
    }


def extract(document, cache, packages, sec_plugin=None):
    from arelle.api.Session import Session
    from arelle.RuntimeOptions import RuntimeOptions

    logs = FilingLogs()
    options = RuntimeOptions(
        entrypointFile=str(document),
        workOffline=True,
        keepOpen=True,
        cacheDirectory=str(cache),
        xdgConfigHome=str(cache.parent),
        disablePersistentConfig=True,
        packages=[str(path) for path in packages],
        plugins=sec_plugin,
    )
    with Session() as session:
        session.run(options, logHandler=logs)
        models = session.get_models()
        if len(models) != 1 or models[0].modelDocument is None:
            return {"observations": [], "issues": logs.issues or [{"code": "MODEL_UNAVAILABLE"}]}
        model = models[0]
        arcs = relationships(model)
        default_members = {
            (qname(rel.fromModelObject.qname), qname(rel.toModelObject.qname))
            for rel in model.relationshipSet(
                "http://xbrl.org/int/dim/arcrole/dimension-default"
            ).modelRelationships
            if getattr(rel.fromModelObject, "qname", None)
            and getattr(rel.toModelObject, "qname", None)
        }
        facts = []
        ordered = sorted(model.factsInInstance, key=lambda fact: fact.objectIndex)
        for ordinal, fact in enumerate(ordered, 1):
            if fact.isTuple or not (fact.unitID or fact.isNumeric):
                continue
            item = fact_data(fact, ordinal, arcs)
            for dimension in (item["context"] or {}).get("dimensions", []):
                if (dimension["axis"], dimension.get("member")) in default_members:
                    dimension["is_default"] = True
            if fact.concept is None or item["context"] is None or item["unit"] is None:
                item["evidence_issue"] = "TAXONOMY_OR_CONTEXT_UNAVAILABLE"
            facts.append(item)
        dts = []
        roots = (document.parent, cache, Path(sys.path[0]))
        for uri, document in sorted(model.urlDocs.items()):
            row = {"uri": uri, "resolved_path": document.filepath, "sha256": None}
            path = Path(document.filepath).resolve() if document.filepath else None
            if (
                path is not None
                and any(path.is_relative_to(root) for root in roots)
                and path.is_file()
            ):
                with path.open("rb") as stream:
                    row["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            dts.append(row)
        return {
            "observations": facts,
            "issues": logs.issues,
            "taxonomy_documents": dts,
        }


def main():
    request = json.load(sys.stdin)
    sys.path.insert(0, request["site"])
    sys.addaudithook(prohibit_network)
    if importlib.metadata.version("arelle-release") != VERSION:
        raise ValueError(f"arelle-release=={VERSION} is required in the Research directory")
    if request.get("probe"):
        from arelle.api.Session import Session

        print(
            json.dumps(
                {
                    "version": VERSION,
                    "api": Session.__module__,
                    "packages": {
                        dist.metadata["Name"]: dist.version
                        for dist in importlib.metadata.distributions(path=[request["site"]])
                    },
                }
            )
        )
        return
    print(
        json.dumps(
            extract(
                Path(request["document"]),
                Path(request["cache"]),
                request["packages"],
                request.get("sec_plugin"),
            ),
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
