# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio da Hill Valley Tech. São dois processos Python separados:

- **`servidor-mcp/`**: servidor MCP em Streamable HTTP (`http://localhost:7301/mcp`), feito com o SDK oficial `mcp` 2.3.0 (spec `2026-07-28`). Expõe as tools `listar_salas`, `consultar_disponibilidade` e `reservar_sala`, o resource `politica://uso` e o ciclo de MRTR na reserva.
- **`agente/`**: agente A2A v1.0 com binding JSON-RPC (`http://localhost:7300/a2a`, card em `/.well-known/agent-card.json`). Por dentro, ele é host MCP do servidor acima. Não usa LLM: interpreta o pedido em formato fixo e traduz protocolo.

`dados/`, `validador/` e `exemplos/` não foram alterados.

## Como rodar

Pré-requisito: Python 3.10 ou superior. No Windows, use `py` no lugar de `python3`.

```bash
git clone <url-do-fork> desafio-a2a-com-mcp
cd desafio-a2a-com-mcp

python3 -m venv .venv
source .venv/bin/activate              # Windows (PowerShell): .venv\Scripts\Activate.ps1
python -m pip install ./servidor-mcp ./agente
```

O servidor MCP exige a chave de integridade do `requestState` na variável `REQUEST_STATE_SECRET`, com no mínimo 32 bytes aleatórios. Gere uma chave sua e não publique o valor:

```bash
export REQUEST_STATE_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

```powershell
$env:REQUEST_STATE_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
```

Suba cada processo em um terminal, com o venv ativado. O servidor MCP precisa da variável exportada naquele terminal:

```bash
# terminal 1: servidor MCP (o log de requests sai no stderr)
python servidor-mcp/servidor.py

# terminal 2: agente A2A
python agente/agente.py

# terminal 3: validador, sempre com os dois processos recém-iniciados
python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis opcionais e seus valores padrão: `MCP_PORT=7301`, `MCP_PATH=/mcp`, `AGENTE_PORT=7300`, `MCP_URL=http://127.0.0.1:7301/mcp` e `AGENTE_URL_PUBLICA=http://127.0.0.1:7300/a2a`. Esta última é a URL publicada no card.

Cada request que chega ao servidor MCP aparece no stderr assim:

```
mcp request method=tools/call id=4 alvo=reservar_sala traceparent=00-<trace-id do cliente A2A>-<span novo>-01
```

O log é escrito por um middleware ASGI que fica antes do SDK. Por isso também aparecem os requests que o SDK recusa, como os que chegam sem `_meta`.

## Onde a ponte acontece

A ponte está em `agente/agente.py`.

Na ida, `_aplicar_resultado` (linha 93) recebe o resultado cru do `tools/call`. Quando `resultType` é `input_required`, a função pega a única entrada de `inputRequests` e lê as alternativas do `enum` (ou do `const`) da elicitation. Em seguida, coloca a Task em `TASK_STATE_INPUT_REQUIRED` com a mensagem `alternativas: ...` (linha 106) e guarda na Task, em `PausaMcp` (linha 107), a chave da elicitation, as alternativas e o `requestState`. A `PausaMcp` é uma parte privada da Task e nunca é serializada (`agente/tasks.py`).

Na volta, `continuar` (linha 158) recebe o `escolha=<valor>` do novo `SendMessage`:

- uma sala do `enum` vira `{"action": "accept", "content": {"sala": ...}}`;
- `recusar` vira `{"action": "decline"}`;
- qualquer outro valor mantém a Task pausada e repete a lista.

O retry vai ao servidor MCP na linha 182: os mesmos argumentos, `inputResponses` com a mesma chave e o `requestState` ecoado sem modificação. O retry sai por `HostMcp._request` (`agente/host_mcp.py`), que tira cada id de JSON-RPC de um contador do processo. Por isso o id do retry é sempre diferente do id do request inicial. O resultado volta para `_aplicar_resultado`, que conclui a Task (`COMPLETED`, com o artifact `reserva`), cancela (`CANCELED`) ou pausa de novo.

Do lado do servidor, `reservar_sala` (`servidor-mcp/servidor.py`, linha 168) devolve um `InputRequiredResult` montado por `_pedir_escolha` (linha 124). No retry, `_retomar` (linha 191) reconstrói o pedido a partir do `requestState` e conclui a reserva.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do SDK: `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)`, passado ao `MCPServer` (`servidor-mcp/servidor.py`, linha 94).

