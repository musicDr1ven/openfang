//! Graphiti REST API backend for the `GraphBackend` trait.
//!
//! When `[graph] backend = "graphiti"` is set in `openfang.toml`, this backend
//! replaces the SQLite knowledge store. All entity/relation/search operations
//! are forwarded to the Graphiti service running at `graphiti_url`.
//!
//! Falls back to the SQLite store for `add_entity` / `add_relation` / `query_graph`
//! so that data added via the existing tools is never silently dropped when the
//! Graphiti service is unavailable.

use crate::graph_backend::GraphBackend;
use crate::knowledge::SqliteGraphStore;
use async_trait::async_trait;
use openfang_types::error::{OpenFangError, OpenFangResult};
use openfang_types::memory::{Entity, GraphMatch, GraphPattern, Relation};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::sync::{Arc, Mutex};
use tracing::{debug, warn};

/// Graphiti REST API backend.
///
/// Wraps the Graphiti service (Python FastAPI) to provide temporal knowledge
/// graph storage with hybrid retrieval (vector + BM25 + graph traversal).
pub struct GraphitiBackend {
    /// Base URL of the Graphiti service (e.g. "http://localhost:8891").
    graphiti_url: String,
    /// HTTP client for REST calls.
    client: reqwest::Client,
    /// SQLite fallback for structural add_entity / add_relation / query_graph.
    sqlite: SqliteGraphStore,
}

impl GraphitiBackend {
    /// Create a new Graphiti backend.
    ///
    /// `sqlite_conn` is used as a fallback for structural operations so that
    /// agents can still add/query entities even when Graphiti is unavailable.
    pub fn new(graphiti_url: &str, sqlite_conn: Arc<Mutex<Connection>>) -> Self {
        Self {
            graphiti_url: graphiti_url.trim_end_matches('/').to_string(),
            client: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(60))
                .build()
                .unwrap_or_default(),
            sqlite: SqliteGraphStore::new(sqlite_conn),
        }
    }
}

// ----- Graphiti service request/response types -----

#[derive(Serialize)]
struct EpisodeRequest<'a> {
    content: &'a str,
    source_description: &'a str,
    group_id: &'a str,
    episode_type: &'a str,
}

#[derive(Serialize)]
struct SearchRequest<'a> {
    query: &'a str,
    group_id: &'a str,
    limit: usize,
}

#[derive(Deserialize, Debug)]
struct GraphitiEdge {
    uuid: Option<String>,
    name: Option<String>,
    fact: Option<String>,
    #[serde(rename = "source_node")]
    source: Option<GraphitiNode>,
    #[serde(rename = "target_node")]
    target: Option<GraphitiNode>,
    #[serde(rename = "score")]
    relevance: Option<f64>,
}

#[derive(Deserialize, Debug, Clone)]
struct GraphitiNode {
    uuid: Option<String>,
    name: Option<String>,
    summary: Option<String>,
}

#[derive(Deserialize, Debug)]
struct SearchResponse {
    edges: Option<Vec<GraphitiEdge>>,
}

/// Convert a Graphiti edge into a `GraphMatch` for the OpenFang memory API.
fn edge_to_match(edge: GraphitiEdge) -> Option<GraphMatch> {
    use openfang_types::memory::{EntityType, RelationType};

    let make_entity = |node: &GraphitiNode| openfang_types::memory::Entity {
        id: node.uuid.clone().unwrap_or_default(),
        entity_type: EntityType::Custom("graphiti".to_string()),
        name: node.name.clone().unwrap_or_default(),
        properties: {
            let mut m = std::collections::HashMap::new();
            if let Some(s) = &node.summary {
                m.insert("summary".to_string(), serde_json::Value::String(s.clone()));
            }
            m
        },
        created_at: chrono::Utc::now(),
        updated_at: chrono::Utc::now(),
    };

    let source_node = edge.source.as_ref()?;
    let target_node = edge.target.as_ref()?;

    let relation = openfang_types::memory::Relation {
        source: source_node.uuid.clone().unwrap_or_default(),
        relation: RelationType::Custom(edge.name.clone().unwrap_or_else(|| "relates_to".to_string())),
        target: target_node.uuid.clone().unwrap_or_default(),
        properties: {
            let mut m = std::collections::HashMap::new();
            if let Some(f) = &edge.fact {
                m.insert("fact".to_string(), serde_json::Value::String(f.clone()));
            }
            if let Some(score) = edge.relevance {
                m.insert(
                    "relevance_score".to_string(),
                    serde_json::Value::Number(
                        serde_json::Number::from_f64(score)
                            .unwrap_or_else(|| serde_json::Number::from(0)),
                    ),
                );
            }
            m
        },
        confidence: edge.relevance.map(|s| s as f32).unwrap_or(1.0),
        created_at: chrono::Utc::now(),
    };

    Some(GraphMatch {
        source: make_entity(source_node),
        relation,
        target: make_entity(target_node),
    })
}

