#!/usr/bin/env python3
"""Local Wikipedia-style interface for an AI-maintained markdown wiki."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import io
import json
import mimetypes
import os
import posixpath
import queue
import re
import socket
import shutil
import ssl
import subprocess
import threading
import time
import textwrap
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import Request, urlopen

import certifi


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
WORKSPACES_ROOT = ROOT / "workspaces"
WORKSPACE_STATE_PATH = ROOT / ".workspace-state.json"
DEFAULT_WORKSPACE = "default"
CURRENT_WORKSPACE = DEFAULT_WORKSPACE
WIKI_ROOT = ROOT / "wiki"
RAW_ROOT = ROOT / "raw"
RAW_SOURCES = RAW_ROOT / "sources"
DOMAINS_ROOT = WIKI_ROOT / "domains"
GLOBAL_ROOT = WIKI_ROOT / "global"
LOG_PATH = WIKI_ROOT / "log.md"
INDEX_PATH = WIKI_ROOT / "index.md"
REVISIONS_ROOT = WIKI_ROOT / "revisions"
ARCHIVE_ROOT = WIKI_ROOT / "archive"
STAGING_ROOT = WIKI_ROOT / "staging"
STATUS_PATH = ROOT / ".ingestion-status.json"
BATCHES_ROOT = ROOT / ".batches"
REVIEW_STATE_PATH = ROOT / ".review-state.json"
CHAT_STATE_PATH = ROOT / ".chat-state.json"
DOMAIN_CONFIDENCE_THRESHOLD = 0.62
STATUS_LOCK = threading.Lock()
BATCH_LOCK = threading.Lock()
CHAT_LOCK = threading.Lock()
INGEST_QUEUE: queue.Queue[dict[str, object]] = queue.Queue()
WORKER_STARTED = False


SECTIONS = {
    "overview": "Overview",
    "domains": "Domains",
    "global": "Global Knowledge",
    "sources": "AI Source Pages",
    "entities": "Entities",
    "concepts": "Concepts",
    "queries": "Queries",
    "staging": "Staging",
    "archive": "Archive",
}


def workspace_slug(name: str) -> str:
    slug = slugify(name)
    return slug or DEFAULT_WORKSPACE


def workspace_root(name: str) -> Path:
    if name == DEFAULT_WORKSPACE:
        return ROOT
    return WORKSPACES_ROOT / name


def configure_workspace(name: str) -> str:
    global CURRENT_WORKSPACE, WIKI_ROOT, RAW_ROOT, RAW_SOURCES, DOMAINS_ROOT, GLOBAL_ROOT, LOG_PATH, INDEX_PATH, REVISIONS_ROOT, ARCHIVE_ROOT, STAGING_ROOT, STATUS_PATH, BATCHES_ROOT, REVIEW_STATE_PATH, CHAT_STATE_PATH
    normalized = workspace_slug(name)
    root = workspace_root(normalized)
    CURRENT_WORKSPACE = normalized
    WIKI_ROOT = root / "wiki"
    RAW_ROOT = root / "raw"
    RAW_SOURCES = RAW_ROOT / "sources"
    DOMAINS_ROOT = WIKI_ROOT / "domains"
    GLOBAL_ROOT = WIKI_ROOT / "global"
    LOG_PATH = WIKI_ROOT / "log.md"
    INDEX_PATH = WIKI_ROOT / "index.md"
    REVISIONS_ROOT = WIKI_ROOT / "revisions"
    ARCHIVE_ROOT = WIKI_ROOT / "archive"
    STAGING_ROOT = WIKI_ROOT / "staging"
    STATUS_PATH = root / ".ingestion-status.json"
    BATCHES_ROOT = root / ".batches"
    REVIEW_STATE_PATH = root / ".review-state.json"
    CHAT_STATE_PATH = root / ".chat-state.json"
    return normalized


def read_workspace_state() -> str:
    if not WORKSPACE_STATE_PATH.exists():
        return DEFAULT_WORKSPACE
    try:
        payload = json.loads(WORKSPACE_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_WORKSPACE
    value = str(payload.get("current_workspace", DEFAULT_WORKSPACE)).strip()
    return workspace_slug(value) or DEFAULT_WORKSPACE


def write_workspace_state(name: str) -> None:
    WORKSPACE_STATE_PATH.write_text(json.dumps({"current_workspace": workspace_slug(name)}, indent=2), encoding="utf-8")


def list_workspaces() -> list[str]:
    workspaces = [DEFAULT_WORKSPACE]
    if WORKSPACES_ROOT.exists():
        for path in sorted(WORKSPACES_ROOT.iterdir()):
            if path.is_dir():
                workspaces.append(path.name)
    seen: set[str] = set()
    result: list[str] = []
    for name in workspaces:
        if name not in seen:
            result.append(name)
            seen.add(name)
    return result


def display_workspace_name(name: str) -> str:
    return "default" if name == DEFAULT_WORKSPACE else name


def initialize_workspace_files(name: str) -> None:
    root = workspace_root(name)
    wiki_root = root / "wiki"
    raw_root = root / "raw"
    templates_root = wiki_root / "templates"
    (raw_root / "sources").mkdir(parents=True, exist_ok=True)
    (raw_root / "assets").mkdir(parents=True, exist_ok=True)
    (wiki_root / "domains").mkdir(parents=True, exist_ok=True)
    (wiki_root / "global" / "entities").mkdir(parents=True, exist_ok=True)
    (wiki_root / "global" / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki_root / "sources").mkdir(parents=True, exist_ok=True)
    (wiki_root / "entities").mkdir(parents=True, exist_ok=True)
    (wiki_root / "concepts").mkdir(parents=True, exist_ok=True)
    (wiki_root / "queries").mkdir(parents=True, exist_ok=True)
    (wiki_root / "revisions").mkdir(parents=True, exist_ok=True)
    (wiki_root / "staging" / "sources").mkdir(parents=True, exist_ok=True)
    (wiki_root / "staging" / "domain-review").mkdir(parents=True, exist_ok=True)
    (wiki_root / "archive" / "entities").mkdir(parents=True, exist_ok=True)
    (wiki_root / "archive" / "concepts").mkdir(parents=True, exist_ok=True)
    (root / ".batches").mkdir(parents=True, exist_ok=True)
    templates_root.mkdir(parents=True, exist_ok=True)

    shared_templates = ROOT / "wiki" / "templates"
    for template_name in ["source-summary-template.md", "entity-template.md", "concept-template.md"]:
        target = templates_root / template_name
        source = shared_templates / template_name
        if not target.exists() and source.exists():
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    defaults: dict[Path, str] = {
        wiki_root / "index.md": "# Index\n\nThis workspace is empty. Upload source material to begin building the wiki.\n",
        wiki_root / "log.md": "# Log\n\n- Workspace initialized.\n",
        wiki_root / "overview.md": "# Overview\n\nThis workspace is currently empty.\n",
        wiki_root / "domains" / "README.md": "# Domains\n\nAuto-detected domain wikis are written here.\n",
        wiki_root / "global" / "README.md": "# Global Knowledge\n\nOnly concepts and entities reused across multiple domains belong here.\n",
        wiki_root / "queries" / "README.md": "# Queries\n\nDurable query artifacts and review outputs are written here.\n",
        wiki_root / "sources" / "README.md": "# Sources\n\nAI-generated source summaries are written here after ingest.\n",
        wiki_root / "entities" / "README.md": "# Entities\n\nAI-maintained entity pages are written here.\n",
        wiki_root / "concepts" / "README.md": "# Concepts\n\nAI-maintained concept pages are written here.\n",
        wiki_root / "staging" / "README.md": "# Staging\n\nSources that need review before active promotion are written here.\n",
        wiki_root / "staging" / "sources" / "README.md": "# Staged Sources\n\nPer-source intake assessments and held drafts are written here.\n",
        wiki_root / "staging" / "domain-review" / "README.md": "# Domain Review\n\nSources with uncertain domain classification are held here.\n",
        raw_root / "README.md": "# Raw Sources\n\nImmutable uploaded source material lives here.\n",
        raw_root / "assets" / ".gitkeep": "",
        root / ".ingestion-status.json": json.dumps({"status": "idle", "queued_jobs": 0, "current_batch": None, "phase": "idle", "current_file": None, "last_event": "Workspace initialized.", "recent_events": []}),
        root / ".review-state.json": json.dumps({"pending": False, "review_page": None, "questions": [], "created_at": None}),
        root / ".chat-state.json": json.dumps({"messages": []}),
    }
    for path, content in defaults.items():
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def switch_workspace(name: str) -> str:
    normalized = configure_workspace(name)
    initialize_workspace_files(normalized)
    write_workspace_state(normalized)
    return normalized


STYLE = """
:root {
  --bg: #f8f9fa;
  --paper: #ffffff;
  --sidebar: #f8f9fa;
  --border: #a2a9b1;
  --border-light: #c8ccd1;
  --line: #eaecf0;
  --text: #202122;
  --muted: #54595d;
  --link: #36c;
  --link-visited: #6b4ba1;
  --accent: #36c;
  --chip: #f8f9fa;
  --nav: #f8f9fa;
  --shadow: none;
  --heading: #000;
}

* {
  box-sizing: border-box;
}

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: sans-serif;
}

a {
  color: var(--link);
  text-decoration: none;
}

a:visited {
  color: var(--link-visited);
}

a:hover {
  text-decoration: underline;
}

.layout {
  display: grid;
  grid-template-columns: 176px minmax(0, 1fr);
  min-height: 100vh;
}

.sidebar {
  background: var(--sidebar);
  border-right: 1px solid var(--border-light);
  padding: 12px 14px 32px;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}

.brand {
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-light);
}

.brand h1 {
  margin: 0;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.5rem;
  font-weight: normal;
  letter-spacing: 0;
}

.brand p {
  margin: 4px 0 0;
  color: var(--muted);
  font-size: 0.78rem;
  line-height: 1.3;
}

.search-form {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
}

.search-form input,
.upload-form input[type="text"],
.upload-form textarea,
.query-form textarea,
.query-form select {
  width: 100%;
  padding: 7px 8px;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text);
  font: inherit;
  font-size: 0.875rem;
}

.search-form button,
.upload-form button,
.query-form button,
.toolbar a.button-link {
  display: inline-block;
  border: 1px solid var(--border);
  background: #f8f9fa;
  color: var(--text);
  padding: 7px 10px;
  font: inherit;
  font-size: 0.875rem;
  cursor: pointer;
  box-shadow: none;
}

.nav-section {
  margin-top: 14px;
}

.nav-section h2 {
  margin: 0 0 8px;
  font-size: 0.78rem;
  font-weight: normal;
  color: var(--muted);
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 3px;
}

.nav-section ul {
  list-style: none;
  margin: 0;
  padding: 0;
}

.nav-section li {
  margin: 0 0 6px;
  line-height: 1.3;
  font-size: 0.875rem;
}

.nav-meta {
  color: var(--muted);
  font-size: 0.78rem;
  line-height: 1.35;
}

.content-wrap {
  padding: 0;
  padding-bottom: 400px;
}

.topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  background: var(--paper);
  border-bottom: 1px solid var(--border-light);
  padding: 10px 24px 0;
}

.wordmark {
  display: flex;
  align-items: center;
  gap: 12px;
}

.wordmark-mark {
  width: 42px;
  height: 42px;
  border: 1px solid var(--border);
  border-radius: 50%;
  display: grid;
  place-items: center;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.2rem;
  background: linear-gradient(180deg, #fff, #eaecf0);
}

.wordmark-text strong {
  display: block;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.45rem;
  font-weight: normal;
}

.wordmark-text span {
  display: block;
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}

.top-links {
  font-size: 0.78rem;
  color: var(--muted);
}

.content-inner {
  padding: 0 24px 24px;
}

.toolbar {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: center;
  margin-bottom: 0;
  padding: 10px 0 0;
}

.breadcrumbs {
  font-size: 0.78rem;
  color: var(--muted);
}

.shell {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 280px;
  gap: 0;
  align-items: start;
}

.page,
.panel {
  background: var(--paper);
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
}

.page {
  padding: 0 24px 32px;
  border-top: 0;
}

.article-title {
  margin: 0;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.8rem;
  font-weight: normal;
  line-height: 1.2;
  color: var(--heading);
  border-bottom: 1px solid var(--border-light);
  padding: 14px 0 10px;
}

.subtitle {
  margin: 8px 0 0;
  color: var(--muted);
  font-size: 0.875rem;
}

.article-body {
  line-height: 1.6;
  font-size: 0.95rem;
  font-family: sans-serif;
}

.article-body h1,
.article-body h2,
.article-body h3 {
  font-weight: normal;
  font-family: Georgia, "Times New Roman", serif;
  color: var(--heading);
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 2px;
  margin-top: 1.6em;
  margin-bottom: 0.35em;
}

.article-body h1 {
  font-size: 1.5rem;
}

.article-body h2 {
  font-size: 1.32rem;
}

.article-body h3 {
  font-size: 1.05rem;
}

.article-body code,
.mono {
  font-family: monospace;
  background: #f8f9fa;
  padding: 0.08em 0.28em;
}

.article-body pre {
  background: #f8f9fa;
  border: 1px solid var(--border-light);
  padding: 14px;
  overflow-x: auto;
}

.article-body ul {
  padding-left: 24px;
}

.panel {
  padding: 14px 16px;
  align-self: start;
  border-left: 0;
  border-top: 0;
  background: #fbfbfc;
}

.panel h2 {
  margin: 0 0 8px;
  font-size: 1rem;
  font-family: Georgia, "Times New Roman", serif;
  font-weight: normal;
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 4px;
}

.stats {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 0;
  margin: 16px 0 22px;
  border: 1px solid var(--border-light);
  background: #f8f9fa;
}

.stat {
  padding: 12px 14px;
  border-right: 1px solid var(--border-light);
  background: transparent;
}

.stat:last-child {
  border-right: 0;
}

.stat strong {
  display: block;
  font-size: 1.2rem;
  font-weight: 600;
}

.chip {
  display: inline-block;
  padding: 4px 8px;
  border: 1px solid var(--border-light);
  background: var(--chip);
  color: var(--text);
  font-size: 0.75rem;
  margin-right: 6px;
  margin-bottom: 6px;
}

.item-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.item-list li {
  padding: 8px 0;
  border-top: 1px solid var(--border-light);
}

.item-list li:first-child {
  border-top: 0;
  padding-top: 0;
}

.muted {
  color: var(--muted);
}

.section-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.section-card {
  border: 1px solid var(--border-light);
  background: #fff;
  padding: 14px;
}

.section-card h2 {
  margin: 0 0 8px;
  font-family: Georgia, "Times New Roman", serif;
  font-size: 1.1rem;
  font-weight: normal;
  border-bottom: 1px solid var(--border-light);
  padding-bottom: 4px;
}

.upload-form {
  display: grid;
  gap: 10px;
}

.query-form {
  display: grid;
  gap: 10px;
}

.upload-form input[type="file"] {
  font: inherit;
}

.flash {
  margin: 14px 0;
  padding: 10px 12px;
  border: 1px solid #a3bfb1;
  background: #f2fff5;
  font-size: 0.9rem;
}

.upload-note {
  margin-top: 8px;
  color: var(--muted);
  font-size: 0.88rem;
}

.upload-status {
  margin-top: 8px;
  color: var(--accent);
  font-size: 0.92rem;
}

.status-box {
  border: 1px solid var(--border-light);
  background: #f8f9fa;
  padding: 12px;
  margin: 0 0 16px;
}

.status-box h2 {
  margin: 0 0 8px;
  font-family: Georgia, "Times New Roman", serif;
  font-weight: normal;
  font-size: 1.08rem;
}

.status-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px 12px;
  margin-top: 10px;
}

.status-line {
  font-size: 0.84rem;
  color: var(--muted);
}

.status-line strong {
  display: block;
  color: var(--text);
  font-size: 1rem;
  font-weight: 600;
}

.status-events {
  margin: 10px 0 0;
  padding-left: 18px;
  font-size: 0.82rem;
  color: var(--muted);
}

.chat-shell {
  position: fixed;
  right: 18px;
  bottom: 18px;
  width: min(460px, calc(100vw - 28px));
  background: #fff;
  border: 1px solid var(--border);
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.14);
  z-index: 50;
}

.chat-header {
  border-bottom: 1px solid var(--border-light);
  padding: 10px 12px;
  background: #f8f9fa;
}

.chat-header strong {
  display: block;
  font-size: 0.96rem;
}

.chat-header span {
  color: var(--muted);
  font-size: 0.78rem;
}

.chat-messages {
  max-height: 340px;
  overflow-y: auto;
  padding: 12px;
  background: #fff;
}

.chat-empty {
  color: var(--muted);
  font-size: 0.84rem;
}

