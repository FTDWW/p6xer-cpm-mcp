"""
LLM client for the AI-powered schedule analysis tool.

Talks to any OpenAI-compatible chat-completions endpoint.  The default is the
public DeepSeek API; point it at another provider with environment variables:

    P6XER_LLM_BASE_URL   default https://api.deepseek.com/v1
    P6XER_LLM_MODEL      default deepseek-chat
    P6XER_LLM_API_KEY    explicit key (highest priority; falls back to
                         DEEPSEEK_API_KEY)
    P6XER_LLM_TIMEOUT    request timeout in seconds (default 60)
    P6XER_LLM_MAX_TOKENS upper bound for the adaptive token budget (default 8192)
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


def _resolve_api_key() -> str | None:
    for name in ("P6XER_LLM_API_KEY", "DEEPSEEK_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


class DeepSeekClient:
    """OpenAI-SDK wrapper around the configured OpenAI-compatible endpoint."""

    _MAX_TOKENS_CAP = int(os.environ.get("P6XER_LLM_MAX_TOKENS") or 8192)

    def __init__(self) -> None:
        api_key = _resolve_api_key()
        if not api_key:
            raise RuntimeError(
                "No LLM API key found. Set P6XER_LLM_API_KEY or DEEPSEEK_API_KEY."
            )
        self.base_url = os.environ.get("P6XER_LLM_BASE_URL") or DEFAULT_BASE_URL
        # Set an explicit timeout: the SDK default is 600s, which can block a
        # single-threaded MCP event loop for minutes on a stalled connection.
        timeout = float(os.environ.get("P6XER_LLM_TIMEOUT") or 60.0)
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=1,
        )
        self.model = os.environ.get("P6XER_LLM_MODEL") or DEFAULT_MODEL

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 2048,
    ) -> str:
        """Send messages and return the response text.

        Reasoning tokens count against ``max_tokens`` on reasoning-capable
        endpoints; when the budget is exhausted during reasoning the content
        comes back empty.  Rather than hand-tuning budgets per call site, we
        double the budget and retry up to ``_MAX_TOKENS_CAP``.
        """
        budget = max_tokens
        while True:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=budget,
            )
            choice = response.choices[0]
            text = choice.message.content or ""
            if text or choice.finish_reason != "length":
                return text
            details = getattr(response.usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", None)
            if budget >= self._MAX_TOKENS_CAP:
                raise RuntimeError(
                    f"LLM consumed max_tokens={budget} during reasoning "
                    f"(reasoning_tokens={reasoning_tokens}) without producing "
                    f"content. Increase max_tokens."
                )
            budget = min(budget * 2, self._MAX_TOKENS_CAP)

    def chat_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        """Send messages and parse the response as JSON."""
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = lines[1:] if lines[0].startswith("```") else lines
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        if not text:
            raise ValueError(
                "LLM returned an empty response. Check your API key and network "
                "connectivity."
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            preview = text[:500] + "..." if len(text) > 500 else text
            raise ValueError(f"LLM returned invalid JSON. Raw response: {preview}")