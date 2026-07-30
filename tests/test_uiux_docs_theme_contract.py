from pathlib import Path


DEMO_DOCS = [
    Path("docs/ui-ux/index.html"),
    Path("docs/ui-ux/two-stage-proposal.html"),
]

THEME_VALUES = ("system", "dark", "light")
SKIN_VALUES = (
    "default",
    "ares",
    "mono",
    "slate",
    "poseidon",
    "sisyphus",
    "charizard",
    "sienna",
    "catppuccin",
    "nous",
)
LEGACY_THEME_LABELS = ("Solarized", "Monokai", "Nord", "OLED")


def test_uiux_demo_docs_use_current_theme_skin_axes():
    for doc_path in DEMO_DOCS:
        html = doc_path.read_text()
        assert 'data-theme="' not in html, f"{doc_path} should not use legacy data-theme"
        assert 'class="dark" data-skin="slate"' in html
        assert "classList.toggle('dark'" in html
        assert "dataset.skin" in html

        for theme in THEME_VALUES:
            assert f'data-mode-btn="{theme}"' in html

        for skin in SKIN_VALUES:
            assert f'data-skin-btn="{skin}"' in html

        for label in LEGACY_THEME_LABELS:
            assert f">{label}<" not in html
