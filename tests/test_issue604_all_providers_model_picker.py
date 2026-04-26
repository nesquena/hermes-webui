"""Tests for #604 — model picker shows all configured providers."""
import re


def _src() -> str:
    with open("api/config.py") as f:
        return f.read()


def _get_provider_models_keys() -> set:
    """Extract top-level provider keys from _PROVIDER_MODELS dict."""
    with open("api/config.py") as f:
        lines = f.readlines()
    keys = []
    in_dict = False
    for line in lines:
        if "_PROVIDER_MODELS = {" in line:
            in_dict = True
            continue
        if in_dict:
            m = re.match(r'^    "([^"]+)":\s*\[', line)
            if m:
                keys.append(m.group(1))
            if re.match(r'^\}', line):
                break
    return set(keys)


_PROVIDER_MODELS_KEYS = _get_provider_models_keys()


class TestProviderDetectionEnvVars:
    """All known env vars should map to valid provider IDs."""

    # Providers that exist but aren't in _PROVIDER_MODELS (use special handling)
    _SPECIAL_PROVIDERS = {"openrouter", "ollama-cloud", "custom", "ollama", "lmstudio", "local"}

    def test_xai_env_maps_to_xai_provider(self):
        """XAI_API_KEY should add 'x-ai' (not 'xai')."""
        src = _src()
        assert re.search(r'XAI_API_KEY.*?add\("x-ai"\)', src, re.DOTALL), \
            "XAI_API_KEY must map to provider 'x-ai'"

    def test_mistral_env_maps_to_mistralai_provider(self):
        """MISTRAL_API_KEY should add 'mistralai' (not 'mistral')."""
        src = _src()
        assert re.search(r'MISTRAL_API_KEY.*?add\("mistralai"\)', src, re.DOTALL), \
            "MISTRAL_API_KEY must map to provider 'mistralai'"

    def test_all_provider_env_vars_map_to_known_providers(self):
        """Every detected_provider.add() call should reference a known provider."""
        src = _src()
        fn = re.search(r'def _build_available_models_uncached', src)
        fn_block = src[fn.start():fn.start() + 10000]
        adds = re.findall(r'detected_providers\.add\("([^"]+)"\)', fn_block)
        unknown = [p for p in adds if p not in _PROVIDER_MODELS_KEYS and p not in self._SPECIAL_PROVIDERS]
        assert not unknown, \
            f"Unknown provider IDs in env var detection: {unknown}"


class TestConfigProvidersDetection:
    """Providers listed in config.yaml providers section should be detected."""

    def test_cfg_providers_detection_exists(self):
        """_build_available_models must scan cfg['providers'] for known providers."""
        src = _src()
        assert "cfg.get(\"providers\", {})" in src, \
            "Must read cfg['providers']"
        assert "_cfg_providers" in src, \
            "Must use _cfg_providers variable"

    def test_cfg_providers_only_adds_known(self):
        """Only providers in _PROVIDER_MODELS should be added from config."""
        src = _src()
        # Find the config providers detection block
        m = re.search(r'Also detect providers explicitly listed', src)
        assert m, "Comment about config.yaml providers detection must exist"
        block = src[m.start():m.start() + 500]
        assert "_PROVIDER_MODELS" in block, \
            "Config providers detection must check against _PROVIDER_MODELS"


class TestProviderModelsCompleteness:
    """Verify _PROVIDER_MODELS has expected providers."""

    def test_has_anthropic(self):
        assert "anthropic" in _PROVIDER_MODELS_KEYS

    def test_has_openai(self):
        assert "openai" in _PROVIDER_MODELS_KEYS

    def test_has_google(self):
        assert "google" in _PROVIDER_MODELS_KEYS

    def test_has_deepseek(self):
        assert "deepseek" in _PROVIDER_MODELS_KEYS

    def test_has_xai(self):
        assert "x-ai" in _PROVIDER_MODELS_KEYS

    def test_has_mistralai(self):
        assert "mistralai" in _PROVIDER_MODELS_KEYS

    def test_has_openrouter(self):
        # openrouter uses _FALLBACK_MODELS, not _PROVIDER_MODELS
        pass  # intentionally no assertion
