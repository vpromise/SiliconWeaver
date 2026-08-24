from __future__ import annotations

import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx2

from rtl_closure.backend import (
    ReasoningBackend,
    _validate_private_base_url,
    load_backend_config,
    public_api_origin,
)
from rtl_closure.errors import ModelInfrastructureError


class FakeStream:
    def __init__(self, parts: list[str]):
        self.parts = parts
        self.closed = False

    def __iter__(self):
        for part in self.parts:
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=part))]
            )

    def close(self):
        self.closed = True


class FakeHTTPError(Exception):
    def __init__(self, status_code: int, *, retry_after: int | None = None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.body = {
            "ray_id": "test-ray",
            **({"retry_after": retry_after} if retry_after is not None else {}),
        }
        self.response = None


class BackendTests(unittest.TestCase):
    def test_complete_rtl_closure_environment_never_reads_pi_state(self):
        environment = {
            "RTL_CLOSURE_MODEL": "gpt-test",
            "RTL_CLOSURE_BASE_URL": "https://example.invalid/v1",
            "RTL_CLOSURE_API_KEY": "secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = load_backend_config({})

        self.assertEqual(config["model"], "gpt-test")
        self.assertEqual(config["base_url"], "https://example.invalid/v1")
        self.assertEqual(config["provider"], "openai_compatible")

    def test_explicit_spec_model_overrides_environment_model(self):
        environment = {
            "RTL_CLOSURE_MODEL": "environment-model",
            "RTL_CLOSURE_BASE_URL": "https://example.invalid/v1",
            "RTL_CLOSURE_API_KEY": "secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = load_backend_config({"model": "spec-model"})

        self.assertEqual(config["model"], "spec-model")

    def test_missing_rtl_closure_environment_is_a_typed_configuration_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelInfrastructureError) as raised:
                load_backend_config({})

        self.assertFalse(raised.exception.retryable)
        self.assertFalse(raised.exception.repairable_by_rtl)
        self.assertEqual(raised.exception.failure_gate, "model_request")
        self.assertIn("RTL_CLOSURE_MODEL", str(raised.exception))

    def make_backend(self, *, events=None, max_retries=1, max_concurrency=2):
        openai_patch = patch("rtl_closure.backend.OpenAI")
        http_patch = patch(
            "rtl_closure.backend.DefaultHttpx2Client", return_value=object()
        )
        openai = openai_patch.start()
        http_patch.start()
        self.addCleanup(openai_patch.stop)
        self.addCleanup(http_patch.stop)
        backend = ReasoningBackend(
            model="gpt-test",
            base_url="https://example.invalid/v1",
            api_key="secret",
            reasoning_effort="xhigh",
            max_retries=max_retries,
            max_concurrency=max_concurrency,
            progress=(events.append if events is not None else None),
        )
        return backend, openai.return_value

    def test_openai3_httpx2_transport_streams_chat_completion(self):
        requests: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request)
            chunk = {
                "id": "chatcmpl-transport-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "module demo; endmodule"},
                        "finish_reason": None,
                    }
                ],
            }
            content = (
                "data: "
                + json.dumps(chunk, separators=(",", ":"))
                + "\n\ndata: [DONE]\n\n"
            ).encode("utf-8")
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=content,
                request=request,
            )

        transport_client = httpx2.Client(
            transport=httpx2.MockTransport(handler)
        )
        with patch(
            "rtl_closure.backend.DefaultHttpx2Client",
            return_value=transport_client,
        ):
            backend = ReasoningBackend(
                model="gpt-test",
                base_url="https://example.invalid/v1",
                api_key="secret",
            )
        try:
            self.assertEqual(
                backend.generate("design", max_tokens=64),
                ["module demo; endmodule"],
            )
        finally:
            backend.client.close()

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.url.path, "/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer secret")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "gpt-test")
        self.assertIs(payload["stream"], True)

    def test_streams_and_concatenates_content(self):
        events: list[dict] = []
        backend, client = self.make_backend(events=events)
        stream = FakeStream(["module ", "demo;", " endmodule"])
        client.chat.completions.create.return_value = stream

        result = backend.generate("design", max_tokens=64)

        self.assertEqual(result, ["module demo; endmodule"])
        self.assertTrue(stream.closed)
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["reasoning_effort"], "xhigh")
        self.assertRegex(
            kwargs["extra_headers"]["Idempotency-Key"],
            r"^rtl-closure-[0-9a-f]{64}$",
        )
        self.assertEqual(events[0]["event"], "request_started")
        self.assertRegex(events[0]["idempotency_key_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("stream_connected", [row["event"] for row in events])
        self.assertEqual(events[-1]["event"], "request_completed")
        self.assertNotIn("content", events[-1])

    def test_distinct_logical_calls_do_not_share_upstream_idempotency_key(self):
        backend, client = self.make_backend()
        client.chat.completions.create.side_effect = [
            FakeStream(["first"]),
            FakeStream(["second"]),
        ]

        self.assertEqual(backend.generate("same prompt"), ["first"])
        self.assertEqual(backend.generate("same prompt"), ["second"])

        first, second = client.chat.completions.create.call_args_list
        self.assertNotEqual(
            first.kwargs["extra_headers"]["Idempotency-Key"],
            second.kwargs["extra_headers"]["Idempotency-Key"],
        )

    def test_public_api_origin_never_exposes_url_credentials(self):
        unsafe = "https://user:secret@example.invalid:8443/v1?token=hidden#fragment"
        self.assertEqual(public_api_origin(unsafe), "https://example.invalid:8443")
        with self.assertRaisesRegex(RuntimeError, "must not embed credentials"):
            _validate_private_base_url(unsafe)

    def test_emits_heartbeat_while_stream_is_open(self):
        events: list[dict] = []
        backend, client = self.make_backend(events=events)

        class SlowStream(FakeStream):
            def __iter__(self):
                time.sleep(0.03)
                yield from super().__iter__()

        client.chat.completions.create.return_value = SlowStream(["ok"])

        with patch("rtl_closure.backend.MODEL_HEARTBEAT_SECONDS", 0.01):
            self.assertEqual(backend.generate("design"), ["ok"])

        heartbeats = [row for row in events if row["event"] == "request_waiting"]
        self.assertTrue(heartbeats)
        self.assertEqual(heartbeats[0]["chunk_count"], 0)

    def test_retryable_524_honors_retry_after_then_succeeds(self):
        events: list[dict] = []
        backend, client = self.make_backend(events=events, max_retries=1)
        client.chat.completions.create.side_effect = [
            FakeHTTPError(524, retry_after=120),
            FakeStream(["ok"]),
        ]

        with patch("rtl_closure.backend.time.sleep") as sleep:
            result = backend.generate("design")

        self.assertEqual(result, ["ok"])
        sleep.assert_called_once_with(120.0)
        retry = next(row for row in events if row["event"] == "request_retrying")
        self.assertEqual(retry["http_status"], 524)
        self.assertEqual(retry["ray_id"], "test-ray")
        calls = client.chat.completions.create.call_args_list
        self.assertEqual(
            calls[0].kwargs["extra_headers"]["Idempotency-Key"],
            calls[1].kwargs["extra_headers"]["Idempotency-Key"],
        )

    def test_midstream_failure_discards_partial_content_before_retry(self):
        backend, client = self.make_backend(max_retries=1)

        class BrokenStream(FakeStream):
            def __iter__(self):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(delta=SimpleNamespace(content="partial"))
                    ]
                )
                raise FakeHTTPError(524)

        broken = BrokenStream([])
        client.chat.completions.create.side_effect = [broken, FakeStream(["fresh"])]

        with patch("rtl_closure.backend.time.sleep"):
            result = backend.generate("design")

        self.assertEqual(result, ["fresh"])
        self.assertTrue(broken.closed)

    def test_does_not_sleep_after_final_failure(self):
        backend, client = self.make_backend(max_retries=1)
        client.chat.completions.create.side_effect = [
            FakeHTTPError(524, retry_after=120),
            FakeHTTPError(524, retry_after=120),
        ]

        with patch("rtl_closure.backend.time.sleep") as sleep:
            with self.assertRaisesRegex(ModelInfrastructureError, "failed after retries"):
                backend.generate("design")

        sleep.assert_called_once_with(120.0)

    def test_non_retryable_400_fails_immediately(self):
        backend, client = self.make_backend(max_retries=3)
        client.chat.completions.create.side_effect = FakeHTTPError(400)

        with patch("rtl_closure.backend.time.sleep") as sleep:
            with self.assertRaisesRegex(ModelInfrastructureError, "HTTP 400"):
                backend.generate("design")

        self.assertEqual(client.chat.completions.create.call_count, 1)
        sleep.assert_not_called()

    def test_candidate_batch_keeps_successes_in_original_order(self):
        backend, _ = self.make_backend(max_concurrency=2)

        def generate_candidate(*args, candidate_index, **kwargs):
            del args, kwargs
            if candidate_index == 2:
                raise RuntimeError("candidate failed")
            return f"candidate-{candidate_index}"

        with patch.object(backend, "_call", side_effect=generate_candidate):
            result = backend.generate("design", n=3)

        self.assertEqual(result, ["candidate-1", "", "candidate-3"])

    def test_candidate_batch_fails_only_when_every_candidate_fails(self):
        backend, _ = self.make_backend(max_concurrency=2)
        with patch.object(backend, "_call", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(ModelInfrastructureError, "all 3 model candidates failed"):
                backend.generate("design", n=3)


if __name__ == "__main__":
    unittest.main()
