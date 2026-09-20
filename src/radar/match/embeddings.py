"""Swappable paragraph embedding interface with a lazy local default."""

from typing import Protocol

import numpy as np

from radar.config import Settings
from radar.db import Database, content_key
from radar.models import FilingParagraph


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, paragraphs: list[FilingParagraph]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    def __init__(self, settings: Settings, db: Database):
        self.name = settings.embedding_model
        self.batch_size = settings.embedding_batch_size
        self.db = db
        self._model = None

    def embed(self, paragraphs: list[FilingParagraph]) -> np.ndarray:
        if not paragraphs:
            return np.empty((0, 0), dtype=np.float32)
        keys = [
            content_key(
                {
                    "provider": "sentence-transformers",
                    "model": self.name,
                    "normalization": "v1",
                    "hash": p.text_hash,
                }
            )
            for p in paragraphs
        ]
        cached = {key: self.db.get_embedding(key) for key in dict.fromkeys(keys)}
        missing = {
            key: p.normalized_text for key, p in zip(keys, paragraphs) if cached[key] is None
        }
        if missing:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(self.name)
            encoded = self._model.encode(
                list(missing.values()),
                batch_size=self.batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            for key, vector in zip(missing, encoded, strict=True):
                value = vector.tolist()
                self.db.save_embedding(key, value)
                cached[key] = value
        matrix = np.asarray([cached[key] for key in keys], dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if matrix.ndim != 2 or not np.isfinite(matrix).all() or np.any(norms == 0):
            raise ValueError("Embedding provider returned invalid vectors")
        return matrix / norms
