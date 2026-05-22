"""Retrieval-Augmented Generation (RAG) + Intent Fence + RBAC layer.

Public surface:

* `IdentityClaims` — the single source of truth for who the caller is
  and what data they may see. Built once at the API boundary and threaded
  through every downstream component (retriever, intent classifier,
  guardrail, prompt builder).
* `IntentClassifier` — maps a free-text question to one of a small set of
  intents using deterministic rules. Used by the guardrail to decide
  whether the request is allowed for the caller's role.
* `Guardrail` — `(role, intent)` policy matrix. Returns an allow/block
  decision and an optional rewrite/refusal message.
* `Retriever` — metadata-filtered vector search over `document_chunks`.
  Every query is pre-filtered by tenant_id and the caller's allowed
  visibility scopes; vector similarity is computed only over the surviving
  rows. This is the "Metadata Filter" in the architecture diagram.
* `KnowledgeGraph` — read-only graph over existing SQL FKs. Used to
  surface related entities (e.g. classmates of a struggling student,
  topics linked to a subject) without inventing a separate graph DB.
* `SessionManager` — loads the last-N turns from `messages` so the LLM
  has conversational memory without us re-querying every node.
* `chunker` — utility that splits raw text into overlapping chunks ready
  for embedding.
* `embeddings` — async embedding service wrapping Ollama.
"""
