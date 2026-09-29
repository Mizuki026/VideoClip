from __future__ import annotations

import asyncio
import json
import sys
import time
import unittest
from collections.abc import AsyncIterator
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx

from probes.stage0 import probe


class ManifestTests(unittest.TestCase):
    def test_manifest_covers_last_partial_segment(self) -> None:
        manifest = probe.build_manifest(61.25, 30)
        self.assertEqual(manifest, [(0, 0, 30), (1, 30, 60), (2, 60, 61.25)])

    def test_manifest_rejects_invalid_duration(self) -> None:
        with self.assertRaisesRegex(probe.ProbeError, "duration"):
            probe.build_manifest(0, 30)


class TimelineTests(unittest.TestCase):
    def fixture(self, audio_start: str = "0.000000") -> dict[str, object]:
        return {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "62.0", "start_time": "0.000000"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "pix_fmt": "yuv420p",
                    "start_time": "0.000000",
                    "duration": "62.0",
                    "time_base": "1/90000",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "start_time": audio_start,
                    "duration": "62.0",
                    "time_base": "1/48000",
                },
            ],
        }

    def test_zero_based_mp4_mapping_is_identity(self) -> None:
        self.assertEqual(probe.map_supported_mp4(self.fixture()), (0, 62))

    def test_delayed_audio_is_explicitly_rejected(self) -> None:
        with self.assertRaisesRegex(probe.ProbeError, "playback-tested mapping") as caught:
            probe.map_supported_mp4(self.fixture("0.500000"))
        self.assertEqual(caught.exception.code, "UNSUPPORTED_TIMELINE_OFFSET")

    def test_unknown_time_base_is_rejected(self) -> None:
        media = self.fixture()
        streams = cast(list[dict[str, object]], media["streams"])
        streams[0].pop("time_base")
        with self.assertRaisesRegex(probe.ProbeError, "stream starts and time bases"):
            probe.map_supported_mp4(media)


class CitationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segments = [probe.Segment(0, 0, 30, 0, 30, "本节介绍 FastAPI 处理请求流程。")]

    def test_normalized_continuous_quote_and_time_are_derived(self) -> None:
        raw = json.dumps(
            {
                "chapters": [{"title": "请求", "evidence": [{"segment_id": 0, "quote": "FastAPI 处理请求"}]}],
                "key_points": [{"text": "由 FastAPI 处理", "evidence": [{"segment_id": 0, "quote": "FastAPI 处理"}]}],
            },
            ensure_ascii=False,
        )
        result = probe.validate_summary(raw, self.segments)
        self.assertEqual(result["chapters"][0]["video_start"], 0)
        self.assertEqual(result["chapters"][0]["video_end"], 30)

    def test_non_contiguous_or_unknown_citation_fails(self) -> None:
        raw = json.dumps(
            {
                "chapters": [{"title": "请求", "evidence": [{"segment_id": 0, "quote": "FastAPI流程"}]}],
                "key_points": [{"text": "请求", "evidence": [{"segment_id": 0, "quote": "FastAPI"}]}],
            }
        )
        with self.assertRaisesRegex(probe.ProbeError, "continuous normalized"):
            probe.validate_summary(raw, self.segments)
        raw = raw.replace('"segment_id": 0', '"segment_id": 3')
        with self.assertRaisesRegex(probe.ProbeError, "complete run"):
            probe.validate_summary(raw, self.segments)


class ConfigurationTests(unittest.TestCase):
    def complete(self) -> dict[str, str]:
        return {
            "SILICONFLOW_API_KEY": "test-only-secret",
            "SILICONFLOW_ASR_MODEL": "FunAudioLLM/SenseVoiceSmall",
            "SILICONFLOW_LLM_MODEL": "THUDM/GLM-4-9B-0414",
            "SILICONFLOW_ASR_PRICE_EVIDENCE": "test assertion only",
            "SILICONFLOW_ASR_USD_PER_MINUTE": "0",
            "SILICONFLOW_LLM_PRICE_EVIDENCE": "test assertion only",
            "SILICONFLOW_LLM_INPUT_USD_PER_MILLION": "0",
            "SILICONFLOW_LLM_OUTPUT_USD_PER_MILLION": "0",
            "SILICONFLOW_FREE_USE_CONFIRMED": "I_CONFIRMED_BOTH_MODELS_ARE_FREE_IN_MY_ACCOUNT",
            "LLM_CONTEXT_TOKENS": "32768",
        }

    def test_requires_explicit_free_confirmation_and_price_evidence(self) -> None:
        values = self.complete()
        values.pop("SILICONFLOW_FREE_USE_CONFIRMED")
        with self.assertRaisesRegex(probe.ProbeError, "confirm both selected models"):
            probe.settings_from(values)
        values = self.complete()
        values["SILICONFLOW_LLM_MODEL"] = "Pro/vendor/model"
        with self.assertRaisesRegex(probe.ProbeError, "Pro model IDs"):
            probe.settings_from(values)

    def test_cannot_configure_more_than_three_summary_calls(self) -> None:
        values = self.complete()
        values["MAX_SUMMARY_REQUESTS"] = "4"
        with self.assertRaisesRegex(probe.ProbeError, "cannot exceed 3"):
            probe.settings_from(values)

    def test_paid_price_is_refused_and_zero_rate_estimate_is_explicit(self) -> None:
        values = self.complete()
        values["SILICONFLOW_LLM_INPUT_USD_PER_MILLION"] = "0.01"
        with self.assertRaisesRegex(probe.ProbeError, "outside the free-only"):
            probe.settings_from(values)
        settings = probe.settings_from(self.complete())
        result = probe.estimated_cost(
            settings,
            90,
            [{"usage": {"prompt_tokens": 1000, "completion_tokens": 250}}],
        )
        self.assertEqual(result["total_estimated_usd"], 0)
        self.assertEqual(result["llm_prompt_tokens"], 1000)


