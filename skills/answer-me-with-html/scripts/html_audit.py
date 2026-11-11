#!/usr/bin/env python3
"""Check standalone HTML without changing it. Python 3.10+; browser needs Playwright."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit


LIMITATIONS = [
    "Static rules are not complete CSS/JavaScript parsing or security analysis.",
    "Visual quality, semantic correctness and arbitrary interactions are not verified.",
]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
EXCLUDED = {"script", "style", "code", "pre", "textarea"}
CSS_URL = re.compile(r"url\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s)]*))\s*\)", re.I)
CSS_IMPORT = re.compile(r"@import\s+(?:\"([^\"]*)\"|'([^']*)')", re.I)
RAW_FORMULA = re.compile(r"\\\(|\\\[|\$\$")


def result(mode: str) -> dict:
    return {"mode": mode, "ok": False, "errors": [], "visual_review": "not_performed",
            "interaction_review": "not_performed", "limitations": list(LIMITATIONS)}


def fail(report: dict, code: str, **detail) -> None:
    report["errors"].append({"code": code, **detail})


def snapshot(path: Path) -> tuple[bytes, dict]:
    before = path.stat()
    if not path.is_file():
        raise ValueError("Input must be a regular local HTML file")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("Input changed while being read")
    return data, {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def unchanged(path: Path, identity: dict) -> None:
    _, current = snapshot(path)
    if current != identity:
        raise ValueError("Input changed during audit")


def write_json(path: Path, report: dict) -> None:
    # Exclusive creation also protects against a race after argument checking.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class StaticAudit(HTMLParser):
    def __init__(self, report: dict):
        super().__init__(convert_charrefs=True)
        self.report = report
        self.stack: list[str] = []
        self.ids: set[str] = set()
        self.anchors: list[tuple[str, int]] = []
        self.styles: list[str] = []
        self.text: list[str] = []
        self.text_line = 1

    def error(self, code: str, **detail) -> None:
        fail(self.report, code, line=self.getpos()[0], **detail)

    def resource(self, value: str, location: str) -> None:
        value = value.strip()
        if value and not value.lower().startswith("data:") and not value.startswith("#"):
            self.error("external-resource", location=location, value=value)

    def css(self, text: str, location: str) -> None:
        # Deliberately a bounded heuristic, not a CSS interpreter.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        for match in CSS_URL.finditer(text):
            self.resource(next(x for x in match.groups() if x is not None), location)
        for match in CSS_IMPORT.finditer(text):
            self.resource(next(x for x in match.groups() if x is not None), location + ".import")

    def srcset(self, value: str, location: str) -> None:
        # URL token ends at whitespace; trailing commas delimit candidates.
        # Commas inside data URLs belong to the URL, not candidate separators.
        remaining = value.strip()
        while remaining:
            remaining = remaining.lstrip(" ,\t\r\n")
            if not remaining:
                break
            # Tabs/newlines also separate descriptors from the URL.
            match = re.match(r"([^\s]+)(.*)", remaining, re.S)
            if not match:
                break
            token, rest = match.groups()
            if token.lower().startswith("data:"):
                self.resource(token.rstrip(","), location)
                if token.endswith(","):
                    remaining = rest
                else:
                    remaining = rest.partition(",")[2]
            else:
                # Adjacent ordinary candidates may have no separating space.
                for url in token.split(","):
                    self.resource(url, location)
                remaining = rest if token.endswith(",") else rest.partition(",")[2]

    def flush_text(self) -> None:
        if self.text:
            text = "".join(self.text)
            if RAW_FORMULA.search(text):
                fail(self.report, "raw-formula", line=self.text_line, text=text.strip()[:180])
            self.text.clear()

    def handle_starttag(self, tag: str, attrs) -> None:
        self.flush_text()
        attrs = dict(attrs)
        value = attrs.get("id")
        if value is not None:
            if value in self.ids:
                self.error("duplicate-id", value=value)
            self.ids.add(value)
        if tag == "a" and attrs.get("name"):
            self.anchors.append(("__named_target__" + attrs["name"], self.getpos()[0]))
        for name in ("href", "xlink:href"):
            href = attrs.get(name) or ""
            if href.startswith("#") and len(href) > 1:
                self.anchors.append((href, self.getpos()[0]))
        resources = {
            "script": ("src",), "img": ("src",), "input": ("src",),
            "audio": ("src",), "video": ("src", "poster"), "source": ("src",),
            "track": ("src",), "iframe": ("src",), "embed": ("src",),
            "object": ("data",), "image": ("href", "xlink:href"),
            "use": ("href", "xlink:href"), "feimage": ("href", "xlink:href"),
            "body": ("background",), "table": ("background",), "td": ("background",),
        }
        for name in resources.get(tag, ()):
            if attrs.get(name):
                self.resource(attrs[name], f"{tag}.{name}")
        if tag in {"img", "source"} and attrs.get("srcset"):
            self.srcset(attrs["srcset"], f"{tag}.srcset")
        if tag == "link":
            active = {"stylesheet", "icon", "preload", "modulepreload", "prefetch", "manifest", "preconnect", "dns-prefetch", "apple-touch-icon", "mask-icon"}
            if active.intersection((attrs.get("rel") or "").lower().split()):
                self.resource(attrs.get("href") or "", "link.href")
            if attrs.get("imagesrcset"):
                self.srcset(attrs["imagesrcset"], "link.imagesrcset")
        if tag == "base" and attrs.get("href"):
            self.resource(attrs["href"], "base.href")
        if tag == "meta" and (attrs.get("http-equiv") or "").lower() == "refresh":
            self.error("automatic-navigation", value=attrs.get("content") or "")
        if attrs.get("style"):
            self.css(attrs["style"], f"{tag}.style")
        if tag not in VOID:
            self.stack.append(tag)
        if tag == "style":
            self.styles = []

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self.flush_text()
        if tag == "style":
            self.css("".join(self.styles), "style")
            self.styles = []
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1] == "style":
            self.styles.append(data)
        if not EXCLUDED.intersection(self.stack):
            if not self.text:
                self.text_line = self.getpos()[0]
            self.text.append(data)

    def finish(self) -> None:
        self.close()
        self.flush_text()
        if self.styles:
            self.css("".join(self.styles), "style")
        named = {href.removeprefix("__named_target__") for href, _ in self.anchors if href.startswith("__named_target__")}
        for href, line in self.anchors:
            if href.startswith("#") and unquote(href[1:]) not in self.ids | named:
                fail(self.report, "broken-anchor", line=line, value=href)


def static_audit(data: bytes, report: dict) -> None:
    parser = StaticAudit(report)
    parser.feed(data.decode("utf-8-sig", errors="strict"))
    parser.finish()
    report["scope"] = "UTF-8 HTML: active non-embedded resource attributes, heuristic CSS URLs/imports, IDs, fragment targets, raw \\( / \\[ / $$ outside script/style/code/pre/textarea. Ordinary reference links are allowed. Inline scripts, CSP and local screenshot reference links are not errors."


def browser_executable(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("--browser-exe must name an existing Edge/Chrome executable")
        return path
    candidates: list[Path] = []
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if root:
            candidates.extend(Path(root) / tail for tail in (
                "Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"))
    for name in ("msedge", "msedge.exe", "google-chrome", "chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise RuntimeError("No installed Edge/Chrome found; supply --browser-exe. No browser will be downloaded.")


GEOMETRY = r"""() => {
    const findings = [];
    const root = document.documentElement;
    if (Math.max(root.scrollWidth, document.body?.scrollWidth || 0) > innerWidth + 2)
        findings.push({code:'page-overflow', viewport:innerWidth,
            scroll_width:Math.max(root.scrollWidth, document.body?.scrollWidth || 0)});
    const rect = r => ({left:r.left, top:r.top, right:r.right, bottom:r.bottom,
                        width:r.width, height:r.height});
    for (const [index, svg] of [...document.querySelectorAll('svg')].entries()) {
        if (svg.closest('mjx-container')) continue; // Typeset math isn't a labelled diagram.
        const bounds = svg.getBoundingClientRect();
        if (!bounds.width || !bounds.height || getComputedStyle(svg).visibility === 'hidden') continue;
        const labels = [];
        for (const text of svg.querySelectorAll('text')) {
            if (text.ownerSVGElement !== svg) continue;
            const r = text.getBoundingClientRect(), style = getComputedStyle(text);
            if (!text.textContent.trim() || !r.width || !r.height ||
                style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
            const label = {text:text.textContent.trim().slice(0,200), rect:rect(r)};
            labels.push(label);
            if (r.left < bounds.left-2 || r.right > bounds.right+2 ||
                r.top < bounds.top-2 || r.bottom > bounds.bottom+2)
                findings.push({code:'label-outside-svg', svg:svg.id || index, ...label});
        }
        labels.sort((a,b) => a.rect.left-b.rect.left);
        for (let i=0; i<labels.length; i++) {
            const a=labels[i];
            for (let j=i+1; j<labels.length && labels[j].rect.left<a.rect.right-2; j++) {
                const b=labels[j];
                if (Math.min(a.rect.right,b.rect.right)-Math.max(a.rect.left,b.rect.left)>2 &&
                    Math.min(a.rect.bottom,b.rect.bottom)-Math.max(a.rect.top,b.rect.top)>2)
                    findings.push({code:'label-overlap',svg:svg.id || index,labels:[a,b]});
            }
        }
    }
    return findings;
}"""


def browser_audit(path: Path, data: bytes, args, report: dict, directory: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("browser requires already-installed Python Playwright (with WebSocket routing support); nothing was installed") from exc
    executable = browser_executable(args.browser_exe)
    report.update(browser_executable=str(executable), views=[], scope="Initial load only, at each requested viewport; blocked non-embedded resource/network requests, JS errors, horizontal overflow and SVG text bounding-box heuristics.")
    report["limitations"].extend([
        "Only initial load plus a 250 ms settling window is observed; delayed requests and arbitrary interactions are not covered.",
        "Request interception is not an OS security sandbox; browser background traffic is not certified absent.",
        "SVG text axis-aligned bounds (2 CSS px tolerance) can overreport rotated or intentionally overlapping labels; no general occlusion detector.",
        "Screenshots and optional print PDF require human review; pagination and visual correctness are not passed.",
    ])
    # Explicit executable: never select Playwright's bundled/downloadable Chromium.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True,
            args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
        try:
            for width in args.width or [390, 1280, 1600]:
                context = browser.new_context(viewport={"width": width, "height": 1000},
                    reduced_motion="reduce", service_workers="block", accept_downloads=False)
                try:
                    if not hasattr(context, "route_web_socket"):
                        raise RuntimeError("Installed Playwright lacks context.route_web_socket; browser audit cannot safely cover WebSocket attempts")
                    page = context.new_page()
                    initial = True

                    def route_request(route):
                        nonlocal initial
                        request = route.request
                        url = request.url
                        if initial and url == path.as_uri() and request.is_navigation_request() and request.frame == page.main_frame:
                            initial = False
                            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=data)
                        elif urlsplit(url).scheme.lower() in {"data", "blob", "about"}:
                            route.continue_()
                        else:
                            fail(report, "blocked-resource", width=width, url=url, resource_type=request.resource_type)
                            route.abort("blockedbyclient")

                    def block_socket(socket):
                        fail(report, "blocked-websocket", width=width, url=socket.url)
                        # No connect_to_server: Playwright keeps this endpoint local.
                        # context.close releases it; sync close in this callback can deadlock.

                    context.route("**/*", route_request)
                    context.route_web_socket("**/*", block_socket)
                    # HTTP/WebSocket routing does not cover these transports.
                    # Fail closed on APIs whose worker/native traffic we cannot audit.
                    context.expose_binding("__htmlAuditBlocked", lambda source, api, target:
                        fail(report, "blocked-uninspectable-api", width=width, api=api, target=target))
                    context.add_init_script(r"""(() => {
                        const notify = globalThis.__htmlAuditBlocked;
                        for (const api of ['WebTransport', 'RTCPeerConnection',
                                           'webkitRTCPeerConnection', 'Worker', 'SharedWorker']) {
                            if (!(api in globalThis)) continue;
                            Object.defineProperty(globalThis, api, {configurable:false,
                                writable:false, value:function(...args) {
                                    notify(api, typeof args[0] === 'string' ? args[0] : '');
                                    throw new Error('HTML audit blocks uninspectable API: '+api);
                                }});
                        }
                        if (navigator.serviceWorker) {
                            Object.defineProperty(navigator.serviceWorker, 'register', {
                                configurable:false, writable:false, value:function(url) {
                                    notify('ServiceWorker.register', String(url));
                                    return Promise.reject(new Error('HTML audit blocks service workers'));
                                }});
                        }
                    })();""")
                    page.on("pageerror", lambda error: fail(report, "page-error", width=width, message=str(error)))
                    page.on("console", lambda message: fail(report, "console-error", width=width, message=message.text) if message.type == "error" else None)
                    context.on("page", lambda popup: fail(report, "unexpected-page", width=width, url=popup.url))
                    page.on("download", lambda download: fail(report, "download-attempt", width=width, filename=download.suggested_filename))
                    page.goto(path.as_uri(), wait_until="load", timeout=30000)
                    page.evaluate("() => Promise.race([document.fonts.ready, new Promise((_, reject) => setTimeout(() => reject(new Error('Font readiness timed out')), 10000))])")
                    page.wait_for_timeout(250)
                    for finding in page.evaluate(GEOMETRY):
                        fail(report, finding.pop("code"), width=width, **finding)
                    screenshot = f"viewport-{width}.png"
                    page.screenshot(path=str(directory / screenshot), full_page=True, timeout=30000)
                    view = {"width": width, "height": 1000, "screenshot": screenshot}
                    if args.print_pdf and width == (args.width or [390, 1280, 1600])[-1]:
                        page.pdf(path=str(directory / "print.pdf"), format="A4", print_background=True)
                        view["print_pdf"] = "print.pdf"
                    report["views"].append(view)
                finally:
                    context.close()
        finally:
            browser.close()


def positive_width(value: str) -> int:
    width = int(value)
    if width <= 0:
        raise argparse.ArgumentTypeError("width must be positive")
    return width


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"ok": False, "errors": [{"code": "arguments", "message": message}]}, ensure_ascii=False))
        raise SystemExit(2)


def main() -> int:
    parser = JsonParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True, parser_class=JsonParser)
    static = commands.add_parser("static", help="standard-library static heuristics")
    static.add_argument("file", type=Path)
    static.add_argument("--output", type=Path, help="new JSON file; never overwritten")
    browser = commands.add_parser("browser", help="initial-load browser audit, not visual acceptance")
    browser.add_argument("file", type=Path)
    browser.add_argument("--artifacts", type=Path, required=True, help="new directory, including screenshots and browser.json")
    browser.add_argument("--browser-exe")
    browser.add_argument("--width", type=positive_width, action="append", help="repeat; default: 390,1280,1600")
    browser.add_argument("--print-pdf", action="store_true", help="print final viewport to print.pdf; not pagination validation")
    args = parser.parse_args()
    report = result(args.mode)
    output = None
    try:
        path = args.file.expanduser().resolve(strict=True)
        data, identity = snapshot(path)
        report["source"] = identity
        # Fail decoding rather than silently auditing replacement characters.
        data.decode("utf-8-sig", errors="strict")
        if args.mode == "static":
            if args.output:
                output = args.output.expanduser().absolute()
                if output.exists():
                    raise FileExistsError(f"Refusing existing output: {output}")
            static_audit(data, report)
        else:
            if args.width and len(args.width) != len(set(args.width)):
                raise ValueError("Repeated width values would overwrite screenshots")
            directory = args.artifacts.expanduser().absolute()
            directory.mkdir(parents=False, exist_ok=False)
            output = directory / "browser.json"
            browser_audit(path, data, args, report, directory)
        unchanged(path, identity)
        report["ok"] = not report["errors"]
    except Exception as exc:
        fail(report, "audit-unavailable", message=str(exc), exception=type(exc).__name__)
        report["ok"] = False
    if output is not None:
        try:
            write_json(output, report)
        except Exception as exc:
            fail(report, "report-write-failed", message=str(exc), exception=type(exc).__name__)
            report["ok"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())