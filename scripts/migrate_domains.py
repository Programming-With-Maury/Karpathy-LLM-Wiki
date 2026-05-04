#!/usr/bin/env python3
"""Migrate the flat generated wiki into domain-aware folders."""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
ARCHIVE = WIKI / "archive" / "domain-migration"


DOMAIN_RULES = {
    "boansel": [
        "boansel",
        "topmate",
        "creator",
        "marketplace",
        "booking",
        "razorpay",
        "whatsapp",
        "membership",
        "service-card",
        "service-details",
    ],
    "explaingithub": [
        "explaingithub",
        "explain-github",
        "devshield",
        "github",
        "repository",
        "repo",
        "pull request",
        "pr-impact",
        "diffing",
        "codebase",
        "browser-extension",
        "gitdiagram",
    ],
    "ai-strategy": [
        "rag",
        "openai",
        "anthropic",
        "agent",
        "alignment",
        "llm",
        "token",
        "ai-",
        "foundation-model",
        "indian-it",
        "atlassian",
        "jensen",
        "sarvam",
        "neysa",
        "qwen",
        "llamaindex",
        "ragflow",
    ],
}


DOMAIN_TITLES = {
    "boansel": "Boansel",
    "explaingithub": "ExplainGitHub",
    "ai-strategy": "AI Strategy",
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def title_for(path: Path) -> str:
    match = re.search(r"^#\s+(.+)$", read_text(path), flags=re.MULTILINE)
    return match.group(1).strip() if match else path.stem.replace("-", " ").title()


def summary_for(path: Path) -> str:
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", "-", "---", "tags:", "domain:", "status:", "source_paths:", "shared_scope:")):
            return stripped[:180]
    return "Domain page migrated from the previous flat wiki."


def classify(path: Path) -> str:
    haystack = f"{path.as_posix()} {title_for(path)} {read_text(path)[:3000]}".lower()
    scores = {
        domain: sum(haystack.count(keyword) for keyword in keywords)
        for domain, keywords in DOMAIN_RULES.items()
    }
    best, score = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0]
    return best if score > 0 else "ai-strategy"


def count_sources(text: str) -> int:
    in_sources = False
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_sources = stripped == "## Sources"
            continue
        if in_sources and stripped.startswith("- "):
            count += 1
    return count


def is_weak_generated_page(path: Path) -> bool:
    text = read_text(path)
    words = re.findall(r"[A-Za-z0-9]+", text)
    return path.parent.name in {"entities", "concepts"} and len(words) < 90 and count_sources(text) <= 1


def ensure_domain(domain: str) -> None:
    root = WIKI / "domains" / domain
    for child in ["sources", "entities", "concepts", "queries"]:
        (root / child).mkdir(parents=True, exist_ok=True)
        readme = root / child / "README.md"
        if not readme.exists():
            readme.write_text(f"# {DOMAIN_TITLES[domain]} {child.title()}\n\nMigrated {child} for this domain.\n", encoding="utf-8")
    overview = root / "overview.md"
    if not overview.exists():
        overview.write_text(
            f"---\ntags: [domain]\ndomain: {domain}\nstatus: active\n---\n\n"
            f"# {DOMAIN_TITLES[domain]}\n\n"
            f"## Summary\n\nDomain workspace for {DOMAIN_TITLES[domain]}.\n\n"
            "## Key Points\n\n- Migrated from the previous flat wiki.\n\n"
            "## Evidence / Notes\n\n- Needs domain-level synthesis after migration.\n\n"
            "## Links\n\n- [Sources](sources/README.md)\n- [Entities](entities/README.md)\n- [Concepts](concepts/README.md)\n\n"
            "## Open Questions\n\n- Which migrated pages should be merged or archived next?\n",
            encoding="utf-8",
        )


def add_frontmatter(text: str, *, domain: str, status: str, scope: str = "domain") -> str:
    if text.startswith("---\n"):
        return text
    return (
        "---\n"
        f"domain: {domain}\n"
        f"shared_scope: {scope}\n"
        "source_paths: []\n"
        f"status: {status}\n"
        "---\n\n"
        + text
    )


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def rel_link(from_rel: str, to_rel: str) -> str:
    return os.path.relpath(WIKI / to_rel, (WIKI / from_rel).parent).replace(os.sep, "/")


