# LLM Wiki

This repository is a practical scaffold for the "LLM-maintained wiki" pattern described in Andrej Karpathy's gist:

<https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>

The model is simple:

- `raw/` contains immutable source material.
- `wiki/` contains LLM-maintained markdown pages.
- `AGENTS.md` defines the operating rules for ingesting, querying, and linting the wiki.
- `scripts/llm_wiki.py` helps with source setup and basic bookkeeping.
- `app.py` serves a local Wikipedia-style browser over the repository.

## Structure

```text
.
├── AGENTS.md
├── raw/
│   ├── assets/
│   └── sources/
├── scripts/
│   └── llm_wiki.py
└── wiki/
    ├── concepts/
    ├── entities/
    ├── queries/
    ├── sources/
    ├── index.md
    ├── log.md
    └── overview.md
```

## Quick start

1. Add a source stub:

   ```bash
   python3 scripts/llm_wiki.py init-source "Example Article"
   ```

2. Put the original article, notes, or transcript in `raw/sources/...`.

3. Ask your coding agent to ingest the new source using the rules in `AGENTS.md`.

4. Read the updated `wiki/index.md`, `wiki/log.md`, and generated pages.

## Run the interface

```bash
python3 app.py
```

Then open `http://127.0.0.1:8000`.

The interface shows:

- AI-managed wiki pages
- raw uploaded files
- recent activity from `wiki/log.md`
- per-ingest revisions showing every page touched by the AI
- search across both layers
- a browser upload form that writes into `raw/sources/` and auto-ingests text files into the wiki
- a query form that answers from the wiki and files results into `wiki/queries/`
- a lint action that writes a health report back into `wiki/queries/`
- repeated sources now reconcile existing entity, concept, and overview pages instead of only appending new references

## Typical workflow

- Ingest one source at a time.
- Let the agent update multiple wiki pages in one pass.
- Ask questions against the wiki, not the raw folder.
- File valuable answers back into `wiki/queries/`.
- Periodically run a lint pass to detect contradictions, stale claims, and orphan pages.

## Open-source safety

This project is designed to work on personal knowledge, so privacy hygiene matters before publishing.

- Keep `.env` private. It contains API tokens and must never be committed.
- Keep `notion-sync.config.json` private. It may include personal/private Notion IDs.
- Treat `raw/` and generated `wiki/` content as potentially sensitive unless manually reviewed.
- Run locally on `127.0.0.1` (default) unless you intentionally want network access.

### Pre-publish checklist

- Rotate any previously exposed credentials (Vertex, Notion, etc.).
- Confirm `.gitignore` excludes local secrets, runtime state, and private datasets.
- Review all files for personal names, emails, phone numbers, invoices, IDs, and internal docs.
- Remove or redact private uploads before `git add .`.

## License

This project is licensed under the MIT License. See `LICENSE`.
