"""Readable identifiers for tasks, contexts, messages and artifacts: ``task-3f658e57d468``."""

import secrets

from a2a.server.id_generator import IDGenerator, IDGeneratorContext


class PrefixedIdGenerator(IDGenerator):
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def generate(self, context: IDGeneratorContext) -> str:
        return f"{self._prefix}-{secrets.token_hex(6)}"
