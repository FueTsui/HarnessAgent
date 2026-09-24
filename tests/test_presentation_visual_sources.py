import asyncio
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from backend.guardrail_policies import ContentBlocked, GuardedModelClient
from backend.model_governance import GovernedLLMClient
from backend.runtime import presentation_requirements as source
from test_presentation_coverage import _pdf, _slides


class Vision:
    provider_id = 4
    vision_model = "same-provider-model"
    calls = []

    async def vision(self, system, user, image_paths, temperature=.1):
        self.calls.append((system, user, image_paths))
        return "可见图表包含用户输入、模型推理、工具调用和对话历史，箭头按此顺序从左到右连接。"


def _visual_pdf(path):
    _pdf(path, 3)
    reader = PdfReader(str(path))
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    # 第二页在有足够文本的同时画出四条独立连接线，不能依赖低文本检测。
    page = writer.pages[1]
    content = DecodedStreamObject()
    content.set_data(page.get_contents().get_data() + b"\n" + b"\n".join(
        f"40 {100+i*20} m 500 {100+i*20} l S".encode() for i in range(4)
    ))
    page[NameObject("/Contents")] = writer._add_object(content)
    # 第三页只有图像与短页脚；图像本身有内容，不能被当成空白页。
    pixels = bytearray()
    for y in range(120):
        for x in range(160):
            value = 0 if (x // 10 + y // 10) % 2 else 255
            pixels.extend([value, value, value])
    image = DecodedStreamObject()
    image.set_data(bytes(pixels))
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(160), NameObject("/Height"): NumberObject(120),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page = writer.pages[2]
    page[NameObject("/Resources")][NameObject("/XObject")] = DictionaryObject({NameObject("/Content"): writer._add_object(image)})
    content = DecodedStreamObject()
    content.set_data(b"q 700 0 0 380 10 10 cm /Content Do Q\nBT /F1 10 Tf 20 20 Td (footer) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as stream:
        writer.write(stream)


class PresentationVisualSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.pdf"
        _visual_pdf(self.pdf)
        self.requirements = source.build_presentation_requirements("中文逐页重新创建这个ppt", [self.pdf])
        Vision.calls = []

    def test_detects_nonempty_vector_page_and_image_page_without_page_numbers(self):
        self.assertEqual(self.requirements.page_count, 3)
        self.assertEqual(self.requirements.pages[0].visual_reasons, ())
        self.assertIn("vector_diagram", self.requirements.pages[1].visual_reasons)
        self.assertIn("low_text", self.requirements.pages[2].visual_reasons)
        self.assertIn("content_image", self.requirements.pages[2].visual_reasons)
        self.assertTrue(any("图像识读证据" in issue for issue in self.requirements.validate_slides(_slides(3))))

    def test_same_provider_pin_removes_both_fallbacks_without_changing_original(self):
        first = Vision()
        fallback = Vision()
        fallback.provider_id = 5
        first.vision_fallback = fallback
        guarded = GuardedModelClient(first, user_id=1, agent_id=3, provider_id=4)
        governed = GovernedLLMClient([(4, "first", guarded), (5, "other", fallback)])
        guarded._guardrail_parent = governed
        pinned = source._same_provider_vision_client(governed)
        self.assertEqual(len(pinned._clients), 1)
        self.assertIsInstance(pinned._clients[0][2], GuardedModelClient)
        self.assertEqual(pinned._clients[0][2]._identity, guarded._identity)
        self.assertNotIn("_guardrail_parent", pinned._clients[0][2].__dict__)
        self.assertIsNone(pinned._clients[0][2]._guardrail_client.vision_fallback)
        self.assertEqual(len(governed._clients), 2)
        self.assertIs(first.vision_fallback, fallback)
        self.assertIs(guarded._guardrail_parent, governed)

    def test_failed_primary_vision_never_calls_another_provider(self):
        class Failing(Vision):
            async def vision(self, *args, **kwargs):
                raise RuntimeError("PRIVATE_ERROR_SENTINEL")

        fallback = Vision()
        fallback.provider_id = 5
        governed = GovernedLLMClient([(4, "first", Failing()), (5, "other", fallback)])
        with patch.object(source, "_source_worker", return_value={"ok": True}):
            result = asyncio.run(source.enrich_presentation_visual_sources(self.requirements, governed))
        self.assertEqual(Vision.calls, [])
        self.assertTrue(result.source_errors)
        self.assertNotIn("PRIVATE_ERROR_SENTINEL", str(result.source_errors))
        self.assertFalse(any(page.visual_verified for page in result.pages))

    def test_missing_vision_render_failure_empty_and_unclosed_think_are_hard_failures(self):
        with patch.object(source, "_source_worker") as renderer:
            result = asyncio.run(source.enrich_presentation_visual_sources(self.requirements, object()))
            renderer.assert_not_called()
        self.assertTrue(result.source_errors)
        for text in ("", "VISUAL_SOURCE_UNREADABLE", "<think>private reasoning without end"):
            class Empty(Vision):
                async def vision(self, *args, **kwargs):
                    return text
            with self.subTest(text=text), patch.object(source, "_source_worker", return_value={"ok": True}):
                result = asyncio.run(source.enrich_presentation_visual_sources(self.requirements, Empty()))
                self.assertTrue(result.source_errors)
                self.assertNotIn("private reasoning", result.context())
                self.assertFalse(any(page.visual_verified for page in result.pages))
        with patch.object(source, "_source_worker", side_effect=ImportError("renderer missing")):
            result = asyncio.run(source.enrich_presentation_visual_sources(self.requirements, Vision()))
        self.assertTrue(result.source_errors)

    def test_guardrail_block_is_propagated_and_public_event_never_contains_raw_content(self):
        class Blocked(Vision):
            async def vision(self, *args, **kwargs):
                raise ContentBlocked("model_output")
        with patch.object(source, "_source_worker", return_value={"ok": True}):
            with self.assertRaises(ContentBlocked):
                asyncio.run(source.enrich_presentation_visual_sources(self.requirements, Blocked()))
        events = []
        with patch.object(source, "_source_worker", return_value={"ok": True}):
            result = asyncio.run(source.enrich_presentation_visual_sources(
                self.requirements, Vision(), runtime_event=lambda name, data: events.append((name, data)),
            ))
        self.assertFalse(result.source_errors)
        self.assertTrue(all(page.visual_verified for page in result.pages if page.visual_reasons))
        self.assertCountEqual([row[1]["page"] for row in events], [2, 3])
        self.assertNotIn("用户输入", str(events))
        self.assertNotIn(str(self.root), str(events))
        self.assertIn("非可信来源数据", result.context())

    def test_two_concurrent_requests_and_global_character_limit(self):
        state = {"running": 0, "peak": 0}
        barrier = asyncio.Barrier(2)
        class Slow(Vision):
            async def vision(self, *args, **kwargs):
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
                try:
                    # 两次请求都进入视觉阶段后才释放，不依赖线程调度与短sleep重叠。
                    await asyncio.wait_for(barrier.wait(), timeout=5)
                finally:
                    state["running"] -= 1
                return "图中展示四个有序节点及其箭头关系。" * 5
        with patch.object(source, "_source_worker", return_value={"ok": True}), \
             patch.object(source, "MAX_VISUAL_CONTEXT_CHARS", 100):
            result = asyncio.run(source.enrich_presentation_visual_sources(self.requirements, Slow()))
        self.assertEqual(state["peak"], 2)
        self.assertTrue(result.source_errors)
        self.assertLessEqual(sum(len(page.visual_text) for page in result.pages), 100)

    def test_guardrail_cancellation_waits_for_render_before_removing_temp_directory(self):
        second_started = threading.Event()
        second_finished = threading.Event()
        paths = []
        def render(payload, timeout):
            if payload["source_page"] == 2:
                second_started.wait(1)
            else:
                second_started.set()
                time.sleep(.04)
                path = Path(payload["output"])
                path.write_bytes(b"rendered")
                paths.append(path)
                second_finished.set()
            return {"ok": True}
        class Blocked(Vision):
            async def vision(self, *args, **kwargs):
                raise ContentBlocked("model_output")
        with patch.object(source, "_source_worker", side_effect=render):
            with self.assertRaises(ContentBlocked):
                asyncio.run(source.enrich_presentation_visual_sources(self.requirements, Blocked()))
        self.assertTrue(second_finished.is_set())
        self.assertTrue(paths)
        self.assertFalse(paths[0].parent.exists())

    def test_real_worker_bounds_render_pixels_and_has_killable_timeout(self):
        output = self.root / "render.png"
        source._source_worker({"action": "render", "source_path": str(self.pdf), "source_page": 3, "output": str(output)}, 20)
        with Image.open(output) as image:
            self.assertLessEqual(max(image.size), source.MAX_RENDER_EDGE + 1)
            self.assertLessEqual(image.width * image.height, source.MAX_RENDER_EDGE ** 2)
        with self.assertRaises(subprocess.TimeoutExpired):
            source._source_worker({"action": "build", "objective": "中文逐页重制ppt", "documents": [str(self.pdf)]}, .001)

    def test_async_build_uses_bounded_worker_and_preserves_detector_state(self):
        result = asyncio.run(source.build_presentation_requirements_async("中文逐页重新创建这个ppt", [self.pdf]))
        self.assertEqual(result.page_count, 3)
        self.assertIn("vector_diagram", result.pages[1].visual_reasons)
        with patch.object(source, "_source_worker", side_effect=subprocess.TimeoutExpired("worker", 30)):
            result = asyncio.run(source.build_presentation_requirements_async("中文逐页重新创建这个ppt", [self.pdf]))
        self.assertTrue(result.source_errors)
        self.assertIn("30秒", result.source_errors[0])


if __name__ == "__main__":
    unittest.main()
