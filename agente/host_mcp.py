"""O agente como host MCP: um cliente JSON-RPC sobre Streamable HTTP, sem sessao.

Cada request carrega o envelope completo no `_meta` (versao, clientInfo,
capabilities) e os headers que o transporte espelha do corpo. Nao ha callback
de elicitation: um `input_required` volta cru para quem chamou, porque quem
responde a pergunta e o cliente A2A, nao este processo.
"""

from __future__ import annotations

import itertools
import re
import secrets
import sys

import httpx2
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    LATEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META_KEY,
)

CLIENT_INFO = {"name": "agente-central-de-salas", "version": "1.0.0"}
CAPABILITIES = {"elicitation": {"form": {}}}
URI_POLITICA = "politica://uso"

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


class ErroMcp(Exception):
    """O servidor MCP respondeu com `error` de JSON-RPC, ou nao respondeu."""


class Trace:
    """Trace context de uma Task: o trace-id fica, o span-id muda a cada request MCP."""

    def __init__(self, traceparent: str | None) -> None:
        achado = _TRACEPARENT.match((traceparent or "").strip().lower())
        self.trace_id = achado.group(1) if achado else secrets.token_hex(16)
        self.flags = achado.group(3) if achado else "01"

    def novo_traceparent(self) -> str:
        return f"00-{self.trace_id}-{secrets.token_hex(8)}-{self.flags}"


class HostMcp:
    def __init__(self, url: str) -> None:
        self.url = url
        self._http = httpx2.AsyncClient(timeout=30.0)
        # Ids unicos no processo: o retry de um tools/call nunca reaproveita o id do request inicial.
        self._ids = itertools.count(1)
        self.ferramentas: dict[str, dict] | None = None
        self.politica: str | None = None

    async def fechar(self) -> None:
        await self._http.aclose()

    async def _request(self, metodo: str, params: dict, trace: Trace, nome: str | None = None) -> dict:
        id_ = next(self._ids)
        meta = {
            PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
            CLIENT_INFO_META_KEY: CLIENT_INFO,
            CLIENT_CAPABILITIES_META_KEY: CAPABILITIES,
            "traceparent": trace.novo_traceparent(),
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
            "Mcp-Method": metodo,
        }
        if nome is not None:
            headers["Mcp-Name"] = nome
        corpo = {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": {**params, "_meta": meta}}
        print(f"-> mcp {metodo} id={id_} traceparent={meta['traceparent']}", file=sys.stderr, flush=True)
        try:
            resposta = await self._http.post(self.url, json=corpo, headers=headers)
            dados = resposta.json()
        except (httpx2.HTTPError, ValueError) as erro:
            raise ErroMcp(f"falha falando com o servidor MCP: {erro}") from erro
        if "error" in dados:
            erro = dados["error"]
            raise ErroMcp(f"servidor MCP recusou {metodo} ({erro.get('code')}): {erro.get('message')}")
        return dados.get("result") or {}

    async def descobrir(self, trace: Trace) -> None:
        """tools/list e resources/read da politica, uma vez por processo, antes do primeiro tools/call."""
        if self.ferramentas is None:
            ferramentas: dict[str, dict] = {}
            cursor = None
            while True:
                resultado = await self._request("tools/list", {"cursor": cursor} if cursor else {}, trace)
                ferramentas |= {t["name"]: t for t in resultado.get("tools", [])}
                cursor = resultado.get("nextCursor")
                if not cursor:
                    break
            self.ferramentas = ferramentas
        if self.politica is None:
            resultado = await self._request("resources/read", {"uri": URI_POLITICA}, trace, nome=URI_POLITICA)
            texto = (resultado.get("contents") or [{}])[0].get("text", "")
            primeira = texto.splitlines()[0] if texto else ""
            if not primeira.startswith("versao:"):
                raise ErroMcp(f"{URI_POLITICA} nao declara a versao na primeira linha")
            self.politica = primeira.removeprefix("versao:").strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict,
        trace: Trace,
        *,
        input_responses: dict | None = None,
        request_state: str | None = None,
    ) -> dict:
        """Um `tools/call`; o resultado pode ser `complete` ou `input_required`, devolvido sem tratamento."""
        await self.descobrir(trace)
        assert self.ferramentas is not None
        if nome not in self.ferramentas:
            raise ErroMcp(f"o servidor MCP nao oferece a tool {nome}")
        params: dict = {"name": nome, "arguments": argumentos}
        if input_responses is not None:
            params["inputResponses"] = input_responses
        if request_state is not None:
            params["requestState"] = request_state
        return await self._request("tools/call", params, trace, nome=nome)
