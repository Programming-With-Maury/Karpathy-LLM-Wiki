from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app
from benchmarks import core_benchmarks
from scripts import llm_wiki, notion_sync


class SlugAndParsingTests(unittest.TestCase):
    def test_slugify_normalizes_titles_and_falls_back(self) -> None:
        self.assertEqual(app.slugify("AI vs. Indian IT: what's next?"), "ai-vs-indian-it-what-s-next")
        self.assertEqual(app.slugify("!!!"), "untitled")
        self.assertEqual(llm_wiki.slugify("!!!"), "untitled-source")

    def test_vertex_json_accepts_plain_and_fenced_objects(self) -> None:
        self.assertEqual(app.parse_vertex_json_text('{"ok": true}')["ok"], True)
        fenced = "```json\n{\"answer\": \"yes\"}\n```"
        self.assertEqual(app.parse_vertex_json_text(fenced)["answer"], "yes")

    def test_notion_id_normalization_handles_common_forms(self) -> None:
        raw = "0123456789abcdef0123456789abcdef"
        expected = "01234567-89ab-cdef-0123-456789abcdef"
        self.assertEqual(notion_sync.normalize_notion_id(raw), expected)
        self.assertEqual(
            notion_sync.normalize_notion_id(f"https://www.notion.so/Page-Title-{raw}?v=ignored"),
            expected,
        )


class PathSafetyTests(unittest.TestCase):
    def test_upload_paths_are_sanitized_without_traversal(self) -> None:
        self.assertEqual(
            app.sanitize_relative_upload_path("../Private Notes/topic A.md", "fallback.md"),
            Path("Private-Notes/topic-A.md"),
        )
        self.assertEqual(
            app.sanitize_relative_upload_path("", "fallback name!.md"),
            Path("fallback-name-.md"),
        )

    def test_ingest_and_skip_filters_reject_hidden_files(self) -> None:
        self.assertTrue(app.should_ingest_path(Path("raw/sources/note.md")))
        self.assertFalse(app.should_ingest_path(Path("raw/sources/.secret.md")))
        self.assertTrue(app.should_skip_upload_path(Path("folder/.DS_Store")))

    def test_normalize_repo_path_stays_inside_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            target = base / "wiki" / "note.md"
            target.parent.mkdir()
            target.write_text("# Note", encoding="utf-8")

            self.assertEqual(app.normalize_repo_path(base, "wiki/note.md"), target.resolve())
            self.assertIsNone(app.normalize_repo_path(base, "../outside.md"))


class MarkdownRenderingTests(unittest.TestCase):
    def test_render_markdown_escapes_html_and_resolves_wiki_links(self) -> None:
        rendered = app.render_markdown(
            "# Title\n\nHello <script>bad()</script> and `code` with [Link](concepts/a.md).",
            "domains/demo",
            "wiki",
        )
        self.assertIn("<h1>Title</h1>", rendered)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt;", rendered)
        self.assertIn("<code>code</code>", rendered)
        self.assertIn('href="/page/domains/demo/concepts/a.md"', rendered)

    def test_internal_link_extraction_ignores_external_and_anchor_links(self) -> None:
        links = app.extract_internal_links(
            "[Local](concepts/a.md) [External](https://example.com/a.md) [Anchor](#part)"
        )
        self.assertEqual(links, ["concepts/a.md"])


class BenchmarkHarnessTests(unittest.TestCase):
    def test_benchmark_runner_reports_all_core_cases(self) -> None:
        results = core_benchmarks.run_benchmarks(iterations=1)
        names = {item["name"] for item in results}
        self.assertEqual(names, set(core_benchmarks.BENCHMARKS))
        for item in results:
            self.assertGreater(item["mean_seconds"], 0)
            self.assertEqual(item["iterations"], 1)

    def test_benchmark_cli_json_output_is_valid(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "benchmarks" / "core_benchmarks.py"), "--iterations", "1", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(proc.stdout)
        self.assertEqual({item["name"] for item in payload}, set(core_benchmarks.BENCHMARKS))


if __name__ == "__main__":
    unittest.main()
