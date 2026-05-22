from __future__ import annotations

from typing import Iterable

import httpx

from backend.core.circuit_breaker import get_breaker
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.redis_client import cache_get_vector, cache_key, cache_set_json

logger = get_logger(__name__)


class EmbeddingClient:
    """Thin async wrapper around Ollama's /api/embeddings.

    Uses `ollama_embed_model` (default `nomic-embed-text`, 768-d) — which
    matches `vector_dim` in `Settings` and the `Vector(768)` columns. If
    you change the model, also change `vector_dim` and re-create the HNSW
    index, otherwise inserts will fail.
    """

    def __init__(self) -> None:
        s = get_settings()
        self._base = s.ollama_base_url
        self._model = s.ollama_embed_model
        self._timeout = float(s.ollama_timeout_sec)
        self._dim = s.vector_dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            return [0.0] * self._dim

        # Embeddings are deterministic for a given (model, text) — cache
        # them in Redis so a repeated chat query or re-ingested doc skips
        # the Ollama round-trip entirely.
        key = cache_key("embed", self._model, self._dim, text)
        cached = await cache_get_vector(key)
        if cached is not None and len(cached) == self._dim:
            return cached

        vec = await get_breaker("ollama_embed").call(self._embed_impl, text)
        await cache_set_json(key, vec, get_settings().cache_embedding_ttl_sec)
        return vec

    async def _embed_impl(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._base}/api/embeddings",
                json={"model": self._model, "prompt": text},
            )
            if resp.status_code == 404:
                # Model not loaded — surface a clear error rather than a
                # generic 404 from httpx. Operators run `ollama pull` to fix.
                logger.error(
                    "ollama.embed.model_not_loaded",
                    model=self._model, base=self._base,
                )
                raise httpx.HTTPError(
                    f"Ollama embed model '{self._model}' not loaded at {self._base}. "
                    f"Run `ollama pull {self._model}` to install it."
                )
            resp.raise_for_status()
            data = resp.json()
            vec = data.get("embedding") or []
            if len(vec) != self._dim:
                # Defensive: if Ollama returns a wrong-dim embedding we want
                # to fail loudly rather than corrupt the index.
                raise ValueError(
                    f"embedding dim mismatch: got {len(vec)}, expected {self._dim}"
                )
            return [float(x) for x in vec]

    async def embed_batch(self, texts: Iterable[str]) -> list[list[float]]:
        # Ollama's embeddings endpoint is single-prompt today; we serialise
        # sequentially because that mirrors what the server does internally
        # anyway and keeps memory bounded. If we later switch to a model
        # server that supports batch, change this in one place.
        out: list[list[float]] = []
        for t in texts:
            out.append(await self.embed(t))
        return out


_instance: EmbeddingClient | None = None


def get_embedder() -> EmbeddingClient:
    global _instance
    if _instance is None:
        _instance = EmbeddingClient()
    return _instance
