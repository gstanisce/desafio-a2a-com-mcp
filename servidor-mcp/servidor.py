"""Servidor MCP da Central de Salas: Streamable HTTP em :7301/mcp.

Tres tools, um resource e o ciclo de MRTR em `reservar_sala`. O servidor nao
guarda nada entre o `input_required` e o retry: o pedido original viaja selado
no `requestState`, que o `RequestStateBoundary` do SDK cifra (AES-256-GCM), amarra
a tool e aos argumentos, e expira.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
)
from pydantic import BaseModel

import dominio

PORTA = int(os.environ.get("MCP_PORT", "7301"))
CAMINHO = os.environ.get("MCP_PATH", "/mcp")
VALIDADE_DO_ESTADO = 600.0  # segundos: o requestState vale 10 minutos

# Chave do inputRequests. O cliente devolve a mesma no inputResponses.
CHAVE_ESCOLHA = "escolha_de_sala"
CAPABILITY_EXIGIDA = {"elicitation": {"form": {}}}


def _segredo() -> bytes:
    """Le REQUEST_STATE_SECRET; hex vira bytes, qualquer outro texto vale como UTF-8."""
    valor = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not valor:
        raise SystemExit(
            "REQUEST_STATE_SECRET nao definido. Gere com: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    try:
        segredo = bytes.fromhex(valor)
    except ValueError:
        segredo = valor.encode()
    if len(segredo) < 32:
        raise SystemExit(f"REQUEST_STATE_SECRET precisa de no minimo 32 bytes; tem {len(segredo)}")
    return segredo


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


mcp = MCPServer(
    "central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=VALIDADE_DO_ESTADO),
)


def _validar(sala: str, inicio: str, fim: str):
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.ErroDeRegra as erro:
        raise ToolError(str(erro)) from None


def _reservar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    reserva = dominio.criar_reserva(sala, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva.id,
        sala=reserva.sala,
        inicio=reserva.inicio,
        fim=reserva.fim,
        responsavel=reserva.responsavel,
        politica=dominio.POLITICA_VERSAO,
    )


def _declarou_elicitation_form(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    # `elicitation: {}` sem modos equivale a form; so `url` nao serve.
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _pedir_escolha(pedido: dict[str, str], opcoes: list[str]) -> InputRequiredResult:
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(
            message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": opcoes,
                    }
                },
                "required": ["sala"],
            },
        )
    )
    # Texto puro aqui: o RequestStateBoundary sela na saida e abre na volta.
    estado = json.dumps({"pedido": pedido, "alternativas": opcoes}, separators=(",", ":"))
    return InputRequiredResult(input_requests={CHAVE_ESCOLHA: pergunta}, request_state=estado)


def _campo(resposta: Any, nome: str) -> Any:
    return resposta.get(nome) if isinstance(resposta, dict) else getattr(resposta, nome, None)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**vars(s)) for s in dominio.SALAS.values()])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    ini, fi = _validar(sala, inicio, fim)
    em_conflito = dominio.conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(id=r.id, inicio=r.inicio, fim=r.fim, responsavel=r.responsavel) for r in em_conflito],
    )


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
) -> ReservaOut | InputRequiredResult:
    if ctx.request_state is not None:
        return _retomar(ctx)

    ini, fi = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, ini, fi):
        return _reservar(sala, inicio, fim, responsavel)

    opcoes = dominio.alternativas(sala, ini, fi)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    if not _declarou_elicitation_form(ctx):
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message="Client did not declare the form elicitation capability required to choose an alternative room",
            data={"requiredCapabilities": CAPABILITY_EXIGIDA},
        )
    pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    return _pedir_escolha(pedido, opcoes)


def _retomar(ctx: Context) -> ReservaOut | InputRequiredResult:
    """Segunda volta do MRTR: o pedido sai do estado selado, nunca dos argumentos reenviados."""
    assert ctx.request_state is not None
    estado = json.loads(ctx.request_state)
    pedido: dict[str, str] = estado["pedido"]
    oferecidas: list[str] = estado["alternativas"]

    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if resposta is None:
        return _pedir_escolha(pedido, oferecidas)

    acao = _campo(resposta, "action")
    if acao in ("decline", "cancel"):
        return ReservaOut(reservado=False, motivo="recusado" if acao == "decline" else "cancelado")

    escolhida = (_campo(resposta, "content") or {}).get("sala")
    if escolhida not in oferecidas:
        raise ToolError(f"Escolha fora das alternativas oferecidas: {escolhida}")
    ini, fi = _validar(escolhida, pedido["inicio"], pedido["fim"])
    if dominio.conflitos(escolhida, ini, fi):
        raise ToolError(f"Sala {escolhida} deixou de estar livre no intervalo")
    return _reservar(escolhida, pedido["inicio"], pedido["fim"], pedido["responsavel"])


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


class LogDeRequests:
    """Middleware ASGI: registra no stderr metodo, id e traceparent de cada request JSON-RPC.

    Fica antes do SDK para que tambem os requests recusados (sem _meta, header
    divergente) aparecam no log.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        corpo = b""
        mensagens = []
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break
        self._registrar(corpo)

        async def reenviar():
            return mensagens.pop(0) if mensagens else {"type": "http.disconnect"}

        await self.app(scope, reenviar, send)

    @staticmethod
    def _registrar(corpo: bytes) -> None:
        try:
            dados = json.loads(corpo)
        except ValueError:
            print("mcp request: corpo nao e JSON", file=sys.stderr, flush=True)
            return
        for item in dados if isinstance(dados, list) else [dados]:
            if not isinstance(item, dict):
                continue
            params = item.get("params") if isinstance(item.get("params"), dict) else {}
            meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
            alvo = params.get("name") or params.get("uri") or "-"
            print(
                f"mcp request method={item.get('method')} id={item.get('id')} "
                f"alvo={alvo} traceparent={meta.get('traceparent', '-')}",
                file=sys.stderr,
                flush=True,
            )


app = LogDeRequests(mcp.streamable_http_app(streamable_http_path=CAMINHO, json_response=True))


def main() -> None:
    print(f"servidor MCP em http://127.0.0.1:{PORTA}{CAMINHO}", file=sys.stderr, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
