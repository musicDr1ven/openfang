# OpenFang Hybrid Knowledge System Upgrade

## Context
OpenFang's current knowledge graph is a minimal SQLite-backed system (hardcoded SQL JOINs, 100-result limit, no native graph traversal, no chunking pipeline). The goal is to upgrade it to a production-quality hybrid knowledge system supporting two high-value use cases:
1. **Stock/Crypto Trading Agent** — large financial book/document corpus; dynamic signals; needs real-time query performance
2. **Infusion Nursing Staffing Agent** — medical/nursing protocols, certifications, staffing relationships; slower-moving but complex domain knowledge

The user is evaluating **Memgraph**, **FalkorDB**, and **GLinker** as candidates.

---

## Critical Distinction: These Tools Serve Different Layers

**GLinker is NOT a graph database** — it's a knowledge extraction pipeline (NER + entity disambiguation). It doesn't compete with the other two; it feeds data into them.

| Tool | Layer | Role |
|------|-------|------|
| **GLinker** | Construction | Extract entities + relationships from raw text/PDFs |
| **FalkorDB** | Storage + Query | Store and query the knowledge graph |
| **Memgraph** | Storage + Query | Store and query the knowledge graph |

The correct architecture is: **GLinker (extract) → FalkorDB or Memgraph (store/query)**

---

## Library Recommendation

### Memgraph vs FalkorDB Decision

| Criterion | Memgraph | FalkorDB |
|-----------|----------|----------|
| **Real-time streaming** | ✅ Kafka/Redpanda native | Redis Streams (good, not purpose-built) |
| **Embedded prototype path** | Single-node Docker | ✅ FalkorDBLite (same API, zero migration) |
| **Rust client** | rsmgclient (good) | ✅ falkordb-rs (official, better maintained) |
| **Vector + graph hybrid** | ✅ Native (2.1+) | ✅ Native (4.0+) |
| **Schema flexibility ("loose")** | ✅ Schema-free | ✅ Schema-free |
| **Scaling model** | RAM-constrained single node | ✅ Redis cluster path |
| **Deployment simplicity** | Docker (simple) | ✅ Docker or embedded |
| **Medical/complex domain** | Good | ✅ Better (knowledge graph focus, GraphRAG SDK) |

### Recommendation: **FalkorDB for both use cases**

**Why FalkorDB wins:**
- **FalkorDBLite** → FalkorDB is the cleanest prototype-to-production path with identical API (zero code rewrites)
- **falkordb-rs** is the better Rust client for OpenFang's Rust codebase
- **GraphRAG SDK** is purpose-built for the LLM agent + knowledge graph use case
- Medical knowledge bases are not primarily real-time streaming problems
- The "keep it loose" ontology preference maps well to FalkorDB's schema-free properties
- FalkorDB's sparse matrix (GraphBLAS) representation handles large, sparse graphs efficiently — better for both use cases at scale

**When to reconsider Memgraph:**
- If the trading agent needs live Kafka stream ingestion (price feeds, order books)
- If the trading graph outgrows RAM on a single node before you're ready to manage Redis cluster

### GLinker Role
Use GLinker as a **Python microservice** for document ingestion:
- Ingest PDFs/EPUBs/text via GLinker (NER + entity linking)
- GLinker outputs structured entities + relationships → POST to OpenFang's knowledge tools
- Models: `gliner-bi-large-v2.0` for accuracy; `gliner-bi-edge-v2.0` for throughput
- Pre-compute BiEncoder embeddings once per knowledge base load (10–100x speedup)

---

## Proposed Hybrid Architecture

```
Books/PDFs/Web
      │
      ▼
 GLinker Service (Python)
  ├── NER: GLiNER zero-shot entity extraction
  ├── Disambiguation: neural ranking against entity base
  └── Output: {entities[], relations[], confidence[]}
      │
      ▼ HTTP POST /api/knowledge/ingest
 OpenFang Kernel
      │
      ├── knowledge_add_entity() ──────────────────► FalkorDB
      ├── knowledge_add_relation() ─────────────────► (Cypher + GraphBLAS)
      │                                                    │
      └── recall() ◄────────────────────────────── vector + graph hybrid query
            │
            ▼
       Agent System Prompt
     (graph context + semantic memories)
```

---

