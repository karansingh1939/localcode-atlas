import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    VECTORAI_DB_PATH: str = os.getenv("VECTORAI_DB_PATH", "./.vectorai_db")
    OLLAMA_HOST: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    EMBED_MODEL: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
    EXPLAIN_MODEL: str = os.getenv("EXPLAIN_MODEL", "llama3.2")
    EMBED_BATCH_SIZE: int = max(1, int(os.getenv("EMBED_BATCH_SIZE", "32")))
    WATCH_DEBOUNCE_SECONDS: float = max(
        0, float(os.getenv("WATCH_DEBOUNCE_SECONDS", "0.35"))
    )
