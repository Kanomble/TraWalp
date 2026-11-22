"""Bootstrap exact SEC filing taxonomy dependencies for isolated offline Arelle."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import posixpath
import re
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from collections import deque
from contextlib import closing
from datetime import UTC, datetime
from functools import cache
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True
import prepare_f01_filing_evidence as filing  # noqa: E402

bulk = filing.bulk
require = filing.require
DRAFT = "f01-original-filing-evidence-draft-20261122-092354.json"
DRAFT_SHA = "858aefaba438d82710fbfca7792abff7e393aa5dbaa3aaa86e08a7587c957536"
HOSTS = {"xbrl.sec.gov", "xbrl.fasb.org", "www.xbrl.org", "xbrl.org", "www.w3.org"}
PLUGIN_BASE = f"https://raw.githubusercontent.com/Arelle/EDGAR/{filing.EDGAR_COMMIT}/transform/"
LIMIT = 64 * 1024**2
FILE_BINDING = ".f01-source.json"
REFERENCE = re.compile(
    r"""\b(?:schemaLocation|(?:[\w.-]+:)?href)\s*=\s*["'](https?://[^"'\s]+)["']""",
    re.I,
)


def parts(url):
    value = urllib.parse.urlsplit(url)
    path = value.path.split("/")[1:]
    if (
        value.scheme not in {"http", "https"}
        or value.netloc not in HOSTS
        or value.query
        or not path
        or not all(filing.NAME.fullmatch(item) and ".." not in item for item in path)
        or not path[-1].lower().endswith((".xsd", ".xml", ".dtd"))
    ):
        return None
    return value.scheme, value.netloc, *path


def normalize(url):
    clean, _ = urllib.parse.urldefrag(url)
    return clean if parts(clean) else None


def xml_dependencies(data, base):
    # XML used only for taxonomy dependency discovery, never accounting facts.
    require(not re.search(rb"<!DOCTYPE|<!ENTITY", data, re.I), "UNSAFE_TAXONOMY_DECLARATION")
    root = ET.fromstring(data)
    pending = [(root, base)]
    dependencies = set()
    while pending:
        node, parent = pending.pop()
        location = urllib.parse.urljoin(
            parent, node.get("{http://www.w3.org/XML/1998/namespace}base", "")
        )
        for key in ("schemaLocation", "{http://www.w3.org/1999/xlink}href"):
            if node.get(key):
                url = urllib.parse.urljoin(location, node.get(key))
                accepted = normalize(url)
                if accepted and accepted != base:
                    dependencies.add(accepted)
                elif (
                    not accepted
                    and urllib.parse.urlsplit(url).netloc not in {"www.sec.gov", "sec.gov"}
                    and urllib.parse.urlsplit(url).path.lower().endswith((".xsd", ".xml", ".dtd"))
                ):
                    dependencies.add(urllib.parse.urldefrag(url)[0])
        pending.extend((child, location) for child in node)
    return dependencies


def seeds(draft):
    result = set()
    for row in draft["filings"]:
        result.update(issue_urls(row["issues"]))
        directory = filing.ROOT / row["cik"] / row["accession"]
        for source in row["cached_sources"]:
            name = source["filename"]
            require(filing.NAME.fullmatch(name) and ".." not in name, "INVALID_DRAFT_SOURCE_NAME")
            if not name.lower().endswith((".xsd", ".xml", ".htm", ".html")):
                continue
            data, metadata = filing.cache_read(directory / name, row, name)
            require(metadata["sha256"] == source["sha256"], "DRAFT_SOURCE_HASH_MISMATCH")
            for match in REFERENCE.findall(data.decode("utf-8", errors="replace")):
                if url := normalize(match):
                    result.add(url)
    return result


def issue_urls(issues):
    return {
        url
        for issue in issues
        if issue.get("code") == "IOerror"
        for word in re.findall(r'https?://[^\s<>"]+', issue.get("reason", ""))
        if (url := normalize(word.rstrip(".,;)")))
    }


def package_url(url):
    _, host, *path = parts(url)
    if len(path) < 3:
        return None
    family, year = path[:2]
    if host == "xbrl.sec.gov" and year in {"2024", "2025", "2026"}:
        return f"https://{host}/{year}.zip"
    if host == "xbrl.fasb.org" and family in {"us-gaap", "srt"}:
        if year in {"2018", "2019", "2021"}:
            return f"https://{host}/{family}/{year}/{family}-{year}-01-31.zip"
        if year in {"2023", "2024", "2025", "2026"}:
            return f"https://{host}/{family}/{year}/{family}-{year}.zip"
    return None


def member_bytes(raw, url):
    _, host, *parts_ = parts(url)
    target = "/".join(parts_)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        items = archive.infolist()
        require(len(items) <= 30000, "TAXONOMY_PACKAGE_MEMBER_LIMIT")
        catalogs = [
            item
            for item in items
            if item.filename.endswith("/META-INF/catalog.xml")
            or item.filename == "META-INF/catalog.xml"
        ]
        mapped = set()
        for catalog in catalogs:
            require(catalog.file_size <= 1024**2, "TAXONOMY_CATALOG_TOO_LARGE")
            xml = archive.read(catalog)
            require(not re.search(rb"<!DOCTYPE|<!ENTITY", xml, re.I), "UNSAFE_TAXONOMY_CATALOG")
            for rule in ET.fromstring(xml).iter():
                kind = rule.tag.rsplit("}", 1)[-1]
                start = rule.get("uriStartString") if kind == "rewriteURI" else rule.get("name")
                prefix = rule.get("rewritePrefix") if kind == "rewriteURI" else rule.get("uri")
                if not start or not prefix or "://" in prefix:
                    continue
                for same_transport_path in (
                    url,
                    url.replace("http://", "https://", 1),
                    url.replace("https://", "http://", 1),
                ):
                    if (kind == "rewriteURI" and same_transport_path.startswith(start)) or (
                        kind == "uri" and same_transport_path == start
                    ):
                        tail = same_transport_path[len(start) :]
                        mapped.add(
                            posixpath.normpath(
                                posixpath.join(
                                    posixpath.dirname(catalog.filename),
                                    prefix + tail,
                                )
                            )
                        )
        candidates = [item for item in items if item.filename in mapped]
        matches = [
            item
            for item in items
            if item.filename == target or item.filename.endswith("/" + target)
        ]
        if candidates:
            matches = candidates
        if not matches and host == "xbrl.sec.gov" and parts_[1] in parts_[-1]:
            tail = f"{parts_[0]}/{parts_[-1]}"
            matches = [
                item
                for item in items
                if item.filename == tail or item.filename.endswith("/" + tail)
            ]
        if not matches and host == "xbrl.fasb.org":
            # FASB version-specific packages commonly omit family/year in member paths.
            tail = "/".join(parts_[2:])
            matches = [
                item
                for item in items
                if item.filename == tail or item.filename.endswith("/" + tail)
            ]
        if len(matches) != 1:
            return None
        item = matches[0]
        require(
            item.file_size <= LIMIT
            and not any(part in {"..", "."} for part in PurePosixPath(item.filename).parts),
            "UNSAFE_TAXONOMY_MEMBER",
        )
        return archive.read(item), item.filename


def verify(path, metadata_path, url):
    bulk.plain(path)
    _, metadata = bulk.read_metadata(metadata_path)
    require(
        metadata["url"] == url
        and 0 < bulk.plain(path).st_size == metadata["bytes"] <= LIMIT
        and filing.pilot.file_sha(path) == metadata["sha256"],
        f"TAXONOMY_CACHE_BINDING_MISMATCH: {url}",
    )
    return path.read_bytes(), metadata


def local_cache_path(root, url):
    return root / "cache" / Path(*parts(url))


@cache
def wheel_records(site):
    record = site / f"arelle_release-{filing.ARELLE_VERSION}.dist-info" / "RECORD"
    if not bulk.exists(record):
        return {}
    bulk.plain(record)
    with record.open(encoding="utf-8", newline="") as stream:
        return {name: digest for name, digest, _ in csv.reader(stream)}


def bound_input(root, url):
    path = local_cache_path(root, url)
    binding = path.with_name(path.name + FILE_BINDING)
    if bulk.exists(path):
        bulk.directory(path.parent)
        if bulk.exists(binding):
            return verify(path, binding, url)
        return None
    site = root / "site"
    relative = Path("arelle", "resources", "cache", *parts(url))
    builtin = site / relative
    if bulk.exists(builtin):
        digest = wheel_records(site).get(relative.as_posix(), "")
        if digest.startswith("sha256="):
            bulk.directory(builtin.parent)
            require(bulk.plain(builtin).st_size <= LIMIT, "BUILTIN_TAXONOMY_TOO_LARGE")
            data = builtin.read_bytes()
            from_record = (
                base64.urlsafe_b64encode(bytes.fromhex(filing.sha(data))).decode().rstrip("=")
            )
            require(digest == f"sha256={from_record}", "BUILTIN_TAXONOMY_HASH_MISMATCH")
            return data, {
                "url": url,
                "bytes": len(data),
                "sha256": filing.sha(data),
                "arelle_release": filing.ARELLE_VERSION,
                "wheel_record": relative.as_posix(),
            }
    return None


def verify_used_packages(root, results):
    packages = {}
    for row in results.values():
        source = row.get("source", {})
        url = source.get("package_url")
        if not url:
            continue
        if url not in packages:
            folder = root / "bootstrap-packages" / url.rsplit("/", 1)[-1]
            bulk.directory(folder)
            _, package = verify(folder / "content", folder / "source.json", url)
            packages[url] = package["sha256"]
        require(packages[url] == source["package_sha256"], "TAXONOMY_PACKAGE_HASH_MISMATCH")
    return packages


def fetch(client, url):
    with closing(client.open_url(url, "GET")) as response:
        require(
            response.status == 200
            and response.headers.get("Content-Encoding", "identity") == "identity",
            "UNEXPECTED_TAXONOMY_RESPONSE",
        )
        data = response.read(LIMIT + 1)
        length = response.headers.get("Content-Length")
        require(
            0 < len(data) <= LIMIT and (length is None or length == str(len(data))),
            "TAXONOMY_DOWNLOAD_LENGTH_MISMATCH",
        )
    return data


def metadata(url, data, source):
    return {
        "url": url,
        "bytes": len(data),
        "sha256": filing.sha(data),
        "retrieved_or_bound_utc": datetime.now(UTC).isoformat(),
        **source,
    }


def ensure_tree(root, folder):
    current = root
    for name in folder.relative_to(root).parts:
        current = filing.safe_directory(current / name)


def package_data(client, root, url):
    packages = filing.safe_directory(root / "bootstrap-packages")
    name = url.rsplit("/", 1)[-1]
    key = packages / name
    if bulk.exists(key):
        bulk.directory(key)
        return verify(key / "content", key / "source.json", url)
    content = fetch(client, url)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        require(archive.infolist(), "EMPTY_TAXONOMY_PACKAGE")
    record = metadata(url, content, {"official_package": True})
    with tempfile.TemporaryDirectory(dir=packages) as parent:
        stage = Path(parent) / "package"
        stage.mkdir()
        bulk.write_new(stage / "content", content)
        bulk.write_new(stage / "source.json", bulk.json_bytes(record))
        stage.rename(key)
    return content, record


def publisher(root, client):
    packages = {}

    def bind(url):
        key = local_cache_path(root, url)
        package = package_url(url)
        member = None
        if package:
            if package not in packages:
                try:
                    packages[package] = package_data(client, root, package)
                except bulk.Blocked as error:
                    if "SEC_HTTP_404" not in str(error):
                        raise
                    packages[package] = None
            archive = packages[package]
            if archive:
                member = member_bytes(archive[0], url)
        if member:
            data, name = member
            source = {
                "package_url": package,
                "package_sha256": packages[package][1]["sha256"],
                "package_member": name,
            }
        else:
            transport = url.replace("http://", "https://", 1)
            data = fetch(client, transport)
            source = {"transport_url": transport}
        if not url.endswith(".dtd"):
            xml_dependencies(data, url)
        meta = metadata(url, data, source)
        ensure_tree(root, key.parent)
        if bulk.exists(key):
            bulk.plain(key)
            require(
                filing.pilot.file_sha(key) == meta["sha256"],
                f"UNBOUND_EXISTING_TAXONOMY_CACHE_FILE: {key}",
            )
        with tempfile.TemporaryDirectory(dir=key.parent) as temporary:
            file = Path(temporary) / "content"
            info = Path(temporary) / "source.json"
            bulk.write_new(file, data)
            bulk.write_new(info, bulk.json_bytes(meta))
            if not bulk.exists(key):
                file.rename(key)
            info.rename(key.with_name(key.name + FILE_BINDING))
        return data, meta

    return bind


def coverage(initial, read, acquire=None):
    pending = deque(sorted(initial))
    results = {}
    while pending:
        url = pending.popleft()
        if url in results:
            continue
        require(len(results) < 1000, "TAXONOMY_DEPENDENCY_LIMIT")
        if not parts(url):
            results[url] = {"status": "UNSUPPORTED_DEPENDENCY_HOST_OR_PATH"}
            continue
        entry = read(url)
        if entry is None and acquire is not None:
            try:
                entry = acquire(url)
            except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as error:
                results[url] = {"status": "MISSING_EVIDENCE", "reason": str(error)}
                continue
        if entry is None:
            results[url] = {"status": "MISSING_EVIDENCE", "preferred_package": package_url(url)}
            continue
        data, meta = entry
        dependencies = sorted(xml_dependencies(data, url)) if not url.endswith(".dtd") else []
        pending.extend(dependencies)
        results[url] = {"status": "BOUND", "dependencies": dependencies, "source": meta}
    return results


def prepare_plugin(root, client):
    parent = filing.safe_directory(root / "plugins")
    target = parent / f"edgar-{filing.EDGAR_COMMIT}"
    if bulk.exists(target):
        require(filing.verified_sec_plugin(root), "SEC_PLUGIN_DIRECTORY_INCOMPLETE")
        return
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        stage = Path(temporary) / "edgar"
        package = stage / "transform"
        package.mkdir(parents=True)
        for name, expected in filing.SEC_PLUGIN_FILES.items():
            data = fetch(client, PLUGIN_BASE + name)
            require(filing.sha(data) == expected, f"SEC_PLUGIN_HASH_MISMATCH: {name}")
            bulk.write_new(package / name, data)
        stage.rename(target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--download", action="store_true")
    modes.add_argument("--offline-coverage", action="store_true")
    parser.add_argument("--authorize-download", action="store_true")
    parser.add_argument("--user-agent")
    parser.add_argument("--draft", type=Path, default=filing.ROOT / DRAFT)
    parser.add_argument("--extraction-diagnostics", type=Path)
    args = parser.parse_args(argv)
    if args.download != args.authorize_download or (args.download and not args.user_agent):
        parser.error("Download requires --authorize-download and --user-agent")
    try:
        candidate = filing.pilot.code_binding()
        raw, draft = bulk.read_metadata(args.draft.absolute(), max_bytes=64 * 1024**2)
        require(
            draft["contract"] in {"F01_ORIGINAL_FILING_EVIDENCE_DRAFT_V1", filing.EXTENDED_INPUT},
            "INVALID_DRAFT",
        )
        if draft["contract"] == filing.EXTENDED_INPUT:
            require(
                args.draft.is_relative_to(filing.ROOT) and draft["candidate"] == candidate,
                "EXTENDED_CACHE_BINDING_MISMATCH",
            )
        filing.validate_rows(draft["filings"])
        if args.draft == filing.ROOT / DRAFT:
            require(filing.sha(raw) == DRAFT_SHA, "ORIGINAL_DRAFT_HASH_MISMATCH")
        root = bulk.directory(filing.ARELLE_ROOT)
        initial = seeds(draft)
        if args.extraction_diagnostics:
            require(
                args.extraction_diagnostics.is_relative_to(filing.ROOT),
                "EXTENDED_RESEARCH_PATH_REQUIRED",
            )
            _, prior = bulk.read_metadata(args.extraction_diagnostics)
            require(
                prior["inputs"]["cache_sha256"] == filing.sha(raw)
                and prior["candidate_code"] == candidate,
                "STALE_EXTRACTION_DIAGNOSTICS",
            )
            for _, row in filing.extended_records(args.extraction_diagnostics, prior):
                initial.update(issue_urls(row["issues"]))
        client = bulk.SecHttp(args.user_agent) if args.download else None
        if client:
            prepare_plugin(root, client)
        results = coverage(
            initial, lambda url: bound_input(root, url), publisher(root, client) if client else None
        )
        plugin = filing.verified_sec_plugin(root)
        print(
            json.dumps(
                {
                    "candidate": candidate,
                    "draft_sha256": filing.sha(raw),
                    "initial_urls": len(initial),
                    "urls": results,
                    "verified_packages": verify_used_packages(root, results),
                    "sec_plugin_commit": filing.EDGAR_COMMIT,
                    "sec_plugin_verified": bool(plugin),
                    "offline_coverage": args.offline_coverage,
                },
                indent=2,
            )
        )
        if args.offline_coverage or args.download:
            return int(not plugin or any(row["status"] != "BOUND" for row in results.values()))
    except (
        bulk.Blocked,
        OSError,
        RuntimeError,
        ValueError,
        ET.ParseError,
        KeyError,
        TypeError,
    ) as error:
        print(f"TAXONOMY_BOOTSTRAP_BLOCKED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
