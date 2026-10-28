"""Dominio das salas: dados do starter, regras da politica e reservas em memoria.

Nada aqui conhece MCP. O servidor chama estas funcoes e traduz `ErroDeRegra`
em erro de execucao da tool (`isError: true`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parents[1] / "dados"

SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAXIMO_ALTERNATIVAS = 3


class ErroDeRegra(Exception):
    """Violacao de regra de negocio; a mensagem e a do enunciado, sem alteracao."""


@dataclass(frozen=True)
class Sala:
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


@dataclass(frozen=True)
class Reserva:
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str

    @property
    def intervalo(self) -> tuple[datetime, datetime]:
        return _instante(self.inicio), _instante(self.fim)


def _ler_json(nome: str) -> list[dict]:
    return json.loads((DADOS / nome).read_text(encoding="utf-8"))


SALAS: dict[str, Sala] = {s["id"]: Sala(**s) for s in _ler_json("salas.json")}
RESERVAS: list[Reserva] = [Reserva(**r) for r in _ler_json("reservas.json")]

POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].removeprefix("versao:").strip()


def _instante(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except ValueError:
        raise ErroDeRegra(f"Data invalida: {valor}") from None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=SAO_PAULO)
    return instante


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras de sala e de politica, na ordem em que o validador as exercita."""
    if sala not in SALAS:
        raise ErroDeRegra(f"Sala inexistente: {sala}")
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise ErroDeRegra("Intervalo invalido: fim deve ser posterior a inicio")
    ini_local, fim_local = ini.astimezone(SAO_PAULO), fi.astimezone(SAO_PAULO)
    if (
        ini_local.time() < ABERTURA
        or fim_local.date() != ini_local.date()
        or fim_local.time() > FECHAMENTO
    ):
        raise ErroDeRegra("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ErroDeRegra("Duracao acima do limite: a politica permite no maximo 2 horas")
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[Reserva]:
    return [r for r in RESERVAS if r.sala == sala and r.intervalo[0] < fi and ini < r.intervalo[1]]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, por (capacidade, id), no maximo tres."""
    minima = SALAS[sala].capacidade
    candidatas = [
        s for s in SALAS.values() if s.id != sala and s.capacidade >= minima and not conflitos(s.id, ini, fi)
    ]
    candidatas.sort(key=lambda s: (s.capacidade, s.id))
    return [s.id for s in candidatas[:MAXIMO_ALTERNATIVAS]]


def _proximo_id() -> str:
    maior = max((int(r.id.removeprefix("res-")) for r in RESERVAS), default=0)
    return f"res-{maior + 1:04d}"


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> Reserva:
    reserva = Reserva(id=_proximo_id(), sala=sala, inicio=inicio, fim=fim, responsavel=responsavel)
    RESERVAS.append(reserva)
    return reserva
