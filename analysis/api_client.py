"""
Anthropic SDK wrapper for Meridian Capital Partners Layer 3.

Features:
  - Prompt caching (cache_control: ephemeral) on every system prompt
  - SDK-native retry on 429/5xx (exponential backoff, max 4 attempts)
  - JSON extraction from raw JSON, ```json fences, or prose-wrapped responses
  - Token count estimation for cost prediction before a call
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import anthropic
import yaml
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    _CFG = yaml.safe_load(f)

_ANALYSIS_CFG = _CFG["analysis"]
DEFAULT_MODEL = _ANALYSIS_CFG["model"]
DEFAULT_MAX_TOKENS = _ANALYSIS_CFG["max_tokens"]


class ClaudeClient:
    """
    Thread-safe Anthropic client with caching, retry, and JSON extraction.
    One instance per run — pass it to every analyzer.
    """

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS):
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. Add it to your .env file."
            )
        self.model = model
        self.max_tokens = max_tokens
        # SDK handles 429/5xx retry with exponential backoff automatically
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=4)
        logger.info(f"ClaudeClient initialized: model={model}, max_tokens={max_tokens}")

    # ------------------------------------------------------------------ #
    # Core call                                                           #
    # ------------------------------------------------------------------ #

    def call(
        self,
        system: str,
        user: str,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[str, anthropic.types.Usage]:
        """
        Single-turn call with cached system prompt.

        Returns:
            (response_text, usage) where usage has:
                .input_tokens
                .output_tokens
                .cache_creation_input_tokens
                .cache_read_input_tokens
        """
        resp = self._client.messages.create(
            model=model or self.model,
            max_tokens=max_tokens or self.max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user}],
        )

        text = ""
        for block in resp.content:
            if block.type == "text":
                text = block.text
                break

        usage = resp.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        logger.debug(
            f"[{self.model}] in={usage.input_tokens} out={usage.output_tokens} "
            f"cache_write={cache_write} cache_read={cache_read}"
        )
        return text, usage

    # ------------------------------------------------------------------ #
    # JSON extraction                                                     #
    # ------------------------------------------------------------------ #

    def call_json(
        self,
        system: str,
        user: str,
        *,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> tuple[Optional[dict], anthropic.types.Usage]:
        """
        Call Claude and extract JSON from the response.
        Handles: raw JSON, ```json fences, prose-wrapped JSON.
        Returns (parsed_dict_or_None, usage).
        """
        text, usage = self.call(system, user, model=model, max_tokens=max_tokens)
        parsed = extract_json(text)
        if parsed is None:
            logger.warning(f"JSON extraction failed. Raw response (first 500 chars): {text[:500]}")
        return parsed, usage

    # ------------------------------------------------------------------ #
    # Token count estimation (no charge for tokens counted)              #
    # ------------------------------------------------------------------ #

    def estimate_tokens(self, system: str, user: str) -> int:
        """
        Count tokens for a hypothetical call without actually calling Claude.
        Uses the token counting API — free, no generation charge.
        """
        try:
            result = self._client.messages.count_tokens(
                model=self.model,
                system=[{"type": "text", "text": system}],
                messages=[{"role": "user", "content": user}],
            )
            return result.input_tokens
        except Exception as e:
            logger.debug(f"Token estimation failed: {e}")
            # Rough fallback: ~4 chars per token
            return (len(system) + len(user)) // 4


# ------------------------------------------------------------------ #
# JSON extraction utility (module-level, usable standalone)           #
# ------------------------------------------------------------------ #

def extract_json(text: str) -> Optional[dict]:
    """
    Extract the first valid JSON object from a string.
    Tries three strategies in order:
      1. Entire response is valid JSON
      2. ```json ... ``` fenced block
      3. First '{' to last '}' in the response
    """
    if not text:
        return None

    # Strategy 1: whole text is JSON
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Strategy 2: ```json ... ``` fence
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: find outermost { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None


# Module-level singleton
_client: Optional[ClaudeClient] = None


def get_client() -> ClaudeClient:
    global _client
    if _client is None:
        _client = ClaudeClient()
    return _client