## OpenFang Integration Plan

### Phase 1: Abstract the Graph Backend (Rust)

**Problem:** `KnowledgeStore` is directly instantiated in `MemorySubstrate` with no trait — cannot swap backends.

**Files to modify:**
- `crates/openfang-memory/src/knowledge.rs` — rename to `sqlite_graph.rs`; wrap in `SqliteGraphStore`
- `crates/openfang-memory/src/substrate.rs:32` — change `knowledge: KnowledgeStore` → `knowledge: Arc<dyn GraphBackend>`
- `crates/openfang-memory/src/substrate.rs:51` — change direct instantiation to factory call
- `crates/openfang-types/src/memory.rs` — no changes (trait already has `add_entity`, `add_relation`, `query_graph`)
- `crates/openfang-kernel/src/kernel.rs` — add `graph_config: GraphConfig` to kernel init

**New file:** `crates/openfang-memory/src/graph_backend.rs`
```rust
#[async_trait]
pub trait GraphBackend: Send + Sync {
    async fn add_entity(&self, entity: Entity) -> OpenFangResult<String>;
    async fn add_relation(&self, relation: Relation) -> OpenFangResult<String>;
    async fn query_graph(&self, pattern: GraphPattern) -> OpenFangResult<Vec<GraphMatch>>;
}

pub fn create_graph_backend(config: &GraphConfig) -> Arc<dyn GraphBackend> {
    match config.backend.as_str() {
        "falkordb" => Arc::new(FalkorGraphStore::new(&config)),
        "sqlite" | _ => Arc::new(SqliteGraphStore::new(&config.sqlite_conn)),
    }
}
```
Follow the existing `EmbeddingDriver` factory pattern in `crates/openfang-runtime/src/embedding.rs:178-250`.

**New file:** `crates/openfang-memory/src/falkor_graph.rs`
- Uses `falkordb` crate (falkordb-rs)
- Translates `Entity`/`Relation` structs → Cypher `CREATE`/`MERGE` queries
- Translates `GraphPattern` → Cypher `MATCH` with optional depth param
- Returns `Vec<GraphMatch>` (same type, no changes upstream)

### Phase 2: Document Chunking + Ingestion Pipeline

**Problem:** Documents are stored as one giant memory fragment — bad for recall precision on long books.

**Files to modify:**
- `crates/openfang-runtime/src/web_content.rs` — add `chunk_markdown(text, max_tokens)` function
- `crates/openfang-runtime/src/agent_loop.rs` — store document chunks individually (loop over chunks when `MemorySource::Document`)
- New tool in `crates/openfang-runtime/src/tool_runner.rs` — `ingest_file` tool for local PDF/EPUB/TXT

**Chunking strategy:** Semantic chunking at paragraph/section boundaries, 512 tokens max, 10% overlap. Attach metadata: `{source_doc, chunk_index, total_chunks, section_heading}`.

### Phase 3: Document Intelligence Microservice (PageIndex + GLinker)

**Why PageIndex?** Raw PDFs/books fed directly to GLinker produce noisy, context-poor extractions. PageIndex solves this by first building a hierarchy-aware tree of the document (chapters → sections → subsections), extracting clean text per section, and attaching rich metadata (page numbers, section heading, token counts). GLinker then runs NER on each clean section — dramatically improving entity accuracy and giving every extracted entity a traceable source location.

**PageIndex does NOT overlap with GLinker:**
- PageIndex = document structure extraction + LLM-reasoning retrieval
- GLinker = named entity recognition + relation extraction

**Pipeline:**
```
PDF / EPUB / Book
    ↓
[PageIndex]  builds hierarchical tree, extracts clean text per section
    ↓
[GLinker]    NER + entity linking on each section's clean text
    ↓
Entities + Relations + metadata (section, page, confidence)
    ↓
knowledge_add_entity / knowledge_add_relation → FalkorDB
```

**New directory:** `services/knowledge-ingestion/`
- `server.py` — FastAPI service wrapping both PageIndex + GLinker in sequence
- `pageindex_stage.py` — wraps PageIndex tree extraction
- `glinker_stage.py` — wraps GLinker NER pipeline
- `docker-compose.yaml` — port 8890
- `requirements.txt` — `pageindex`, `glinker`, `fastapi`, `pymupdf`

