from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
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


class SampleScopeTests(unittest.TestCase):
    def test_authorized_video_and_private_input_paths_are_accepted(self) -> None:
        self.assertEqual(
            probe.ensure_authorized_sample_path(probe.AUTHORIZED_VIDEO_ROOT / "sample.mp4"),
            (probe.AUTHORIZED_VIDEO_ROOT / "sample.mp4").resolve(),
        )
        private_input = probe.DATA_ROOT / "input" / "sample.mp4"
        self.assertEqual(probe.ensure_authorized_sample_path(private_input), private_input.resolve())

    def test_other_locations_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(probe.ProbeError, "repository video directory"):
                probe.ensure_authorized_sample_path(Path(temp_dir) / "sample.mp4")

    def test_duration_accepts_any_positive_value_up_to_20_minutes(self) -> None:
        probe.validate_sample_duration(0.01, 1200)
        probe.validate_sample_duration(368.6, 1200)
        probe.validate_sample_duration(1200, 1200)
        with self.assertRaisesRegex(probe.ProbeError, "greater than zero"):
            probe.validate_sample_duration(0, 1200)
        with self.assertRaisesRegex(probe.ProbeError, "duration limit"):
            probe.validate_sample_duration(1200.001, 1200)


class ProbeReportTests(unittest.TestCase):
    def test_failed_summary_report_keeps_completed_media_and_asr_evidence(self) -> None:
        async def fail_summary(
            *_: object, **__: object
        ) -> tuple[dict[str, object], list[dict[str, object]]]:
            raise probe.ProbeError("HTTP_TIMEOUT", "summary timed out")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_root = root / "data"
            video_root = root / "video"
            video_root.mkdir()
            sample = video_root / "sample.mp4"
            sample.write_bytes(b"authorized fixture")

            async def fake_asr(*_: object) -> tuple[str, dict[str, object]]:
                return "FastAPI handles requests", {"duration_seconds": 0.01}

            def fake_extract(
                _: Path, work_dir: Path, duration: float, settings: probe.Settings
            ) -> list[dict[str, object]]:
                manifest = probe.build_manifest(duration, settings.segment_seconds)
                result: list[dict[str, object]] = []
                for segment_id, start, end in manifest:
                    audio_path = work_dir / f"{segment_id}.wav"
                    audio_path.write_bytes(b"wav fixture")
                    result.append(
                        {
                            "segment_id": segment_id,
                            "audio_start": start,
                            "audio_end": end,
                            "audio_path": audio_path,
                        }
                    )
                return result

            with (
                patch.object(probe, "DATA_ROOT", data_root),
                patch.object(probe, "AUTHORIZED_VIDEO_ROOT", video_root),
                patch.object(probe, "ffprobe_media", return_value=TimelineTests().fixture()),
                patch.object(probe, "extract_segments", side_effect=fake_extract),
                patch.object(probe, "asr_request", side_effect=fake_asr),
                patch.object(probe, "summarize", side_effect=fail_summary),
            ):
                report_path = asyncio.run(probe.run_probe(sample, probe.settings_from(ConfigurationTests().complete())))

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "failed")
            self.assertEqual(len(report["asr"]), 3)
            self.assertEqual(
                report["pipeline"],
                [
                    "ffprobe",
                    "ffmpeg extract + complete 30-second manifest",
                    "sequential real ASR for every segment",
                ],
            )
            self.assertEqual(report["llm_attempt"]["status"], "failed")
            self.assertEqual(report["failure"]["code"], "HTTP_TIMEOUT")


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

    def test_prompt_requests_source_segment_and_program_derived_quote(self) -> None:
        source = "FastAPI 处理请求流程，并使用依赖注入完成参数校验和响应生成。"
        segments = [probe.Segment(4, 120, 150, 120, 150, source)]
        prompt = probe.summary_prompt(segments, "test-v4")
        self.assertIn("[4] 120.000-150.000s", prompt)
        self.assertIn("不要输出 quote", prompt)
        self.assertIn("提取最相关的连续原文", prompt)

    def test_quote_is_chosen_as_an_exact_source_window_matching_the_summary(self) -> None:
        source = "前段介绍背景信息。" * 8 + "数据库连接池复用连接以减少重复建立连接的开销。" + "后段补充说明。" * 8
        quote = probe.best_source_excerpt("连接池复用连接减少重复建立开销", source)
        self.assertIn("连接池复用连接", quote)
        self.assertIn(quote, source)
        self.assertLessEqual(len(quote), 48)

    def test_normalized_continuous_quote_and_time_are_derived(self) -> None:
        evidence = [{"segment_id": 0}]
        raw = json.dumps(
            {
                "chapters": [{"title": f"请求 {index}", "evidence": evidence} for index in range(3)],
                "key_points": [{"text": f"由 FastAPI 处理 {index}", "evidence": evidence} for index in range(3)],
            },
            ensure_ascii=False,
        )
        result = probe.validate_summary(raw, self.segments)
        self.assertEqual(result["chapters"][0]["evidence"][0]["quote"], self.segments[0].text)
        self.assertEqual(result["chapters"][0]["video_start"], 0)
        self.assertEqual(result["chapters"][0]["video_end"], 30)

    def test_model_quote_or_unknown_segment_fails(self) -> None:
        raw = json.dumps(
            {
                "chapters": [{"title": f"请求 {index}", "evidence": [{"segment_id": 0, "quote": "臆造引文"}]} for index in range(3)],
                "key_points": [{"text": f"请求 {index}", "evidence": [{"segment_id": 0, "quote": "臆造引文"}]} for index in range(3)],
            }
        )
        with self.assertRaisesRegex(probe.ProbeError, "evidence needs only a segment_id"):
            probe.validate_summary(raw, self.segments)
        raw = json.dumps(
            {
                "chapters": [{"title": f"请求 {index}", "evidence": [{"segment_id": 3}]} for index in range(3)],
                "key_points": [{"text": f"请求 {index}", "evidence": [{"segment_id": 3}]} for index in range(3)],
            }
        )
        with self.assertRaisesRegex(probe.ProbeError, "complete run"):
            probe.validate_summary(raw, self.segments)

    def test_summary_item_counts_are_bounded(self) -> None:
        raw = json.dumps({"chapters": [], "key_points": []})
        with self.assertRaisesRegex(probe.ProbeError, "chapters must contain between 3 and 6"):
            probe.validate_summary(raw, self.segments)

    def test_each_summary_item_has_exactly_one_citation(self) -> None:
        evidence = {"segment_id": 0}
        document = {
            "chapters": [{"title": f"章节 {index}", "evidence": [evidence, evidence]} for index in range(3)],
            "key_points": [{"text": f"要点 {index}", "evidence": [evidence]} for index in range(3)],
        }
        with self.assertRaisesRegex(probe.ProbeError, "text and evidence are required"):
            probe.validate_summary(json.dumps(document, ensure_ascii=False), self.segments)


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
            "LLM_ENABLE_THINKING": "false",
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

    def test_default_output_reservation_is_3072_tokens(self) -> None:
        self.assertEqual(probe.settings_from(self.complete()).max_output_tokens, 3072)

    def test_thinking_mode_is_explicit_and_validated(self) -> None:
        settings = probe.settings_from(self.complete())
        self.assertFalse(settings.llm_enable_thinking)
        values = self.complete()
        values["LLM_ENABLE_THINKING"] = "true"
        self.assertTrue(probe.settings_from(values).llm_enable_thinking)
        values["LLM_ENABLE_THINKING"] = "sometimes"
        with self.assertRaisesRegex(probe.ProbeError, "must be true or false"):
            probe.settings_from(values)

    def test_llm_request_sends_configured_thinking_mode(self) -> None:
        settings = probe.settings_from(self.complete())

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.assertFalse(body["enable_thinking"])
            self.assertEqual(
                body["response_format"],
                {"type": "json_schema", "json_schema": probe.SUMMARY_RESPONSE_JSON_SCHEMA},
            )
            return httpx.Response(
                200,
                json={
                    "model": settings.llm_model,
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        async def run() -> None:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                content, _ = await probe.llm_request(client, settings, "prompt")
                self.assertEqual(content, "{}")

        asyncio.run(run())

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

    def test_legacy_video_byte_setting_is_ignored(self) -> None:
        values = self.complete()
        values["MAX_VIDEO_BYTES"] = "1"
        settings = probe.settings_from(values)
        self.assertEqual(settings.max_video_seconds, 1200)


class BoundedExecutionTests(unittest.TestCase):
    def test_truncated_llm_responses_keep_usage_and_respect_the_three_call_cap(self) -> None:
        settings = probe.settings_from(ConfigurationTests().complete())
        request_count = 0

        async def handler(_: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                json={
                    "model": settings.llm_model,
                    "choices": [{"message": {"content": "{}"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
                },
            )

        async def run() -> list[dict[str, object]]:
            history: list[dict[str, object]] = []
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                with self.assertRaisesRegex(probe.ProbeError, "after 3 requests"):
                    await probe.summarize(
                        client,
                        settings,
                        [probe.Segment(0, 0, 30, 0, 30, "course text")],
                        llm_results_sink=history,
                    )
            return history

        history = asyncio.run(run())
        self.assertEqual(request_count, 3)
        self.assertEqual(len(history), 3)
        expected_usage = {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}
        self.assertTrue(all(item["usage"] == expected_usage for item in history))
        self.assertTrue(all(item["validation_error_code"] == "TRUNCATED_LLM_RESPONSE" for item in history))

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
        results_sink: list[dict[str, object]] = []

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                with patch.object(probe, "llm_request", fake):
                    with self.assertRaisesRegex(probe.ProbeError, "after 3 requests"):
                        await probe.summarize(
                            client,
                            settings,
                            [probe.Segment(0, 0, 30, 0, 30, "text")],
                            llm_results_sink=results_sink,
                        )

        asyncio.run(run())
        self.assertEqual(fake.await_count, 3)
        self.assertEqual(len(results_sink), 3)
        self.assertTrue(all(item["validation_status"] == "failed" for item in results_sink))


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
