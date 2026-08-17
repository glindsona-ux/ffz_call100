"""
Cog: Verificação de Tela (!t / !tela / !tela @user) + Configuração (!config)
-----------------------------------------------------------------------------
!config  -> só quem tem Administrator no server. Abre um seletor de cargos
            (até 10) que ficam autorizados a usar !t / !tela dali em diante.
            Enquanto ninguém configurar nada, cai no fallback antigo
            (manage_guild), pra não travar quem já estava usando.

!t / !tela [@alguem] -> só quem tem um dos cargos configurados (ou admin)
            consegue usar. Mostra um painel V2 sem cor de card, sem emoji
            no título, com separadores, e dois botões:
              - "Entrar na Análise" (link direto)
              - "Copiar link" (o bot manda o link cru no chat, sem embed,
                fácil de copiar no celular)
            A sala expira sozinha em 5 minutos se ninguém entrar.

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
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button, Separator

DB_PATH = "ffz_data.db"  # ajuste pro caminho/handle real do seu database.py
BASE_URL = os.getenv("SCREENCHECK_BASE_URL", "https://suaapp.discloud.app")
TEMPO_EXPIRACAO = 300  # 5 minutos
INTERVALO_CHECAGEM = 10  # a cada quantos segundos o bot pergunta pro signaling_server
MAX_CARGOS = 10


# ---------- camada de dados (sqlite síncrono, rodado em thread) ----------

def _criar_tabelas_sync():
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            cargos_autorizados TEXT NOT NULL
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


def _salvar_config_sync(guild_id: int, cargos_ids: list[int]):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO guild_config (guild_id, cargos_autorizados) VALUES (?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET cargos_autorizados = excluded.cargos_autorizados",
        (guild_id, json.dumps(cargos_ids)),
    )
    conn.commit()
    conn.close()


def _buscar_cargos_sync(guild_id: int) -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT cargos_autorizados FROM guild_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    return json.loads(row[0]) if row else []


# ---------- painel de configuração (!config) ----------

class ConfigCargosView(discord.ui.View):
    def __init__(self, guild_id: int, cargos_atuais: list[int]):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.cargos_selecionados: list[int] = list(cargos_atuais)

        self.select = discord.ui.RoleSelect(
            placeholder=f"Selecione até {MAX_CARGOS} cargos autorizados",
            min_values=1,
            max_values=MAX_CARGOS,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        self.cargos_selecionados = [r.id for r in self.select.values]
        await interaction.response.defer()

    @discord.ui.button(label="Salvar configuração", style=discord.ButtonStyle.success)
    async def salvar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cargos_selecionados:
            return await interaction.response.send_message(
                "Selecione pelo menos 1 cargo antes de salvar.", ephemeral=True
            )
        await asyncio.to_thread(_salvar_config_sync, self.guild_id, self.cargos_selecionados)
        mencoes = ", ".join(f"<@&{r}>" for r in self.cargos_selecionados)
        await interaction.response.edit_message(
            content=f"✅ Configuração salva. Cargos autorizados a usar `!t`: {mencoes}",
            view=None,
        )


# ---------- painel V2 (!t / !tela) ----------

class BotaoCopiarLink(Button):
    def __init__(self, watch_url: str):
        super().__init__(label="Copiar link", style=discord.ButtonStyle.secondary, emoji="🔗")
        self.watch_url = watch_url

    async def callback(self, interaction: discord.Interaction):
        # manda o link cru, sem embed nem formatação, fácil de segurar-e-copiar no celular
        await interaction.response.send_message(self.watch_url)


class PainelTela(LayoutView):
    def __init__(self, token: str, watch_url: str, alvo: discord.Member = None):
        super().__init__(timeout=None)  # quem controla a expiração é a task de polling, não o discord.py
        self.token = token
        self.watch_url = watch_url
        self.message: discord.Message | None = None

        self.container = Container()  # sem accent_color -> sem barra colorida
        self._montar_conteudo(alvo)
        self.add_item(self.container)

    def _montar_conteudo(self, alvo, expirado=False):
        self.container.clear_items()

        if expirado:
            self.container.add_item(TextDisplay("## Verificação de Tela"))
            self.container.add_item(Separator())
            self.container.add_item(TextDisplay(
                "**Status:** ⏱️ sala expirada (ninguém entrou em 5 min)\n\n"
                "Use `!t` de novo pra abrir uma nova sala."
            ))
            return

        alvo_linha = f"**Alvo:** {alvo.mention}" if alvo else "**Tipo:** sala aberta"
        self.container.add_item(TextDisplay("## Verificação de Tela"))
        self.container.add_item(Separator())
        self.container.add_item(TextDisplay(
            f"{alvo_linha}\n"
            f"**Status:** ⏳ aguardando entrada (expira em 5 min se ninguém entrar)"
        ))
        self.container.add_item(Separator())

        row = ActionRow()
        row.add_item(Button(
            label="Entrar na Análise",
            style=discord.ButtonStyle.link,
            url=self.watch_url,
        ))
        row.add_item(BotaoCopiarLink(self.watch_url))
        self.container.add_item(row)

    def marcar_expirada(self):
        self._montar_conteudo(alvo=None, expirado=True)


# ---------- cog ----------

class Tela(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await asyncio.to_thread(_criar_tabelas_sync)

    async def _checar_mediador(self, ctx: commands.Context) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True

        cargos_autorizados = await asyncio.to_thread(_buscar_cargos_sync, ctx.guild.id)

        if not cargos_autorizados:
            # ninguém configurou !config ainda nesse server -> mantém o
            # comportamento antigo, pra não travar quem já estava usando
            return ctx.author.guild_permissions.manage_guild

        ids_do_autor = {r.id for r in ctx.author.roles}
        return bool(ids_do_autor & set(cargos_autorizados))

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

    @commands.command(name="config", aliases=["configurar"])
    @commands.has_permissions(administrator=True)
    async def config(self, ctx: commands.Context):
        cargos_atuais = await asyncio.to_thread(_buscar_cargos_sync, ctx.guild.id)
        view = ConfigCargosView(ctx.guild.id, cargos_atuais)
        await ctx.send(
            f"⚙️ Selecione até {MAX_CARGOS} cargos que poderão usar os comandos "
            f"de análise (`!t`, `!tela`) e clique em salvar.",
            view=view,
        )

    @config.error
    async def config_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Só administradores podem usar `!config`.", delete_after=8)

    @commands.command(name="tela", aliases=["t"])
    async def tela(self, ctx: commands.Context, alvo: discord.Member = None):
        if not await self._checar_mediador(ctx):
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
