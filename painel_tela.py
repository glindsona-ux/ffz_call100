"""
Cog: Verificação de Tela (!t / !tela / !tela @user)
----------------------------------------------------
ADM digita !t, !tela ou !tela @alguem -> aparece um painel V2 na hora,
sem DM nenhuma. O painel tem:
  - botão "Entrar na Análise" (link direto pra call)
  - o mesmo link em bloco de código, pra copiar e mandar pra outros mediadores
  - a sala expira sozinha em 5 minutos se ninguém entrar

Como o bot no Discloud não tem porta externa pra RECEBER avisos, quem
checa se alguém entrou é o próprio bot: a cada 10s ele PERGUNTA pro
signaling_server (GET /status/<token>) se já tem gente na call.

Precisa do signaling_server.py rodando (TYPE=site no Discloud) e da
env SCREENCHECK_BASE_URL apontando pra URL pública dele.
"""

import os
import json
import uuid
import sqlite3
import asyncio
import urllib.request
import discord
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button

DB_PATH = "ffz_data.db"  # ajuste pro caminho/handle real do seu database.py
BASE_URL = os.getenv("SCREENCHECK_BASE_URL", "https://suaapp.discloud.app")
TEMPO_EXPIRACAO = 300  # 5 minutos
INTERVALO_CHECAGEM = 10  # a cada quantos segundos o bot pergunta pro signaling_server


# ---------- camada de dados (sqlite síncrono, rodado em thread) ----------

def _criar_tabela_sync():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tela_sessoes (
            token TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            adm_id INTEGER NOT NULL,
            alvo_id INTEGER,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pendente'
        )
    """)
    conn.commit()
    conn.close()


def _salvar_sessao_sync(token: str, guild_id: int, adm_id: int, alvo_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tela_sessoes (token, guild_id, adm_id, alvo_id) VALUES (?, ?, ?, ?)",
        (token, guild_id, adm_id, alvo_id),
    )
    conn.commit()
    conn.close()


def _atualizar_status_sync(token: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tela_sessoes SET status = ? WHERE token = ?", (status, token))
    conn.commit()
    conn.close()


def _checar_status_sync(token: str) -> bool:
    """Pergunta pro signaling_server se já tem alguém na call. Roda em thread
    porque urllib é bloqueante."""
    try:
        with urllib.request.urlopen(f"{BASE_URL}/status/{token}", timeout=5) as resp:
            dados = json.loads(resp.read().decode())
            return bool(dados.get("joined"))
    except Exception:
        return False


# ---------- painel V2 ----------

class PainelTela(LayoutView):
    def __init__(self, token: str, watch_url: str, alvo: discord.Member = None):
        super().__init__(timeout=None)  # quem controla a expiração é a task de polling, não o discord.py
        self.token = token
        self.watch_url = watch_url
        self.message: discord.Message | None = None

        self.container = Container(accent_color=discord.Color.dark_grey())
        self._montar_conteudo(alvo)
        self.add_item(self.container)

    def _montar_conteudo(self, alvo, expirado=False):
        self.container.clear_items()

        if expirado:
            texto = (
                f"## 🔎 Verificação de Tela\n"
                f"**Status:** ⏱️ sala expirada (ninguém entrou em 5 min)\n\n"
                f"Use `!t` de novo pra abrir uma nova sala."
            )
        else:
            alvo_linha = f"**Alvo:** {alvo.mention}\n" if alvo else "**Tipo:** sala aberta\n"
            texto = (
                f"## 🔎 Verificação de Tela\n"
                f"{alvo_linha}"
                f"**Status:** ⏳ aguardando entrada (expira em 5 min se ninguém entrar)\n\n"
                f"Clique no botão pra entrar na call.\n"
                f"Pra chamar outro mediador, é só mandar esse link:\n"
                f"```{self.watch_url}```"
            )

        self.container.add_item(TextDisplay(texto))

        if not expirado:
            row = ActionRow()
            row.add_item(Button(
                label="Entrar na Análise",
                style=discord.ButtonStyle.link,
                url=self.watch_url,
            ))
            self.container.add_item(row)

    def marcar_expirada(self):
        self._montar_conteudo(alvo=None, expirado=True)


# ---------- cog ----------

class Tela(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await asyncio.to_thread(_criar_tabela_sync)

    def _checar_mediador(self, ctx: commands.Context) -> bool:
        # TODO: trocar por checar_licenca_view() / checagem de cargo
        # de mediador igual você já usa em painel_moderacao.py
        return ctx.author.guild_permissions.manage_guild

    async def _monitorar_sessao(self, painel: PainelTela):
        """Fica perguntando pro signaling_server, de INTERVALO_CHECAGEM em
        INTERVALO_CHECAGEM segundos, se alguém entrou. Se ninguém entrar em
        TEMPO_EXPIRACAO segundos, edita o painel pra 'expirada'."""
        decorrido = 0
        while decorrido < TEMPO_EXPIRACAO:
            await asyncio.sleep(INTERVALO_CHECAGEM)
            decorrido += INTERVALO_CHECAGEM

            entrou = await asyncio.to_thread(_checar_status_sync, painel.token)
            if entrou:
                await asyncio.to_thread(_atualizar_status_sync, painel.token, "ativa")
                return  # sessão em uso, não expira mais

        await asyncio.to_thread(_atualizar_status_sync, painel.token, "expirada")
        painel.marcar_expirada()
        if painel.message:
            try:
                await painel.message.edit(view=painel)
            except discord.HTTPException:
                pass

    @commands.command(name="tela", aliases=["t"])
    async def tela(self, ctx: commands.Context, alvo: discord.Member = None):
        if not self._checar_mediador(ctx):
            return await ctx.send("❌ Você não tem permissão pra usar esse comando.", delete_after=8)

        # Se não passou @menção, tenta pegar o autor da mensagem respondida.
        if alvo is None and ctx.message.reference:
            msg_respondida = ctx.message.reference.resolved
            if isinstance(msg_respondida, discord.Message) and isinstance(msg_respondida.author, discord.Member):
                alvo = msg_respondida.author

        if alvo is not None and alvo.bot:
            return await ctx.send("❌ Não dá pra verificar um bot.", delete_after=8)

        token = uuid.uuid4().hex[:16]
        watch_url = f"{BASE_URL}/watch/{token}"

        await asyncio.to_thread(
            _salvar_sessao_sync, token, ctx.guild.id, ctx.author.id,
            alvo.id if alvo else None,
        )

        painel = PainelTela(token, watch_url, alvo)
        painel.message = await ctx.send(view=painel)

        asyncio.create_task(self._monitorar_sessao(painel))


async def setup(bot: commands.Bot):
    await bot.add_cog(Tela(bot))
