"""
LLM and embedder provider factory for the Graphiti service.

Graphiti requires an LLM client for entity extraction, deduplication, and
bi-temporal reasoning, plus an OpenAI-compatible embedder for vector search.

Provider selection is driven by the GRAPHITI_LLM_PROVIDER and
GRAPHITI_EMBEDDER_PROVIDER environment variables (set via openfang.toml
graphiti_llm and graphiti_embedder sections).
"""

from __future__ import annotations

import os

from graphiti_core.llm_client import LLMConfig


def create_llm_client(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
):
    """
    Create a Graphiti LLM client for the given provider.

    Supported providers:
    - "anthropic"  — uses ANTHROPIC_API_KEY env var (recommended)
    - "openai"     — uses OPENAI_API_KEY env var
    - "openrouter" — uses OPENROUTER_API_KEY, routes via OpenRouter
    - "ollama"     — uses local Ollama (no API key required)

    Falls back to reading GRAPHITI_LLM_PROVIDER / GRAPHITI_LLM_MODEL
    environment variables when arguments are not provided.
    """
    provider = (provider or os.environ.get("GRAPHITI_LLM_PROVIDER", "anthropic")).lower()
    model = model or os.environ.get(
        "GRAPHITI_LLM_MODEL", "claude-haiku-4-5-20251001"
    )
    base_url = base_url or os.environ.get("GRAPHITI_LLM_BASE_URL") or None

    match provider:
        case "anthropic":
            from graphiti_core.llm_client import AnthropicClient

            cfg = LLMConfig(model=model)
            return AnthropicClient(cfg)

        case "openai":
            from graphiti_core.llm_client import OpenAIClient

            kwargs: dict = {"model": model}
            if base_url:
                kwargs["base_url"] = base_url
            return OpenAIClient(LLMConfig(**kwargs))

        case "openrouter":
            from graphiti_core.llm_client import OpenAIClient

            openrouter_url = (
                base_url
                or os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
            )
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            cfg = LLMConfig(model=model, base_url=openrouter_url, api_key=api_key)
            return OpenAIClient(cfg)

        case "ollama":
            from graphiti_core.llm_client import OpenAIClient

            ollama_url = base_url or os.environ.get(
                "OLLAMA_BASE_URL", "http://localhost:11434/v1"
            )
            cfg = LLMConfig(model=model, base_url=ollama_url, api_key="ollama")
            return OpenAIClient(cfg)

        case _:
            # Unknown provider — attempt OpenAI-compat with the provided base_url
            from graphiti_core.llm_client import OpenAIClient

            cfg = LLMConfig(model=model, base_url=base_url or "https://api.openai.com/v1")
            return OpenAIClient(cfg)


def create_embedder(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
):
    """
    Create a Graphiti embedder.

    Only OpenAI-compatible providers are supported since Anthropic does not
    offer an embeddings API.

    Supported providers:
    - "openai"     — OPENAI_API_KEY (default)
    - "openrouter" — OPENROUTER_API_KEY with OpenAI embedding models
    - "ollama"     — local Ollama (e.g. nomic-embed-text)
    """
    from graphiti_core.embedder import OpenAIEmbedder, EmbedderConfig

    provider = (provider or os.environ.get("GRAPHITI_EMBEDDER_PROVIDER", "openai")).lower()
    model = model or os.environ.get("GRAPHITI_EMBEDDER_MODEL", "text-embedding-3-small")
    base_url = base_url or os.environ.get("GRAPHITI_EMBEDDER_BASE_URL") or None

    match provider:
        case "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            cfg = EmbedderConfig(
                model=model,
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
            )
            return OpenAIEmbedder(cfg)

        case "openrouter":
            openrouter_url = base_url or "https://openrouter.ai/api/v1"
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            cfg = EmbedderConfig(model=model, api_key=api_key, base_url=openrouter_url)
            return OpenAIEmbedder(cfg)

        case "ollama":
            ollama_url = base_url or os.environ.get(
                "OLLAMA_BASE_URL", "http://localhost:11434/v1"
            )
            cfg = EmbedderConfig(model=model, api_key="ollama", base_url=ollama_url)
            return OpenAIEmbedder(cfg)

        case _:
            # Default OpenAI-compat
            api_key = os.environ.get("OPENAI_API_KEY", "")
            cfg = EmbedderConfig(
                model=model,
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
            )
            return OpenAIEmbedder(cfg)
