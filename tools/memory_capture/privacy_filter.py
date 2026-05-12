from __future__ import annotations

import re
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Redaction:
    type: str
    replacement: str


@dataclass(frozen=True)
class SanitizeResult:
    text: str
    redactions: list[Redaction]
    safe_to_store: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "redactions": [asdict(r) for r in self.redactions],
            "safe_to_store": self.safe_to_store,
        }


PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "private_block",
        re.compile(r"<private>[\s\S]*?</private>", re.IGNORECASE),
        "[REDACTED-PRIVATE-BLOCK]",
    ),
    (
        "openai_api_key",
        re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
        "[REDACTED-OPENAI-PROJECT-KEY]",
    ),
    (
        "openai_api_key",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
        "[REDACTED-OPENAI-KEY]",
    ),
    (
        "bearer_token",
        re.compile(r"Bearer\s+[A-Za-z0-9._\-+/=]{20,}", re.IGNORECASE),
        "Bearer [REDACTED-BEARER-TOKEN]",
    ),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{40,})"),
        "[REDACTED-GITHUB-TOKEN]",
    ),
    (
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "[REDACTED-AWS-ACCESS-KEY]",
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "[REDACTED-PRIVATE-KEY]",
    ),
    (
        "env_secret",
        re.compile(
            r"(?i)\b(api[_-]?key|token|password|passwd|secret|credential|auth)\b\s*[=:]\s*[\"']?[^\s\"']{12,}[\"']?"
        ),
        "[REDACTED-SECRET-ASSIGNMENT]",
    ),
]


def sanitize_text(text: str) -> SanitizeResult:
    """Redact common sensitive values before quarantine writes.

    This is a safety layer, not a proof of safety. Any redaction marks the item
    as `safe_to_store=False` so Yuto/human review is required before promotion.
    """
    output = text
    redactions: list[Redaction] = []
    for kind, pattern, replacement in PATTERNS:
        output, count = pattern.subn(replacement, output)
        if count:
            redactions.extend(Redaction(type=kind, replacement=replacement) for _ in range(count))
    return SanitizeResult(text=output, redactions=redactions, safe_to_store=not redactions)
