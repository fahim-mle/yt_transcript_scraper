"""
One gateway for every LLM call in the pipeline.

`rewriter` and `llm_processor` used to each hold their own copy of an Ollama
HTTP call, which meant "run this against a hosted model instead" was a change
in two files with two different payload shapes. They now both call `chat()`
here and neither knows which backend answered.

Two backends:

  ollama  — POST {OLLAMA_HOST}/api/chat, JSON schema via the `format` field.
  openai  — POST {LLM_BASE_URL}/chat/completions with a bearer key. This is the
            wire protocol, not the vendor: Z.ai/GLM, OpenRouter, Together and
            a local vLLM all speak it, so switching providers is a URL and a
            key, never a code change.

Every call returns a `ChatResult` carrying token usage. Usage is recorded as a
physical fact about the request; pricing is deliberately *not* applied here
(see `config.LLM_BILLING_MODE`) because on a subscription plan the marginal
cost is zero and a dollar figure written now would be a number later analysis
would wrongly trust.
"""

import json
import logging
import time
import urllib.error
import urllib.request

import config

logger = logging.getLogger(__name__)


class Usage:
    """Token counts for one call. Zeros when the backend does not report them."""

    __slots__ = ("prompt_tokens", "completion_tokens", "cached_tokens", "calls")

    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0,
                 cached_tokens: int = 0, calls: int = 1) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens
        self.calls = calls

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.calls += other.calls

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Usage(prompt={self.prompt_tokens}, completion={self.completion_tokens}, "
                f"calls={self.calls})")


class ChatResult:
    """
    Outcome of one chat call.

    `text` is the raw assistant message. `data` is that message parsed as JSON,
    populated only when a schema was requested. `error` is None on success —
    callers check `ok` rather than inspecting the transport.
    """

    __slots__ = ("text", "data", "usage", "error", "model", "provider", "duration_ms")

    def __init__(self, *, text: str | None, data: dict | None, usage: Usage,
                 error: str | None, model: str, provider: str, duration_ms: int) -> None:
        self.text = text
        self.data = data
        self.usage = usage
        self.error = error
        self.model = model
        self.provider = provider
        self.duration_ms = duration_ms

    @property
    def ok(self) -> bool:
        return self.error is None and self.text is not None


# ── shared HTTP ────────────────────────────────────────────────────────────

