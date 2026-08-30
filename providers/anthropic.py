import aiohttp
from aiohttp import web


class AnthropicProvider:
    BASE_URL = "https://api.anthropic.com"
    API_VERSION = "2023-06-01"

    def __init__(self, config: dict):
        self._api_key = config.get("api_key", "")
        self._default_model = config.get("default_model", "claude-sonnet-4-6")

    async def chat(self, req: web.Request, body: dict) -> web.StreamResponse:
        model = body.get("model") or self._default_model
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": body.get("max_tokens", 8192),
            "stream": stream,
        }
        if "system" in body:
            payload["system"] = body["system"]

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.API_VERSION,
            "content-type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.BASE_URL}/v1/messages",
                json=payload, headers=headers,
            ) as resp:
                if stream:
                    response = web.StreamResponse()
                    await response.prepare(req)
                    async for chunk in resp.content:
                        await response.write(chunk)
                    await response.write_eof()
                    return response
                else:
                    data = await resp.json()
                    if resp.status != 200 or data.get("type") == "error":
                        err = data.get("error", {})
                        raise web.HTTPBadGateway(
                            reason=err.get("message", "Anthropic API error"),
                            text=err.get("message", str(data)),
                            content_type="text/plain",
                        )
                    return web.json_response(self._to_openai_format(data))

    def _to_openai_format(self, anthropic_resp: dict) -> dict:
        content = anthropic_resp.get("content", [{}])
        text = content[0].get("text", "") if content else ""
        return {
            "id": anthropic_resp.get("id", ""),
            "object": "chat.completion",
            "model": anthropic_resp.get("model", ""),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": anthropic_resp.get("stop_reason", "stop"),
            }],
            "usage": anthropic_resp.get("usage", {}),
        }

    def list_models(self) -> list[dict]:
        return [
            {"id": m, "object": "model", "owned_by": "anthropic"}
            for m in ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
        ]

    def status(self) -> dict:
        return {"configured": bool(self._api_key), "default_model": self._default_model}
