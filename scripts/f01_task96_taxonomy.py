"""Exact URI bindings for historical taxonomies used by the Task 9.6 processor."""

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict, deque
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urldefrag, urljoin, urlsplit

import run_f01_historical_50_manual as task9
from f01_task96_contract import RESEARCH, publish, require

from trading_system.data.canonical_facts import fingerprint

TAX = task9.taxonomy
BULK = task9.bulk
CACHE = RESEARCH / "taxonomy-cache/f01-historical-taxonomies-v1"
MIB = 1024**2
MAX_ARCHIVE = 64 * MIB
MAX_EXPANDED = 512 * MIB
XSD = "{http://www.w3.org/2001/XMLSchema}"
LINK = "{http://www.xbrl.org/2003/linkbase}"
XLINK = "{http://www.w3.org/1999/xlink}"
CATALOG = "{urn:oasis:names:tc:entity:xmlns:xml:catalog}"
PACKAGE = "{http://xbrl.org/2016/taxonomy-package}"
RELEASE = "2012-01-31"
SCHEMA_REFS = {XSD + tag for tag in ("import", "include", "redefine", "override")}
LINK_REFS = {LINK + tag for tag in ("schemaRef", "linkbaseRef", "roleRef", "arcroleRef", "loc")}
REQUIRED = {
    **{
        f"http://xbrl.fasb.org/us-gaap/2012/elts/{family}-{RELEASE}.xsd": f"http://fasb.org/{family}/{RELEASE}"
        for family in ("us-gaap", "us-types")
    },
    **{
        f"http://xbrl.sec.gov/{family}/2012/{family}-{RELEASE}.xsd": f"http://xbrl.sec.gov/{family}/{RELEASE}"
        for family in ("country", "dei", "invest")
    },
}


def xml(data):
    require(
        b"\x00" not in data and not re.search(rb"<!DOCTYPE|<!ENTITY", data, re.I),
        "UNSAFE_TAXONOMY_XML",
    )
    return ET.fromstring(data)


def references(data, base):
    """Return DTS edges and the namespaces declared on schema imports."""
    pending = [(xml(data), base)]
    while pending:
        node, location = pending.pop()
        location = urljoin(location, node.get("{http://www.w3.org/XML/1998/namespace}base", ""))
        if node.tag in SCHEMA_REFS:
            target = node.get("schemaLocation")
            if target:
                yield urldefrag(urljoin(location, target))[0], node.get("namespace")
        if node.tag in LINK_REFS:
            target = node.get(XLINK + "href")
            if target:
                yield urldefrag(urljoin(location, target))[0], None
        pending.extend((child, location) for child in node)


def document_namespace(data, uri):
    if uri.lower().endswith(".dtd"):
        return None
    root = xml(data)
    namespace = root.get("targetNamespace")
    require(
        not uri.lower().endswith(".xsd") or root.tag == XSD + "schema",
        f"EXPECTED_TAXONOMY_SCHEMA: {uri}",
    )
    expected = REQUIRED.get(uri.replace("https://", "http://", 1))
    require(expected is None or namespace == expected, f"TAXONOMY_NAMESPACE_MISMATCH: {uri}")
    return namespace


def archive_members(archive):
    entries = archive.infolist()
    require(
        0 < len(entries) <= 30000 and sum(e.file_size for e in entries) <= MAX_EXPANDED,
        "TAXONOMY_ARCHIVE_SIZE_OR_MEMBER_LIMIT",
    )
    result = {}
    seen = set()
    for entry in entries:
        name = entry.filename.rstrip("/")
        require(
            all(
                part
                and part not in {".", ".."}
                and not part.endswith((".", " "))
                and not re.search(r'[\\:<>"|?*\x00-\x1f]', part)
                for part in name.split("/")
            )
            and name.casefold() not in seen
            and not entry.flag_bits & 1
            and (entry.external_attr >> 16) & 0o170000 != 0o120000,
            "UNSAFE_OR_DUPLICATE_TAXONOMY_MEMBER",
        )
        seen.add(name.casefold())
        if not entry.is_dir():
            result[name] = entry
    return result


