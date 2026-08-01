def extract_function(js: str, name: str, prefix: str = "function") -> str:
    marker = f"{prefix} {name}("
    start = js.find(marker)
    assert start >= 0, f"{name} function not found in static/panels.js"
    paren = js.find("(", start)
    assert paren >= 0, f"{name} opening parenthesis not found"
    depth = 1
    i = paren + 1
    while i < len(js) and depth > 0:
        if js[i] == "(":
            depth += 1
        elif js[i] == ")":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} parentheses unbalanced"
    brace = js.find("{", i)
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
