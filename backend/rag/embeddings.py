from __future__ import annotations

from typing import Iterable

import httpx

from backend.core.circuit_breaker import CircuitBreaker
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.redis_client import cache_get_vector, cache_key, cache_set_json

logger = get_logger(__name__)

# Below this size we stop trying to split an overflowing chunk.
_MIN_RESILIENT_CHARS = 40
_MAX_RESILIENT_DEPTH = 8


class ContextLengthError(httpx.HTTPError):
    """The text was longer than the embedding model's context window.

    Subclasses httpx.HTTPError so existing broad handlers (chat endpoint,
    etc.) still catch it cleanly. Raised distinctly so `embed_resilient`
    can split-and-retry — this happens with non-Latin scripts (Kannada,
    Hindi…) where a given character count tokenizes into many more
    tokens than the equivalent English text.
    """


# Module-level breaker with ContextLengthError in `ignore`: a chunk being
# too long is not a downstream-health signal — it's the input's fault —
# so it must not count against the breaker (otherwise a single big
# textbook upload could trip the circuit and kill chat embeddings).
_embed_breaker = CircuitBreaker("ollama_embed", ignore=(ContextLengthError,))


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

        vec = await _embed_breaker.call(self._embed_impl, text)
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
            if resp.status_code != 200:
                body = resp.text[:300]
                # 500 with "context length" / "exceeds the context" =
                # input too long for the model. Distinct exception so
                # embed_resilient can split-and-retry.
                if (
                    resp.status_code == 500
                    and ("context length" in body.lower()
                         or "exceeds the context" in body.lower())
                ):
                    raise ContextLengthError(body)
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

    async def embed_resilient(
        self, text: str, *, _depth: int = 0
    ) -> list[tuple[str, list[float]]]:
        """Embed `text`, splitting it if the model rejects it as too long.

        Returns a list of (text_piece, vector) pairs — normally one, but
        more when a chunk had to be split because of context-window
        overflow. The caller stores each pair as its own RagDocumentChunk
        so no content is lost.
        """
        text = (text or "").strip()
        if not text:
            return []
        try:
            return [(text, await self.embed(text))]
        except ContextLengthError:
            if len(text) <= _MIN_RESILIENT_CHARS or _depth >= _MAX_RESILIENT_DEPTH:
                raise
            mid = len(text) // 2
            window = max(20, len(text) // 6)
            space = text.rfind(" ", max(0, mid - window), mid + window)
            cut = space if space > 0 else mid
            left, right = text[:cut].strip(), text[cut:].strip()
            if not left or not right:
                raise
            return (
                await self.embed_resilient(left, _depth=_depth + 1)
                + await self.embed_resilient(right, _depth=_depth + 1)
            )

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
