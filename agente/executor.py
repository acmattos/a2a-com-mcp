"""The A2A side of the agent, and the bridge to the MCP pause (MRTR).

Task lifecycle:

    Task(SUBMITTED) -> WORKING -> COMPLETED + artifact "reserva"   (booking made)
                               -> FAILED                           (tool error, server unreachable, bad answer)
                               -> REJECTED                         (the text is not a booking command)
                               -> INPUT_REQUIRED                   (MCP answered input_required: see below)

    INPUT_REQUIRED -> (escolha=<id>)       WORKING -> COMPLETED | FAILED | INPUT_REQUIRED again
                   -> (escolha=recusar)    WORKING -> CANCELED (no artifact)
                   -> (anything else)      INPUT_REQUIRED again, same list, MCP not called

THE BRIDGE: ``_conclude`` is where an MCP ``input_required`` becomes ``TASK_STATE_INPUT_REQUIRED`` (the opaque
pause goes into the private :class:`PendingStore`, the caller only sees ``alternativas: ...``), and ``_continue``
is where the stored pause goes back to the server, through ``McpHost.resume`` (same key, same state, same
arguments, new JSON-RPC id).

The executor keeps no per-Task state of its own: the pause lives in the store, keyed by ``taskId``; the trace
context is the Task's own (stored with the pause) or the request's, passed down explicitly.
"""

import json
import logging
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import Part, Task, TaskState, TaskStatus
from a2a.utils.errors import UnsupportedOperationError

from command import DECLINE, Choice, InvalidCommand, parse_choice, parse_command
from ids import PrefixedIdGenerator
from mcp_host import (
    Answer,
    ContractError,
    Declined,
    InputRequired,
    McpHost,
    McpHostError,
    McpProtocolError,
    McpUnavailable,
    Reserved,
    ToolFailed,
)
from pending import PendingStore, PendingTask

logger = logging.getLogger(__name__)

ARTIFACT_NAME = "reserva"
TRACEPARENT_HEADER = "traceparent"

UNAVAILABLE_TEXT = "Servidor de salas indisponivel. Tente novamente."
UNEXPECTED_TEXT = "Resposta inesperada do servidor de salas."
ALTERNATIVES_PREFIX = "alternativas: "
CANCELED_TEXT = "Reserva cancelada: pedido recusado."


class RoomBookingExecutor(AgentExecutor):
    def __init__(self, host: McpHost, pending: PendingStore | None = None) -> None:
        self._host = host
        self._pending = pending if pending is not None else PendingStore()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(
            event_queue,
            context.task_id,
            context.context_id,
            artifact_id_generator=PrefixedIdGenerator("art"),
            message_id_generator=PrefixedIdGenerator("msg"),
        )
        if context.current_task is None:
            await self._start(context, event_queue, updater)
        else:
            await self._continue(context, updater)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError(message="Cancelamento nao suportado")

    # --- new Task ------------------------------------------------------------------------------------------------

    async def _start(self, context: RequestContext, event_queue: EventQueue, updater: TaskUpdater) -> None:
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message],
            )
        )
        await updater.start_work()
        command = parse_command(context.get_user_input())
        if isinstance(command, InvalidCommand):
            await _finish(updater, TaskState.TASK_STATE_REJECTED, command.message)
            return
        try:
            outcome = await self._host.book(command.request, traceparent=_traceparent(context))
        except McpHostError as error:
            await _finish(updater, TaskState.TASK_STATE_FAILED, _describe_failure(error))
            return
        await self._conclude(outcome, updater, context.task_id, _traceparent(context))

    # --- continuation of a paused Task ---------------------------------------------------------------------------

    async def _continue(self, context: RequestContext, updater: TaskUpdater) -> None:
        task_id = context.task_id
        if context.current_task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
            raise _not_waiting(task_id)
        entry = self._pending.get(task_id)
        if entry is None:
            raise _not_waiting(task_id)
        answer = _answer_to(parse_choice(context.get_user_input()), entry)
        if answer is None:  # not one of the offered ids and not "recusar": ask again, the server is not involved
            await _ask(updater, _alternatives_line(entry))
            return
        # claim() removes the entry as it returns it: of two simultaneous answers only one gets the pause.
        entry = self._pending.claim(task_id)
        if entry is None:
            raise _not_waiting(task_id)
        await updater.start_work()
        # The Task keeps the trace it started with; a request can only supply one to a Task that had none.
        traceparent = entry.traceparent or _traceparent(context)
        try:
            outcome = await self._host.resume(entry.pause, answer, traceparent=traceparent)
        except McpHostError as error:
            await _finish(updater, TaskState.TASK_STATE_FAILED, _describe_failure(error))
            return
        await self._conclude(outcome, updater, task_id, traceparent)

    # --- outcome -> Task state -----------------------------------------------------------------------------------

    async def _conclude(self, outcome: object, updater: TaskUpdater, task_id: str, traceparent: str | None) -> None:
        if isinstance(outcome, Reserved):
            await updater.add_artifact(
                [Part(text=_reservation_json(outcome))], name=ARTIFACT_NAME, last_chunk=True
            )
            reservation = outcome.reservation
            text = f"Reserva {reservation['reserva']} confirmada na {reservation['sala']}."
            await _finish(updater, TaskState.TASK_STATE_COMPLETED, text)
        elif isinstance(outcome, ToolFailed):
            await _finish(updater, TaskState.TASK_STATE_FAILED, outcome.message)
        elif isinstance(outcome, InputRequired):
            # The entry is stored before the Task says it is waiting, so an answer can never arrive too early.
            entry = PendingTask(outcome, traceparent)
            self._pending.put(task_id, entry)
            await _ask(updater, _alternatives_line(entry))
        elif isinstance(outcome, Declined):
            await _finish(updater, TaskState.TASK_STATE_CANCELED, CANCELED_TEXT)
        else:
            raise AssertionError(f"unhandled outcome {type(outcome).__name__}")


