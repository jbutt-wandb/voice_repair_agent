"""Entrypoint: load API keys from .env, then serve the app.

Run with:  uv run main.py   then open http://127.0.0.1:8000 in Chrome.
"""

import uvicorn
from dotenv import load_dotenv

from server.config import SERVER_HOST, SERVER_PORT


def main() -> None:
    load_dotenv()  # DEEPGRAM_API_KEY + OPENAI_API_KEY
    uvicorn.run("server.app:app", host=SERVER_HOST, port=SERVER_PORT)


if __name__ == "__main__":
    main()
