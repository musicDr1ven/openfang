"""
PageIndex integration stage — PDF → hierarchical section tree.

Accepts a file path and returns a list of section dicts, each with:
  { heading, text, page_start, page_end, depth }

Two modes:
1. PageIndex service (HTTP) — calls the configured PageIndex REST API
2. Local fallback (PyMuPDF) — simple page-level extraction when PageIndex is unavailable
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _chunk_text(text: str, max_tokens: int = 20000, chars_per_token: int = 4) -> list[str]:
    """Split a long text into chunks of approximately `max_tokens` tokens."""
    max_chars = max_tokens * chars_per_token
    if len(text) <= max_chars:
        return [text]
    chunks = []
    while text:
        chunk = text[:max_chars]
        # Try to break at sentence boundary
        last_period = chunk.rfind(". ")
        if last_period > max_chars // 2:
            chunk = chunk[: last_period + 1]
        chunks.append(chunk)
        text = text[len(chunk):]
    return chunks


async def extract_sections_from_pdf_local(
    file_path: str,
    max_tokens_per_node: int = 20000,
) -> list[dict[str, Any]]:
    """
    Fallback PDF extraction using PyMuPDF when PageIndex service is unavailable.

    Returns sections at the page level.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError(
            "PyMuPDF not installed. Run: pip install pymupdf"
        )

    sections: list[dict[str, Any]] = []
    doc = fitz.open(file_path)

    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text").strip()
        if not text:
            continue

        chunks = _chunk_text(text, max_tokens=max_tokens_per_node)
        for i, chunk in enumerate(chunks):
            sections.append(
                {
                    "heading": f"Page {page_num + 1}" + (f" (part {i + 1})" if len(chunks) > 1 else ""),
                    "text": chunk,
                    "page_start": page_num + 1,
                    "page_end": page_num + 1,
                    "depth": 1,
                }
            )

    doc.close()
    logger.info(f"Local PDF extraction: {len(sections)} sections from '{file_path}'")
    return sections


async def extract_sections(
    file_path: str,
    pageindex_url: str | None = None,
    max_tokens_per_node: int = 20000,
) -> list[dict[str, Any]]:
    """
    Extract sections from a file.

    For PDFs: tries the PageIndex service first, falls back to local PyMuPDF extraction.
    For text files: splits by paragraph/heading markers.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Plain text files — split on double newlines (paragraph-level)
    if path.suffix.lower() not in (".pdf",):
        text = path.read_text(encoding="utf-8", errors="replace")
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        sections = []
        for i, para in enumerate(paragraphs):
            chunks = _chunk_text(para, max_tokens=max_tokens_per_node)
            for j, chunk in enumerate(chunks):
                sections.append(
                    {
                        "heading": f"Section {i + 1}" + (f" (part {j + 1})" if len(chunks) > 1 else ""),
                        "text": chunk,
                        "page_start": None,
                        "page_end": None,
                        "depth": 1,
                    }
                )
        logger.info(f"Text extraction: {len(sections)} sections from '{file_path}'")
        return sections

    # PDF files — try PageIndex service first
    if pageindex_url:
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{pageindex_url.rstrip('/')}/extract",
                    json={
                        "file_path": str(file_path),
                        "max_tokens_per_node": max_tokens_per_node,
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    sections = data.get("sections", [])
                    logger.info(
                        f"PageIndex extraction: {len(sections)} sections from '{file_path}'"
                    )
                    return sections
                else:
                    logger.warning(
                        f"PageIndex returned HTTP {resp.status_code}, falling back to local extraction"
                    )
        except Exception as e:
            logger.warning(f"PageIndex service unavailable: {e}. Falling back to local extraction.")

    # Local fallback
    return await extract_sections_from_pdf_local(
        file_path, max_tokens_per_node=max_tokens_per_node
    )
