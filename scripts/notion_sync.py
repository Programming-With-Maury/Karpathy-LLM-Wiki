#!/usr/bin/env python3
"""
Incremental Notion → markdown sync into raw/sources/notion/pages/.

- Uses Notion API with a per-database high-water mark (max last_edited_time seen)
  minus overlap for delta queries; per-page last_edited_time skips redundant work.
- State: <workspace-root>/.notion-sync-state.json (gitignored).
- Config: repo-root/notion-sync.config.json (copy from notion-sync.config.example.json).

Environment: NOTION_TOKEN in .env or env (see repo .env).

After a successful sync, by default loads app.py and runs enqueue_notion_ingest_batch so the
same ingest + Batches UI flow runs as for uploads (requires Vertex config in .env). Use
--no-ingest or "queue_ingest": false to only update raw/.

If pages are already in raw/ but never ingested (e.g. you used --no-ingest, ingest failed, or a run
skipped unchanged files so nothing was queued), use --ingest-raw-only to enqueue all
raw/sources/notion/pages/*.md without calling Notion again.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

import certifi
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DEFAULT_WORKSPACE = "default"
WORKSPACES_ROOT = ROOT / "workspaces"
NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
CONFIG_NAME = "notion-sync.config.json"
EXAMPLE_CONFIG = ROOT / "notion-sync.config.example.json"


def workspace_root(name: str) -> Path:
    if name == DEFAULT_WORKSPACE:
        return ROOT
    return WORKSPACES_ROOT / name


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(
            f"Missing {path.name}. Copy {EXAMPLE_CONFIG.name} to {path.name} and fill in.",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def notion_token() -> str:
    dotenv = load_dotenv(ENV_PATH)
    token = os.environ.get("NOTION_TOKEN", dotenv.get("NOTION_TOKEN", "")).strip()
    if not token:
        print("Set NOTION_TOKEN in .env or environment.", file=sys.stderr)
        sys.exit(1)
    return token


def parse_iso(ts: str) -> datetime:
    # Notion returns e.g. 2024-01-15T12:30:00.000Z
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def format_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class SyncStats:
    pages_considered: int = 0
    pages_fetched: int = 0
    pages_skipped: int = 0
    errors: int = 0


class NotionClient:
    def __init__(self, token: str, *, dry_run: bool = False, verbose: bool = False) -> None:
        self.token = token
        self.dry_run = dry_run
        self.verbose = verbose
        # macOS / some Python builds ship without a usable CA store for urllib.
        self._ssl = ssl.create_default_context(cafile=certifi.where())

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        retries: int = 4,
    ) -> dict[str, Any] | list[Any] | None:
        url = f"{NOTION_BASE}{path}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120, context=self._ssl) as resp:
                    raw = resp.read().decode("utf-8")
                    if not raw:
                        return None
                    return json.loads(raw)
            except urllib.error.HTTPError as e:
                payload = e.read().decode("utf-8", errors="replace")
                if e.code == 429 and attempt < retries - 1:
                    wait = 2 ** attempt + 1
                    if self.verbose:
                        print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                last_err = RuntimeError(f"HTTP {e.code}: {payload}")
                break
            except urllib.error.URLError as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
        if last_err:
            raise last_err
        return None

    def get_page(self, page_id: str) -> dict[str, Any]:
        out = self._request("GET", f"/pages/{page_id}")
        assert isinstance(out, dict)
        return out

    def query_database(
        self,
        database_id: str,
        *,
        start_cursor: str | None = None,
        filter_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        if filter_body:
            body["filter"] = filter_body
        out = self._request("POST", f"/databases/{database_id}/query", body)
        assert isinstance(out, dict)
        return out

    def list_block_children(self, block_id: str, start_cursor: str | None = None) -> dict[str, Any]:
        q = f"?{urlencode({'start_cursor': start_cursor})}" if start_cursor else ""
        out = self._request("GET", f"/blocks/{block_id}/children{q}")
        assert isinstance(out, dict)
        return out


def page_last_edited(page: dict[str, Any]) -> str:
    return str(page.get("last_edited_time") or page.get("created_time") or "")


def page_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    for key, val in props.items():
        if not isinstance(val, dict):
            continue
        if val.get("type") == "title":
            return rich_text_plain(val.get("title") or [])
    return "Untitled"


def rich_text_plain(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(item.get("plain_text") or item.get("text", {}).get("content", ""))
        else:
            parts.append(item.get("plain_text", ""))
    return "".join(parts).strip()


def rich_text_to_markdown(items: list[dict[str, Any]]) -> str:
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = ""
        href: str | None = None
        if item.get("type") == "text":
            t = item.get("text", {})
            text = t.get("content", "")
            link = t.get("link")
            if isinstance(link, dict) and link.get("url"):
                href = str(link["url"])
        else:
            text = item.get("plain_text", "")
        ann = item.get("annotations") or {}
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        if ann.get("strikethrough"):
            text = f"~~{text}~~"
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def blocks_to_markdown(
    client: NotionClient,
    block_id: str,
    *,
    indent: int = 0,
    list_stack: list[str] | None = None,
) -> str:
    list_stack = list_stack or []
    lines: list[str] = []
    prefix = " " * indent
    cursor: str | None = None
    while True:
        chunk = client.list_block_children(block_id, cursor)
        results = chunk.get("results") or []
        if not isinstance(results, list):
            break
        for block in results:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if not btype:
                continue
            payload = block.get(btype) or {}
            bid = block.get("id", "")
            if btype == "paragraph":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                if t.strip():
                    lines.append(f"{prefix}{t}\n")
            elif btype in ("heading_1", "heading_2", "heading_3"):
                level = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}[btype]
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                lines.append(f"{prefix}{level}{t}\n\n")
            elif btype == "bulleted_list_item":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                lines.append(f"{prefix}- {t}\n")
                if block.get("has_children"):
                    lines.append(
                        blocks_to_markdown(client, bid, indent=indent + 2, list_stack=list_stack + ["bullet"])
                    )
            elif btype == "numbered_list_item":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                lines.append(f"{prefix}1. {t}\n")
                if block.get("has_children"):
                    lines.append(
                        blocks_to_markdown(client, bid, indent=indent + 2, list_stack=list_stack + ["num"])
                    )
            elif btype == "to_do":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                checked = payload.get("checked", False)
                mark = "[x]" if checked else "[ ]"
                lines.append(f"{prefix}- {mark} {t}\n")
                if block.get("has_children"):
                    lines.append(blocks_to_markdown(client, bid, indent=indent + 2, list_stack=list_stack))
            elif btype == "code":
                lang = (payload.get("language") or "").strip()
                code = rich_text_plain(payload.get("rich_text") or [])
                lines.append(f"{prefix}```{lang}\n{code}\n{prefix}```\n\n")
            elif btype == "quote":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                for ln in t.splitlines() or [t]:
                    lines.append(f"{prefix}> {ln}\n")
                lines.append("\n")
            elif btype == "divider":
                lines.append(f"{prefix}---\n\n")
            elif btype == "callout":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                lines.append(f"{prefix}> **Callout:** {t}\n")
                if block.get("has_children"):
                    lines.append(blocks_to_markdown(client, bid, indent=indent + 2, list_stack=list_stack))
            elif btype == "toggle":
                t = rich_text_to_markdown(payload.get("rich_text") or [])
                lines.append(f"{prefix}- **{t}**\n")
                if block.get("has_children"):
                    lines.append(blocks_to_markdown(client, bid, indent=indent + 4, list_stack=list_stack))
            elif btype == "table":
                lines.append(f"{prefix}*[Notion table — see source in Notion]*\n\n")
                if block.get("has_children"):
                    lines.append(blocks_to_markdown(client, bid, indent=indent, list_stack=list_stack))
            elif btype == "child_page":
                title = payload.get("title") or "Child page"
                lines.append(f"{prefix}- [[Child page: {title}]]\n")
            elif btype == "synced_block":
                if block.get("has_children"):
                    lines.append(blocks_to_markdown(client, bid, indent=indent, list_stack=list_stack))
            else:
                lines.append(f"{prefix}*[Block: {btype}]*\n")
        cursor = chunk.get("next_cursor")
        if not chunk.get("has_more"):
            break
    return "".join(lines)


UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)


def _hex32_to_uuid(h: str) -> str:
    h = h.lower()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def normalize_notion_id(raw: str) -> str:
    s = raw.strip()
    m = UUID_RE.search(s)
    if m:
        return m.group(0).lower()
    # Notion share links often use 32 hex chars (no dashes) in the path; ?v= is a view id — use path only.
    if "notion.so" in s or "notion.site" in s:
        path = urlparse(s).path.strip("/")
        if path:
            last_seg = path.split("/")[-1]
            tail = re.search(r"([0-9a-f]{32})$", last_seg, re.I)
            if tail:
                return _hex32_to_uuid(tail.group(1))
    bare = re.fullmatch(r"[0-9a-f]{32}", s, re.I)
    if bare:
        return _hex32_to_uuid(bare.group(0))
    return s


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "databases": {}, "pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "databases": {}, "pages": {}}
    data.setdefault("version", 1)
    data.setdefault("databases", {})
    data.setdefault("pages", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def db_high_water(state: dict[str, Any], db_id: str) -> str | None:
    entry = state["databases"].get(db_id)
    if not isinstance(entry, dict):
        return None
    hw = entry.get("high_water_mark")
    return str(hw) if hw else None


def set_db_high_water(state: dict[str, Any], db_id: str, iso: str) -> None:
    if db_id not in state["databases"] or not isinstance(state["databases"][db_id], dict):
        state["databases"][db_id] = {}
    cur = state["databases"][db_id].get("high_water_mark")
    if not cur:
        state["databases"][db_id]["high_water_mark"] = iso
        return
    try:
        if parse_iso(iso) > parse_iso(str(cur)):
            state["databases"][db_id]["high_water_mark"] = iso
    except ValueError:
        state["databases"][db_id]["high_water_mark"] = iso


def filter_after_edited(after_iso: str) -> dict[str, Any]:
    return {
        "timestamp": "last_edited_time",
        "last_edited_time": {"after": after_iso},
    }


def write_page_markdown(
    out_dir: Path,
    page: dict[str, Any],
    body_md: str,
    *,
    database_id: str | None,
    synced_at: str,
    dry_run: bool,
) -> Path:
    page_id = str(page.get("id", ""))
    title = page_title(page)
    le = page_last_edited(page)
    notion_url = f"https://www.notion.so/{page_id.replace('-', '')}"
    fname = f"{page_id}.md"
    path = out_dir / fname
    frontmatter = {
        "notion_page_id": page_id,
        "notion_url": notion_url,
        "title": title,
        "last_edited_time": le,
        "synced_at": synced_at,
    }
    if database_id:
        frontmatter["notion_database_id"] = database_id
    fm = json.dumps(frontmatter, indent=2)
    content = f"---\n{fm}\n---\n\n# {title}\n\n{body_md.strip()}\n"
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return path


def sync_one_page(
    client: NotionClient,
    page: dict[str, Any],
    state: dict[str, Any],
    out_dir: Path,
    *,
    raw_root: Path,
    database_id: str | None,
    dry_run: bool,
    verbose: bool,
    ingest_paths: list[str] | None,
) -> bool:
    page_id = str(page.get("id", ""))
    le = page_last_edited(page)
    if not page_id or not le:
        return False
    prev = state["pages"].get(page_id)
    if prev == le:
        if verbose:
            print(f"  skip unchanged {page_id}", file=sys.stderr)
        return True
    try:
        body = blocks_to_markdown(client, page_id)
    except Exception as e:
        print(f"  blocks failed {page_id}: {e}", file=sys.stderr)
        return False
    synced_at = format_iso(datetime.now(timezone.utc))
    path = write_page_markdown(
        out_dir,
        page,
        body,
        database_id=database_id,
        synced_at=synced_at,
        dry_run=dry_run,
    )
    if verbose:
        print(f"  wrote {path.relative_to(ROOT)}", file=sys.stderr)
    state["pages"][page_id] = le
    if not dry_run and ingest_paths is not None:
        try:
            ingest_paths.append(str(path.relative_to(raw_root)))
        except ValueError:
            pass
    return True


def iter_database_pages(
    client: NotionClient,
    database_id: str,
    filter_body: dict[str, Any] | None,
    *,
    max_pages: int,
) -> Iterator[dict[str, Any]]:
    cursor: str | None = None
    count = 0
    while count < max_pages:
        chunk = client.query_database(database_id, start_cursor=cursor, filter_body=filter_body)
        results = chunk.get("results") or []
        if not isinstance(results, list):
            break
        for page in results:
            if isinstance(page, dict):
                yield page
                count += 1
                if count >= max_pages:
                    return
        if not chunk.get("has_more"):
            break
        cursor = chunk.get("next_cursor")
        if not cursor:
            break


def sync_database(
    client: NotionClient,
    database_id: str,
    state: dict[str, Any],
    out_dir: Path,
    *,
    raw_root: Path,
    overlap_seconds: int,
    max_pages: int,
    dry_run: bool,
    verbose: bool,
    stats: SyncStats,
    ingest_paths: list[str] | None,
) -> None:
    hw = db_high_water(state, database_id)
    filter_body: dict[str, Any] | None = None
    if hw:
        try:
            boundary = parse_iso(hw) - timedelta(seconds=overlap_seconds)
            filter_body = filter_after_edited(format_iso(boundary))
        except ValueError:
            filter_body = None
    if verbose:
        mode = "incremental" if filter_body else "full"
        print(f"database {database_id} ({mode})", file=sys.stderr)

    max_seen: datetime | None = None
    try:
        parsed_hw = parse_iso(hw) if hw else None
    except ValueError:
        parsed_hw = None
    if parsed_hw:
        max_seen = parsed_hw

    for page in iter_database_pages(client, database_id, filter_body, max_pages=max_pages):
        stats.pages_considered += 1
        le = page_last_edited(page)
        try:
            le_dt = parse_iso(le)
            if max_seen is None or le_dt > max_seen:
                max_seen = le_dt
        except ValueError:
            pass
        page_id = str(page.get("id", ""))
        prev = state["pages"].get(page_id)
        if prev == le:
            stats.pages_skipped += 1
            if verbose:
                print(f"  skip unchanged {page_id}", file=sys.stderr)
            continue
        stats.pages_fetched += 1
        ok = sync_one_page(
            client,
            page,
            state,
            out_dir,
            raw_root=raw_root,
            database_id=database_id,
            dry_run=dry_run,
            verbose=verbose,
            ingest_paths=ingest_paths,
        )
        if not ok:
            stats.errors += 1

    if max_seen and not dry_run:
        set_db_high_water(state, database_id, format_iso(max_seen))


def sync_standalone_pages(
    client: NotionClient,
    page_ids: list[str],
    state: dict[str, Any],
    out_dir: Path,
    *,
    raw_root: Path,
    dry_run: bool,
    verbose: bool,
    stats: SyncStats,
    ingest_paths: list[str] | None,
) -> None:
    for raw_id in page_ids:
        pid = normalize_notion_id(raw_id)
        if verbose:
            print(f"page {pid}", file=sys.stderr)
        try:
            page = client.get_page(pid)
        except Exception as e:
            print(f"  get_page failed {pid}: {e}", file=sys.stderr)
            stats.errors += 1
            continue
        stats.pages_considered += 1
        le = page_last_edited(page)
        prev = state["pages"].get(pid)
        if prev == le:
            stats.pages_skipped += 1
            continue
        stats.pages_fetched += 1
        ok = sync_one_page(
            client,
            page,
            state,
            out_dir,
            raw_root=raw_root,
            database_id=None,
            dry_run=dry_run,
            verbose=verbose,
            ingest_paths=ingest_paths,
        )
        if not ok:
            stats.errors += 1


def run_wiki_ingest_batch(workspace: str, paths: list[str]) -> str:
    """Load app.py and run enqueue_notion_ingest_batch (Vertex ingest + batch JSON)."""
    if not paths:
        return ""
    mod_name = "llm_wiki_app"
    app_path = ROOT / "app.py"
    spec = importlib.util.spec_from_file_location(mod_name, app_path)
    if spec is None or spec.loader is None:
        print("Could not load app.py for ingest.", file=sys.stderr)
        return ""
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    enqueue = getattr(mod, "enqueue_notion_ingest_batch", None)
    if enqueue is None:
        print("app.py missing enqueue_notion_ingest_batch.", file=sys.stderr)
        return ""
    return str(enqueue(workspace, paths))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Incremental Notion → raw/sources/notion sync.")
    p.add_argument(
        "--config",
        type=Path,
        default=ROOT / CONFIG_NAME,
        help=f"Path to {CONFIG_NAME}",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write files or state")
    p.add_argument(
        "--no-ingest",
        action="store_true",
        help="Skip wiki ingest batch after sync (only fetch to raw/)",
    )
    p.add_argument(
        "--ingest-raw-only",
        action="store_true",
        help="Do not call Notion; run ingest batch for every .md in raw/sources/notion/pages/ (catch-up)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    ws = str(cfg.get("workspace") or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
    overlap = int(cfg.get("overlap_seconds") or 120)
    max_pages = int(cfg.get("max_pages_per_run") or 500)

    raw_dbs = cfg.get("databases") or []
    raw_pages = cfg.get("pages") or []
    if not isinstance(raw_dbs, list):
        raw_dbs = []
    if not isinstance(raw_pages, list):
        raw_pages = []
    databases = [normalize_notion_id(str(x)) for x in raw_dbs if str(x).strip()]
    pages = [normalize_notion_id(str(x)) for x in raw_pages if str(x).strip()]

    if args.ingest_raw_only:
        root = workspace_root(ws)
        raw_root = root / "raw"
        pages_dir = raw_root / "sources" / "notion" / "pages"
        if args.dry_run:
            paths = sorted(
                str(p.relative_to(raw_root)) for p in pages_dir.glob("*.md") if p.is_file()
            )
            print(
                json.dumps(
                    {
                        "workspace": ws,
                        "mode": "ingest_raw_only_dry_run",
                        "would_ingest": len(paths),
                        "files": paths[:80],
                    },
                    indent=2,
                )
            )
            return
        rel_paths = sorted(
            str(p.relative_to(raw_root)) for p in pages_dir.glob("*.md") if p.is_file()
        )
        if args.verbose:
            print(f"ingest-raw-only: {len(rel_paths)} file(s) under {pages_dir}", file=sys.stderr)
        batch_id = run_wiki_ingest_batch(ws, rel_paths)
        print(
            json.dumps(
                {
                    "workspace": ws,
                    "mode": "ingest_raw_only",
                    "ingest_batch_id": batch_id,
                    "ingest_files": len(rel_paths),
                },
                indent=2,
            )
        )
        return

    if not databases and not pages:
        print("Configure at least one database or page id.", file=sys.stderr)
        sys.exit(1)

    root = workspace_root(ws)
    raw_root = root / "raw"
    raw_sources = raw_root / "sources"
    out_dir = raw_sources / "notion" / "pages"
    state_path = root / ".notion-sync-state.json"

    token = notion_token()
    client = NotionClient(token, dry_run=args.dry_run, verbose=args.verbose)
    state = load_state(state_path)
    stats = SyncStats()
    queue_ingest = bool(cfg.get("queue_ingest", True)) and not args.no_ingest
    ingest_paths: list[str] | None = [] if queue_ingest and not args.dry_run else None

    for db_id in databases:
        try:
            sync_database(
                client,
                db_id,
                state,
                out_dir,
                raw_root=raw_root,
                overlap_seconds=overlap,
                max_pages=max_pages,
                dry_run=args.dry_run,
                verbose=args.verbose,
                stats=stats,
                ingest_paths=ingest_paths,
            )
        except Exception as e:
            print(f"database {db_id}: {e}", file=sys.stderr)
            stats.errors += 1

    if pages:
        sync_standalone_pages(
            client,
            pages,
            state,
            out_dir,
            raw_root=raw_root,
            dry_run=args.dry_run,
            verbose=args.verbose,
            stats=stats,
            ingest_paths=ingest_paths,
        )

    if not args.dry_run:
        save_state(state_path, state)

    ingest_batch_id = ""
    if ingest_paths is not None and ingest_paths:
        if args.verbose:
            print(f"ingest batch: {len(ingest_paths)} file(s) → wiki", file=sys.stderr)
        ingest_batch_id = run_wiki_ingest_batch(ws, ingest_paths)

    print(
        json.dumps(
            {
                "workspace": ws,
                "considered": stats.pages_considered,
                "fetched": stats.pages_fetched,
                "skipped": stats.pages_skipped,
                "errors": stats.errors,
                "out_dir": str(out_dir.relative_to(ROOT)),
                "state": str(state_path.relative_to(ROOT)),
                "ingest_batch_id": ingest_batch_id,
                "ingest_files": len(ingest_paths) if ingest_paths else 0,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