.chat-message {
  margin: 0 0 12px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.chat-message.user {
  align-items: flex-end;
}

.chat-message.assistant {
  align-items: flex-start;
}

.chat-bubble {
  max-width: 92%;
  padding: 9px 10px;
  border: 1px solid var(--border-light);
  background: #f8f9fa;
  font-size: 0.86rem;
  line-height: 1.55;
}

.chat-message.user .chat-bubble {
  background: #eef3ff;
  border-color: #b6ccf5;
}

.chat-message.pending .chat-bubble {
  color: var(--muted);
  font-style: italic;
}

.chat-bubble p,
.chat-bubble ul,
.chat-bubble h1,
.chat-bubble h2,
.chat-bubble h3 {
  margin-top: 0;
}

.chat-meta {
  color: var(--muted);
  font-size: 0.73rem;
}

.chat-form {
  border-top: 1px solid var(--border-light);
  padding: 10px 12px 12px;
  background: #fff;
}

.chat-form textarea,
.chat-form input {
  width: 100%;
  border: 1px solid var(--border);
  padding: 8px 10px;
  font: inherit;
  background: #fff;
}

.chat-form textarea {
  min-height: 74px;
  resize: vertical;
}

.chat-form input {
  margin-top: 8px;
}

.chat-form-actions {
  margin-top: 8px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.chat-help {
  color: var(--muted);
  font-size: 0.74rem;
}

.article-tabs {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  border-bottom: 1px solid var(--border-light);
  margin-top: 6px;
}

.tab-row {
  display: flex;
  gap: 0;
}

.tab {
  display: inline-block;
  padding: 8px 12px;
  border: 1px solid transparent;
  border-bottom: 0;
  color: var(--link);
  font-size: 0.86rem;
}

.tab.active {
  background: #fff;
  border-color: var(--border-light);
  color: var(--text);
}

.tab-tools {
  display: flex;
  gap: 4px;
  padding-bottom: 1px;
}

.tab-tools .tab {
  padding: 6px 8px;
}

@media (max-width: 1120px) {
  .shell {
    grid-template-columns: 1fr;
  }

  .panel {
    border-left: 1px solid var(--border);
    border-top: 0;
  }

  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 860px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .sidebar {
    position: static;
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }

  .content-wrap {
    padding: 0;
  }

  .content-inner,
  .topbar {
    padding-left: 16px;
    padding-right: 16px;
  }

  .section-grid {
    grid-template-columns: 1fr;
  }

  .stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .chat-shell {
    right: 10px;
    bottom: 10px;
    width: calc(100vw - 20px);
  }
}
"""


@dataclass
class Page:
    root: Path
    path: Path
    section: str
    managed_by_ai: bool

    @property
    def rel_path(self) -> str:
        return self.path.relative_to(self.root).as_posix()

    @property
    def title(self) -> str:
        return extract_title(self.path)

    @property
    def summary(self) -> str:
        return extract_summary(self.path)

    @property
    def updated(self) -> str:
        return dt.datetime.fromtimestamp(self.path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    @property
    def archived(self) -> bool:
        return self.rel_path.startswith("archive/")

    @property
    def domain(self) -> str:
        parts = self.rel_path.split("/")
        if len(parts) >= 2 and parts[0] == "domains":
            return parts[1]
        if parts and parts[0] == "global":
            return "global"
        return ""

    @property
    def kind(self) -> str:
        parts = self.rel_path.split("/")
        if len(parts) >= 3 and parts[0] == "domains":
            return {"sources": "source", "entities": "entity", "concepts": "concept", "queries": "query"}.get(parts[2], parts[2])
        if len(parts) >= 2 and parts[0] == "global":
            return {"entities": "entity", "concepts": "concept", "queries": "query"}.get(parts[1], parts[1])
        if parts[0] in {"sources", "entities", "concepts", "queries"}:
            return {"sources": "source", "entities": "entity", "concepts": "concept", "queries": "query"}[parts[0]]
        if self.rel_path.endswith("overview.md"):
            return "overview"
        return self.section.removesuffix("s")


@dataclass
class VertexConfig:
    api_key: str
    model_id: str
    project_id: str
    location: str = "us-central1"

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.model_id and self.project_id)


def log_event(event: str, detail: str = "") -> None:
    timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {event}"
    if detail:
        line += f" | {detail}"
    print(line, flush=True)


def read_status() -> dict[str, object]:
    if not STATUS_PATH.exists():
        return {
            "active": False,
            "batch_id": "",
            "job_type": "",
            "job_label": "",
            "phase": "idle",
            "saved_count": 0,
            "total_items": 0,
            "ingest_total": 0,
            "ingest_completed": 0,
            "failure_count": 0,
            "current_file": "",
            "current_step": "",
            "progress_label": "",
            "error": "",
            "started_at": "",
            "finished_at": "",
            "last_event": "",
            "recent_events": [],
            "queue_depth": 0,
        }
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "active": False,
            "batch_id": "",
            "job_type": "",
            "job_label": "",
            "phase": "idle",
            "saved_count": 0,
            "total_items": 0,
            "ingest_total": 0,
            "ingest_completed": 0,
            "failure_count": 0,
            "current_file": "",
            "current_step": "",
            "progress_label": "",
            "error": "",
            "started_at": "",
            "finished_at": "",
            "last_event": "Status file was corrupted and reset.",
            "recent_events": [],
            "queue_depth": 0,
        }


def write_status(status: dict[str, object]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")


def update_status(**changes: object) -> dict[str, object]:
    with STATUS_LOCK:
        status = read_status()
        recent = status.get("recent_events", [])
        if not isinstance(recent, list):
            recent = []
        event = changes.pop("event", "")
        if event:
            recent.append(f"{dt.datetime.now().strftime('%H:%M:%S')} {event}")
        status.update(changes)
        status["progress_label"] = status_progress_label(status)
        status["recent_events"] = recent[-12:]
        write_status(status)
        return status


def start_batch_status(total_items: int) -> str:
    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    update_status(
        active=True,
        batch_id=batch_id,
        job_type="ingest",
        job_label="Ingest Batch",
        phase="saving",
        saved_count=0,
        total_items=total_items,
        ingest_total=0,
        ingest_completed=0,
        failure_count=0,
        current_file="",
        current_step="Saving uploaded files",
        progress_label=f"Saved 0 / {total_items} files",
        error="",
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
        finished_at="",
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=f"Batch started with {total_items} uploaded items.",
        recent_events=[],
        event=f"batch started ({total_items} items)",
    )
    return batch_id


def finish_batch_status(last_event: str, *, phase: str = "done") -> None:
    update_status(
        active=False,
        phase=phase,
        current_file="",
        current_step="Completed" if phase == "done" else phase.title(),
        finished_at=dt.datetime.now().isoformat(timespec="seconds"),
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=last_event,
        event=last_event,
    )


def batch_path(batch_id: str) -> Path:
    return BATCHES_ROOT / f"{batch_id}.json"


def write_batch(batch_id: str, payload: dict[str, object]) -> None:
    with BATCH_LOCK:
        BATCHES_ROOT.mkdir(parents=True, exist_ok=True)
        batch_path(batch_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_batch(batch_id: str) -> dict[str, object]:
    path = batch_path(batch_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def update_batch(batch_id: str, **changes: object) -> dict[str, object]:
    with BATCH_LOCK:
        payload = read_batch(batch_id)
        payload.update(changes)
        batch_path(batch_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload


def read_chat_state() -> dict[str, object]:
    if not CHAT_STATE_PATH.exists():
        return {"messages": []}
    try:
        payload = json.loads(CHAT_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"messages": []}
    if not isinstance(payload, dict):
        return {"messages": []}
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    return {"messages": messages}


def write_chat_state(payload: dict[str, object]) -> None:
    CHAT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHAT_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_chat_messages(limit: int = 30) -> list[dict[str, object]]:
    messages = read_chat_state().get("messages", [])
    if not isinstance(messages, list):
        return []
    return [item for item in messages[-limit:] if isinstance(item, dict)]


def append_chat_message(role: str, content_md: str, *, status: str = "done", kind: str = "chat", batch_id: str = "", related_pages: list[str] | None = None) -> str:
    message_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    with CHAT_LOCK:
        payload = read_chat_state()
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        messages.append(
            {
                "id": message_id,
                "role": role,
                "status": status,
                "kind": kind,
                "content_md": content_md,
                "batch_id": batch_id,
                "related_pages": related_pages or [],
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
        payload["messages"] = messages[-60:]
        write_chat_state(payload)
    return message_id


def update_chat_message(message_id: str, **changes: object) -> dict[str, object]:
    with CHAT_LOCK:
        payload = read_chat_state()
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        updated: dict[str, object] = {}
        for item in messages:
            if not isinstance(item, dict):
                continue
            if str(item.get("id", "")) == message_id:
                item.update(changes)
                updated = item
                break
        payload["messages"] = messages
        write_chat_state(payload)
        return updated


def recent_batches(limit: int = 12) -> list[dict[str, object]]:
    if not BATCHES_ROOT.exists():
        return []
    batches: list[dict[str, object]] = []
    for path in sorted(BATCHES_ROOT.glob("*.json"), reverse=True):
        try:
            batches.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
        if len(batches) >= limit:
            break
    return batches


def batch_job_label(job_type: str) -> str:
    return {
        "ingest": "Ingest Batch",
        "review": "Wiki Review",
        "clarification": "Clarification Apply",
        "query": "Wiki Query",
        "action": "Wiki Action",
    }.get(job_type, "Background Job")


def describe_batch_metrics(batch: dict[str, object]) -> tuple[str, str]:
    job_type = str(batch.get("job_type", "ingest"))
    if job_type == "ingest":
        return (
            f"saved {batch.get('saved_count', 0)}",
            f"ingested {batch.get('ingest_completed', 0)} / {batch.get('ingest_total', 0)}",
        )
    if job_type == "review":
        return (
            f"pages scanned {batch.get('pages_scanned', 0)}",
            f"questions {len(batch.get('questions', [])) if isinstance(batch.get('questions'), list) else 0}",
        )
    if job_type == "clarification":
        return (
            f"tagged docs {batch.get('tagged_count', 0)}",
            f"pages updated {len(batch.get('touched_pages', [])) if isinstance(batch.get('touched_pages'), list) else 0}",
        )
    if job_type == "query":
        return (
            f"context pages {batch.get('context_count', 0)}",
            f"artifact {'yes' if batch.get('query_page') else 'pending'}",
        )
    if job_type == "action":
        return (
            f"actions {batch.get('planned_actions', 0)}",
            f"pages changed {len(batch.get('touched_pages', [])) if isinstance(batch.get('touched_pages'), list) else 0}",
        )
    return ("background job", str(batch.get("phase", "queued")))


def status_progress_label(status: dict[str, object]) -> str:
    job_type = str(status.get("job_type", ""))
    if job_type == "ingest":
        return f"Saved {status.get('saved_count', 0)} / {status.get('total_items', 0)} · Ingested {status.get('ingest_completed', 0)} / {status.get('ingest_total', 0)}"
    if job_type == "review":
        return f"Pages scanned {status.get('pages_scanned', 0)} · Questions {status.get('question_count', 0)}"
    if job_type == "clarification":
        return f"Tagged docs {status.get('tagged_count', 0)} · Pages updated {status.get('updated_pages', 0)}"
    if job_type == "query":
        return f"Context pages {status.get('context_count', 0)}"
    if job_type == "action":
        return f"Actions {status.get('planned_actions', 0)} · Pages changed {status.get('updated_pages', 0)}"
    return str(status.get("progress_label", "") or "No active progress.")


def markdown_links_for_pages(rel_paths: list[str]) -> str:
    lines = [f"- [{rel_path}](../{rel_path})" for rel_path in rel_paths if rel_path]
    return "\n".join(lines) or "- None."


def render_chat_messages_html(messages: list[dict[str, object]]) -> str:
    if not messages:
        return "<p class='chat-empty'>Ask from anywhere. The wiki will answer here and still file durable markdown artifacts in the background.</p>"
    chunks: list[str] = []
    for item in messages:
        role = str(item.get("role", "assistant"))
        status = str(item.get("status", "done"))
        body = str(item.get("content_md", "")).strip() or ("Working on it..." if status == "pending" else "No content.")
        bubble = render_markdown(body, "queries" if role == "assistant" else "", "wiki")
        meta_parts = [str(item.get("created_at", "")).replace("T", " ").strip()]
        if item.get("kind"):
            meta_parts.append(str(item.get("kind")))
        if status != "done":
            meta_parts.append(status)
        chunks.append(
            f"<div class='chat-message {html.escape(role)} {html.escape(status)}'>"
            f"<div class='chat-bubble'>{bubble}</div>"
            f"<div class='chat-meta'>{html.escape(' · '.join(part for part in meta_parts if part))}</div>"
            "</div>"
        )
    return "".join(chunks)


def read_review_state() -> dict[str, object]:
    if not REVIEW_STATE_PATH.exists():
        return {"pending": False, "questions": [], "review_page": "", "created_at": "", "context_pages": []}
    try:
        return json.loads(REVIEW_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"pending": False, "questions": [], "review_page": "", "created_at": "", "context_pages": []}


def write_review_state(payload: dict[str, object]) -> None:
    REVIEW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def count_page_sources(text: str) -> int:
    lines = text.splitlines()
    in_sources = False
    count = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_sources = stripped == "## Sources"
            continue
        if in_sources and stripped.startswith("- "):
            count += 1
    return count


def page_confidence(rel_path: str, text: str) -> str:
    if rel_path.startswith("archive/"):
        return "archived"
    if rel_path.startswith("staging/"):
        return "staged"
    lowered = text.lower()
    if "too thin to remain" in lowered or "low-confidence" in lowered or "merged into" in lowered:
        return "low"
    if "untitled" in text or count_page_sources(text) <= 1:
        return "medium"
    return "high"


def page_metadata(page: Page) -> dict[str, object]:
    text = read_text_file(page.path)
    return {
        "status": "archived" if page.archived else "active",
        "confidence": page_confidence(page.rel_path, text),
        "source_count": count_page_sources(text),
        "last_reviewed": page.updated,
    }


def extract_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except UnicodeDecodeError:
        pass
    return path.stem.replace("-", " ").title()


def is_archived_stub(path: Path) -> bool:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (UnicodeDecodeError, IndexError):
        return False
    return first_line == "<!-- archived-stub -->"


def extract_summary(path: Path) -> str:
    try:
        in_frontmatter = False
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped == "---":
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if not stripped or stripped.startswith("#") or stripped.startswith("-") or stripped.startswith("```"):
                continue
            return stripped[:180]
    except UnicodeDecodeError:
        return "Binary file"
    return "No summary yet."


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


def vertex_config() -> VertexConfig:
    dotenv = load_dotenv(ENV_PATH)
    return VertexConfig(
        api_key=os.getenv("VERTEX_API", dotenv.get("VERTEX_API", "")),
        model_id=os.getenv("MODEL_ID", dotenv.get("MODEL_ID", "")),
        project_id=os.getenv("PROJECT_ID", dotenv.get("PROJECT_ID", "")),
        location=os.getenv("VERTEX_LOCATION", dotenv.get("VERTEX_LOCATION", "us-central1")),
    )


def vertex_generate_url(config: VertexConfig) -> str:
    return f"https://aiplatform.googleapis.com/v1/publishers/google/models/{quote(config.model_id)}:generateContent?key={quote(config.api_key)}"


def extract_vertex_text(body: dict[str, object], *, error_message: str) -> str:
    candidates = body.get("candidates", [])
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError(error_message)
    parts = candidates[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list) or not parts:
        raise RuntimeError(error_message)
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()


def parse_vertex_json_text(text: str) -> dict[str, object]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```json\s*|```$", "", cleaned, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Vertex returned invalid JSON: {cleaned[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Vertex returned JSON that was not an object.")
    return parsed


def vertex_request_json(
    config: VertexConfig,
    payload: dict[str, object],
    *,
    label: str,
    timeout: int = 90,
    retries: int = 2,
) -> dict[str, object]:
    url = vertex_generate_url(config)
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    last_error: Exception | None = None
    total_attempts = retries + 1

    for attempt in range(1, total_attempts + 1):
        try:
            with urlopen(request, timeout=timeout, context=ssl_context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retriable = exc.code >= 500 or exc.code == 429
            last_error = RuntimeError(f"{label} failed with HTTP {exc.code}: {detail[:500]}")
            if not retriable or attempt >= total_attempts:
                raise last_error from exc
            wait_seconds = min(8, attempt * 2)
            log_event("vertex:retry", f"{label} | attempt {attempt}/{total_attempts} | HTTP {exc.code} | sleeping {wait_seconds}s")
            time.sleep(wait_seconds)
        except (URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = RuntimeError(f"{label} timed out or failed: {reason}")
            if attempt >= total_attempts:
                raise last_error from exc
            wait_seconds = min(8, attempt * 2)
            log_event("vertex:retry", f"{label} | attempt {attempt}/{total_attempts} | {reason} | sleeping {wait_seconds}s")
            time.sleep(wait_seconds)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{label} failed unexpectedly.")


def scan_wiki_pages(*, include_archived: bool = False) -> list[Page]:
    pages: list[Page] = []
    for path in sorted(WIKI_ROOT.rglob("*.md")):
        rel = path.relative_to(WIKI_ROOT).as_posix()
        if rel.startswith("templates/"):
            continue
        if rel.startswith("staging/"):
            continue
        if rel.startswith("archive/") and not include_archived:
            continue
        if is_archived_stub(path):
            continue
        if rel in {"index.md", "log.md"}:
            continue
        if path.name == "README.md":
            continue
        section = rel.split("/", 1)[0] if "/" in rel else "overview"
        pages.append(Page(root=WIKI_ROOT, path=path, section=section, managed_by_ai=True))
    return pages


def scan_staging_pages() -> list[Page]:
    pages: list[Page] = []
    if not STAGING_ROOT.exists():
        return pages
    for path in sorted(STAGING_ROOT.rglob("*.md")):
        rel = path.relative_to(WIKI_ROOT).as_posix()
        if path.name == "README.md":
            continue
        pages.append(Page(root=WIKI_ROOT, path=path, section="staging", managed_by_ai=True))
    return pages


def scan_raw_files() -> list[Page]:
    pages: list[Page] = []
    for path in sorted(RAW_ROOT.rglob("*")):
        if not path.is_file() or path.name in {".gitkeep", "README.md"}:
            continue
        section = path.relative_to(RAW_ROOT).parts[0]
        pages.append(Page(root=RAW_ROOT, path=path, section=section, managed_by_ai=False))
    return pages


def parse_log_entries() -> list[dict[str, str]]:
    if not LOG_PATH.exists():
        return []
    text = LOG_PATH.read_text(encoding="utf-8")
    entries: list[dict[str, str]] = []
    chunks = [chunk.strip() for chunk in text.split("## ") if chunk.strip()]
    for chunk in chunks:
        lines = chunk.splitlines()
        header = lines[0]
        if not header.startswith("["):
            continue
        body = " ".join(line.lstrip("- ").strip() for line in lines[1:] if line.strip())
        entries.append({"header": header, "body": body})
    entries.reverse()
    return entries[:8]


def repository_stats() -> dict[str, int]:
    wiki_pages = scan_wiki_pages()
    staging_pages = scan_staging_pages()
    raw_files = scan_raw_files()
    revisions = list(REVISIONS_ROOT.glob("*.json")) if REVISIONS_ROOT.exists() else []
    return {
        "wiki_pages": len(wiki_pages),
        "staging_pages": len(staging_pages),
        "raw_files": len(raw_files),
        "domains": len(scan_domains()),
        "sources": len([p for p in wiki_pages if p.kind == "source"]),
        "entities": len([p for p in wiki_pages if p.kind == "entity"]),
        "concepts": len([p for p in wiki_pages if p.kind == "concept"]),
        "queries": len([p for p in wiki_pages if p.kind == "query"]),
        "revisions": len(revisions),
    }


def ensure_directories() -> None:
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    initialize_workspace_files(CURRENT_WORKSPACE)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def wiki_rel_link(from_rel_path: str, to_rel_path: str) -> str:
    return os.path.relpath(WIKI_ROOT / to_rel_path, (WIKI_ROOT / from_rel_path).parent).replace(os.sep, "/")


def domain_root(domain_slug: str) -> Path:
    return DOMAINS_ROOT / slugify(domain_slug)


def domain_rel(domain_slug: str, *parts: str) -> str:
    return "/".join(["domains", slugify(domain_slug), *parts])


def domain_from_rel_path(rel_path: str) -> str:
    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] == "domains":
        return parts[1]
    return ""


def domain_title_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def domain_overview_rel(domain_slug: str) -> str:
    return domain_rel(domain_slug, "overview.md")


def scan_domains() -> list[dict[str, str]]:
    domains: list[dict[str, str]] = []
    if not DOMAINS_ROOT.exists():
        return domains
    for path in sorted(DOMAINS_ROOT.iterdir()):
        if not path.is_dir():
            continue
        overview = path / "overview.md"
        title = extract_title(overview) if overview.exists() else domain_title_from_slug(path.name)
        summary = extract_summary(overview) if overview.exists() else "No summary yet."
        domains.append({"slug": path.name, "title": title, "summary": summary})
    return domains


def ensure_domain(domain_slug: str, title: str = "", reason: str = "") -> str:
    slug = slugify(domain_slug or title)
    root = domain_root(slug)
    for child in ["sources", "entities", "concepts", "queries"]:
        (root / child).mkdir(parents=True, exist_ok=True)
    overview = root / "overview.md"
    if not overview.exists():
        display_title = title.strip() or domain_title_from_slug(slug)
        content = f"""---
tags: [domain]
domain: {slug}
status: active
---

# {display_title}

## Summary

Domain workspace for {display_title}. {reason.strip() or "This page will accumulate the domain-level synthesis as sources are ingested."}

## Key Points

- No domain synthesis yet.

## Evidence / Notes

- Domain created on {dt.date.today().isoformat()}.

## Links

- [Sources](sources/README.md)
- [Entities](entities/README.md)
- [Concepts](concepts/README.md)
- [Queries](queries/README.md)

## Open Questions

- What are the strongest recurring claims and decisions in this domain?
"""
        overview.write_text(content, encoding="utf-8")
    defaults = {
        root / "sources" / "README.md": f"# {domain_title_from_slug(slug)} Sources\n\nSource summaries for this domain.\n",
        root / "entities" / "README.md": f"# {domain_title_from_slug(slug)} Entities\n\nEntities specific to this domain.\n",
        root / "concepts" / "README.md": f"# {domain_title_from_slug(slug)} Concepts\n\nConcepts specific to this domain.\n",
        root / "queries" / "README.md": f"# {domain_title_from_slug(slug)} Queries\n\nDurable query artifacts for this domain.\n",
    }
    for path, content in defaults.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    return slug


def domain_metadata_from_result(result: dict[str, object]) -> dict[str, object]:
    slug = slugify(str(result.get("domain_slug", "")).strip() or str(result.get("domain_title", "")).strip())
    return {
        "domain_slug": slug,
        "domain_title": str(result.get("domain_title", "")).strip() or domain_title_from_slug(slug),
        "confidence": float(result.get("confidence", 0.0) or 0.0),
        "reason": str(result.get("reason", "")).strip(),
        "new_domain": bool(result.get("new_domain", False)),
        "cross_domain_candidates": sanitize_list(result.get("cross_domain_candidates", [])),
    }


def derive_title_from_raw(path: Path) -> str:
    title = extract_title(path)
    if title and title != path.stem.replace("-", " ").title():
        return title
    return path.stem.replace("-", " ").replace("_", " ").title()


def update_index_for_source(source_rel_path: str, summary: str) -> None:
    upsert_index_entry("Sources", source_rel_path, summary.strip() or "AI-generated source summary.")


def upsert_index_entry(section: str, rel_path: str, description: str) -> None:
    entry = f"- [{rel_path}]({rel_path}) - {description}"
    existing = INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else "# Index\n"
    section_header = f"## {section}"

    if f"[{rel_path}]({rel_path})" in existing:
        updated = re.sub(
            rf"^- \[{re.escape(rel_path)}\]\({re.escape(rel_path)}\) - .*$",
            entry,
            existing,
            flags=re.MULTILINE,
        )
        INDEX_PATH.write_text(updated, encoding="utf-8")
        return

    if section_header not in existing:
        existing += f"\n{section_header}\n\n- None yet.\n"

    pattern = rf"({re.escape(section_header)}\n\n)(.*?)(\n## |\Z)"
    match = re.search(pattern, existing, flags=re.DOTALL)
    if not match:
        existing += f"\n{section_header}\n\n{entry}\n"
        INDEX_PATH.write_text(existing, encoding="utf-8")
        return

    body = match.group(2)
    body = body.replace("- None yet.\n", "")
    if body and not body.endswith("\n"):
        body += "\n"
    new_section = f"{match.group(1)}{body}{entry}\n{match.group(3)}"
    updated = existing[:match.start()] + new_section + existing[match.end():]
    INDEX_PATH.write_text(updated, encoding="utf-8")


def rebuild_index_page() -> str:
    pages = scan_wiki_pages()
    domains = scan_domains()
    global_entities = [page for page in pages if page.rel_path.startswith("global/entities/")]
    global_concepts = [page for page in pages if page.rel_path.startswith("global/concepts/")]
    recent_queries = [
        page for page in pages
        if page.rel_path.startswith("queries/") or "/queries/" in page.rel_path
    ]
    staging_pages = scan_staging_pages()

    def render_section(header: str, items: list[Page]) -> str:
        if not items:
            return f"## {header}\n\n- None yet.\n"
        rows = "\n".join(
            (
                f"- [{page.rel_path}]({page.rel_path}) - {page.summary or 'No summary yet.'}"
                f" ({page_metadata(page)['confidence']}, {page_metadata(page)['source_count']} sources)"
            )
            for page in sorted(items, key=lambda item: item.rel_path)
        )
        return f"## {header}\n\n{rows}\n"

    domain_rows = []
    for domain in domains:
        slug = domain["slug"]
        domain_pages = [page for page in pages if page.rel_path.startswith(f"domains/{slug}/")]
        source_count = len([page for page in domain_pages if "/sources/" in page.rel_path])
        concept_count = len([page for page in domain_pages if "/concepts/" in page.rel_path])
        entity_count = len([page for page in domain_pages if "/entities/" in page.rel_path])
        domain_rows.append(
            f"- [domains/{slug}/overview.md](domains/{slug}/overview.md) - {domain['summary']} "
            f"({source_count} sources, {entity_count} entities, {concept_count} concepts)"
        )
    domains_md = "\n".join(domain_rows) or "- None yet."
    staging_md = "\n".join(
        f"- [{page.rel_path}]({page.rel_path}) - {page.summary or 'Staged for review.'}"
        for page in sorted(staging_pages, key=lambda item: item.rel_path)
    ) or "- None yet."

    content = "\n".join(
        [
            "# Index",
            "",
            "This index is rebuilt from the current domain-aware wiki state.",
            "",
            "## Overview",
            "",
            "- [overview.md](overview.md) - Compact map of all active domains.",
            "",
            "## Domains",
            "",
            domains_md,
            "",
            render_section("Global Concepts", global_concepts),
            render_section("Global Entities", global_entities),
            render_section("Recent Queries", sorted(recent_queries, key=lambda item: item.path.stat().st_mtime, reverse=True)[:20]),
            "## Staging",
            "",
            staging_md,
        ]
    ).strip() + "\n"
    INDEX_PATH.write_text(content, encoding="utf-8")
    return "index.md"


def rebuild_root_overview() -> str:
    domains = scan_domains()
    domain_rows = "\n".join(
        f"- [{domain['title']}](domains/{domain['slug']}/overview.md): {domain['summary']}"
        for domain in domains
    ) or "- No active domains yet."
    content = f"""---
tags: [overview]
status: active
---

# Overview

This wiki is organized as a domain-aware Karpathy LLM Wiki. Raw uploads remain in `raw/`, while durable synthesis is compiled into domain folders under `wiki/domains/`.

## Domains

{domain_rows}

## Global Knowledge

- [Global concepts](global/concepts/README.md) are reserved for concepts reused across multiple domains.
- [Global entities](global/entities/README.md) are reserved for entities reused across multiple domains.

## Staging

- [Domain review](staging/domain-review/README.md) holds sources whose domain classification is uncertain.
- [Source staging](staging/sources/README.md) holds sources with weak or partial extraction.
"""
    (WIKI_ROOT / "overview.md").write_text(content, encoding="utf-8")
    return "overview.md"


def append_log(operation: str, title: str, summary: str, pages: list[str]) -> None:
    pages_text = ", ".join(f"[{Path(page).stem}]({page})" for page in pages) if pages else "None"
    entry = (
        f"\n## [{dt.date.today().isoformat()}] {operation} | {title}\n\n"
        f"- {summary}\n"
        f"- Pages touched: {pages_text}\n"
    )
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(entry)


def sanitize_list(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def sanitize_page_items(items: object, name_key: str = "name") -> list[dict[str, object]]:
    cleaned: list[dict[str, object]] = []
    if not isinstance(items, list):
        return cleaned
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get(name_key, "")).strip()
        if not name:
            continue
        cleaned.append(
            {
                "name": name,
                "summary": str(item.get("summary", "")).strip(),
                "facts": sanitize_list(item.get("facts", [])),
                "links": sanitize_list(item.get("links", [])),
            }
        )
    return cleaned


def source_type_label(source_type: str) -> str:
    return {
        "invoice": "Invoice",
        "workbook": "Workbook",
        "dataset": "Dataset",
        "meeting-notes": "Meeting notes",
        "transcript": "Transcript",
        "spec": "Specification",
        "article": "Article",
        "notes": "Notes",
    }.get(source_type, source_type.replace("-", " ").title())


def infer_source_type(path: Path, raw_text: str, workbook_info: dict[str, object] | None = None) -> str:
    suffix = path.suffix.lower()
    name = path.name.lower()
    lowered = raw_text.lower()
    if suffix in {".xlsx", ".xls"}:
        return "workbook"
    if suffix == ".json" and ("conversation_id" in lowered or "mapping" in lowered or "default_model_slug" in lowered):
        return "transcript"
    if suffix in {".csv", ".json"}:
        return "dataset"
    invoice_signals = ["invoice", "invoice no", "subtotal", "bill to", "qty", "amount", "tax", "gst", "cash"]
    if any(signal in name for signal in ["invoice", "bill"]) or sum(signal in lowered for signal in invoice_signals) >= 2:
        return "invoice"
    meeting_signals = ["action items", "minutes", "agenda", "attendees", "decisions", "follow-ups"]
    if sum(signal in lowered for signal in meeting_signals) >= 2:
        return "meeting-notes"
    transcript_signals = ["speaker", "transcript", "interviewer", "audience", "q&a", "question:"]
    if sum(signal in lowered for signal in transcript_signals) >= 2:
        return "transcript"
    spec_signals = ["requirements", "api", "endpoint", "acceptance criteria", "functional", "non-functional", "architecture"]
    if sum(signal in lowered for signal in spec_signals) >= 2:
        return "spec"
    if workbook_info and int(workbook_info.get("sheet_count", 0)) > 0:
        return "workbook"
    if suffix == ".pdf":
        return "article"
    if raw_text.count("\n#") >= 1 or raw_text.count("\n##") >= 2:
        return "article"
    if len(re.findall(r"[A-Za-z]{4,}", raw_text)) >= 150:
        return "article"
    return "notes"


def score_source_extraction(path: Path, raw_text: str, workbook_info: dict[str, object] | None = None) -> dict[str, object]:
    cleaned = raw_text.strip()
    if not cleaned:
        return {"label": "unusable", "score": 0.0, "reason": "No readable text was extracted from the source."}

    suffix = path.suffix.lower()
    words = re.findall(r"[A-Za-z]{3,}", cleaned)
    lines = [line for line in cleaned.splitlines() if line.strip()]
    signal_count = len(words)

    if suffix in {".xlsx", ".xls"}:
        sheets = workbook_info.get("sheets", []) if isinstance(workbook_info, dict) else []
        sheet_count = int(workbook_info.get("sheet_count", 0)) if isinstance(workbook_info, dict) else 0
        sample_rows = sum(len(sheet.get("sample_rows", [])) for sheet in sheets if isinstance(sheet, dict))
        if sheet_count >= 4 and sample_rows >= 16:
            return {"label": "high", "score": 0.95, "reason": f"Workbook extraction captured {sheet_count} sheets with representative rows."}
        if sheet_count >= 1 and sample_rows >= 4:
            return {"label": "medium", "score": 0.72, "reason": f"Workbook extraction captured {sheet_count} sheets but coverage is still partial."}
        return {"label": "low", "score": 0.38, "reason": "Workbook text was extracted, but too few rows or sheets were captured to trust promotion."}

    if suffix == ".pdf":
        if signal_count >= 180 and len(lines) >= 12:
            return {"label": "high", "score": 0.9, "reason": "PDF extraction returned substantial readable text with strong line structure."}
        if signal_count >= 45 and len(lines) >= 4:
            return {"label": "medium", "score": 0.68, "reason": "PDF extraction returned usable text, but the document may still be partial."}
        return {"label": "low", "score": 0.33, "reason": "PDF extraction returned only a thin text layer. Hold it in staging until verified."}

    if signal_count >= 250 and len(lines) >= 12:
        return {"label": "high", "score": 0.9, "reason": "The source has enough readable text to promote directly after summarization."}
    if signal_count >= 60 and len(lines) >= 4:
        return {"label": "medium", "score": 0.7, "reason": "The source has usable text, but it should still be treated with moderate confidence."}
    return {"label": "low", "score": 0.35, "reason": "The extracted text is too thin or fragmented to trust as an active wiki source."}


def should_promote_source(profile: dict[str, object]) -> bool:
    return str(profile.get("quality_label", "low")) in {"high", "medium"}


def source_type_prompt_guidance(source_type: str) -> str:
    return {
        "invoice": "- Treat this as a transactional document. Extract vendor, invoice number, date, totals, and line items. Do not invent abstract concepts from routine accounting terms.",
        "workbook": "- Treat this as a workbook. Prioritize sheet purpose, structure, notable columns, and high-signal patterns across sheets over generic narrative summaries.",
        "dataset": "- Treat this as structured data. Focus on what fields exist, what the records represent, and what analyses or follow-up pages would be useful.",
        "meeting-notes": "- Treat this as meeting notes. Prioritize decisions, action items, owners, dates, and unresolved questions.",
        "transcript": "- Treat this as a transcript. Prioritize speakers, arguments, and recurring topics rather than one-off filler lines.",
        "spec": "- Treat this as a specification. Prioritize scope, requirements, interfaces, constraints, and acceptance criteria.",
        "article": "- Treat this as prose. Extract core claims, evidence, and links to existing entities or concepts.",
    }.get(source_type, "- Treat this as a general note. Extract grounded facts and avoid over-creating entities or concepts.")


def build_source_profile(path: Path, raw_text: str, workbook_info: dict[str, object] | None = None) -> dict[str, object]:
    source_type = infer_source_type(path, raw_text, workbook_info)
    quality = score_source_extraction(path, raw_text, workbook_info)
    return {
        "source_type": source_type,
        "source_type_label": source_type_label(source_type),
        "quality_label": str(quality.get("label", "low")),
        "quality_score": float(quality.get("score", 0.0)),
        "quality_reason": str(quality.get("reason", "")).strip(),
        "decision": "promote" if should_promote_source({"quality_label": quality.get("label", "low")}) else "hold",
    }


def local_source_snapshot(raw_text: str) -> str:
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return "No readable text was extracted."
    for line in lines[:12]:
        if len(line) >= 40:
            return line[:220]
    return " ".join(lines[:4])[:220]


def summarize_json_value(value: object, *, max_depth: int = 2, max_items: int = 5) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        compact = re.sub(r"\s+", " ", value).strip()
        return compact[:140] + ("..." if len(compact) > 140 else "")
    if isinstance(value, list):
        if max_depth <= 0:
            return f"list[{len(value)}]"
        preview = ", ".join(summarize_json_value(item, max_depth=max_depth - 1, max_items=max_items) for item in value[:max_items])
        suffix = ", ..." if len(value) > max_items else ""
        return f"list[{len(value)}]: [{preview}{suffix}]"
    if isinstance(value, dict):
        keys = list(value.keys())
        if max_depth <= 0:
            return "object{" + ", ".join(str(key) for key in keys[:max_items]) + ("..." if len(keys) > max_items else "") + "}"
        rendered = []
        for key in keys[:max_items]:
            rendered.append(f"{key}={summarize_json_value(value.get(key), max_depth=max_depth - 1, max_items=max_items)}")
        suffix = ", ..." if len(keys) > max_items else ""
        return "{" + ", ".join(rendered) + suffix + "}"
    return str(value)[:140]


def extract_json_text(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""
    if len(raw) <= 120000:
        return raw

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:120000]

    lines: list[str] = [
        f"# JSON Source Summary: {path.name}",
        "",
        f"Original size: {len(raw):,} characters",
    ]

    if isinstance(parsed, list):
        lines.append(f"Top-level type: array")
        lines.append(f"Record count: {len(parsed)}")
        dict_items = [item for item in parsed if isinstance(item, dict)]
        if dict_items:
            key_counts: dict[str, int] = {}
            for item in dict_items[:200]:
                for key in item.keys():
                    key_counts[key] = key_counts.get(key, 0) + 1
            common_keys = [key for key, _ in sorted(key_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:40]]
            if common_keys:
                lines.append("Common keys: " + ", ".join(common_keys))
            conversation_like = {"conversation_id", "title", "mapping", "create_time", "update_time", "current_node"} & set(common_keys)
            if conversation_like:
                lines.append("Detected export shape: conversation/chat export")
            lines.append("")
            lines.append("Representative records:")
            for index, item in enumerate(dict_items[:8], start=1):
                focus_keys = common_keys[:12] if common_keys else list(item.keys())[:12]
                rendered = ", ".join(
                    f"{key}={summarize_json_value(item.get(key), max_depth=1, max_items=3)}"
                    for key in focus_keys
                    if key in item
                )
                lines.append(f"- Record {index}: {rendered}")
        else:
            lines.append("Array contains non-object items.")
            for index, item in enumerate(parsed[:12], start=1):
                lines.append(f"- Item {index}: {summarize_json_value(item, max_depth=1, max_items=3)}")
        return "\n".join(lines)

    if isinstance(parsed, dict):
        lines.append("Top-level type: object")
        keys = list(parsed.keys())
        lines.append("Top-level keys: " + ", ".join(str(key) for key in keys[:80]))
        lines.append("")
        lines.append("Representative fields:")
        for key in keys[:40]:
            lines.append(f"- {key}: {summarize_json_value(parsed.get(key), max_depth=2, max_items=4)}")
        return "\n".join(lines)

    return raw[:120000]


def vertex_generate_structured_summary(
    config: VertexConfig,
    *,
    title: str,
    raw_path: Path,
    raw_text: str,
    workbook_info: dict[str, object] | None = None,
    source_profile: dict[str, object] | None = None,
    existing_entities: list[str] | None = None,
    existing_concepts: list[str] | None = None,
) -> dict[str, object]:
    source_profile = source_profile or {}
    workbook_section = ""
    if workbook_info and raw_path.suffix.lower() in {".xlsx", ".xls"}:
        sheet_lines = []
        for sheet in workbook_info.get("sheets", [])[:40]:
            if not isinstance(sheet, dict):
                continue
            sheet_lines.append(
                f"- {sheet.get('name', '')}: rows={sheet.get('row_count', 0)}, cols={sheet.get('column_count', 0)}, columns={', '.join(sheet.get('columns', [])[:12])}"
            )
        workbook_section = textwrap.dedent(
            f"""
            Workbook metadata:
            - Sheet count: {workbook_info.get('sheet_count', 0)}
            - Sheets:
            {chr(10).join(sheet_lines) or '- None'}
            """
        ).strip()
    source_type = str(source_profile.get("source_type_label", "General source"))
    quality_label = str(source_profile.get("quality_label", "unknown"))
    quality_reason = str(source_profile.get("quality_reason", "")).strip()
    source_guidance = source_type_prompt_guidance(str(source_profile.get("source_type", "notes")))
    prompt = textwrap.dedent(
        f"""
        You are maintaining a markdown wiki.
        Read the uploaded source and produce a concise JSON object with this exact schema:
        {{
          "summary": "one short paragraph",
          "key_points": ["point 1", "point 2", "point 3"],
          "entities": [
            {{
              "name": "entity name",
              "summary": "one short paragraph",
              "facts": ["fact 1", "fact 2"],
              "links": ["related concept or entity"]
            }}
          ],
          "concepts": [
            {{
              "name": "concept name",
              "summary": "one short paragraph",
              "facts": ["fact 1", "fact 2"],
              "links": ["related concept or entity"]
            }}
          ],
          "overview_update": "2-4 sentence update for the wiki overview page",
          "contradictions": ["possible contradiction 1"],
          "open_questions": ["question 1"],
          "sheet_summaries": [
            {{
              "name": "sheet name",
              "summary": "what this sheet contains",
              "highlights": ["highlight 1", "highlight 2"]
            }}
          ]
        }}

        Rules:
        - Return valid JSON only.
        - Keep claims grounded in the source.
        - If information is absent, use an empty list.
        - Keep each list item short.
        - Quality over quantity: Extract ONLY highly specific, domain-central entities and concepts. Do NOT extract generic terms (e.g., "Company", "Software", "Data").
        - IMPORTANT: Try to map findings to the following existing Entities or Concepts instead of creating new ones if they are conceptually identical. Only create new ones if they represent a truly novel and important domain specific idea.
        - Existing Entities: {", ".join(existing_entities) if existing_entities else "None"}
        - Existing Concepts: {", ".join(existing_concepts) if existing_concepts else "None"}
        - If an entity or concept is not critical to understanding the core thesis of the source, omit it entirely.
        - Ensure that the summaries and facts are highly actionable and explain the exact relevance to the domain.
        - The source classification and extraction quality are part of the context. Adjust your output to them.
        - If the source is a workbook, cover the whole workbook, not just the first sheet.
        - Prefer describing major sheets and their role over inventing random entities.
        {source_guidance}

        Source title: {title}
        Source repo path: {raw_path.relative_to(ROOT).as_posix()}
        Source type: {source_type}
        Extraction quality: {quality_label}
        Extraction notes: {quality_reason or "None"}
        {workbook_section}

        Source content:
        {raw_text[:60000] if raw_path.suffix.lower() in {'.xlsx', '.xls'} else raw_text[:18000]}
        """
    ).strip()

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    body = vertex_request_json(config, payload, label="Structured ingest summary", timeout=120, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no candidates.")
    return parse_vertex_json_text(text)


def vertex_classify_source_domain(
    config: VertexConfig,
    *,
    title: str,
    raw_path: Path,
    raw_text: str,
    source_profile: dict[str, object],
) -> dict[str, object]:
    domains = scan_domains()
    domain_context = "\n".join(
        f"- {item['slug']}: {item['title']} - {item['summary']}"
        for item in domains
    ) or "- None yet."
    prompt = textwrap.dedent(
        f"""
        You are routing an uploaded source into a domain-aware LLM wiki.
        Return valid JSON only with this exact schema:
        {{
          "domain_slug": "lowercase-kebab-domain",
          "domain_title": "Human Domain Title",
          "confidence": 0.0,
          "reason": "short reason",
          "new_domain": false,
          "cross_domain_candidates": ["other-domain"]
        }}

        Rules:
        - Prefer an existing domain if the source clearly belongs there.
        - Create a new domain only if the source is clearly a different durable project/topic.
        - Use confidence 0.80+ only when the domain choice is obvious.
        - Use confidence below {DOMAIN_CONFIDENCE_THRESHOLD} when the source spans multiple domains or the destination is ambiguous.
        - Do not use generic domains like "general", "misc", or "notes".
        - Domain slugs must be lowercase kebab-case.

        Existing domains:
        {domain_context}

        Source title: {title}
        Source repo path: {raw_path.relative_to(ROOT).as_posix()}
        Source type: {source_profile.get('source_type_label', 'Unknown')}
        Extraction quality: {source_profile.get('quality_label', 'unknown')}

        Source excerpt:
        {raw_text[:9000]}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    body = vertex_request_json(config, payload, label="Domain classifier", timeout=90, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no domain classification.")
    return domain_metadata_from_result(parse_vertex_json_text(text))


def vertex_rewrite_markdown_page(
    config: VertexConfig,
    *,
    page_kind: str,
    title: str,
    current_markdown: str,
    source_title: str,
    source_page_link: str,
    source_summary: str,
    facts: list[str],
    links: list[str],
) -> str:
    prompt = textwrap.dedent(
        f"""
        You are maintaining an AI-written markdown wiki.
        Rewrite the existing {page_kind} page so it cleanly incorporates new evidence from one source.

        Output markdown only.

        Requirements:
        - Preserve or improve the page structure.
        - Keep the page concise and coherent.
        - Update claims if the new evidence sharpens, qualifies, or changes the current summary.
        - Keep links as relative markdown links.
        - Include a Sources section with the new source link if relevant.
        - Do not mention that you are an AI or that this was generated from a prompt.

        Preferred structure:
        # Title

        ## Summary
        ...

        ## Key Points
        - ...

        ## Evidence / Notes
        - ...

        ## Links
        - ...

        ## Sources
        - ...

        ## Open Questions
        - ...

        Page kind: {page_kind}
        Page title: {title}
        New source title: {source_title}
        New source page: {source_page_link}
        New source summary: {source_summary}
        New facts:
        {chr(10).join(f"- {fact}" for fact in facts) or "- None"}
        Candidate related links:
        {chr(10).join(f"- {link}" for link in links) or "- None"}

        Current page markdown:
        {current_markdown[:14000] if current_markdown.strip() else "(page does not exist yet)"}
        """
    ).strip()

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    body = vertex_request_json(config, payload, label=f"{page_kind.title()} rewrite", timeout=120, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no page rewrite.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*|```$", "", text, flags=re.DOTALL).strip()
    return text


def repair_internal_links(page_rel_path: str, markdown: str, pending_slugs: set[str] | None = None) -> str:
    current_dir = Path(page_rel_path).parent
    pending = pending_slugs or set()

    def replace_link(match: re.Match[str]) -> str:
        label = match.group(1)
        target = match.group(2)
        if "://" in target or target.startswith("#"):
            return match.group(0)
        resolved = posixpath.normpath((current_dir / target).as_posix())
        candidate = WIKI_ROOT / resolved
        if candidate.exists():
            return match.group(0)

        slug = Path(target).stem
        if slug in pending:
            domain_slug = domain_from_rel_path(page_rel_path)
            if "entities" in target:
                candidate = WIKI_ROOT / (domain_rel(domain_slug, "entities", f"{slug}.md") if domain_slug else f"entities/{slug}.md")
                rel = os.path.relpath(candidate, (WIKI_ROOT / page_rel_path).parent).replace(os.sep, "/")
                return f"[{label}]({rel})"
            elif "concepts" in target:
                candidate = WIKI_ROOT / (domain_rel(domain_slug, "concepts", f"{slug}.md") if domain_slug else f"concepts/{slug}.md")
                rel = os.path.relpath(candidate, (WIKI_ROOT / page_rel_path).parent).replace(os.sep, "/")
                return f"[{label}]({rel})"
            return match.group(0)

        domain_slug = domain_from_rel_path(page_rel_path)
        alternatives = [
            *((
                WIKI_ROOT / domain_rel(domain_slug, "entities", f"{slug}.md"),
                WIKI_ROOT / domain_rel(domain_slug, "concepts", f"{slug}.md"),
                WIKI_ROOT / domain_rel(domain_slug, "sources", f"{slug}.md"),
                WIKI_ROOT / domain_rel(domain_slug, "queries", f"{slug}.md"),
            ) if domain_slug else ()),
            WIKI_ROOT / "global" / "entities" / f"{slug}.md",
            WIKI_ROOT / "global" / "concepts" / f"{slug}.md",
            WIKI_ROOT / "queries" / f"{slug}.md",
            WIKI_ROOT / "entities" / f"{slug}.md",
            WIKI_ROOT / "concepts" / f"{slug}.md",
            WIKI_ROOT / "sources" / f"{slug}.md",
        ]
        for alt in alternatives:
            if alt.exists():
                rel = os.path.relpath(alt, (WIKI_ROOT / page_rel_path).parent).replace(os.sep, "/")
                return f"[{label}]({rel})"
        return label  # Strip broken link

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, markdown)


def write_staging_source_page(
    raw_rel_path: str,
    *,
    source_profile: dict[str, object],
    source_data: dict[str, object] | None = None,
    promoted_page_rel: str = "",
    domain_meta: dict[str, object] | None = None,
    domain_review: bool = False,
) -> str:
    raw_path = RAW_ROOT / raw_rel_path
    title = derive_title_from_raw(raw_path)
    rel_path = f"staging/{'domain-review' if domain_review else 'sources'}/{slugify(raw_path.stem)}.md"
    summary = ""
    if isinstance(source_data, dict):
        summary = str(source_data.get("summary", "")).strip()
    if not summary:
        summary = local_source_snapshot(read_text_file(raw_path, for_ingest=True))
    decision = str(source_profile.get("decision", "hold")).strip() or "hold"
    promoted_md = f"- Active page: [../../{promoted_page_rel}](../../{promoted_page_rel})" if promoted_page_rel else "- Active page: Not promoted yet."
    domain_meta = domain_meta or {}
    domain_slug = str(domain_meta.get("domain_slug", "")).strip()
    domain_confidence = domain_meta.get("confidence", "")
    domain_reason = str(domain_meta.get("reason", "")).strip() or "Not classified yet."
    content = f"""---
tags: [staging, source]
domain: {domain_slug or "unclassified"}
domain_confidence: {domain_confidence if domain_confidence != "" else "unknown"}
domain_reason: "{domain_reason.replace('"', "'")}"
shared_scope: domain
source_paths: ["../../raw/{raw_rel_path}"]
status: staged
---

# {title} Intake Assessment
 
## Summary

{summary}

## Intake Metadata

- Raw path: ../../raw/{raw_rel_path}
- Source type: {source_profile.get('source_type_label', 'Unknown')}
- Extraction quality: {source_profile.get('quality_label', 'unknown')}
- Decision: {decision}
- Domain: {domain_slug or 'unclassified'}
- Domain confidence: {domain_confidence if domain_confidence != '' else 'unknown'}
- Domain reason: {domain_reason}
- Ingested: {dt.date.today().isoformat()}

## Decision Notes

- {source_profile.get('quality_reason', 'No decision notes captured.')}
{promoted_md}

## Open Questions

- Should this source stay in staging, or is it strong enough to promote into the active wiki?
"""
    target = WIKI_ROOT / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel_path


def write_ai_source_page(
    raw_rel_path: str,
    source_data: dict[str, object],
    *,
    domain_slug: str,
    domain_meta: dict[str, object] | None = None,
    sheet_page_links: list[str] | None = None,
    source_profile: dict[str, object] | None = None,
    staging_page_rel: str = "",
) -> str:
    raw_path = RAW_ROOT / raw_rel_path
    title = derive_title_from_raw(raw_path)
    slug = slugify(raw_path.stem)
    domain_slug = ensure_domain(domain_slug, str((domain_meta or {}).get("domain_title", "")), str((domain_meta or {}).get("reason", "")))
    wiki_rel_path = domain_rel(domain_slug, "sources", f"{slug}.md")
    wiki_path = WIKI_ROOT / wiki_rel_path
    wiki_path.parent.mkdir(parents=True, exist_ok=True)
    source_profile = source_profile or {}
    domain_meta = domain_meta or {}

    summary = str(source_data.get("summary", "")).strip() or "AI-generated source summary."
    key_points = [str(item).strip() for item in source_data.get("key_points", []) if str(item).strip()]
    entities = sanitize_page_items(source_data.get("entities", []))
    concepts = sanitize_page_items(source_data.get("concepts", []))
    contradictions = sanitize_list(source_data.get("contradictions", []))
    open_questions = sanitize_list(source_data.get("open_questions", []))
    sheet_summaries = source_data.get("sheet_summaries", [])

    key_points_md = "\n".join(f"- {point}" for point in key_points) or "- No key points extracted."
    entities_md = "\n".join(
        f"- [{item['name']}](../entities/{slugify(item['name'])}.md)"
        for item in entities
    ) or "- None identified."
    concepts_md = "\n".join(
        f"- [{item['name']}](../concepts/{slugify(item['name'])}.md)"
        for item in concepts
    ) or "- None identified."
    contradictions_md = "\n".join(f"- {item}" for item in contradictions) or "- None noted."
    questions_md = "\n".join(f"- {item}" for item in open_questions) or "- None yet."
    sheet_link_map = {Path(rel).stem.replace(f"{slug}-", "", 1): Path(rel).name for rel in (sheet_page_links or [])}
    sheet_md = "\n".join(
        (
            f"- **[{str(item.get('name', '')).strip()}]({sheet_link_map.get(slugify(str(item.get('name', '')).strip()), '')})**: {str(item.get('summary', '')).strip() or 'No summary.'}"
            if sheet_link_map.get(slugify(str(item.get("name", "")).strip()), "")
            else f"- **{str(item.get('name', '')).strip()}**: {str(item.get('summary', '')).strip() or 'No summary.'}"
        )
        + (
            f" Highlights: {', '.join(sanitize_list(item.get('highlights', []))[:4])}."
            if isinstance(item, dict) and sanitize_list(item.get("highlights", []))
            else ""
        )
        for item in sheet_summaries
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ) or "- No sheet-level breakdown captured."

    content = f"""---
tags: [source]
domain: {domain_slug}
domain_confidence: {domain_meta.get('confidence', 'unknown')}
domain_reason: "{str(domain_meta.get('reason', '')).replace('"', "'")}"
shared_scope: domain
source_paths: ["../../../../raw/{raw_rel_path}"]
status: active
---

# {title}

## Summary

{summary}

## Source Metadata

- Raw path: ../../raw/{raw_rel_path}
- Source type: {source_profile.get('source_type_label', 'Unknown')}
- Extraction quality: {source_profile.get('quality_label', 'unknown')}
- Domain: {domain_slug}
- Domain confidence: {domain_meta.get('confidence', 'unknown')}
- Author:
- Published:
- Ingested: {dt.date.today().isoformat()}
- Intake assessment: {'[' + staging_page_rel + '](' + wiki_rel_link(wiki_rel_path, staging_page_rel) + ')' if staging_page_rel else 'None'}

## Key Points

{key_points_md}

## Evidence / Notes

- Generated from the uploaded raw source file.

## Contradictions / Tensions

{contradictions_md}

## Related Entities

{entities_md}

## Related Concepts

{concepts_md}

## Workbook Sheets

{sheet_md}

## Wiki Updates

- [overview](../overview.md)

## Open Questions

{questions_md}
"""
    wiki_path.write_text(content, encoding="utf-8")
    rebuild_index_page()
    return wiki_rel_path


def write_workbook_sheet_pages(
    raw_rel_path: str,
    source_page_rel: str,
    source_title: str,
    workbook_info: dict[str, object],
    source_data: dict[str, object],
    *,
    domain_slug: str,
) -> list[str]:
    sheets = workbook_info.get("sheets", [])
    if not isinstance(sheets, list) or not sheets:
        return []
    summaries = source_data.get("sheet_summaries", [])
    summary_map: dict[str, dict[str, object]] = {}
    if isinstance(summaries, list):
        for item in summaries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if name:
                summary_map[name] = item

    raw_path = RAW_ROOT / raw_rel_path
    workbook_slug = slugify(raw_path.stem)
    domain_slug = ensure_domain(domain_slug)
    touched: list[str] = []
    for sheet in sheets[:24]:
        if not isinstance(sheet, dict):
            continue
        sheet_name = str(sheet.get("name", "")).strip()
        if not sheet_name:
            continue
        rel_path = domain_rel(domain_slug, "sources", f"{workbook_slug}-{slugify(sheet_name)}.md")
        path = WIKI_ROOT / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        item = summary_map.get(sheet_name, {})
        summary = str(item.get("summary", "")).strip() or f"Sheet from workbook {source_title}."
        highlights = sanitize_list(item.get("highlights", []))
        rows = sheet.get("sample_rows", [])
        rows_md = "\n".join(f"- {str(row)}" for row in rows[:12]) or "- No representative rows captured."
        highlights_md = "\n".join(f"- {point}" for point in highlights[:8]) or "- No highlights captured."
        columns = sheet.get("columns", [])
        columns_md = ", ".join(str(col) for col in columns[:20]) or "None."
        content = f"""---
tags: [source, workbook-sheet]
domain: {domain_slug}
shared_scope: domain
source_paths: ["../../../../raw/{raw_rel_path}"]
status: active
---

# {source_title} · {sheet_name}

## Summary

{summary}

## Source Metadata

Raw workbook path: ../../../../raw/{raw_rel_path}
Parent source page: [{source_page_rel}]({wiki_rel_link(rel_path, source_page_rel)})
Sheet name: {sheet_name}
Row count: {sheet.get("row_count", 0)}
Column count: {sheet.get("column_count", 0)}
Columns: {columns_md}
Ingested: {dt.date.today().isoformat()}

## Key Points

{highlights_md}

## Sample Rows

{rows_md}

## Links

- [{source_title}]({wiki_rel_link(rel_path, source_page_rel)})
- [overview](../overview.md)

## Open Questions

- None yet.
"""
        path.write_text(content, encoding="utf-8")
        touched.append(rel_path)
    return touched


def upsert_knowledge_page(
    section: str,
    item: dict[str, object],
    source_page_rel: str,
    source_title: str,
    pending_slugs: set[str] | None = None,
    *,
    domain_slug: str = "",
    shared_scope: str = "domain",
) -> str:
    pending = pending_slugs or set()
    slug = slugify(str(item["name"]))
    if shared_scope == "global":
        rel_path = f"global/{section}/{slug}.md"
    else:
        domain_slug = ensure_domain(domain_slug or domain_from_rel_path(source_page_rel) or "unclassified")
        rel_path = domain_rel(domain_slug, section, f"{slug}.md")
    path = WIKI_ROOT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    title = str(item["name"]).strip()
    summary = str(item.get("summary", "")).strip() or f"AI-maintained {section[:-1]} page."
    facts = sanitize_list(item.get("facts", []))
    links = sanitize_list(item.get("links", []))

    facts_md = "\n".join(f"- {fact}" for fact in facts) or "- No extracted notes yet."
    link_lines = []
    for link in links:
        link_slug = slugify(link)
        candidates = []
        if shared_scope != "global" and domain_slug:
            candidates.extend([
                WIKI_ROOT / domain_rel(domain_slug, "entities", f"{link_slug}.md"),
                WIKI_ROOT / domain_rel(domain_slug, "concepts", f"{link_slug}.md"),
            ])
        candidates.extend([
            WIKI_ROOT / "global" / "entities" / f"{link_slug}.md",
            WIKI_ROOT / "global" / "concepts" / f"{link_slug}.md",
        ])
        existing_candidate = next((candidate for candidate in candidates if candidate.exists()), None)
        if existing_candidate:
            rel = os.path.relpath(existing_candidate, path.parent).replace(os.sep, "/")
        elif link_slug in pending:
            rel = f"../concepts/{link_slug}.md" if section == "concepts" else f"../entities/{link_slug}.md"
        else:
            link_lines.append(f"- {link}")
            continue
        link_lines.append(f"- [{link}]({rel})")
    source_link = wiki_rel_link(rel_path, source_page_rel)
    links_md = "\n".join(link_lines) or f"- [{source_title}]({source_link})"

    exists = path.exists()
    existing = path.read_text(encoding="utf-8") if exists else ""
    config = vertex_config()
    if exists and config.configured:
        rewritten = vertex_rewrite_markdown_page(
            config,
            page_kind=section[:-1],
            title=title,
            current_markdown=existing,
            source_title=source_title,
            source_page_link=source_link,
            source_summary=summary,
            facts=facts,
            links=links,
        )
        path.write_text(repair_internal_links(rel_path, rewritten, pending), encoding="utf-8")
    else:
        content = f"""---
tags: [{section[:-1]}]
domain: {domain_slug or "global"}
shared_scope: {shared_scope}
source_paths: ["{source_link}"]
status: active
---

# {title}

## Summary

{summary}

## Key Points

{facts_md}

## Evidence / Notes

- Updated from [{source_title}]({source_link}).

## Links

{links_md}

## Sources

- [{source_title}]({source_link})

## Open Questions

- None yet.
"""
        path.write_text(content, encoding="utf-8")

    rebuild_index_page()
    return rel_path


def update_overview_page(overview_update: str, source_page_rel: str, source_title: str, *, domain_slug: str = "") -> str:
    domain_slug = ensure_domain(domain_slug or domain_from_rel_path(source_page_rel) or "unclassified")
    overview_rel = domain_overview_rel(domain_slug)
    overview_path = WIKI_ROOT / overview_rel
    existing = overview_path.read_text(encoding="utf-8") if overview_path.exists() else "# Overview\n"
    config = vertex_config()
    source_link = wiki_rel_link(overview_rel, source_page_rel)
    if overview_path.exists() and config.configured:
        rewritten = vertex_rewrite_markdown_page(
            config,
            page_kind="overview",
            title=domain_title_from_slug(domain_slug),
            current_markdown=existing,
            source_title=source_title,
            source_page_link=source_link,
            source_summary=overview_update.strip() or "Incremental update for the top-level synthesis.",
            facts=[overview_update.strip()] if overview_update.strip() else [],
            links=[],
        )
        overview_path.write_text(repair_internal_links(overview_rel, rewritten), encoding="utf-8")
    else:
        addition = (
            f"\n### Update From {dt.date.today().isoformat()} · [{source_title}]({source_link})\n\n"
            f"{overview_update.strip() or 'This source contributed incremental updates to the wiki synthesis.'}\n"
        )
        if "## Evidence / Notes" in existing:
            existing = existing.replace("## Evidence / Notes\n\n", "## Evidence / Notes\n\n" + addition, 1)
        else:
            existing += "\n## Evidence / Notes\n" + addition
        overview_path.write_text(existing, encoding="utf-8")
    rebuild_root_overview()
    rebuild_index_page()
    return overview_rel


def write_revision_manifest(
    raw_relative_path: str,
    source_page_rel: str,
    touched_pages: list[str],
    source_data: dict[str, object],
    *,
    domain_meta: dict[str, object] | None = None,
    staged_pages: list[str] | None = None,
) -> str:
    revision_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slugify(Path(raw_relative_path).stem)}"
    path = REVISIONS_ROOT / f"{revision_id}.json"
    domain_meta = domain_meta or {}
    domain_slug = str(domain_meta.get("domain_slug", "")).strip()
    manifest = {
        "id": revision_id,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "raw_source": raw_relative_path,
        "source_page": source_page_rel,
        "touched_pages": touched_pages,
        "domain": domain_slug,
        "domain_confidence": domain_meta.get("confidence", ""),
        "domain_reason": domain_meta.get("reason", ""),
        "new_domain_created": bool(domain_meta.get("new_domain", False)),
        "domain_pages_touched": [page for page in touched_pages if domain_slug and page.startswith(f"domains/{domain_slug}/")],
        "global_pages_touched": [page for page in touched_pages if page.startswith("global/")],
        "staged_pages": staged_pages or [page for page in touched_pages if page.startswith("staging/")],
        "archived_pages": [page for page in touched_pages if page.startswith("archive/")],
        "summary": str(source_data.get("summary", "")).strip(),
        "key_points": sanitize_list(source_data.get("key_points", [])),
        "entities": [item["name"] for item in sanitize_page_items(source_data.get("entities", []))],
        "concepts": [item["name"] for item in sanitize_page_items(source_data.get("concepts", []))],
        "open_questions": sanitize_list(source_data.get("open_questions", [])),
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return revision_id


def ingest_raw_file(raw_relative_path: str) -> str:
    target = normalize_repo_path(RAW_ROOT, raw_relative_path)
    if target is None:
        raise RuntimeError("Invalid raw path")
    log_event("ingest:start", raw_relative_path)

    config = vertex_config()
    if not config.configured:
        raise RuntimeError("Vertex is not fully configured. Set VERTEX_API, MODEL_ID, and PROJECT_ID in .env.")

    raw_text = read_text_file(target, for_ingest=True)
    if not raw_text.strip():
        raise RuntimeError("Only text-like sources can be ingested in this version.")
    workbook_info = extract_excel_workbook(target) if target.suffix.lower() in {".xlsx", ".xls"} else None
    source_profile = build_source_profile(target, raw_text, workbook_info)
    source_title = derive_title_from_raw(target)

    if source_profile["quality_label"] == "unusable":
        staging_page_rel = write_staging_source_page(
            target.relative_to(RAW_ROOT).as_posix(),
            source_profile=source_profile,
        )
        append_log(
            "stage",
            source_title,
            f"Held {raw_relative_path} in staging because extraction was unusable.",
            [staging_page_rel],
        )
        log_event("ingest:staged", f"{raw_relative_path} -> {staging_page_rel} | unusable extraction")
        return staging_page_rel

    domain_meta = vertex_classify_source_domain(
        config,
        title=source_title,
        raw_path=target,
        raw_text=raw_text,
        source_profile=source_profile,
    )
    if float(domain_meta.get("confidence", 0.0) or 0.0) < DOMAIN_CONFIDENCE_THRESHOLD or not str(domain_meta.get("domain_slug", "")).strip():
        source_profile = {**source_profile, "decision": "domain-review"}
        staging_page_rel = write_staging_source_page(
            target.relative_to(RAW_ROOT).as_posix(),
            source_profile=source_profile,
            domain_meta=domain_meta,
            domain_review=True,
        )
        append_log(
            "stage",
            source_title,
            f"Held {raw_relative_path} for domain review because classification confidence was {domain_meta.get('confidence', 0)}.",
            [staging_page_rel],
        )
        log_event("ingest:domain-review", f"{raw_relative_path} -> {staging_page_rel} | confidence={domain_meta.get('confidence', 0)}")
        return staging_page_rel

    domain_slug = ensure_domain(
        str(domain_meta.get("domain_slug", "")),
        str(domain_meta.get("domain_title", "")),
        str(domain_meta.get("reason", "")),
    )
    domain_meta["domain_slug"] = domain_slug

    all_pages = scan_wiki_pages()
    existing_entities = [p.title for p in all_pages if p.kind == "entity" and p.domain in {domain_slug, "global"}]
    existing_concepts = [p.title for p in all_pages if p.kind == "concept" and p.domain in {domain_slug, "global"}]

    source_data = vertex_generate_structured_summary(
        config=config,
        title=source_title,
        raw_path=target,
        raw_text=raw_text,
        workbook_info=workbook_info,
        source_profile=source_profile,
        existing_entities=existing_entities,
        existing_concepts=existing_concepts,
    )
    staging_page_rel = write_staging_source_page(
        target.relative_to(RAW_ROOT).as_posix(),
        source_profile=source_profile,
        source_data=source_data,
        domain_meta=domain_meta,
    )

    if not should_promote_source(source_profile):
        append_log(
            "stage",
            source_title,
            f"Held {raw_relative_path} in staging because extraction quality was {source_profile['quality_label']}.",
            [staging_page_rel],
        )
        log_event("ingest:staged", f"{raw_relative_path} -> {staging_page_rel} | quality={source_profile['quality_label']}")
        return staging_page_rel

    source_page_rel = write_ai_source_page(
        target.relative_to(RAW_ROOT).as_posix(),
        source_data,
        domain_slug=domain_slug,
        domain_meta=domain_meta,
        source_profile=source_profile,
        staging_page_rel=staging_page_rel,
    )
    staging_page_rel = write_staging_source_page(
        target.relative_to(RAW_ROOT).as_posix(),
        source_profile=source_profile,
        source_data=source_data,
        domain_meta=domain_meta,
        promoted_page_rel=source_page_rel,
    )
    touched_pages = [staging_page_rel, source_page_rel]
    if workbook_info and int(workbook_info.get("sheet_count", 0)) > 0:
        sheet_pages = write_workbook_sheet_pages(
            target.relative_to(RAW_ROOT).as_posix(),
            source_page_rel,
            source_title,
            workbook_info,
            source_data,
            domain_slug=domain_slug,
        )
        if sheet_pages:
            touched_pages.extend(sheet_pages)
            source_page_rel = write_ai_source_page(
                target.relative_to(RAW_ROOT).as_posix(),
                source_data,
                domain_slug=domain_slug,
                domain_meta=domain_meta,
                sheet_page_links=sheet_pages,
                source_profile=source_profile,
                staging_page_rel=staging_page_rel,
            )
            touched_pages[1] = source_page_rel
            staging_page_rel = write_staging_source_page(
                target.relative_to(RAW_ROOT).as_posix(),
                source_profile=source_profile,
                source_data=source_data,
                domain_meta=domain_meta,
                promoted_page_rel=source_page_rel,
            )
            touched_pages[0] = staging_page_rel

    pending_slugs = {slugify(str(item["name"])) for item in sanitize_page_items(source_data.get("entities", [])) + sanitize_page_items(source_data.get("concepts", []))}

    for entity in sanitize_page_items(source_data.get("entities", [])):
        touched_pages.append(upsert_knowledge_page("entities", entity, source_page_rel, source_title, pending_slugs, domain_slug=domain_slug))
    for concept in sanitize_page_items(source_data.get("concepts", [])):
        touched_pages.append(upsert_knowledge_page("concepts", concept, source_page_rel, source_title, pending_slugs, domain_slug=domain_slug))

    touched_pages.append(update_overview_page(str(source_data.get("overview_update", "")).strip(), source_page_rel, source_title, domain_slug=domain_slug))
    touched_pages.append("index.md")
    revision_id = write_revision_manifest(raw_relative_path, source_page_rel, sorted(set(touched_pages)), source_data, domain_meta=domain_meta)
    append_log(
        "ingest",
        source_title,
        f"Ingested {raw_relative_path} with Vertex and updated {len(set(touched_pages))} wiki pages. Revision: {revision_id}.",
        sorted(set(touched_pages)),
    )
    log_event("ingest:done", f"{raw_relative_path} -> {source_page_rel} | pages={len(set(touched_pages))} | revision={revision_id}")
    return source_page_rel


def recent_revisions(limit: int = 8) -> list[dict[str, object]]:
    if not REVISIONS_ROOT.exists():
        return []
    revisions = []
    for path in sorted(REVISIONS_ROOT.glob("*.json"), reverse=True):
        try:
            revisions.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
        if len(revisions) >= limit:
            break
    return revisions


def process_batch_job(job: dict[str, object]) -> None:
    batch_id = str(job["id"])
    text_paths = [str(item) for item in job.get("text_paths", [])]
    saved_paths = [str(item) for item in job.get("saved_paths", [])]
    log_event("batch:start", f"{batch_id} | saved={len(saved_paths)} | ingest={len(text_paths)}")
    update_status(
        active=True,
        batch_id=batch_id,
        job_type="ingest",
        job_label=batch_job_label("ingest"),
        phase="ingesting",
        saved_count=len(saved_paths),
        total_items=len(saved_paths),
        ingest_total=len(text_paths),
        ingest_completed=0,
        failure_count=0,
        current_file="",
        current_step="Generating source summaries and updating wiki pages",
        progress_label=f"Saved {len(saved_paths)} / {len(saved_paths)} · Ingested 0 / {len(text_paths)}",
        error="",
        started_at=str(job.get("started_at", dt.datetime.now().isoformat(timespec="seconds"))),
        finished_at="",
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=f"Batch {batch_id} is ingesting.",
        event=f"batch {batch_id} ingesting",
    )
    update_batch(batch_id, status="running", phase="ingesting", started_processing_at=dt.datetime.now().isoformat(timespec="seconds"))

    successes: list[str] = []
    failures: list[str] = []
    revisions_before = len(recent_revisions(5000))

    for index, rel_path in enumerate(text_paths, start=1):
        update_status(
            batch_id=batch_id,
            phase="ingesting",
            current_file=rel_path,
            ingest_completed=len(successes),
            failure_count=len(failures),
            current_step=f"Ingesting file {index} of {len(text_paths)}",
            progress_label=f"Saved {len(saved_paths)} / {len(saved_paths)} · Ingested {len(successes)} / {len(text_paths)}",
            queue_depth=INGEST_QUEUE.qsize(),
            last_event=f"Ingesting {index}/{len(text_paths)}: {rel_path}",
            event=f"{index}/{len(text_paths)} {rel_path}",
        )
        update_batch(batch_id, current_file=rel_path, ingest_completed=len(successes), failure_count=len(failures), current_step=f"Ingesting file {index} of {len(text_paths)}")
        try:
            source_page_rel = ingest_raw_file(rel_path)
            successes.append(source_page_rel)
            update_status(
                batch_id=batch_id,
                phase="ingesting",
                current_file=rel_path,
                ingest_completed=len(successes),
                current_step=f"Completed file {index} of {len(text_paths)}",
                progress_label=f"Saved {len(saved_paths)} / {len(saved_paths)} · Ingested {len(successes)} / {len(text_paths)}",
                last_event=f"Completed {index}/{len(text_paths)}: {rel_path}",
                event=f"completed {rel_path}",
            )
        except RuntimeError as exc:
            message = f"{rel_path}: {str(exc)}"
            failures.append(message)
            log_event("ingest:error", message)
            update_status(
                batch_id=batch_id,
                phase="ingesting",
                current_file=rel_path,
                failure_count=len(failures),
                current_step=f"Failed file {index} of {len(text_paths)}",
                progress_label=f"Saved {len(saved_paths)} / {len(saved_paths)} · Ingested {len(successes)} / {len(text_paths)}",
                last_event=f"Failed {index}/{len(text_paths)}: {rel_path}",
                event=f"failed {rel_path}",
            )
        update_batch(
            batch_id,
            ingest_completed=len(successes),
            failure_count=len(failures),
            successes=successes[-200:],
            failures=failures[-200:],
        )

    maintenance_report = ""
    try:
        report = run_lint_pass()
        maintenance_report = write_lint_report(report)
        log_event("batch:maintenance", f"{batch_id} -> {maintenance_report}")
    except Exception as exc:
        failures.append(f"maintenance: {str(exc)}")
        log_event("batch:maintenance-error", f"{batch_id} | {str(exc)}")

    revisions_after = len(recent_revisions(5000))
    update_batch(
        batch_id,
        status="completed",
        phase="done",
        current_file="",
        completed_at=dt.datetime.now().isoformat(timespec="seconds"),
        ingest_completed=len(successes),
        failure_count=len(failures),
        successes=successes[-200:],
        failures=failures[-200:],
        maintenance_report=maintenance_report,
        revisions_created=max(0, revisions_after - revisions_before),
    )
    finish_batch_status(
        f"Batch {batch_id} finished. Saved {len(saved_paths)} files, ingested {len(successes)}, failures {len(failures)}, maintenance {maintenance_report or 'none'}."
    )
    log_event("batch:done", f"{batch_id} | ingested={len(successes)} | failures={len(failures)}")


def enqueue_notion_ingest_batch(workspace_name: str, text_paths: list[str]) -> str:
    """
    Run the same ingest batch pipeline as file uploads for raw paths already on disk
    (e.g. after scripts/notion_sync.py). Writes a batch under .batches/ and processes it
    synchronously so the Batches page shows one completed job without requiring the HTTP server.
    Returns batch_id, or "" if nothing ingestible.
    """
    normalized = workspace_slug(workspace_name)
    configure_workspace(normalized)
    ensure_directories()
    filtered: list[str] = []
    for rel in text_paths:
        rel_clean = str(rel).strip().lstrip("/")
        if not rel_clean:
            continue
        target = normalize_repo_path(RAW_ROOT, rel_clean)
        if target is None:
            continue
        if should_ingest_path(target) and (
            target.suffix.lower() not in {".pdf", ".xlsx", ".xls"} or bool(read_text_file(target).strip())
        ):
            filtered.append(rel_clean)
    if not filtered:
        return ""
    batch_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    now = dt.datetime.now().isoformat(timespec="seconds")
    batch_payload: dict[str, object] = {
        "id": batch_id,
        "job_type": "ingest",
        "status": "queued",
        "phase": "queued",
        "created_at": now,
        "workspace": normalized,
        "started_at": now,
        "completed_at": "",
        "saved_count": len(filtered),
        "total_items": len(filtered),
        "ingest_total": len(filtered),
        "ingest_completed": 0,
        "failure_count": 0,
        "saved_paths": filtered,
        "text_paths": filtered,
        "skipped_paths": [],
        "current_file": "",
        "successes": [],
        "failures": [],
        "maintenance_report": "",
        "revisions_created": 0,
    }
    write_batch(batch_id, batch_payload)
    process_batch_job(batch_payload)
    return batch_id


def process_review_job(job: dict[str, object]) -> None:
    batch_id = str(job["id"])
    log_event("review-job:start", batch_id)
    pages = pages_for_maintenance()
    lint_report = run_lint_pass()
    update_status(
        active=True,
        batch_id=batch_id,
        job_type="review",
        job_label=batch_job_label("review"),
        phase="reviewing",
        current_file="wiki review",
        current_step="Auditing the wiki and planning maintenance",
        progress_label=f"Pages scanned {len(pages)} · Questions 0",
        pages_scanned=len(pages),
        question_count=0,
        updated_pages=0,
        error="",
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=f"Running wiki review job {batch_id}.",
        event=f"review job {batch_id} started",
    )
    update_batch(
        batch_id,
        status="running",
        phase="reviewing",
        started_processing_at=dt.datetime.now().isoformat(timespec="seconds"),
        pages_scanned=len(pages),
        current_step="Auditing the wiki and planning maintenance",
        lint_report=lint_report,
    )
    config = vertex_config()
    result = vertex_plan_wiki_maintenance(config, pages, lint_report)
    review_page = write_review_page(result, pages)
    questions = sanitize_list(result.get("clarifying_questions", []))
    directives = maintenance_directives(result)
    write_review_state(
        {
            "pending": bool(questions),
            "questions": questions,
            "review_page": review_page,
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "context_pages": [page.rel_path for page in pages[:10]],
        }
    )
    touched_pages: list[str] = []
    action_log: list[dict[str, str]] = []
    page_map = page_lookup(pages)
    queued_directives = directives[:]
    if not queued_directives:
        queued_directives = [{"path": page.rel_path, "action": "rewrite", "reason": "", "instruction": "", "target_title": "", "merge_into": "", "source_paths": []} for page in pages]

    total_pages = len(queued_directives)
    for index, directive in enumerate(queued_directives, start=1):
        rel_path = str(directive.get("path", "")).strip()
        action = str(directive.get("action", "rewrite")).strip().lower() or "rewrite"
        update_status(
            batch_id=batch_id,
            phase="reviewing",
            current_file=rel_path,
            current_step=f"{action.title()} page {index} of {total_pages}",
            progress_label=f"Pages scanned {len(pages)} · Updated {len(touched_pages)} / {total_pages}",
            pages_scanned=len(pages),
            updated_pages=len(touched_pages),
            question_count=len(questions),
            last_event=f"{action.title()} {rel_path}",
            event=f"maintenance {action} {rel_path}",
        )
        update_batch(
            batch_id,
            current_file=rel_path,
            current_step=f"{action.title()} page {index} of {total_pages}",
            updated_pages=len(touched_pages),
            questions=questions,
        )
        page = page_map.get(rel_path)
        related_rel_paths = sanitize_list(directive.get("source_paths", []))
        related_pages = [page_map[item] for item in related_rel_paths if item in page_map]
        if not related_pages:
            related_pages = [candidate for candidate in pages if candidate.rel_path != rel_path][:6]

        if action == "keep":
            action_log.append({"path": rel_path, "action": action, "note": "Left unchanged."})
            continue
        if action in {"delete", "archive"} and page:
            output_rel = archive_or_delete_page(rel_path, mode=action, reason=str(directive.get("reason", "")).strip())
            touched_pages.append(output_rel)
            action_log.append({"path": rel_path, "action": action, "note": str(directive.get("reason", "")).strip() or action})
            log_event("review-job:crud", f"{batch_id} | {action} | {rel_path}")
            continue
        if action == "merge" and page:
            merge_target = str(directive.get("merge_into", "")).strip()
            if merge_target and merge_target in page_map:
                rewritten = vertex_rewrite_page_for_maintenance(config, page=page_map[merge_target], plan=result, related_pages=[page] + related_pages)
                page_map[merge_target].path.write_text(repair_internal_links(merge_target, rewritten), encoding="utf-8")
                touched_pages.append(merge_target)
                archive_or_delete_page(rel_path, mode="archive", reason=f"Merged into {merge_target}")
                touched_pages.append(f"archive/{rel_path.replace('/', '--')}")
                action_log.append({"path": rel_path, "action": action, "note": f"Merged into {merge_target}"})
                log_event("review-job:crud", f"{batch_id} | merge | {rel_path} -> {merge_target}")
                continue
        if action == "create" and not page:
            target = WIKI_ROOT / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            title = str(directive.get("target_title", "")).strip() or Path(rel_path).stem.replace("-", " ").title()
            created = vertex_create_page_for_maintenance(config, rel_path=rel_path, title=title, plan=result, directive=directive, related_pages=related_pages)
            target.write_text(created, encoding="utf-8")
            touched_pages.append(rel_path)
            action_log.append({"path": rel_path, "action": action, "note": str(directive.get("reason", "")).strip() or "Created during maintenance."})
            log_event("review-job:crud", f"{batch_id} | create | {rel_path}")
            continue
        if page:
            rewritten = vertex_rewrite_page_for_maintenance(config, page=page, plan=result, related_pages=related_pages)
            page.path.write_text(rewritten, encoding="utf-8")
            touched_pages.append(page.rel_path)
            action_log.append({"path": page.rel_path, "action": "rewrite", "note": str(directive.get("reason", "")).strip() or "Rewritten during maintenance."})
            log_event("review-job:rewrite", f"{batch_id} | {page.rel_path}")

    touched_pages.append(rebuild_index_page())
    post_lint_report = run_lint_pass()
    maintenance_lint_page = write_lint_report(post_lint_report)
    touched_pages.append(maintenance_lint_page)
    revision_id = write_maintenance_revision_manifest(result, sorted(set(touched_pages)), review_page, action_log)
    update_batch(
        batch_id,
        status="completed",
        phase="done",
        completed_at=dt.datetime.now().isoformat(timespec="seconds"),
        review_page=review_page,
        questions=questions,
        touched_pages=sorted(set(touched_pages)),
        updated_pages=len(set(touched_pages)),
        maintenance_revision=revision_id,
        maintenance_lint_page=maintenance_lint_page,
        actions=action_log,
    )
    update_status(
        batch_id=batch_id,
        phase="done",
        current_file=review_page,
        current_step="Review completed",
        progress_label=f"Pages scanned {len(pages)} · Rewritten {len(set(touched_pages))}",
        pages_scanned=len(pages),
        question_count=len(questions),
        updated_pages=len(set(touched_pages)),
        last_event=f"Review repaired {len(set(touched_pages))} pages and left {len(questions)} clarifying questions.",
        event=f"review completed {batch_id}",
    )
    append_log(
        "review-repair",
        str(result.get("title", "")).strip() or "Wiki maintenance",
        f"Ran a wiki-wide maintenance pass, touched {len(set(touched_pages))} pages, and left {len(questions)} clarifying questions. Revision: {revision_id}.",
        [review_page] + sorted(set(touched_pages))[:11],
    )
    finish_batch_status(f"Review job {batch_id} finished. Repaired {len(set(touched_pages))} pages. Review page: {review_page}. Pending questions: {len(questions)}.")
    log_event("review-job:done", f"{batch_id} -> {review_page} | repaired={len(set(touched_pages))} | questions={len(questions)} | revision={revision_id}")


def process_clarification_job(job: dict[str, object]) -> None:
    batch_id = str(job["id"])
    message = str(job.get("message", ""))
    tagged = [str(item) for item in job.get("tagged_pages", [])]
    assistant_message_id = str(job.get("assistant_message_id", ""))
    log_event("clarify-job:start", batch_id)
    update_status(
        active=True,
        batch_id=batch_id,
        job_type="clarification",
        job_label=batch_job_label("clarification"),
        phase="clarifying",
        current_file="review clarification",
        current_step="Applying user clarification to the wiki",
        progress_label=f"Tagged docs {len(tagged)} · Pages updated 0",
        tagged_count=len(tagged),
        updated_pages=0,
        error="",
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=f"Applying review clarification job {batch_id}.",
        event=f"clarification job {batch_id} started",
    )
    update_batch(batch_id, status="running", phase="clarifying", started_processing_at=dt.datetime.now().isoformat(timespec="seconds"), tagged_count=len(tagged), current_step="Applying user clarification to the wiki")
    config = vertex_config()
    review_state = read_review_state()
    tagged_pages = [page for page in scan_wiki_pages() if page.rel_path in tagged]
    result = vertex_apply_clarification_message(config, review_state, message, tagged_pages)
    touched = apply_clarification_updates(result, str(review_state.get("review_page", "")))
    summary = str(result.get("summary", "")).strip() or "Applied clarification answers to the wiki."
    append_log("review-apply", "Apply clarifications", summary, touched[:8])
    write_review_state({"pending": False, "questions": [], "review_page": "", "created_at": "", "context_pages": []})
    update_batch(
        batch_id,
        status="completed",
        phase="done",
        completed_at=dt.datetime.now().isoformat(timespec="seconds"),
        touched_pages=touched,
        summary=summary,
    )
    update_status(
        batch_id=batch_id,
        phase="done",
        current_file="review clarification",
        current_step="Clarification applied",
        progress_label=f"Tagged docs {len(tagged)} · Pages updated {len(touched)}",
        tagged_count=len(tagged),
        updated_pages=len(touched),
        last_event=f"Clarification updated {len(touched)} wiki pages.",
        event=f"clarification completed {batch_id}",
    )
    finish_batch_status(f"Clarification job {batch_id} finished. Updated {len(touched)} wiki pages.")
    if assistant_message_id:
        response_md = (
            f"{summary}\n\n"
            f"Updated wiki pages:\n{markdown_links_for_pages(sorted(set(touched))[:10])}"
        )
        update_chat_message(assistant_message_id, status="done", content_md=response_md, related_pages=sorted(set(touched)))
    log_event("clarify-job:done", f"{batch_id} | touched={len(touched)}")


def process_query_job(job: dict[str, object]) -> None:
    batch_id = str(job["id"])
    question = str(job.get("question", "")).strip()
    tagged = [str(item) for item in job.get("tagged_pages", [])]
    assistant_message_id = str(job.get("assistant_message_id", ""))
    context_pages = [page for page in scan_wiki_pages() if page.rel_path in tagged] if tagged else select_relevant_wiki_pages(question)
    log_event("query-job:start", f"{batch_id} | context={len(context_pages)}")
    update_status(
        active=True,
        batch_id=batch_id,
        job_type="query",
        job_label=batch_job_label("query"),
        phase="querying",
        current_file=question[:120] or "wiki query",
        current_step="Answering question from the wiki",
        progress_label=f"Context pages {len(context_pages)}",
        context_count=len(context_pages),
        error="",
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=f"Running wiki query job {batch_id}.",
        event=f"query job {batch_id} started",
    )
    update_batch(
        batch_id,
        status="running",
        phase="querying",
        started_processing_at=dt.datetime.now().isoformat(timespec="seconds"),
        context_count=len(context_pages),
        current_step="Answering question from the wiki",
    )
    config = vertex_config()
    if not context_pages:
        raise RuntimeError("No relevant wiki pages found yet.")
    result = vertex_answer_query(config, question, context_pages)
    query_rel_path = write_query_page(question, result, context_pages)
    related_pages = sanitize_list(result.get("related_pages", [])) or [page.rel_path for page in context_pages]
    update_batch(
        batch_id,
        status="completed",
        phase="done",
        completed_at=dt.datetime.now().isoformat(timespec="seconds"),
        query_page=query_rel_path,
    )
    update_status(
        batch_id=batch_id,
        phase="done",
        current_file=query_rel_path,
        current_step="Query filed into wiki",
        progress_label=f"Context pages {len(context_pages)}",
        context_count=len(context_pages),
        last_event=f"Query result written to {query_rel_path}.",
        event=f"query completed {batch_id}",
    )
    if assistant_message_id:
        answer_md = str(result.get("answer_md", "")).strip() or str(result.get("summary", "")).strip() or "No answer generated."
        response_md = (
            f"{answer_md}\n\n"
            f"Related docs:\n{markdown_links_for_pages(related_pages[:8])}\n\n"
            f"Filed artifact: [{query_rel_path}](../{query_rel_path})"
        )
        update_chat_message(assistant_message_id, status="done", content_md=response_md, related_pages=related_pages[:8])
    finish_batch_status(f"Query job {batch_id} finished. Result: {query_rel_path}.")
    log_event("query-job:done", f"{batch_id} -> {query_rel_path}")


def process_action_job(job: dict[str, object]) -> None:
    batch_id = str(job["id"])
    message = str(job.get("message", "")).strip()
    tagged = [str(item) for item in job.get("tagged_pages", [])]
    action_hint = str(job.get("action_hint", "")).strip()
    assistant_message_id = str(job.get("assistant_message_id", ""))
    pages = scan_wiki_pages()
    tagged_pages = [page for page in pages if page.rel_path in tagged]
    context_pages = tagged_pages or resolve_pages_from_message(message) or select_relevant_wiki_pages(message)
    log_event("action-job:start", f"{batch_id} | hint={action_hint or 'auto'} | context={len(context_pages)}")
    update_status(
        active=True,
        batch_id=batch_id,
        job_type="action",
        job_label=batch_job_label("action"),
        phase="planning",
        current_file=message[:120] or "wiki action",
        current_step="Planning wiki document actions",
        progress_label=f"Actions 0 · Pages changed 0",
        planned_actions=0,
        updated_pages=0,
        context_count=len(context_pages),
        error="",
        queue_depth=INGEST_QUEUE.qsize(),
        last_event=f"Planning wiki action job {batch_id}.",
        event=f"action job {batch_id} started",
    )
    update_batch(
        batch_id,
        status="running",
        phase="planning",
        started_processing_at=dt.datetime.now().isoformat(timespec="seconds"),
        context_count=len(context_pages),
        current_step="Planning wiki document actions",
    )
    config = vertex_config()
    result = vertex_plan_assistant_actions(config, message, context_pages)
    directives = maintenance_directives(result)
    if not directives:
        if action_hint == "create":
            directives = [{
                "path": f"queries/{slugify(message[:80])}.md",
                "action": "create",
                "reason": "No explicit target was found; created a new page from the assistant command.",
                "instruction": message,
                "target_title": message[:80],
                "merge_into": "",
                "source_paths": [page.rel_path for page in context_pages[:4]],
            }]
        elif context_pages:
            directives = [{
                "path": context_pages[0].rel_path,
                "action": "rewrite",
                "reason": "The assistant command requested a direct page update.",
                "instruction": message,
                "target_title": "",
                "merge_into": "",
                "source_paths": [page.rel_path for page in context_pages[1:5]],
            }]
        else:
            raise RuntimeError("No actionable wiki page could be identified. Tag docs or name the target page more explicitly.")
    plan = {
        "canonical_scope": str(result.get("canonical_scope", "")).strip() or "Apply the user's requested wiki action while keeping the wiki coherent and source-aware.",
        "global_guidelines": sanitize_list(result.get("global_guidelines", [])) or ["Prefer editing existing pages over creating duplicates.", "Keep links relative and preserve factual grounding."],
        "what_looks_wrong": [str(result.get("summary", "")).strip() or "The target page needs adjustment."],
        "page_directives": directives,
    }
    update_batch(batch_id, phase="working", planned_actions=len(directives), actions=directives, current_step="Applying wiki document actions")
    update_status(
        batch_id=batch_id,
        job_type="action",
        job_label=batch_job_label("action"),
        phase="working",
        current_file=message[:120] or "wiki action",
        current_step="Applying wiki document actions",
        planned_actions=len(directives),
        updated_pages=0,
        progress_label=f"Actions {len(directives)} · Pages changed 0",
        context_count=len(context_pages),
        last_event=f"Applying wiki action job {batch_id}.",
        event=f"action applying {batch_id}",
    )
    touched_pages, action_log = execute_page_directives(
        config,
        batch_id=batch_id,
        directives=directives,
        plan=plan,
        context_pages=context_pages,
        status_job_type="action-job",
    )
    touched_pages.append(rebuild_index_page())
    summary = str(result.get("summary", "")).strip() or "Applied assistant-requested wiki document changes."
    append_log("assistant-action", str(result.get("title", "")).strip() or "Assistant wiki action", summary, sorted(set(touched_pages))[:8])
    update_batch(
        batch_id,
        status="completed",
        phase="done",
        completed_at=dt.datetime.now().isoformat(timespec="seconds"),
        planned_actions=len(directives),
        touched_pages=sorted(set(touched_pages)),
        updated_pages=len(set(touched_pages)),
        summary=summary,
        actions=action_log,
    )
    update_status(
        batch_id=batch_id,
        phase="done",
        current_file=sorted(set(touched_pages))[0] if touched_pages else (message[:120] or "wiki action"),
        current_step="Wiki action applied",
        planned_actions=len(directives),
        updated_pages=len(set(touched_pages)),
        progress_label=f"Actions {len(directives)} · Pages changed {len(set(touched_pages))}",
        last_event=f"Assistant action updated {len(set(touched_pages))} wiki pages.",
        event=f"action completed {batch_id}",
    )
    finish_batch_status(f"Wiki action job {batch_id} finished. Updated {len(set(touched_pages))} wiki pages.")
    if assistant_message_id:
        response_md = (
            f"{summary}\n\n"
            f"Changed pages:\n{markdown_links_for_pages(sorted(set(touched_pages))[:10])}"
        )
        update_chat_message(assistant_message_id, status="done", content_md=response_md, related_pages=sorted(set(touched_pages)))
    log_event("action-job:done", f"{batch_id} | actions={len(directives)} | touched={len(set(touched_pages))}")


def ingest_worker() -> None:
    while True:
        job = INGEST_QUEUE.get()
        try:
            workspace_name = workspace_slug(str(job.get("workspace", CURRENT_WORKSPACE)))
            configure_workspace(workspace_name)
            ensure_directories()
            job_type = str(job.get("job_type", "ingest"))
            if job_type == "ingest":
                process_batch_job(job)
            elif job_type == "review":
                process_review_job(job)
            elif job_type == "clarification":
                process_clarification_job(job)
            elif job_type == "query":
                process_query_job(job)
            elif job_type == "action":
                process_action_job(job)
            else:
                log_event("job:error", f"unknown job type {job_type}")
        except Exception as exc:
            job_id = str(job.get("id", "unknown"))
            log_event("job:fatal", f"{job_id} | {str(exc)}")
            assistant_message_id = str(job.get("assistant_message_id", ""))
            update_batch(
                job_id,
                status="failed",
                phase="failed",
                completed_at=dt.datetime.now().isoformat(timespec="seconds"),
                error=str(exc),
                current_step="Failed",
            )
            update_status(
                active=False,
                batch_id=job_id,
                phase="failed",
                current_step="Failed",
                error=str(exc),
                queue_depth=INGEST_QUEUE.qsize(),
                last_event=f"Job {job_id} failed: {str(exc)}",
                event=f"job failed {job_id}",
            )
            if assistant_message_id:
                update_chat_message(assistant_message_id, status="failed", content_md=f"Request failed.\n\nError: `{str(exc)}`")
            finish_batch_status(f"Job {job_id} failed: {str(exc)}", phase="failed")
        finally:
            INGEST_QUEUE.task_done()


def ensure_worker() -> None:
    global WORKER_STARTED
    if WORKER_STARTED:
        return
    worker = threading.Thread(target=ingest_worker, name="llm-wiki-ingest-worker", daemon=True)
    worker.start()
    WORKER_STARTED = True


def tokenize(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2]


def select_relevant_wiki_pages(question: str, limit: int = 6) -> list[Page]:
    tokens = tokenize(question)
    index_text = INDEX_PATH.read_text(encoding="utf-8").lower() if INDEX_PATH.exists() else ""
    domain_scores: dict[str, int] = {}
    for domain in scan_domains():
        haystack = f"{domain['slug']} {domain['title']} {domain['summary']}".lower()
        score = sum(haystack.count(token) for token in tokens)
        if score:
            domain_scores[domain["slug"]] = score
    scored: list[tuple[int, Page]] = []
    for page in scan_wiki_pages():
        text = read_text_file(page.path).lower()
        score = sum(text.count(token) for token in tokens)
        if page.domain in domain_scores:
            score += domain_scores[page.domain] * 3
        if page.rel_path.lower() in index_text:
            score += 1
        if score > 0:
            scored.append((score, page))
    scored.sort(key=lambda item: (-item[0], item[1].rel_path))
    selected = [page for _, page in scored[:limit]]
    linked_globals: list[Page] = []
    selected_text = "\n".join(read_text_file(page.path) for page in selected)
    for page in scan_wiki_pages():
        if page.domain == "global" and page.rel_path in selected_text and page not in selected:
            linked_globals.append(page)
    return (selected + linked_globals)[:limit]


def normalize_assistant_action(message: str) -> str:
    lowered = message.strip().lower()
    lowered = re.sub(r"^(please|pls|can you|could you|would you|hey|assistant|wiki assistant)\s+", "", lowered)
    match = re.match(r"^(create|update|rewrite|merge|archive|delete|remove|fix|improve)\b", lowered)
    if not match:
        return ""
    verb = match.group(1)
    return {
        "update": "rewrite",
        "rewrite": "rewrite",
        "fix": "rewrite",
        "improve": "rewrite",
        "remove": "delete",
    }.get(verb, verb)


def resolve_pages_from_message(message: str, limit: int = 6) -> list[Page]:
    lowered = message.lower()
    resolved: list[Page] = []
    seen: set[str] = set()
    for page in scan_wiki_pages():
        if page.rel_path.lower() in lowered or page.title.lower() in lowered or Path(page.rel_path).stem.replace("-", " ").lower() in lowered:
            if page.rel_path not in seen:
                resolved.append(page)
                seen.add(page.rel_path)
        if len(resolved) >= limit:
            break
    return resolved


def parse_tag_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def resolve_tagged_pages(raw_tags: str) -> list[Page]:
    tags = parse_tag_list(raw_tags)
    if not tags:
        return []
    pages = scan_wiki_pages()
    by_rel = {page.rel_path.lower(): page for page in pages}
    by_title = {page.title.lower(): page for page in pages}
    resolved: list[Page] = []
    seen: set[str] = set()
    for tag in tags:
        key = tag.lower()
        page = by_rel.get(key) or by_title.get(key)
        if page and page.rel_path not in seen:
            resolved.append(page)
            seen.add(page.rel_path)
    return resolved


def pages_for_maintenance() -> list[Page]:
    candidates: list[Page] = []
    for page in scan_wiki_pages():
        if page.section in {"queries", "archive"}:
            continue
        candidates.append(page)
    overview_path = WIKI_ROOT / "overview.md"
    if overview_path.exists() and not any(page.rel_path == "overview.md" for page in candidates):
        candidates.insert(0, Page(root=WIKI_ROOT, path=overview_path, section="overview", managed_by_ai=True))
    return sorted(candidates, key=lambda page: page.rel_path)


def page_lookup(pages: list[Page]) -> dict[str, Page]:
    return {page.rel_path: page for page in pages}


def infer_section_from_path(rel_path: str) -> str:
    if "/" not in rel_path:
        return "overview"
    return rel_path.split("/", 1)[0]


def summarize_pages_for_review(pages: list[Page], *, max_chars_per_page: int = 1400, max_total_chars: int = 32000) -> str:
    blocks: list[str] = []
    total = 0
    for page in pages:
        excerpt = read_text_file(page.path)[:max_chars_per_page]
        block = f"Page: {page.rel_path}\nTitle: {page.title}\nSummary: {page.summary}\nContent excerpt:\n{excerpt}"
        if total + len(block) > max_total_chars and blocks:
            break
        blocks.append(block)
        total += len(block)
    return "\n\n---\n\n".join(blocks)


def vertex_plan_wiki_maintenance(config: VertexConfig, pages: list[Page], lint_report: dict[str, object]) -> dict[str, object]:
    prompt = textwrap.dedent(
        f"""
        You are the expert curator of a long-lived markdown wiki.
        Your job is to understand the whole wiki, detect mixed domains, weak structure, duplicated concepts, stale framing,
        and missing top-level synthesis, then produce a maintenance plan for rewriting the wiki into a more coherent and useful state.

        Return valid JSON only with this exact schema:
        {{
          "title": "short maintenance title",
          "summary": "one paragraph about the current wiki and needed direction",
          "canonical_scope": "one paragraph describing what this wiki is actually about",
          "what_looks_right": ["item 1", "item 2"],
          "what_looks_wrong": ["item 1", "item 2"],
          "missing_or_unclear": ["item 1", "item 2"],
          "clarifying_questions": ["question 1", "question 2"],
          "global_guidelines": ["guideline 1", "guideline 2", "guideline 3"],
          "priority_pages": ["overview.md", "entities/example.md"],
          "page_directives": [
            {{
              "path": "overview.md",
              "action": "rewrite",
              "reason": "why this page needs work",
              "instruction": "how the page should change",
              "target_title": "optional title for a created page",
              "merge_into": "optional destination page if this page should be merged",
              "source_paths": ["optional related page", "optional second related page"]
            }}
          ]
        }}

        Rules:
        - Assume the user wants one coherent wiki, not disconnected scraps.
        - If the wiki mixes multiple document families, identify the dominant structure and how pages should be reframed.
        - Be opinionated about what should be merged, clarified, tightened, or de-emphasized.
        - Ask clarifying questions only for things that truly block good curation.
        - Use page actions from this set only: create, rewrite, merge, archive, delete, keep.
        - "create" means a new wiki page should be created because the current wiki is missing it.
        - "merge" means the current page should be merged into another page, then removed or archived.
        - "archive" means keep the page out of the active wiki because it is low-value or off-scope.
        - "delete" means the page is noise and should be removed from the wiki.
        - "keep" means the page is already fine and does not need rewriting.
        - Include directives for both existing pages and any missing pages that should be created.
        - Keep page_directives focused on the pages that most need expert attention.

        Lint summary:
        {json.dumps(lint_report, ensure_ascii=False)[:6000]}

        Wiki snapshot:
        {summarize_pages_for_review(pages)}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    body = vertex_request_json(config, payload, label="Wiki maintenance plan", timeout=180, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no maintenance plan.")
    return parse_vertex_json_text(text)


def directive_for_page(plan: dict[str, object], page_rel_path: str) -> dict[str, str]:
    directives = plan.get("page_directives", [])
    if not isinstance(directives, list):
        return {}
    for item in directives:
        if not isinstance(item, dict):
            continue
        if str(item.get("path", "")).strip() == page_rel_path:
            return {
                "action": str(item.get("action", "")).strip(),
                "reason": str(item.get("reason", "")).strip(),
                "instruction": str(item.get("instruction", "")).strip(),
                "target_title": str(item.get("target_title", "")).strip(),
                "merge_into": str(item.get("merge_into", "")).strip(),
            }
    return {}


def maintenance_directives(plan: dict[str, object]) -> list[dict[str, object]]:
    directives = plan.get("page_directives", [])
    if not isinstance(directives, list):
        return []
    cleaned: list[dict[str, object]] = []
    for item in directives:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        action = str(item.get("action", "")).strip().lower()
        if not path or not action:
            continue
        cleaned.append(
            {
                "path": path,
                "action": action,
                "reason": str(item.get("reason", "")).strip(),
                "instruction": str(item.get("instruction", "")).strip(),
                "target_title": str(item.get("target_title", "")).strip(),
                "merge_into": str(item.get("merge_into", "")).strip(),
                "source_paths": sanitize_list(item.get("source_paths", [])),
            }
        )
    return cleaned


def vertex_rewrite_page_for_maintenance(
    config: VertexConfig,
    *,
    page: Page,
    plan: dict[str, object],
    related_pages: list[Page],
) -> str:
    directive = directive_for_page(plan, page.rel_path)
    related_blocks = []
    for related in related_pages[:4]:
        if related.rel_path == page.rel_path:
            continue
        related_blocks.append(
            f"Related page: {related.rel_path}\nTitle: {related.title}\nSummary: {related.summary}\nContent excerpt:\n{read_text_file(related.path)[:1800]}"
        )
    prompt = textwrap.dedent(
        f"""
        You are rewriting one page in a maintained markdown wiki so the whole wiki becomes more coherent and useful.
        Rewrite the page markdown only.

        Requirements:
        - Preserve the core factual content grounded in the current page and related wiki pages.
        - Remove drift, repetition, vague filler, and framing that does not fit the wiki's canonical scope.
        - Make the page clearer, better structured, and more linked to the rest of the wiki.
        - Keep relative markdown links.
        - Do not mention the maintenance plan or the prompt.
        - Do not invent facts not supported by the current page or related pages.

        Canonical scope:
        {str(plan.get("canonical_scope", "")).strip()}

        Global guidelines:
        {chr(10).join(f"- {item}" for item in sanitize_list(plan.get("global_guidelines", []))) or "- Keep pages coherent and specific."}

        What looks wrong globally:
        {chr(10).join(f"- {item}" for item in sanitize_list(plan.get("what_looks_wrong", []))) or "- None."}

        Page directive:
        - action: {directive.get("action", "rewrite")}
        - reason: {directive.get("reason", "Improve clarity and fit with the wiki.")}
        - instruction: {directive.get("instruction", "Tighten and clarify this page for the wiki's actual scope.")}

        Current page path: {page.rel_path}
        Current page title: {page.title}
        Current page markdown:
        {read_text_file(page.path)[:14000]}

        Related wiki context:
        {"\n\n---\n\n".join(related_blocks) or "None."}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    body = vertex_request_json(config, payload, label=f"Wiki maintenance rewrite {page.rel_path}", timeout=180, retries=2)
    text = extract_vertex_text(body, error_message=f"Vertex returned no rewrite for {page.rel_path}.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*|```$", "", text, flags=re.DOTALL).strip()
    return repair_internal_links(page.rel_path, text)


def vertex_create_page_for_maintenance(
    config: VertexConfig,
    *,
    rel_path: str,
    title: str,
    plan: dict[str, object],
    directive: dict[str, object],
    related_pages: list[Page],
) -> str:
    related_blocks = []
    for related in related_pages[:6]:
        related_blocks.append(
            f"Related page: {related.rel_path}\nTitle: {related.title}\nSummary: {related.summary}\nContent excerpt:\n{read_text_file(related.path)[:2200]}"
        )
    prompt = textwrap.dedent(
        f"""
        You are creating a new page in a maintained markdown wiki.
        Write markdown only.

        Requirements:
        - Create a useful page that fits the wiki's canonical scope.
        - Ground the page only in the related wiki context.
        - Make it concise, well-structured, and cross-linked to the existing wiki.
        - Use relative markdown links.
        - Do not invent unsupported facts.

        New page path: {rel_path}
        New page title: {title}

        Canonical scope:
        {str(plan.get("canonical_scope", "")).strip()}

        Global guidelines:
        {chr(10).join(f"- {item}" for item in sanitize_list(plan.get("global_guidelines", []))) or "- Keep pages coherent and specific."}

        Creation directive:
        - reason: {str(directive.get("reason", "")).strip() or "This page is missing."}
        - instruction: {str(directive.get("instruction", "")).strip() or "Create a page that fills a real gap in the wiki."}

        Related wiki context:
        {"\n\n---\n\n".join(related_blocks) or "None."}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    body = vertex_request_json(config, payload, label=f"Wiki maintenance create {rel_path}", timeout=180, retries=2)
    text = extract_vertex_text(body, error_message=f"Vertex returned no created page for {rel_path}.")
    if text.startswith("```"):
        text = re.sub(r"^```(?:markdown|md)?\s*|```$", "", text, flags=re.DOTALL).strip()
    return repair_internal_links(rel_path, text)


def archive_or_delete_page(rel_path: str, *, mode: str, reason: str) -> str:
    target = WIKI_ROOT / rel_path
    if not target.exists():
        return rel_path
    archive_root = WIKI_ROOT / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    if mode == "archive":
        archive_rel = f"archive/{rel_path.replace('/', '--')}"
        archive_path = WIKI_ROOT / archive_rel
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        content = read_text_file(target)
        archive_header = (
            f"# Archived: {extract_title(target)}\n\n"
            f"## Archive Note\n\n"
            f"- Original path: {rel_path}\n"
            f"- Archived: {dt.date.today().isoformat()}\n"
            f"- Reason: {reason or 'Moved out of the active wiki during maintenance.'}\n\n"
            f"## Original Content\n\n"
        )
        archive_path.write_text(archive_header + content, encoding="utf-8")
        target.unlink()
        return archive_rel
    target.unlink()
    return rel_path


def write_maintenance_revision_manifest(
    plan: dict[str, object],
    touched_pages: list[str],
    review_page: str,
    actions: list[dict[str, str]],
) -> str:
    revision_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-wiki-maintenance"
    path = REVISIONS_ROOT / f"{revision_id}.json"
    manifest = {
        "id": revision_id,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "type": "wiki-maintenance",
        "review_page": review_page,
        "summary": str(plan.get("summary", "")).strip(),
        "canonical_scope": str(plan.get("canonical_scope", "")).strip(),
        "priority_pages": sanitize_list(plan.get("priority_pages", [])),
        "clarifying_questions": sanitize_list(plan.get("clarifying_questions", [])),
        "touched_pages": touched_pages,
        "actions": actions,
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return revision_id


def vertex_answer_query(config: VertexConfig, question: str, pages: list[Page]) -> dict[str, object]:
    context_blocks = []
    for page in pages:
        context_blocks.append(
            f"Page: {page.rel_path}\nTitle: {page.title}\nContent:\n{read_text_file(page.path)[:7000]}"
        )
    prompt = textwrap.dedent(
        f"""
        You are answering a question strictly from an existing markdown wiki.
        Produce valid JSON only with this exact schema:
        {{
          "title": "short title",
          "summary": "one short paragraph",
          "answer_md": "markdown answer with inline citations like [Page Title](../sources/example.md)",
          "related_pages": ["sources/example.md", "concepts/example.md"],
          "follow_up_questions": ["question 1", "question 2"]
        }}

        Rules:
        - Answer only from the provided wiki context.
        - If something is uncertain, say so.
        - Use markdown links for citations and keep them relative to wiki/queries/.
        - Keep the answer concise but useful.

        Question:
        {question}

        Wiki context:
        {"\n\n---\n\n".join(context_blocks)}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    body = vertex_request_json(config, payload, label="Wiki query", timeout=90, retries=1)
    text = extract_vertex_text(body, error_message="Vertex returned no query answer.")
    return parse_vertex_json_text(text)


def dominant_query_domain(pages: list[Page]) -> str:
    counts: dict[str, int] = {}
    for page in pages:
        if page.domain and page.domain != "global":
            counts[page.domain] = counts.get(page.domain, 0) + 1
    if not counts:
        return ""
    domain, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return domain if count >= max(2, len(pages) // 2) else ""


def vertex_review_wiki(config: VertexConfig, pages: list[Page]) -> dict[str, object]:
    context_blocks = []
    for page in pages[:18]:
        context_blocks.append(f"Page: {page.rel_path}\nTitle: {page.title}\nContent:\n{read_text_file(page.path)[:5000]}")
    prompt = textwrap.dedent(
        f"""
        You are reviewing a markdown wiki for coherence and direction.
        Return valid JSON only with this exact schema:
        {{
          "title": "short review title",
          "summary": "one paragraph",
          "what_looks_right": ["item 1", "item 2"],
          "what_looks_wrong": ["item 1", "item 2"],
          "missing_or_unclear": ["item 1", "item 2"],
          "clarifying_questions": ["question 1", "question 2", "question 3"],
          "priority_pages": ["overview.md", "entities/example.md"]
        }}

        Rules:
        - Focus on global wiki quality, not line-by-line lint.
        - Ask clarifying questions only where user intent or domain is unclear.
        - Keep the questions actionable and specific.

        Wiki context:
        {"\n\n---\n\n".join(context_blocks)}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    body = vertex_request_json(config, payload, label="Wiki review", timeout=150, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no review.")
    return parse_vertex_json_text(text)


def vertex_apply_clarification_message(config: VertexConfig, review_state: dict[str, object], message: str, pages: list[Page]) -> dict[str, object]:
    review_page = str(review_state.get("review_page", ""))
    review_text = read_text_file(WIKI_ROOT / review_page) if review_page else ""
    context_blocks = []
    for page in pages[:8]:
        context_blocks.append(f"Page: {page.rel_path}\nTitle: {page.title}\nContent:\n{read_text_file(page.path)[:4000]}")
    prompt = textwrap.dedent(
        f"""
        You are updating a markdown wiki after the user responded to pending clarification questions.
        Return valid JSON only with this exact schema:
        {{
          "summary": "one paragraph describing the updated direction",
          "overview_update": "2-4 sentence overview update",
          "new_or_revised_concepts": [
            {{
              "name": "concept name",
              "summary": "short summary",
              "facts": ["fact 1", "fact 2"],
              "links": ["related page"]
            }}
          ],
          "new_or_revised_entities": [
            {{
              "name": "entity name",
              "summary": "short summary",
              "facts": ["fact 1", "fact 2"],
              "links": ["related page"]
            }}
          ],
          "open_questions": ["remaining question 1"]
        }}

        Review artifact:
        {review_text[:12000]}

        User clarification message:
        {message}

        Optional tagged wiki context:
        {"\n\n---\n\n".join(context_blocks)}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    body = vertex_request_json(config, payload, label="Clarification apply", timeout=150, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no clarification output.")
    return parse_vertex_json_text(text)


def vertex_plan_assistant_actions(config: VertexConfig, message: str, pages: list[Page]) -> dict[str, object]:
    context_blocks = []
    for page in pages[:8]:
        context_blocks.append(f"Page: {page.rel_path}\nTitle: {page.title}\nSummary: {page.summary}\nContent:\n{read_text_file(page.path)[:5000]}")
    prompt = textwrap.dedent(
        f"""
        You are a disciplined markdown wiki maintainer.
        The user is issuing an explicit document-management command through a universal assistant box.
        Convert the request into concrete wiki actions.

        Return valid JSON only with this exact schema:
        {{
          "title": "short action title",
          "summary": "one paragraph describing what should change",
          "canonical_scope": "one paragraph describing the relevant scope for this action",
          "global_guidelines": ["guideline 1", "guideline 2"],
          "page_directives": [
            {{
              "path": "entities/example.md",
              "action": "create|rewrite|merge|archive|delete|keep",
              "reason": "why",
              "instruction": "what to do",
              "target_title": "optional title for create",
              "merge_into": "optional merge destination",
              "source_paths": ["related/page.md", "another/page.md"]
            }}
          ]
        }}

        Rules:
        - Only use actions from: create, rewrite, merge, archive, delete, keep.
        - Prefer updating existing pages over creating duplicates.
        - Use tagged or directly relevant pages as context.
        - If the request is underspecified, choose the smallest safe set of actions.
        - If the request names a page implicitly, infer the matching page path from context.
        - Keep the directives concrete and executable.

        User request:
        {message}

        Wiki context:
        {"\n\n---\n\n".join(context_blocks) or "No wiki pages available."}
        """
    ).strip()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    body = vertex_request_json(config, payload, label="Wiki action plan", timeout=180, retries=2)
    text = extract_vertex_text(body, error_message="Vertex returned no wiki action plan.")
    return parse_vertex_json_text(text)


def write_query_page(question: str, result: dict[str, object], pages: list[Page]) -> str:
    title = str(result.get("title", "")).strip() or question[:80]
    slug = slugify(title)
    query_domain = dominant_query_domain(pages)
    if query_domain:
        ensure_domain(query_domain)
        rel_path = domain_rel(query_domain, "queries", f"{slug}.md")
    else:
        rel_path = f"queries/{slug}.md"
    path = WIKI_ROOT / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = str(result.get("summary", "")).strip() or "AI-generated query artifact."
    answer_md = str(result.get("answer_md", "")).strip() or "No answer generated."
    related_pages = sanitize_list(result.get("related_pages", [])) or [page.rel_path for page in pages]
    follow_ups = sanitize_list(result.get("follow_up_questions", []))
    related_md = "\n".join(f"- [{page}]({wiki_rel_link(rel_path, page) if not page.startswith('../') else page})" for page in related_pages)
    follow_md = "\n".join(f"- {item}" for item in follow_ups) or "- None yet."

    content = f"""---
tags: [query]
domain: {query_domain or "cross-domain"}
shared_scope: {"domain" if query_domain else "global"}
source_paths: [{", ".join(json.dumps(page) for page in related_pages[:12])}]
status: active
---

# {title}

## Summary

{summary}

## Question

{question}

## Answer

{answer_md}

## Related Pages

{related_md}

## Follow-up Questions

{follow_md}
"""
    path.write_text(content, encoding="utf-8")
    rebuild_index_page()
    append_log("query", title, f"Filed a query artifact for: {question}", [rel_path] + related_pages[:4])
    return rel_path


def write_review_page(result: dict[str, object], pages: list[Page]) -> str:
    title = str(result.get("title", "")).strip() or "Wiki Review"
    slug = slugify(title + "-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    rel_path = f"queries/{slug}.md"
    path = WIKI_ROOT / rel_path
    right_md = "\n".join(f"- {item}" for item in sanitize_list(result.get("what_looks_right", []))) or "- None."
    wrong_md = "\n".join(f"- {item}" for item in sanitize_list(result.get("what_looks_wrong", []))) or "- None."
    missing_md = "\n".join(f"- {item}" for item in sanitize_list(result.get("missing_or_unclear", []))) or "- None."
    questions = sanitize_list(result.get("clarifying_questions", []))
    questions_md = "\n".join(f"- {item}" for item in questions) or "- None."
    priority_pages = sanitize_list(result.get("priority_pages", [])) or [page.rel_path for page in pages[:6]]
    priority_md = "\n".join(f"- [{page}]({('../' + page) if not page.startswith('../') else page})" for page in priority_pages)
    guidelines_md = "\n".join(f"- {item}" for item in sanitize_list(result.get("global_guidelines", []))) or "- None."
    directives = result.get("page_directives", [])
    directives_md = "\n".join(
        f"- `{str(item.get('path', '')).strip()}`: {str(item.get('instruction', '')).strip() or str(item.get('reason', '')).strip()}"
        for item in directives
        if isinstance(item, dict) and str(item.get("path", "")).strip()
    ) or "- None."
    content = f"""# {title}

## Summary

{str(result.get('summary', '')).strip() or 'Wiki-wide review generated by AI.'}

## Canonical Scope

{str(result.get('canonical_scope', '')).strip() or 'Not specified.'}

## What Looks Right

{right_md}

## What Looks Wrong

{wrong_md}

## Missing Or Unclear

{missing_md}

## Clarifying Questions

{questions_md}

## Global Guidelines

{guidelines_md}

## Priority Pages

{priority_md}

## Planned Page Directives

{directives_md}
"""
    path.write_text(content, encoding="utf-8")
    upsert_index_entry("Queries", rel_path, str(result.get("summary", "")).strip() or "Wiki-wide review artifact.")
    append_log("review", title, "Ran a wiki-wide review and generated clarifying questions.", [rel_path] + priority_pages[:4])
    return rel_path


def apply_clarification_updates(result: dict[str, object], review_page_rel: str) -> list[str]:
    touched = []
    overview_summary = str(result.get("overview_update", "")).strip()
    if overview_summary:
        touched.append(update_overview_page(overview_summary, review_page_rel, "Wiki Review Clarification"))
    for item in sanitize_page_items(result.get("new_or_revised_entities", [])):
        touched.append(upsert_knowledge_page("entities", item, review_page_rel, "Wiki Review Clarification"))
    for item in sanitize_page_items(result.get("new_or_revised_concepts", [])):
        touched.append(upsert_knowledge_page("concepts", item, review_page_rel, "Wiki Review Clarification"))
    return sorted(set(touched))


def execute_page_directives(
    config: VertexConfig,
    *,
    batch_id: str,
    directives: list[dict[str, object]],
    plan: dict[str, object],
    context_pages: list[Page],
    status_job_type: str,
) -> tuple[list[str], list[dict[str, str]]]:
    pages = scan_wiki_pages()
    page_map = page_lookup(pages)
    touched_pages: list[str] = []
    action_log: list[dict[str, str]] = []
    total_actions = len(directives)

    for index, directive in enumerate(directives, start=1):
        rel_path = str(directive.get("path", "")).strip()
        action = str(directive.get("action", "rewrite")).strip().lower() or "rewrite"
        update_status(
            batch_id=batch_id,
            phase="working",
            current_file=rel_path or "wiki action",
            current_step=f"{action.title()} page {index} of {total_actions}",
            planned_actions=total_actions,
            updated_pages=len(set(touched_pages)),
            progress_label=f"Actions {total_actions} · Pages changed {len(set(touched_pages))}",
            last_event=f"{action.title()} {rel_path or 'wiki page'}",
            event=f"{status_job_type} {action} {rel_path or 'wiki page'}",
        )
        update_batch(
            batch_id,
            current_file=rel_path or "wiki action",
            current_step=f"{action.title()} page {index} of {total_actions}",
            planned_actions=total_actions,
            updated_pages=len(set(touched_pages)),
        )
        page = page_map.get(rel_path)
        related_rel_paths = sanitize_list(directive.get("source_paths", []))
        related_pages = [page_map[item] for item in related_rel_paths if item in page_map]
        if not related_pages:
            related_pages = [candidate for candidate in context_pages if candidate.rel_path != rel_path][:6]
        if not related_pages:
            related_pages = [candidate for candidate in pages if candidate.rel_path != rel_path][:6]

        if action == "keep":
            action_log.append({"path": rel_path, "action": action, "note": "Left unchanged."})
            continue
        if action in {"delete", "archive"} and page:
            output_rel = archive_or_delete_page(rel_path, mode=action, reason=str(directive.get("reason", "")).strip())
            touched_pages.append(output_rel)
            page_map.pop(rel_path, None)
            action_log.append({"path": rel_path, "action": action, "note": str(directive.get("reason", "")).strip() or action})
            log_event(f"{status_job_type}:crud", f"{batch_id} | {action} | {rel_path}")
            continue
        if action == "merge" and page:
            merge_target = str(directive.get("merge_into", "")).strip()
            if merge_target and merge_target in page_map:
                rewritten = vertex_rewrite_page_for_maintenance(config, page=page_map[merge_target], plan=plan, related_pages=[page] + related_pages)
                page_map[merge_target].path.write_text(repair_internal_links(merge_target, rewritten), encoding="utf-8")
                touched_pages.append(merge_target)
                archived_rel = archive_or_delete_page(rel_path, mode="archive", reason=f"Merged into {merge_target}")
                touched_pages.append(archived_rel)
                page_map.pop(rel_path, None)
                action_log.append({"path": rel_path, "action": action, "note": f"Merged into {merge_target}"})
                log_event(f"{status_job_type}:crud", f"{batch_id} | merge | {rel_path} -> {merge_target}")
                continue
        if action == "create" and not page:
            target = WIKI_ROOT / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            title = str(directive.get("target_title", "")).strip() or Path(rel_path).stem.replace("-", " ").title()
            created = vertex_create_page_for_maintenance(config, rel_path=rel_path, title=title, plan=plan, directive=directive, related_pages=related_pages)
            target.write_text(created, encoding="utf-8")
            page_map[rel_path] = Page(root=WIKI_ROOT, path=target, section=infer_section_from_path(rel_path), managed_by_ai=True)
            touched_pages.append(rel_path)
            action_log.append({"path": rel_path, "action": action, "note": str(directive.get("reason", "")).strip() or "Created during assistant action."})
            log_event(f"{status_job_type}:crud", f"{batch_id} | create | {rel_path}")
            continue
        if page:
            rewritten = vertex_rewrite_page_for_maintenance(config, page=page, plan=plan, related_pages=related_pages)
            page.path.write_text(repair_internal_links(page.rel_path, rewritten), encoding="utf-8")
            touched_pages.append(page.rel_path)
            action_log.append({"path": page.rel_path, "action": "rewrite", "note": str(directive.get("reason", "")).strip() or "Rewritten during assistant action."})
            log_event(f"{status_job_type}:rewrite", f"{batch_id} | {page.rel_path}")

    return sorted(set(touched_pages)), action_log


def extract_internal_links(text: str) -> list[str]:
    links = []
    for _, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text):
        if "://" in target or target.startswith("#"):
            continue
        links.append(target)
    return links


def run_lint_pass() -> dict[str, object]:
    pages = [page for page in scan_wiki_pages() if not page.rel_path.endswith("/README.md")]
    page_map = {page.rel_path: page for page in pages}
    inbound: dict[str, int] = {page.rel_path: 0 for page in pages}
    broken_links: list[str] = []

    for page in pages:
        text = read_text_file(page.path)
        current_dir = page.path.parent.relative_to(WIKI_ROOT).as_posix()
        for target in extract_internal_links(text):
            resolved = posixpath.normpath((Path(current_dir) / target).as_posix())
            if resolved in {"index.md", "log.md"}:
                continue
            if resolved in page_map:
                inbound[resolved] += 1
            else:
                candidate = WIKI_ROOT / resolved
                if not candidate.exists():
                    broken_links.append(f"{page.rel_path} -> {resolved}")

    orphans = [
        rel_path for rel_path, count in inbound.items()
        if count == 0 and rel_path not in {"overview.md"} and not rel_path.startswith("queries/")
    ]
    flat_pages = [
        page.rel_path for page in pages
        if page.rel_path.startswith(("sources/", "entities/", "concepts/"))
    ]
    low_value_pages = []
    title_to_paths: dict[str, list[str]] = {}
    for page in pages:
        if page.kind in {"concept", "entity"}:
            title_to_paths.setdefault(slugify(page.title), []).append(page.rel_path)
            text = read_text_file(page.path)
            if count_page_sources(text) <= 1 and len(text.split()) < 90:
                low_value_pages.append(page.rel_path)
    duplicated = {
        title: paths for title, paths in title_to_paths.items()
        if len(paths) > 1
    }
    report = {
        "broken_links": sorted(set(broken_links)),
        "orphans": sorted(orphans),
        "flat_pages": sorted(flat_pages),
        "low_value_pages": sorted(low_value_pages),
        "duplicated_entities_or_concepts": duplicated,
        "pages_scanned": len(pages),
        "summary": f"Scanned {len(pages)} wiki pages, found {len(set(broken_links))} broken links, {len(orphans)} orphan pages, {len(flat_pages)} old flat pages, and {len(low_value_pages)} low-value pages.",
    }
    return report


def write_lint_report(report: dict[str, object]) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rel_path = f"queries/lint-report-{timestamp}.md"
    path = WIKI_ROOT / rel_path
    broken = sanitize_list(report.get("broken_links", []))
    orphans = sanitize_list(report.get("orphans", []))
    flat_pages = sanitize_list(report.get("flat_pages", []))
    low_value = sanitize_list(report.get("low_value_pages", []))
    duplicates = report.get("duplicated_entities_or_concepts", {})
    broken_md = "\n".join(f"- {item}" for item in broken) or "- None."
    orphan_md = "\n".join(f"- [{item}](../{item})" for item in orphans) or "- None."
    flat_md = "\n".join(f"- [{item}](../{item})" for item in flat_pages) or "- None."
    low_value_md = "\n".join(f"- [{item}](../{item})" for item in low_value) or "- None."
    duplicate_md = "\n".join(
        f"- {title}: {', '.join(paths)}"
        for title, paths in duplicates.items()
    ) if isinstance(duplicates, dict) and duplicates else "- None."
    content = f"""# Lint Report {timestamp}

## Summary

{str(report.get("summary", "")).strip()}

## Broken Links

{broken_md}

## Orphan Pages

{orphan_md}

## Old Flat Pages

{flat_md}

## Low-Value Pages

{low_value_md}

## Duplicated Entities Or Concepts

{duplicate_md}

## Notes

- This lint pass checks internal links, inbound-link counts, old flat namespace pages, thin generated pages, and duplicated concept/entity titles across domains.
"""
    path.write_text(content, encoding="utf-8")
    upsert_index_entry("Queries", rel_path, str(report.get("summary", "")).strip() or "Wiki lint report.")
    append_log("lint", f"Lint Report {timestamp}", str(report.get("summary", "")).strip(), [rel_path])
    return rel_path


def normalize_repo_path(base: Path, relative_url_path: str) -> Path | None:
    candidate = (base / unquote(relative_url_path)).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


def render_markdown(text: str, current_dir: str, mode: str) -> str:
    output: list[str] = []
    lines = text.splitlines()
    in_list = False
    in_code = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            joined = " ".join(paragraph).strip()
            output.append(f"<p>{format_inline(joined, current_dir, mode)}</p>")
            paragraph = []

    for line in lines:
        stripped = line.rstrip()
        if stripped.startswith("```"):
            flush_paragraph()
            if in_list:
                output.append("</ul>")
                in_list = False
            if in_code:
                output.append("</code></pre>")
                in_code = False
            else:
                output.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            output.append(html.escape(line))
            continue
        if not stripped:
            flush_paragraph()
            if in_list:
                output.append("</ul>")
                in_list = False
            continue
        heading_match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading_match:
            flush_paragraph()
            if in_list:
                output.append("</ul>")
                in_list = False
            level = len(heading_match.group(1))
            content = format_inline(heading_match.group(2), current_dir, mode)
            output.append(f"<h{level}>{content}</h{level}>")
            continue
        if stripped.startswith("- "):
            flush_paragraph()
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{format_inline(stripped[2:].strip(), current_dir, mode)}</li>")
            continue
        paragraph.append(stripped)

    flush_paragraph()
    if in_list:
        output.append("</ul>")
    if in_code:
        output.append("</code></pre>")
    return "\n".join(output)


def format_inline(text: str, current_dir: str, mode: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", escaped)

    def replace_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1))
        target = match.group(2)
        href = resolve_link(current_dir, target, mode)
        return f'<a href="{href}">{label}</a>'

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, escaped)


def resolve_link(current_dir: str, target: str, mode: str) -> str:
    if "://" in target:
        return html.escape(target, quote=True)
    current = Path(current_dir)
    resolved = posixpath.normpath((current / target).as_posix())
    if mode == "wiki":
        return f"/page/{quote(resolved)}"
    return f"/raw/{quote(resolved)}"


def read_text_file(path: Path, *, for_ingest: bool = False) -> str:
    if path.suffix.lower() == ".pdf":
        return extract_pdf_text(path)
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return extract_excel_text(path)
    if path.suffix.lower() == ".json" and for_ingest:
        return extract_json_text(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def extract_pdf_text(path: Path) -> str:
    text = extract_pdf_text_with_pypdf(path)
    if is_usable_pdf_text(text):
        return text
    try:
        result = subprocess.run(
            ["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    text = result.stdout.strip()
    return text if is_usable_pdf_text(text) else ""


def extract_excel_text(path: Path) -> str:
    workbook = extract_excel_workbook(path)
    return workbook.get("text", "") if isinstance(workbook, dict) else ""


def extract_excel_workbook(path: Path) -> dict[str, object]:
    try:
        import pandas as pd
    except Exception:
        return {"sheet_count": 0, "sheets": [], "text": ""}
    try:
        workbook = pd.ExcelFile(path)
    except Exception:
        return {"sheet_count": 0, "sheets": [], "text": ""}

    blocks: list[str] = []
    sheets: list[dict[str, object]] = []
    for sheet_name in workbook.sheet_names:
        try:
            frame = workbook.parse(sheet_name=sheet_name, dtype=str)
        except Exception:
            continue
        if frame.empty and len(frame.columns) == 0:
            continue
        trimmed = frame.fillna("").astype(str).iloc[:200, :30]
        sheet_info = {
            "name": sheet_name,
            "row_count": int(len(frame.index)),
            "column_count": int(len(frame.columns)),
            "columns": [str(col) for col in list(trimmed.columns)[:30]],
            "sample_rows": [],
        }
        blocks.append(f"# Sheet: {sheet_name}")
        if len(trimmed.columns) > 0:
            blocks.append("Columns: " + ", ".join(str(col) for col in trimmed.columns))
        rows = trimmed.to_dict(orient="records")
        for index, row in enumerate(rows[:200], start=1):
            cells = [f"{key}={value}" for key, value in row.items() if str(value).strip()]
            if cells:
                rendered = " | ".join(cells)
                blocks.append(f"- Row {index}: " + rendered)
                if len(sheet_info["sample_rows"]) < 12:
                    sheet_info["sample_rows"].append(rendered)
        if not rows:
            blocks.append("- Sheet is empty.")
        blocks.append("")
        sheets.append(sheet_info)
    return {"sheet_count": len(sheets), "sheets": sheets, "text": "\n".join(blocks).strip()}


def extract_pdf_text_with_pypdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    parts: list[str] = []
    for page in reader.pages:
        try:
            page_text = page.extract_text() or ""
        except Exception:
            page_text = ""
        if page_text.strip():
            parts.append(page_text.strip())
    return "\n\n".join(parts).strip()


def is_usable_pdf_text(text: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    strong_bad_signals = [
        "font family",
        "inter bold",
        "inter semibold",
        "dart_pdf",
        "pdf-1.",
        "/font",
        "/pages",
        "/type /catalog",
        "obj endobj",
        "xref",
    ]
    if sum(signal in lower for signal in strong_bad_signals) >= 3:
        return False
    words = re.findall(r"[A-Za-z]{3,}", cleaned)
    digit_groups = re.findall(r"\d{2,}", cleaned)
    if len(words) < 8 and len(digit_groups) < 3:
        return False
    alpha_ratio = sum(ch.isalpha() for ch in cleaned) / max(len(cleaned), 1)
    if alpha_ratio < 0.2:
        return False
    return True


def raw_ingest_status(path: Path) -> str:
    suffix = path.suffix.lower()
    text = read_text_file(path, for_ingest=True)
    workbook_info = extract_excel_workbook(path) if suffix in {".xlsx", ".xls"} else None
    profile = build_source_profile(path, text, workbook_info) if text.strip() else None
    if suffix == ".pdf":
        return f"PDF extracted · {profile['quality_label']}" if profile else "PDF no usable text"
    if suffix in {".xlsx", ".xls"}:
        return f"Workbook extracted · {profile['quality_label']}" if profile else "Workbook no usable text"
    if should_ingest_path(path):
        return f"Text ready · {profile['quality_label']}" if profile else "Text ready"
    return "Stored only"


def build_navigation(pages: Iterable[Page]) -> str:
    grouped: dict[str, list[Page]] = {key: [] for key in SECTIONS}
    for page in pages:
        grouped.setdefault(page.section, []).append(page)

    chunks: list[str] = []
    for key, label in SECTIONS.items():
        items = grouped.get(key, [])
        if not items:
            continue
        links = "".join(
            f'<li><a href="/page/{quote(page.rel_path)}">{html.escape(page.title)}</a></li>'
            for page in items[:12]
        )
        chunks.append(
            f'<section class="nav-section"><h2>{label}</h2><ul>{links}</ul></section>'
        )
    return "".join(chunks)


def page_shell(*, title: str, body: str, pages: list[Page], flash: str = "") -> str:
    stats = repository_stats()
    logs = parse_log_entries()
    nav = build_navigation(pages)
    config = vertex_config()
    status = read_status()
    review_state = read_review_state()
    chat_messages_html = render_chat_messages_html(list_chat_messages())
    page_options = "".join(
        f'<option value="{html.escape(page.rel_path)}">{html.escape(page.title)}</option>'
        for page in sorted(pages, key=lambda item: item.rel_path)[:400]
    )
    logs_html = "".join(
        f"<li><strong>{html.escape(entry['header'])}</strong><div class='nav-meta'>{html.escape(entry['body'])}</div></li>"
        for entry in logs
    ) or "<li class='muted'>No log entries yet.</li>"
    flash_html = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    vertex_html = (
        f"Vertex linked · model {html.escape(config.model_id)}"
        if config.configured
        else "Vertex not configured"
    )
    workspace_html = f"Workspace: {html.escape(display_workspace_name(CURRENT_WORKSPACE))}"
    status_summary = "Processing batch" if status.get("active") else "Idle"
    if status.get("phase") == "done":
        status_summary = "Last batch completed"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} | Solo-Corp OS</title>
  <style>{STYLE}</style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <h1>Solo-Corp OS</h1>
        <p>The Co-Founder Brain</p>
      </div>
      <form class="search-form" method="get" action="/search">
        <input type="search" name="q" placeholder="Search the OS">
        <button type="submit">Search</button>
      </form>
      <div class="nav-meta">AI pages: {stats['wiki_pages']} · Raw uploads: {stats['raw_files']}</div>
      <div class="nav-meta" style="margin-top: 6px;">{workspace_html}</div>
      <div class="nav-meta" style="margin-top: 6px;">{vertex_html}</div>
      <div class="nav-meta" style="margin-top: 6px;">Ingestion: {html.escape(status_summary)}</div>
      <div class="nav-meta" style="margin-top: 6px;">Queue depth: {html.escape(str(status.get('queue_depth', 0)))}</div>
      {nav}
      <section class="nav-section">
        <h2>System</h2>
        <ul>
          <li><a href="/">Main page</a></li>
          <li><a href="/page/index.md">Wiki index</a></li>
          <li><a href="/graph">Graph View</a></li>
          <li><a href="/page/log.md">Activity log</a></li>
          <li><a href="/revisions">Revisions</a></li>
          <li><a href="/queries">Queries</a></li>
          <li><a href="/staging">Staging</a></li>
          <li><a href="/batches">Batches</a></li>
          <li>
            <form action="/reset-wiki" method="post" style="display:inline;">
              <button type="submit" style="background:none;border:none;color:var(--link);padding:0;font:inherit;cursor:pointer;text-decoration:none;" onclick="return confirm('Are you sure you want to reset the wiki? This deletes all generated pages (sources, entities, concepts) but keeps raw files.');">Reset Wiki</button>
            </form>
          </li>
          <li>
            <form action="/ingest-all" method="post" style="display:inline;">
              <button type="submit" style="background:none;border:none;color:var(--link);padding:0;font:inherit;cursor:pointer;text-decoration:none;" onclick="return confirm('Are you sure you want to reingest all raw files? This will queue them for background processing.');">Reingest All</button>
            </form>
          </li>
        </ul>
      </section>
    </aside>
    <main class="content-wrap">
      <div class="topbar">
        <div class="wordmark">
          <div class="wordmark-mark">W</div>
          <div class="wordmark-text">
            <strong>LLM Wiki</strong>
            <span>AI-maintained knowledge base</span>
          </div>
        </div>
        <div class="top-links">Read · View source · View history</div>
      </div>
      <div class="content-inner">
        {flash_html}
        <div class="article-tabs">
          <div class="tab-row">
            <a class="tab active" href="#">Article</a>
            <a class="tab" href="/page/index.md">Discussion</a>
          </div>
          <div class="tab-tools">
            <a class="tab active" href="#">Read</a>
            <a class="tab" href="/revisions">View history</a>
            <a class="tab" href="/page/log.md">Logs</a>
          </div>
        </div>
        {body}
        <div class="panel" style="margin-top: 0;">
          <h2>Recent Activity</h2>
          <ul class="item-list">{logs_html}</ul>
        </div>
      </div>
    </main>
  </div>
  <div class="chat-shell" data-chat-shell>
    <div class="chat-header">
      <strong>Wiki Chat</strong>
      <span>{'Pending review clarification is active. Your next message will be treated as clarification.' if review_state.get('pending') else 'Ask questions or issue doc actions. Answers appear here and durable markdown is still filed in the wiki.'}</span>
    </div>
    <div class="chat-messages" data-chat-messages>{chat_messages_html}</div>
    <form class="chat-form" data-chat-form>
      <textarea name="message" placeholder="Ask a question, or say: update entities/openai.md based on the latest source" required></textarea>
      <input type="text" name="tags" list="wiki-page-options" placeholder="Optional tagged docs, comma separated">
      <div class="chat-form-actions">
        <span class="chat-help">Tag docs by relative path or title for safer answers.</span>
        <button type="submit">{'Send Clarification' if review_state.get('pending') else 'Send To Wiki'}</button>
      </div>
    </form>
    <datalist id="wiki-page-options">
      {page_options}
    </datalist>
  </div>
<script>
  document.querySelectorAll('form[action="/upload"]').forEach((uploadForm) => {{
    const fileInput = uploadForm.querySelector('input[name="file"]');
    const statusNode = uploadForm.querySelector('[data-upload-status]');
    let uploadInFlight = false;

    const submitUpload = () => {{
      if (uploadInFlight || !fileInput || !fileInput.files || fileInput.files.length === 0) {{
        return;
      }}
      uploadInFlight = true;
      if (statusNode) {{
        statusNode.textContent = 'Uploading ' + fileInput.files.length + ' file(s)...';
      }}
      const formData = new FormData();
      const notes = uploadForm.querySelector('textarea[name="notes"]');
      const filename = uploadForm.querySelector('input[name="filename"]');
      if (notes && notes.value) {{
        formData.append('notes', notes.value);
      }}
      if (filename && filename.value) {{
        formData.append('filename', filename.value);
      }}
      for (const file of fileInput.files) {{
        formData.append('file', file, file.name);
        formData.append('relative_path', file.webkitRelativePath || file.name);
      }}
      fetch('/upload', {{ method: 'POST', body: formData }})
        .then((response) => {{
          if (response.redirected) {{
            window.location.href = response.url;
            return;
          }}
          window.location.reload();
        }})
        .catch(() => {{
          if (statusNode) {{
            statusNode.textContent = 'Upload failed before the server responded.';
          }}
          uploadInFlight = false;
        }});
    }};

    uploadForm.addEventListener('submit', (event) => {{
      event.preventDefault();
      submitUpload();
    }});

    fileInput?.addEventListener('change', () => {{
      if (statusNode && fileInput.files && fileInput.files.length > 0) {{
        statusNode.textContent = 'Selected ' + fileInput.files.length + ' file(s). Starting upload...';
      }}
      submitUpload();
    }});
  }});

  const statusRoot = document.querySelector('[data-ingestion-status]');
  if (statusRoot) {{
    const applyStatus = (status) => {{
      const activeNode = statusRoot.querySelector('[data-status-active]');
      const jobNode = statusRoot.querySelector('[data-status-job]');
      const batchNode = statusRoot.querySelector('[data-status-batch]');
      const phaseNode = statusRoot.querySelector('[data-status-phase]');
      const progressNode = statusRoot.querySelector('[data-status-progress]');
      const failureNode = statusRoot.querySelector('[data-status-failures]');
      const queueNode = statusRoot.querySelector('[data-status-queue]');
      const currentNode = statusRoot.querySelector('[data-status-current]');
      const stepNode = statusRoot.querySelector('[data-status-step]');
      const eventNode = statusRoot.querySelector('[data-status-last-event]');
      const errorNode = statusRoot.querySelector('[data-status-error]');
      const eventsNode = statusRoot.querySelector('[data-status-events]');
      if (activeNode) activeNode.textContent = status.active ? 'Running' : 'Idle';
      if (jobNode) jobNode.textContent = status.job_label || 'None';
      if (batchNode) batchNode.textContent = status.batch_id || 'None';
      if (phaseNode) phaseNode.textContent = status.phase || 'idle';
      if (progressNode) progressNode.textContent = status.progress_label || 'No active progress.';
      if (failureNode) failureNode.textContent = String(status.failure_count || 0);
      if (queueNode) queueNode.textContent = String(status.queue_depth || 0);
      if (currentNode) currentNode.textContent = status.current_file || 'None';
      if (stepNode) stepNode.textContent = status.current_step || 'Idle';
      if (eventNode) eventNode.textContent = status.last_event || 'No activity yet.';
      if (errorNode) errorNode.textContent = status.error || 'None';
      if (eventsNode) {{
        const events = Array.isArray(status.recent_events) ? status.recent_events : [];
        eventsNode.innerHTML = events.length ? events.map((item) => '<li>' + item + '</li>').join('') : '<li>No events yet.</li>';
      }}
    }};

    const refreshStatus = () => {{
      fetch('/status')
        .then((response) => response.json())
        .then((data) => applyStatus(data))
        .catch(() => null);
    }};

    refreshStatus();
    window.setInterval(refreshStatus, 3000);
  }}

  const chatForm = document.querySelector('[data-chat-form]');
  const chatMessages = document.querySelector('[data-chat-messages]');
  let forceChatScrollToBottom = false;
  const refreshChat = () => {{
    fetch('/chat-state')
      .then((response) => response.json())
      .then((payload) => {{
        if (chatMessages && payload && typeof payload.messages_html === 'string') {{
          const distanceFromBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight;
          const shouldStickToBottom = forceChatScrollToBottom || distanceFromBottom < 32;
          chatMessages.innerHTML = payload.messages_html;
          if (shouldStickToBottom) {{
            chatMessages.scrollTop = chatMessages.scrollHeight;
          }}
          forceChatScrollToBottom = false;
        }}
      }})
      .catch(() => null);
  }};

  if (chatForm) {{
    chatForm.addEventListener('submit', (event) => {{
      event.preventDefault();
      const payload = new URLSearchParams(new FormData(chatForm));
      fetch('/chat', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8' }},
        body: payload.toString()
      }})
        .then((response) => response.json())
        .then(() => {{
          chatForm.reset();
          forceChatScrollToBottom = true;
          refreshChat();
        }})
        .catch(() => null);
    }});
    refreshChat();
    window.setInterval(refreshChat, 3000);
  }}
</script>
</body>
</html>"""


class WikiHandler(BaseHTTPRequestHandler):
    server_version = "LLMWiki/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.render_home(parse_qs(parsed.query))
            return
        if parsed.path == "/search":
            self.render_search(parse_qs(parsed.query))
            return
        if parsed.path == "/status":
            self.write_json(read_status())
            return
        if parsed.path.startswith("/v1/models"):
            self.handle_v1_models()
            return
        if parsed.path == "/openapi.json":
            self.handle_openapi_json()
            return
        if parsed.path == "/docs":
            self.render_swagger()
            return
        if parsed.path == "/api/graph":
            self.handle_graph_api()
            return
        if parsed.path == "/graph":
            self.render_graph()
            return
        if parsed.path == "/chat-state":
            self.write_json({"messages_html": render_chat_messages_html(list_chat_messages())})
            return
        if parsed.path == "/batches":
            self.render_batches()
            return
        if parsed.path == "/archive":
            self.render_archive()
            return
        if parsed.path == "/staging":
            self.render_staging()
            return
        if parsed.path == "/revisions":
            self.render_revisions()
            return
        if parsed.path == "/queries":
            self.render_queries()
            return
        if parsed.path.startswith("/revision/"):
            revision_id = parsed.path.removeprefix("/revision/")
            self.render_revision(revision_id)
            return
        if parsed.path.startswith("/page/"):
            rel = parsed.path.removeprefix("/page/")
            self.render_page(rel)
            return
        if parsed.path.startswith("/raw/"):
            rel = parsed.path.removeprefix("/raw/")
            self.render_raw(rel)
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self.handle_upload()
            return
        if parsed.path.startswith("/v1/chat/completions") or parsed.path.startswith("/v1/responses"):
            self.handle_v1_chat_completions()
            return
        if parsed.path == "/query":
            self.handle_query()
            return
        if parsed.path == "/assistant":
            self.handle_assistant()
            return
        if parsed.path == "/chat":
            self.handle_chat()
            return
        if parsed.path == "/workspace/create":
            self.handle_workspace_create()
            return
        if parsed.path == "/workspace/select":
            self.handle_workspace_select()
            return
        if parsed.path == "/review":
            self.handle_review()
            return
        if parsed.path == "/review-answer":
            self.handle_review_answer()
            return
        if parsed.path == "/lint":
            self.handle_lint()
            return
        if parsed.path == "/ingest":
            self.handle_ingest()
            return
        if parsed.path == "/reset-wiki":
            self.handle_reset_wiki()
            return
        if parsed.path == "/ingest-all":
            self.handle_ingest_all()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_graph_api(self) -> None:
        pages = scan_wiki_pages()
        nodes = []
        edges = []
        
        # Build node map to ensure edges connect valid nodes
        page_paths = {page.rel_path for page in pages}
        
        for page in pages:
            # Determine group
            group = "other"
            if page.rel_path.startswith("domains/"):
                parts = page.rel_path.split("/")
                if len(parts) > 1:
                    group = parts[1]
            elif page.rel_path.startswith("global/"):
                group = "global"
            elif page.rel_path.startswith("entities/"):
                group = "entity"
            elif page.rel_path.startswith("concepts/"):
                group = "concept"
            elif page.rel_path.startswith("sources/"):
                group = "source"
                
            nodes.append({
                "id": page.rel_path,
                "label": page.title or page.path.name,
                "group": group,
                "title": f"Path: {page.rel_path}\\nSummary: {page.summary}"
            })
            
            # Extract links
            content = read_text_file(page.path)
            # Find markdown links: [text](link)
            link_pattern = re.compile(r'\[.*?\]\((.*?)\)')
            for match in link_pattern.finditer(content):
                link = match.group(1)
                # Cleanup link to match rel_path
                link = unquote(link).split('#')[0]
                if link.startswith('file://'):
                    continue # Skip absolute file links or external links
                if link.startswith('http'):
                    continue
                # Normalize relative links
                if link.startswith('/'):
                    link = link[1:]
                else:
                    link = posixpath.normpath(posixpath.join(posixpath.dirname(page.rel_path), link))
                
                if link in page_paths:
                    edges.append({
                        "from": page.rel_path,
                        "to": link
                    })
                    
        self.write_json({"nodes": nodes, "edges": edges})

    def render_graph(self) -> None:
        pages = scan_wiki_pages()
        html_content = page_shell(
            title="Graph View",
            body="""
            <div class="topbar">
                <div class="wordmark">
                    <div class="wordmark-mark">S</div>
                    <div class="wordmark-text">
                        <strong>Solo-Corp OS</strong>
                        <span>Graph View</span>
                    </div>
                </div>
            </div>
            <div class="content-inner">
                <h1 class="article-title">Knowledge Graph</h1>
                <p class="subtitle">Interactive visualization of your wiki connections.</p>
                <div class="article-body">
                    <div id="mynetwork" style="width: 100%; height: 600px; border: 1px solid var(--border-light); background: #fafbfc; border-radius: 4px; margin-top: 16px;"></div>
                </div>
            </div>
            
            <!-- Vis-Network CDN -->
            <script type="text/javascript" src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
            <script type="text/javascript">
                document.addEventListener('DOMContentLoaded', function() {
                    fetch('/api/graph')
                        .then(response => response.json())
                        .then(data => {
                            var container = document.getElementById('mynetwork');
                            var nodes = new vis.DataSet(data.nodes);
                            var edges = new vis.DataSet(data.edges);
                            var graphData = {
                                nodes: nodes,
                                edges: edges
                            };
                            var options = {
                                nodes: {
                                    shape: 'dot',
                                    size: 16,
                                    font: {
                                        size: 14,
                                        color: '#333'
                                    },
                                    borderWidth: 2
                                },
                                edges: {
                                    width: 1.5,
                                    color: { inherit: 'both' },
                                    smooth: {
                                        type: 'continuous'
                                    }
                                },
                                physics: {
                                    forceAtlas2Based: {
                                        gravitationalConstant: -50,
                                        centralGravity: 0.01,
                                        springLength: 100,
                                        springConstant: 0.08
                                    },
                                    maxVelocity: 50,
                                    solver: 'forceAtlas2Based',
                                    timestep: 0.35,
                                    stabilization: { iterations: 150 }
                                },
                                interaction: {
                                    hover: true,
                                    tooltipDelay: 200,
                                    zoomView: true
                                }
                            };
                            var network = new vis.Network(container, graphData, options);
                            
                            // Double click opens the page
                            network.on("doubleClick", function(params) {
                                if (params.nodes.length > 0) {
                                    window.location.href = "/page/" + params.nodes[0];
                                }
                            });
                            
                            // Click opens the page
                            network.on("selectNode", function(params) {
                                if (params.nodes.length > 0) {
                                    window.location.href = "/page/" + params.nodes[0];
                                }
                            });
                        })
                        .catch(error => {
                            console.error('Error fetching graph data:', error);
                            document.getElementById('mynetwork').innerHTML = '<div style="padding: 20px; color: red;">Failed to load graph data. Check console for details.</div>';
                        });
                });
            </script>
            """,
            pages=pages,
        )
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_content.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html_content.encode("utf-8"))

    def handle_openapi_json(self) -> None:
        openapi = {
            "openapi": "3.0.0",
            "info": {
                "title": "Solo-Corp OS API",
                "version": "1.0.0"
            },
            "paths": {
                "/v1/chat/completions": {
                    "post": {
                        "summary": "Create chat completion",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "messages": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "role": {"type": "string"},
                                                        "content": {"type": "string"}
                                                    }
                                                }
                                            },
                                            "stream": {"type": "boolean"}
                                        }
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "Successful response"
                            }
                        }
                    }
                },
                "/v1/models": {
                    "get": {
                        "summary": "List models",
                        "responses": {
                            "200": {
                                "description": "Successful response"
                            }
                        }
                    }
                }
            }
        }
        self.write_json(openapi)

    def render_swagger(self) -> None:
        html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>Swagger UI - Solo-Corp OS</title>
          <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui.css" />
        </head>
        <body>
        <div id="swagger-ui"></div>
        <script src="https://unpkg.com/swagger-ui-dist@5.11.0/swagger-ui-bundle.js" crossorigin></script>
        <script>
          window.onload = () => {
            window.ui = SwaggerUIBundle({
              url: '/openapi.json',
              dom_id: '#swagger-ui',
            });
          };
        </script>
        </body>
        </html>
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def handle_v1_models(self) -> None:
        self.write_json({
            "object": "list",
            "data": [
                {
                    "id": "solo-corp-brain",
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "solo-corp-os"
                }
            ]
        })

    def handle_v1_chat_completions(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Empty body")
            return
        raw_body = self.rfile.read(length).decode("utf-8")
        try:
            req = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return
            
        messages = req.get("messages", [])
        if not messages:
            self.send_error(HTTPStatus.BAD_REQUEST, "No messages provided")
            return
            
        question = messages[-1].get("content", "")
        
        config = vertex_config()
        if not config.configured:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Vertex AI not configured")
            return
            
        pages = select_relevant_wiki_pages(question)
        if not pages:
            answer = "I don't have any relevant knowledge in my OS yet to answer this. Please ingest more sources first."
        else:
            try:
                result = vertex_answer_query(config, question, pages)
                answer = result.get("answer_md", "Error generating answer.")
                
                # Format sources nicely
                if result.get("related_pages"):
                    sources = "\\n\\n**Sources:**\\n" + "\\n".join(f"- {p}" for p in result.get("related_pages", []))
                    answer += sources
            except Exception as e:
                answer = f"Error generating answer: {str(e)}"
                
        # Handle streaming
        stream = req.get("stream", False)
        
        if stream:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            
            chat_id = f"chatcmpl-{int(time.time())}"
            created_time = int(time.time())
            
            # Send the initial role chunk
            chunk_role = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": "solo-corp-brain",
                "choices": [{"delta": {"role": "assistant", "content": ""}, "index": 0, "finish_reason": None}]
            }
            self.wfile.write(f"data: {json.dumps(chunk_role)}\n\n".encode("utf-8"))
            self.wfile.flush()
            
            # Artificially stream the answer in small pieces to perfectly match OpenAI behavior
            chunk_size = 20
            for i in range(0, len(answer), chunk_size):
                piece = answer[i:i+chunk_size]
                chunk_content = {
                    "id": chat_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": "solo-corp-brain",
                    "choices": [{"delta": {"content": piece}, "index": 0, "finish_reason": None}]
                }
                self.wfile.write(f"data: {json.dumps(chunk_content)}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.01)
            
            # Send the finish_reason chunk
            chunk_finish = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": "solo-corp-brain",
                "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}]
            }
            self.wfile.write(f"data: {json.dumps(chunk_finish)}\n\n".encode("utf-8"))
            self.wfile.flush()
            
            # Send the DONE indicator
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            resp = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "solo-corp-brain",
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": answer
                    },
                    "finish_reason": "stop",
                    "index": 0
                }]
            }
            self.write_json(resp)

    def handle_reset_wiki(self) -> None:
        paths_to_clear = [
            WIKI_ROOT / "sources",
            WIKI_ROOT / "entities",
            WIKI_ROOT / "concepts",
            WIKI_ROOT / "queries",
            WIKI_ROOT / "archive",
            WIKI_ROOT / "revisions",
            WIKI_ROOT / "staging" / "sources",
        ]
        for p in paths_to_clear:
            if p.exists() and p.is_dir():
                shutil.rmtree(p)
        for p in [WIKI_ROOT / "index.md", WIKI_ROOT / "log.md", WIKI_ROOT / "overview.md"]:
            if p.exists():
                p.unlink()
        initialize_workspace_files(CURRENT_WORKSPACE)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def handle_ingest_all(self) -> None:
        raw_files = scan_raw_files()
        paths_to_ingest: list[Path] = []
        for page in raw_files:
            if should_ingest_path(page.path) and (
                page.path.suffix.lower() not in {".pdf", ".xlsx", ".xls", ".json"} or bool(read_text_file(page.path, for_ingest=True).strip())
            ):
                paths_to_ingest.append(page.path)

        if not paths_to_ingest:
            self.redirect("/?flash=No+raw+files+to+ingest")
            return

        batch_id = start_batch_status(len(paths_to_ingest))
        
        batch_payload = {
            "id": batch_id,
            "job_type": "ingest",
            "status": "queued",
            "phase": "queued",
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "workspace": CURRENT_WORKSPACE,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "completed_at": "",
            "saved_count": len(paths_to_ingest),
            "total_items": len(paths_to_ingest),
            "ingest_total": len(paths_to_ingest),
            "ingest_completed": 0,
            "failure_count": 0,
            "saved_paths": [path.relative_to(RAW_ROOT).as_posix() for path in paths_to_ingest],
            "text_paths": [path.relative_to(RAW_ROOT).as_posix() for path in paths_to_ingest],
            "skipped_paths": [],
            "current_file": "",
            "successes": [],
            "failures": [],
            "maintenance_report": "",
            "revisions_created": 0,
        }
        write_batch(batch_id, batch_payload)
        INGEST_QUEUE.put(batch_payload)
        update_status(
            batch_id=batch_id,
            active=True,
            job_type="ingest",
            job_label=batch_job_label("ingest"),
            phase="queued",
            saved_count=len(paths_to_ingest),
            total_items=len(paths_to_ingest),
            ingest_total=len(paths_to_ingest),
            ingest_completed=0,
            failure_count=0,
            current_file="",
            current_step="Queued for background ingest",
            queue_depth=INGEST_QUEUE.qsize(),
            last_event=f"Queued {len(paths_to_ingest)} files for reingestion"
        )
        ensure_worker()
        flash = f"Queued {len(paths_to_ingest)} files for background reingestion."
        log_event("ingest-all:queued", f"total={len(paths_to_ingest)} | batch={batch_id}")
        self.redirect(f"/?flash={quote(flash)}")

    def render_home(self, query: dict[str, list[str]]) -> None:
        pages = scan_wiki_pages()
        staging_pages = scan_staging_pages()
        raw_files = scan_raw_files()
        stats = repository_stats()
        latest_pages = sorted(pages, key=lambda p: p.path.stat().st_mtime, reverse=True)[:6]
        latest_staging = sorted(staging_pages, key=lambda p: p.path.stat().st_mtime, reverse=True)[:4]
        latest_raw = sorted(raw_files, key=lambda p: p.path.stat().st_mtime, reverse=True)[:6]
        revisions = recent_revisions(5)
        status = read_status()
        batches = recent_batches(5)
        review_state = read_review_state()
        workspaces = list_workspaces()
        workspace_options = "".join(
            f"<option value='{html.escape(name)}' {'selected' if name == CURRENT_WORKSPACE else ''}>{html.escape(display_workspace_name(name))}</option>"
            for name in workspaces
        )

        def render_items(items: list[Page], raw: bool = False) -> str:
            if not items:
                return "<p class='muted'>Nothing yet.</p>"
            result = []
            for item in items:
                href = f"/raw/{quote(item.rel_path)}" if raw else f"/page/{quote(item.rel_path)}"
                badge = f"Raw upload · {raw_ingest_status(item.path)}" if raw else f"AI managed · {SECTIONS.get(item.section, item.section.title())}"
                result.append(
                    "<li>"
                    f"<a href='{href}'>{html.escape(item.title)}</a>"
                    f"<div class='nav-meta'>{html.escape(item.summary)} · {html.escape(item.updated)} · {html.escape(badge)}</div>"
                    "</li>"
                )
            return f"<ul class='item-list'>{''.join(result)}</ul>"

        flash = query.get("flash", [""])[0]
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">Main page</div>
          <a class="button-link" href="/page/overview.md">Main article</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Solo-Corp OS</h1>
            <p class="subtitle">From today’s uploads to long-running concepts, this wiki is maintained as the Co-Founder Brain rather than a temporary chat answer.</p>
            <div class="status-box" data-ingestion-status>
              <h2>Ingestion Status</h2>
              <div class="status-grid">
                <div class="status-line"><strong data-status-active>{'Running' if status.get('active') else 'Idle'}</strong>Status</div>
                <div class="status-line"><strong data-status-job>{html.escape(str(status.get('job_label', '') or 'None'))}</strong>Job Type</div>
                <div class="status-line"><strong data-status-phase>{html.escape(str(status.get('phase', 'idle')))}</strong>Phase</div>
                <div class="status-line"><strong data-status-progress>{html.escape(status_progress_label(status))}</strong>Progress</div>
                <div class="status-line"><strong data-status-failures>{html.escape(str(status.get('failure_count', 0)))}</strong>Failures</div>
                <div class="status-line"><strong data-status-queue>{html.escape(str(status.get('queue_depth', 0)))}</strong>Queued jobs</div>
                <div class="status-line"><strong data-status-batch>{html.escape(str(status.get('batch_id', '') or 'None'))}</strong>Batch ID</div>
              </div>
              <p class="nav-meta" style="margin-top: 10px;"><b>Current target:</b> <span data-status-current>{html.escape(str(status.get('current_file', '') or 'None'))}</span></p>
              <p class="nav-meta"><b>Current step:</b> <span data-status-step>{html.escape(str(status.get('current_step', '') or 'Idle'))}</span></p>
              <p class="nav-meta"><b>Last event:</b> <span data-status-last-event>{html.escape(str(status.get('last_event', '') or 'No activity yet.'))}</span></p>
              {"<p class='nav-meta'><b>Error:</b> <span data-status-error>" + html.escape(str(status.get('error', ''))) + "</span></p>" if status.get("error") else "<p class='nav-meta'><b>Error:</b> <span data-status-error>None</span></p>"}
              <ul class="status-events" data-status-events>{''.join(f'<li>{html.escape(str(item))}</li>' for item in status.get('recent_events', [])[-8:]) or '<li>No events yet.</li>'}</ul>
            </div>
            <div class="stats">
              <div class="stat"><strong>{stats['wiki_pages']}</strong><span>AI-managed pages</span></div>
              <div class="stat"><strong>{stats['staging_pages']}</strong><span>Staged pages</span></div>
              <div class="stat"><strong>{stats['raw_files']}</strong><span>Uploaded raw files</span></div>
              <div class="stat"><strong>{stats['sources']}</strong><span>Source summaries</span></div>
              <div class="stat"><strong>{stats['entities'] + stats['concepts'] + stats['queries']}</strong><span>Derived knowledge pages</span></div>
              <div class="stat"><strong>{stats['revisions']}</strong><span>Recorded ingests</span></div>
            </div>
            <div class="section-grid">
              <section class="section-card">
                <h2>About This Wiki</h2>
                <span class="chip">overview</span>
                <span class="chip">source summaries</span>
                <span class="chip">entities</span>
                <span class="chip">concepts</span>
                <span class="chip">query artifacts</span>
                <p><b>Solo-Corp OS</b> is a locally hosted Co-Founder Brain where raw uploads live in <span class="mono">raw/</span> and curated knowledge lives in <span class="mono">wiki/</span>. Each ingest can revise multiple related pages and leave a revision record behind.</p>
              </section>
              <section class="section-card">
                <h2>Recently Updated Pages</h2>
                {render_items(latest_pages)}
              </section>
              <section class="section-card">
                <h2>Recently Uploaded Sources</h2>
                {render_items(latest_raw, raw=True)}
              </section>
              <section class="section-card">
                <h2>Staging Queue</h2>
                {render_items(latest_staging)}
              </section>
              <section class="section-card">
                <h2>Recent Revisions</h2>
                {self.render_revision_list(revisions)}
              </section>
              <section class="section-card">
                <h2>Recent Batches</h2>
                {self.render_batch_list(batches)}
              </section>
            </div>
          </article>
          <aside class="panel">
            <h2>Contribute Source Material</h2>
            <form class="upload-form" method="post" action="/upload" enctype="multipart/form-data">
              <input type="file" name="file" multiple webkitdirectory directory required>
              <button type="submit">Upload Folder</button>
              <p class="upload-status" data-upload-status>Select a folder to start uploading immediately.</p>
            </form>
            <form class="upload-form" method="post" action="/upload" enctype="multipart/form-data" style="margin-top: 14px;">
              <input type="file" name="file" multiple accept=".md,.txt,.csv,.json,.pdf,.xlsx,.xls" required>
              <input type="text" name="filename" placeholder="Optional custom filename for single-file uploads">
              <textarea name="notes" rows="6" placeholder="Optional notes appended to a single raw markdown/text stub"></textarea>
              <button type="submit">Upload Files</button>
              <p class="upload-status" data-upload-status>Select one or more files to start uploading immediately.</p>
            </form>
            <p class="muted">Folder upload preserves nested paths beneath <span class="mono">raw/sources/</span>. File upload is better when you want to pick a specific Excel, PDF, markdown, or JSON file directly.</p>
            <p class="upload-note">Use the folder picker for directory imports and the file picker for individual files or ad hoc multi-file uploads.</p>
            <h2>Search and Analysis</h2>
            <form class="query-form" method="post" action="/query">
              <textarea name="question" rows="5" placeholder="Ask a question against the wiki" required></textarea>
              <button type="submit">Search This Wiki</button>
            </form>
            <form class="query-form" method="post" action="/review">
              <button type="submit">Review And Repair Wiki</button>
            </form>
            <form class="query-form" method="post" action="/lint">
              <button type="submit">Run Maintenance Report</button>
            </form>
            <h2>Project Information</h2>
            <p class="muted">{'Vertex is configured and ready for source ingestion and maintenance passes.' if vertex_config().configured else 'Vertex config is missing. Set VERTEX_API, MODEL_ID, and PROJECT_ID in .env.'}</p>
            <h2>Workspaces</h2>
            <p class="muted">Current workspace: <span class="mono">{html.escape(display_workspace_name(CURRENT_WORKSPACE))}</span>. Each workspace has its own raw uploads, wiki pages, batches, and chat state.</p>
            <form class="query-form" method="post" action="/workspace/select">
              <select name="workspace" required>
                {workspace_options}
              </select>
              <button type="submit">Switch Workspace</button>
            </form>
            <form class="query-form" method="post" action="/workspace/create">
              <input type="text" name="workspace_name" placeholder="New workspace name" required>
              <button type="submit">Create Workspace</button>
            </form>
            <h2>Navigation</h2>
            <ul class="item-list">
              <li><a href="/page/index.md">Wiki index</a><div class="nav-meta">Catalog of AI-managed pages.</div></li>
              <li><a href="/page/log.md">Activity log</a><div class="nav-meta">Chronological record of ingest, query, and lint passes.</div></li>
              <li><a href="/revisions">Revisions</a><div class="nav-meta">Per-ingest manifests of all AI-touched pages.</div></li>
              <li><a href="/queries">Queries</a><div class="nav-meta">Durable answer and lint artifacts written back into the wiki.</div></li>
              <li><a href="/staging">Staging</a><div class="nav-meta">Sources that were held or assessed before active promotion.</div></li>
              <li><a href="/archive">Archive</a><div class="nav-meta">Merged, demoted, and archived wiki pages.</div></li>
              <li><a href="/batches">Batches</a><div class="nav-meta">Background ingestion jobs and maintenance sweeps.</div></li>
              <li><a href="/raw/sources/">Raw sources directory</a><div class="nav-meta">Source of truth for uploaded files.</div></li>
            </ul>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Main Page", body=body, pages=pages, flash=flash))

    def render_page(self, relative_path: str) -> None:
        pages = scan_wiki_pages()
        special = {"index.md", "log.md"}
        if relative_path in special:
            target = WIKI_ROOT / relative_path
        else:
            target = normalize_repo_path(WIKI_ROOT, relative_path)
        if target is None or not target.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Page not found")
            return

        text = read_text_file(target)
        title = extract_title(target)
        body_html = render_markdown(text, target.parent.relative_to(WIKI_ROOT).as_posix(), "wiki")
        rel_path = target.relative_to(WIKI_ROOT).as_posix()
        section_key = rel_path.split("/", 1)[0] if "/" in rel_path else "overview"
        chips = [
            "<span class='chip'>AI managed</span>",
            f"<span class='chip'>{html.escape(rel_path)}</span>",
            f"<span class='chip'>Updated {dt.datetime.fromtimestamp(target.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}</span>",
        ]
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">Wiki / {html.escape(target.relative_to(WIKI_ROOT).as_posix())}</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">{html.escape(title)}</h1>
            <p class="subtitle">{''.join(chips)}</p>
            <div class="article-body">{body_html}</div>
          </article>
          <aside class="panel">
            <h2>Page Metadata</h2>
            <p class="muted">This page lives in the AI-maintained wiki layer.</p>
              <ul class="item-list">
                <li><strong>Path</strong><div class="nav-meta">{html.escape(target.relative_to(ROOT).as_posix())}</div></li>
                <li><strong>Section</strong><div class="nav-meta">{html.escape(SECTIONS.get(section_key, 'Overview'))}</div></li>
              </ul>
          </aside>
        </div>
        """
        self.write_html(page_shell(title=title, body=body, pages=pages))

    def render_raw(self, relative_path: str) -> None:
        pages = scan_wiki_pages()
        if not relative_path or relative_path.endswith("/"):
            prefix = relative_path.strip("/")
            items = [page for page in scan_raw_files() if page.rel_path.startswith(prefix)] if prefix else scan_raw_files()
            listing = "".join(
                f"<li><a href='/raw/{quote(item.rel_path)}'>{html.escape(item.rel_path)}</a><div class='nav-meta'>{html.escape(item.updated)} · {html.escape(raw_ingest_status(item.path))}</div></li>"
                for item in items
            ) or "<li class='muted'>No uploaded files.</li>"
            body = f"""
            <div class="toolbar">
              <div class="breadcrumbs">Raw uploads / {html.escape(prefix or 'all')}</div>
              <a class="button-link" href="/">Main page</a>
            </div>
            <div class="shell">
              <article class="page">
                <h1 class="article-title">Raw Source Files</h1>
                <p class="subtitle">These are uploaded source files. The AI should read them but not rewrite them.</p>
                <ul class="item-list">{listing}</ul>
              </article>
              <aside class="panel">
                <h2>Raw Layer</h2>
                <p class="muted">This is the immutable source-of-truth layer that feeds the wiki.</p>
              </aside>
            </div>
            """
            self.write_html(page_shell(title="Raw Sources", body=body, pages=pages))
            return

        target = normalize_repo_path(RAW_ROOT, relative_path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Raw file not found")
            return

        mime = mimetypes.guess_type(target.name)[0] or "text/plain"
        if mime.startswith("text") or target.suffix.lower() in {".md", ".txt", ".csv", ".json", ".pdf", ".xlsx", ".xls"}:
            text = read_text_file(target)
            ingest_text = read_text_file(target, for_ingest=True)
            workbook_info = extract_excel_workbook(target) if target.suffix.lower() in {".xlsx", ".xls"} else None
            source_profile = build_source_profile(target, ingest_text, workbook_info) if ingest_text.strip() else None
            if target.suffix.lower() == ".md":
                rendered = render_markdown(text, target.parent.relative_to(RAW_ROOT).as_posix(), "raw")
            elif target.suffix.lower() == ".pdf":
                preview = text[:24000] if text else "No extractable PDF text was found."
                rendered = f"<pre><code>{html.escape(preview)}</code></pre>"
            elif target.suffix.lower() in {".xlsx", ".xls"}:
                preview = text[:24000] if text else "No extractable workbook text was found."
                rendered = f"<pre><code>{html.escape(preview)}</code></pre>"
            else:
                rendered = f"<pre><code>{html.escape(text)}</code></pre>"
            body = f"""
            <div class="toolbar">
              <div class="breadcrumbs">Raw / {html.escape(target.relative_to(RAW_ROOT).as_posix())}</div>
              <a class="button-link" href="/">Main page</a>
            </div>
            <div class="shell">
              <article class="page">
                <h1 class="article-title">{html.escape(target.name)}</h1>
                <p class="subtitle"><span class="chip">Raw upload</span><span class="chip">{html.escape(target.relative_to(ROOT).as_posix())}</span></p>
                <form class="upload-form" method="post" action="/ingest" style="margin: 18px 0 20px;">
                  <input type="hidden" name="path" value="{html.escape(target.relative_to(RAW_ROOT).as_posix(), quote=True)}">
                  <button type="submit">Reingest With AI</button>
                </form>
                <div class="article-body">{rendered}</div>
              </article>
              <aside class="panel">
                <h2>File Metadata</h2>
                <ul class="item-list">
                  <li><strong>Type</strong><div class="nav-meta">{html.escape(mime)}</div></li>
                  <li><strong>Source class</strong><div class="nav-meta">{html.escape(str(source_profile.get('source_type_label', 'Unknown')) if source_profile else 'Unknown')}</div></li>
                  <li><strong>Extraction quality</strong><div class="nav-meta">{html.escape(str(source_profile.get('quality_label', 'unknown')) if source_profile else 'unusable')}</div></li>
                  <li><strong>Updated</strong><div class="nav-meta">{dt.datetime.fromtimestamp(target.stat().st_mtime).strftime('%Y-%m-%d %H:%M')}</div></li>
                  <li><strong>Extraction</strong><div class="nav-meta">{'PDF text extracted for AI ingest.' if target.suffix.lower() == '.pdf' and text else ('PDF uploaded but no usable text was extracted.' if target.suffix.lower() == '.pdf' else ('Workbook sheets extracted for AI ingest.' if target.suffix.lower() in {'.xlsx', '.xls'} and text else ('Workbook uploaded but no usable sheet text was extracted.' if target.suffix.lower() in {'.xlsx', '.xls'} else 'Direct text read.')))}</div></li>
                  <li><strong>Decision hint</strong><div class="nav-meta">{html.escape(str(source_profile.get('quality_reason', 'No decision hint.')) if source_profile else 'No usable extraction was found.')}</div></li>
                  <li><strong>AI action</strong><div class="nav-meta">This file is auto-ingested on upload. Use this button only to refresh the wiki page.</div></li>
                </ul>
              </aside>
            </div>
            """
            self.write_html(page_shell(title=target.name, body=body, pages=pages))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.end_headers()
        self.wfile.write(target.read_bytes())

    def render_search(self, query: dict[str, list[str]]) -> None:
        pages = scan_wiki_pages()
        raw_files = scan_raw_files()
        term = query.get("q", [""])[0].strip().lower()

        def matches(page: Page) -> bool:
            haystack = f"{page.title}\n{page.summary}\n{page.rel_path}".lower()
            return term in haystack

        wiki_hits = [page for page in pages if term and matches(page)]
        raw_hits = [page for page in raw_files if term and matches(page)]

        def render_hits(items: list[Page], raw: bool = False) -> str:
            if not items:
                return "<p class='muted'>No matches.</p>"
            rows = []
            for item in items:
                href = f"/raw/{quote(item.rel_path)}" if raw else f"/page/{quote(item.rel_path)}"
                rows.append(
                    "<li>"
                    f"<a href='{href}'>{html.escape(item.title)}</a>"
                    f"<div class='nav-meta'>{html.escape(item.rel_path)} · {html.escape(item.summary)}</div>"
                    "</li>"
                )
            return f"<ul class='item-list'>{''.join(rows)}</ul>"

        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">Search / {html.escape(term or 'empty query')}</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Search results</h1>
            <p class="subtitle">Term: <span class="mono">{html.escape(term or '(empty)')}</span></p>
            <h2>AI-managed pages</h2>
            {render_hits(wiki_hits)}
            <h2>Raw uploads</h2>
            {render_hits(raw_hits, raw=True)}
          </article>
          <aside class="panel">
            <h2>Search Scope</h2>
            <p class="muted">Search checks titles, summaries, and relative paths across both the raw source layer and the AI-maintained wiki.</p>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Search", body=body, pages=pages))

    def render_revision_list(self, revisions: list[dict[str, object]]) -> str:
        if not revisions:
            return "<p class='muted'>No revisions yet.</p>"
        rows = []
        for revision in revisions:
            touched = revision.get("touched_pages", [])
            rows.append(
                "<li>"
                f"<a href='/revision/{quote(str(revision.get('id', '')))}'>{html.escape(str(revision.get('id', '')))}</a>"
                f"<div class='nav-meta'>{html.escape(str(revision.get('raw_source', '')))} · {len(touched) if isinstance(touched, list) else 0} pages touched</div>"
                "</li>"
            )
        return f"<ul class='item-list'>{''.join(rows)}</ul>"

    def render_batch_list(self, batches: list[dict[str, object]]) -> str:
        if not batches:
            return "<p class='muted'>No batches yet.</p>"
        rows = []
        for batch in batches:
            left_metric, right_metric = describe_batch_metrics(batch)
            rows.append(
                "<li>"
                f"<strong>{html.escape(str(batch.get('id', '')))}</strong>"
                f"<div class='nav-meta'>{html.escape(str(batch_job_label(str(batch.get('job_type', 'ingest')))))} · {html.escape(str(batch.get('status', 'unknown')))} · {html.escape(left_metric)} · {html.escape(right_metric)}</div>"
                "</li>"
            )
        return f"<ul class='item-list'>{''.join(rows)}</ul>"

    def render_batches(self) -> None:
        pages = scan_wiki_pages()
        batches = recent_batches(50)
        status = read_status()
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">System / batches</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Background Jobs</h1>
            <p class="subtitle">Uploads, review passes, assistant actions, and queries all run as background jobs so the wiki can keep working after the HTTP request returns.</p>
            <div class="status-box">
              <h2>Live Queue Status</h2>
              <div class="status-grid">
                <div class="status-line"><strong>{'Running' if status.get('active') else 'Idle'}</strong>Status</div>
                <div class="status-line"><strong>{html.escape(str(status.get('job_label', '') or 'None'))}</strong>Job Type</div>
                <div class="status-line"><strong>{html.escape(str(status.get('queue_depth', 0)))}</strong>Queued jobs</div>
                <div class="status-line"><strong>{html.escape(str(status.get('batch_id', '') or 'None'))}</strong>Current batch</div>
                <div class="status-line"><strong>{html.escape(str(status.get('phase', 'idle')))}</strong>Phase</div>
                <div class="status-line"><strong>{html.escape(status_progress_label(status))}</strong>Progress</div>
              </div>
              <p class="nav-meta" style="margin-top: 10px;"><b>Current target:</b> {html.escape(str(status.get('current_file', '') or 'None'))}</p>
              <p class="nav-meta"><b>Current step:</b> {html.escape(str(status.get('current_step', '') or 'Idle'))}</p>
              <p class="nav-meta"><b>Last event:</b> {html.escape(str(status.get('last_event', '') or 'No activity yet.'))}</p>
              <p class="nav-meta"><b>Error:</b> {html.escape(str(status.get('error', '') or 'None'))}</p>
            </div>
            {self.render_batch_list(batches)}
          </article>
          <aside class="panel">
            <h2>Behavior</h2>
            <p class="muted">Files save immediately, then the worker handles ingest, review, direct wiki actions, and durable queries without blocking the browser.</p>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Batches", body=body, pages=pages))

    def render_archive(self) -> None:
        pages = scan_wiki_pages()
        archived = scan_wiki_pages(include_archived=True)
        archive_pages = [page for page in archived if page.archived]
        rows = []
        for page in archive_pages:
            meta = page_metadata(page)
            rows.append(
                "<li>"
                f"<a href='/page/{quote(page.rel_path)}'>{html.escape(page.title)}</a>"
                f"<div class='nav-meta'>{html.escape(page.rel_path)} · {html.escape(str(meta.get('confidence', 'archived')))} · {html.escape(str(meta.get('source_count', 0)))} sources</div>"
                "</li>"
            )
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">System / archive</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Archive</h1>
            <p class="subtitle">Archived pages are kept for traceability but excluded from the active wiki index and navigation surface.</p>
            <ul class="item-list">{''.join(rows) or "<li class='muted'>No archived pages yet.</li>"}</ul>
          </article>
          <aside class="panel">
            <h2>Archive Policy</h2>
            <p class="muted">Pages land here when they are merged, too weakly sourced, or too noisy for the active wiki. They remain inspectable but no longer define the canonical layer.</p>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Archive", body=body, pages=pages))

    def render_staging(self) -> None:
        pages = scan_wiki_pages()
        staging_pages = scan_staging_pages()
        rows = []
        for page in staging_pages:
            rows.append(
                "<li>"
                f"<a href='/page/{quote(page.rel_path)}'>{html.escape(page.title)}</a>"
                f"<div class='nav-meta'>{html.escape(page.rel_path)} · {html.escape(page.summary)} · {html.escape(page.updated)}</div>"
                "</li>"
            )
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">System / staging</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Staging</h1>
            <p class="subtitle">Every ingest is assessed here first. Low-quality or ambiguous sources stay here until they are strong enough for the active wiki.</p>
            <ul class="item-list">{''.join(rows) or "<li class='muted'>No staged sources yet.</li>"}</ul>
          </article>
          <aside class="panel">
            <h2>Promotion Rule</h2>
            <p class="muted">Only sources with at least medium extraction quality promote into the active wiki. Thin or ambiguous extractions stay here so they do not pollute entities, concepts, and overview pages.</p>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Staging", body=body, pages=pages))

    def render_revisions(self) -> None:
        pages = scan_wiki_pages()
        revisions = recent_revisions(50)
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">System / revisions</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Revisions</h1>
            <p class="subtitle">Each ingest stores a manifest of what the AI changed across the wiki.</p>
            {self.render_revision_list(revisions)}
          </article>
          <aside class="panel">
            <h2>Why this exists</h2>
            <p class="muted">A source ingest should update multiple pages. Revisions let you inspect each operation as a cohesive change set.</p>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Revisions", body=body, pages=pages))

    def render_queries(self) -> None:
        pages = scan_wiki_pages()
        query_pages = [page for page in pages if page.section == "queries"]
        rows = []
        for page in sorted(query_pages, key=lambda item: item.path.stat().st_mtime, reverse=True):
            rows.append(
                "<li>"
                f"<a href='/page/{quote(page.rel_path)}'>{html.escape(page.title)}</a>"
                f"<div class='nav-meta'>{html.escape(page.summary)} · {html.escape(page.updated)}</div>"
                "</li>"
            )
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">System / queries</div>
          <a class="button-link" href="/">Main page</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">Query Artifacts</h1>
            <p class="subtitle">Durable answers and lint reports filed back into the wiki.</p>
            <ul class="item-list">{''.join(rows) or "<li class='muted'>No query artifacts yet.</li>"}</ul>
          </article>
          <aside class="panel">
            <h2>Behavior</h2>
            <p class="muted">Questions should compound into reusable pages instead of disappearing into chat history.</p>
          </aside>
        </div>
        """
        self.write_html(page_shell(title="Queries", body=body, pages=pages))

    def render_revision(self, revision_id: str) -> None:
        pages = scan_wiki_pages()
        path = normalize_repo_path(REVISIONS_ROOT, f"{revision_id}.json")
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Revision not found")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        touched = data.get("touched_pages", [])
        touched_html = "".join(
            f"<li><a href='/page/{quote(str(page))}'>{html.escape(str(page))}</a></li>"
            for page in touched
        ) if isinstance(touched, list) else "<li class='muted'>No touched pages recorded.</li>"
        key_points = data.get("key_points", [])
        points_html = "".join(f"<li>{html.escape(str(item))}</li>" for item in key_points) if isinstance(key_points, list) else ""
        body = f"""
        <div class="toolbar">
          <div class="breadcrumbs">Revision / {html.escape(revision_id)}</div>
          <a class="button-link" href="/revisions">All revisions</a>
        </div>
        <div class="shell">
          <article class="page">
            <h1 class="article-title">{html.escape(revision_id)}</h1>
            <p class="subtitle"><span class='chip'>{html.escape(str(data.get('raw_source', '')))}</span><span class='chip'>{html.escape(str(data.get('created_at', '')))}</span></p>
            <div class="article-body">
              <h2>Summary</h2>
              <p>{html.escape(str(data.get('summary', '')))}</p>
              <h2>Touched Pages</h2>
              <ul>{touched_html}</ul>
              <h2>Key Points</h2>
              <ul>{points_html}</ul>
            </div>
          </article>
          <aside class="panel">
            <h2>Revision Metadata</h2>
            <ul class="item-list">
              <li><strong>Source Page</strong><div class="nav-meta"><a href="/page/{quote(str(data.get('source_page', '')))}">{html.escape(str(data.get('source_page', '')))}</a></div></li>
              <li><strong>Entities</strong><div class="nav-meta">{html.escape(', '.join(data.get('entities', [])) if isinstance(data.get('entities', []), list) else '')}</div></li>
              <li><strong>Concepts</strong><div class="nav-meta">{html.escape(', '.join(data.get('concepts', [])) if isinstance(data.get('concepts', []), list) else '')}</div></li>
            </ul>
          </aside>
        </div>
        """
        self.write_html(page_shell(title=revision_id, body=body, pages=pages))

    def handle_upload(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self.send_error(HTTPStatus.BAD_REQUEST, "Expected multipart form data")
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
        )

        uploaded_files: list[dict[str, object]] = []
        notes = ""
        override_name = ""
        relative_paths: list[str] = []

        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if name == "file":
                uploaded_name = part.get_filename() or ""
                payload = part.get_payload(decode=True)
                uploaded_files.append(
                    {
                        "filename": uploaded_name,
                        "bytes": payload if payload is not None else b"",
                    }
                )
            elif name == "notes":
                notes = part.get_content().strip()
            elif name == "filename":
                override_name = part.get_content().strip()
            elif name == "relative_path":
                relative_paths.append(part.get_content().strip())

        if not uploaded_files:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing uploaded file")
            return

        batch_id = start_batch_status(len(uploaded_files))
        log_event("upload:start", f"items={len(uploaded_files)}")

        saved_paths: list[str] = []
        paths_to_ingest: list[Path] = []
        skipped_paths: list[str] = []

        for idx, uploaded in enumerate(uploaded_files):
            uploaded_name = str(uploaded["filename"])
            uploaded_bytes = uploaded["bytes"]
            if not uploaded_name or not uploaded_bytes:
                continue

            if len(uploaded_files) == 1 and override_name:
                rel_path = Path(sanitize_filename(override_name))
            else:
                rel_hint = relative_paths[idx] if idx < len(relative_paths) else uploaded_name
                rel_path = sanitize_relative_upload_path(rel_hint, uploaded_name)

            if should_skip_upload_path(rel_path):
                skipped_paths.append(rel_path.as_posix())
                continue

            destination = RAW_SOURCES / rel_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(uploaded_bytes)
            saved_paths.append(destination.relative_to(RAW_ROOT).as_posix())
            log_event("upload:saved", destination.relative_to(RAW_ROOT).as_posix())
            update_status(
                batch_id=batch_id,
                job_type="ingest",
                job_label=batch_job_label("ingest"),
                phase="saving",
                saved_count=len(saved_paths),
                current_file=destination.relative_to(RAW_ROOT).as_posix(),
                current_step="Saving uploaded files",
                last_event=f"Saved {destination.relative_to(RAW_ROOT).as_posix()}",
                event=f"saved {destination.relative_to(RAW_ROOT).as_posix()}",
            )

            if notes and len(uploaded_files) == 1 and destination.suffix.lower() in {".md", ".txt", ".csv", ".json"}:
                existing = destination.read_text(encoding="utf-8")
                destination.write_text(existing + "\n\n## Upload Notes\n\n" + notes + "\n", encoding="utf-8")

            if should_ingest_path(destination) and (
                destination.suffix.lower() not in {".pdf", ".xlsx", ".xls", ".json"} or bool(read_text_file(destination, for_ingest=True).strip())
            ):
                paths_to_ingest.append(destination)
            elif destination.suffix.lower() == ".pdf":
                skipped_paths.append(destination.relative_to(RAW_ROOT).as_posix() + " (no usable text)")
            elif destination.suffix.lower() in {".xlsx", ".xls"}:
                skipped_paths.append(destination.relative_to(RAW_ROOT).as_posix() + " (no usable sheet text)")
            elif destination.suffix.lower() == ".json":
                skipped_paths.append(destination.relative_to(RAW_ROOT).as_posix() + " (no usable JSON text)")

        batch_payload = {
            "id": batch_id,
            "job_type": "ingest",
            "status": "queued",
            "phase": "queued",
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "workspace": CURRENT_WORKSPACE,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "completed_at": "",
            "saved_count": len(saved_paths),
            "total_items": len(saved_paths),
            "ingest_total": len(paths_to_ingest),
            "ingest_completed": 0,
            "failure_count": 0,
            "saved_paths": saved_paths,
            "text_paths": [path.relative_to(RAW_ROOT).as_posix() for path in paths_to_ingest],
            "skipped_paths": skipped_paths,
            "current_file": "",
            "successes": [],
            "failures": [],
            "maintenance_report": "",
            "revisions_created": 0,
        }
        write_batch(batch_id, batch_payload)
        INGEST_QUEUE.put(batch_payload)
        update_status(
            batch_id=batch_id,
            active=True,
            job_type="ingest",
            job_label=batch_job_label("ingest"),
            phase="queued",
            saved_count=len(saved_paths),
            total_items=len(saved_paths),
            ingest_total=len(paths_to_ingest),
            ingest_completed=0,
            failure_count=0,
            current_file="",
            current_step="Queued for background ingest",
            queue_depth=INGEST_QUEUE.qsize(),
            last_event=f"Queued batch {batch_id} with {len(paths_to_ingest)} text files.",
            event=f"queued batch {batch_id}",
        )

        flash = f"Uploaded {len(saved_paths)} files and queued {len(paths_to_ingest)} text files for background ingest."
        if skipped_paths:
            flash += f" Skipped {len(skipped_paths)} hidden/system files."

        log_event("upload:done", f"saved={len(saved_paths)} | queued={len(paths_to_ingest)} | skipped={len(skipped_paths)} | batch={batch_id}")
        self.redirect(f"/?flash={quote(flash)}")

    def handle_ingest(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        relative_path = form.get("path", [""])[0]
        target = normalize_repo_path(RAW_ROOT, relative_path)
        if target is None:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid raw path")
            return

        try:
            wiki_rel_path = ingest_raw_file(target.relative_to(RAW_ROOT).as_posix())
        except RuntimeError as exc:
            log_event("ingest:error", f"{target.relative_to(RAW_ROOT).as_posix()} | {str(exc)}")
            self.redirect("/?flash=" + quote(f"Ingest failed: {str(exc)}"))
            return

        self.redirect("/?flash=" + quote(f"Ingested {target.name} with Vertex and updated {wiki_rel_path}."))

    def handle_query(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        question = form.get("question", [""])[0].strip()
        if not question:
            self.redirect("/?flash=" + quote("Query failed: question was empty."))
            return
        log_event("query:start", question[:120])
        config = vertex_config()
        if not config.configured:
            self.redirect("/?flash=" + quote("Query failed: Vertex is not fully configured."))
            return
        pages = select_relevant_wiki_pages(question)
        if not pages:
            self.redirect("/?flash=" + quote("Query failed: no relevant wiki pages found yet. Ingest more sources first."))
            return
        job_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-query"
        job = {
            "id": job_id,
            "job_type": "query",
            "status": "queued",
            "phase": "queued",
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "workspace": CURRENT_WORKSPACE,
            "question": question,
            "tagged_pages": [page.rel_path for page in pages],
            "context_count": len(pages),
        }
        write_batch(job_id, job)
        INGEST_QUEUE.put(job)
        update_status(
            active=True,
            batch_id=job_id,
            job_type="query",
            job_label=batch_job_label("query"),
            phase="queued",
            current_file=question[:120] or "wiki query",
            current_step="Queued wiki query",
            context_count=len(pages),
            error="",
            queue_depth=INGEST_QUEUE.qsize(),
            last_event=f"Queued wiki query job {job_id}.",
            event=f"queued query {job_id}",
        )
        self.redirect("/?flash=" + quote(f"Queued wiki query job {job_id}. Follow progress on /batches."))

    def queue_assistant_request(self, message: str, raw_tags: str, *, respond_json: bool) -> None:
        tagged_pages = resolve_tagged_pages(raw_tags)
        action_hint = normalize_assistant_action(message)
        review_state = read_review_state()

        def reply_success(job_id: str, label: str) -> None:
            if respond_json:
                self.write_json({"ok": True, "job_id": job_id, "message": label}, status=HTTPStatus.ACCEPTED)
            else:
                self.redirect("/?flash=" + quote(label))

        def reply_error(label: str) -> None:
            append_chat_message("assistant", f"Request failed.\n\n{label}", status="failed", kind="system")
            if respond_json:
                self.write_json({"ok": False, "error": label}, status=HTTPStatus.BAD_REQUEST)
            else:
                self.redirect("/?flash=" + quote(label))

        append_chat_message("user", message, kind="clarification" if review_state.get("pending") else ("action" if action_hint else "query"))

        if review_state.get("pending"):
            log_event("assistant:clarify", message[:120])
            job_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-clarification"
            assistant_message_id = append_chat_message("assistant", "Working on your clarification...", status="pending", kind="clarification", batch_id=job_id)
            job = {
                "id": job_id,
                "job_type": "clarification",
                "status": "queued",
                "phase": "queued",
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "workspace": CURRENT_WORKSPACE,
                "message": message,
                "tagged_pages": [page.rel_path for page in tagged_pages],
                "assistant_message_id": assistant_message_id,
            }
            write_batch(job_id, job)
            INGEST_QUEUE.put(job)
            update_status(
                active=True,
                batch_id=job_id,
                job_type="clarification",
                job_label=batch_job_label("clarification"),
                phase="queued",
                queue_depth=INGEST_QUEUE.qsize(),
                current_file="review clarification",
                current_step="Queued clarification job",
                tagged_count=len(tagged_pages),
                updated_pages=0,
                error="",
                last_event=f"Queued clarification job {job_id}.",
                event=f"queued clarification {job_id}",
            )
            reply_success(job_id, f"Queued clarification job {job_id}. Follow progress on /batches.")
            return

        if action_hint:
            pages = tagged_pages or resolve_pages_from_message(message) or select_relevant_wiki_pages(message)
            if action_hint != "create" and not pages:
                reply_error("Assistant action failed: no target page was identified. Tag docs or name the page more explicitly.")
                return
            log_event("assistant:action", f"{action_hint} | {message[:120]}")
            job_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-action"
            assistant_message_id = append_chat_message("assistant", "Planning the requested wiki action...", status="pending", kind="action", batch_id=job_id)
            job = {
                "id": job_id,
                "job_type": "action",
                "status": "queued",
                "phase": "queued",
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "workspace": CURRENT_WORKSPACE,
                "message": message,
                "action_hint": action_hint,
                "tagged_pages": [page.rel_path for page in pages],
                "context_count": len(pages),
                "assistant_message_id": assistant_message_id,
            }
            write_batch(job_id, job)
            INGEST_QUEUE.put(job)
            update_status(
                active=True,
                batch_id=job_id,
                job_type="action",
                job_label=batch_job_label("action"),
                phase="queued",
                current_file=message[:120] or "wiki action",
                current_step="Queued wiki document action",
                planned_actions=0,
                updated_pages=0,
                context_count=len(pages),
                error="",
                queue_depth=INGEST_QUEUE.qsize(),
                last_event=f"Queued wiki action job {job_id}.",
                event=f"queued action {job_id}",
            )
            reply_success(job_id, f"Queued wiki action job {job_id}. Follow progress on /batches.")
            return

        pages = tagged_pages or select_relevant_wiki_pages(message)
        if not pages:
            reply_error("Assistant request failed: no relevant wiki pages found yet.")
            return
        log_event("assistant:query", message[:120])
        job_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-query"
        assistant_message_id = append_chat_message("assistant", "Searching the wiki and drafting an answer...", status="pending", kind="query", batch_id=job_id)
        job = {
            "id": job_id,
            "job_type": "query",
            "status": "queued",
            "phase": "queued",
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "workspace": CURRENT_WORKSPACE,
            "question": message,
            "tagged_pages": [page.rel_path for page in pages],
            "context_count": len(pages),
            "assistant_message_id": assistant_message_id,
        }
        write_batch(job_id, job)
        INGEST_QUEUE.put(job)
        update_status(
            active=True,
            batch_id=job_id,
            job_type="query",
            job_label=batch_job_label("query"),
            phase="queued",
            current_file=message[:120] or "assistant query",
            current_step="Queued assistant query",
            context_count=len(pages),
            error="",
            queue_depth=INGEST_QUEUE.qsize(),
            last_event=f"Queued assistant query job {job_id}.",
            event=f"queued assistant query {job_id}",
        )
        reply_success(job_id, f"Queued assistant query job {job_id}. Follow progress on /batches.")

    def handle_assistant(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        message = form.get("message", [""])[0].strip()
        raw_tags = form.get("tags", [""])[0].strip()
        if not message:
            self.redirect("/?flash=" + quote("Assistant request failed: message was empty."))
            return
        config = vertex_config()
        if not config.configured:
            self.redirect("/?flash=" + quote("Assistant request failed: Vertex is not fully configured."))
            return
        self.queue_assistant_request(message, raw_tags, respond_json=False)

    def handle_chat(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        message = form.get("message", [""])[0].strip()
        raw_tags = form.get("tags", [""])[0].strip()
        if not message:
            self.write_json({"ok": False, "error": "Chat request failed: message was empty."}, status=HTTPStatus.BAD_REQUEST)
            return
        config = vertex_config()
        if not config.configured:
            self.write_json({"ok": False, "error": "Chat request failed: Vertex is not fully configured."}, status=HTTPStatus.BAD_REQUEST)
            return
        self.queue_assistant_request(message, raw_tags, respond_json=True)

    def handle_workspace_create(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        requested = form.get("workspace_name", [""])[0].strip()
        if not requested:
            self.redirect("/?flash=" + quote("Workspace creation failed: name was empty."))
            return
        workspace_name = workspace_slug(requested)
        if workspace_name == DEFAULT_WORKSPACE or workspace_name in list_workspaces():
            self.redirect("/?flash=" + quote(f"Workspace creation failed: {workspace_name} already exists."))
            return
        initialize_workspace_files(workspace_name)
        self.redirect("/?flash=" + quote(f"Created workspace {workspace_name}. Switch to it when you are ready."))

    def handle_workspace_select(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw_body)
        requested = workspace_slug(form.get("workspace", [""])[0].strip())
        if not requested:
            self.redirect("/?flash=" + quote("Workspace switch failed: no workspace selected."))
            return
        if requested not in list_workspaces():
            self.redirect("/?flash=" + quote(f"Workspace switch failed: {requested} does not exist."))
            return
        status = read_status()
        if status.get("active") or INGEST_QUEUE.qsize() > 0:
            self.redirect("/?flash=" + quote("Workspace switch failed: background jobs are still running. Wait until the queue is idle."))
            return
        selected = switch_workspace(requested)
        self.redirect("/?flash=" + quote(f"Switched to workspace {selected}."))

    def handle_review(self) -> None:
        log_event("review:start")
        config = vertex_config()
        if not config.configured:
            self.redirect("/?flash=" + quote("Review failed: Vertex is not fully configured."))
            return
        pages = scan_wiki_pages()
        if not pages:
            self.redirect("/?flash=" + quote("Review failed: the wiki has no pages yet."))
            return
        job_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-review"
        job = {
            "id": job_id,
            "job_type": "review",
            "status": "queued",
            "phase": "queued",
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "workspace": CURRENT_WORKSPACE,
        }
        write_batch(job_id, job)
        INGEST_QUEUE.put(job)
        update_status(
            active=True,
            batch_id=job_id,
            job_type="review",
            job_label=batch_job_label("review"),
            phase="queued",
            queue_depth=INGEST_QUEUE.qsize(),
            current_file="wiki review",
            current_step="Queued wiki review",
            pages_scanned=len(pages),
            question_count=0,
            error="",
            last_event=f"Queued wiki maintenance job {job_id}.",
            event=f"queued review {job_id}",
        )
        self.redirect("/?flash=" + quote(f"Queued wiki maintenance job {job_id}. Follow progress on /batches."))

    def handle_lint(self) -> None:
        log_event("lint:start")
        report = run_lint_pass()
        rel_path = write_lint_report(report)
        log_event("lint:done", f"{rel_path} | {report.get('summary', '')}")
        self.redirect("/?flash=" + quote(f"Lint completed and wrote {rel_path}."))

    def write_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def write_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        log_event("http", fmt % args)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return cleaned or f"upload-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"


def sanitize_relative_upload_path(path_value: str, fallback_name: str) -> Path:
    normalized = path_value.replace("\\", "/").strip()
    raw_parts = Path(normalized).parts if normalized else (fallback_name,)
    cleaned_parts: list[str] = []
    for part in raw_parts:
        if part in {"", ".", ".."}:
            continue
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip(".-")
        if safe:
            cleaned_parts.append(safe)
    if not cleaned_parts:
        cleaned_parts = [sanitize_filename(fallback_name)]
    return Path(*cleaned_parts)


def should_ingest_path(path: Path) -> bool:
    if path.name == ".DS_Store":
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    return path.suffix.lower() in {".md", ".txt", ".csv", ".json", ".pdf", ".xlsx", ".xls"}


def should_skip_upload_path(path: Path) -> bool:
    if path.name == ".DS_Store":
        return True
    return any(part.startswith(".") for part in path.parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    configure_workspace(read_workspace_state())
    ensure_directories()
    ensure_worker()
    server = ThreadingHTTPServer((args.host, args.port), WikiHandler)
    print(f"Serving LLM Wiki at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