**API contract:**
```
POST /ingest/file
Body: { "file_path": "/path/to/book.pdf", "entity_types": ["concept", "strategy", "indicator"] }
Response: { "sections_processed": 42, "entities": 318, "relations": 127, "tree": {...} }

POST /ingest/text
Body: { "text": "...", "source": "chapter title", "entity_types": [...] }
Response: { "entities": [...], "relations": [...] }
```

**New tool in tool_runner.rs:** `knowledge_ingest_document`
- Accepts local file path or raw text
- Calls ingestion service at `localhost:8890`
- Pipes output to `knowledge_add_entity` / `knowledge_add_relation`
- Stores document tree structure as graph nodes in FalkorDB (`Document → Section → Subsection`)
- Returns summary: "Processed N sections, extracted M entities, K relations"

**PageIndex advantage for retrieval:** The document tree is also stored as a `Document → hasSection → Section` subgraph in FalkorDB. During agent retrieval, graph queries can traverse `"find all sections mentioning X concept across all ingested books"` — something pure vector search can't do.

### Phase 4: Hybrid Recall (Graph + Vector)

**Problem:** Current recall is semantic-only (vector similarity). Graph context is never injected into agent prompts.

**Files to modify:**
- `crates/openfang-runtime/src/agent_loop.rs:163-235` — after semantic recall, add a graph recall step
- New function in agent_loop: `graph_context_for_query(query, kernel)` — extracts key entities from query (regex or simple NER), runs `query_graph()`, formats results
- Append graph context block to system prompt alongside memory section

**Recall flow (enhanced):**
1. Embed query → vector recall (top 5 semantic memories) — existing
2. Extract entities from query → graph recall (related nodes + edges) — new
3. Combine: semantic memories + graph context → system prompt injection

---

## Configuration

Add to `openfang.toml`:
```toml
[graph]
backend = "falkordb"          # or "sqlite" (default, backward compat)
url = "redis://localhost:6379"
graph_name = "openfang"

[knowledge_ingestion]
url = "http://localhost:8890"
enabled = false               # opt-in
default_entity_types = ["person", "organization", "concept", "event"]
# PageIndex settings
pageindex_model = "claude-sonnet-4-6"   # LLM used for tree extraction reasoning
max_tokens_per_node = 20000
max_pages_per_node = 10
```

---

## Use-Case Specific Agent Setup

### Trading Agent (`agents/trading/agent.toml`)
```toml
[capabilities]
tools = ["knowledge_add_entity", "knowledge_add_relation", "knowledge_query",
         "knowledge_ingest_text", "web_fetch", "memory_store", "memory_recall"]

[knowledge]
scopes = ["financial_theory", "market_patterns", "trading_signals"]
entity_types = ["asset", "strategy", "indicator", "event", "author", "concept"]
```

### Nursing Agent (`agents/infusion-nurse/agent.toml`)
```toml
[capabilities]
tools = ["knowledge_add_entity", "knowledge_add_relation", "knowledge_query",
         "knowledge_ingest_text", "memory_store", "memory_recall"]

[knowledge]
scopes = ["protocols", "certifications", "medications", "procedures"]
entity_types = ["medication", "procedure", "diagnosis", "certification", "nurse", "facility"]
```

---

## Implementation Order

1. **Phase 1** — `GraphBackend` trait abstraction + SQLite wrapper (no behavior change, just refactor)
2. **Phase 2** — FalkorDB backend implementation + config toggle
3. **Phase 3** — Document chunking in ingestion pipeline
4. **Phase 4** — PageIndex + GLinker microservice + `knowledge_ingest_document` tool
5. **Phase 5** — Hybrid recall (graph context injection into agent loop)

---

## Verification

- **Unit**: `SqliteGraphStore` passes existing knowledge tests; `FalkorGraphStore` passes same tests
- **Integration**: Start FalkorDB docker, run agent loop, verify graph entities appear in FalkorDB via CLI (`redis-cli` + FalkorDB commands)
- **Recall quality**: Ingest one trading book chapter → query for key concept → verify graph context appears in agent system prompt
- **GLinker**: `POST /extract` with sample nursing protocol text → verify extracted entities match expected
- **Backward compat**: Default config (`backend = "sqlite"`) passes all existing tests unchanged
