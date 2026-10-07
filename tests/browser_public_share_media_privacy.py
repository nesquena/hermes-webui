#!/usr/bin/env python3
"""Real Chromium DOM/network check after build_share_snapshot and renderMd.

Run with the browser Python and --snapshot-python .venv/bin/python. All HTTP
requests are intercepted; no private file, provider or personal state is used.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = """
import json, tempfile
from pathlib import Path
from tests.test_share_renderer_privacy_review import review_cases, rendered_case, PNG_B64, PRIVATE
with tempfile.TemporaryDirectory() as folder:
    rows=[]
    for body, expected, kind in review_cases():
        content, markup, images=rendered_case(body, Path(folder))
        label = f'<img src="{PRIVATE}">' if kind == "inert-image-label" else (kind.partition(":")[2] if kind.startswith("entity-label:") else None)
        rows.append(dict(body=body, content=content, markup=markup, expected=expected, kind=kind, label=label))
    print(json.dumps(dict(rows=rows, png=PNG_B64.split(',',1)[1])))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-python", required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="share-browser-") as folder:
        env = dict(os.environ, HERMES_HOME=folder + "/home", HERMES_WEBUI_STATE_DIR=folder + "/state")
        result = subprocess.run(
            [args.snapshot_python, "-c", FIXTURES], cwd=ROOT, env=env,
            text=True, capture_output=True, check=True, timeout=60,
        )
    fixture = json.loads(result.stdout)
    results, failures = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        requests = []

        def intercept(route):
            request = route.request
            requests.append(request.url)
            if request.resource_type == "image":
                route.fulfill(status=200, content_type="image/png", body=base64.b64decode(fixture["png"]))
            else:
                route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>")

        for width in (1280, 390):
            page = browser.new_page(viewport={"width": width, "height": 900})
            page.route("**/*", intercept)
            page.goto("https://webui.example/share/review")
            for row in fixture["rows"]:
                requests.clear()
                page.evaluate("html => {document.body.innerHTML=html;}", row["markup"])
                page.wait_for_function("Array.from(document.images).every(img => img.complete)")
                images = page.locator("img").evaluate_all(
                    "images => images.map(img => ({src:img.getAttribute('src'), width:img.naturalWidth}))"
                )
                private = [url for url in requests if urlsplit(url).path == "/api/media"]
                expected = row["expected"]
                passed = not private and (
                    not images if expected is None else
                    len(images) == 1 and images[0]["src"] == expected and images[0]["width"] == 3
                )
                if row["label"] is not None:
                    passed = passed and row["label"] in page.locator("body").text_content()
                evidence = dict(width=width, kind=row["kind"], passed=passed, images=images, requests=list(requests))
                results.append(evidence)
                if not passed:
                    failures.append(dict(evidence, content=row["content"], markup=row["markup"]))
            page.close()
        browser.close()
    if args.evidence:
        args.evidence.write_text(json.dumps(dict(results=results, failures=failures), indent=2))
    print(f"Chromium share privacy: {len(results)} cases, {len(failures)} failures")
    if failures:
        print(json.dumps(failures, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
