# Launch Checklist

Use this before publishing or announcing the project.

## Privacy

- Confirm `.env`, `notion-sync.config.json`, runtime state files, and `.batches/` are not tracked.
- Confirm `raw/sources/**`, `raw/assets/**`, `workspaces/**`, `wiki/revisions/**`, `wiki/archive/**`, and generated root wiki content are not tracked.
- Search for real names, emails, phone numbers, API keys, invoices, resumes, certificates, and private client/project material.
- Publish only sanitized demo material under `examples/demo-workspace/`.

## Product Demo

- Run `python3 scripts/init_demo_workspace.py`.
- Start the app with `python3 app.py`.
- Select the `demo` workspace.
- Confirm the demo shows multiple domains, one global concept, a query artifact, and a staged domain-review source.

## Health Checks

- Run `python3 -m py_compile app.py scripts/*.py`.
- Run `python3 scripts/lint.py` against any workspace you plan to showcase.
- Check the browser home page and index page locally.

## Positioning

- Describe the project as a domain-aware memory layer for AI agents.
- Be explicit that this is an alpha.
- Avoid promising perfect automated knowledge management; emphasize staging, revision manifests, and human review.

