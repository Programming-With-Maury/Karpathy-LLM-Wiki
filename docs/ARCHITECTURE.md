# Architecture

The Domain-Aware LLM Wiki separates immutable source material from curated synthesis.

## Layers

- `raw/`: immutable uploaded source material.
- `wiki/domains/<domain>/`: active domain-specific synthesis.
- `wiki/global/`: cross-domain entities and concepts.
- `wiki/staging/`: sources that should not enter the active wiki yet.
- `wiki/revisions/`: inspectable records of ingest and maintenance operations.

## Ingest

1. Read a raw source.
2. Score extraction quality.
3. Classify the source into a domain.
4. Stage low-confidence or low-quality sources.
5. Write source, entity, concept, overview, and revision pages for promotable sources.

## Query

1. Read the index.
2. Select domain-local pages first.
3. Add global pages only when useful.
4. Save durable answers as query artifacts.

## Maintenance

Lint and review passes look for:

- broken links
- orphan pages
- old flat namespace pages
- low-value generated pages
- duplicated concepts/entities across domains

