"""
Hermes WebUI -- Smart LLM Router
=================================
Classifies the user's message into SIMPLE / MEDIUM / COMPLEX and returns
the best model to use.

Classification pipeline:
  L1 — Gemma-2-2b via Ollama (local, ~80-120ms)
       If confidence >= 0.85 → use result
  L2 — GPT-5 Mini via Databricks (only when L1 confidence < 0.85 or Ollama unavailable)
       If confidence >= 0.85 → use result
  L3 — Keyword rules from route_rules.yaml (safety net when both LLMs fail)
  L4 — Fallback: MEDIUM (haiku)

Routing table:
  SIMPLE  → databricks-gpt-5-mini        (factual, short answer, conversational)
  MEDIUM  → databricks-claude-haiku-4-5  (explanation, reasoning, how-to)
  COMPLEX → databricks-claude-sonnet-4-6 (code, research, deep analysis, legal/medical)
"""

import json
import logging
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[smart_router] %(levelname)s %(message)s"))
    logger.addHandler(_h)
logger.setLevel(logging.DEBUG)

# ── Routing table ────────────────────────────────────────────────────────────
CLASS_MODEL_MAP = {
    "SIMPLE":  "databricks-gpt-5-mini",
    "MEDIUM":  "databricks-claude-haiku-4-5",
    "COMPLEX": "databricks-claude-sonnet-4-6",
}
FALLBACK_CLASS = "MEDIUM"
CONFIDENCE_THRESHOLD = 0.85

# ── Rules path ───────────────────────────────────────────────────────────────
_DEFAULT_RULES = os.path.join(
    os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")),
    "route_rules.yaml",
)
RULES_PATH = os.environ.get("HERMES_ROUTE_RULES", _DEFAULT_RULES)

# ── Classifier prompt ────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a query classifier. Classify the user query into exactly one of:
- SIMPLE: factual question, short answer, greeting, translation, definition
- MEDIUM: requires explanation, some reasoning, how-to, summary, comparison
- COMPLEX: code writing/debugging, deep research, multi-step planning, legal or medical analysis

Respond with JSON only, no explanation. Example: {"class": "SIMPLE", "confidence": 0.95}"""

_FEW_SHOT = """Examples:
"what is the capital of France" -> {"class": "SIMPLE", "confidence": 0.98}
"hello how are you" -> {"class": "SIMPLE", "confidence": 0.97}
"explain how transformers work" -> {"class": "MEDIUM", "confidence": 0.91}
"summarize this article" -> {"class": "MEDIUM", "confidence": 0.93}
"write a python function to sort a list" -> {"class": "COMPLEX", "confidence": 0.96}
"design a microservices architecture" -> {"class": "COMPLEX", "confidence": 0.94}
"is this contract clause enforceable" -> {"class": "COMPLEX", "confidence": 0.92}"""


def _parse_classification(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON classification from LLM response text."""
    try:
        # Try direct parse first
        text = text.strip()
        # Find JSON object in response
        match = re.search(r'\{[^}]+\}', text)
        if match:
            data = json.loads(match.group())
            cls = data.get("class", "").upper()
            conf = float(data.get("confidence", 0))
            if cls in CLASS_MODEL_MAP and 0 <= conf <= 1:
                return {"class": cls, "confidence": conf}
    except Exception:
        pass
    return None


