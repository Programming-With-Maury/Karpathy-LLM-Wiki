# Security And Privacy

This project is designed for local-first knowledge work. Treat raw uploads and generated wiki pages as private unless you intentionally sanitize and publish them.

## Do Not Commit

- `.env`
- `notion-sync.config.json`
- raw source uploads
- generated private wiki pages
- revision manifests from private sources
- chat/review/ingestion state files
- invoices, resumes, certificates, or exported conversations

The default `.gitignore` is intentionally conservative and keeps private runtime data out of git.

## Before Publishing

Run a manual review for:

- API keys and tokens
- email addresses and phone numbers
- personal names and resumes
- invoices and payment details
- private customer or company documents
- exported chat logs

Use `examples/demo-workspace/` for public demos.

