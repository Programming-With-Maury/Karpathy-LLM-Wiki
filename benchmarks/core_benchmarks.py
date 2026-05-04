#!/usr/bin/env python3
"""Small benchmark harness for core LLM Wiki helpers.

The goal is not to produce lab-grade performance numbers; it gives maintainers
a repeatable smoke benchmark for the hot utility paths used by the local app.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def _markdown_fixture() -> str:
    block = """# Domain Note

This page links to [a concept](concepts/agentic-workflows.md), keeps `inline code`,
and escapes <unsafe html>.

- One point
- Another point with [external](https://example.com/path?q=1)

```python
print("hello")
```
"""
    return "\n".join(block for _ in range(80))


def _path_fixture() -> list[tuple[str, str]]:
    return [
        ("../private note.md", "fallback.md"),
        ("topic A/nested file.txt", "fallback.md"),
        (".hidden/.env", "upload.md"),
        ("", "spaces and symbols !.md"),
    ] * 50


def bench_slugify() -> None:
    for value in (
        "ExplainGitHub Enterprise Roadmap",
        "  AI vs. Indian IT: what's next? ",
        "!!!",
        "Boansel Creator Dashboard",
    ) * 100:
        app.slugify(value)


def bench_render_markdown() -> None:
    app.render_markdown(_markdown_fixture(), "domains/ai-strategy", "wiki")


def bench_upload_path_sanitization() -> None:
    for raw_path, fallback in _path_fixture():
        app.sanitize_relative_upload_path(raw_path, fallback)


BENCHMARKS: dict[str, Callable[[], None]] = {
    "slugify": bench_slugify,
    "render_markdown": bench_render_markdown,
    "upload_path_sanitization": bench_upload_path_sanitization,
}


def run_benchmarks(*, iterations: int = 5) -> list[dict[str, float | int | str]]:
    results: list[dict[str, float | int | str]] = []
    for name, func in BENCHMARKS.items():
        samples: list[float] = []
        for _ in range(iterations):
            start = time.perf_counter()
            func()
            samples.append(time.perf_counter() - start)
        mean_seconds = statistics.fmean(samples)
        results.append(
            {
                "name": name,
                "iterations": iterations,
                "mean_seconds": mean_seconds,
                "min_seconds": min(samples),
                "max_seconds": max(samples),
                "runs_per_second": 1 / mean_seconds if mean_seconds else 0,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    results = run_benchmarks(iterations=max(1, args.iterations))
    if args.json:
        print(json.dumps(results, indent=2))
        return
    for result in results:
        print(
            f"{result['name']}: {result['mean_seconds']:.6f}s avg "
            f"({result['runs_per_second']:.1f} runs/s, n={result['iterations']})"
        )


if __name__ == "__main__":
    main()