def _alternatives_line(entry: PendingTask) -> str:
    return ALTERNATIVES_PREFIX + ", ".join(entry.pause.choices)


def _answer_to(choice: Choice | None, entry: PendingTask) -> Answer | None:
    """``recusar`` declines; an id the server offered accepts; everything else is no answer at all."""
    if choice is None:
        return None
    if choice.value == DECLINE:
        return Answer.decline()
    if choice.value in entry.pause.choices:
        return Answer.accept(choice.value)
    return None


def _not_waiting(task_id: str) -> UnsupportedOperationError:
    return UnsupportedOperationError(message=f"Task {task_id} is not waiting for input")


async def _ask(updater: TaskUpdater, text: str) -> None:
    """Pause the Task with ``text`` as ``status.message``.

    Unlike :func:`_finish` the message is sent once: the SDK moves a status message into ``history`` when the Task
    is continued, so the pause message reaches ``history`` exactly once, between the request and the answer.
    """
    await updater.requires_input(updater.new_agent_message([Part(text=text)]))


async def _finish(updater: TaskUpdater, state: TaskState, text: str) -> None:
    """Move the Task to the terminal ``state`` with the agent's message visible in ``status.message`` AND in ``history``.

    The SDK moves a status message into ``history`` only when the next status update replaces it, so a message
    sent once with the last update would never reach ``history``. Sending the same Message (same messageId) with
    a WORKING update first, then with the final state, leaves ``history = [..., message]`` and
    ``status.message = message``, the shape of the contract examples.
    """
    message = updater.new_agent_message([Part(text=text)])
    await updater.start_work(message)
    await updater.update_status(state, message)


def _traceparent(context: RequestContext) -> str | None:
    return context.call_context.state.get("headers", {}).get(TRACEPARENT_HEADER)


def _reservation_json(outcome: Reserved) -> str:
    payload: dict[str, Any] = {**outcome.reservation, "politica": outcome.policy_version}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _describe_failure(error: McpHostError) -> str:
    """What the caller may know: never the server URL, a stack trace or anything from a token."""
    if isinstance(error, McpUnavailable):
        return UNAVAILABLE_TEXT
    if isinstance(error, McpProtocolError):
        logger.warning("MCP protocol error %s", error.code)
        return f"O servidor de salas recusou o pedido (codigo {error.code})."
    if isinstance(error, ContractError):
        logger.warning("MCP contract error: %s", error)
        return UNEXPECTED_TEXT
    logger.warning("MCP host error: %s", error)
    return UNEXPECTED_TEXT
