//! `GraphBackend` trait — abstracts the knowledge graph storage layer.
//!
//! This trait allows `MemorySubstrate` to swap between the legacy SQLite
//! knowledge graph (`SqliteGraphStore`) and the Graphiti temporal knowledge
//! graph engine backed by FalkorDB, without changing any caller code.

use async_trait::async_trait;
use openfang_types::error::OpenFangResult;
use openfang_types::memory::{Entity, GraphMatch, GraphPattern, Relation};
use std::sync::Arc;

/// Pluggable graph backend for knowledge storage and retrieval.
///
/// Implementations:
/// - [`SqliteGraphStore`] — legacy SQLite backend (backward-compat default)
/// - [`GraphitiBackend`] — Graphiti REST client (temporal, hybrid-search)
#[async_trait]
pub trait GraphBackend: Send + Sync {
    /// Add an entity to the knowledge graph. Returns the assigned entity ID.
    async fn add_entity(&self, entity: Entity) -> OpenFangResult<String>;

    /// Add a relation between two entities. Returns the relation ID.
    async fn add_relation(&self, relation: Relation) -> OpenFangResult<String>;

    /// Query the graph using a structural pattern (source, relation, target).
    async fn query_graph(&self, pattern: GraphPattern) -> OpenFangResult<Vec<GraphMatch>>;

    /// Hybrid search (vector + BM25 + graph traversal, fused via RRF).
    ///
    /// `group_id` namespaces the search to a specific agent's knowledge graph.
    /// Falls back to `query_graph` for backends that don't support hybrid search.
    async fn search(
        &self,
        query: &str,
        group_id: &str,
        limit: usize,
    ) -> OpenFangResult<Vec<GraphMatch>>;

    /// Ingest a raw text episode into the knowledge graph.
    ///
    /// The backend extracts entities and relations automatically.
    /// `source` is a human-readable description of the episode source
    /// (e.g. the section heading for a document, or "conversation" for chat).
    /// `group_id` namespaces the episode to a specific agent knowledge graph.
    ///
    /// No-op for backends that don't support episode ingestion.
    async fn ingest_episode(
        &self,
        text: &str,
        source: &str,
        group_id: &str,
    ) -> OpenFangResult<()>;
}

/// Create a graph backend from configuration.
///
/// Follows the same factory pattern as [`crate::embedding::create_embedding_driver`].
///
/// - `backend = "graphiti"` → [`GraphitiBackend`] (HTTP client to Graphiti service)
/// - `backend = "sqlite"` or any other value → [`SqliteGraphStore`] (default, no-op search)
pub fn create_graph_backend(
    config: &openfang_types::config::GraphConfig,
    sqlite_conn: Arc<std::sync::Mutex<rusqlite::Connection>>,
) -> Arc<dyn GraphBackend + Send + Sync> {
    match config.backend.as_str() {
        "graphiti" => Arc::new(crate::graphiti_backend::GraphitiBackend::new(
            &config.graphiti_url,
            sqlite_conn,
        )),
        _ => Arc::new(crate::knowledge::SqliteGraphStore::new(sqlite_conn)),
    }
}
