"""
Integração com Google Meet (via Google Meet API — recurso "spaces")
-----------------------------------------------------------------------
Antes esse arquivo criava um evento no Google Calendar com uma
videochamada anexada (conferenceData). Isso funciona, mas a Calendar API
não deixa configurar o "accessType" da sala -- então toda call caía com
sala de espera (quem não tava convidado no evento precisava ser admitido
manualmente por um organizador). É exatamente o que você via na tela de
"Aguarde até que um organizador da reunião adicione você à chamada".

Agora a gente cria a sala direto pela Meet API (recurso `spaces`), que
permite marcar accessType="OPEN" -- ou seja, qualquer pessoa com o link
entra direto, sem sala de espera e sem precisar de aprovação. Isso
funciona em conta Google pessoal também (não precisa de Workspace).

Setup necessário (uma vez só, feito por você, fora do bot):
    1. No Google Cloud Console, em "APIs e serviços" > "Biblioteca",
       ativar a "Google Meet API" pro projeto (além da Calendar API, se
       ainda quiser deixar ela habilitada).
    2. Rodar setup_oauth.py de novo (ele agora pede a permissão
       "meetings.space.created" em vez de "calendar.events") -- isso gera
       um refresh token NOVO, porque o token antigo só tinha permissão
       pra Calendar e não serve pra Meet API.
    3. Atualizar a variável de ambiente GOOGLE_REFRESH_TOKEN no Discloud
       com esse token novo. GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET
       continuam os mesmos (é o mesmo projeto/app no Cloud Console).

Com conta Google comum (não-Workspace): reuniões com 3+ participantes caem
sozinhas em 60 minutos (aviso aos 55min). É a limitação do plano gratuito do
Google, não tem contorno via código — só pagando Workspace. Isso vale tanto
pra sala criada via Calendar quanto via Meet API, então não muda com essa
mudança.
"""

import os
import asyncio

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# escopo da Meet API -- "cria, edita e vê info das salas criadas pelo app".
# Precisa ser autorizado de novo com setup_oauth.py (o token antigo, que só
# tinha calendar.events, não serve mais).
SCOPES = ["https://www.googleapis.com/auth/meetings.space.created"]

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
        _service = build("meet", "v2", credentials=creds, cache_discovery=False)
    return _service


def _criar_reuniao_sync(titulo: str, minutos_duracao: int = 60) -> tuple[str, str]:
    """Cria uma sala (space) na Meet API com acesso aberto (sem sala de
    espera). O parâmetro `titulo` é mantido só por compatibilidade com quem
    chama essa função -- a Meet API não tem campo de título pra uma sala
    avulsa (isso só existe quando a sala nasce de um evento do Calendar).
    Retorna (link_do_meet, nome_do_espaco), onde nome_do_espaco é tipo
    "spaces/abc123", usado depois pra encerrar a call."""
    service = _get_service_sync()

    espaco_criado = service.spaces().create(body={
        "config": {
            "accessType": "OPEN",
            "entryPointAccess": "ALL",
        }
    }).execute()

    link = espaco_criado.get("meetingUri")
    nome_espaco = espaco_criado.get("name")
    if not link or not nome_espaco:
        raise RuntimeError("O Google não devolveu um link de Meet pra essa sala.")
    return link, nome_espaco


def _excluir_reuniao_sync(nome_espaco: str):
    """Encerra a conferência ativa da sala (se tiver alguém nela) pra tirar
    todo mundo. A sala em si (o link) continua existindo na Meet API -- só
    não tem mais ninguém dentro. Não precisa "deletar" a sala como fazia
    com o evento do Calendar."""
    service = _get_service_sync()
    try:
        service.spaces().endActiveConference(name=nome_espaco, body={}).execute()
    except Exception:
        pass  # não é crítico -- se já tava vazia ou deu erro, só ignora


async def criar_reuniao(titulo: str, minutos_duracao: int = 60) -> tuple[str, str]:
    """Versão async (roda o client síncrono do Google numa thread)."""
    return await asyncio.to_thread(_criar_reuniao_sync, titulo, minutos_duracao)


async def excluir_reuniao(evento_id: str):
    await asyncio.to_thread(_excluir_reuniao_sync, evento_id)


def _contar_participantes_sync(nome_espaco: str) -> int:
    """Quantas pessoas estão dentro da call agora. Devolve 0 se ninguém
    entrou ainda ou se a última conferência da sala já acabou -- nunca
    estoura erro pro painel (só devolve 0 se a consulta falhar)."""
    service = _get_service_sync()
    try:
        registros = service.conferenceRecords().list(
            filter=f'space.name="{nome_espaco}"', pageSize=1,
        ).execute()
        lista = registros.get("conferenceRecords", [])
        if not lista:
            return 0

        registro = lista[0]
        if registro.get("endTime"):  # a conferência mais recente já acabou
            return 0

        participantes = service.conferenceRecords().participants().list(
            parent=registro["name"], filter="latest_end_time IS NULL", pageSize=250,
        ).execute()
        return len(participantes.get("participants", []))
    except Exception:
        return 0


async def contar_participantes(nome_espaco: str) -> int:
    return await asyncio.to_thread(_contar_participantes_sync, nome_espaco)
