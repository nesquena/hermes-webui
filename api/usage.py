"""Usage metric helpers for WebUI display payloads.

Prompt-cache hit percentage is cached prompt reads over the full prompt total
(input + cache reads + cache writes). Keep this calculation in the backend so
browser display code cannot drift across context indicator and per-turn labels.
"""


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def prompt_cache_hit_percent(cache_read_tokens, prompt_tokens):
    """Return cached reads as a percent of full prompt-token total.

    ``prompt_tokens`` must include ordinary input, cache reads, and cache writes
    (matching Agent's ``session_prompt_tokens`` value).
    """
    cache_read = _to_int(cache_read_tokens)
    prompt = _to_int(prompt_tokens)
    if cache_read <= 0 or prompt <= 0:
        return None
    return min(100, round((cache_read / prompt) * 100))
