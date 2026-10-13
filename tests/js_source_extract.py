def extract_function(js: str, name: str, prefix: str = "function") -> str:
    marker = f"{prefix} {name}("
    start = js.find(marker)
    assert start >= 0, f"{name} function not found in static/panels.js"
    brace = js.find("{", start)
    assert brace >= 0, f"{name} opening brace not found"
    depth = 1
    i = brace + 1
    while i < len(js) and depth > 0:
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} function braces unbalanced"
    return js[start:i]


def extract_command_entry(js: str, name: str) -> str:
    marker = f"{{name:'{name}'"
    start = js.index(marker, js.index("const COMMANDS=["))
    next_entry = js.find("\n  {name:'", start + len(marker))
    end = next_entry if next_entry >= 0 else js.index("\n];", start)
    return js[start:end].rstrip().removesuffix(",")