- O `RequestStateBoundary` do SDK sela o estado na saída com AES-256-GCM, com chave derivada por HKDF-SHA256 e token no formato `v1.<base64url>`. Na volta, ele verifica o token antes de a tool rodar.
- O envelope selado carrega a expiração, a audiência (o nome do servidor) e o vínculo com o request: método, nome da tool e um digest dos `arguments`.
- Qualquer adulteração do token, expiração, troca de tool ou de argumentos é recusada com `-32602 "Invalid or expired requestState"`. O motivo real vai para o log do servidor (`requestState rejected on tools/call: seal` / `request binding`).
- A chave vem só do ambiente. Hex é decodificado para bytes e qualquer outro texto é usado como UTF-8. Abaixo de 32 bytes, o servidor não sobe.

**Validade.** O `requestState` vale 10 minutos (`VALIDADE_DO_ESTADO = 600.0`), dentro da faixa de 5 a 30 minutos.

**Estado selado vence.** O plaintext do estado é `{"pedido": {sala, inicio, fim, responsavel}, "alternativas": [...]}`. No retry, `_retomar` usa só o que veio selado e ignora os `arguments` reenviados. Como o SDK já recusa argumentos divergentes, a verificação 18 passa pelos dois caminhos. A escolha do cliente precisa estar entre as alternativas seladas e continuar livre. O servidor não guarda nada entre o `input_required` e o retry, então um retry feito depois de reiniciar o servidor conclui a reserva (testei: `input_required`, reinício do processo e retry com sucesso).

**Capability.** Em conflito com alternativas, se o `_meta` do request não declara elicitation em form mode, o servidor responde `-32021` com `data.requiredCapabilities = {"elicitation": {"form": {}}}` e HTTP 400. Isso acontece antes de montar a pergunta.

**Estado das Tasks.** Fica em memória no agente, em `TaskStore` (`agente/tasks.py`): um `dict` por id, com um `asyncio.Lock` por Task. A parte pública da Task (`id`, `contextId`, `status`, `history`, `artifacts`) é o que vai para o JSON. A `PausaMcp` com o `requestState` fica fora dele e é apagada quando a Task sai de `INPUT_REQUIRED`. Cada Task tem a sua pausa, então duas Tasks pausadas ao mesmo tempo não trocam de estado. Estados terminais são definitivos: `SendMessage` em Task terminal recebe erro `-32004`.

**Cliente MCP do agente.** O agente usa um cliente JSON-RPC enxuto sobre HTTP (`agente/host_mcp.py`), com as constantes de `_meta` e de versão do pacote `mcp-types` do SDK oficial. Não usei o `Client` do SDK por dois motivos, com evidência no código instalado do `mcp` 2.3.0:

1. O `Client` só declara elicitation quando recebe um callback, e nesse caso declara form e url juntos: `types.ElicitationCapability(form=..., url=types.UrlElicitationCapability()) if self._elicitation_callback is not _default_elicitation_callback` (`mcp/client/session.py:635`). O enunciado pede exatamente `{"elicitation": {"form": {}}}`.
2. Com callback, o `Client.call_tool` responde a elicitation sozinho e o ciclo fecha sem voltar ao cliente A2A. O agente precisa ver o `input_required` cru.

Cada request MCP leva no `_meta` `protocolVersion`, `clientInfo`, `clientCapabilities` e `traceparent`. Nos headers vão `MCP-Protocol-Version`, `Mcp-Method` e, em `tools/call` e `resources/read`, `Mcp-Name`. Antes do primeiro `tools/call`, o agente faz `tools/list` e `resources/read politica://uso`, uma vez por processo. Do resource, ele tira a versão (`versao: ...`) que vai no campo `politica` do artifact. A tool só é chamada se aparecer no `tools/list`.

**Trace context.** O trace-id vem do header `traceparent` da chamada A2A. Cada request MCP daquela Task usa o mesmo trace-id com um span-id novo. Se o cliente A2A não envia o header, a Task gera um trace-id próprio e o mantém em todos os requests dela.

## Saída do validador

Última execução, com os dois processos recém-iniciados (código de saída 0):

```
trace-id desta execucao: ab812abadb32ad0e1156658683ffb537
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
```