def _post(url: str, payload: dict, headers: dict, timeout: int) -> tuple[dict | None, str | None]:
    """POST JSON and return (body, error). Never raises for transport failure."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        # The body of an error response is where providers explain themselves;
        # losing it turns every misconfiguration into a bare status code.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        return None, f"http_{exc.code}: {detail or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"unreachable: {exc.reason}"
    except TimeoutError:
        return None, "timeout"
    except json.JSONDecodeError as exc:
        return None, f"invalid_json_response: {exc}"


def _extra_body() -> dict:
    """Provider-specific request fields from LLM_EXTRA_BODY, or {}."""
    if not config.LLM_EXTRA_BODY.strip():
        return {}
    try:
        extra = json.loads(config.LLM_EXTRA_BODY)
    except json.JSONDecodeError as exc:
        logger.warning("LLM_EXTRA_BODY is not valid JSON, ignoring it: %s", exc)
        return {}
    if not isinstance(extra, dict):
        logger.warning("LLM_EXTRA_BODY must be a JSON object, ignoring it.")
        return {}
    return extra


# ── ollama backend ─────────────────────────────────────────────────────────

def _chat_ollama(*, model: str, system: str, user: str, temperature: float,
                 max_tokens: int | None, schema: dict | None, num_ctx: int,
                 timeout: int) -> tuple[str | None, Usage, str | None]:
    options: dict = {"temperature": temperature, "num_ctx": num_ctx}
    if max_tokens:
        options["num_predict"] = max_tokens

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        # Neither stage is a reasoning task; thinking tokens are pure cost.
        # Ignored by models that don't support the flag.
        "think": False,
        "options": options,
        **_extra_body(),
    }
    if schema is not None:
        payload["format"] = schema

    body, error = _post(f"{config.OLLAMA_HOST}/api/chat", payload, {}, timeout)
    if error:
        return None, Usage(calls=1), error

    usage = Usage(
        prompt_tokens=body.get("prompt_eval_count", 0) or 0,
        completion_tokens=body.get("eval_count", 0) or 0,
    )
    content = (body.get("message") or {}).get("content", "")
    if not content:
        return None, usage, "empty_response"
    return content, usage, None


# ── openai-compatible backend ──────────────────────────────────────────────

def _response_format(schema: dict | None, mode: str) -> dict | None:
    if schema is None:
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {"name": "result", "schema": schema, "strict": False},
    }


def _chat_openai(*, model: str, system: str, user: str, temperature: float,
                 max_tokens: int | None, schema: dict | None, timeout: int
                 ) -> tuple[str | None, Usage, str | None]:
    if not config.LLM_BASE_URL:
        return None, Usage(calls=1), "LLM_BASE_URL is not set"
    if not config.LLM_API_KEY:
        return None, Usage(calls=1), "LLM_API_KEY is not set"

    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    url = f"{config.LLM_BASE_URL}/chat/completions"

    def build(mode: str) -> dict:
        payload: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": temperature,
            **_extra_body(),
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        rf = _response_format(schema, mode)
        if rf:
            payload["response_format"] = rf
        return payload

    body, error = _post(url, build("json_schema"), headers, timeout)

    # Structured-output support is uneven across OpenAI-compatible providers:
    # some accept only {"type": "json_object"}. A 400 on the schema form is a
    # capability signal, not a failure — retry once in the simpler mode. The
    # caller validates with Pydantic either way, so nothing is lost.
    if error and error.startswith("http_400") and schema is not None:
        logger.info("Provider rejected json_schema response_format; retrying as json_object.")
        body, error = _post(url, build("json_object"), headers, timeout)

    if error:
        return None, Usage(calls=1), error

    raw_usage = body.get("usage") or {}
    details = raw_usage.get("prompt_tokens_details") or {}
    usage = Usage(
        prompt_tokens=raw_usage.get("prompt_tokens", 0) or 0,
        completion_tokens=raw_usage.get("completion_tokens", 0) or 0,
        cached_tokens=details.get("cached_tokens", 0) or 0,
    )

    choices = body.get("choices") or []
    if not choices:
        return None, usage, "no_choices_returned"

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if content:
        return content, usage, None

    # Empty content is not one failure but several, and they need different
    # fixes. A reasoning model that spent the whole budget thinking reports
    # finish_reason=length with reasoning tokens but no answer — raising
    # max_tokens or disabling thinking fixes that, while a bare "empty
    # response" sends you looking at the wrong thing entirely.
    finish = choices[0].get("finish_reason") or "unknown"
    thinking = (message.get("reasoning_content") or "").strip()
    if thinking:
        return None, usage, (
            f"model_returned_only_reasoning (finish_reason={finish}, "
            f"{usage.completion_tokens} completion tokens) — raise max_tokens "
            f"or disable thinking via LLM_EXTRA_BODY"
        )
    return None, usage, f"empty_response (finish_reason={finish})"


# ── public API ─────────────────────────────────────────────────────────────

def chat(*, model: str, system: str, user: str, temperature: float = 0.2,
         max_tokens: int | None = None, schema: dict | None = None,
         num_ctx: int = 8192, timeout: int = 600) -> ChatResult:
    """
    Send one chat request through the configured provider.

    `schema` asks for JSON output constrained to that JSON Schema and makes
    `ChatResult.data` the parsed object. Always returns a ChatResult — transport
    and provider failures land in `.error`, never as an exception.
    """
    provider = config.LLM_PROVIDER
    started = time.monotonic()

    if provider == "ollama":
        text, usage, error = _chat_ollama(
            model=model, system=system, user=user, temperature=temperature,
            max_tokens=max_tokens, schema=schema, num_ctx=num_ctx, timeout=timeout,
        )
    elif provider in {"openai", "openai-compatible"}:
        text, usage, error = _chat_openai(
            model=model, system=system, user=user, temperature=temperature,
            max_tokens=max_tokens, schema=schema, timeout=timeout,
        )
    else:
        text, usage, error = None, Usage(calls=1), f"unknown LLM_PROVIDER {provider!r}"

    duration_ms = int((time.monotonic() - started) * 1000)

    data = None
    if text is not None and schema is not None:
        try:
            data = json.loads(_strip_json_fence(text))
        except json.JSONDecodeError:
            error = "model_output_was_not_valid_json"
            text = None

    if error:
        logger.warning("LLM call failed (%s/%s): %s", provider, model, error)

    return ChatResult(text=text, data=data, usage=usage, error=error,
                      model=model, provider=provider, duration_ms=duration_ms)


def _strip_json_fence(text: str) -> str:
    """Unwrap ```json fences some providers add even in JSON mode."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[-1]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


def is_available() -> bool:
    """
    Cheap reachability check for the configured provider.

    For a hosted endpoint this only verifies that a base URL and key are
    present — a real request is the only way to know a key is valid, and
    spending one on every worker start is not worth it.
    """
    if config.LLM_PROVIDER == "ollama":
        try:
            req = urllib.request.Request(f"{config.OLLAMA_HOST}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False
    return bool(config.LLM_BASE_URL and config.LLM_API_KEY)


def describe() -> str:
    """Human-readable provider summary for logs."""
    if config.LLM_PROVIDER == "ollama":
        return f"ollama @ {config.OLLAMA_HOST}"
    return f"{config.LLM_PROVIDER} @ {config.LLM_BASE_URL or '<no base url>'}"
