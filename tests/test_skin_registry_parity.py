"""Every built-in skin must be registered in all three skin registries.

The JS `_SKINS` array (static/boot.js) drives the settings picker and the
`/theme` autocomplete; the inline no-flash allowlist (static/index.html)
applies the skin before first paint; `_SETTINGS_SKIN_VALUES`
(api/config.py) gates server-side persistence. A skin missing from any of
the three either flashes, silently reverts to `default` on reload, or never
shows up in the picker — so this test keeps them in lockstep.
"""

import re
from pathlib import Path

REPO = Path(__file__).parent.parent
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CONFIG_PY = (REPO / "api" / "config.py").read_text(encoding="utf-8")
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")


def _boot_skins():
    block = re.search(r"const _SKINS=\[(.*?)\];", BOOT_JS, re.DOTALL)
    assert block, "could not locate _SKINS array in boot.js"
    skins = set()
    for entry in re.finditer(
        r"\{name:'([^']+)'\s*,(?:\s*value:'([^']+)'\s*,)?", block.group(1)
    ):
        name, value = entry.groups()
        skins.add(value or name.lower())
    assert len(skins) >= 20, f"suspiciously few skins parsed: {sorted(skins)}"
    return skins


def _index_allowlist():
    block = re.search(r"skins=\{([^}]*)\}", INDEX_HTML)
    assert block, "could not locate inline skins allowlist in index.html"
    return {
        key.strip().strip("'\"")
        for key in re.findall(r"([A-Za-z0-9'\"-]+):1", block.group(1))
    }


def _server_allowlist():
    block = re.search(r"_SETTINGS_SKIN_VALUES = \{(.*?)\}", CONFIG_PY, re.DOTALL)
    assert block, "could not locate _SETTINGS_SKIN_VALUES in config.py"
    return set(re.findall(r'"([a-z0-9-]+)"', block.group(1)))


def test_boot_skins_are_in_index_noflash_allowlist():
    missing = _boot_skins() - _index_allowlist()
    assert not missing, f"skins missing from index.html no-flash allowlist: {sorted(missing)}"


def test_boot_skins_are_in_server_allowlist():
    missing = _boot_skins() - _server_allowlist()
    assert not missing, f"skins missing from _SETTINGS_SKIN_VALUES: {sorted(missing)}"


def test_allowlists_contain_no_orphan_skins():
    """The reverse direction: allowlisted names must exist as real skins.

    `default` is implicit in the picker (no data-skin attribute) but listed
    in both allowlists, so it is exempted.
    """
    skins = _boot_skins() | {"default"}
    orphans_index = _index_allowlist() - skins
    orphans_server = _server_allowlist() - skins
    assert not orphans_index, f"index.html allowlists unknown skins: {sorted(orphans_index)}"
    assert not orphans_server, f"config.py allowlists unknown skins: {sorted(orphans_server)}"
