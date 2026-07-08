from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body not found")


def test_settled_scene_keys_live_token_prefix_dedupe_to_final_answer_identity():
    settle_body = _function_body(MESSAGES_JS, "_completeSettledAnchorSceneForTurn")
    final_overlap_body = _function_body(MESSAGES_JS, "_anchorSceneRowLooksLikeFinalAnswer")

    assert "lastNonTerminalWorkRowIndex" in settle_body
    assert "rowIsLiveTokenFinalPrefix(row,textKey,rowIndex>lastNonTerminalWorkRowIndex)" in settle_body
    assert "rowHasNonLiveDuplicate" not in settle_body
    assert "_anchorSceneRowLooksLikeFinalAnswer(textKey,finalKey)" in settle_body
    assert "(shorter/longer)>=0.9" in final_overlap_body


def test_render_scene_passes_final_segment_eligibility_to_live_prefix_guard():
    ui_js = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    render_body = _function_body(ui_js, "_renderSettledAnchorSceneTransparentForMessage")
    row_body = _function_body(ui_js, "_anchorSceneTransparentNodeForRow")

    assert "const lastNonTerminalWorkRowIndex=_anchorSceneLastNonTerminalWorkRowIndex(rows);" in render_body
    assert "liveTokenFinalPrefixEligible:idx>lastNonTerminalWorkRowIndex" in render_body
    assert "opts&&opts.liveTokenFinalPrefixEligible&&_anchorSceneLiveTokenFinalPrefix" in row_body