#[async_trait]
impl GraphBackend for GraphitiBackend {
    /// Delegate to SQLite for structural entity storage.
    /// Graphiti manages entities internally during episode ingestion.
    async fn add_entity(&self, entity: Entity) -> OpenFangResult<String> {
        let store = self.sqlite.clone();
        tokio::task::spawn_blocking(move || store.add_entity(entity))
            .await
            .map_err(|e| OpenFangError::Internal(e.to_string()))?
    }

    /// Delegate to SQLite for structural relation storage.
    async fn add_relation(&self, relation: Relation) -> OpenFangResult<String> {
        let store = self.sqlite.clone();
        tokio::task::spawn_blocking(move || store.add_relation(relation))
            .await
            .map_err(|e| OpenFangError::Internal(e.to_string()))?
    }

    /// Delegate to SQLite for structural pattern queries.
    async fn query_graph(&self, pattern: GraphPattern) -> OpenFangResult<Vec<GraphMatch>> {
        let store = self.sqlite.clone();
        tokio::task::spawn_blocking(move || store.query_graph(pattern))
            .await
            .map_err(|e| OpenFangError::Internal(e.to_string()))?
    }

    /// Hybrid search via the Graphiti service.
    ///
    /// Returns graph context blocks (entity + relation + entity triples) ranked
    /// by relevance using fused vector + BM25 + graph traversal scoring.
    async fn search(
        &self,
        query: &str,
        group_id: &str,
        limit: usize,
    ) -> OpenFangResult<Vec<GraphMatch>> {
        let url = format!("{}/search", self.graphiti_url);
        let req = SearchRequest { query, group_id, limit };

        debug!(url = %url, query = %query, group_id = %group_id, "Graphiti hybrid search");

        let response = self
            .client
            .post(&url)
            .json(&req)
            .send()
            .await
            .map_err(|e| {
                warn!("Graphiti search request failed: {e}");
                OpenFangError::Internal(format!("Graphiti search failed: {e}"))
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            warn!("Graphiti search returned HTTP {status}: {body}");
            return Err(OpenFangError::Internal(format!(
                "Graphiti search HTTP {status}: {body}"
            )));
        }

        let search_resp: SearchResponse = response.json().await.map_err(|e| {
            OpenFangError::Internal(format!("Graphiti search response parse error: {e}"))
        })?;

        let matches = search_resp
            .edges
            .unwrap_or_default()
            .into_iter()
            .filter_map(edge_to_match)
            .collect();

        Ok(matches)
    }

    /// Ingest a text episode into the Graphiti temporal knowledge graph.
    ///
    /// Graphiti automatically extracts entities and relations via LLM, deduplicates
    /// against the existing graph, and stores the result with bi-temporal metadata.
    async fn ingest_episode(
        &self,
        text: &str,
        source: &str,
        group_id: &str,
    ) -> OpenFangResult<()> {
        let url = format!("{}/episodes", self.graphiti_url);
        let req = EpisodeRequest {
            content: text,
            source_description: source,
            group_id,
            episode_type: "text",
        };

        debug!(
            url = %url,
            source = %source,
            group_id = %group_id,
            text_len = text.len(),
            "Graphiti ingest episode"
        );

        let response = self
            .client
            .post(&url)
            .json(&req)
            .send()
            .await
            .map_err(|e| {
                warn!("Graphiti ingest_episode request failed: {e}");
                OpenFangError::Internal(format!("Graphiti ingest failed: {e}"))
            })?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            warn!("Graphiti ingest returned HTTP {status}: {body}");
            return Err(OpenFangError::Internal(format!(
                "Graphiti ingest HTTP {status}: {body}"
            )));
        }

        debug!("Graphiti episode ingested successfully");
        Ok(())
    }
}
