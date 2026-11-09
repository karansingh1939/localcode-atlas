"""Embed natural-language queries and search the local code index."""

import asyncio
import logging
from typing import Dict, Any

from ollama import AsyncClient
from backend.vector_store import get_db
from backend.config import Settings

logger = logging.getLogger(__name__)


def _get_ollama_client() -> AsyncClient:
    return AsyncClient(host=Settings.OLLAMA_HOST)


async def run_query(
    query: str,
    top_k: int = 10,
    explain: bool = False,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    """
    1. Embed the query with the same model used during indexing.
    2. Search SQLite vectors with cosine similarity.
    3. Optionally explain the results with a local model.
    """
    async with _get_ollama_client() as client:
        return await _query_with_client(client, query, top_k, explain, repo_path)


async def _query_with_client(
    client: AsyncClient,
    query: str,
    top_k: int,
    explain: bool,
    repo_path: str | None = None,
) -> Dict[str, Any]:

    # --- Embed query ---
    embed_prompt = f"search_query: {query}"
    try:
        resp = await client.embed(
            model=Settings.EMBED_MODEL, input=embed_prompt, truncate=False
        )
        # ollama SDK 0.5+: EmbeddingsResponse object
        query_vec = (
            list(resp.embeddings[0])
            if hasattr(resp, "embeddings")
            else resp["embeddings"][0]
        )
    except Exception as e:
        logger.error(f"Failed to embed query: {e}")
        raise

    # --- Local vector search ---
    db = get_db(repo_path)
    raw_results = await asyncio.to_thread(
        db.search, query_vec, top_k=top_k, query=query
    )

    # Normalise scores to [0, 1]
    results = []
    for r in raw_results:
        score = r.get("score", 0.0)
        # Cosine scores are in [-1, 1]; hybrid scores are already in [0, 1].
        r["score"] = max(0.0, min(1.0, score))
        results.append(r)

    # --- Optional explanation ---
    explanation: str | None = None
    if explain and results:
        context = "\n\n".join(
            f"{r['file_path']}:{r['line_start']} ({r['symbol_name']})\n{r['chunk_text']}"
            for r in results[:3]
        )
        system_msg = (
            "You are a code navigation assistant. "
            "Answer in 2-3 sentences. Reference specific function names and files."
        )
        messages = [
            {"role": "system", "content": system_msg},
            {
                "role": "user",
                "content": f"Codebase context:\n{context}\n\nQuestion: {query}",
            },
        ]
        try:
            chat_resp = await client.chat(
                model=Settings.EXPLAIN_MODEL, messages=messages
            )
            # ollama SDK 0.5+: ChatResponse object
            if hasattr(chat_resp, "message"):
                explanation = chat_resp.message.content
            else:
                explanation = chat_resp["message"]["content"]
        except Exception as e:
            logger.warning(f"Explanation failed ({Settings.EXPLAIN_MODEL}): {e}")
            explanation = (
                f"(Explain mode unavailable — is {Settings.EXPLAIN_MODEL} pulled?)"
            )

    return {"results": results, "explain_text": explanation}
