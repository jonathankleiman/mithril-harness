"""DeepSeek LLM judge — API-compatible with evaluation.scoring.score_rubric.

We reuse LAB's grading pipeline verbatim (same rubric_criterion prompt, same
per-criterion scoping, same all-pass logic). Only the judge *model* changes:
the official benchmark judges with claude-sonnet-4-6 / GPT-5.4; we judge with
DeepSeek because that is the API key available. This is a faithful
re-implementation of the ``Judge`` interface, not a modification of scoring.

Methodological caveat (documented in the report): agent and judge are the
same model family, so verdicts carry some self-preference bias. We mitigate
with temperature 0 and the unchanged strict rubric prompt, and recommend a
frontier judge for any official leaderboard number.
"""

from __future__ import annotations

import time
import httpx
import openai

from evaluation.judge import Judge as _LabJudge  # for _parse_json + PROMPTS_DIR
from evaluation.judge import PROMPTS_DIR, _VERDICT_SCHEMA
from mithril import config

_JUDGE_TIMEOUT = httpx.Timeout(180.0, connect=15.0, read=180.0, write=30.0)

# GPT-5.4 pricing (USD per 1M tokens): $2.50 input, $0.25 cached input, $15 output.
_GPT54_PRICE = {"in": 2.50, "cached": 0.25, "out": 15.0}


class GPT54Judge:
    """GPT-5.4 judge via the OpenAI Responses API, with usage/cost tracking.

    Same evaluate_from_file/model/usage_dict surface as DeepSeekJudge so it
    drops into score_rubric unchanged. This is the judge used for the official
    leaderboard comparison (GPT-5.4 grades the LAB rubric)."""

    def __init__(self, model: str = "gpt-5.4", max_retries: int = 4):
        self.model = model
        self.max_retries = max_retries
        self.client = openai.OpenAI(timeout=_JUDGE_TIMEOUT, max_retries=0)  # reads OPENAI_API_KEY
        self.calls = 0
        self.in_tok = 0
        self.out_tok = 0
        self.cached_in = 0

    def evaluate_from_file(self, prompt_name: str, variables: dict) -> dict:
        prompt = (PROMPTS_DIR / f"{prompt_name}.txt").read_text().format(**variables)
        delay = 4.0
        last = None
        for attempt in range(self.max_retries):
            kwargs = dict(model=self.model, input=prompt, max_output_tokens=8192)
            if attempt < self.max_retries - 1:
                kwargs["text"] = {"format": {"type": "json_schema", "name": "verdict",
                                             "schema": _VERDICT_SCHEMA, "strict": True}}
            try:
                resp = self.client.responses.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(delay); delay = min(delay * 2, 60)
                continue
            self.calls += 1
            u = getattr(resp, "usage", None)
            if u:
                self.in_tok += getattr(u, "input_tokens", 0) or 0
                self.out_tok += getattr(u, "output_tokens", 0) or 0
                det = getattr(u, "input_tokens_details", None)
                self.cached_in += (getattr(det, "cached_tokens", 0) or 0) if det else 0
            try:
                return _LabJudge._parse_json(resp.output_text or "")
            except Exception as e:  # noqa: BLE001
                last = e
        return {"verdict": "fail", "reasoning": f"JUDGE_ERROR: GPT-5.4 judge failed: {last}"}

    def cost_usd(self) -> float:
        full = max(self.in_tok - self.cached_in, 0)
        return full / 1e6 * _GPT54_PRICE["in"] + self.cached_in / 1e6 * _GPT54_PRICE["cached"] + self.out_tok / 1e6 * _GPT54_PRICE["out"]

    def usage_dict(self) -> dict:
        return {"judge_calls": self.calls, "input_tokens": self.in_tok,
                "cached_input_tokens": self.cached_in, "output_tokens": self.out_tok,
                "est_cost_usd": round(self.cost_usd(), 4)}


def make_judge(model: str | None):
    """Route to the right judge by model name: gpt*/o* → GPT-5.4 (OpenAI), else DeepSeek."""
    model = model or config.JUDGE_MODEL
    if model.startswith(("gpt", "o1", "o3", "o4", "o5")):
        return GPT54Judge(model=model)
    return DeepSeekJudge(model=model)


class DeepSeekJudge:
    def __init__(self, model: str | None = None, max_retries: int = 5):
        self.model = model or config.JUDGE_MODEL
        self.max_retries = max_retries
        self.client = openai.OpenAI(
            api_key=config.deepseek_api_key(),
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=_JUDGE_TIMEOUT,
            max_retries=0,
        )
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0

    def evaluate_from_file(self, prompt_name: str, variables: dict) -> dict:
        template = (PROMPTS_DIR / f"{prompt_name}.txt").read_text()
        prompt = template.format(**variables)
        return self._evaluate(prompt)

    def _evaluate(self, prompt: str) -> dict:
        delay = 4.0
        last_err = None
        for attempt in range(self.max_retries):
            kwargs = dict(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=4096,
                stream=True,
                stream_options={"include_usage": True},
            )
            # JSON mode on early attempts; drop it on the last (brace-match fallback).
            if attempt < self.max_retries - 1:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                stream = self.client.chat.completions.create(**kwargs)
                parts: list[str] = []
                usage = None
                for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if chunk.choices and getattr(chunk.choices[0].delta, "content", None):
                        parts.append(chunk.choices[0].delta.content)
                text = "".join(parts)
            except (openai.RateLimitError, openai.APITimeoutError,
                    openai.InternalServerError, openai.APIConnectionError) as e:
                last_err = e
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            self.calls += 1
            if usage:
                self.prompt_tokens += usage.prompt_tokens
                self.completion_tokens += usage.completion_tokens
                self.cache_hit_tokens += getattr(usage, "prompt_cache_hit_tokens", 0) or 0
                self.cache_miss_tokens += getattr(usage, "prompt_cache_miss_tokens", 0) or 0
            try:
                return _LabJudge._parse_json(text)
            except Exception as e:  # noqa: BLE001
                last_err = e
        # Judge infra failure → conservative fail, but TAGGED so it can be told
        # apart from a genuine criterion failure (and excluded/retried in aggregation).
        return {"verdict": "fail", "reasoning": f"JUDGE_ERROR: unparseable/failed: {last_err}"}

    def cost_usd(self) -> float:
        rate = config.PRICE_PER_M.get(self.model)
        if not rate:
            return 0.0
        miss = self.cache_miss_tokens or self.prompt_tokens
        return (miss / 1e6 * rate["in_miss"] + self.cache_hit_tokens / 1e6 * rate["in_hit"]
                + self.completion_tokens / 1e6 * rate["out"])

    def usage_dict(self) -> dict:
        return {
            "judge_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "est_cost_usd": round(self.cost_usd(), 4),
        }
