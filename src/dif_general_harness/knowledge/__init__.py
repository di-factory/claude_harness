"""Knowledge corpora (RAG): chunking, sync, retrieval and citations."""

from .chunk import Chunk, chunk
from .store import Hit, KnowledgeBase, SyncReport

__all__ = ["Chunk", "Hit", "KnowledgeBase", "SyncReport", "chunk"]
