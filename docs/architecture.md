# Architecture and contracts

The active search generation contains a SQLite message-location catalog, Tantivy session documents, provenance mapping, and catalog fingerprint. A complete staging generation is published by one pointer switch. Indexing failure leaves the previous pair available and records source-specific pending/failed updates.

An unchanged complete transcript skips parsing and document updates. An append validates the prior prefix, parses only new complete JSONL records, and replaces only the changed session document. Earlier-byte edits or truncation reparse that session. Partial tails are deferred. Duplicate IDs with different appended content fail closed.

Readers use immutable read-only catalogs. Writers retain a previous coherent generation. Staging generation cloning and prefix checks are deliberately retained costs; the implementation is not constant-time ingestion or fully incremental disk copying.

Capture hooks enqueue allowlisted metadata; a detached single worker indexes public text. It does not copy full hook payloads or hidden reasoning. A checkpoint is a request for original-evidence review, not an LLM-produced policy. Active notebook changes remain a separate agent task.

Native schemas are adapters, not stable provider APIs. The adapters currently recognize Codex public phases and human-content kinds, and Claude human origin with sidechain exclusion. Unsupported formats may produce no eligible history until their provenance is verified. Do not silently substitute inferred history for missing data.

The local evidence and password outlive runtime caches. Restic owns backup format, encryption, and repository verification. Adapters verify message identity/content plus restored file manifests, merge verified immutable assets, and refuse conflicting content.
