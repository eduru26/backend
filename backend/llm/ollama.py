from __future__ import annotations

import httpx

from backend.core.circuit_breaker import get_breaker
from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)


class OllamaModelNotLoadedError(httpx.HTTPError):
    """Ollama is reachable but the configured model isn't pulled.

    Subclasses httpx.HTTPError so existing callers (chat.send_message)
    catch it through their generic httpx handler without special casing.
    """


class OllamaClient:
    def __init__(self) -> None:
        s = get_settings()
        self._base = s.ollama_base_url
        self._model = s.ollama_gen_model
        self._temperature = s.ollama_temperature
        self._timeout = float(s.ollama_timeout_sec)
        self._num_ctx = s.ollama_num_ctx
        self._num_predict = s.ollama_num_predict

    async def complete(
        self,
        system: str,
        user: str,
        *,
        timeout: float | None = None,
        num_predict: int | None = None,
        json_mode: bool = False,
    ) -> str:
        # Route through a circuit breaker: if Ollama is hung/dead, after a
        # few failures every call fails fast instead of every request
        # worker blocking on the full timeout.
        #
        # num_predict caps output tokens — callers that only need a short
        # answer (e.g. the PASS/FAIL validator) pass a small value so the
        # call returns fast instead of generating up to the default limit.
        # json_mode forces Ollama to emit valid JSON only (no prose, no
        # code fences), which small models would otherwise wrap freely.
        return await get_breaker("ollama_gen").call(
            self._complete_impl, system, user, timeout, num_predict, json_mode
        )

    async def _complete_impl(
        self,
        system: str,
        user: str,
        timeout: float | None,
        num_predict: int | None,
        json_mode: bool = False,
    ) -> str:
        t = timeout if timeout is not None else self._timeout
        body: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": self._temperature,
                "num_ctx": self._num_ctx,
                "num_predict": num_predict if num_predict is not None
                else self._num_predict,
            },
        }
        if json_mode:
            body["format"] = "json"
        async with httpx.AsyncClient(timeout=t) as client:
            resp = await client.post(f"{self._base}/api/chat", json=body)
            # A 404 from /api/chat means the requested model isn't loaded in
            # Ollama (vs an unreachable host, which would have raised earlier
            # with httpx.RequestError). Surface a clearer log so operators
            # know to run `ollama pull <model>` rather than chasing network
            # issues.
            if resp.status_code == 404:
                body = ""
                try:
                    body = resp.text[:300]
                except Exception:  # noqa: BLE001
                    pass
                logger.error(
                    "ollama.model_not_loaded",
                    model=self._model,
                    base=self._base,
                    body=body,
                )
                raise OllamaModelNotLoadedError(
                    f"Ollama model '{self._model}' is not loaded at "
                    f"{self._base}. Run `ollama pull {self._model}` in the "
                    "ollama container to install it."
                )
            resp.raise_for_status()
            return str(resp.json()["message"]["content"])


_instance: OllamaClient | None = None


def get_ollama() -> OllamaClient:
    global _instance
    if _instance is None:
        _instance = OllamaClient()
    return _instance
