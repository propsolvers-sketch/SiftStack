"""Thin LLM abstraction layer — routes calls to Anthropic or Ollama.

Supports two backends:
  - anthropic: Claude Haiku via Anthropic API (production, paid)
  - ollama: Local model via Ollama OpenAI-compatible API (development, free)

Backend selection: LLM_BACKEND env var or config.LLM_BACKEND.
"""

import json
import logging
import re
from typing import TYPE_CHECKING

import config as cfg

if TYPE_CHECKING:
    from observability import ServiceRateTracker

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────


def _record_and_validate(
    parsed: dict | None,
    *,
    rate_tracker: "ServiceRateTracker | None",
    required_keys: tuple[str, ...] | None,
) -> dict | None:
    """Emit exactly ONE record() call per LLM invocation and validate keys.

    Per CONTEXT.md D-04 (LLM success semantics):
      - success = parsed is not None AND (required_keys is None OR every
        required key is present in parsed)
      - failure = parsed is None (parse error / HTTP error already turned
        the backend's return into None) OR a required key is missing

    Returns the original parsed dict when valid, OR None when invalid
    (so callers see the same return-None contract as the legacy code).
    """
    if parsed is None:
        if rate_tracker is not None:
            rate_tracker.record("llm", False)
        return None

    if required_keys is not None:
        missing = [k for k in required_keys if k not in parsed]
        if missing:
            if rate_tracker is not None:
                rate_tracker.record("llm", False)
            logger.warning("LLM response missing required keys: %s", missing)
            return None

    if rate_tracker is not None:
        rate_tracker.record("llm", True)
    return parsed


# ── Backend dispatch ──────────────────────────────────────────────────


def chat_json(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
    required_keys: tuple[str, ...] | None = None,
) -> dict | None:
    """Send prompt, get parsed JSON response. Routes to configured backend.

    Per CONTEXT.md D-04, when ``rate_tracker`` is supplied this records
    exactly ONE outcome per call (success if parsed AND all required_keys
    present, failure otherwise). ``required_keys`` is per-call-site —
    callers (e.g. ``llm_parser.extract_with_llm``) pass the per-prompt
    expected-keys tuple so the rate reflects "extraction returned what we
    asked for" rather than "raw HTTP 200".

    Returns parsed dict on success, None on failure (parse error, HTTP
    error, or missing required key).
    """
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        parsed = _chat_ollama(prompt, system, max_tokens)
    elif backend == "openrouter":
        parsed = _chat_openrouter(prompt, system, max_tokens)
    else:
        parsed = _chat_anthropic(prompt, system, max_tokens, api_key)
    return _record_and_validate(
        parsed, rate_tracker=rate_tracker, required_keys=required_keys,
    )


def chat_json_async(
    prompt: str,
    system: str = "",
    max_tokens: int = 1024,
    api_key: str | None = None,
    *,
    rate_tracker: "ServiceRateTracker | None" = None,
    required_keys: tuple[str, ...] | None = None,
):
    """Async version — returns a coroutine. For llm_parser.py compatibility.

    Identical instrumentation semantics to ``chat_json`` (see docstring).
    """
    backend = getattr(cfg, "LLM_BACKEND", "anthropic")
    if backend == "ollama":
        coro = _chat_ollama_async(prompt, system, max_tokens)
    elif backend == "openrouter":
        coro = _chat_openrouter_async(prompt, system, max_tokens)
    else:
        coro = _chat_anthropic_async(prompt, system, max_tokens, api_key)

    async def _wrap():
        parsed = await coro
        return _record_and_validate(
            parsed, rate_tracker=rate_tracker, required_keys=required_keys,
        )

    return _wrap()


# ── Anthropic backend ────────────────────────────────────────────────


def _chat_anthropic(
    prompt: str, system: str, max_tokens: int, api_key: str | None,
) -> dict | None:
    """Call Claude Haiku via Anthropic API (sync)."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    model = getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.Anthropic(api_key=key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except Exception as e:
        logger.warning("Anthropic LLM call failed: %s", e)
        return None


async def _chat_anthropic_async(
    prompt: str, system: str, max_tokens: int, api_key: str | None,
) -> dict | None:
    """Call Claude Haiku via Anthropic API (async)."""
    import anthropic

    key = api_key or cfg.ANTHROPIC_API_KEY
    if not key:
        logger.warning("No Anthropic API key — skipping LLM call")
        return None

    model = getattr(cfg, "LLM_MODEL", "claude-haiku-4-5-20251001")
    try:
        client = anthropic.AsyncAnthropic(api_key=key)
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text.strip()
        return _parse_json(result_text)
    except Exception as e:
        logger.warning("Anthropic async LLM call failed: %s", e)
        return None


# ── Ollama backend ───────────────────────────────────────────────────


def _chat_ollama(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key="ollama")
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            # Retry once with explicit JSON instruction appended
            logger.debug("Ollama JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama LLM call failed: %s", e)
        return None


async def _chat_ollama_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call local Ollama model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    base_url = getattr(cfg, "OLLAMA_BASE_URL", "http://localhost:11434/v1/")
    model = getattr(cfg, "OLLAMA_MODEL", "qwen2.5:7b")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key="ollama")
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("Ollama async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("Ollama async LLM call failed: %s", e)
        return None


# ── OpenRouter backend ──────────────────────────────────────────────


def _chat_openrouter(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (sync)."""
    from openai import OpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = OpenAI(base_url=base_url, api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter LLM call failed: %s", e)
        return None


async def _chat_openrouter_async(
    prompt: str, system: str, max_tokens: int,
) -> dict | None:
    """Call OpenRouter model via OpenAI-compatible API (async)."""
    from openai import AsyncOpenAI

    api_key = getattr(cfg, "OPENROUTER_API_KEY", "")
    if not api_key:
        logger.warning("No OpenRouter API key — skipping LLM call")
        return None

    base_url = getattr(cfg, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    model = getattr(cfg, "OPENROUTER_MODEL", "qwen/qwen-2.5-72b-instruct")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        result_text = response.choices[0].message.content.strip()
        parsed = _parse_json(result_text)
        if parsed is None:
            logger.debug("OpenRouter async JSON parse failed, retrying with hint")
            retry_prompt = prompt + "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no explanation."
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    *(([{"role": "system", "content": system}] if system else [])),
                    {"role": "user", "content": retry_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            result_text = response.choices[0].message.content.strip()
            parsed = _parse_json(result_text)
        return parsed
    except Exception as e:
        logger.warning("OpenRouter async LLM call failed: %s", e)
        return None


# ── JSON parsing ─────────────────────────────────────────────────────


def _parse_json(text: str) -> dict | None:
    """Parse JSON from LLM response, stripping markdown fences."""
    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"items": result}  # Wrap list in dict for consistency
        return None
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        logger.debug("Failed to parse JSON from LLM response: %.200s", text)
        return None
