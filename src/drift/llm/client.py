"""LLM access through one interface (LL-1..LL-5).

Providers are chosen by ``type`` in configuration: ``openai_compat`` (LM Studio, Ollama, OpenRouter, vLLM, ...)
or ``anthropic``. Call sites only see ``LLMClient.complete``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from drift.config.models import ProviderConfig, StepModelConfig

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class LLMError(Exception):
    """The call failed (bad status, unusable response). The chain step fails; the run falls back."""


class LLMUnavailable(LLMError):
    """The provider could not be reached at all (CH-9): skip the chain, use the fallback."""


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True)
class LLMResponse:
    text: str  # content with any reasoning stripped (LL-4)
    raw_text: str  # content exactly as returned
    model: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    latency_ms: int
    cost_usd: float


# ------------------------------------------------------------------------------ LL-4

_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning)>.*\Z", re.DOTALL | re.IGNORECASE)
_THINK_CLOSE_ONLY = re.compile(r"\A.*?</(think|thinking|reasoning)>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove reasoning a model emits inline. Handles closed blocks, a dangling open block (truncated output)
    and a leading block whose opening tag was consumed by the chat template."""
    text = _THINK_BLOCK.sub("", text)
    text = _THINK_CLOSE_ONLY.sub("", text)
    text = _THINK_OPEN.sub("", text)
    return text.strip()


# ------------------------------------------------------------------------- providers


class Provider(Protocol):
    async def call(
        self, http: httpx.AsyncClient, messages: Sequence[Message], step: StepModelConfig, schema: dict[str, Any] | None
    ) -> tuple[str, dict[str, int]]: ...


@dataclass(frozen=True)
class OpenAICompat:
    cfg: ProviderConfig
    api_key: str | None

    async def call(
        self, http: httpx.AsyncClient, messages: Sequence[Message], step: StepModelConfig, schema: dict[str, Any] | None
    ) -> tuple[str, dict[str, int]]:
        body: dict[str, Any] = {
            "model": step.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": step.temperature,
            "max_tokens": step.max_tokens,
            "stream": False,
            **self.cfg.extra_body,
        }
        if schema is not None and self.cfg.json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        elif schema is not None and self.cfg.json_mode == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "schema": schema}}
        headers = {"Content-Type": "application/json", **self.cfg.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = await http.post(f"{self.cfg.base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
        _raise_for(resp)
        data = resp.json()
        try:
            content = data["choices"][0]["message"].get("content") or ""  # reasoning fields are ignored (LL-4)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError("response has no choices[0].message") from exc
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return content, {
            "prompt": int(usage.get("prompt_tokens") or 0),
            "completion": int(usage.get("completion_tokens") or 0),
            "reasoning": int(details.get("reasoning_tokens") or 0),
        }


@dataclass(frozen=True)
class Anthropic:
    cfg: ProviderConfig
    api_key: str | None

    async def call(
        self, http: httpx.AsyncClient, messages: Sequence[Message], step: StepModelConfig, schema: dict[str, Any] | None
    ) -> tuple[str, dict[str, int]]:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        body: dict[str, Any] = {
            "model": step.model,
            "max_tokens": step.max_tokens,
            "temperature": step.temperature,
            "messages": [{"role": m.role, "content": m.content} for m in messages if m.role != "system"],
        }
        if system:
            body["system"] = system
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", **self.cfg.extra_headers}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        resp = await http.post(f"{self.cfg.base_url.rstrip('/')}/v1/messages", json=body, headers=headers)
        _raise_for(resp)
        data = resp.json()
        try:
            content = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMError("response has no content blocks") from exc
        usage = data.get("usage") or {}
        return content, {
            "prompt": int(usage.get("input_tokens") or 0),
            "completion": int(usage.get("output_tokens") or 0),
            "reasoning": 0,
        }


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code >= 500 or resp.status_code == 429:
        raise _Retryable(f"HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMError(f"HTTP {resp.status_code}")


class _Retryable(Exception):
    pass


# --------------------------------------------------------------------------- client


class LLMClient:
    """One step's view of the LLM: provider + model settings + transport-level retries."""

    def __init__(
        self,
        providers: Mapping[str, ProviderConfig],
        step: StepModelConfig,
        environ: Mapping[str, str],
        transport_factory: TransportFactory | None = None,
    ) -> None:
        pcfg = providers[step.provider]
        key = environ.get(pcfg.api_key_env) if pcfg.api_key_env else None
        self.step = step
        self.provider_name = step.provider
        self._provider: Provider = Anthropic(pcfg, key) if pcfg.type == "anthropic" else OpenAICompat(pcfg, key)
        self._transport_factory = transport_factory

    async def complete(self, messages: Sequence[Message], schema: dict[str, Any] | None = None) -> LLMResponse:
        step = self.step
        transport = self._transport_factory() if self._transport_factory else None
        last: Exception | None = None
        unreachable = True
        async with httpx.AsyncClient(timeout=httpx.Timeout(step.timeout_s), transport=transport) as http:
            for attempt in range(step.retries + 1):
                t0 = time.perf_counter()
                try:
                    raw, usage = await self._provider.call(http, messages, step, schema)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    last = exc
                except (httpx.TimeoutException, httpx.RemoteProtocolError, _Retryable) as exc:
                    last, unreachable = exc, False
                except (httpx.HTTPError, ValueError) as exc:  # ValueError: body was not JSON
                    raise LLMError(type(exc).__name__) from exc
                else:
                    latency = int((time.perf_counter() - t0) * 1000)
                    cost = (
                        usage["prompt"] * step.price_in_per_mtok + usage["completion"] * step.price_out_per_mtok
                    ) / 1e6
                    return LLMResponse(
                        strip_thinking(raw),
                        raw,
                        step.model,
                        usage["prompt"],
                        usage["completion"],
                        usage["reasoning"],
                        latency,
                        round(cost, 6),
                    )
                if attempt < step.retries:
                    await asyncio.sleep(min(2.0, 0.5 * (attempt + 1)))
        kind = type(last).__name__ if last else "unknown"
        if unreachable:
            raise LLMUnavailable(kind)
        raise LLMError(kind)
