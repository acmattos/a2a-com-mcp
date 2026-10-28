# Decisões técnicas e evidências da Fase 0

Registro da prova de viabilidade (Gate técnico 0) executada em 28/10/2026.
Os probes foram descartáveis (fora do repositório); o que vale está abaixo.

## Versões fixadas

Python 3.12.6, `uv` 0.11.3. Dependências diretas com `==` em `pyproject.toml`, travadas em `uv.lock`:

| Pacote | Versão |
|---|---|
| `mcp` (inclui `mcp-types` 2.3.0 e `httpx2` 2.16.0) | 2.3.0 |
| `a2a-sdk[http-server]` | 1.3.0 |
| `starlette` | 1.10.0 |
| `uvicorn` | 0.54.0 |
| `httpx` (testes e cliente de apoio) | 0.28.1 |
| `pytest` (dev) | 9.2.0 |

## Decisão de stack: servidor MCP high-level (`MCPServer`)

O plano preferia o servidor low-level, condicionado à prova. A prova mostrou que o **high-level
atende todos os contratos** e é o caminho que gerou os exemplos de `exemplos/wire/` (a chave
`__main__:escolha_de_sala` e a mensagem de `-32021` citando o *resolver* vêm daqui). Portanto:

- `MCPServer(name="central-de-salas", version="1.0.0", request_state_security=RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600))`
- MRTR via resolver: `Annotated[ElicitationResult[Any], Resolve(fn)]`, onde `fn` devolve `Elicit(msg, Model)`.
  O enum é dinâmico: `create_model("Escolha", sala=(Literal[tuple(alternativas)], ...))`.
- `app.streamable_http_app(stateless_http=True, json_response=True)` servido com `uvicorn` em `/mcp`.
- `RequestStateBoundary` já vem acoplado pelo `MCPServer` (AES-256-GCM, TTL, audience = nome do servidor).

## Evidências do Gate 0 (itens 1–7)

| # | Item | Resultado |
|---|---|---|
| 1 | POST MCP com `_meta` 2026-07-28 chega ao SDK e responde JSON-RPC | OK, `Content-Type` JSON, sem redirect em `/mcp` |
| 2 | Tool devolve `InputRequiredResult` com form e `requestState` | OK, `resultType=input_required`, enum `["sala-fusca","sala-mirante"]` |
| 3 | Agente recebe o resultado cru, sem callback automático | OK com `client.session.call_tool(..., allow_input_required=True)`; o `elicitation_callback` (que levanta exceção) nunca foi chamado |
| 4 | Retry com novo id e token original conclui | OK; ids no wire 1, 2, 3 (incrementais), `Mcp-Method`/`Mcp-Name`/`MCP-Protocol-Version` corretos |
| 5 | Token adulterado → `-32602` | OK, HTTP 400, `Invalid or expired requestState` |
| 6 | Restart com mesmo segredo mantém o token válido | OK; controle negativo: outro segredo → `-32602` |
| 7 | A2A v1.0: `SendMessage`/`GetTask`, `supportedInterfaces` | OK (ver achados abaixo) |

Extras observados: argumentos adulterados no retry → `-32602` (o token é amarrado a nome da tool + digest dos
argumentos); `_meta` sem `protocolVersion` → `-32602` HTTP 400; sem capability de elicitation → `-32021`
com `data.requiredCapabilities` e HTTP 400; `decline` → `complete` sem `isError`; `SendMessage` em Task
terminal → erro `-32004`; `GetTask` desconhecido → erro `-32001`.

## Achados que afetam as próximas fases

1. **A2A exige `A2A-Version`; o validador não envia.** O SDK 1.3.0 interpreta header ausente como `0.3` e
   responde `-32009`. O validador e os exemplos só mandam `Content-Type` e `traceparent`. Solução pela API
   pública: `context_builder` de `create_jsonrpc_routes`, com uma subclasse de
   `DefaultServerCallContextBuilder` que faz `state["headers"].setdefault("a2a-version", "1.0")`.
   Não é uma reimplementação do protocolo; é um default na fronteira, para o contrato do starter.
2. **O cliente MCP anuncia `{"elicitation":{"form":{},"url":{}}}`** e ignora override via `meta`
   (capacidade fixa em `ClientSession._build_capabilities`). O enunciado pede exatamente
   `{"elicitation":{"form":{}}}`. **Pendente para a Fase 4**: avaliar um `http_client` (parâmetro público de
   `streamable_http_client`) com hook que normalize só essa chave, com teste de conformidade, ou documentar
   a limitação com evidência (regra do enunciado para limitações reais de SDK).
3. **O SDK MCP usa `httpx2`**, não `httpx`; interceptação de wire em testes deve mirar `httpx2.AsyncClient`.
4. **`GetTask` devolve a Task direto em `result`**, sem o envelope `task` que aparece em `SendMessage` e nos
   exemplos. O validador aceita as duas formas (`result.get("task") or result`).
5. **Histórico:** a mensagem de status da pausa não entra em `history` por padrão (o exemplo 08 mostra
   a mensagem do agente no histórico). Verificar na Fase 5 se `status.message` basta (o validador lê
   `status.message`) ou se vale incluí-la no histórico para o requisito "visível no histórico".
6. **ids de Task** são UUIDs gerados pelo SDK (os exemplos usam `task-...`); o contrato não exige formato.

## Comandos de subida (a partir da raiz)

```bash
uv sync --frozen
uv run python servidor-mcp/main.py    # MCP em :7301/mcp (requer REQUEST_STATE_SECRET)
uv run python agente/main.py          # agente A2A em :7300/a2a
uv run python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```
