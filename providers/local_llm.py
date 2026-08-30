"""Local LLM via ollama (http://localhost:11434) or any OpenAI-compat endpoint."""

import aiohttp
from aiohttp import web

from .openai_compat import OpenAICompatProvider


class LocalLLMProvider(OpenAICompatProvider):
    """Thin wrapper — ollama exposes /v1/chat/completions since 0.1.24."""

    def __init__(self, config: dict):
        config.setdefault("url", "http://localhost:11434")
        config.setdefault("api_key", "ollama")
        config.setdefault("default_model", "llama3")
        super().__init__(config)

    def list_models(self) -> list[dict]:
        # Attempt to fetch live model list from ollama
        return [{"id": self._default_model, "object": "model", "owned_by": "local"}]

    def status(self) -> dict:
        return {"url": self._base_url, "default_model": self._default_model}
