# RAG Evaluation Notes

The research team compared generic vector search with a curated wiki layer. Vector search was fast, but answers drifted when multiple projects used the same terms. The domain-aware wiki improved precision by routing sources into project-specific contexts before retrieval.

Evaluation ideas:

- Track source-grounded answer rate.
- Track wrong-domain retrieval rate.
- Track how often query artifacts are reused.

