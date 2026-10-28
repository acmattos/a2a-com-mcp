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

## Fase 2 — servidor MCP básico

Arquitetura (dependências apontam para dentro; o domínio não conhece o MCP):

```
main.py ──► protocol.py (adaptador MCP) ──► service.py (casos de uso) ──► domain.py (regras puras)
   │                                                  └────────────────► repository.py (dados + memória)
   └──► telemetry.py (middleware ASGI de log em stderr)
```

- `service.py`: `BookingService` (listar, consultar, reservar). Valida, checa conflito e só então grava.
  Conflito levanta `BookingConflict`; erro de regra levanta `DomainError`. Sem tipos do MCP.
- `protocol.py`: traduz o serviço para tools/resource. As tools retornam `Annotated[CallToolResult, Modelo]`:
  o `outputSchema` vem do modelo e o erro de negócio sai como `CallToolResult(is_error=True)` com a mensagem
  **literal** (sem o prefixo `Error executing tool ...` que o SDK poria ao levantar `ToolError`).
  `structuredContent` e o bloco de texto saem do mesmo `model_dump`, logo são o mesmo objeto JSON.
- `telemetry.py`: middleware ASGI puro. Registra `mcp-request method=... id=... [name=...] traceparent=...`
  antes de o SDK validar, então requisições rejeitadas (sem `_meta`, header incoerente) também aparecem.
  Nunca registra argumentos, `requestState` ou corpo. Valores são escapados para não forjar linhas de log.
- `main.py`: `MCP_HOST` (padrão `127.0.0.1`) e `MCP_PORT` (padrão `7301`), `stateless_http=True`,
  `json_response=True`. `localhost` resolve e é aceito pela proteção de Host do SDK.
- O texto do resource é lido com `read_text`, que normaliza para LF. O arquivo do disco está em CRLF só pelo
  `core.autocrlf=true` do Windows; o blob do Git e o exemplo do starter usam LF.

### Comportamento provisório (SUBSTITUÍDO na Fase 3; mantido aqui como histórico)

`reservar_sala` em intervalo ocupado devolve erro de tool `Conflito de horario: a sala ja esta reservada nesse
intervalo` (`isError=true`) e **não grava nada**. Não há `InputRequiredResult`, elicitation nem `requestState`.
Por isso os checks 13–20 do validador falham nesta fase, como esperado.

### Limitação do SDK: `MCP-Protocol-Version` legado/ausente contra envelope moderno

- **API:** `mcp.shared.inbound.classify_inbound_request` (a escada de validação) e o roteamento de
  `mcp.server._streamable_http_modern`, `mcp` 2.3.0.
- **Evidência (reproduzível):** `tests/test_mcp_http.py::test_legacy_or_absent_version_header_against_a_modern_envelope_is_header_mismatch`
  (`xfail(strict=True)`). Um `tools/call` com `_meta.protocolVersion = 2026-07-28` e header
  `MCP-Protocol-Version: 2025-11-25` (ou sem o header) responde 200 em vez de `-32020`.
- **Causa:** o header escolhe o caminho; versão legada ou ausente vai para o transporte legado, que não roda a
  escada. Versão desconhecida (`1999-01-01`), `Mcp-Method` e `Mcp-Name` incoerentes já dão `-32020` + HTTP 400.
- **Alternativas consideradas:** (a) guard próprio no middleware que compare o header com o `_meta` — descartado,
  é reimplementar parte da validação do protocolo, o que o enunciado proíbe; (b) aceitar e documentar — adotado.
  O `xfail` estrito avisa quando uma versão futura do SDK corrigir.
- **Impacto no desafio:** nenhum check automatizado cobre isso; o agente sempre envia header e envelope coerentes.

## Fase 3 — MRTR com `requestState` selado

### Desenho

```
reservar_sala(sala, inicio, fim, responsavel, escolha=Resolve(escolha_de_sala))
   escolha_de_sala ─► service.alternatives_for ─► domain.find_alternatives   (regra só no domínio)
```

- **Resolver oficial do `MCPServer`** (`Resolve`/`Elicit`). O resolver devolve `Elicit` só quando o intervalo está
  ocupado **e** há alternativas. Nos outros casos (livre, entrada inválida, sem alternativa) devolve `None`: a
  pergunta não é feita e o corpo da tool responde, com as mensagens literais (`Sem alternativas disponiveis no
  intervalo` entre elas). O adaptador não calcula nada; só traduz.
- **Form**: um modelo dinâmico `create_model("EscolhaDeSala", sala=(Literal[ids], ...))` gera `enum` (ou `const`
  com uma só alternativa, aceito pelo enunciado). Texto: `A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.`
