"""Composition root of the MCP server: ``uv run python servidor-mcp/main.py``."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from protocol import build_server
from repository import DEFAULT_DATA_DIR, Repository
from service import BookingService
from telemetry import RequestLogMiddleware

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7301
MCP_PATH = "/mcp"


def create_app(data_dir: Path | str = DEFAULT_DATA_DIR, host: str = DEFAULT_HOST):
    service = BookingService(Repository(data_dir))
    asgi = build_server(service).streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, json_response=True, host=host
    )
    return RequestLogMiddleware(asgi)


def main() -> None:
    host = os.environ.get("MCP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("MCP_PORT", DEFAULT_PORT))
    uvicorn.run(create_app(host=host), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
