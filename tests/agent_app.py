"""Loads ``agente/main.py`` by path: ``import main`` would be ambiguous with ``servidor-mcp/main.py``."""

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "agente" / "main.py"
_spec = importlib.util.spec_from_file_location("agent_main", _PATH)
agent_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_main)

create_app = agent_main.create_app
V1CallContextBuilder = agent_main.V1CallContextBuilder
