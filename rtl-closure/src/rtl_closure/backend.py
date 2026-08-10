"""Credential-safe API backend for isolated RTL-Closure agents."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from openai import APIConnectionError, APITimeoutError, DefaultHttpxClient, OpenAI

from .errors import ModelInfrastructureError


MODEL_HEARTBEAT_SECONDS = 30


class Backend:
    """Minimal local generation backend interface used by RTL-Closure."""

    def generate(
        self,
        prompt: str,
        n: int = 1,
        temperature: float = 0.6,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
    ) -> list[str]:
        raise NotImplementedError


def public_api_origin(base_url: str) -> str:
    """Return a credential-free endpoint identity suitable for logs and state."""
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "<redacted-invalid-url>"
    if parsed.scheme not in {"http", "https"} or not hostname:
        return "<redacted-invalid-url>"
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = rendered_host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, netloc, "", "", ""))


def _validate_private_base_url(base_url: str) -> None:
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("RTL-Closure model base URL is invalid") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("RTL-Closure model base URL must be absolute HTTP(S)")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(
            "RTL-Closure model base URL must not embed credentials, query, or fragment"
        )


def _chat_messages(
    prompt: str, system_prompt: str | None = None
) -> list[dict[str, str]]:
    """Build OpenAI-compatible messages without relying on provider internals."""
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _bounded_env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be from {low} to {high}")
    return value


def _retryable_model_error(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (
        status in {408, 409, 425, 429} or 500 <= status <= 599
    )


def _error_metadata(exc: Exception) -> dict[str, Any]:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        body = {}
    status = getattr(exc, "status_code", None)
    ray_id = body.get("ray_id") or body.get("instance")
    retry_after = body.get("retry_after")
    response = getattr(exc, "response", None)
    if retry_after is None and response is not None:
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("retry-after")
    try:
        retry_after_seconds = float(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after_seconds = None
    return {
        "error_type": type(exc).__name__,
        "http_status": status if isinstance(status, int) else None,
        "ray_id": str(ray_id) if ray_id else None,
        "retry_after_seconds": retry_after_seconds,
    }


def _safe_error_summary(exc: Exception) -> str:
    metadata = _error_metadata(exc)
    parts = [str(metadata["error_type"])]
    if metadata["http_status"] is not None:
        parts.append(f"HTTP {metadata['http_status']}")
    if metadata["ray_id"] is not None:
        parts.append(f"ray_id={metadata['ray_id']}")
    return ", ".join(parts)


def load_backend_config(generation: dict[str, Any]) -> dict[str, str]:
    provider_name = os.environ.get("RTL_CLOSURE_PROVIDER", "openai_compatible")
    configured_model = (
        generation.get("model")
        if "model" in generation
        else os.environ.get("RTL_CLOSURE_MODEL")
    )
    model = str(configured_model or "")
    base_url = str(os.environ.get("RTL_CLOSURE_BASE_URL") or "")
    api_key = str(os.environ.get("RTL_CLOSURE_API_KEY") or "")
    missing = [
        name
        for name, value in (
            ("RTL_CLOSURE_MODEL", model),
            ("RTL_CLOSURE_BASE_URL", base_url),
            ("RTL_CLOSURE_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise ModelInfrastructureError(
            "incomplete RTL-Closure model configuration; missing " + ", ".join(missing),
            retryable=False,
        )
    _validate_private_base_url(base_url)
    return {
        "provider": provider_name,
        "model": model,
        "base_url": base_url,
        "public_base_url": public_api_origin(base_url),
        "api_key": api_key,
    }


class ReasoningBackend(Backend):
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        provider: str = "openai_compatible",
        reasoning_effort: str | None = None,
        timeout_seconds: int = 900,
        max_retries: int = 2,
        max_concurrency: int = 2,
        request_scope: str | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.model = model
        self.provider = provider
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_concurrency = max_concurrency
        self.request_scope = request_scope or uuid.uuid4().hex
        self.progress = progress
        self._request_lock = threading.Lock()
        self._request_sequence = 0
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=DefaultHttpxClient(trust_env=False),
        )

    def _emit(self, event: dict[str, Any]) -> None:
        if self.progress is None:
            return
        try:
            self.progress(event)
        except Exception:
            pass

    def _next_request_id(self) -> int:
        with self._request_lock:
            self._request_sequence += 1
            return self._request_sequence

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        retry_after = _error_metadata(exc)["retry_after_seconds"]
        exponential = float(min(30, 2 ** (attempt - 1)))
        if retry_after is None:
            return exponential
        return max(exponential, min(300.0, retry_after))

    def _call(
        self,
        prompt: str,
        temperature: float,
        max_tokens: int,
        system_prompt: str | None,
        *,
        candidate_index: int,
        candidate_count: int,
    ) -> str:
        request_id = self._next_request_id()
        logical_request_key = hashlib.sha256(
            json.dumps(
                {
                    "provider": self.provider,
                    "model": self.model,
                    "request_scope": self.request_scope,
                    "logical_request_id": request_id,
                    "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                    "system_prompt_sha256": (
                        hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
                        if system_prompt is not None
                        else None
                    ),
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "candidate_index": candidate_index,
                    "candidate_count": candidate_count,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        max_attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            started = time.monotonic()
            heartbeat_stop = threading.Event()
            heartbeat_lock = threading.Lock()
            heartbeat_state: dict[str, Any] = {
                "connected": False,
                "chunk_count": 0,
            }
            self._emit(
                {
                    "event": "request_started",
                    "request_id": request_id,
                    "candidate_index": candidate_index,
                    "candidate_count": candidate_count,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "model": self.model,
                    "streaming": True,
                    "idempotency_key_sha256": logical_request_key,
                }
            )

            def heartbeat() -> None:
                while not heartbeat_stop.wait(MODEL_HEARTBEAT_SECONDS):
                    with heartbeat_lock:
                        connected = bool(heartbeat_state["connected"])
                        chunk_count = int(heartbeat_state["chunk_count"])
                    self._emit(
                        {
                            "event": "streaming" if connected else "request_waiting",
                            "request_id": request_id,
                            "candidate_index": candidate_index,
                            "candidate_count": candidate_count,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "elapsed_seconds": round(time.monotonic() - started),
                            "chunk_count": chunk_count,
                        }
                    )

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name=f"rtl_closure-model-heartbeat-{request_id}",
                daemon=True,
            )
            heartbeat_thread.start()
            attempt_error: Exception | None = None
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": _chat_messages(prompt, system_prompt),
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "top_p": 0.95,
                    "stream": True,
                    "extra_headers": {
                        "Idempotency-Key": f"rtl-closure-{logical_request_key}"
                    },
                }
                if self.reasoning_effort:
                    kwargs["reasoning_effort"] = self.reasoning_effort
                stream = self.client.chat.completions.create(**kwargs)
                parts: list[str] = []
                chunks = 0
                first_event_seconds: float | None = None
                try:
                    for chunk in stream:
                        now = time.monotonic()
                        chunks += 1
                        with heartbeat_lock:
                            heartbeat_state["connected"] = True
                            heartbeat_state["chunk_count"] = chunks
                        if first_event_seconds is None:
                            first_event_seconds = now - started
                            self._emit(
                                {
                                    "event": "stream_connected",
                                    "request_id": request_id,
                                    "candidate_index": candidate_index,
                                    "candidate_count": candidate_count,
                                    "attempt": attempt,
                                    "first_event_seconds": round(
                                        first_event_seconds, 3
                                    ),
                                }
                            )
                        if not chunk.choices:
                            continue
                        content = chunk.choices[0].delta.content
                        if content:
                            parts.append(content)
                finally:
                    close = getattr(stream, "close", None)
                    if callable(close):
                        close()
                content = "".join(parts)
                if not content.strip():
                    raise RuntimeError("model stream returned no content")
                self._emit(
                    {
                        "event": "request_completed",
                        "request_id": request_id,
                        "candidate_index": candidate_index,
                        "candidate_count": candidate_count,
                        "attempt": attempt,
                        "elapsed_seconds": round(time.monotonic() - started),
                        "chunk_count": chunks,
                        "content_chars": len(content),
                    }
                )
                return content
            except Exception as exc:
                attempt_error = exc
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)

            assert attempt_error is not None
            last_error = attempt_error
            retryable = _retryable_model_error(attempt_error)
            metadata = _error_metadata(attempt_error)
            if not retryable or attempt >= max_attempts:
                self._emit(
                    {
                        "event": "request_failed",
                        "request_id": request_id,
                        "candidate_index": candidate_index,
                        "candidate_count": candidate_count,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "retryable": retryable,
                        **metadata,
                    }
                )
                break
            delay = self._retry_delay(attempt_error, attempt)
            self._emit(
                {
                    "event": "request_retrying",
                    "request_id": request_id,
                    "candidate_index": candidate_index,
                    "candidate_count": candidate_count,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "retry_in_seconds": delay,
                    **metadata,
                }
            )
            time.sleep(delay)
        assert last_error is not None
        raise ModelInfrastructureError(
            "model call failed after retries: " + _safe_error_summary(last_error),
            retryable=_retryable_model_error(last_error),
        ) from last_error

    def generate(
        self,
        prompt: str,
        n: int = 1,
        temperature: float = 0.6,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
    ) -> list[str]:
        if n <= 1:
            return [
                self._call(
                    prompt,
                    temperature,
                    max_tokens,
                    system_prompt,
                    candidate_index=1,
                    candidate_count=1,
                )
            ]
        responses: list[str | None] = [None] * n
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=min(n, self.max_concurrency)) as pool:
            futures = {
                pool.submit(
                    self._call,
                    prompt,
                    temperature,
                    max_tokens,
                    system_prompt,
                    candidate_index=index + 1,
                    candidate_count=n,
                ): index
                for index in range(n)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    responses[index] = future.result()
                except Exception as exc:
                    errors.append(exc)
                    responses[index] = ""
        if not any(responses):
            detail = errors[-1] if errors else "no candidate returned content"
            if isinstance(detail, ModelInfrastructureError):
                raise detail
            raise ModelInfrastructureError(
                f"all {n} model candidates failed: {detail}", retryable=True
            )
        return [response or "" for response in responses]


class StaticBackend(Backend):
    """A Backend that keeps a previously reviewed artifact immutable."""

    def __init__(self, content: str, *, language: str):
        self.content = content
        self.language = language

    def generate(
        self,
        prompt: str,
        n: int = 1,
        temperature: float = 0.6,
        max_tokens: int = 4096,
        system_prompt: str | None = None,
    ) -> list[str]:
        del prompt, temperature, max_tokens, system_prompt
        wrapped = f"<answer>\n```{self.language}\n{self.content}\n```\n</answer>"
        return [wrapped for _ in range(n)]


def make_backends(
    generation: dict[str, Any],
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    request_scope: str | None = None,
) -> tuple[Backend, Backend, dict[str, Any]]:
    if generation.get("profile", "api") != "api":
        raise RuntimeError("rtl-closure supports only the isolated api profile")
    config = load_backend_config(generation)
    timeout_seconds = _bounded_env_int(
        "RTL_CLOSURE_MODEL_TIMEOUT_SECONDS", 900, 30, 3600
    )
    max_retries = _bounded_env_int("RTL_CLOSURE_MODEL_MAX_RETRIES", 2, 0, 8)
    max_concurrency = _bounded_env_int(
        "RTL_CLOSURE_MODEL_CONCURRENCY", 2, 1, 8
    )
    backend = ReasoningBackend(
        provider=config["provider"],
        model=config["model"],
        base_url=config["base_url"],
        api_key=config["api_key"],
        reasoning_effort=generation["reasoning_effort"],
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        max_concurrency=max_concurrency,
        request_scope=request_scope,
        progress=progress,
    )
    public = {
        "profile": "api",
        "provider": config["provider"],
        "model": config["model"],
        "base_url": config["public_base_url"],
        "reasoning_effort": generation["reasoning_effort"],
        "streaming": True,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "max_concurrency": max_concurrency,
    }
    return backend, backend, public