- **Consumidor**: `ElicitationResult[Any]` (a união), para distinguir `accept` de `decline`/`cancel`. `decline` e
  `cancel` retornam `complete`, `reservado=false`, `motivo="recusado"`, sem `isError` e sem gravar.
- `protocol.py` **não** usa `from __future__ import annotations`: o SDK avalia as anotações e
  `Resolve(escolha_de_sala)` aponta para um nome local.

### Selo do `requestState` (o que é do SDK e o que é nosso)

| Garantia | Quem entrega | Evidência |
|---|---|---|
| Integridade e confidencialidade | SDK: AES-256-GCM, chave derivada por HKDF | token adulterado em 9 formas ⇒ `-32602` |
| Expiração | SDK: `RequestStateSecurity(ttl=600)` | `tests/test_mrtr_expiry.py`: válido em TTL−5 s, recusado em TTL+5 s |
| Vínculo com o pedido | SDK: nome da tool + digest dos argumentos | argumento trocado, token de outro pedido, outra tool ⇒ `-32602` |
| Vínculo com o serviço | SDK: `audience` = nome do servidor | token de outro deployment (outro segredo) ⇒ `-32602` |
| Resposta vale só para a pergunta vista | SDK: digest da pergunta no estado | se as alternativas mudarem, ele **pergunta de novo** (novo token) |
| Segredo só do ambiente, ≥ 32 bytes | nós: `config.py` | sem segredo/curto ⇒ o processo nem sobe (exit 2); nunca ecoado |
| Sem chave efêmera nem sessão | nós: `build_server` exige `RequestStateSecurity` | mutante com chave efêmera derruba o teste de restart |

Política do segredo: valor hexadecimal conta pelos bytes que codifica (`token_hex(32)` = 64 caracteres = 32 bytes;
32 caracteres hex são só 16 bytes e são recusados); qualquer outro valor conta pelo tamanho em UTF-8. Entropia
real não é mensurável, então a exigência é de tamanho.

O corpo do `requestState` não é aberto pelo nosso código em nenhum ponto; ele só passa pelo SDK.

### Comportamento observado (resposta real do servidor)

| Entrada | Resposta |
|---|---|
| token adulterado / truncado / lixo | `-32602` `Invalid or expired requestState`, HTTP 400 |
| argumento alterado no retry | `-32602` (vínculo com o pedido) |
| mesmo token em outra tool | `-32602` |
| escolha fora do enum, sala original, `accept` sem `content`, `sala` não-string | `complete` + `isError=true` (erro do resolver); nada é gravado |
| `action` inválida, entrada que não é objeto, `inputResponses` que não é mapa | `-32602 Invalid request parameters`, HTTP 400 |
| chave errada em `inputResponses`, ou nenhuma resposta | pergunta de novo (`input_required`); nada é gravado |
| replay de um `accept` já consumido | pergunta de novo, com as alternativas atuais; **sem reserva duplicada** |
| cliente sem `elicitation.form` e há alternativas | `-32021`, `data.requiredCapabilities`, HTTP 400 |
| cliente sem `elicitation.form` e não há alternativas | `isError` `Sem alternativas...` (nenhuma pergunta é necessária) |

### Pontos de atenção para as próximas fases

1. **Pergunta que muda entre rodadas.** O estado sela o digest da pergunta. Se outra reserva ocupar uma das
   alternativas antes da resposta, o retry devolve `input_required` outra vez (novo token e novo enum). O agente
   (Fases 4 e 6) precisa tratar uma segunda pausa na mesma Task, não assumir que o retry conclui.
2. **Chave de `inputRequests`** = `protocol:build_server.<locals>.escolha_de_sala`. É determinística, atribuída pelo
   servidor e estável entre restarts; o agente só a ecoa. Não é formato fixo do enunciado.
3. **`{"elicitation": {}}`** (sem `form` nem `url`) é tratado pelo SDK como suporte a form (compatibilidade com
   a forma anterior aos modos). `{}` e `{"elicitation": {"url": {}}}` dão `-32021`.
4. **Reservas não persistem e o estado selado sim.** Se o conflito era com uma reserva criada em memória e o
   processo reiniciou, o conflito deixou de existir: a pergunta não é refeita e o retry reserva a sala
   **originalmente pedida** (agora livre). Com as reservas do starter (`res-0001`/`res-0002`), que são relidas no
   boot, o fluxo do avaliador (passo 12) conclui na sala escolhida. É consequência direta de "reservas só em memória".
5. A limitação do SDK sobre o header `MCP-Protocol-Version` (Fase 2) continua válida e inalterada
   (`xfail(strict=True)`); não é regressão desta fase.
