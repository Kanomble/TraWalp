import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_f01_arelle_taxonomies as tax  # noqa: E402

ROOT = "https://xbrl.sec.gov/dei/2025/"
A = ROOT + "a.xsd"
B = ROOT + "sub/b.xsd"
C = ROOT + "c.xsd"


def test_dependency_closure_uses_relative_urls_and_preserves_versions():
    entries = {
        A: (b'<schema><import schemaLocation="sub/b.xsd"/></schema>', {}),
        B: (b'<schema><import schemaLocation="../c.xsd"/></schema>', {}),
    }
    result = tax.coverage({A}, entries.get)
    assert result[A]["dependencies"] == [B]
    assert result[B]["dependencies"] == [C]
    assert result[C]["status"] == "MISSING_EVIDENCE"
    assert tax.package_url(A) == "https://xbrl.sec.gov/2025.zip"


def test_package_catalog_and_exact_cache_hash_binding(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "wrapper/META-INF/catalog.xml",
            (
                f'<catalog><rewriteURI uriStartString="{ROOT}" '
                'rewritePrefix="../schema/"/></catalog>'
            ),
        )
        archive.writestr("wrapper/schema/a.xsd", "<schema/>")
    assert tax.member_bytes(buffer.getvalue(), A) == (b"<schema/>", "wrapper/schema/a.xsd")
    path = tax.local_cache_path(tmp_path, A)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"<schema/>")
    source = tax.metadata(A, b"<schema/>", {})
    path.with_name(path.name + tax.FILE_BINDING).write_bytes(tax.bulk.json_bytes(source))
    assert tax.bound_input(tmp_path, A)[1] == source
    path.write_bytes(b"<changed>")
    with pytest.raises(ValueError, match="TAXONOMY_CACHE_BINDING_MISMATCH"):
        tax.bound_input(tmp_path, A)
