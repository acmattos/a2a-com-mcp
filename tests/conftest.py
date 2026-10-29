import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
MCP_MAIN = ROOT / "servidor-mcp" / "main.py"

# "servidor-mcp" has a hyphen, so it is not importable as a package: expose it as a source root.
# The agent's modules get their own root. Module names must stay distinct between the two roots.
sys.path.insert(0, str(ROOT / "servidor-mcp"))
sys.path.insert(0, str(ROOT / "agente"))

PROTOCOL = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
ELICITATION_FORM = {"elicitation": {"form": {}}}
STARTUP_TIMEOUT = 30


class McpServerProcess:
    """The real MCP server in its own process, reached over real HTTP."""

    def __init__(self, workdir: Path, secret: str | None = None, port: int | None = None) -> None:
        self.secret = secret or secrets.token_hex(32)
        self._fixed_port = port
        self._workdir = workdir
        self._stderr_path = workdir / "stderr.log"
        self._process: subprocess.Popen | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "McpServerProcess":
        if self._fixed_port:
            self.port = self._fixed_port
        else:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.port = probe.getsockname()[1]
        env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": str(self.port),
               "REQUEST_STATE_SECRET": self.secret}
        with open(self._stderr_path, "wb") as stderr:
            # Started from an unrelated cwd on purpose: data paths must not depend on it.
            self._process = subprocess.Popen(
                [sys.executable, str(MCP_MAIN)], cwd=self._workdir, env=env, stdout=subprocess.DEVNULL, stderr=stderr
            )
        self._wait_until_listening()
        return self

    def _wait_until_listening(self) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"MCP server exited early:\n{self.stderr_text()}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.1)
        raise RuntimeError("MCP server did not start listening in time")

    def stderr_text(self) -> str:
        return self._stderr_path.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()


class McpHttpClient:
    """Raw JSON-RPC over HTTP, written like the grader's client: no SDK between test and wire."""

    def __init__(self, base_url: str) -> None:
        self.endpoint = base_url + "/mcp"
        self._counter = 0

    def call(self, method: str, params: dict | None = None, *, meta: dict | None | str = "default",
             headers: dict | None = None, request_id: object = None, mirrored_name: str | None = "auto"):
        params = dict(params or {})
        if meta == "default":
            meta = {VERSION_KEY: PROTOCOL, CAPABILITIES_KEY: ELICITATION_FORM}
        if meta is not None:
            params["_meta"] = meta
        self._counter += 1
        body = {"jsonrpc": "2.0", "id": request_id if request_id is not None else self._counter,
                "method": method, "params": params}
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL, "Mcp-Method": method}
        name = params.get("name") or params.get("uri") if mirrored_name == "auto" else mirrored_name
        if name:
            sent["Mcp-Name"] = name
        for key, value in (headers or {}).items():
            if value is None:
                sent.pop(key, None)
            else:
                sent[key] = value
        response = httpx.post(self.endpoint, json=body, headers=sent, timeout=30)
        return response.status_code, response.json(), response

    def tool(self, name: str, arguments: dict | None = None, **kwargs) -> dict:
        _, body, _ = self.call("tools/call", {"name": name, "arguments": arguments or {}}, **kwargs)
        return body

    def tool_result(self, name: str, arguments: dict | None = None, **kwargs) -> dict:
        return self.tool(name, arguments, **kwargs)["result"]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """One server per test module, for read-only tests."""
    process = McpServerProcess(tmp_path_factory.mktemp("mcp-shared")).start()
    yield process
    process.stop()


@pytest.fixture
def fresh_server(tmp_path):
    """A brand-new process (initial state) for tests that create reservations."""
    process = McpServerProcess(tmp_path).start()
    yield process
    process.stop()


@pytest.fixture(scope="module")
def mcp(server):
    return McpHttpClient(server.url)


@pytest.fixture
def fresh_mcp(fresh_server):
    return McpHttpClient(fresh_server.url)


AGENT_MAIN = ROOT / "agente" / "main.py"


class AgentProcess:
    """The real agent (A2A server + MCP host) in its own process, pointed at a real MCP server."""

    def __init__(self, workdir: Path, mcp_url: str) -> None:
        self._stderr_path = workdir / "agent-stderr.log"
        self._mcp_url = mcp_url
        self._workdir = workdir
        self._process: subprocess.Popen | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "AgentProcess":
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        env = {**os.environ, "AGENT_HOST": "127.0.0.1", "AGENT_PORT": str(self.port), "MCP_URL": self._mcp_url,
               "AGENT_PUBLIC_URL": f"http://localhost:{self.port}"}
        env.pop("REQUEST_STATE_SECRET", None)  # the agent never needs the server's secret
        with open(self._stderr_path, "wb") as stderr:
            self._process = subprocess.Popen(
                [sys.executable, str(AGENT_MAIN)], cwd=self._workdir, env=env, stdout=subprocess.DEVNULL, stderr=stderr
            )
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"agent exited early:\n{self.stderr_text()}")
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.1)
        raise RuntimeError("agent did not start listening in time")

    def stderr_text(self) -> str:
        return self._stderr_path.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()


class Stack:
    """MCP server + agent, both real processes. ``mcp_server`` exposes the server's stderr for trace evidence."""

    def __init__(self, mcp_server: McpServerProcess, agent: AgentProcess) -> None:
        self.mcp_server, self.agent = mcp_server, agent

    @property
    def rpc_url(self) -> str:
        return self.agent.url + "/a2a"

    @property
    def card_url(self) -> str:
        return self.agent.url + "/.well-known/agent-card.json"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """One MCP server and one agent for a whole test module. Tests use a distinct day each (see ``day``)."""
    mcp_server = McpServerProcess(tmp_path_factory.mktemp("mcp-stack")).start()
    agent = AgentProcess(tmp_path_factory.mktemp("agent-stack"), mcp_server.url + "/mcp").start()
    yield Stack(mcp_server, agent)
    agent.stop()
    mcp_server.stop()