class BoundedExecutionTests(unittest.TestCase):
    def test_media_hang_is_stopped_within_deadline_and_grace(self) -> None:
        started = time.monotonic()
        with self.assertRaisesRegex(probe.ProbeError, "total deadline") as caught:
            probe._run_limited([sys.executable, "-c", "import time; time.sleep(30)"], 0.15, 0.5, 128)
        self.assertEqual(caught.exception.code, "MEDIA_TIMEOUT")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_media_output_limit_terminates_noisy_child(self) -> None:
        with self.assertRaisesRegex(probe.ProbeError, "output exceeded") as caught:
            probe._run_limited([sys.executable, "-c", "print('x' * 100000)"], 2, 0.5, 128)
        self.assertEqual(caught.exception.code, "MEDIA_DIAGNOSTIC_LIMIT")

    def test_http_total_deadline_stops_slow_upstream(self) -> None:
        values = ConfigurationTests().complete()
        values["HTTP_TOTAL_TIMEOUT_SECONDS"] = "0.05"
        settings = probe.settings_from(values)

        async def handler(_: httpx.Request) -> httpx.Response:
            await asyncio.sleep(2)
            return httpx.Response(200, json={"choices": []})

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(probe.ProbeError, "total deadline") as caught:
                    await probe.llm_request(client, settings, "bounded request")
                self.assertEqual(caught.exception.code, "HTTP_TOTAL_TIMEOUT")

        asyncio.run(run())

    def test_http_total_deadline_stops_continuous_slow_body(self) -> None:
        values = ConfigurationTests().complete()
        values["HTTP_TOTAL_TIMEOUT_SECONDS"] = "0.09"
        values["HTTP_READ_TIMEOUT_SECONDS"] = "2"
        settings = probe.settings_from(values)

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                for chunk in (b'{"choices":', b'[{"message":', b'{"content":', b'"{}"}}]}'):
                    await asyncio.sleep(0.04)
                    yield chunk

        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=SlowBody())

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(probe.ProbeError, "total deadline") as caught:
                    await probe.llm_request(client, settings, "slow request")
                self.assertEqual(caught.exception.code, "HTTP_TOTAL_TIMEOUT")

        asyncio.run(run())

    def test_authentication_and_rate_limit_fail_without_retry(self) -> None:
        settings = probe.settings_from(ConfigurationTests().complete())

        async def check_status(status: int, expected: str) -> None:
            request_count = 0

            async def handler(_: httpx.Request) -> httpx.Response:
                nonlocal request_count
                request_count += 1
                return httpx.Response(status, json={"message": "redacted"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaises(probe.ProbeError) as caught:
                    await probe.llm_request(client, settings, "one request")
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(request_count, 1)

        async def run() -> None:
            await check_status(401, "AUTHENTICATION_FAILED")
            await check_status(429, "RATE_LIMITED")

        asyncio.run(run())

    def test_response_body_cap_is_checked_during_streaming(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 9)

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with client.stream("GET", "https://example.test") as response:
                    with self.assertRaisesRegex(probe.ProbeError, "byte cap") as caught:
                        await probe._safe_response_bytes(response, 8)
                    self.assertEqual(caught.exception.code, "RESPONSE_TOO_LARGE")

        asyncio.run(run())

    def test_summary_repair_is_limited_to_three_requests(self) -> None:
        values = ConfigurationTests().complete()
        settings = probe.settings_from(values)
        invalid = '{"chapters": [], "key_points": []}'
        fake = AsyncMock(return_value=(invalid, {"duration_seconds": 0.01}))

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                with patch.object(probe, "llm_request", fake):
                    with self.assertRaisesRegex(probe.ProbeError, "after 3 requests"):
                        await probe.summarize(client, settings, [probe.Segment(0, 0, 30, 0, 30, "text")])

        asyncio.run(run())
        self.assertEqual(fake.await_count, 3)


class ContextBudgetTests(unittest.TestCase):
    def test_20m_projection_scales_complete_transcript_and_reserves_output(self) -> None:
        segments = [probe.Segment(0, 0, 60, 0, 60, "课程文本" * 100)]
        result = probe.estimate_20m_context(segments, "test-v1", 128_000, 4_096)
        self.assertEqual(result["target_duration_seconds"], 1200)
        self.assertEqual(result["projection_factor"], 1.5)
        self.assertGreater(result["projected_transcript_utf8_bytes"], result["observed_transcript_utf8_bytes"])
        self.assertTrue(result["fits_conservative_estimate"])

    def test_sample_upper_bound_counts_complete_prompt_once_and_message_overhead(self) -> None:
        prompt = probe.SYSTEM_PROMPT + "\n课程转写"
        result = probe.upper_bound_context_bytes("", prompt, 4096, 8192)
        expected_bytes = len(prompt.encode("utf-8"))
        self.assertEqual(result["input_utf8_bytes_upper_bound"], expected_bytes)
        self.assertEqual(
            result["required_context_upper_bound"],
            expected_bytes + 4096 + probe.CHAT_MESSAGE_OVERHEAD_TOKENS,
        )


if __name__ == "__main__":
    unittest.main()
