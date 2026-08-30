import aiohttp
from aiohttp import web


class OpenAICompatProvider:
    """Generic OpenAI-compatible provider (OpenAI, Together, Groq, LM Studio, etc.)"""

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._base_url = config.get("url", "https://api.openai.com").rstrip("/")
        self._default_model = config.get("default_model", "gpt-4o-mini")

    async def chat(self, req: web.Request, body: dict) -> web.StreamResponse:
        body.setdefault("model", self._default_model)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/v1/chat/completions",
                json=body, headers=headers,
            ) as resp:
                if body.get("stream"):
                    response = web.StreamResponse()
                    await response.prepare(req)
                    async for chunk in resp.content:
                        await response.write(chunk)
                    await response.write_eof()
                    return response
                return web.json_response(await resp.json())

    def list_models(self) -> list[dict]:
        return [{"id": self._default_model, "object": "model", "owned_by": self._base_url}]

    def status(self) -> dict:
        return {"configured": bool(self._api_key), "default_model": self._default_model, "url": self._base_url}
