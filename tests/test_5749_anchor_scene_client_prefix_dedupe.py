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

    assert "rowIsLiveTokenFinalPrefix(row,textKey)" in settle_body
    assert "rowHasNonLiveDuplicate" not in settle_body
    assert "_anchorSceneRowLooksLikeFinalAnswer(textKey,finalKey)" in settle_body
    assert "(shorter/longer)>=0.9" in final_overlap_body
