# OpenFang Platform Analysis

*Analysis date: 2026-03-18*

---

## Memory System

OpenFang uses a **unified `MemorySubstrate`** backed entirely by **SQLite** with 4 specialized stores.

### 1. Semantic Store (`crates/openfang-memory/src/semantic.rs`)
The primary RAG store. Each memory fragment has:
- `content` (text), `source` enum, `scope` string, `confidence` (0.0–1.0)
- `embedding` (BLOB of float32 array) for vector search
- Access tracking (`access_count`, `accessed_at`)

**Search is hybrid**: vector cosine similarity re-ranking over LIKE-matched candidates. Falls back to pure text search if no embedding driver is configured.

**Memory sources**: `Conversation`, `Document`, `Observation`, `Inference`, `UserProvided`, `System`

**Schema:**
```sql
CREATE TABLE memories (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  content TEXT NOT NULL,
  source TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'episodic',
  confidence REAL NOT NULL DEFAULT 1.0,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  accessed_at TEXT NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  embedding BLOB DEFAULT NULL
);
```

### 2. Knowledge Graph Store (`crates/openfang-memory/src/knowledge.rs`)
Entity-relation graph stored in two SQLite tables:
- **entities** — typed nodes: `Person`, `Organization`, `Project`, `Concept`, `Event`, `Location`, `Document`, `Tool`, `Custom`
- **relations** — directed edges: `WorksAt`, `KnowsAbout`, `DependsOn`, `OwnedBy`, `CreatedBy`, `PartOf`, `Uses`, `Produces`, `Custom`

Queried via triple patterns `(source, relation_type, target)` with depth limiting. Not a dedicated graph DB — pure SQL JOINs.

**Query API:**
```rust
pub struct GraphPattern {
    pub source: Option<String>,
    pub relation: Option<RelationType>,
    pub target: Option<String>,
    pub max_depth: u32,
}
```

### 3. Structured Store (`crates/openfang-memory/src/structured.rs`)
Simple versioned KV store (`agent_id + key → BLOB value`). Used for agent state, preferences, and schedules.

**Schema:**
```sql
CREATE TABLE kv_store (
  agent_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value BLOB NOT NULL,
  version INTEGER DEFAULT 1,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (agent_id, key)
);
```

### 4. Session Store (`crates/openfang-memory/src/session.rs`)
MessagePack-encoded conversation history per session. Also has **canonical sessions** — a single persistent session per agent across all channels (vs. per-channel sessions).

**Schema:**
```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  messages BLOB NOT NULL,
  context_window_tokens INTEGER DEFAULT 0,
  label TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### Consolidation Engine (`crates/openfang-memory/src/consolidation.rs`)
Runs periodically to decay confidence of unaccessed memories after 7 days:
- Formula: `confidence *= (1.0 - decay_rate)`
- Minimum confidence floor: 0.1 (prevents complete erasure)
- Tracks metrics: memories_merged, memories_decayed, duration_ms

### Unified Substrate (`crates/openfang-memory/src/substrate.rs`)
All four stores are composed behind a single async interface:

```rust
pub struct MemorySubstrate {
    conn: Arc<Mutex<Connection>>,
    structured: StructuredStore,
    semantic: SemanticStore,
    knowledge: KnowledgeStore,
    sessions: SessionStore,
    consolidation: ConsolidationEngine,
    usage: UsageStore,
}
```

---

## Agent System

### Agent Types
32+ pre-built agents in `agents/*/`, each with an `agent.toml` manifest. Key agents:
- `assistant` — general-purpose default
- `researcher` — deep research with CRAAP evaluation and APA formatting
- `coder` — code generation and debugging
- `analyst` — data analysis
- `health-tracker`, `meeting-assistant`, and 27+ more

### Agent Manifest Structure (`agents/*/agent.toml`)
```toml
[model]
provider = "default"
model = "default"
max_tokens = 8192
temperature = 0.5
system_prompt = """..."""

[[fallback_models]]
provider = "default"
model = "gemini-2.0-flash"

[resources]
max_llm_tokens_per_hour = 300000
max_concurrent_tools = 10