def migrate() -> tuple[dict[str, str], list[str]]:
    for domain in DOMAIN_TITLES:
        ensure_domain(domain)
    (WIKI / "global" / "entities").mkdir(parents=True, exist_ok=True)
    (WIKI / "global" / "concepts").mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(parents=True, exist_ok=True)

    mapping: dict[str, str] = {}
    archived: list[str] = []
    for section in ["sources", "entities", "concepts", "queries"]:
        section_root = WIKI / section
        if not section_root.exists():
            continue
        for path in sorted(section_root.glob("*.md")):
            if path.name == "README.md":
                continue
            old_rel = path.relative_to(WIKI).as_posix()
            domain = classify(path)
            if is_weak_generated_page(path):
                target = unique_target(ARCHIVE / section / path.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(add_frontmatter(read_text(path), domain=domain, status="archived"), encoding="utf-8")
                path.unlink()
                archive_rel = target.relative_to(WIKI).as_posix()
                mapping[old_rel] = archive_rel
                archived.append(archive_rel)
                continue
            target = unique_target(WIKI / "domains" / domain / section / path.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(add_frontmatter(read_text(path), domain=domain, status="active"), encoding="utf-8")
            path.unlink()
            mapping[old_rel] = target.relative_to(WIKI).as_posix()
    return mapping, archived


def rewrite_links(mapping: dict[str, str]) -> None:
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)(#[^)]+)?\)")
    for path in sorted(WIKI.rglob("*.md")):
        text = read_text(path)
        if not text:
            continue
        current_rel = path.relative_to(WIKI).as_posix()
        current_dir = Path(current_rel).parent

        def replace(match: re.Match[str]) -> str:
            label, target, anchor = match.group(1), match.group(2), match.group(3) or ""
            if "://" in target:
                return match.group(0)
            resolved = os.path.normpath((current_dir / target).as_posix())
            if resolved not in mapping:
                return match.group(0)
            return f"[{label}]({rel_link(current_rel, mapping[resolved])}{anchor})"

        updated = link_re.sub(replace, text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def rebuild_root_pages(mapping: dict[str, str], archived: list[str]) -> None:
    domain_rows = []
    for domain, title in DOMAIN_TITLES.items():
        pages = list((WIKI / "domains" / domain).rglob("*.md"))
        sources = len([p for p in pages if "/sources/" in p.as_posix()])
        entities = len([p for p in pages if "/entities/" in p.as_posix()])
        concepts = len([p for p in pages if "/concepts/" in p.as_posix()])
        overview = WIKI / "domains" / domain / "overview.md"
        summary = summary_for(overview)
        domain_rows.append(f"- [domains/{domain}/overview.md](domains/{domain}/overview.md) - {summary} ({sources} sources, {entities} entities, {concepts} concepts)")

    (WIKI / "overview.md").write_text(
        "---\ntags: [overview]\nstatus: active\n---\n\n"
        "# Overview\n\n"
        "This wiki is organized as a domain-aware Karpathy LLM Wiki. Raw uploads remain in `raw/`, while durable synthesis is compiled into domain folders under `wiki/domains/`.\n\n"
        "## Domains\n\n"
        + "\n".join(f"- [{DOMAIN_TITLES[d]}](domains/{d}/overview.md): {summary_for(WIKI / 'domains' / d / 'overview.md')}" for d in DOMAIN_TITLES)
        + "\n\n## Global Knowledge\n\n- [Global concepts](global/concepts/README.md) are reserved for concepts reused across multiple domains.\n- [Global entities](global/entities/README.md) are reserved for entities reused across multiple domains.\n\n"
        "## Migration Notes\n\n"
        f"- Migrated {len(mapping)} flat generated pages on {dt.date.today().isoformat()}.\n"
        f"- Archived {len(archived)} weak generated pages under `archive/domain-migration/`.\n",
        encoding="utf-8",
    )
    (WIKI / "index.md").write_text(
        "# Index\n\n"
        "This index is rebuilt from the current domain-aware wiki state.\n\n"
        "## Overview\n\n- [overview.md](overview.md) - Compact map of all active domains.\n\n"
        "## Domains\n\n"
        + "\n".join(domain_rows)
        + "\n\n## Global Concepts\n\n- None yet.\n\n## Global Entities\n\n- None yet.\n\n## Recent Queries\n\n- See each domain's `queries/` folder.\n\n## Staging\n\n- [staging/domain-review/README.md](staging/domain-review/README.md) - Sources awaiting domain routing.\n",
        encoding="utf-8",
    )
    log = WIKI / "log.md"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n## [{dt.date.today().isoformat()}] migration | Domain-aware wiki migration\n\n"
            f"- Migrated {len(mapping)} flat generated pages into domain folders and archived {len(archived)} weak pages.\n"
            "- Pages touched: [index](index.md), [overview](overview.md)\n"
        )
    rev = WIKI / "revisions" / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-domain-migration.json"
    rev.parent.mkdir(parents=True, exist_ok=True)
    rev.write_text(
        "{\n"
        f'  "id": "{rev.stem}",\n'
        f'  "created_at": "{dt.datetime.now().isoformat(timespec="seconds")}",\n'
        '  "type": "domain-migration",\n'
        f'  "migrated_pages": {len(mapping)},\n'
        f'  "archived_pages": {len(archived)},\n'
        '  "domains": ["boansel", "explaingithub", "ai-strategy"]\n'
        "}\n",
        encoding="utf-8",
    )


def main() -> None:
    mapping, archived = migrate()
    rewrite_links(mapping)
    rebuild_root_pages(mapping, archived)
    print(f"Migrated {len(mapping)} pages; archived {len(archived)} weak pages.")


if __name__ == "__main__":
    main()
