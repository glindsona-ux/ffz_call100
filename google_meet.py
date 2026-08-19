"""
Integração com Google Meet (via Google Calendar API)
-----------------------------------------------------
O Google Meet não tem como o Jitsi um jeito de "inventar" uma sala só montando
uma URL. Pra gerar um link de Meet de verdade, a gente precisa criar um evento
de verdade no Google Calendar de uma conta Google (a sua) pedindo pra anexar
uma videochamada (conferenceData). O Google devolve o link do Meet daquele
evento.

Setup necessário (uma vez só, feito por você, fora do bot):
    1. Rodar setup_oauth.py na sua máquina (com navegador) pra autorizar o
       bot a usar SUA conta Google. Isso gera um "refresh token".
    2. Colocar 3 variáveis de ambiente no Discloud:
       GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
       (as duas primeiras vêm do Google Cloud Console, a terceira do passo 1)

Com conta Google comum (não-Workspace): reuniões com 3+ participantes caem
sozinhas em 60 minutos (aviso aos 55min). É a limitação do plano gratuito do
Google, não tem contorno via código — só pagando Workspace.
"""

import os
import asyncio
import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

_service = None  # cache do client autenticado, montado na primeira chamada


def _montar_credenciais() -> Credentials:
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())  # troca o refresh_token por um access_token novo
    return creds


def _get_service_sync():
    global _service
    if _service is None:
        faltando = [v for v in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
                    if not os.getenv(v)]
        if faltando:
            raise RuntimeError(
                f"Faltam variáveis de ambiente do Google: {', '.join(faltando)}. "
                "Rode setup_oauth.py e configure elas no Discloud."
            )
        creds = _montar_credenciais()
        _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def _criar_reuniao_sync(titulo: str, minutos_duracao: int = 60) -> tuple[str, str]:
    """Cria o evento no Calendar com uma videochamada do Meet anexada.
    Retorna (link_do_meet, id_do_evento)."""
    service = _get_service_sync()

    agora = datetime.datetime.utcnow()
    inicio = agora.isoformat() + "Z"
    fim = (agora + datetime.timedelta(minutes=minutos_duracao)).isoformat() + "Z"

    corpo_evento = {
        "summary": titulo,
        "start": {"dateTime": inicio, "timeZone": "UTC"},
        "end": {"dateTime": fim, "timeZone": "UTC"},
        # request_id só precisa ser único por chamada; usamos algo simples
        "conferenceData": {
            "createRequest": {
                "requestId": f"ffz-{agora.timestamp()}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        # evento "privado" -- não convida ninguém, só existe pra gerar o link
        "visibility": "private",
        "guestsCanInviteOthers": False,
    }

    evento_criado = service.events().insert(
        calendarId="primary",
        body=corpo_evento,
        conferenceDataVersion=1,  # obrigatório pro Google realmente gerar o Meet
    ).execute()

    link = evento_criado.get("hangoutLink")
    if not link:
        raise RuntimeError("O Google não devolveu um link de Meet pra esse evento.")
    return link, evento_criado["id"]


def _excluir_reuniao_sync(evento_id: str):
    service = _get_service_sync()
    try:
        service.events().delete(calendarId="primary", eventId=evento_id).execute()
    except Exception:
        pass  # não é crítico -- se já sumiu ou deu erro, só ignora


async def criar_reuniao(titulo: str, minutos_duracao: int = 60) -> tuple[str, str]:
    """Versão async (roda o client síncrono do Google numa thread)."""
    return await asyncio.to_thread(_criar_reuniao_sync, titulo, minutos_duracao)


async def excluir_reuniao(evento_id: str):
    await asyncio.to_thread(_excluir_reuniao_sync, evento_id)
