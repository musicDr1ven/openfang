"""
Graphiti Service — FastAPI wrapper around the Graphiti temporal knowledge graph engine.

Exposes:
  POST /episodes           — ingest a raw text episode
  POST /ingest/document    — full PageIndex → Graphiti pipeline for a file
  POST /search             — hybrid search (vector + BM25 + graph)
  GET  /health             — liveness check

Port: 8891 (fixed — update [graph] graphiti_url in openfang.toml if changed)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Load .env / .env.local from the openfang project root (if present)
load_dotenv(".env.local")
load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graphiti initialization
# ---------------------------------------------------------------------------

graphiti_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Graphiti on startup, shut it down on exit."""
    global graphiti_client

    falkordb_url = os.environ.get("FALKORDB_URL", "redis://localhost:6379")
    falkordb_host, falkordb_port = _parse_redis_url(falkordb_url)

    try:
        from graphiti_core import Graphiti

        from llm_provider import create_embedder, create_llm_client

        llm = create_llm_client()
        embedder = create_embedder()

        graphiti_client = Graphiti(
            falkordb_host,
            falkordb_port,
            llm_client=llm,
            embedder=embedder,
        )

        # Build the graph schema (idempotent — safe to call on every startup)
        await graphiti_client.build_indices_and_constraints()
        logger.info(
            f"Graphiti initialized (FalkorDB at {falkordb_host}:{falkordb_port})"
        )
    except Exception as e:
        logger.error(f"Failed to initialize Graphiti: {e}")
        # Don't crash — return degraded responses with clear error messages

    yield

    if graphiti_client:
        await graphiti_client.close()
        logger.info("Graphiti shut down")


def _parse_redis_url(url: str) -> tuple[str, int]:
    """Parse 'redis://host:port' → (host, port)."""
    url = url.removeprefix("redis://").removeprefix("redis+tls://")
    if ":" in url:
        host, port_str = url.rsplit(":", 1)
        return host, int(port_str)
    return url, 6379


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenFang Graphiti Service",
    description="Temporal knowledge graph service for the OpenFang Agent OS",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class EpisodeRequest(BaseModel):
    content: str
    source_description: str = "unknown"
    group_id: str = "default"
    episode_type: str = "text"


class EpisodeResponse(BaseModel):
    message: str
    episode_uuid: Optional[str] = None


class IngestDocumentRequest(BaseModel):
    file_path: str
    group_id: str = "default"
    pageindex_url: Optional[str] = None


class IngestDocumentResponse(BaseModel):
    message: str
    sections_ingested: int
    entities_extracted: Optional[int] = None


class SearchRequest(BaseModel):
    query: str
    group_id: str = "default"
    limit: int = 10


class SearchResponse(BaseModel):
    edges: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Helper: require Graphiti to be initialized
# ---------------------------------------------------------------------------


def _require_graphiti():
    if graphiti_client is None:
        raise HTTPException(
            status_code=503,
            detail="Graphiti service is not initialized. Check FalkorDB connection and LLM provider configuration.",
        )
    return graphiti_client


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {
        "status": "ok" if graphiti_client is not None else "degraded",
        "graphiti_ready": graphiti_client is not None,
    }


@app.post("/episodes", response_model=EpisodeResponse)
async def ingest_episode(req: EpisodeRequest):
    """
    Ingest a raw text episode into the Graphiti temporal knowledge graph.

    Graphiti automatically:
    1. Extracts entities and relations via LLM (using the configured model)
    2. Deduplicates against existing graph nodes (3-tier: exact → fuzzy → LLM)
    3. Manages bi-temporal metadata (valid_at, created_at, expired_at)
    4. Stores to FalkorDB with GraphBLAS sparse matrix representation
    """
    g = _require_graphiti()

    from graphiti_core.nodes import EpisodeType

    ep_type = EpisodeType.text
    if req.episode_type == "json":
        ep_type = EpisodeType.json
    elif req.episode_type == "message":
        ep_type = EpisodeType.message

    try:
        result = await g.add_episode(
            name=req.source_description,
            episode_body=req.content,
            source=ep_type,
            source_description=req.source_description,
            group_id=req.group_id,
        )
        episode_uuid = getattr(result, "uuid", None) or getattr(result, "episode_uuid", None)
        return EpisodeResponse(
            message=f"Episode ingested into group '{req.group_id}'",
            episode_uuid=str(episode_uuid) if episode_uuid else None,
        )
    except Exception as e:
        logger.error(f"Episode ingestion failed: {e}")
        raise HTTPException(status_code=500, detail=f"Episode ingestion failed: {e}")


