"""The agent's private memory of paused Tasks, one entry per ``taskId``.

An entry is the MCP pause exactly as the host returned it (question key, choices, the opaque ``requestState``,
the original request, the policy version) plus the trace context the Task started with. It never goes into a
Task, a message, an artifact, metadata or a log: the A2A side only ever sees the ``alternativas:`` line.

All methods are synchronous and never await, so under asyncio each one is atomic. :meth:`claim` is the guard
against two continuations of the same Task: it removes the entry as it returns it, so only one caller gets it.
"""

from dataclasses import dataclass

from mcp_host import InputRequired


@dataclass(frozen=True)
class PendingTask:
    pause: InputRequired  # request_state is repr=False there: never rendered
    traceparent: str | None  # the Task's own trace context, reused by every later MCP call of that Task


class PendingStore:
    def __init__(self) -> None:
        self._entries: dict[str, PendingTask] = {}

    def put(self, task_id: str, entry: PendingTask) -> None:
        self._entries[task_id] = entry

    def get(self, task_id: str) -> PendingTask | None:
        return self._entries.get(task_id)

    def claim(self, task_id: str) -> PendingTask | None:
        """Take the entry out; whoever gets a value owns the pause, anyone after gets ``None``."""
        return self._entries.pop(task_id, None)

    def discard(self, task_id: str) -> None:
        self._entries.pop(task_id, None)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"PendingStore({len(self)} paused)"
