"""Exercise a built user manual in Chromium; never run calculation commands."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--build", type=Path, required=True, help="Sphinx HTML directory")
parser.add_argument("--output", type=Path, required=True, help="QA report and screenshots directory")
args = parser.parse_args()
BUILD = args.build.resolve()
if not (BUILD / "index.html").is_file():
    parser.error("--build must contain the generated index.html")
ROOT = args.output.resolve()
ROOT.mkdir(parents=True, exist_ok=True)
SHOTS = ROOT / "screenshots"
SHOTS.mkdir(exist_ok=True)
report = {"checks": [], "errors": [], "overflow": []}
remote = os.environ.get("MANUAL_URL")
server = None
if remote:
    base = remote.rstrip("/") + "/"
else:
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(BUILD)))
    Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
proxy = None
if remote:
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy_url:
        parts = urlsplit(proxy_url)
        proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
        if parts.username:
            proxy.update(username=parts.username, password=parts.password or "")
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"], proxy=proxy)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        origin = "{uri.scheme}://{uri.netloc}".format(uri=urlsplit(base))
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        page = context.new_page()
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        page.goto(base, wait_until="networkidle")
        page.wait_for_selector(".copybtn")
        page.locator(".copybtn").first.click()
        assert "vaspsolkit" in page.evaluate("navigator.clipboard.readText()")
        report["checks"].append("command copy")
        page.mouse.move(1000, 10)
        page.screenshot(path=str(SHOTS / "desktop.png"), full_page=True)
        page.goto(base + "quickstart.html")
        expect(page.locator(".manual-toc")).to_be_visible()
        assert page.locator(".wy-menu-vertical button.toctree-expand:visible").count() == 0
        toc_links = page.locator(".manual-toc a.reference.internal:visible")
        assert toc_links.count() >= 5
        last = toc_links.last
        href = last.get_attribute("href")
        last.click()
        assert page.url.endswith(href)
        expect(last).to_have_attribute("aria-current", "location")
        page.goto(base + "quickstart.html")
        page.keyboard.press("Tab")
        expect(page.locator(".manual-skip")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()
        report["checks"].append("desktop TOC and keyboard skip")
        for query in ("中性", "分区", "KSPACING"):
            page.goto(base)
            page.locator('input[name="q"]').fill(query)
            page.locator('input[name="q"]').press("Enter")
            page.wait_for_selector("#search-results .search li")
            page.locator("#search-results .search li a").first.click()
            assert page.url.startswith(base)
        report["checks"].append("Chinese and English search")
        page.goto(base + "results.html")
        assert page.locator('img[alt^="合成"]').evaluate("img => img.complete && img.naturalWidth > 1000")
        page.screenshot(path=str(SHOTS / "results.png"), full_page=True)
        pages = [path.name for path in BUILD.glob("*.html") if path.stem not in {"search", "genindex"}]
        for width in (1440, 1024, 390, 360):
            page.set_viewport_size({"width": width, "height": 900})
            for name in pages:
                page.goto(base + name)
                metrics = page.evaluate("""() => ({width: innerWidth,
                    document: document.documentElement.scrollWidth,
                    article: document.querySelector('.wy-nav-content').getBoundingClientRect().right})""")
                if metrics["document"] > width + 1 or metrics["article"] > width + 1:
                    report["overflow"].append({"page": name, **metrics})
        report["checks"].append("all chapters at 1440/1024/390/360 widths")
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(base)
        page.screenshot(path=str(SHOTS / "mobile.png"), full_page=True)
        menu = page.locator(".manual-menu-toggle")
        page.keyboard.press("Tab")
        expect(page.locator(".manual-skip")).to_be_focused()
        page.keyboard.press("Tab")
        expect(menu).to_be_focused()
        page.keyboard.press("Enter")
        expect(menu).to_have_attribute("aria-expanded", "true")
        expect(page.locator('.wy-nav-side input[name="q"]')).to_be_focused()
        for _ in range(24):
            page.keyboard.press("Tab")
            expect(page.locator(":focus")).to_be_in_viewport()
        menu.click()
        expect(menu).to_have_attribute("aria-expanded", "false")
        expect(menu).to_be_focused()
        for width in (360, 768):
            page.set_viewport_size({"width": width, "height": 844})
            menu.click()
            expect(menu).to_have_attribute("aria-expanded", "true")
            menu.click()
            expect(menu).to_have_attribute("aria-expanded", "false")
        page.set_viewport_size({"width": 390, "height": 844})
        menu.focus()
        page.keyboard.press("Enter")
        expect(menu).to_have_attribute("aria-expanded", "true")
        page.keyboard.press("Escape")
        expect(menu).to_have_attribute("aria-expanded", "false")
        expect(menu).to_be_focused()
        menu.click()
        page.locator('.wy-menu-vertical a[href="quickstart.html"]').first.click()
        expect(page.locator("h1")).to_contain_text("第一次计算")
        expect(menu).to_have_attribute("aria-expanded", "false")
        details = page.locator(".manual-mobile-toc")
        details.locator("summary").click()
        last = details.locator("a.reference.internal:visible").last
        href = last.get_attribute("href")
        last.click()
        assert page.url.endswith(href)
        assert not details.evaluate("element => element.open")
        assert page.locator(".wy-nav-top").bounding_box()["y"] >= 0
        expect(page.locator(".manual-menu-toggle")).to_be_in_viewport()
        report["checks"].append("mobile keyboard menu, Escape and collapsible TOC")
        menu.click()
        page.set_viewport_size({"width": 1440, "height": 1000})
        expect(page.locator('.wy-nav-side input[name="q"]')).to_be_enabled()
        assert not page.locator(".wy-nav-side").evaluate("element => element.inert")
        assert not page.locator("#main-content").evaluate("element => element.inert")
        report["checks"].append("drawer click-close, tab traversal and responsive focus")
        if not remote:
            page.goto((BUILD / "search.html").as_uri() + "?q=KSPACING")
            page.wait_for_selector("#search-results .search li")
            report["checks"].append("offline file search")
            no_js = browser.new_context(java_script_enabled=False, viewport={"width": 1440, "height": 1000})
            plain = no_js.new_page()
            plain.goto(base + "quickstart.html")
            plain.locator(".manual-toc a.reference.internal:visible").last.click()
            assert "#" in plain.url
            report["checks"].append("TOC without JavaScript")
        if os.environ.get("EXPECTED_COMMIT"):
            metadata = context.request.get(base + "version.json?revision=" + os.environ["EXPECTED_COMMIT"]).json()
            assert metadata["commit"] == os.environ["EXPECTED_COMMIT"], metadata
            report["checks"].append("published source revision")
        browser.close()
    assert not report["overflow"], report["overflow"]
    assert not report["errors"], report["errors"]
finally:
    if server:
        server.shutdown()
        server.server_close()
    (ROOT / ("published-report.json" if remote else "browser-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
