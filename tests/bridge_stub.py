"""A scripted MCP server for the bridge: pauses on owners named ``Conf*`` and checks what the retry echoes."""

import asyncio
import json

import httpx2

from host_helpers import StubMcpServer


def pause_result(token: str, choices=("sala-a", "sala-b"), key="escolha_de_sala") -> dict:
    return {"resultType": "input_required", "requestState": token, "inputRequests": {key: {
        "method": "elicitation/create", "params": {"mode": "form", "message": "ocupada", "requestedSchema": {
            "type": "object", "properties": {"sala": {"type": "string", "enum": list(choices)}},
            "required": ["sala"]}}}}}


def reserved(args: dict, room: str, number: int) -> dict:
    payload = {"reserva": f"res-{number:04d}", "reservado": True, "sala": room, "inicio": args["inicio"],
               "fim": args["fim"], "responsavel": args["responsavel"], "politica": "stub", "motivo": None}
    return {"resultType": "complete", "isError": False, "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload)}]}


class BridgeStub(StubMcpServer):
    """Initial call by ``Conf<tag>`` -> pause with token ``TOKEN-<tag>-<n>``. A retry must echo that token and key.

    ``repause`` lists tags whose first retry is answered with a second pause (token ``TOKEN2-<tag>``, choices
    ``sala-z``). ``fail`` lists tags whose retry is an execution error.
    """

    def __init__(self, *, repause=(), fail=(), delay=0.0, **kwargs):
        super().__init__(policy_text="versao: stub-policy\n", **kwargs)
        self.repause, self.fail, self.delay = set(repause), set(fail), delay
        self.issued: dict[str, str] = {}  # tag -> token currently valid
        self.retries: list[dict] = []  # every tools/call that carried inputResponses
        self.bad_retries: list[str] = []
        self._count = 0

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.delay:
            await asyncio.sleep(self.delay)
        document = json.loads(await request.aread())
        if document["method"] != "tools/call":
            return await super()._handle(request)
        self.requests.append(document)
        params = document["params"]
        args = params["arguments"]
        tag = args["responsavel"].removeprefix("Conf")
        self._count += 1
        if "inputResponses" not in params:
            if not args["responsavel"].startswith("Conf"):
                result = reserved(args, args["sala"], self._count)
            else:
                token = f"TOKEN-{tag}-{self._count}"
                self.issued[tag] = token
                result = pause_result(token)
        else:
            self.retries.append(document)
            if params.get("requestState") != self.issued.get(tag) or list(params["inputResponses"]) != ["escolha_de_sala"]:
                self.bad_retries.append(tag)
            answer = params["inputResponses"]["escolha_de_sala"]
            if tag in self.fail:
                result = {"resultType": "complete", "isError": True,
                          "content": [{"type": "text", "text": f"falhou {tag}"}]}
            elif tag in self.repause:
                self.repause.discard(tag)
                token = f"TOKEN2-{tag}"
                self.issued[tag] = token
                result = pause_result(token, choices=("sala-z",))
            elif answer["action"] == "decline":
                result = {"resultType": "complete", "isError": False, "structuredContent": {
                    "reservado": False, "motivo": "recusado"}, "content": [{"type": "text", "text": "{}"}]}
            else:
                result = reserved(args, answer["content"]["sala"], self._count)
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": document["id"], "result": result})
