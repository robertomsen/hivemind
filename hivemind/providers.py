"""Multi-provider abstraction layer for LLM backends."""

import asyncio
import base64
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".hivemind"
PROVIDERS_FILE = CONFIG_DIR / "providers.json"


@dataclass
class ProviderConfig:
    ollama_base_url: str = "http://localhost:11434"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"


def load_provider_config() -> ProviderConfig:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if PROVIDERS_FILE.exists():
        data = json.loads(PROVIDERS_FILE.read_text())
        return ProviderConfig(**{k: v for k, v in data.items() if k in ProviderConfig.__dataclass_fields__})
    config = ProviderConfig()
    save_provider_config(config)
    return config


def save_provider_config(config: ProviderConfig) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROVIDERS_FILE.write_text(json.dumps(asdict(config), indent=2))


class BaseProvider(ABC):
    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        system_prompt: str = "",
    ) -> AsyncGenerator[str, None]:
        ...
        yield  # pragma: no cover

    @abstractmethod
    async def check_connection(self) -> bool: ...

    @abstractmethod
    async def list_models(self) -> list[str]: ...


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url

    async def chat_stream(self, messages, model, system_prompt=""):
        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})

        # Convert multimodal messages to Ollama format
        for msg in messages:
            if isinstance(msg.get("content"), list):
                # Multimodal: extract text and images
                text_parts = []
                images = []
                for block in msg["content"]:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "image_url":
                        # Extract base64 data from data URI
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            b64 = url.split(",", 1)[1] if "," in url else url
                            images.append(b64)
                ollama_msg = {"role": msg["role"], "content": "\n".join(text_parts)}
                if images:
                    ollama_msg["images"] = images
                full_messages.append(ollama_msg)
            else:
                full_messages.append(msg)

        payload = {"model": model, "messages": full_messages, "stream": True}
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as c:
            async with c.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    tok = chunk.get("message", {}).get("content", "")
                    if tok:
                        yield tok
                    if chunk.get("done"):
                        break

    async def check_connection(self) -> bool:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(self.base_url, timeout=3)
                return r.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    async def list_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.base_url}/api/tags", timeout=5)
                return [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return []


class AnthropicProvider(BaseProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _convert_messages(self, messages):
        """Convert generic multimodal messages to Anthropic format."""
        converted = []
        for msg in messages:
            if isinstance(msg.get("content"), list):
                blocks = []
                for block in msg["content"]:
                    if block.get("type") == "text":
                        blocks.append({"type": "text", "text": block["text"]})
                    elif block.get("type") == "image_url":
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            # Parse data:image/png;base64,<data>
                            header, b64_data = url.split(",", 1) if "," in url else ("", url)
                            media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                            blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                },
                            })
                converted.append({"role": msg["role"], "content": blocks})
            else:
                converted.append(msg)
        return converted

    async def chat_stream(self, messages, model, system_prompt=""):
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic")

        client = AsyncAnthropic(api_key=self.api_key)
        converted = self._convert_messages(messages)
        async with client.messages.stream(
            model=model,
            max_tokens=4096,
            system=system_prompt or "You are a helpful assistant.",
            messages=converted,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def check_connection(self) -> bool:
        return bool(self.api_key)

    async def list_models(self) -> list[str]:
        return []


class OpenAIProvider(BaseProvider):
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key
        self.base_url = base_url

    async def chat_stream(self, messages, model, system_prompt=""):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise RuntimeError("openai SDK not installed. Run: pip install openai")

        full_messages = []
        if system_prompt:
            full_messages.append({"role": "system", "content": system_prompt})
        full_messages.extend(messages)

        client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        stream = await client.chat.completions.create(
            model=model,
            messages=full_messages,
            stream=True,
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    async def check_connection(self) -> bool:
        return bool(self.api_key)

    async def list_models(self) -> list[str]:
        return []


# ═══ Retry wrapper ═══════════════════════════════════════════════════════════

# Errors worth retrying — transient network / server issues
_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    ConnectionError,
    OSError,
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is worth retrying."""
    if isinstance(exc, _RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _RETRYABLE_STATUS_CODES:
        return True
    # Anthropic / OpenAI SDK errors that wrap HTTP status codes
    exc_name = type(exc).__name__
    if exc_name in ("RateLimitError", "InternalServerError", "APIConnectionError",
                     "APITimeoutError", "ServiceUnavailableError"):
        return True
    return False


class RetryProvider(BaseProvider):
    """Wraps any BaseProvider with automatic retry + exponential backoff.

    For chat_stream: retries only if the error occurs before any tokens
    are yielded (i.e. connection/request phase). Mid-stream errors are
    propagated immediately — partial output cannot be cleanly retried.
    """

    def __init__(self, inner: BaseProvider, max_retries: int = MAX_RETRIES,
                 base_delay: float = BASE_DELAY):
        self._inner = inner
        self._max_retries = max_retries
        self._base_delay = base_delay

    async def chat_stream(self, messages, model, system_prompt=""):
        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                yielded = False
                async for token in self._inner.chat_stream(messages, model, system_prompt):
                    yielded = True
                    yield token
                return  # Success
            except Exception as e:
                last_exc = e
                if yielded or not _is_retryable(e):
                    raise  # Mid-stream or non-retryable: propagate
                if attempt < self._max_retries:
                    delay = self._base_delay * (2 ** attempt)
                    log.warning("Provider error (attempt %d/%d): %s — retrying in %.1fs",
                                attempt + 1, self._max_retries + 1, e, delay)
                    await asyncio.sleep(delay)
        raise last_exc  # All retries exhausted

    async def check_connection(self) -> bool:
        for attempt in range(self._max_retries + 1):
            try:
                return await self._inner.check_connection()
            except Exception as e:
                if not _is_retryable(e) or attempt == self._max_retries:
                    return False
                await asyncio.sleep(self._base_delay * (2 ** attempt))
        return False

    async def list_models(self) -> list[str]:
        for attempt in range(self._max_retries + 1):
            try:
                return await self._inner.list_models()
            except Exception as e:
                if not _is_retryable(e) or attempt == self._max_retries:
                    return []
                await asyncio.sleep(self._base_delay * (2 ** attempt))
        return []


def get_provider(provider_name: str, config: ProviderConfig | None = None) -> BaseProvider:
    if config is None:
        config = load_provider_config()
    match provider_name:
        case "ollama":
            inner = OllamaProvider(base_url=config.ollama_base_url)
        case "anthropic":
            if not config.anthropic_api_key:
                raise ValueError("Anthropic API key not configured. Edit ~/.hivemind/providers.json")
            inner = AnthropicProvider(api_key=config.anthropic_api_key)
        case "openai":
            if not config.openai_api_key:
                raise ValueError("OpenAI API key not configured. Edit ~/.hivemind/providers.json")
            inner = OpenAIProvider(api_key=config.openai_api_key, base_url=config.openai_base_url)
        case _:
            raise ValueError(f"Unknown provider: {provider_name}")
    return RetryProvider(inner)
