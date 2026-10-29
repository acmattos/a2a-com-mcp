# A Ponte: um agente A2A com MCP por dentro

Dois processos independentes, sem LLM, para a Hill Valley Tech reservar salas de reunião:

| Processo | Pasta | Porta | Papel |
|---|---|---|---|
| Servidor MCP | `servidor-mcp/` | `7301` (`POST /mcp`) | tools, resource e MRTR com `requestState` selado (spec MCP `2026-07-28`, Streamable HTTP *stateless*) |
| Agente | `agente/` | `7300` (`POST /a2a`, card em `/.well-known/agent-card.json`) | servidor A2A v1.0 por fora, host MCP por dentro |

O enunciado original do desafio está no repositório base (<https://github.com/devfullcycle/desafio-a2a-com-mcp>); este README o substitui pela documentação da entrega. `dados/`, `exemplos/` e `validador/` são os do starter, sem alteração.

## Como rodar

### Pré-requisitos

- **Python 3.12** (testado com 3.12.6) e **[uv](https://docs.astral.sh/uv/)** (testado com 0.11.3). Não é preciso criar nem ativar `venv`: o `uv` cria a `.venv` do projeto.
- Portas `7300` e `7301` livres.
- O validador usa só a biblioteca padrão e roda com o mesmo Python do `uv run`.

### 1. Instalar as dependências travadas

```bash
git clone https://github.com/acmattos/a2a-com-mcp.git && cd a2a-com-mcp
uv sync --frozen
```

`--frozen` instala exatamente o `uv.lock` (nunca o altera). Versões diretas, fixadas com `==` em `pyproject.toml`:

| Pacote | Versão |
|---|---|
| `mcp` | 2.3.0 |
| `a2a-sdk[http-server]` | 1.3.0 |
| `starlette` | 1.10.0 |
| `uvicorn` | 0.54.0 |
| `httpx` | 0.28.1 |
| `pytest` (grupo `dev`) | 9.2.0 |

### 2. Gerar o segredo do `requestState`

O servidor MCP **não sobe sem** `REQUEST_STATE_SECRET` (nenhum valor padrão existe) e recusa segredos com menos de 32 bytes. `secrets.token_hex(32)` imprime 64 caracteres hexadecimais = 32 bytes de aleatoriedade. O segredo só é lido do ambiente: nunca é gravado em arquivo do projeto nem em log.

**Linux / macOS (bash/zsh):**

```bash
export REQUEST_STATE_SECRET="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')"
```

**Windows (PowerShell):**

```powershell
$env:REQUEST_STATE_SECRET = (uv run python -c "import secrets; print(secrets.token_hex(32))")
```

A variável vale para o terminal em que foi definida. **Reinicie o servidor MCP com o mesmo segredo** quando quiser provar que um `requestState` sobrevive à reinicialização (passo 12 do roteiro do avaliador): com outro segredo, o estado é corretamente rejeitado com `-32602`.

### 3. Subir os dois processos (um terminal cada, a partir da raiz)

Suba o MCP **primeiro** e deixe o stderr dele visível. O agente se conecta ao MCP ao subir.

Terminal 1, servidor MCP (porta 7301):

```bash
uv run python servidor-mcp/main.py
```

Terminal 2, agente A2A (porta 7300; o segredo não é necessário aqui):

```bash
uv run python agente/main.py
```

Variáveis opcionais (valores padrão entre parênteses): `MCP_HOST` (`127.0.0.1`), `MCP_PORT` (`7301`), `AGENT_HOST` (`127.0.0.1`), `AGENT_PORT` (`7300`), `MCP_URL` (`http://localhost:7301/mcp`, usada pelo agente), `AGENT_PUBLIC_URL` (`http://localhost:<AGENT_PORT>`, URL publicada no Agent Card).

Conferir que o agente está no ar:

```bash
curl http://localhost:7300/.well-known/agent-card.json
```

### 4. Validador oficial

Com os dois processos **recém-iniciados** (terminal 3):

```bash
uv run python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

O validador imprime na primeira linha o `trace-id` que usa; procure-o no stderr do servidor MCP (linhas `mcp-request ... traceparent=00-<trace-id>-...`). Termina com exit code 0 somente se as 36 verificações passarem.

### 5. Testes automatizados

```bash
uv run --frozen pytest -q
```

A suíte sobe processos reais (MCP e agente) e inclui os testes de MRTR após reinicialização do MCP (`tests/test_mrtr_http.py`, `tests/test_bridge_http.py`). Ela leva alguns minutos; cada teste usa portas livres escolhidas na hora e segredos próprios, então pode rodar com os processos do passo 3 no ar.

### Resetar o estado das reservas

As reservas ficam só em memória. Para voltar ao estado inicial (`dados/reservas.json`), reinicie o processo do servidor MCP (`Ctrl+C` e suba de novo). Como o validador cria reservas, **reinicie o MCP (e o agente) antes de cada execução dele**; duas execuções seguidas sem reiniciar dão falsos negativos.

### Parar os processos

`Ctrl+C` em cada terminal. Em Windows, se algum processo ficar órfão: `Get-NetTCPConnection -LocalPort 7300,7301 | Select OwningProcess` e `Stop-Process -Id <pid>`. Em Linux/macOS: `lsof -ti :7300 :7301 | xargs kill`.

## Onde a ponte acontece

A ponte é a costura entre o `InputRequiredResult` do MCP e a Task do A2A, inteira em `agente/executor.py` (`RoomBookingExecutor`) e `agente/mcp_host.py` (`McpHost`).

1. **Recepção do `InputRequiredResult`.** `McpHost._call` (`agente/mcp_host.py:215`) chama `session.call_tool(..., allow_input_required=True)`, que devolve o `InputRequiredResult` cru em vez de resolver a elicitation sozinho. `_outcome` (`:265`) o reconhece e `_pause` (`:280`) o converte em `InputRequired`: guarda a chave da pergunta, as alternativas (na ordem do `enum`) e o `requestState` (`:294`), validando que há exatamente uma pergunta *form mode*.
2. **Task → `TASK_STATE_INPUT_REQUIRED`.** `RoomBookingExecutor._start` (`agente/executor.py:83`) chama `host.book` (`:98`); o resultado vai para `_conclude` (`:133`). No ramo `InputRequired` (`:143`), `_ask` (`:173`, `updater.requires_input` em `:179`) pausa a Task com a mensagem `alternativas: <id>, <id>`, a única coisa que o cliente A2A vê.
3. **Estado pendente por `taskId`.** Antes de pausar, `_conclude` grava `PendingTask(pause, traceparent)` em `PendingStore.put(task_id, ...)` (`:146`; `agente/pending.py`). É um dicionário em memória do agente, uma entrada por Task. O `requestState` nunca entra em Task, mensagem, artifact, metadata, card ou log.
4. **Continuação.** O `SendMessage` com `taskId` e `escolha=<valor>` cai em `_continue` (`:106`), que lê a entrada do `taskId`. `_answer_to` (`:158`) traduz a escolha: `recusar` vira `decline`, um id oferecido vira `accept`, qualquer outra coisa não é resposta e a Task fica pausada, repetindo a lista (`:113-115`, sem falar com o MCP). `PendingStore.claim` (`:118`) remove a entrada ao devolvê-la, de modo que de duas respostas simultâneas só uma assume a pausa.
5. **`requestState` volta ao MCP, sem alteração.** `McpHost.resume` (`agente/mcp_host.py:181`) repete a mesma `tools/call` com os mesmos argumentos, `inputResponses = {chave original: resposta}` e o `requestState` exatamente como veio (`:228`).
6. **Id JSON-RPC novo no retry.** O retry é um novo `call_tool` na mesma sessão do cliente MCP, e o SDK atribui a cada request um id JSON-RPC novo e crescente. O stderr do MCP mostra os dois `tools/call` com ids diferentes.
7. **Desfechos** (`_conclude` e `_continue`):
   - sucesso (`Reserved`): artifact `reserva` com o campo `politica` e Task `COMPLETED`;
   - recusa (`Declined`, `escolha=recusar`): Task `CANCELED`;
   - erro de regra (`ToolFailed`, ex.: sala inexistente): Task `FAILED` com a mensagem da tool;
   - erro de protocolo ou MCP indisponível (`McpHostError`): Task `FAILED` com mensagem genérica, sem detalhes internos;
   - **segunda pausa**: se o retry volta a pedir informação (a alternativa escolhida foi tomada nesse meio-tempo), `_conclude` grava uma nova entrada, a Task volta a `INPUT_REQUIRED` com a nova lista e o novo `requestState`;
   - Task terminal ou desconhecida: o SDK recusa (`-32004`); uma Task pausada cuja entrada sumiu é recusada em `TaskAwareContextBuilder` (`agente/main.py`) sem virar `FAILED`.

Do lado do servidor, o MRTR está em `servidor-mcp/protocol.py`: `escolha_de_sala` (`:152`) é o *resolver* que pergunta as alternativas; `reservar_sala` (`:200`) conclui a reserva.

**Tracing.** O `traceparent` do cliente A2A (header HTTP) é lido em `executor._traceparent` (`:195`), guardado na `PendingTask` (a Task mantém o trace com que nasceu) e propagado em `_meta.traceparent` de **todas** as chamadas MCP (`tools/list`, `resources/read`, `tools/call` e o retry) por `mcp_host._meta` (`:239`), com o mesmo `trace-id` e um `span-id` novo a cada salto (`agente/tracing.py`). `servidor-mcp/telemetry.py` registra no stderr uma linha por request: `mcp-request method=... id=... traceparent=...`. Argumentos e `requestState` nunca são logados.

## Decisões técnicas

O registro completo, com evidências, está em [`docs/decisoes.md`](docs/decisoes.md). O resumo:

- **Dois processos independentes.** O agente só conhece o servidor por HTTP e pelo contrato MCP (descobre a tool com `tools/list`); não importa código do domínio. O servidor MCP não sabe nada de A2A.
- **SDKs.** Servidor: SDK oficial `mcp` 2.3.0 (`MCPServer` high-level, resolvers `Elicit`/`Resolve` para o MRTR, `RequestStateSecurity`). Agente: `a2a-sdk` 1.3.0 (`DefaultRequestHandler`, `TaskUpdater`, `InMemoryTaskStore`) e o cliente do `mcp` 2.3.0. `starlette` e `uvicorn` como camada HTTP. Sem LLM, sem ORM, sem banco.
- **Segurança e expiração do `requestState`.** Usa o mecanismo oficial do SDK, `RequestStateSecurity(keys=[segredo], ttl=600)`: o estado é cifrado e autenticado (AES-GCM), vinculado à tool, aos argumentos e à audiência, e expira em **600 s (10 minutos)**, o padrão do SDK e dentro da faixa de 5 a 30 min da spec. Estado adulterado, expirado ou com argumentos alterados é rejeitado com `-32602`. O servidor não guarda sessão: com o mesmo segredo, um `requestState` emitido antes de uma reinicialização continua válido. Sem `REQUEST_STATE_SECRET` de 32+ bytes o processo não sobe.
- **Persistência em memória.** Reservas (servidor) e Tasks/estados pendentes (agente) vivem em memória, por escolha do enunciado (sem banco). Reiniciar o MCP volta às reservas do starter. Reiniciar o agente perde as Tasks e as pausas, e uma Task pausada deixa de poder ser continuada.
- **Retomada após reinicialização do MCP.** Como as reservas somem no restart, a lista de alternativas pode mudar entre a pergunta e o retry, e o SDK, por segurança, descarta uma resposta cuja pergunta mudou. O servidor recupera a oferta original sem guardar nada: `domain.possible_offers` enumera as listas que a regra de alternativas pode produzir e `_standing_answer` (`servidor-mcp/protocol.py:104`) compara o *digest* da pergunta selado no `requestState` com cada uma. Recusas sempre valem; escolhas só valem para salas que foram de fato oferecidas e que ainda estão livres; salas nunca oferecidas são rejeitadas.
- **Dependência de funções privadas do SDK.** Para ler esse *digest* o servidor usa três funções privadas de `mcp.server.mcpserver.resolve` (`_decode_state`, `_render_request`, `_request_digest`). O SDK não expõe a oferta original por API pública, e as alternativas (guardar no servidor, pedir ao cliente) violam "sem sessão" ou confiam em entrada controlada por atacante. A versão está **travada em `mcp==2.3.0`**; se o SDK mudar os nomes, o servidor falha ao importar (na subida), e se mudar o formato, `_standing_answer` não reconhece o estado e vale o comportamento original do SDK (nunca uma reserva errada). Ao atualizar o `mcp`, revisar esse trecho e rodar `tests/test_mrtr_http.py`.
- **Ausência de LLM.** O agente interpreta um comando de formato fixo (`reservar sala=... inicio=... fim=... responsavel=...`, `escolha=<id>|recusar`) por regra (`agente/command.py`). Isso mantém a saída determinística (duas pausas iguais são idênticas byte a byte) e confirma a opacidade do A2A. Nenhum SDK de LLM está nas dependências.
- **Isolamento e concorrência das Tasks.** Cada Task tem a sua entrada em `PendingStore`, indexada por `taskId`; duas Tasks pausadas ao mesmo tempo terminam cada uma com a sua reserva. Mensagens para a mesma Task são serializadas (`OneAtATimePerTask`, `agente/main.py`) e `claim` garante que uma pausa só é consumida uma vez.
- **Compatibilidade A2A.** Requests sem `A2A-Version` são tratados como v1.0; um `A2A-Version` explícito de outra versão continua sendo recusado.

### Limitações conhecidas

- **Header `MCP-Protocol-Version` legado ou ausente.** Com envelope moderno (`_meta`) mas header `2025-11-25`, `2025-06-18` ou ausente, o SDK 2.3.0 roteia o request pelo transporte legado: não devolve `-32020` e um conflito responde `-32600 ... has no back-channel`. Nenhuma reserva fica insegura (o serviço revalida tudo e o MRTR não é atravessado). Fica documentado e coberto por `xfail(strict=True)` (`tests/test_mcp_http.py`) em vez de reimplementar a validação do SDK. O agente e o validador sempre enviam header e envelope coerentes.
- Estado em memória: reiniciar o **agente** perde as Tasks (reiniciar o **MCP** não perde nenhuma pausa, desde que o segredo seja o mesmo).
- O `requestState` vale 10 minutos; depois disso o retry é recusado pelo servidor (`-32602`) e a Task pausada termina em `FAILED` com `O servidor de salas recusou o pedido (codigo -32602).`; basta enviar um novo pedido.

## Saída do validador

Saída integral da última execução, com processos recém-iniciados a partir de um clone limpo (`uv sync --frozen`, `REQUEST_STATE_SECRET` de 64 hex):

```
trace-id desta execucao: ccd9495b84c03c6a965c2290d1c29db9
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
exit=0
```
