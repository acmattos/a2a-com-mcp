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

## Fase 4 — agente como host MCP

### Módulos (`agente/`)

| Módulo | Papel |
|---|---|
| `mcp_host.py` | `McpHost`: cliente MCP real (SDK `mcp` 2.3.0) sobre Streamable HTTP. Interface interna: `book()` e `resume()` devolvem `Reserved \| Declined \| ToolFailed \| InputRequired`; falhas viram `McpUnavailable`, `McpProtocolError` ou `ContractError`. |
| `http_boundary.py` | Dois transportes `httpx2` na fronteira HTTP (ver abaixo). |
| `tracing.py` | `traceparent` W3C: mantém o trace-id, gera span-id novo por requisição. Funções puras, sem estado global. |

O agente não importa nada de `servidor-mcp/` e não contém sala, horário nem valor de política (dois testes
estáticos garantem as duas coisas).

### Fluxo de uma reserva

`tools/list` (e confere o que foi descoberto) → `resources/read politica://uso` (extrai `versao:`) → `tools/call`.
A descoberta é por reserva, e não só no boot, para que as três requisições levem o trace-id da Task. Ela tem efeito:
sem a tool `reservar_sala`, ou com um argumento obrigatório que o agente não fornece, o host levanta `ContractError`
e **nenhum** `tools/call` é enviado. O `resume` repete só o `tools/call`.

Se o servidor responde `input_required`, o host devolve `InputRequired` (chave, `inputRequests` e `requestState`
exatamente como vieram; `choices` lida do `enum`/`const`). Ele nunca responde à elicitation: a chamada é feita com
`client.session.call_tool(..., allow_input_required=True)`, e o `elicitation_callback` declarado (só para anunciar a
capability) levanta erro se for invocado (um teste prova que não é). `request_state` fica fora de `repr`/`str`.
O host só aceita o formato que espera (uma pergunta, `form`, propriedade `sala` com `enum`/`const`); qualquer outro
formato é `ContractError`, sem adivinhar.

### Achado 1: a capability anunciada pelo SDK não é a do desafio

`mcp` 2.3.0 monta o anúncio em `ClientSession._build_capabilities` e, havendo `elicitation_callback`, sempre emite
`{"elicitation": {"form": {}, "url": {}}}`. O carimbo de cada requisição (`_make_modern_stamp`) sobrescreve
`clientCapabilities`, então passar o valor em `meta` não adianta (testado na Fase 0). O enunciado pede exatamente
`{"elicitation": {"form": {}}}`, e anunciar `url` convidaria elicitações que o agente não trata.

- **API pública usada:** o parâmetro `http_client` de `streamable_http_client`.
- **Solução:** `FormOnlyElicitationTransport` envolve o transporte real e remove só a chave `elicitation.url` do
  corpo; o resto da mensagem (inclusive o `requestState`) segue intacto. Se nada muda, a mensagem passa idêntica.
- **Evidência no wire:** `tests/test_http_boundary.py` (transporte isolado) e
  `test_every_request_carries_complete_meta_and_coherent_mirrored_headers` (todas as requisições, vistas no último
  salto antes do socket, anunciam `{"elicitation":{"form":{}}}`). Sem o adaptador o mesmo teste falha.
- **Descartado:** subclassear `Client`/`ClientSession` (API privada) e aceitar `{form, url}` (anuncia um modo que não
  tratamos).

### Achado 2: uma falha de conexão mata o cliente do SDK

Com o servidor fora do ar, a falha sobe como `ExceptionGroup(ConnectError)` e o `Client` fica inutilizável para sempre
(toda chamada seguinte dá `CancelledError`, mesmo com o servidor de volta). A alternativa óbvia, um `Client` novo por
operação, reinicia o contador de ids JSON-RPC (`JSONRPCDispatcher._next_id`, sem API pública para alterá-lo) e o retry
poderia reutilizar o id de uma chamada anterior, o que o enunciado proíbe.

- **Solução:** `UnreachableAsErrorTransport` transforma `httpx2.TransportError` em uma resposta HTTP 503 com erro
  JSON-RPC (`-32000`, `data.origin = "agent-http-boundary"`, `data.reason = <classe da exceção>`). A requisição falha
  sozinha, o `Client` sobrevive e um único cliente longevo mantém os ids únicos. O host converte o erro em
  `McpUnavailable` (sem URL nem detalhes internos).
- **Evidência:** `test_the_same_host_recovers_by_itself_when_the_server_comes_back` (queda e volta do processo real,
  ids únicos durante a falha) e `test_every_jsonrpc_id_on_the_wire_is_unique_across_the_whole_session`.
