"""Tasks A2A em memoria: identidade, estado, historico e produto.

Cada Task tem uma parte publica, que vira JSON nas respostas A2A, e uma parte
privada (`PausaMcp`), que guarda o `requestState` do MCP e nunca e serializada.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field

from host_mcp import Trace

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAIS = frozenset({COMPLETED, CANCELED, FAILED, REJECTED})


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


@dataclass
class PausaMcp:
    """O que o agente precisa para repetir o tools/call: guardado, ecoado, nunca aberto."""

    ferramenta: str
    argumentos: dict
    chave: str
    request_state: str
    alternativas: list[str]


@dataclass
class Task:
    id: str
    context_id: str
    estado: str = SUBMITTED
    mensagem_status: dict | None = None
    historico: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    trace: Trace | None = field(default=None, repr=False)
    pausa: PausaMcp | None = field(default=None, repr=False)
    trava: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def registrar_usuario(self, mensagem: dict) -> None:
        self.historico.append(mensagem)

    def transitar(self, estado: str, texto: str | None = None) -> None:
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja terminou em {self.estado}")
        self.estado = estado
        if texto is not None:
            mensagem = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.mensagem_status = mensagem
            self.historico.append(mensagem)
        if estado != INPUT_REQUIRED:
            self.pausa = None

    def adicionar_artifact(self, nome: str, texto: str) -> None:
        self.artifacts.append({"artifactId": novo_id("art"), "name": nome, "parts": [{"text": texto}]})

    def para_wire(self) -> dict:
        status: dict = {"state": self.estado}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(self.historico),
            "artifacts": list(self.artifacts),
        }


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def criar(self, context_id: str | None = None) -> Task:
        task = Task(id=novo_id("task"), context_id=context_id or novo_id("ctx"))
        self._tasks[task.id] = task
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)
