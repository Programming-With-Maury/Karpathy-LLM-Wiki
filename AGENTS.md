# LLM Wiki Operating Schema

This file defines how an LLM agent should operate on this repository.

## Mission

Maintain the central company brain for a solo-founder and their AI coding/marketing agents.

- `raw/` is read-only source material (voice memos, meeting transcripts, API docs).
- `wiki/` is the maintained knowledge layer (the "Company Intranet").
- `wiki/index.md` is the content catalog.
- `wiki/log.md` is the append-only activity log.

The agent should optimize for durable synthesis, tracking engineering constraints, and business roadmap action items, not just one-off answers.

## Directory rules

### Raw sources

- `raw/sources/` stores immutable source documents.
- `raw/assets/` stores downloaded local images or attachments referenced by sources.
- Never modify source content in `raw/` unless the user explicitly asks for file hygiene work unrelated to the knowledge layer.

### Wiki content

- `wiki/overview.md` is the high-level map of active domains, not a catch-all synthesis page.
- `wiki/domains/<domain>/overview.md` is the high-level synthesis for one project/topic domain.
- `wiki/domains/<domain>/sources/` contains one summary page per source assigned to that domain.
- `wiki/domains/<domain>/entities/` contains people, organizations, products, places, works, etc. that are specific to that domain.
- `wiki/domains/<domain>/concepts/` contains topic, thesis, method, and theme pages that are specific to that domain.
- `wiki/domains/<domain>/queries/` contains reusable outputs produced in response to questions about that domain.
- `wiki/global/entities/` and `wiki/global/concepts/` contain only pages reused across two or more domains.
- Legacy flat folders (`wiki/sources/`, `wiki/entities/`, `wiki/concepts/`) are migration-only and should not receive new active pages.
- `wiki/archive/` contains pages that are merged, demoted, or kept only for traceability.
- `wiki/staging/` contains sources or drafts pending human review before ingestion.
- `wiki/staging/domain-review/` contains sources whose domain classification is uncertain.

## General writing rules

- Use markdown.
- All new wiki pages MUST contain YAML frontmatter at the top (e.g., tags, date_created) matching the files in `wiki/templates/`.
- Prefer concise, high-signal pages over long raw notes.
- Cross-link aggressively using relative markdown links.
- Preserve uncertainty explicitly.
- Distinguish facts, interpretations, open questions, and contradictions.
- Update existing pages when possible instead of creating duplicates.
- When a new page is created, ensure it is linked from at least one other page and listed in `wiki/index.md`.
- Keep the active wiki smaller than the total archive. If a page is weakly sourced, duplicated, merged, or no longer worth surfacing, move it to `wiki/archive/`.

## Page conventions

Every substantive wiki page should try to include:

- YAML frontmatter at the top.
- A short summary paragraph at the top.
- `## Key Points`
- `## Evidence / Notes`
- `## Links`
- `## Open Questions` when unresolved issues exist.

For `wiki/overview.md` and major concept pages, maintain a visual `mermaid.js` graph summarizing core relationships.

All active source/entity/concept/query pages should include YAML frontmatter with:

- `domain`
- `domain_confidence` when produced by source ingest
- `domain_reason` when produced by source ingest
- `shared_scope: domain|global`
- `source_paths`
- `status: active|staged|archived`

Source summary pages in `wiki/domains/<domain>/sources/` should also include:

- Source path in the repo.
- Date ingested.
- Main claims or takeaways.
- Important entities and concepts touched by the source.
- A list of wiki pages updated because of that source.

## Operations

### Staging (Human in the Loop)

When the user asks to stage a large document or book:

1. Read the raw source in `wiki/staging/`.
2. Generate an implementation plan detailing the proposed changes to the wiki.
3. Await user approval.
4. Once approved, execute the ingest workflow and move the source to `raw/sources/`.

### Ingest

When the user asks to ingest a source:

1. Read the raw source from `raw/sources/` and any local assets it references.
2. Classify the source into an existing domain or a clearly justified new domain.
3. If domain confidence is low or the source spans unclear domains, write an intake page to `wiki/staging/domain-review/` and stop active ingest.
4. Identify the most important claims, entities, concepts, engineering constraints, and roadmap action items within the selected domain.
5. Create or update the source summary page in `wiki/domains/<domain>/sources/`.
6. Update relevant pages in `wiki/domains/<domain>/entities/`, `wiki/domains/<domain>/concepts/`, and `wiki/domains/<domain>/overview.md`.
7. Promote a page to `wiki/global/` only when it is genuinely reused across two or more domains.
8. Update `wiki/index.md`.
9. Record the touched pages as a revision artifact, including domain metadata.
10. Append an entry to `wiki/log.md`.
11. Run `./scripts/commit_wiki.sh` to auto-version the changes.

Expected result: one source can legitimately touch many wiki pages.
When a relevant page already exists, prefer reconciling and rewriting it coherently rather than merely appending a note.

### Query

When the user asks a question:

1. Read `wiki/index.md` first.
2. Identify the most relevant domain or domains.
3. Prefer domain-local pages, then include global pages only when linked or clearly reusable.
4. Synthesize the answer from the wiki, citing page paths inline.
5. If the answer creates durable value, save it under `wiki/domains/<domain>/queries/` for domain-specific questions or `wiki/queries/` for cross-domain questions.
6. Update `wiki/index.md` and append a `query` entry to `wiki/log.md` if a durable artifact was created.

Expected result: good questions should usually leave behind a reusable query artifact.

### Lint

When the user asks for a cleanup, health check, or lint pass:

1. Run `python3 scripts/lint.py` to identify broken links and orphaned files.
2. Check for legacy flat pages that should move under `wiki/domains/`.
3. Check for duplicated concepts or entities across domains.
4. Check for contradictions across pages.
5. Check for stale claims that newer sources appear to supersede.
6. Check for important terms mentioned repeatedly without dedicated pages.
7. Recommend global promotion only when a concept/entity is reused across two or more domains.
8. Propose or apply fixes.
9. Append a `lint` entry to `wiki/log.md`.
10. Run `./scripts/commit_wiki.sh` to auto-version the changes.

Expected result: the lint pass should leave behind an inspectable report, not just a transient chat answer.
The lint pass should also identify pages that belong in `wiki/archive/` because they are weakly sourced, duplicated, stale, or artifact-level noise.

## Index format

`wiki/index.md` is the primary navigation surface. Keep it compact and skimmable.

- Organize by section: overview, domains, global concepts, global entities, recent queries, staging.
- Exclude `wiki/archive/` from the main index.
- Exclude legacy flat folders from the main index once migration is complete.
- Each entry should include:
  - page link
  - one-line description
  - optional metadata such as updated date, source count, or confidence

## Log format

Append entries using this shape:

```md
## [YYYY-MM-DD] operation | title

- Summary of what changed
- Pages touched: [page](relative/path.md), [page](relative/path.md)
```

Keep the log append-only.

## Revisions

- Each ingest should produce a revision artifact that records:
  - the raw source processed
  - the source page generated
  - selected domain and domain confidence
  - whether a new domain was created
  - every wiki page touched by the ingest
  - global pages touched
  - staged or archived pages
  - the key extracted entities and concepts
- Revisions make each ingest inspectable as a single coherent change set.

## Naming

- Use lowercase kebab-case file names.
- Prefer explicit names over clever names.
- If a concept or entity name is ambiguous, disambiguate in the file name.

## Quality bar

- Do not dump large excerpts from sources.
- Do not restate the same idea across many pages without adding page-specific value.
- Prefer editing an existing page if the knowledge belongs there.
- Keep links and indexes in sync.
- Leave the wiki in a more connected state after every operation.
- Prefer source-backed pages in the active wiki. Review artifacts and vague uploads can guide cleanup, but they should not dominate the canonical layer.
