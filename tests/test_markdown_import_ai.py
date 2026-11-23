import json
from pathlib import Path
import tempfile
import unittest

import httpx

from markdown_import.ai import (
    LLMProviderError,
    LLMRequest,
    LLMResult,
    StratifiedDigestBuilder,
    generate_metadata_with_fallback,
)
from markdown_import.ai.httpx_provider import HttpxProvider
from tools.markdown_import import scan_markdown_source
from tools.markdown_import.scanner import list_root_markdown_files


class _SequenceMockLLM:
    """模拟降级阶梯：按设定序列抛出异常或返回结果，并记录每次摘要预算长度。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen_lengths = []

    def generate(self, request: LLMRequest) -> LLMResult:
        self.seen_lengths.append(len(request.digest_text))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class MarkdownImportAIAndScannerTests(unittest.TestCase):
    def test_stratified_digest_handles_120k_chars_with_mid_recall_and_redaction(self):
        # 构造 >120,000 中文/英文混合长文，中间第 60,000 字埋入唯一核心章，附带密钥与 Base64 噪声
        filler_para = ("分布式架构与数据库写入脱水机制在多副本场景下需要保持幂等与锁顺序一致性。" * 35) + "\n\n"
        big_code = "```python\n" + "\n".join(f"def step_{i}(): pass" for i in range(60)) + "\n```\n\n"
        big_table = "| 列A | 列B |\n| --- | --- |\n" + "\n".join(f"| v{i} | w{i} |" for i in range(25)) + "\n\n"
        secret_noise = "注意不要泄露测试密钥 sk-supersecretkey1234567890 和 data:image/png;base64," + ("A" * 120) + "\n\n"

        sections = [
            "## 开篇背景\n\n这是开篇背景，阐述为什么我们需要多页面通用 Markdown 导入框架。\n\n" + (filler_para * 26),
            "## 第一章 存储拓扑\n\n" + big_code + big_table + (filler_para * 26),
            "## 核心中段：RustFS 分段容灾密钥\n\n中段核心唯一口令词是 MIDSECTION_QUANTUM_RECOVERY_TOKEN，用于验证中段抽样绝不遗漏。\n\n" + secret_noise + (filler_para * 26),
            "## 第三章 性能压测\n\n" + (filler_para * 26),
            "## 结尾总结\n\n综上所述，本方案能够在 10 万字规模下保持极轻 PostgreSQL 主表并安全降级。\n\n" + secret_noise,
        ]
        long_doc = "\n".join(sections)
        self.assertGreater(len(long_doc), 120_000)

        digest = StratifiedDigestBuilder().build(long_doc, budget_chars=16_000)
        self.assertLessEqual(len(digest.text), 16_000)
        self.assertIn("核心中段：RustFS 分段容灾密钥", digest.text)
        self.assertIn("MIDSECTION_QUANTUM_RECOVERY_TOKEN", digest.text)
        self.assertIn("结尾总结", digest.text)
        self.assertNotIn("sk-supersecretkey1234567890", digest.text)
        self.assertIn("[REDACTED]", digest.text)
        self.assertIn("[BASE64_DATA_OMITTED]", digest.text)

    def test_httpx_provider_enforces_store_false_and_maps_errors(self):
        seen_payloads = []

        def ok_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            seen_payloads.append((str(request.url), body))
            if str(request.url).endswith("/responses"):
                return httpx.Response(
                    200,
                    json={
                        "output_text": json.dumps(
                            {
                                "title": "PostgreSQL 脱水实战",
                                "summary": "本文深入剖析了 PostgreSQL 与 MongoDB 联合脱水架构与安全回收。",
                                "tags": ["PostgreSQL", "Wagtail", "MongoDB"],
                            },
                            ensure_ascii=False,
                        )
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "title": "Chat 模式标题",
                                        "summary": "Chat 兼容接口生成的导读说明文本内容。",
                                        "tags": ["Django", "Python", "Redis"],
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

        p_resp = HttpxProvider(
            api_key="test-key",
            base_url="https://llm.example.invalid/v1",
            model="gpt-test",
            mode="responses",
            transport=httpx.MockTransport(ok_handler),
        )
        res1 = p_resp.generate(LLMRequest(digest_text="摘要", instructions="指令", json_schema={"type": "object"}))
        self.assertEqual(res1.parsed_json["title"], "PostgreSQL 脱水实战")
        self.assertIs(seen_payloads[0][1]["store"], False)

        p_chat = HttpxProvider(
            api_key="test-key",
            base_url="https://llm.example.invalid/v1",
            model="gpt-test",
            mode="chat_completions",
            transport=httpx.MockTransport(ok_handler),
        )
        res2 = p_chat.generate(LLMRequest(digest_text="摘要", instructions="指令", json_schema={"type": "object"}))
        self.assertEqual(res2.parsed_json["title"], "Chat 模式标题")
        self.assertIs(seen_payloads[1][1]["store"], False)

        for status_code, body_txt, expected_cat in (
            (400, '{"error": "context_length_exceeded"}', "context_limit"),
            (413, "Payload Too Large", "payload_limit"),
            (429, "Rate limited", "rate_limit"),
            (400, '{"error": "store parameter not supported"}', "privacy_unsupported"),
        ):
            def err_handler(_req: httpx.Request, sc=status_code, bt=body_txt) -> httpx.Response:
                return httpx.Response(sc, text=bt)

            p_err = HttpxProvider(
                api_key="test-key",
                base_url="https://llm.example.invalid/v1",
                model="gpt-test",
                transport=httpx.MockTransport(err_handler),
            )
            with self.assertRaises(LLMProviderError) as ctx:
                p_err.generate(LLMRequest(digest_text="摘要", instructions="指令", json_schema={"type": "object"}))
            self.assertEqual(ctx.exception.category, expected_cat)

    def test_service_degradation_ladder_and_local_fallback_never_fails(self):
        long_md = ("## 第一节\n\nPostgreSQL 与 Wagtail 架构讲解正文。" * 600)

        # 场景 A：首次 400 context_limit -> 自动缩容至 6000 字符重试成功
        llm_retry = _SequenceMockLLM(
            [
                LLMProviderError(category="context_limit", message="too long", status_code=400, is_retriable=True),
                LLMResult(
                    raw_text="{}",
                    parsed_json={
                        "title": "AI 自动生成标题",
                        "summary": "压缩摘要后成功生成的百字内容导读说明。",
                        "tags": ["PostgreSQL", "技术", "wagtail"],
                    },
                ),
            ]
        )
        sug = generate_metadata_with_fallback(
            long_md,
            source_name="01-note.md",
            allow_external_ai=True,
            llm_client=llm_retry,
            vocabulary_tags=["Wagtail", "PostgreSQL", "Django"],
        )
        self.assertEqual(sug.source, "ai")
        self.assertEqual(sug.title, "AI 自动生成标题")
        # 泛词 "技术" 被过滤，"wagtail" 自动对齐站内 "Wagtail"
        self.assertEqual(sug.tags, ["PostgreSQL", "Wagtail"])
        self.assertEqual(len(llm_retry.seen_lengths), 2)
        self.assertLess(llm_retry.seen_lengths[1], llm_retry.seen_lengths[0])

        # 场景 B：遭遇 429 限流 -> 不反复重冲，直接切换本地确定性兜底
        llm_429 = _SequenceMockLLM([LLMProviderError(category="rate_limit", message="429", status_code=429)])
        sug_local = generate_metadata_with_fallback(
            long_md,
            source_name="2026-11-22-02-postgres_tips.md",
            allow_external_ai=True,
            llm_client=llm_429,
            vocabulary_tags=["Wagtail", "PostgreSQL"],
        )
        self.assertEqual(sug_local.source, "local")
        self.assertEqual(sug_local.title, "postgres tips")
        self.assertIn("PostgreSQL", sug_local.tags)
        self.assertEqual(len(llm_429.seen_lengths), 1)

    def test_local_scanner_reads_folder_and_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            img_dir = root / "images"
            img_dir.mkdir()
            sub_dir = root / "notes"
            sub_dir.mkdir()
            (sub_dir / "ignored.md").write_text("# 子目录不扫描", encoding="utf-8")

            img_file = img_dir / "arch.png"
            img_file.write_bytes(b"PNGDATA123")

            outside = root.parent / f"outside_{root.name}.txt"
            outside.write_text("secret", encoding="utf-8")
            try:
                md_file = root / "demo.md"
                md_file.write_text(
                    "# 本地测试文章\n\n![图](images/arch.png)\n![缺失](images/missing.png)\n[越界](../outside.txt)\n",
                    encoding="utf-8",
                )
                scanned_root, top_mds = list_root_markdown_files(root)
                self.assertEqual(scanned_root, root.resolve())
                self.assertEqual(top_mds, [md_file.resolve()])

                scanned = scan_markdown_source(md_file, root_dir=root)
                self.assertEqual(len(scanned.assets), 1)
                self.assertEqual(scanned.assets[0].source_path, "images/arch.png")
                self.assertEqual(scanned.assets[0].size_bytes, 10)
                self.assertEqual(scanned.missing_assets, ("images/missing.png",))
                self.assertEqual(len(scanned.blocked_paths), 1)
            finally:
                if outside.exists():
                    outside.unlink()

    def test_openai_sdk_provider_rejects_store_true_and_passes_store_false(self):
        from types import SimpleNamespace
        from markdown_import.ai.openai_provider import OpenAISDKProvider

        with self.assertRaises(LLMProviderError) as ctx:
            OpenAISDKProvider(
                api_key="k",
                base_url="https://llm.example.invalid/v1",
                model="gpt-test",
                allow_store_false=False,
            )
        self.assertEqual(ctx.exception.category, "privacy_unsupported")

        captured = {}

        class _FakeResponses:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    output_text=json.dumps(
                        {"title": "SDK标题", "summary": "SDK生成的导读", "tags": ["Django", "PostgreSQL"]},
                        ensure_ascii=False,
                    )
                )

        provider = OpenAISDKProvider(
            api_key="k",
            base_url="https://llm.example.invalid/v1",
            model="gpt-test",
            mode="responses",
        )
        provider._client = SimpleNamespace(responses=_FakeResponses())
        res = provider.generate(LLMRequest(digest_text="摘要", instructions="指令", json_schema={"type": "object"}))
        self.assertIs(captured["store"], False)
        self.assertEqual(res.parsed_json["title"], "SDK标题")
