"""DeepSeek model adapter — OpenAI-compatible *Chat Completions* API.

The benchmark's stock OpenAI adapter targets the *Responses* API, which
DeepSeek does not serve. DeepSeek speaks standard Chat Completions with
function calling, so this adapter is stateless over the message list: the
agent loop owns ``messages`` (which lets us rewrite history for compaction)
and this adapter just renders one request per turn.

It implements the same ``ModelAdapter`` surface the harness expects so it
plugs into either the stock loop or our improved loop.
"""

from __future__ import annotations

import json
import time

import httpx
import openai

from harness.adapters.base import ModelAdapter, ModelResponse, ToolCall
from mithril import config

# With streaming, `read` is the max gap *between* chunks (not total generation
# time), so a long-but-progressing thinking turn never trips it; a genuinely
# wedged socket still fails fast and retries.
_TIMEOUT = httpx.Timeout(120.0, connect=15.0, read=120.0, write=60.0)


class DeepSeekAdapter(ModelAdapter):
    """Adapter for DeepSeek chat models (deepseek-v4-pro / deepseek-v4-flash)."""

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        reasoning_effort: str | None = None,
        max_retries: int = 5,
    ):
        super().__init__(model, temperature, reasoning_effort)
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.client = openai.OpenAI(
            api_key=config.deepseek_api_key(),
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=_TIMEOUT,
            max_retries=0,  # we do our own backoff so we can log it
        )
        # Cumulative usage, for cost accounting.
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # ── core call (streamed) ──────────────────────────────────────────
    def chat(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        ds_tools = [self._translate_tool(t) for t in tools] if tools else None

        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        if ds_tools:
            kwargs["tools"] = ds_tools
            kwargs["tool_choice"] = "auto"

        agg = self._call_with_retry(kwargs)
        content, reasoning, tcs, usage = agg["content"], agg["reasoning"], agg["tool_calls"], agg["usage"]

        tool_calls = [ToolCall(id=t["id"], name=t["name"], arguments=t["args"]) for t in tcs if t.get("name")]

        # Rebuild the assistant message to append to history. DeepSeek's
        # thinking-mode models REQUIRE reasoning_content be passed back on
        # subsequent turns, so we preserve it verbatim when present.
        assistant_msg: dict = {"role": "assistant", "content": content or ""}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": t["id"], "type": "function",
                 "function": {"name": t["name"], "arguments": t["args"]}}
                for t in tcs if t.get("name")
            ]

        in_tok = usage.prompt_tokens if usage else 0
        out_tok = usage.completion_tokens if usage else 0
        self.prompt_tokens += in_tok
        self.completion_tokens += out_tok
        if usage is not None:
            self.cache_hit_tokens += getattr(usage, "prompt_cache_hit_tokens", 0) or 0
            self.cache_miss_tokens += getattr(usage, "prompt_cache_miss_tokens", 0) or 0

        return ModelResponse(
            message=assistant_msg,
            tool_calls=tool_calls,
            text=content or "",
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    def _call_with_retry(self, kwargs: dict) -> dict:
        """Create a streamed completion and aggregate chunks. Retried as a whole
        so a mid-stream disconnect restarts cleanly. Streaming makes the read
        timeout a per-chunk gap, not total generation time — long thinking-mode
        turns stream tokens continuously and never trip it."""
        delay = 4.0
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                stream = self.client.chat.completions.create(**kwargs)
                content: list[str] = []
                reasoning: list[str] = []
                tool_acc: dict[int, dict] = {}
                usage = None
                for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if getattr(delta, "content", None):
                        content.append(delta.content)
                    rc = getattr(delta, "reasoning_content", None)
                    if rc is None and getattr(delta, "model_extra", None):
                        rc = delta.model_extra.get("reasoning_content")
                    if rc:
                        reasoning.append(rc)
                    for tcd in (delta.tool_calls or []):
                        acc = tool_acc.setdefault(tcd.index, {"id": None, "name": None, "args": ""})
                        if tcd.id:
                            acc["id"] = tcd.id
                        if getattr(tcd, "function", None):
                            if tcd.function.name:
                                acc["name"] = tcd.function.name
                            if tcd.function.arguments:
                                acc["args"] += tcd.function.arguments
                return {
                    "content": "".join(content),
                    "reasoning": "".join(reasoning),
                    "tool_calls": [tool_acc[i] for i in sorted(tool_acc)],
                    "usage": usage,
                }
            except (openai.RateLimitError, openai.APITimeoutError,
                    openai.InternalServerError, openai.APIConnectionError) as e:
                last_err = e
                if attempt == self.max_retries - 1:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError(f"DeepSeek call failed after {self.max_retries} attempts: {last_err}")

    # ── message construction (chat-completions native) ────────────────
    def make_tool_result_messages(self, results: list[tuple[str, str]]) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": tcid, "content": result}
            for tcid, result in results
        ]

    def make_system_message(self, content: str) -> dict:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> dict:
        return {"role": "user", "content": content}

    def _translate_tool(self, tool: dict) -> dict:
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }

    # ── cost accounting ───────────────────────────────────────────────
    def cost_usd(self) -> float:
        rate = config.PRICE_PER_M.get(self.model)
        if not rate:
            return 0.0
        # If the provider gave a hit/miss split, prefer it; else treat all
        # prompt tokens as cache-miss (conservative — overestimates cost).
        miss = self.cache_miss_tokens or self.prompt_tokens
        hit = self.cache_hit_tokens
        return (
            miss / 1e6 * rate["in_miss"]
            + hit / 1e6 * rate["in_hit"]
            + self.completion_tokens / 1e6 * rate["out"]
        )

    def usage_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "est_cost_usd": round(self.cost_usd(), 4),
        }
