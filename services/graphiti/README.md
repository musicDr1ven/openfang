# Graphiti Knowledge Service

Temporal knowledge graph engine for the OpenFang Agent OS.

Sits between document ingestion and agent recall to provide:

- **Bi-temporal fact tracking** — every relation has `valid_at`/`expired_at`. Ask "what was true about momentum strategies in Q3 2022?" and get historically accurate answers.
- **Hybrid search** — vector similarity + BM25 keyword + graph traversal, fused via RRF.
- **Automatic entity extraction and deduplication** — LLM extracts entities/relations from raw text; "RSI" and "Relative Strength Index" become one node automatically.
- **Isolated agent namespaces** — `group_id` keeps the trading agent's knowledge completely separate from the nursing agent's knowledge.

**Stack:** [Graphiti](https://github.com/getzep/graphiti) + [FalkorDB](https://www.falkordb.com/) + FastAPI

---

## Prerequisites

- Docker + Docker Compose
- An API key for at least one LLM provider (Anthropic recommended)
- An API key for an embeddings provider (OpenAI recommended; Anthropic does not offer embeddings)

---

## Quick Start

### 1. Set environment variables

The compose file reads from your shell environment. Export the keys you'll use before starting:

```bash
# Required for entity extraction (choose one)
export ANTHROPIC_API_KEY=sk-ant-...       # recommended — Haiku is fast and cheap
# or
export OPENAI_API_KEY=sk-...              # alternative

# Required for embeddings (OpenAI is the easiest option)
export OPENAI_API_KEY=sk-...

# Optional — only needed if using OpenRouter for either LLM or embeddings
export OPENROUTER_API_KEY=sk-or-...
```

You can also put these in a `.env` file next to the `docker-compose.yaml`. Docker Compose picks it up automatically:

```bash
# services/graphiti/.env
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```

### 2. Start the services

```bash
cd services/graphiti
docker compose up -d
```

This starts two containers:
- `falkordb` — Redis with the GraphBLAS graph module, listening on port `6379`
- `graphiti` — FastAPI service, listening on port `8891`

### 3. Verify everything is running

```bash
# Check container health
docker compose ps

# Hit the health endpoint — should return {"status": "ok", "graphiti_ready": true}
curl http://localhost:8891/health
```

If `graphiti_ready` is `false`, check the service logs:

```bash
docker compose logs graphiti
```

Common causes:
- FalkorDB hasn't finished starting (wait a few more seconds and retry)
- Missing or invalid API key — check that `ANTHROPIC_API_KEY` (or your chosen provider key) is set in the environment

---

## Configure OpenFang to use Graphiti

Edit `~/.openfang/config.toml` (or wherever your `openfang.toml` lives):

```toml
[graph]
backend = "graphiti"
graphiti_url = "http://localhost:8891"
falkordb_url = "redis://localhost:6379"   # only used by the Graphiti service itself

[graphiti_llm]
provider = "anthropic"
model = "claude-haiku-4-5-20251001"       # Haiku: ~$0.01/episode, high throughput
# model = "claude-sonnet-4-6"            # Sonnet: higher extraction quality, more cost

[graphiti_embedder]
provider = "openai"
model = "text-embedding-3-small"
```

Then restart OpenFang:

```bash
openfang restart
# or kill and restart the daemon
```

> **Backward compatibility:** leaving `backend = "sqlite"` (or omitting the `[graph]` section entirely) keeps all existing behavior unchanged. The SQLite knowledge store continues to work as before.

---

## Provider alternatives

### Using Ollama for the LLM (no API key required)

Pull a model first:

```bash
ollama pull llama3.2
```

Then configure:

```toml
[graphiti_llm]
provider = "ollama"
model = "llama3.2"
base_url = "http://localhost:11434/v1"   # default Ollama URL

[graphiti_embedder]
provider = "ollama"
model = "nomic-embed-text"
base_url = "http://localhost:11434/v1"
```

Pull the embedding model:

```bash
ollama pull nomic-embed-text
```

> Note: you'll need to add `network_mode: host` to the `graphiti` service in `docker-compose.yaml` (or use `host.docker.internal`) so the container can reach your local Ollama instance.

### Using OpenRouter

```toml
[graphiti_llm]
provider = "openrouter"
model = "anthropic/claude-haiku-4-5-20251001"
base_url = "https://openrouter.ai/api/v1"

[graphiti_embedder]
provider = "openrouter"
model = "openai/text-embedding-3-small"
```

Set `OPENROUTER_API_KEY` in your environment.

---

## Ingest your first document

Once OpenFang is running with the Graphiti backend, agents with the `knowledge_ingest_document` tool can load documents directly:

```
# From within an agent conversation:
> Use knowledge_ingest_document to load /path/to/trading-strategy-book.pdf into the trading group
```

Or test the service directly with `curl`:

```bash
# Ingest a plain text snippet
curl -X POST http://localhost:8891/episodes \
  -H "Content-Type: application/json" \
  -d '{
    "content": "RSI (Relative Strength Index) is a momentum indicator that measures the speed and magnitude of price changes. Values above 70 indicate overbought conditions; values below 30 indicate oversold conditions.",
    "source_description": "Technical Analysis Fundamentals / Chapter 4",
    "group_id": "trading"
  }'

# Expected response:
# {"message":"Episode ingested into group 'trading'","episode_uuid":"..."}
```

```bash
# Search the graph
curl -X POST http://localhost:8891/search \
  -H "Content-Type: application/json" \
  -d '{"query": "momentum indicators overbought", "group_id": "trading", "limit": 5}'
```

Verify the entities are in FalkorDB:

```bash
# Connect to FalkorDB via redis-cli
docker exec -it graphiti-falkordb-1 redis-cli

# List all graph keys
127.0.0.1:6379> KEYS *

# Run a Cypher query on the trading graph
127.0.0.1:6379> GRAPH.QUERY trading "MATCH (n) RETURN n.name LIMIT 10"
```

---

## Load a PDF document

The service uses [PyMuPDF](https://pymupdf.readthedocs.io/) for local PDF extraction. For structured hierarchical extraction (chapter → section → subsection), the optional PageIndex service can be configured.

### PDF via the REST API

```bash
curl -X POST http://localhost:8891/ingest/document \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "/data/books/technical-analysis-of-financial-markets.pdf",
    "group_id": "trading"
  }'
```

The file path must be accessible inside the container. Mount a volume in `docker-compose.yaml` if needed:

```yaml
services:
  graphiti:
    volumes:
      - /path/to/your/books:/data/books:ro
```

### PDF via an agent tool call

```
> Use knowledge_ingest_document to load /home/user/books/murphy-technical-analysis.pdf as agent_type trading
```

---

## Optional: Enable PageIndex for better PDF structure

PageIndex extracts a hierarchical section tree (Chapter → Section → Subsection) from PDFs before ingestion, which dramatically improves entity extraction quality for structured books and reports.

Add to `openfang.toml`:

```toml
[pageindex]
enabled = true
url = "http://localhost:8890"
max_tokens_per_node = 20000
max_pages_per_node = 10
```

When `enabled = true` and `graphiti_url` is reachable, the `knowledge_ingest_document` tool will route PDF files through PageIndex before ingestion. Plain text files bypass PageIndex regardless.

PageIndex is a separate service not included in this compose file — refer to its own setup instructions.

---

## Agent configuration

### Built-in agents

Two domain agents are included:

| Agent | Group ID | Entity types |
|-------|----------|-------------|
| `agents/trading/agent.toml` | `trading` | TradingStrategy, MarketEvent, Asset, Indicator |
| `agents/infusion-nurse/agent.toml` | `nursing` | Medication, Procedure, NursingCertification, Protocol |

Load them with:

```bash
openfang agent load agents/trading/agent.toml
openfang agent load agents/infusion-nurse/agent.toml
```

### Custom agents

To give any agent access to a specific knowledge namespace, add a `[metadata]` section to its `agent.toml`:

```toml
[metadata]
knowledge_group_id = "my-domain"

[capabilities]
tools = ["knowledge_search", "knowledge_ingest_document", "knowledge_query"]
```

The `knowledge_group_id` value becomes the `group_id` used for all search and ingestion calls made by that agent. Different agents can share a group (e.g., multiple trading agents all reading from `"trading"`) or have isolated namespaces.

---

## Temporal queries

Graphiti stores bi-temporal metadata on every edge. To query what was known at a specific point in time, use `knowledge_search` with a date-scoped query from within an agent:

```
> What did the knowledge graph know about momentum strategies before 2023?
```

The agent translates this into a Graphiti search with temporal filtering. Conflicting facts ingested at different times are automatically managed — only facts valid at the queried point in time are returned.

---

## Stopping and persistence

```bash
# Stop services (data is preserved in the falkordb_data volume)
docker compose down

# Stop and delete all data (irreversible)
docker compose down -v
```

Graph data is stored in the `falkordb_data` Docker volume. Back it up with:

```bash
docker run --rm \
  -v graphiti_falkordb_data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/falkordb-backup-$(date +%Y%m%d).tar.gz /data
```

---

## Troubleshooting

**`graphiti_ready: false` in `/health`**

Check logs: `docker compose logs graphiti --tail 50`

Usually means:
1. FalkorDB is still starting — wait 10s and retry
2. `ANTHROPIC_API_KEY` (or your LLM provider key) is missing — Graphiti calls the LLM on startup to validate the connection
3. `OPENAI_API_KEY` is missing — embedder initialization fails silently

**`Episode ingestion failed: Connection refused`**

The Graphiti service can't reach FalkorDB. Check `docker compose ps` — FalkorDB should show `healthy`. If it's unhealthy, check `docker compose logs falkordb`.

**Slow ingestion**

Each episode makes 1–3 LLM calls for extraction and deduplication. With Haiku at ~$0.01/episode and 100ms latency, ingesting a 300-page book (≈300 sections) takes a few minutes and costs around $3. Use Sonnet only for high-value documents that need better extraction quality.

**`knowledge_ingest_document` returns "Document ingestion requires graphiti backend"**

You haven't switched `[graph] backend` to `"graphiti"` in `openfang.toml`, or OpenFang hasn't been restarted after the config change.
