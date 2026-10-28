"""Contracts for the user documentation, independent of HPC executables."""

import csv
import json
import os
from pathlib import Path
import runpy
from urllib.parse import unquote, urlsplit
from html.parser import HTMLParser


ROOT = Path(__file__).resolve().parents[1]


def test_site_uses_markdown_and_excludes_maintainer_history():
    config = runpy.run_path(str(ROOT / "docs/conf.py"))
    assert config["html_theme"] == "sphinx_rtd_theme"
    assert "myst_parser" in config["extensions"]
    assert config["language"] == "zh_CN"
    assert config["html_show_search_summary"] is False
    assert "superpowers/**" in config["exclude_patterns"]
    assert "DOCUMENTATION_AUDIT.md" in config["exclude_patterns"]


def test_site_navigation_and_chapters_exist():
    index = (ROOT / "docs/index.md").read_text(encoding="utf-8")
    for page in ("quickstart", "inputs", "workflow", "recovery", "results", "reaction", "commands"):
        assert page in index
        assert (ROOT / "docs" / f"{page}.md").is_file()
    for page in ("inputs", "workflow", "recovery", "reaction"):
        assert "{include} USER_GUIDE.md" in (ROOT / "docs" / f"{page}.md").read_text(encoding="utf-8")


def test_manual_navigation_uses_local_accessible_progressive_enhancement():
    config = runpy.run_path(str(ROOT / "docs/conf.py"))
    assert "_templates" in config["templates_path"]
    assert "manual.js" in config["html_js_files"]
    template = (ROOT / "docs/_templates/layout.html").read_text()
    assert 'extends "!layout.html"' in template
    assert 'href="#main-content"' in template
    assert 'id="main-content"' in template
    assert '<button' in template and 'aria-expanded="false"' in template
    assert '<details class="manual-mobile-toc"' in template
    assert 'aria-label="本页目录"' in template
    assert '{{ toc }}' in template
    script = (ROOT / "docs/_static/manual.js").read_text()
    assert 'aria-current' in script
    assert 'Escape' in script
    assert 'fetch(' not in script
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert '*.html' in manifest and '*.js' in manifest


def test_built_manual_has_desktop_and_mobile_toc_when_requested():
    build = os.environ.get("VASPSOLKIT_DOCS_BUILD")
    if not build:
        return
    for name in ("quickstart", "inputs", "recovery", "results"):
        html = (Path(build) / f"{name}.html").read_text()
        assert 'class="manual-toc"' in html
        assert 'class="manual-mobile-toc"' in html
        assert 'id="main-content"' in html
        assert 'class="manual-menu-toggle"' in html


def test_site_records_the_source_commit(monkeypatch, tmp_path):
    from types import SimpleNamespace
    commit = "1234567890abcdef" * 2 + "12345678"
    monkeypatch.setenv("GITHUB_SHA", commit)
    config = runpy.run_path(str(ROOT / "docs/conf.py"))
    assert commit in config["myst_substitutions"]["source_revision"]
    config["write_build_metadata"](SimpleNamespace(outdir=str(tmp_path)), None)
    metadata = json.loads((tmp_path / "version.json").read_text())
    assert metadata["commit"] == commit
    assert metadata["version"] == config["release"]
    assert "{{ source_revision }}" in (ROOT / "docs/versions.md").read_text()


def test_downloadable_example_is_synthetic_and_keeps_all_five_points():
    path = ROOT / "docs/_static/example-summary.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 5
    assert sum(float(row["delta_electrons"]) == 0 for row in rows) == 1
    assert all(row["fit_included"] == "1" for row in rows)
    assert all("synthetic" in row["method_fingerprint"] for row in rows)
    page = (ROOT / "docs/results.md").read_text(encoding="utf-8")
    assert "合成示例" in page and "不是 VASP" in page


def test_docs_deployment_is_test_gated_and_main_only():
    workflow = (ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8")
    assert "docs/requirements.txt" in workflow
    assert "sphinx" in workflow and "upload-pages-artifact" in workflow
    assert "python -m pytest -q" in workflow
    assert "tools/check_manual_browser.py" in workflow
    assert "playwright install --with-deps chromium" in workflow
    assert "needs: build" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.event_name != 'pull_request'" in workflow
    assert "pages: write" in workflow and "id-token: write" in workflow
    assert "actions/deploy-pages" in workflow
    assert 'branches: [main]' in workflow
    assert 'paths:' not in workflow
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "*.css" in manifest and "*.png" in manifest and "*.csv" in manifest
    assert "tools/check_manual_browser.py" in manifest


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag in {"a", "link"} and "href" in attrs:
            self.targets.append(attrs["href"])
        if tag in {"img", "script"} and "src" in attrs:
            self.targets.append(attrs["src"])


def test_built_site_local_links_and_anchors_when_requested():
    build = os.environ.get("VASPSOLKIT_DOCS_BUILD")
    if not build:
        return
    root = Path(build).resolve()
    pages = {}
    for path in root.rglob("*.html"):
        parsed = Links()
        parsed.feed(path.read_text(encoding="utf-8"))
        pages[path] = parsed
    assert root / "index.html" in pages
    for path, parsed in pages.items():
        for target in parsed.targets:
            parts = urlsplit(target)
            if parts.scheme or parts.netloc:
                continue
            resolved = (path.parent / unquote(parts.path)).resolve() if parts.path else path
            if resolved.is_dir():
                resolved = resolved / "index.html"
            assert resolved.is_relative_to(root), (path, target)
            assert resolved.is_file(), (path, target)
            if parts.fragment and resolved in pages:
                assert unquote(parts.fragment) in pages[resolved].ids, (path, target)


def test_built_site_indexes_chinese_and_english_terms_when_requested():
    build = os.environ.get("VASPSOLKIT_DOCS_BUILD")
    if not build:
        return
    script = (Path(build) / "searchindex.js").read_text(encoding="utf-8")
    index = json.loads(script.removeprefix("Search.setIndex(").removesuffix(")"))
    # Sphinx's English stemmer indexes KSPACING as "kspace".
    for term in ("中性", "分区", "kspace"):
        assert term in index["terms"] or term in index["titleterms"], term
