"""Runs the ORIGINAL, unmodified validator against a fresh server and asserts checks 01-20 (the MCP half).

Checks 21-36 need the A2A agent (later phases). The agent URL points to a closed port: the validator
stops at the first A2A call, after printing every MCP verdict.
"""

import re
import subprocess
import sys

import pytest

from conftest import ROOT

VALIDATOR = ROOT / "validador" / "validar.py"
CHECKS = {
    1: "tools/list traz as tres tools",
    2: "toda tool tem inputSchema de objeto",
    3: "listar_salas devolve structuredContent e o mesmo JSON em texto",
    4: "_meta sem protocolVersion devolve -32602 e HTTP 400",
    5: "_meta sem clientCapabilities devolve -32602 e HTTP 400",
    6: "tool inexistente e recusada, por -32602 ou por isError",
    7: "resources/read de politica://uso devolve a politica",
    8: "resources/read de URI inexistente devolve -32602",
    9: "sala inexistente devolve isError com a mensagem exata",
    10: "fora da janela devolve isError com a mensagem exata",
    11: "duracao acima de 2h devolve isError com a mensagem exata",
    12: "intervalo invertido devolve isError com a mensagem exata",
    13: "conflito devolve input_required com inputRequests e requestState",
    14: "a elicitation e form mode e oferece as alternativas na ordem certa",
    15: "conflito sem a capability elicitation devolve -32021 e HTTP 400",
    16: "retry com inputResponses e requestState conclui a reserva",
    17: "requestState adulterado e rejeitado com -32602",
    18: "argumentos adulterados no retry nao tomam efeito",
    19: "recusa conclui sem reservar e sem isError",
    20: "conflito sem alternativa possivel devolve isError com a mensagem exata",
}


@pytest.fixture(scope="module")
def validator_lines(server):
    completed = subprocess.run(
        [sys.executable, str(VALIDATOR), "--agente", "http://127.0.0.1:1", "--mcp", server.url],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    return {int(m.group(2)): (m.group(1), m.group(3)) for m in re.finditer(
        r"^(PASS|FAIL) (\d\d) (.*)$", completed.stdout, re.MULTILINE)}


@pytest.mark.parametrize("number", sorted(CHECKS))
def test_original_validator_check_passes(validator_lines, number):
    verdict, description = validator_lines[number]
    assert verdict == "PASS", f"check {number:02d} {description}"
    assert description.startswith(CHECKS[number])
