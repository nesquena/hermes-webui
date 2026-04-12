from pathlib import Path
import re


REPO = Path(__file__).resolve().parent.parent


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_spanish_locale_block_exists():
    src = read(REPO / "static" / "i18n.js")
    assert "\n  es: {" in src
    assert "_label: 'Español'" in src
    assert "_speech: 'es-ES'" in src


def test_spanish_locale_includes_representative_translations():
    src = read(REPO / "static" / "i18n.js")
    expected = [
        "settings_title: 'Configuración'",
        "login_title: 'Iniciar sesión'",
        "approval_heading: 'Se requiere aprobación'",
        "tab_tasks: 'Tareas'",
        "tab_skills: 'Habilidades'",
        "tab_memory: 'Memoria'",
    ]
    for entry in expected:
        assert entry in src


def test_spanish_locale_covers_english_keys():
    src = read(REPO / "static" / "i18n.js")
    en_match = re.search(r"\n  en: \{([\s\S]*?)\n  \},\n\n  es: \{", src)
    es_match = re.search(r"\n  es: \{([\s\S]*?)\n  \},\n\n  de: \{", src)
    assert en_match, "English locale block not found"
    assert es_match, "Spanish locale block not found"

    key_pattern = re.compile(r"^\s{4}([a-zA-Z0-9_]+):", re.MULTILINE)
    en_keys = set(key_pattern.findall(en_match.group(1)))
    es_keys = set(key_pattern.findall(es_match.group(1)))

    missing = sorted(en_keys - es_keys)
    assert not missing, f"Spanish locale missing keys: {missing}"
