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
import logging
import concurrent.futures

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger("ffz.google_meet")

# escopo da Meet API -- "cria, edita e vê info das salas criadas pelo app".
# Precisa ser autorizado de novo com setup_oauth.py (o token antigo, que só
# tinha calendar.events, não serve mais).
SCOPES = ["https://www.googleapis.com/auth/meetings.space.created"]

_service = None  # cache do client autenticado, montado na primeira chamada

# FIX (bot inteiro caindo de hora em hora): as chamadas HTTP daqui passam
# pelo `httplib2`, usado por baixo dos panos pelo google-api-python-client.
# Os logs mostravam o processo INTEIRO morrendo (crash fatal em C, não uma
# exceção Python normal) bem no meio de uma chamada httplib2/http.client --
# ou seja, um segfault dentro dessa lib (provavelmente por incompatibilidade
# com a versão do Python rodando no Discloud). Rodar isso numa THREAD (como
# era antes, via asyncio.to_thread) não protege contra isso: um crash fatal
# em qualquer thread mata o processo inteiro, já que threads compartilham o
# mesmo processo.
#
# A defesa real é rodar essas chamadas num PROCESSO separado: se esse
# processo-filho morrer, o ProcessPoolExecutor detecta (BrokenProcessPool),
# a gente vira isso numa exceção Python normal (RuntimeError) e o pool sobe
# um processo novo sozinho pra próxima chamada -- o bot principal nunca
# encosta na causa do crash e continua rodando.
_pool: concurrent.futures.ProcessPoolExecutor | None = None

# MARCA DE DIAGNÓSTICO -- não é parte da lógica, é só pra confirmar nos logs
# se ESTE arquivo (com isolamento por processo) é o que está realmente
# rodando no Discloud depois de um deploy. Procure por essa linha logo
# depois de "INICIANDO BOT" nos logs: se ela não aparecer, o deploy não
# pegou o arquivo novo (cache de build / path errado / bot não reiniciou).
logger.warning("[google_meet] build carregado: ISOLAMENTO-POR-PROCESSO-v1")


def _get_pool() -> concurrent.futures.ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = concurrent.futures.ProcessPoolExecutor(max_workers=1)
    return _pool


async def _rodar_isolado(func, *args, timeout: float = 20.0):
    """Roda `func(*args)` num processo separado, com timeout. Nunca deixa
    um crash ou travamento do lado do Google derrubar o bot -- na pior das
    hipóteses, levanta RuntimeError/TimeoutError, que quem chamou já trata."""
    global _pool
    loop = asyncio.get_running_loop()
    pool = _get_pool()
    logger.info(f"[google_meet] chamando {func.__name__} via processo isolado (pid do pool ainda não conhecido até o worker subir)")
    try:
        future = loop.run_in_executor(pool, func, *args)
        return await asyncio.wait_for(future, timeout=timeout)
    except concurrent.futures.process.BrokenProcessPool:
        logger.error(
            "[google_meet] processo isolado morreu (provável crash fatal na "
            "chamada Google/httplib2) -- recriando o pool e propagando erro "
            "normal em vez de derrubar o bot."
        )
        _pool = None  # descarta o pool quebrado, o próximo _get_pool() cria um novo
        raise RuntimeError("Falha de comunicação com o Google Meet (processo isolado caiu). Tente novamente.")
    except asyncio.TimeoutError:
        logger.warning(f"[google_meet] chamada isolada travou mais de {timeout}s, abortando.")
        raise


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
    global _service
    service = _get_service_sync()

    try:
        espaco_criado = service.spaces().create(body={
            "config": {
                "accessType": "OPEN",
                "entryPointAccess": "ALL",
            }
        }).execute(num_retries=3)
    except Exception:
        # conexão keep-alive pode ter caído (BrokenPipe/SSLError) depois de
        # um tempo ocioso -- derruba o client cacheado pra forçar uma
        # conexão nova na próxima chamada, e tenta de novo uma vez agora
        _service = None
        service = _get_service_sync()
        espaco_criado = service.spaces().create(body={
            "config": {
                "accessType": "OPEN",
                "entryPointAccess": "ALL",
            }
        }).execute(num_retries=3)

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
    """Versão async (roda o client síncrono do Google num processo isolado --
    ver comentário grande em _rodar_isolado sobre o porquê de não ser mais
    uma thread)."""
    return await _rodar_isolado(_criar_reuniao_sync, titulo, minutos_duracao)


async def excluir_reuniao(evento_id: str):
    await _rodar_isolado(_excluir_reuniao_sync, evento_id)


def _contar_participantes_sync(nome_espaco: str) -> int | None:
    """Quantas pessoas estão dentro da call agora. Devolve 0 se a consulta
    funcionou e realmente não tem ninguém (ninguém entrou ainda, ou a
    conferência mais recente já acabou). Devolve None se a CONSULTA em si
    falhou (erro de rede/API) -- antes isso também virava 0, o que mostrava
    "Ninguém na call" no painel mesmo quando o problema era só a consulta
    ter falhado, não a call estar vazia de verdade. Quem exibe (painel_tela)
    trata None como "não foi possível confirmar agora" em vez de 0.
    """
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
        return None


async def contar_participantes(nome_espaco: str) -> int | None:
    # Essa é chamada com frequência (painel_tela atualiza o contador),
    # então aqui a gente NÃO deixa RuntimeError/TimeoutError subir --
    # devolve None (quem exibe já trata None como "não deu pra confirmar
    # agora"), pra um Google instável não gerar erro repetido no painel.
    try:
        return await _rodar_isolado(_contar_participantes_sync, nome_espaco)
    except (RuntimeError, asyncio.TimeoutError):
        return None
