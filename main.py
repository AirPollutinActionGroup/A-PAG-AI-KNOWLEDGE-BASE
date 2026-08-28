"""Main Application Entrypoint."""

import uvicorn

from src.api.v1.router import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
