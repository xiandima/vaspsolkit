"""Build the user manual without importing the calculation runtime."""
from pathlib import Path
import json
import os
import re

ROOT = Path(__file__).resolve().parents[1]
project = "VASPsolKit"
author = "VASPsolKit contributors"
copyright = "2026, VASPsolKit contributors"
release = re.search(r'^__version__ = "([^"]+)"', (ROOT / "vaspsolkit/__init__.py").read_text(), re.M).group(1)
version = release + " dev"

extensions = ["myst_parser", "sphinx_copybutton"]
source_suffix = {".md": "markdown"}
root_doc = "index"
language = "zh_CN"
exclude_patterns = ["_build", "_templates", "superpowers/**", "maintainer/**",
                    "DOCUMENTATION_AUDIT.md", "UX_REVIEW.md"]
myst_heading_anchors = 4
myst_enable_extensions = ["colon_fence", "deflist", "substitution"]
source_commit = os.environ.get("GITHUB_SHA", "")
if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
    source_commit = ""
myst_substitutions = {
    "source_revision": (
        f"[{source_commit[:12]}](https://github.com/xiandima/vaspsolkit/commit/{source_commit})"
        if source_commit else "本地预览（未记录提交）"
    ),
}
nitpicky = True
html_theme = "sphinx_rtd_theme"
html_title = "VASPsolKit 用户手册"
html_baseurl = "https://xiandima.github.io/vaspsolkit/"
html_short_title = "VASPsolKit"
html_theme_options = {
    "navigation_depth": 3,
    "collapse_navigation": True,
    "sticky_navigation": True,
    "style_nav_header_background": "#176d72",
    "includehidden": True,
    "titles_only": False,
}
html_static_path = ["_static"]
html_css_files = ["manual.css"]
html_show_sourcelink = False
html_show_sphinx = False
# Keep file:// search self-contained; browser security blocks fetched snippets.
html_show_search_summary = False
html_last_updated_fmt = None
html_context = {"display_github": False}
copybutton_prompt_text = r"\$ |>>> |\.\.\. "
copybutton_prompt_is_regexp = True
copybutton_copy_empty_lines = False


def write_build_metadata(app, exception):
    if exception is None:
        (Path(app.outdir) / "version.json").write_text(
            json.dumps({"version": release, "commit": source_commit or None}) + "\n",
            encoding="utf-8",
        )


def setup(app):
    app.connect("build-finished", write_build_metadata)