def package_catalog(archive, members):
    manifests = [
        name for name in members if name.split("/")[-2:] == ["META-INF", "taxonomyPackage.xml"]
    ]
    if not manifests:
        return {"kind": "LEGACY_ZIP"}, ()
    require(len(manifests) == 1, "AMBIGUOUS_TAXONOMY_PACKAGE_MANIFEST")
    manifest = manifests[0]
    meta = xml(read_member(archive, members, manifest, MIB))
    require(meta.tag == PACKAGE + "taxonomyPackage", "INVALID_TAXONOMY_PACKAGE_MANIFEST")
    catalog = posixpath.join(posixpath.dirname(manifest), "catalog.xml")
    version = meta.findtext(PACKAGE + "version")
    info = {
        "kind": "NATIVE_PACKAGE",
        "manifest": manifest,
        "version": version,
        "identifier": meta.findtext(PACKAGE + "identifier"),
    }
    if catalog not in members:
        return info, ()
    tree = xml(read_member(archive, members, catalog, MIB))
    require(tree.tag == CATALOG + "catalog", "INVALID_TAXONOMY_PACKAGE_CATALOG")
    rules = []
    for node in tree:
        if node.tag not in {CATALOG + "rewriteURI", CATALOG + "uri"}:
            continue
        rewrite = node.tag == CATALOG + "rewriteURI"
        source = node.get("uriStartString" if rewrite else "name")
        target = node.get("rewritePrefix" if rewrite else "uri")
        require(
            source
            and target
            and not urlsplit(target).scheme
            and not target.startswith("/")
            and "\\" not in target
            and tree.get("{http://www.w3.org/XML/1998/namespace}base") is None
            and node.get("{http://www.w3.org/XML/1998/namespace}base") is None,
            "UNSUPPORTED_TAXONOMY_CATALOG_RULE",
        )
        package_root = manifest.removesuffix("META-INF/taxonomyPackage.xml")
        mapped = posixpath.dirname(catalog) + "/" + target
        normalized = posixpath.normpath(mapped)
        require(
            not normalized.startswith(("../", "/"))
            and (
                not package_root
                or normalized == package_root.rstrip("/")
                or normalized.startswith(package_root)
            ),
            "TAXONOMY_CATALOG_TARGET_OUTSIDE_PACKAGE",
        )
        rules.append((rewrite, source, mapped))
    return info | {"catalog": catalog}, rules


def read_member(archive, members, name, limit=TAX.LIMIT):
    require(name in members and 0 < members[name].file_size <= limit, "INVALID_PACKAGE_MEMBER")
    return archive.read(members[name])


def member_for(uri, rules):
    matches = [
        (len(start), posixpath.normpath(target + uri[len(start) :]))
        for rewrite, start, target in rules
        if (rewrite and uri.startswith(start)) or (not rewrite and uri == start)
    ]
    if not matches:
        return None
    longest = max(size for size, _ in matches)
    names = {name for size, name in matches if size == longest}
    require(len(names) == 1, f"CONFLICTING_TAXONOMY_CATALOG: {uri}")
    return names.pop()


def taxonomy_entry(data, uri, provenance):
    return data, {
        "url": uri,
        "bytes": len(data),
        "sha256": task9.sha(data),
        "namespace": document_namespace(data, uri),
        **provenance,
    }