- **Custo:** a conexão do `McpHost` precisa de ciclo de vida (`async with`); na Fase 5 entra no `lifespan` do ASGI.

### Demonstração

`uv run python scripts/demo_mcp_host.py` percorre reserva livre, sala inexistente, pausa + retomada e pausa +
recusa. No stderr do servidor: `tools/list` antes do primeiro `tools/call`, ids 1..N únicos e crescentes, a chamada
pausada e o retry com ids diferentes, o mesmo trace-id em todas as linhas e um span novo em cada uma.

### Pontos de atenção para as próximas fases

1. **Colisão de nomes de módulo nos testes.** `tests/conftest.py` põe `servidor-mcp/` e `agente/` no `sys.path`. Na
   Fase 5, `agente/main.py` colidiria com `servidor-mcp/main.py` (`import main`). Os testes do agente devem carregar
   o app por caminho (`importlib`) ou o módulo deve ter outro nome.
2. **`InputRequired.policy_version`** guarda a versão lida na reserva inicial; o `resume` não relê o resource. A
   Fase 6 pode guardá-la no registro privado da Task junto com o `InputRequired`.
3. **Segunda pausa.** `resume()` também pode devolver `InputRequired` (pergunta que mudou, ver Fase 3). A ponte deve
   tratá-lo como uma nova pausa da mesma Task.
4. **Sem `cancel`.** O host só expõe `accept` e `decline`; é tudo que o A2A (`escolha=recusar`) precisa.
5. O `terminate_on_close` é desligado: o transporte é stateless e não há sessão a encerrar.

## Fase 5 — Servidor A2A v1.0 e ciclo básico de Task

### Módulos (`agente/`)

| Módulo | Papel |
|---|---|
| `main.py` | Composição: `create_app(mcp_url, public_url, host_factory)`, `lifespan`, rotas do SDK, `python agente/main.py` na 7300 |
| `executor.py` | `RoomBookingExecutor(AgentExecutor)`: parser → `McpHost` → estados da Task |
| `command.py` | Parser determinístico de `reservar sala=… inicio=… fim=… responsavel=…` |
| `card.py` | Agent Card (modelos protobuf do SDK, serializados em ProtoJSON pela rota do SDK) |
| `ids.py` | Ids legíveis `task-`/`ctx-`/`msg-`/`art-` + 12 hex |

Nada do servidor MCP é importado (guarda estática da Fase 4 continua valendo) e nenhuma regra de sala/política vive no agente.

### Máquina de estados (nova Task)

```
Task(SUBMITTED) ─► WORKING ─┬─► COMPLETED  (reserva feita; artifact "reserva")
                            ├─► FAILED     (isError da tool; servidor indisponível; resposta inesperada)
                            └─► REJECTED   (texto que não é o comando)
```

- O evento `Task` sai primeiro; só depois os `TaskStatusUpdateEvent`/artifact (o SDK recusa o contrário).
- Terminal é definitivo: o SDK recusa `SendMessage` com `taskId` terminal (`-32004`), desconhecido (`-32001`) ou
  `contextId` divergente (`-32602`). O que faltava: `TaskAwareContextBuilder` recusa `taskId` de Task ainda
  `SUBMITTED`/`WORKING` (`-32004`, "em andamento"); só uma Task pausada (Fase 6) poderá receber continuação.
- Comando inválido = `REJECTED` com texto estável (`Comando invalido: <motivo>. Formato: …`), não erro JSON-RPC: a Task existe,
  tem histórico e `GetTask` a encontra. FAILED fica para falha de execução.

### Decisões

1. **`A2A-Version` ausente = 1.0.** O SDK trata header ausente como 0.3 e recusaria o validador (que não envia o
   header). `V1CallContextBuilder` aplica `setdefault("a2a-version", "1.0")`; um `A2A-Version: 0.3` explícito continua
   recusado (`-32009`) e `message/send`, `tasks/get` (nomes 0.3) não existem (`-32601`). Nenhum modelo 0.3 é usado.
2. **Mensagem final no histórico.** O SDK só move `status.message` para `history` quando o *próximo* status a
   substitui; uma mensagem enviada uma vez no estado final ficaria fora do histórico (e os exemplos `08`/`09` têm a
   mensagem do agente em ambos). `_finish` publica a mesma `Message` (mesmo `messageId`) num `WORKING` e depois no estado
   final: `history=[usuário, agente]`, `status.message=agente`. Custo: durante milissegundos `GetTask` pode ver `WORKING`
   com o texto final. Na Fase 6 vale o mesmo para `INPUT_REQUIRED`.
