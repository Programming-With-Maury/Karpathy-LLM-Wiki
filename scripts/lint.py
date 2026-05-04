#!/usr/bin/env python3
"""
LLM Wiki Linter

Checks for broken relative links, orphaned markdown files, legacy flat pages,
thin generated pages, and duplicate concept/entity titles within wiki/.

Usage:
  python3 scripts/lint.py
"""

import os
import re
import sys

WIKI_DIR = sys.argv[1] if len(sys.argv) > 1 else 'wiki'
IGNORE_ORPHANS = ['index.md', 'overview.md', 'log.md', 'README.md']

def find_markdown_files(directory):
    md_files = []
    for root, _, files in os.walk(directory):
        normalized_root = root.replace(os.sep, "/")
        if "/archive/" in normalized_root or normalized_root.endswith("/archive") or "/templates" in normalized_root:
            continue
        for file in files:
            if file.endswith('.md'):
                md_files.append(os.path.join(root, file))
    return md_files

def check_links_and_orphans():
    md_files = find_markdown_files(WIKI_DIR)
    
    # link_regex matches [text](relative_path.md)
    link_regex = re.compile(r'\[([^\]]+)\]\(([^)]+\.md)(?:#[^)]+)?\)')
    
    inbound_links_count = {f: 0 for f in md_files}
    broken_links = []

    for filepath in md_files:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        links = link_regex.findall(content)
        for text, link in links:
            # Handle relative paths
            dir_name = os.path.dirname(filepath)
            target_path = os.path.normpath(os.path.join(dir_name, link))
            
            if not os.path.exists(target_path):
                broken_links.append((filepath, link, target_path))
            else:
                if target_path in inbound_links_count:
                    inbound_links_count[target_path] += 1

    print("--- Broken Links ---")
    if not broken_links:
        print("None found!")
    else:
        for src, link, target in broken_links:
            print(f"{src}: Broken link to '{link}' (resolved to {target})")
            
    print("\n--- Orphaned Files ---")
    orphans = []
    for filepath, count in inbound_links_count.items():
        basename = os.path.basename(filepath)
        # We don't consider files in templates or archive as orphans strictly, but let's check
        normalized = filepath.replace(os.sep, "/")
        if count == 0 and basename not in IGNORE_ORPHANS and 'templates/' not in normalized and 'archive/' not in normalized:
            orphans.append(filepath)
            
    if not orphans:
        print("None found!")
    else:
        for orphan in orphans:
            print(f"Orphaned file: {orphan}")

    print("\n--- Old Flat Pages ---")
    flat_pages = [
        path for path in md_files
        if path.startswith((f"{WIKI_DIR}/sources/", f"{WIKI_DIR}/entities/", f"{WIKI_DIR}/concepts/"))
        and not path.endswith("/README.md")
    ]
    if not flat_pages:
        print("None found!")
    else:
        for path in flat_pages:
            print(f"Legacy flat page: {path}")

    print("\n--- Low-Value Generated Pages ---")
    low_value = []
    title_map = {}
    for filepath in md_files:
        normalized = filepath.replace(os.sep, "/")
        if "/archive/" in normalized or filepath.endswith("README.md") or not any(part in normalized for part in ["/entities/", "/concepts/"]):
            continue
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        words = re.findall(r"[A-Za-z0-9]+", content)
        source_count = 0
        in_sources = False
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                in_sources = stripped == "## Sources"
            elif in_sources and stripped.startswith("- "):
                source_count += 1
        if len(words) < 90 and source_count <= 1:
            low_value.append(filepath)
        title_match = re.search(r"^#\s+(.+)$", content, flags=re.MULTILINE)
        title = title_match.group(1).strip().lower() if title_match else os.path.splitext(os.path.basename(filepath))[0]
        title_map.setdefault(re.sub(r"[^a-z0-9]+", "-", title).strip("-"), []).append(filepath)
    if not low_value:
        print("None found!")
    else:
        for path in low_value:
            print(f"Low-value page: {path}")

    print("\n--- Duplicate Entity/Concept Titles ---")
    duplicates = {title: paths for title, paths in title_map.items() if len(paths) > 1}
    if not duplicates:
        print("None found!")
    else:
        for title, paths in sorted(duplicates.items()):
            print(f"{title}: {', '.join(paths)}")

if __name__ == '__main__':
    check_links_and_orphans()
