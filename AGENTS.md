# LLM Wiki Operating Schema

This file defines how an LLM agent should operate on this repository.

## Mission

Maintain a persistent markdown wiki that accumulates understanding across sources over time.

- `raw/` is read-only source material.
- `wiki/` is the maintained knowledge layer.
- `wiki/index.md` is the content catalog.
- `wiki/log.md` is the append-only activity log.

The agent should optimize for durable synthesis, not one-off answers.

## Directory rules

### Raw sources

- `raw/sources/` stores immutable source documents.
- `raw/assets/` stores downloaded local images or attachments referenced by sources.
- Never modify source content in `raw/` unless the user explicitly asks for file hygiene work unrelated to the knowledge layer.

### Wiki content

- `wiki/overview.md` is the high-level map of the topic or domain.
- `wiki/sources/` contains one summary page per source.
- `wiki/entities/` contains pages for people, organizations, products, places, works, etc.
- `wiki/concepts/` contains topic, thesis, method, and theme pages.
- `wiki/queries/` contains reusable outputs produced in response to questions.
- `wiki/archive/` contains pages that are merged, demoted, or kept only for traceability.

## General writing rules

- Use markdown.
- Prefer concise, high-signal pages over long raw notes.
- Cross-link aggressively using relative markdown links.
- Preserve uncertainty explicitly.
- Distinguish facts, interpretations, open questions, and contradictions.
- Update existing pages when possible instead of creating duplicates.
- When a new page is created, ensure it is linked from at least one other page and listed in `wiki/index.md`.
- Keep the active wiki smaller than the total archive. If a page is weakly sourced, duplicated, merged, or no longer worth surfacing, move it to `wiki/archive/`.

## Page conventions

Every substantive wiki page should try to include:

- A short summary paragraph at the top.
- `## Key Points`
- `## Evidence / Notes`
- `## Links`
- `## Open Questions` when unresolved issues exist.

Source summary pages in `wiki/sources/` should also include:

- Source path in the repo.
- Date ingested.
- Main claims or takeaways.
- Important entities and concepts touched by the source.
- A list of wiki pages updated because of that source.

## Operations

### Ingest

When the user asks to ingest a source:

1. Read the raw source from `raw/sources/` and any local assets it references.
2. Identify the most important claims, entities, concepts, and contradictions.
3. Create or update the source summary page in `wiki/sources/`.
4. Update any relevant pages in `wiki/entities/`, `wiki/concepts/`, and `wiki/overview.md`.
5. Update `wiki/index.md`.
6. Record the touched pages as a revision artifact.
7. Append an entry to `wiki/log.md`.

Expected result: one source can legitimately touch many wiki pages.
When a relevant page already exists, prefer reconciling and rewriting it coherently rather than merely appending a note.

### Query

When the user asks a question:

1. Read `wiki/index.md` first.
2. Identify the most relevant wiki pages.
3. Synthesize the answer from the wiki, citing page paths inline.
4. If the answer creates durable value, save it as a new page in `wiki/queries/`.
5. Update `wiki/index.md` and append a `query` entry to `wiki/log.md` if a durable artifact was created.

Expected result: good questions should usually leave behind a reusable query artifact.

### Lint

When the user asks for a cleanup, health check, or lint pass:

1. Check for orphan pages.
2. Check for duplicated concepts or entities.
3. Check for contradictions across pages.
4. Check for stale claims that newer sources appear to supersede.
5. Check for important terms mentioned repeatedly without dedicated pages.
6. Propose or apply fixes.
7. Append a `lint` entry to `wiki/log.md`.

Expected result: the lint pass should leave behind an inspectable report, not just a transient chat answer.
The lint pass should also identify pages that belong in `wiki/archive/` because they are weakly sourced, duplicated, stale, or artifact-level noise.

## Index format

`wiki/index.md` is the primary navigation surface. Keep it compact and skimmable.

- Organize by section: overview, sources, entities, concepts, queries.
- Exclude `wiki/archive/` from the main index.
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
  - every wiki page touched by the ingest
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