def bind_archive(raw, *, source, candidates, explicit=None):
    """Explicit mappings come from a reviewed manifest; catalog rules match exact URIs."""
    require(0 < len(raw) <= MAX_ARCHIVE and source.startswith("https://"), "INVALID_ARCHIVE_SOURCE")
    sha = task9.sha(raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive_members(archive)
        package, rules = package_catalog(archive, members)
        selected = {}
        for uri in sorted(candidates):
            require(TAX.parts(uri), f"UNSUPPORTED_TAXONOMY_URI: {uri}")
            reviewed = (explicit or {}).get(uri)
            name = member_for(uri, rules)
            if reviewed:
                require(not name or name == reviewed["member"], f"CATALOG_REVIEW_CONFLICT: {uri}")
                name = reviewed["member"]
            if name is None:
                continue
            data = read_member(archive, members, name)
            entry = taxonomy_entry(
                data,
                uri,
                {
                    "archive_sha256": sha,
                    "source_url": source,
                    "package_member": name,
                    "package": package,
                },
            )
            if reviewed:
                require(
                    entry[1]["sha256"] == reviewed["sha256"]
                    and entry[1]["namespace"] == reviewed["namespace"],
                    f"PACKAGE_MEMBER_BINDING_MISMATCH: {uri}",
                )
            selected[uri] = entry
        return package, selected


def safe_root(root=CACHE):
    task9.filing.safe_directory(root.parent)
    return task9.filing.safe_directory(root)


def store(root, raw, source, entries, evidence):
    """Atomically publish immutable bytes and their reviewed URI bindings."""
    require(entries and evidence, "TAXONOMY_SOURCE_EVIDENCE_REQUIRED")
    sha = task9.sha(raw)
    objects = task9.filing.safe_directory(root / "objects")
    with task9.stage_lock(root / ".publication.lock"):
        known = Resolver(root=root)
        for uri, (_, meta) in entries.items():
            current = known.read(uri)
            require(
                not current or current[1]["sha256"] == meta["sha256"],
                f"TAXONOMY_URI_CONFLICT: {uri}",
            )
        target = objects / sha
        if not target.exists():
            with TemporaryDirectory(dir=objects, prefix=".object-") as temp:
                stage = Path(temp) / "object"
                stage.mkdir()
                BULK.write_new(stage / "content", raw)
                stage.rename(target)
        BULK.directory(target)
        check_bytes(target / "content", sha, len(raw))
        bindings = {
            "sha256": sha,
            "bytes": len(raw),
            "source_url": source,
            "evidence": evidence,
            "uris": {uri: meta for uri, (_, meta) in entries.items()},
        }
        publish(target / f"binding-{fingerprint(bindings)}.json", bindings)
    return bindings


def check_bytes(path, sha, size):
    BULK.directory(path.parent)
    require(0 < size <= MAX_ARCHIVE and BULK.plain(path).st_size == size, "INVALID_TAXONOMY_SIZE")
    raw = path.read_bytes()
    require(len(raw) == size and task9.sha(raw) == sha, f"TAXONOMY_CONTENT_HASH_MISMATCH: {path}")
    return raw


class Resolver:
    def __init__(self, root=CACHE, arelle=None):
        self.arelle = arelle or task9.filing.ARELLE_ROOT
        self.bindings = defaultdict(list)
        self.resolved = {}
        self.archive_bytes = lru_cache(maxsize=2)(check_bytes)
        objects = root / "objects"
        if BULK.exists(objects):
            BULK.directory(objects)
            for folder in sorted(objects.iterdir()):
                if not re.fullmatch("[a-f0-9]{64}", folder.name):
                    continue
                BULK.directory(folder)
                for path in sorted(folder.glob("binding-*.json")):
                    raw, info = task9.checked(path)
                    require(
                        path.name == f"binding-{fingerprint(info)}.json"
                        and folder.name == info["sha256"]
                        and info["evidence"],
                        f"TAXONOMY_BINDING_CHANGED: {path}",
                    )
                    for uri, member in info["uris"].items():
                        self.bindings[uri].append(
                            (folder / "content", info, member, path, task9.sha(raw))
                        )

    def read(self, uri):
        if uri not in self.resolved:
            self.resolved[uri] = self._read(uri)
        return self.resolved[uri]

    def _read(self, uri):
        base = TAX.bound_input(self.arelle, uri)
        entries = [base] if base else []
        if base and (url := base[1].get("package_url")):
            package = self.arelle / "bootstrap-packages" / url.rsplit("/", 1)[-1]
            _, metadata = task9.checked(package / "source.json")
            raw = self.archive_bytes(package / "content", metadata["sha256"], metadata["bytes"])
            require(
                metadata["url"] == url and metadata["sha256"] == base[1]["package_sha256"],
                "BOOTSTRAP_PACKAGE_SOURCE_CHANGED",
            )
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                member = archive.getinfo(base[1]["package_member"])
                require(
                    member.file_size == len(base[0]) and archive.read(member) == base[0],
                    "BOOTSTRAP_PACKAGE_MEMBER_CHANGED",
                )
        for path, binding, meta, binding_path, binding_sha in self.bindings[uri]:
            raw = self.archive_bytes(path, binding["sha256"], binding["bytes"])
            if member := meta.get("package_member"):
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    raw = read_member(archive, {member: archive.getinfo(member)}, member)
            require(
                meta["url"] == uri
                and meta["source_url"] == binding["source_url"]
                and task9.sha(raw) == meta["sha256"]
                and len(raw) == meta["bytes"]
                and document_namespace(raw, uri) == meta["namespace"],
                f"TAXONOMY_MEMBER_CHANGED: {uri}",
            )
            entries.append(
                (raw, meta | {"binding_path": str(binding_path), "binding_sha256": binding_sha})
            )
        if not entries:
            return None
        require(len({meta["sha256"] for _, meta in entries}) == 1, f"TAXONOMY_URI_CONFLICT: {uri}")
        document_namespace(entries[0][0], uri)
        return entries[0]

    def closure(self, seeds, originals=None):
        pending = deque(sorted(seeds))
        expected = defaultdict(set)
        for uri, namespaces in seeds.items():
            expected[uri].update(namespaces)
        results = {}
        namespaces = {}
        while pending:
            uri = pending.popleft()
            if uri in results or uri in (originals or {}):
                continue
            require(len(results) < 10000, "TAXONOMY_DEPENDENCY_LIMIT")
            if not TAX.parts(uri):
                results[uri] = {"status": "UNSUPPORTED_DEPENDENCY_HOST_OR_PATH"}
                continue
            try:
                entry = self.read(uri)
                if entry is None:
                    results[uri] = {"status": "MISSING_EVIDENCE"}
                    continue
                data, source = entry
                namespaces[uri] = document_namespace(data, uri)
                edges = (
                    sorted(set(references(data, uri)), key=lambda e: (e[0], e[1] or ""))
                    if not uri.endswith(".dtd")
                    else []
                )
            except (OSError, ValueError, KeyError, ET.ParseError, zipfile.BadZipFile) as exc:
                results[uri] = {"status": "QUARANTINED", "reason": str(exc)}
                continue
            for dep, namespace in edges:
                if namespace:
                    expected[dep].add(namespace)
                if dep != uri:
                    pending.append(dep)
            results[uri] = {
                "status": "BOUND",
                "dependencies": sorted({dep for dep, _ in edges if dep != uri}),
                "source": source,
            }
        for uri, required in expected.items():
            if uri in namespaces and required and required != {namespaces[uri]}:
                results[uri].update(status="QUARANTINED", reason="IMPORT_NAMESPACE_MISMATCH")
            elif uri in results and required and results[uri]["status"] != "BOUND":
                results[uri]["import_namespaces"] = sorted(required)
        return results

    def materialize(self, folder, closure):
        # Use Arelle's ordinary WebCache layout so relative URIs resolve as published.
        for uri, entry in closure.items():
            require(entry["status"] == "BOUND", "INCOMPLETE_OFFLINE_TAXONOMY_CLOSURE")
            data, metadata = self.read(uri)
            require(metadata == entry["source"], "TAXONOMY_CLOSURE_CHANGED")
            path = TAX.local_cache_path(folder, uri)
            path.parent.mkdir(parents=True, exist_ok=True)
            BULK.write_new(path, data)
