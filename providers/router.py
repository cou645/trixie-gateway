"""
Provider router — selects the right AI backend by model name prefix.

Config (in gateway.json → "providers"):
  {
    "anthropic": { "api_key": "sk-ant-...", "default_model": "claude-sonnet-4-6" },
    "openai":    { "api_key": "sk-...",     "default_model": "gpt-4o-mini" },
    "local":     { "url": "http://localhost:11434", "default_model": "llama3" }
  }
"""

from .anthropic import AnthropicProvider
from .openai_compat import OpenAICompatProvider
from .local_llm import LocalLLMProvider


MODEL_PREFIX_MAP = {
    "claude":      "anthropic",
    "gpt":         "openai",
    "o1":          "openai",
    "o3":          "openai",
    "deepseek":    "deepseek",
    # NVIDIA NIM hosted models (org/name format)
    "nvidia/":     "nvidia",
    "meta/":       "nvidia",
    "mistral/":    "nvidia",
    "microsoft/":  "nvidia",
    "google/":     "nvidia",
    "moonshotai/": "nvidia",
    "nv-":         "nvidia",
    # local
    "llama":       "local",
    "phi":         "local",
    "gemma":       "local",
}


class ProviderRouter:
    def __init__(self, config: dict):
        self._providers = {}
        if "anthropic" in config:
            self._providers["anthropic"] = AnthropicProvider(config["anthropic"])
        if "openai" in config:
            self._providers["openai"] = OpenAICompatProvider(config["openai"])
        if "deepseek" in config:
            self._providers["deepseek"] = OpenAICompatProvider(config["deepseek"])
        if "nvidia" in config:
            self._providers["nvidia"] = OpenAICompatProvider(config["nvidia"])
        if "local" in config:
            self._providers["local"] = LocalLLMProvider(config["local"])
        self._config = config

    def select(self, model: str):
        for prefix, backend in MODEL_PREFIX_MAP.items():
            if model.startswith(prefix) and backend in self._providers:
                return self._providers[backend]
        # fallback: first available
        if self._providers:
            return next(iter(self._providers.values()))
        raise RuntimeError("no AI providers configured")

    def list_models(self) -> list[dict]:
        models = []
        for name, provider in self._providers.items():
            models.extend(provider.list_models())
        return models

    def status(self) -> dict:
        return {name: p.status() for name, p in self._providers.items()}
