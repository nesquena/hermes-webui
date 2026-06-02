from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class _ButtonParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "text": [], "has_svg": False}
        if self.stack:
            if tag == "svg":
                self.stack[-1]["has_svg"] = True
        self.stack.append(node)

    def handle_endtag(self, tag):
        if not self.stack:
            return
        node = self.stack.pop()
        if node["tag"] == "button":
            self.buttons.append(node)
        if self.stack:
            self.stack[-1]["text"].extend(node["text"])
            self.stack[-1]["has_svg"] = self.stack[-1]["has_svg"] or node["has_svg"]

    def handle_data(self, data):
        if self.stack:
            self.stack[-1]["text"].append(data)


def test_icon_only_buttons_have_accessible_names():
    parser = _ButtonParser()
    parser.feed((ROOT / "static" / "index.html").read_text(encoding="utf-8"))
    offenders = []
    for button in parser.buttons:
        attrs = button["attrs"]
        text = "".join(button["text"]).strip()
        has_accessible_name = any(attrs.get(name) for name in ("aria-label", "data-i18n-aria-label", "aria-labelledby"))
        if button["has_svg"] and not text and not has_accessible_name:
            offenders.append(attrs.get("id") or attrs.get("class") or str(attrs))
    assert not offenders, "icon-only buttons missing aria labels: " + ", ".join(offenders)