# ── L1: Gemma via Ollama ─────────────────────────────────────────────────────
class GemmaClassifier:
    """Calls Ollama local REST API to classify query using Gemma-2-2b."""

    def __init__(self, endpoint: str = None, model: str = "gemma2:2b", timeout: float = 10.0):
        if endpoint is None:
            try:
                import subprocess
                gw = subprocess.check_output(["ip","route","show","default"],text=True).split()[2]
                endpoint = f"http://{gw}:11434"
            except Exception:
                endpoint = "http://localhost:11434"
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._available: Optional[bool] = None  # cached availability

    def _check_available(self) -> bool:
        """Check if Ollama is running."""
        try:
            req = urllib.request.Request(f"{self.endpoint}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=1.0):
                return True
        except Exception:
            return False

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = self._check_available()
        return self._available

    def classify(self, query: str) -> Optional[Dict[str, Any]]:
        """Returns {class, confidence} or None if unavailable/failed."""
        if not self.available:
            return None
        try:
            prompt = f"{_SYSTEM_PROMPT}\n\n{_FEW_SHOT}\n\nUser query: {query}"
            payload = json.dumps({
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 30, "temperature": 0.1},
            }).encode()
            req = urllib.request.Request(
                f"{self.endpoint}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                text = data.get("response", "")
                result = _parse_classification(text)
                if result:
                    logger.info("[smart_router] Gemma: class=%s confidence=%.2f query=%.60s",
                                result["class"], result["confidence"], query)
                return result
        except urllib.error.URLError:
            self._available = False  # mark unavailable for this session
            logger.warning("[smart_router] Ollama unavailable — falling back to L2")
        except Exception as e:
            logger.warning("[smart_router] Gemma classify failed: %s", e)
        return None


# ── L2: GPT-5 Mini via Databricks ────────────────────────────────────────────
class GPTMiniClassifier:
    """Calls Databricks GPT-5 Mini to re-classify when Gemma is low-confidence."""

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def classify(self, query: str) -> Optional[Dict[str, Any]]:
        """Returns {class, confidence} or None if failed."""
        try:
            from api.config import get_config
            cfg = get_config()

            # custom_providers is a list — find the GPT-5 Mini entry
            providers = cfg.get("custom_providers", [])
            db_provider = next(
                (p for p in providers if "gpt-5-mini" in p.get("model", "")),
                next((p for p in providers if p), {})  # fallback to first provider
            )
            base_url = db_provider.get("base_url", "").rstrip("/")
            api_key = db_provider.get("api_key", "")
            model = "databricks-gpt-5-mini"

            if not base_url or not api_key:
                logger.warning("[smart_router] GPT-5 Mini: no Databricks config found")
                return None

            messages = [
                {"role": "system", "content": f"{_SYSTEM_PROMPT}\n\n{_FEW_SHOT}"},
                {"role": "user", "content": f"Classify this query: {query}"},
            ]
            payload = json.dumps({
                "model": model,
                "messages": messages,
                "max_tokens": 30,
                "temperature": 0.1,
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                text = data["choices"][0]["message"]["content"]
                result = _parse_classification(text)
                if result:
                    logger.info("[smart_router] GPT-5 Mini: class=%s confidence=%.2f query=%.60s",
                                result["class"], result["confidence"], query)
                return result
        except Exception as e:
            logger.warning("[smart_router] GPT-5 Mini classify failed: %s", e)
        return None


# ── L3: Keyword rules (existing) ─────────────────────────────────────────────
class LLMRouter:
    """Keyword-based fallback router. Loaded from route_rules.yaml."""

    def __init__(self, rules_path: str = RULES_PATH):
        self.rules_path = rules_path
        self.rules: List[Dict[str, Any]] = []
        self.fallback: Dict[str, Any] = {}
        self._loaded = False
        self._try_load()

    def _try_load(self) -> None:
        p = str(Path(self.rules_path).expanduser().resolve())
        if os.path.exists(p):
            try:
                with open(p, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                self.rules = cfg.get("rules", [])
                self.fallback = cfg.get("fallback", {})
                self._loaded = True
                logger.info("[smart_router] Loaded %d rules from %s", len(self.rules), p)
            except Exception as e:
                logger.warning("[smart_router] Failed to load %s: %s", p, e)

    def reload(self) -> None:
        self._loaded = False
        self._try_load()

    def route(self, query: str) -> Optional[Dict[str, Any]]:
        if not self._loaded or not self.rules:
            return None
        q = query.lower()
        for rule in self.rules:
            for kw in rule.get("keywords", []):
                if kw.lower() in q:
                    return self._hit(rule, kw)
            pattern = rule.get("match")
            if pattern and re.search(pattern, query, flags=re.IGNORECASE):
                return self._hit(rule, pattern)
        fb_model = self.fallback.get("model")
        if fb_model:
            return {
                "model": fb_model,
                "reason": self.fallback.get("reason", "default fallback"),
                "rule_id": "fallback",
                "matched_on": None,
            }
        return None

    @staticmethod
    def _hit(rule: Dict[str, Any], matched_on: str) -> Dict[str, Any]:
        return {
            "model": rule.get("model"),
            "reason": rule.get("reason", ""),
            "rule_id": rule.get("id", "unknown"),
            "matched_on": matched_on,
        }


# ── Module-level singletons ───────────────────────────────────────────────────
_router: Optional[LLMRouter] = None
_gemma: Optional[GemmaClassifier] = None
_gpt_mini: Optional[GPTMiniClassifier] = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router


def get_gemma() -> GemmaClassifier:
    global _gemma
    if _gemma is None:
        _gemma = GemmaClassifier()
    return _gemma


def get_gpt_mini() -> GPTMiniClassifier:
    global _gpt_mini
    if _gpt_mini is None:
        _gpt_mini = GPTMiniClassifier()
    return _gpt_mini


def _class_to_result(cls: str, source: str) -> Dict[str, Any]:
    """Map a class label to a full routing result dict."""
    model = CLASS_MODEL_MAP.get(cls, CLASS_MODEL_MAP[FALLBACK_CLASS])
    return {
        "model": model,
        "class_label": cls,
        "rule_id": cls.lower(),
        "reason": f"{cls} — routed by {source}",
        "matched_on": source,
    }


def _boost_class(cls: str) -> str:
    """Boost class when attachments present: SIMPLE→MEDIUM, MEDIUM→COMPLEX."""
    if cls == "SIMPLE":
        return "MEDIUM"
    elif cls == "MEDIUM":
        return "COMPLEX"
    return cls  # COMPLEX stays COMPLEX


def smart_route(query: str, attachments: list = None) -> Optional[Dict[str, Any]]:
    """
    Main routing entry point.
    Returns a dict with: model, class_label, rule_id, reason, matched_on
    Returns None only if routing is completely disabled.

    Pipeline:
      L1 Gemma (local) → L2 GPT-5 Mini (if low confidence) → L3 keywords → L4 fallback
      
    If attachments present, boost class (SIMPLE→MEDIUM, MEDIUM→COMPLEX).
    """
    attachments = attachments or []
    
    try:
        # ── L1: Gemma ────────────────────────────────────────────────────────
        gemma = get_gemma()
        result = gemma.classify(query)
        if result and result["confidence"] >= CONFIDENCE_THRESHOLD:
            cls = result["class"]
            if attachments:
                cls = _boost_class(cls)
            return _class_to_result(cls, "gemma")

        # ── L2: GPT-5 Mini (low confidence or Gemma unavailable) ─────────────
        if result:
            logger.info("[smart_router] Gemma confidence %.2f < %.2f — escalating to GPT-5 Mini",
                        result["confidence"], CONFIDENCE_THRESHOLD)
        gpt = get_gpt_mini()
        gpt_result = gpt.classify(query)
        if gpt_result and gpt_result["confidence"] >= CONFIDENCE_THRESHOLD:
            cls = gpt_result["class"]
            if attachments:
                cls = _boost_class(cls)
            return _class_to_result(cls, "gpt-5-mini")

        # Use best available LLM result even if below threshold
        best = gpt_result or result
        if best:
            logger.info("[smart_router] Using best available LLM result: class=%s confidence=%.2f",
                        best["class"], best["confidence"])
            cls = best["class"]
            if attachments:
                cls = _boost_class(cls)
            return _class_to_result(cls, "llm-low-confidence")

        # ── L3: Keyword rules ────────────────────────────────────────────────
        router = get_router()
        kw_result = router.route(query)
        if kw_result and kw_result.get("model"):
            # Map keyword rule_id back to class label
            rule_id = kw_result.get("rule_id", "fallback")
            cls = rule_id.upper() if rule_id.upper() in CLASS_MODEL_MAP else FALLBACK_CLASS
            if attachments:
                cls = _boost_class(cls)
            return {
                "model": CLASS_MODEL_MAP.get(cls, CLASS_MODEL_MAP[FALLBACK_CLASS]),
                "class_label": cls,
                "rule_id": rule_id,
                "reason": kw_result.get("reason", "keyword match"),
                "matched_on": kw_result.get("matched_on"),
            }

        # ── L4: Hardcoded fallback ───────────────────────────────────────────
        logger.info("[smart_router] All layers failed — using MEDIUM fallback")
        return _class_to_result(FALLBACK_CLASS, "fallback")

    except Exception as e:
        logger.warning("[smart_router] Routing failed: %s — using MEDIUM fallback", e)
        return _class_to_result(FALLBACK_CLASS, "error-fallback")
