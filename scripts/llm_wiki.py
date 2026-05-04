#!/usr/bin/env python3
"""Small helper CLI for maintaining a local LLM wiki scaffold."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RAW_SOURCES = ROOT / "raw" / "sources"
WIKI_ROOT = ROOT / "wiki"
DOMAINS_ROOT = WIKI_ROOT / "domains"
INDEX_PATH = ROOT / "wiki" / "index.md"
LOG_PATH = ROOT / "wiki" / "log.md"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled-source"


def today() -> str:
    return dt.date.today().isoformat()


def source_stub(title: str, raw_name: str, domain: str) -> str:
    return f"""---
tags: [source]
domain: {domain}
domain_confidence: manual
domain_reason: "Created from the helper CLI."
shared_scope: domain
source_paths: ["../../../../raw/sources/{raw_name}"]
status: staged
---

# {title}

## Summary

Pending ingest.

## Source Metadata

- Raw path: ../../../../raw/sources/{raw_name}
- Domain: {domain}
- Author:
- Published:
- Ingested: {today()}

## Key Points

- Pending ingest.

## Evidence / Notes

- Add notes during ingest.

## Related Entities

- None yet.

## Related Concepts

- None yet.

## Wiki Updates

- [overview](../overview.md)

## Open Questions

- What is the central contribution of this source?
"""


def ensure_domain(domain: str) -> Path:
    root = DOMAINS_ROOT / slugify(domain)
    for section in ["sources", "entities", "concepts", "queries"]:
        (root / section).mkdir(parents=True, exist_ok=True)
    overview = root / "overview.md"
    if not overview.exists():
        overview.write_text(
            f"---\ntags: [domain]\ndomain: {slugify(domain)}\nstatus: active\n---\n\n"
            f"# {domain.strip().title()}\n\n## Summary\n\nDomain workspace for {domain.strip().title()}.\n",
            encoding="utf-8",
        )
    return root


def init_source(title: str, domain: str) -> None:
    slug = slugify(title)
    domain_slug = slugify(domain)
    raw_path = RAW_SOURCES / f"{slug}.md"
    wiki_path = ensure_domain(domain_slug) / "sources" / f"{slug}.md"

    if not raw_path.exists():
        raw_path.write_text(f"# {title}\n\nAdd raw source material here.\n", encoding="utf-8")

    if not wiki_path.exists():
        wiki_path.write_text(source_stub(title, raw_path.name, domain_slug), encoding="utf-8")

    print(f"Raw source: {raw_path.relative_to(ROOT)}")
    print(f"Wiki page: {wiki_path.relative_to(ROOT)}")
    print("Next step: fill the raw source file, then ask the agent to ingest it.")


def add_log_entry(operation: str, title: str, summary: str, pages: list[str]) -> None:
    pages_text = ", ".join(f"[{Path(page).stem}]({page})" for page in pages) if pages else "None"
    entry = (
        f"\n## [{today()}] {operation} | {title}\n\n"
        f"- {summary}\n"
        f"- Pages touched: {pages_text}\n"
    )
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    print(f"Appended log entry to {LOG_PATH.relative_to(ROOT)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-source", help="Create a raw source stub and wiki source page")
    init_parser.add_argument("title", help="Human-readable source title")
    init_parser.add_argument("--domain", default="unclassified", help="Domain slug/title for the source stub")

    log_parser = subparsers.add_parser("add-log", help="Append an entry to wiki/log.md")
    log_parser.add_argument("operation", help="Operation name: ingest, query, lint, etc.")
    log_parser.add_argument("title", help="Short title for the log entry")
    log_parser.add_argument("summary", help="One-line summary of the change")
    log_parser.add_argument("--page", action="append", default=[], help="Relative wiki path touched by the operation")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "init-source":
        init_source(args.title, args.domain)
        return
    if args.command == "add-log":
        add_log_entry(args.operation, args.title, args.summary, args.page)
        return
    raise ValueError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    main()
