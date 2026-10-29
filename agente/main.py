"""Composition root of the agent: ``uv run python agente/main.py`` (A2A on :7300, MCP host inside)."""

import asyncio
import logging
import os
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.agent_execution import RequestContext, SimpleRequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types.a2a_pb2 import SendMessageRequest, Task, TaskState
from a2a.utils.errors import UnsupportedOperationError
from starlette.applications import Starlette
from starlette.requests import Request

from card import PROTOCOL_VERSION, build_agent_card
from executor import RoomBookingExecutor
from ids import PrefixedIdGenerator
from mcp_host import DEFAULT_URL, McpHost
from pending import PendingStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7300
RPC_PATH = "/a2a"
VERSION_HEADER = "a2a-version"  # Starlette lower-cases header names


class V1CallContextBuilder(DefaultServerCallContextBuilder):
    """A request that names no A2A-Version is a v1.0 request: the SDK would otherwise read it as 0.3.

    An explicit ``A2A-Version`` is left alone, so a client that asks for 0.3 is still refused.
    """

    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state["headers"].setdefault(VERSION_HEADER, PROTOCOL_VERSION)
        return context


class TaskAwareContextBuilder(SimpleRequestContextBuilder):
    """Prepares a message that names an existing Task, before the SDK runs the executor.

    * A Task that is still running (SUBMITTED / WORKING) cannot take a message.
    * A paused Task whose private entry is gone cannot take one either (refusing here leaves the Task as it is;
      an exception inside the executor would turn it FAILED).
    * The message keeps the Task's ``contextId``: a continuation carries only ``taskId``, and without this the SDK
      would mint a new context for it and the Task would end up with two.

    Unknown and terminal Tasks are refused by the SDK itself.
    """

    def __init__(self, task_store: InMemoryTaskStore, pending: PendingStore) -> None:
        super().__init__(
            task_store=task_store,
            task_id_generator=PrefixedIdGenerator("task"),
            context_id_generator=PrefixedIdGenerator("ctx"),
        )
        self._store = task_store
        self._pending = pending

    async def build(
        self,
        context: ServerCallContext,
        params: SendMessageRequest | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        if task_id:
            stored = await self._store.get(task_id, context)
            if stored is not None:
                state = stored.status.state
                if state in _RUNNING:
                    raise UnsupportedOperationError(message=f"Task {task_id} esta em andamento: {TaskState.Name(state)}")
                if state == TaskState.TASK_STATE_INPUT_REQUIRED and task_id not in self._pending:
                    raise UnsupportedOperationError(message=f"Task {task_id} is not waiting for input")
                context_id = context_id or stored.context_id
        return await super().build(context, params, task_id, context_id, task)


_RUNNING = {TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING}


class OneAtATimePerTask(DefaultRequestHandler):
    """Handles the messages addressed to the same Task one after another.

    Two simultaneous answers to one paused Task then meet in order: the first takes the pause and finishes the
    Task, the second finds it terminal and gets the SDK's own refusal, instead of being queued behind the first
    and answered with a Task it did not change.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def on_message_send(self, params: SendMessageRequest, context: ServerCallContext):
        task_id = params.message.task_id
        if not task_id:
            return await super().on_message_send(params, context)
        lock = self._locks.get(task_id)
        if lock is None:
            lock = self._locks[task_id] = asyncio.Lock()
        async with lock:  # the local name keeps the lock alive while anyone waits on it
            return await super().on_message_send(params, context)


def create_app(
    mcp_url: str = DEFAULT_URL,
    public_url: str = f"http://localhost:{DEFAULT_PORT}",
    host_factory: Callable[[str], McpHost] = McpHost,
    pending: PendingStore | None = None,
) -> Starlette:
    install_log_filters()
    host = host_factory(mcp_url)
    card = build_agent_card(public_url.rstrip("/") + RPC_PATH)
    task_store = InMemoryTaskStore()
    pending = pending if pending is not None else PendingStore()
    handler = OneAtATimePerTask(
        agent_executor=RoomBookingExecutor(host, pending),
        task_store=task_store,
        agent_card=card,
        request_context_builder=TaskAwareContextBuilder(task_store, pending),
    )

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with host:  # one MCP client for the whole life of the process, closed on shutdown
            yield

    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, RPC_PATH, context_builder=V1CallContextBuilder()),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


class _DropMessage(logging.Filter):
    """Drops one known, harmless record of a logger; every other record of that logger still passes.

    a2a-sdk 1.3.0 answers ``SendMessage`` as soon as the Task is terminal and leaves its event generator open
    (``break`` in ``on_message_send``). The garbage collector closes it later, outside the original context, which
    makes OpenTelemetry log a failed ``detach`` and asyncio log the abandoned ``EventQueueSource._dispatch_loop``.
    The responses and the Tasks are already complete by then and no SDK task outlives GC.
    """

    def __init__(self, contains: str) -> None:
        super().__init__()
        self._contains = contains

    def filter(self, record: logging.LogRecord) -> bool:
        return self._contains not in record.getMessage()


_FILTERED_LOGGERS = (
    ("opentelemetry.context", "Failed to detach context"),
    ("asyncio", "EventQueueSource._dispatch_loop"),
    # At DEBUG the MCP client logs each request it sends, and a retry carries the opaque requestState.
    ("mcp.client.streamable_http", "requestState"),
)
_installed = False


def install_log_filters() -> None:
    """Drop the known harmless SDK noise and keep the requestState out of the logs. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    for logger_name, fragment in _FILTERED_LOGGERS:
        logging.getLogger(logger_name).addFilter(_DropMessage(fragment))


def main() -> None:
    host = os.environ.get("AGENT_HOST", DEFAULT_HOST)
    port = int(os.environ.get("AGENT_PORT", DEFAULT_PORT))
    app = create_app(
        mcp_url=os.environ.get("MCP_URL", DEFAULT_URL),
        public_url=os.environ.get("AGENT_PUBLIC_URL", f"http://localhost:{port}"),
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
