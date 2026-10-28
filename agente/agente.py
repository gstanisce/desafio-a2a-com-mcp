"""Agente Central de Salas: servidor A2A v1.0 (JSON-RPC) em :7300, host MCP por dentro.

O agente nao decide nada sobre salas. Ele interpreta o pedido em formato fixo,
chama o servidor MCP e traduz o resultado em estado de Task. A ponte entre os
dois protocolos esta em `_aplicar_resultado` (input_required -> INPUT_REQUIRED)
e em `continuar` (requestState de volta ao servidor, num tools/call de id novo).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from host_mcp import ErroMcp, HostMcp, Trace
from tasks import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, WORKING, PausaMcp, Task, TaskStore

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL_PUBLICA", f"http://127.0.0.1:{PORTA}/a2a")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

FERRAMENTA_RESERVA = "reservar_sala"
PEDIDO = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")
RECUSAR = "recusar"

# Codigos de erro do A2A v1.0 (binding JSON-RPC).
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

CARD = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [{"url": URL_PUBLICA, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}

store = TaskStore()
host = HostMcp(MCP_URL)


class ErroA2A(Exception):
    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


def _texto(mensagem: dict) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [] if isinstance(p, dict)).strip()


def _linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def _opcoes_da_elicitation(pedido: dict) -> list[str]:
    """As alternativas na ordem do enum (ou o const, quando o schema traz uma so)."""
    campo = ((pedido.get("params") or {}).get("requestedSchema") or {}).get("properties", {}).get("sala", {})
    if "enum" in campo:
        return [str(v) for v in campo["enum"]]
    if "const" in campo:
        return [str(campo["const"])]
    return []


def _aplicar_resultado(task: Task, ferramenta: str, argumentos: dict, resultado: dict) -> None:
    """Traduz um resultado de tools/call em estado de Task. Aqui o input_required vira INPUT_REQUIRED."""
    if resultado.get("resultType") == "input_required":
        pedidos = resultado.get("inputRequests") or {}
        if len(pedidos) != 1 or not resultado.get("requestState"):
            task.transitar(FAILED, "Resposta input_required inesperada do servidor MCP")
            return
        chave, pedido = next(iter(pedidos.items()))
        alternativas = _opcoes_da_elicitation(pedido)
        if pedido.get("method") != "elicitation/create" or not alternativas:
            task.transitar(FAILED, "Pedido de informacao do servidor MCP nao suportado")
            return
        # A ponte: a pergunta do MCP sobe para o cliente A2A e o requestState fica com a Task.
        task.transitar(INPUT_REQUIRED, _linha_alternativas(alternativas))
        task.pausa = PausaMcp(
            ferramenta=ferramenta,
            argumentos=argumentos,
            chave=chave,
            request_state=resultado["requestState"],
            alternativas=alternativas,
        )
        return

    texto = " ".join(c.get("text", "") for c in resultado.get("content") or [] if c.get("type") == "text")
    if resultado.get("isError"):
        task.transitar(FAILED, texto or "A tool devolveu erro sem mensagem")
        return

    dados = resultado.get("structuredContent") or {}
    if dados.get("reservado") is False:
        task.transitar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}.")
        return

    reserva = {
        "reserva": dados.get("reserva"),
        "sala": dados.get("sala"),
        "inicio": dados.get("inicio"),
        "fim": dados.get("fim"),
        "responsavel": dados.get("responsavel"),
        "politica": host.politica,
    }
    task.adicionar_artifact("reserva", json.dumps(reserva))
    task.transitar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")


async def iniciar(task: Task, texto: str) -> None:
    achado = PEDIDO.match(texto)
    if not achado:
        task.transitar(
            FAILED,
            "Pedido fora do formato: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
        )
        return
    sala, inicio, fim, responsavel = achado.groups()
    argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel.strip()}
    task.transitar(WORKING)
    assert task.trace is not None
    try:
        resultado = await host.chamar_tool(FERRAMENTA_RESERVA, argumentos, task.trace)
    except ErroMcp as erro:
        task.transitar(FAILED, str(erro))
        return
    _aplicar_resultado(task, FERRAMENTA_RESERVA, argumentos, resultado)


async def continuar(task: Task, texto: str) -> None:
    """Resposta do cliente A2A a uma pausa: vira inputResponses e o requestState volta ao servidor."""
    pausa = task.pausa
    assert pausa is not None
    achado = ESCOLHA.match(texto)
    escolha = achado.group(1) if achado else None
    if escolha == RECUSAR:
        resposta: dict = {"action": "decline"}
    elif escolha in pausa.alternativas:
        resposta = {"action": "accept", "content": {"sala": escolha}}
    else:
        # Fora do enum: a Task continua pausada e repete a mesma lista.
        task.transitar(INPUT_REQUIRED, _linha_alternativas(pausa.alternativas))
        task.pausa = pausa
        return

    task.transitar(WORKING)
    assert task.trace is not None
    try:
        resultado = await host.chamar_tool(
            pausa.ferramenta,
            pausa.argumentos,
            task.trace,
            input_responses={pausa.chave: resposta},
            request_state=pausa.request_state,
        )
    except ErroMcp as erro:
        task.transitar(FAILED, str(erro))
        return
    _aplicar_resultado(task, pausa.ferramenta, pausa.argumentos, resultado)


async def send_message(params: dict, traceparent: str | None) -> dict:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroA2A(-32602, "params.message com parts e obrigatorio")
    texto = _texto(mensagem)
    task_id = mensagem.get("taskId")

    if not task_id:
        task = store.criar(mensagem.get("contextId"))
        task.trace = Trace(traceparent)
        task.registrar_usuario(mensagem)
        async with task.trava:
            await iniciar(task, texto)
        return {"task": task.para_wire()}

    task = store.obter(task_id)
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task not found: {task_id}")
    async with task.trava:
        if task.terminal:
            raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} is in terminal state {task.estado}")
        if task.estado != INPUT_REQUIRED or task.pausa is None:
            raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {task_id} is not waiting for input")
        if traceparent:
            task.trace = Trace(traceparent)
        task.registrar_usuario(mensagem)
        await continuar(task, texto)
    return {"task": task.para_wire()}


async def get_task(params: dict) -> dict:
    task = store.obter(str(params.get("id", "")))
    if task is None:
        raise ErroA2A(TASK_NOT_FOUND, f"Task not found: {params.get('id')}")
    return {"task": task.para_wire()}


def _erro(id_, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}})


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return _erro(None, -32700, "Parse error")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _erro(corpo.get("id") if isinstance(corpo, dict) else None, -32600, "Invalid Request")
    id_, metodo = corpo.get("id"), corpo["method"]
    params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
    traceparent = request.headers.get("traceparent")
    print(f"<- a2a {metodo} id={id_} traceparent={traceparent or '-'}", file=sys.stderr, flush=True)
    try:
        if metodo == "SendMessage":
            resultado = await send_message(params, traceparent)
        elif metodo == "GetTask":
            resultado = await get_task(params)
        else:
            return _erro(id_, -32601, f"Method not found: {metodo}")
    except ErroA2A as erro:
        return _erro(id_, erro.codigo, erro.mensagem)
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


async def agent_card(_: Request) -> JSONResponse:
    return JSONResponse(CARD)


@contextlib.asynccontextmanager
async def ciclo_de_vida(_app):
    yield
    await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


def main() -> None:
    print(f"agente A2A em http://127.0.0.1:{PORTA} (MCP em {MCP_URL})", file=sys.stderr, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
