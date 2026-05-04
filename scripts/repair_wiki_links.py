#!/usr/bin/env python3
"""Repair relative markdown links after structural wiki moves."""

from __future__ import annotations

import os
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)(#[^)]+)?\)")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def rel_link(from_path: Path, to_path: Path) -> str:
    return os.path.relpath(to_path, from_path.parent).replace(os.sep, "/")


def candidate_score(source: Path, candidate: Path) -> tuple[int, str]:
    source_rel = source.relative_to(WIKI).as_posix()
    candidate_rel = candidate.relative_to(WIKI).as_posix()
    source_parts = source_rel.split("/")
    candidate_parts = candidate_rel.split("/")
    score = 0
    if "archive/" in candidate_rel:
        score -= 30
    if "templates/" in candidate_rel:
        score -= 100
    if candidate.name == "README.md":
        score -= 40
    if source_parts[:2] == candidate_parts[:2] and source_parts[:1] == ["domains"]:
        score += 50
    if candidate_rel.startswith("global/"):
        score += 20
    if candidate_rel.startswith("staging/"):
        score += 10
    if candidate_rel.startswith("domains/"):
        score += 15
    return (-score, candidate_rel)


def build_filename_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for path in WIKI.rglob("*.md"):
        if "templates" in path.parts:
            continue
        index.setdefault(path.name, []).append(path)
    return index


def repair() -> int:
    filename_index = build_filename_index()
    changed = 0
    for path in WIKI.rglob("*.md"):
        if "templates" in path.parts or "archive" in path.parts:
            continue
        text = read_text(path)
        if not text:
            continue
        current_rel = path.relative_to(WIKI).as_posix()
        current_dir = Path(current_rel).parent

        def replace(match: re.Match[str]) -> str:
            label, target, anchor = match.group(1), match.group(2), match.group(3) or ""
            if "://" in target or target.startswith("#"):
                return match.group(0)
            resolved = (WIKI / os.path.normpath((current_dir / target).as_posix())).resolve()
            if resolved.exists():
                return match.group(0)
            candidates = filename_index.get(Path(target).name, [])
            if not candidates:
                return label
            best = sorted(candidates, key=lambda candidate: candidate_score(path, candidate))[0]
            return f"[{label}]({rel_link(path, best)}{anchor})"

        updated = LINK_RE.sub(replace, text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


if __name__ == "__main__":
    print(f"Repaired links in {repair()} page(s).")