@app.post("/ingest/document", response_model=IngestDocumentResponse)
async def ingest_document(req: IngestDocumentRequest):
    """
    Full PageIndex → Graphiti pipeline for a document file (PDF or text).

    Steps:
    1. PageIndex extracts hierarchical section tree from the file
    2. Each section is converted to a Graphiti episode
    3. Episodes are ingested sequentially into FalkorDB
    """
    g = _require_graphiti()

    if not Path(req.file_path).exists():
        raise HTTPException(status_code=404, detail=f"File not found: {req.file_path}")

    from episode_builder import sections_to_episodes
    from pageindex_stage import extract_sections

    try:
        sections = await extract_sections(
            req.file_path,
            pageindex_url=req.pageindex_url,
        )
    except Exception as e:
        logger.error(f"Section extraction failed for '{req.file_path}': {e}")
        raise HTTPException(status_code=500, detail=f"Section extraction failed: {e}")

    document_title = Path(req.file_path).stem
    episodes = sections_to_episodes(sections, req.group_id, document_title)

    if not episodes:
        return IngestDocumentResponse(
            message="No content extracted from document",
            sections_ingested=0,
        )

    from graphiti_core.nodes import EpisodeType

    ingested = 0
    errors = []
    for ep in episodes:
        try:
            await g.add_episode(
                name=ep.source_description,
                episode_body=ep.content,
                source=EpisodeType.text,
                source_description=ep.source_description,
                group_id=ep.group_id,
            )
            ingested += 1
        except Exception as e:
            errors.append(str(e))
            logger.warning(f"Episode ingestion error (section '{ep.source_description}'): {e}")

    msg = f"Ingested {ingested}/{len(episodes)} sections from '{document_title}' into group '{req.group_id}'"
    if errors:
        msg += f". {len(errors)} section(s) failed."
    logger.info(msg)

    return IngestDocumentResponse(
        message=msg,
        sections_ingested=ingested,
    )


@app.post("/search", response_model=SearchResponse)
async def search_knowledge(req: SearchRequest):
    """
    Hybrid knowledge graph search.

    Combines:
    - Vector similarity (semantic search over embedded edge facts)
    - BM25 keyword matching
    - Graph traversal (neighborhood expansion)

    Results are fused via Reciprocal Rank Fusion (RRF) for maximum relevance.
    """
    g = _require_graphiti()

    try:
        results = await g.search(
            query=req.query,
            group_ids=[req.group_id],
            num_results=req.limit,
        )
    except Exception as e:
        logger.error(f"Graphiti search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")

    edges = []
    for edge in results:
        edge_dict: dict[str, Any] = {
            "uuid": getattr(edge, "uuid", None),
            "name": getattr(edge, "name", None),
            "fact": getattr(edge, "fact", None),
            "score": getattr(edge, "score", None),
        }

        # Include source/target node info if available
        source_node = getattr(edge, "source_node", None)
        if source_node:
            edge_dict["source_node"] = {
                "uuid": getattr(source_node, "uuid", None),
                "name": getattr(source_node, "name", None),
                "summary": getattr(source_node, "summary", None),
            }

        target_node = getattr(edge, "target_node", None)
        if target_node:
            edge_dict["target_node"] = {
                "uuid": getattr(target_node, "uuid", None),
                "name": getattr(target_node, "name", None),
                "summary": getattr(target_node, "summary", None),
            }

        edges.append(edge_dict)

    return SearchResponse(edges=edges)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("GRAPHITI_PORT", "8891"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
