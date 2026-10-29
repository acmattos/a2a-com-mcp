"""Loads ``servidor-mcp/main.py`` by path: ``import main`` is ambiguous now that ``agente/main.py`` exists too."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "servidor-mcp" / "main.py"
_spec = importlib.util.spec_from_file_location("server_main", _PATH)
server_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server_main)

create_app = server_main.create_app