[capabilities]
tools = ["file_read", "file_write", "memory_store", "memory_recall", ...]
memory_read = ["*"]
memory_write = ["self.*", "shared.*"]
network = ["*"]
shell = ["python *", "cargo *"]
```

Agent manifests are **Ed25519 signed** for capability verification and tamper detection.

---

## How Agents Use Memory

The **agent loop** (`crates/openfang-runtime/src/agent_loop.rs`) follows this pattern every turn:

1. Load session (conversation history)
2. **Recall**: embed the query (if embedding driver available), retrieve top-K semantic memories by cosine similarity → inject into system prompt
3. Call LLM with context-enriched prompt + tools
4. Execute tools (up to 50 iterations)
5. **Store**: save tool observations and conversation turns as new memory fragments with optional embeddings

Memory recall code pattern:
```rust
if let Some(embedding_vec) = embedding_vec {
    memory.recall_with_embedding_async(query, limit, Some(filter), Some(&embedding_vec))
} else {
    memory.recall(query, limit, Some(filter))
}
```

Memory storage after tool execution:
```rust
memory.remember_with_embedding_async(
    agent_id,
    &observation_text,
    MemorySource::Observation,
    "episodic",
    metadata,
    embedding.as_deref()
).await?
```

---

## Knowledge Graph in RAG / Document Management

The KG is **not the primary RAG mechanism** — the semantic store is. The KG is a structured fact layer on top.

### Document Ingestion Flow
1. `web_fetch` tool downloads URL
2. HTML stripped to Markdown (`crates/openfang-runtime/src/web_content.rs`) with `EXTCONTENT_{sha256}` boundary markers (prompt injection protection)
3. Stored as `MemorySource::Document` in the semantic store with optional vector embedding
4. Recalled via cosine similarity during future agent turns

### Knowledge Graph Population
- Agents (particularly `researcher`) call `add_entity()` / `add_relation()` tools to explicitly build the graph from document content
- Researcher agent applies CRAAP evaluation before adding facts
- Graph supports confidence scoring for uncertain facts
- Query results limited to 100 per query; implemented as SQL JOINs (not a native graph DB)

---

## Embedding / Vector Search (`crates/openfang-runtime/src/embedding.rs`)

`EmbeddingDriver` trait with `OpenAIEmbeddingDriver` implementation — works with any OpenAI-compatible endpoint (OpenAI, Ollama, Mistral, vLLM, LM Studio, etc.).

Dimension auto-detected by model name:
| Model | Dimensions |
|-------|-----------|
| `text-embedding-3-small` | 1536 |
| `text-embedding-3-large` | 3072 |
| `text-embedding-ada-002` | 1536 |
| `all-MiniLM-L6-v2` | 384 |
| `all-mpnet-base-v2` | 768 |
| `nomic-embed-text` | 768 |

If no embedding driver is configured, all recall degrades gracefully to SQLite LIKE text search.

---

## Database Schema Overview

**Current version: 8** (`crates/openfang-memory/src/migration.rs`)

| Table | Purpose |
|-------|---------|
| `agents` | Agent registry |
| `sessions` | Conversation history |
| `kv_store` | Structured key-value store |
| `memories` | Semantic memory store with embeddings |
| `entities` | Knowledge graph nodes |
| `relations` | Knowledge graph edges |
| `task_queue` | Async task queue with agent collaboration |
| `usage_events` | Cost tracking and metering |
| `canonical_sessions` | Cross-channel persistent sessions |
| `paired_devices` | Device pairing persistence |
| `audit_entries` | Merkle hash-chain audit trail |
| `events` | System event log |

---

## Key Files Index

| Component | File Path | Purpose |
|-----------|-----------|---------|
| Unified memory interface | `crates/openfang-memory/src/substrate.rs` | MemorySubstrate implementation |
| Semantic/vector store | `crates/openfang-memory/src/semantic.rs` | Vector-searchable memories |
| Knowledge graph | `crates/openfang-memory/src/knowledge.rs` | Entity-relation store |
| Session store | `crates/openfang-memory/src/session.rs` | Conversation history |
| Structured store | `crates/openfang-memory/src/structured.rs` | KV store |
| Consolidation engine | `crates/openfang-memory/src/consolidation.rs` | Memory decay |
| Schema/migrations | `crates/openfang-memory/src/migration.rs` | DB versioning |
| Memory types | `crates/openfang-types/src/memory.rs` | MemoryFragment, Entity, Relation, Memory trait |
| Agent loop | `crates/openfang-runtime/src/agent_loop.rs` | Core execution loop with memory recall |
| Embedding driver | `crates/openfang-runtime/src/embedding.rs` | Vector embedding providers |
| Document extraction | `crates/openfang-runtime/src/web_content.rs` | HTML→Markdown pipeline |
| Tool runner | `crates/openfang-runtime/src/tool_runner.rs` | Built-in tools incl. memory_store/recall |
| Kernel | `crates/openfang-kernel/src/kernel.rs` | Main orchestration |
| API routes | `crates/openfang-api/src/routes.rs` | REST endpoints (memory at lines 3162–3248) |
| Hands system | `crates/openfang-hands/src/lib.rs` | Autonomous capability packages |

---

## Design Considerations

### Strengths
- Graceful degradation: full vector RAG → text search fallback when no embedding driver
- Unified async `Memory` trait abstracts implementation from agents
- Confidence scoring + decay for relevance maintenance
- Audit trail with Merkle hash-chain for tamper detection
- 27 LLM providers with intelligent routing and fallback
- 32+ pre-built specialized agents

### Limitations
- **All storage is SQLite** — no Postgres, Redis, or native graph DB. Limits graph traversal expressiveness and vector search performance at scale.
- **Knowledge graph is additive** — agents build it incrementally via tools; no automated entity extraction pipeline.
- **Embeddings are optional** — RAG quality varies significantly depending on whether an embedding driver is configured.
- **No chunking pipeline** — documents stored as whole memories (after HTML stripping), not chunked. Limits recall precision for long documents.
- **Graph queries are SQL JOINs** — not a native graph traversal engine; complex multi-hop queries may be slow or unexpressive.
