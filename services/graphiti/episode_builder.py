"""
Converts PageIndex section trees into Graphiti episodes for batch ingestion.

Each section becomes one episode with:
  - content = the section text
  - source_description = the section heading (helps LLM contextualize extraction)
  - group_id = the agent knowledge namespace
  - episode_type = "text"
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Episode:
    content: str
    source_description: str
    group_id: str
    episode_type: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "source_description": self.source_description,
            "group_id": self.group_id,
            "episode_type": self.episode_type,
        }


def sections_to_episodes(
    sections: list[dict[str, Any]],
    group_id: str,
    document_title: str = "",
) -> list[Episode]:
    """
    Convert a list of sections (from PageIndex) into Graphiti episodes.

    Each section heading is prepended to the text so the LLM has structural
    context when extracting entities and relations.
    """
    episodes: list[Episode] = []

    for section in sections:
        heading = section.get("heading", "")
        text = section.get("text", "").strip()
        page_start = section.get("page_start")
        page_end = section.get("page_end")

        if not text:
            continue

        # Build a rich source description for the Graphiti edge metadata
        source_parts = []
        if document_title:
            source_parts.append(document_title)
        if heading:
            source_parts.append(heading)
        if page_start is not None:
            if page_end and page_end != page_start:
                source_parts.append(f"pp. {page_start}–{page_end}")
            else:
                source_parts.append(f"p. {page_start}")
        source_description = " / ".join(source_parts) if source_parts else "document"

        # Prefix the heading into the content so the LLM has section context
        full_content = f"{heading}\n\n{text}" if heading else text

        episodes.append(
            Episode(
                content=full_content,
                source_description=source_description,
                group_id=group_id,
            )
        )

    return episodes
