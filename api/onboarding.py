# api/onboarding.py

from api.config import _FALLBACK_MODELS

# Ensure OpenRouter models use the correct 'z-ai/' namespace instead of 'zai/'
openrouter_models = [
    model.replace("zai/", "z-ai/") if model.startswith("zai/") else model
    for model in _FALLBACK_MODELS
]

_SUPPORTED_PROVIDER_SETUPS = {
    "openrouter": {
        "models": openrouter_models,
        # ... other settings
    },
    # ... other providers
}