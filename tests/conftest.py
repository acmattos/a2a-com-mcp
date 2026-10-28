import sys
from pathlib import Path

# "servidor-mcp" has a hyphen, so it is not importable as a package: expose it as a source root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "servidor-mcp"))
