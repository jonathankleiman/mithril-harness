"""Self-contained Anthropic adapter (vendored into mithril).

The stock LAB Anthropic adapter only enables extended thinking for the 4.6
models and doesn't do prompt caching, transient-error retry, or cache-aware
cost accounting. Rather than patch Harvey's (separately-licensed) code, this
adapter lives in mithril and depends only on the standard `ModelAdapter` base.

Adds, over the stock adapter:
  - Adaptive "max" thinking for Opus 4.8 / Sonnet 4.7 (and 4.6/4.7).
  - Prompt caching (system + a rolling breakpoint) — the big cost lever; keeps a
    stable, append-only prefix so the growing history is re-read cheaply.
  - Retry on dropped streams / overload (a transient blip must not kill a long run).
  - Cache-aware cost accounting (reads/writes/output) via usage_dict().

Note: thinking-block *stripping* is intentionally NOT done — it mutates the
cached prefix and forces expensive cache re-writes (measured +80% cost). Caching
(stable prefix) is the correct optimization.
"""

from __future__ import annotations

import json
import time

import anthropic
import httpx

from harness.adapters.base import ModelAdapter, ModelResponse, ToolCall

ADAPTIVE_MODELS = {"claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                   "claude-sonnet-4-7", "claude-sonnet-4-6"}

MAX_OUTPUT = {"claude-opus-4-8": 128000, "claude-opus-4-7": 128000, "claude-opus-4-6": 128000,
              "claude-sonnet-4-7": 64000, "claude-sonnet-4-6": 64000, "claude-haiku-4-5": 64000}

# USD per 1M tokens (standard tier).
RATES = {
    "claude-opus-4-8": {"in": 5.0, "cache_read": 0.50, "cache_write": 6.25, "out": 25.0},
    "claude-opus-4-7": {"in": 5.0, "cache_read": 0.50, "cache_write": 6.25, "out": 25.0},
    "claude-sonnet-4-7": {"in": 3.0, "cache_read": 0.30, "cache_write": 3.75, "out": 15.0},
    "claude-sonnet-4-6": {"in": 3.0, "cache_read": 0.30, "cache_write": 3.75, "out": 15.0},
}


class MithrilAnthropicAdapter(ModelAdapter):
    def __init__(self, model: str, temperature: float = 0.0, max_tokens: int | None = None,
                 reasoning_effort: str | None = None):
        super().__init__(model, temperature, reasoning_effort)
        self.max_tokens = max_tokens or next((v for k, v in MAX_OUTPUT.items() if model.startswith(k)), 16384)
        self.client = anthropic.Anthropic(max_retries=0)
        self._system_prompt: str | None = None
        self.in_tok = self.out_tok = self.cache_read = self.cache_write = 0

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        api_messages = []
        for msg in messages:
            if msg["role"] == "system":
                self._system_prompt = msg["content"]
            else:
                api_messages.append(msg)
        anthropic_tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]

        # Prompt caching: cache the system prompt + a breakpoint at the end of the
        # running history (copy the last message so cache_control doesn't accumulate).
        system_param = ([{"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}}]
                        if self._system_prompt else "")
        if api_messages:
            last = api_messages[-1]
            content = last["content"]
            if isinstance(content, str):
                last = {**last, "content": [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}
            elif isinstance(content, list) and content and isinstance(content[-1], dict):
                last = {**last, "content": content[:-1] + [{**content[-1], "cache_control": {"type": "ephemeral"}}]}
            api_messages = api_messages[:-1] + [last]

        kwargs = dict(model=self.model, max_tokens=self.max_tokens, temperature=self.temperature,
                      system=system_param, messages=api_messages, tools=anthropic_tools)
        if self.reasoning_effort and self.model in ADAPTIVE_MODELS:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["extra_body"] = {"output_config": {"effort": self.reasoning_effort}}
            kwargs["temperature"] = 1

        response = self._stream_with_retry(kwargs)

        tool_calls, text_parts = [], []
        for b in response.content:
            if b.type == "tool_use":
                tool_calls.append(ToolCall(id=b.id, name=b.name, arguments=json.dumps(b.input)))
            elif b.type == "text":
                text_parts.append(b.text)

        message = {"role": "assistant", "content": [self._block_to_dict(b) for b in response.content]}
        u = response.usage
        self.in_tok += u.input_tokens
        self.out_tok += u.output_tokens
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        return ModelResponse(message=message, tool_calls=tool_calls, text="\n".join(text_parts),
                             input_tokens=u.input_tokens, output_tokens=u.output_tokens)

    def _stream_with_retry(self, kwargs: dict):
        delay, last = 4.0, None
        for attempt in range(5):
            try:
                with self.client.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.InternalServerError,
                    httpx.RemoteProtocolError, httpx.ReadError) as e:
                last = e
            except anthropic.APIStatusError as e:
                if getattr(e, "status_code", None) not in (429, 529, 500, 503):
                    raise
                last = e
            if attempt == 4:
                raise last
            time.sleep(delay); delay = min(delay * 2, 60)

    def make_tool_result_messages(self, results: list[tuple[str, str]]) -> list[dict]:
        return [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": tcid, "content": r}
                                             for tcid, r in results]}]

    def make_system_message(self, content: str) -> dict:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> dict:
        return {"role": "user", "content": content}

    def _block_to_dict(self, b) -> dict:
        if b.type == "text":
            return {"type": "text", "text": b.text}
        if b.type == "tool_use":
            return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        if b.type == "thinking":
            d = {"type": "thinking", "thinking": b.thinking}
            if getattr(b, "signature", None):
                d["signature"] = b.signature
            return d
        if b.type == "redacted_thinking":
            return {"type": "redacted_thinking", "data": b.data}
        return b.model_dump() if hasattr(b, "model_dump") else {"type": b.type}

    def cost_usd(self) -> float:
        rate = next((v for k, v in RATES.items() if self.model.startswith(k)), None)
        if not rate:
            return 0.0
        return (self.in_tok / 1e6 * rate["in"] + self.cache_read / 1e6 * rate["cache_read"]
                + self.cache_write / 1e6 * rate["cache_write"] + self.out_tok / 1e6 * rate["out"])

    def usage_dict(self) -> dict:
        return {"input_tokens": self.in_tok, "output_tokens": self.out_tok,
                "cache_read_tokens": self.cache_read, "cache_write_tokens": self.cache_write,
                "est_cost_usd": round(self.cost_usd(), 4)}