3. **Versão da política** no artifact vem do `Reserved.policy_version` (resource lido), sobrescrevendo o campo
   `politica` que a tool devolveu. Teste com stub: resource `2031-05-17`, tool `outra` → artifact `2031-05-17`.
4. **Ciclo de vida:** `create_app` só constrói; o `lifespan` faz `async with host`. O `McpHost` é de uso único
   (o `httpx2.AsyncClient` não reabre), o que combina com um lifespan por processo. Um só cliente MCP atende todas as Tasks.
5. **Trace sem estado compartilhado:** `traceparent` é lido do `ServerCallContext` de *cada* requisição e passado
   por argumento até `McpHost`. O executor não guarda nada por Task. Mutante com variável global é detectado.
6. **Falhas MCP** viram `FAILED` com texto fixo, sem URL, porta, token ou mensagem interna
   (`indisponivel` / `codigo -32602` / `resposta inesperada`). O mesmo host se recupera quando o servidor volta.
7. **Ponto de extensão da Fase 6:** `_conclude` e `_continue` (na Fase 5 um conflito virava `FAILED`). A ponte entra nesses dois
   métodos; handlers, card e lifespan não mudam.
8. **IDs:** `task-…`/`ctx-…`/`msg-…`/`art-…`, como nos exemplos, por `IDGenerator` do SDK.
9. **Ruído do SDK:** com `break` antecipado em `on_message_send`, o a2a-sdk 1.3.0 deixa um gerador aberto que o GC fecha
   fora do contexto: OpenTelemetry loga "Failed to detach context" e o asyncio loga `EventQueueSource._dispatch_loop`
   abandonado. Medido: nenhuma tarefa sobrevive ao GC, respostas e Tasks corretas. `silence_known_sdk_noise()`
   descarta só essas duas mensagens (o resto desses loggers passa).

### Compatibilidade ProtoJSON (achados)

- Card idêntico ao `exemplos/wire/07` exceto pelo host da URL (teste compara o JSON inteiro).
- `SendMessage` devolve `result.task`. `GetTask` devolve a `Task` **direto** em `result` (o SDK 1.3.0 segue a especificação
  v1.0; o exemplo `09` mostra `result.task`). O validador aceita as duas formas (`tarefa_de`). Mantido o do SDK.
- Enums como string (`TASK_STATE_*`, `ROLE_*`), camelCase, `parts[].text`, `timestamp` RFC 3339.

### Validador original contra os processos reais

29/36: **21–26, 31, 35 passam** (alvo da fase) e 01–20 e 34. Falham 27–30, 32, 33 e 36, todos dependem de pausa
(Fase 6). O 34 passa sem ter o que vazar ainda.

### Pendências para a Fase 6 (resolvidas: ver a seção da Fase 6)

1. Registro privado por `taskId` (`InputRequired` inteiro, trace inicial, política); nunca em Task/history/metadata/log.
2. `_conclude`: `InputRequired` → `INPUT_REQUIRED` com `alternativas: a, b` na ordem do `enum`.
3. `_continue`: aceitar só Task pausada, `escolha=<id>`/`recusar`, inválida sem chamar o MCP; segunda pausa possível.
4. `Declined` → `CANCELED` sem artifact (hoje o ramo é defensivo).
5. Continuação sem `traceparent` herda o da Task original.


## Fase 6 — A ponte A2A ↔ MCP (MRTR)

### Onde a ponte acontece

- **Pausa:** `executor.py::_conclude`, ramo `InputRequired`. O `InputRequired` inteiro vai para o `PendingStore` (por
  `taskId`), *depois* a Task é anunciada como `INPUT_REQUIRED` com `alternativas: a, b` (ordem do `enum`).
- **Retomada:** `executor.py::_continue` → `McpHost.resume(entry.pause, answer, traceparent)`. É o único ponto em que o
  `requestState` volta ao servidor, intacto, com a chave de `inputRequests`, os argumentos originais e id JSON-RPC novo
  (o cliente MCP longevo da Fase 4 garante a unicidade).

### Máquina de estados

```
(nova)  SUBMITTED ─► WORKING ─┬─► COMPLETED   reserva feita (artifact "reserva")
                              ├─► FAILED      isError / indisponível / inesperado
                              ├─► REJECTED    texto que não é o comando
                              └─► INPUT_REQUIRED ◄─────────────────────────┐
                                     │ escolha=<id do enum>   ► WORKING ─► COMPLETED | FAILED | INPUT_REQUIRED (2ª pausa)
                                     │ escolha=recusar        ► WORKING ─► CANCELED (sem artifact)
                                     └ qualquer outra coisa   ► INPUT_REQUIRED, mesma lista, MCP não é chamado ─┘
```

### Estado privado (`agente/pending.py`)

`PendingStore`: `dict[taskId → PendingTask(pause: InputRequired, traceparent)]`, em memória, uma instância por
`create_app`. Métodos síncronos (atômicos sob asyncio): `put`, `get`, `claim` (retira e devolve: só um chamador
recebe), `discard`. `repr` nunca mostra estado (`InputRequired.request_state` tem `repr=False`). O agente não
decodifica nem compara o `requestState`; só o guarda e o repete. As alternativas válidas são exclusivamente o `enum`
recebido (`pause.choices`): nenhuma regra de sala no agente. Descarte: `claim` retira a entrada antes do retry, então
ela some em COMPLETED/CANCELED/FAILED e é substituída numa segunda pausa.

### Decisões

1. **Mensagem de pausa enviada uma vez** (`_ask`). O SDK move `status.message` para o `history` quando a Task é
   continuada; enviar a mensagem duas vezes (como `_finish` faz nos estados terminais) a duplicaria no histórico. Assim
   o histórico final é `[pedido, alternativas, escolha, resultado]`, igual ao exemplo `10`. Durante a pausa o
   `history` de `GetTask` tem só o pedido e a pergunta está em `status.message` (o exemplo `09` repete a pergunta no
   histórico; mantive a semântica do SDK, e o validador lê `status.message`).
2. **`contextId` da continuação.** A continuação traz só `taskId`; sem correção o SDK gera um `contextId` novo para a
   mensagem e a Task passa a ter dois (achado ao testar: a 2ª requisição de uma Task falhava com "Context in event
   doesn't match"). `TaskAwareContextBuilder` repassa o `contextId` guardado.
3. **Duas continuações simultâneas da mesma Task.** Três camadas: (a) `OneAtATimePerTask` serializa as mensagens
   do mesmo `taskId` (lock por Task, fraco): a segunda só é avaliada depois da primeira e recebe a recusa padrão do SDK
   (`-32004`, Task terminal); sem isso o SDK a enfileirava e devolvia a Task concluída por outra resposta; (b)
   o builder recusa Task `SUBMITTED/WORKING` (caso `returnImmediately`); (c) `claim` garante que só uma resposta
   obtém a pausa. Medido: 3 e 8 respostas simultâneas geram exatamente 1 `tools/call` de retry.
4. **Recusar uma continuação não pode matar a Task.** Uma exceção dentro do `execute` faz o SDK marcar a Task `FAILED`.
   Por isso o caso "Task pausada sem entrada privada" é recusado no builder (antes do executor).
5. **Trace da Task.** A entrada guarda o `traceparent` do pedido inicial. A continuação usa sempre esse; um
   `traceparent` diferente na continuação não move a Task de trace; uma Task que começou sem trace adota o da
   continuação. `McpHost` gera um span novo por requisição.
6. **Falha MCP no retry** (servidor fora do ar, `-32602` por TTL do token expirado) encerra a Task em `FAILED` e
   descarta a entrada. Não há retomada automática depois de uma queda: o usuário refaz o pedido (nada foi reservado).
7. **Cancelamento:** texto fixo `Reserva cancelada: pedido recusado.`, sem artifact.
8. **Logs:** a pilha de logs do SDK MCP registra, em DEBUG, cada requisição enviada, inclusive o `requestState` do retry.
   `install_log_filters()` descarta essas linhas (`mcp.client.streamable_http` contendo `requestState`). Teste com
   DEBUG ligado.
9. **Testes que carregam `main`:** `agente/main.py` e `servidor-mcp/main.py` coexistem; os testes os carregam por caminho
   (`tests/agent_app.py`, `tests/server_app.py`).

### Limitações e riscos

- O `PendingStore` é só memória: reiniciar o agente perde as pausas (a Task também, pois o `InMemoryTaskStore` é a
  mesma vida). O `requestState` em si sobreviveria (servidor sem estado), mas o agente não o teria mais.
- Pausas abandonadas ficam na memória até o fim do processo (não há expiração; o token do servidor expira em 600 s e o
  retry tardio vira `FAILED`).
- Uma queda do servidor MCP durante o retry cancela a oportunidade de escolher de novo (ver decisão 6).
- `cancel` (CancelTask) continua não suportado; a skill não o anuncia.
