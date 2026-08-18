"""
Cog: Verificação de Tela / Call (!t / !tela / !tela @user) + Configuração (!config)
-------------------------------------------------------------------------------------
Usa a API REST da Zoom (Server-to-Server OAuth) pra criar reuniões instantâneas.

Diferente do LiveKit, a conta Zoom Basic (grátis) NÃO dá acesso à Dashboard API
(quem entrou na sala em tempo real). Por isso não existe mais monitoramento
automático de "sala vazia expira sozinha". No lugar disso tem um botão
"🔄 Renovar sala" que apaga a reunião atual e cria outra na hora — é o mesmo
truque de reiniciar a call pra ganhar mais 40 minutos, só que num clique.

Variáveis de ambiente necessárias (Server-to-Server OAuth app, grátis, em
https://marketplace.zoom.us -> Build App):

    ZOOM_ACCOUNT_ID
    ZOOM_CLIENT_ID
    ZOOM_CLIENT_SECRET

Scope necessário no app: meeting:write:admin (e meeting:read:admin se quiser
listar reuniões futuramente).

!config -> só Administrator. Escolhe até 10 cargos autorizados a usar
           os comandos de análise/call.

!t / !tela [@alguem] -> cria uma reunião Zoom instantânea. Painel sem cor,
           sem emoji no título, com separadores, e dois itens:
             - Botão de link "Entrar na call" -> abre o join_url da Zoom
               direto (mesmo link pra qualquer um, a Zoom Basic não permite
               link pessoal por participante sem fluxo de aprovação).
             - Botão "🔄 Renovar sala" -> apaga a reunião atual e cria outra,
               resetando os 40 minutos. Só quem criou a sala ou um mediador
               autorizado pode renovar/encerrar.
"""

import os
import json
import time
import base64
import sqlite3
import asyncio

import aiohttp
import discord
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button, Separator

DB_PATH = "ffz_data.db"

ZOOM_ACCOUNT_ID = os.getenv("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = os.getenv("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = os.getenv("ZOOM_CLIENT_SECRET")
# Apps Server-to-Server NÃO podem usar o atalho "me" -> precisa do e-mail
# (ou userId) real da conta Zoom que vai hospedar as reuniões.
ZOOM_USER_ID = os.getenv("ZOOM_USER_ID")

MAX_CARGOS = 10
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ---------- camada de dados (sqlite síncrono, rodado em thread) ----------

def _criar_tabelas_sync():
    conn = sqlite3.connect(DB_PATH)

    # Migração: se a tabela tela_sessoes existir no formato antigo (LiveKit,
    # coluna "token"), apaga e recria no formato novo (Zoom, "meeting_id").
    colunas = [row[1] for row in conn.execute("PRAGMA table_info(tela_sessoes)").fetchall()]
    if colunas and "meeting_id" not in colunas:
        conn.execute("DROP TABLE tela_sessoes")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tela_sessoes (
            meeting_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            adm_id INTEGER NOT NULL,
            alvo_id INTEGER,
            criado_em TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'ativa'
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


def _salvar_sessao_sync(meeting_id: str, guild_id: int, adm_id: int, alvo_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tela_sessoes (meeting_id, guild_id, adm_id, alvo_id) VALUES (?, ?, ?, ?)",
        (meeting_id, guild_id, adm_id, alvo_id),
    )
    conn.commit()
    conn.close()


def _atualizar_status_sync(meeting_id: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tela_sessoes SET status = ? WHERE meeting_id = ?", (status, meeting_id))
    conn.commit()
    conn.close()


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


# ---------- Zoom: token OAuth (Server-to-Server) e criação/exclusão de reunião ----------

_token_cache = {"access_token": None, "expira_em": 0}
_token_lock = asyncio.Lock()


async def _obter_token_zoom() -> str:
    """Pega um access token válido, reaproveitando enquanto não expira (~1h)."""
    async with _token_lock:
        agora = time.time()
        if _token_cache["access_token"] and agora < _token_cache["expira_em"] - 60:
            return _token_cache["access_token"]

        auth = base64.b64encode(f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()).decode()
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async with session.post(
                "https://zoom.us/oauth/token",
                headers={"Authorization": f"Basic {auth}"},
                data={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
            ) as resp:
                dados = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"Falha ao autenticar na Zoom: {dados}")

        _token_cache["access_token"] = dados["access_token"]
        _token_cache["expira_em"] = agora + dados.get("expires_in", 3600)
        return _token_cache["access_token"]


async def _criar_reuniao_zoom(topico: str) -> dict:
    """Cria uma reunião instantânea (type=1) na conta host configurada."""
    if not ZOOM_USER_ID:
        raise RuntimeError(
            "ZOOM_USER_ID não configurado (precisa ser o e-mail da conta Zoom "
            "que vai hospedar as reuniões — apps Server-to-Server não usam 'me')."
        )
    token = await _obter_token_zoom()
    payload = {
        "topic": topico,
        "type": 1,  # instantânea
        "settings": {
            "join_before_host": True,
            "waiting_room": False,
            "host_video": True,
            "participant_video": True,
            "mute_upon_entry": True,
        },
    }
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.post(
            f"https://api.zoom.us/v2/users/{ZOOM_USER_ID}/meetings",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        ) as resp:
            dados = await resp.json()
            if resp.status not in (200, 201):
                raise RuntimeError(f"Falha ao criar reunião Zoom ({resp.status}): {dados}")
            return dados


async def _apagar_reuniao_zoom(meeting_id: str):
    token = await _obter_token_zoom()
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        async with session.delete(
            f"https://api.zoom.us/v2/meetings/{meeting_id}",
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            # 204 = sucesso, 404 = já não existe mais -> ambos ok pra nós
            if resp.status not in (204, 404):
                dados = await resp.text()
                raise RuntimeError(f"Falha ao apagar reunião Zoom ({resp.status}): {dados}")


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

class BotaoRenovar(Button):
    def __init__(self, painel: "PainelTela"):
        super().__init__(label="Renovar sala (novos 40min)", style=discord.ButtonStyle.secondary, emoji="🔄")
        self.painel = painel

    async def callback(self, interaction: discord.Interaction):
        painel = self.painel
        eh_dono = interaction.user.id == painel.adm_id
        eh_mediador = await painel.cog._checar_mediador_membro(interaction.user)
        if not (eh_dono or eh_mediador):
            return await interaction.response.send_message(
                "❌ Só quem criou a sala ou um mediador pode renovar.", ephemeral=True
            )

        await interaction.response.defer()

        try:
            await _apagar_reuniao_zoom(painel.meeting_id)
        except Exception:
            pass  # se já tinha caído/expirado do lado da Zoom, seguimos e criamos outra

        try:
            nova = await _criar_reuniao_zoom(painel.topico)
        except Exception as e:
            return await interaction.followup.send(f"❌ Erro ao criar nova reunião na Zoom: {e}", ephemeral=True)

        await asyncio.to_thread(_atualizar_status_sync, painel.meeting_id, "renovada")
        painel.meeting_id = str(nova["id"])
        painel.join_url = nova["join_url"]
        await asyncio.to_thread(
            _salvar_sessao_sync, painel.meeting_id, painel.guild_id, painel.adm_id, painel.alvo_id
        )

        painel._montar_conteudo(painel.alvo)
        await interaction.message.edit(view=painel)


class BotaoEncerrar(Button):
    def __init__(self, painel: "PainelTela"):
        super().__init__(label="Encerrar", style=discord.ButtonStyle.danger)
        self.painel = painel

    async def callback(self, interaction: discord.Interaction):
        painel = self.painel
        eh_dono = interaction.user.id == painel.adm_id
        eh_mediador = await painel.cog._checar_mediador_membro(interaction.user)
        if not (eh_dono or eh_mediador):
            return await interaction.response.send_message(
                "❌ Só quem criou a sala ou um mediador pode encerrar.", ephemeral=True
            )

        await interaction.response.defer()
        try:
            await _apagar_reuniao_zoom(painel.meeting_id)
        except Exception:
            pass
        await asyncio.to_thread(_atualizar_status_sync, painel.meeting_id, "encerrada")

        painel._montar_conteudo(painel.alvo, encerrada=True)
        await interaction.message.edit(view=painel)


class PainelTela(LayoutView):
    def __init__(self, cog: "Tela", meeting_id: str, join_url: str, topico: str,
                 guild_id: int, adm_id: int, alvo: discord.Member = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.meeting_id = meeting_id
        self.join_url = join_url
        self.topico = topico
        self.guild_id = guild_id
        self.adm_id = adm_id
        self.alvo = alvo
        self.alvo_id = alvo.id if alvo else None

        self.container = Container()  # sem accent_color -> sem barra colorida
        self._montar_conteudo(alvo)
        self.add_item(self.container)

    def _montar_conteudo(self, alvo, encerrada=False):
        self.container.clear_items()
        self.clear_items()

        if encerrada:
            self.container.add_item(TextDisplay("## Verificação de Tela"))
            self.container.add_item(Separator())
            self.container.add_item(TextDisplay("**Status:** 🔴 sala encerrada."))
            self.add_item(self.container)
            return

        alvo_linha = f"**Alvo:** {alvo.mention}" if alvo else "**Tipo:** sala aberta"
        self.container.add_item(TextDisplay("## Verificação de Tela"))
        self.container.add_item(Separator())
        self.container.add_item(TextDisplay(
            f"{alvo_linha}\n"
            f"**Status:** 🟢 sala ativa (Zoom grátis corta em ~40min — use *Renovar* se cair)"
        ))
        self.container.add_item(Separator())

        row = ActionRow()
        row.add_item(Button(label="Entrar na call", style=discord.ButtonStyle.link, url=self.join_url))
        self.container.add_item(row)

        row2 = ActionRow()
        row2.add_item(BotaoRenovar(self))
        row2.add_item(BotaoEncerrar(self))
        self.container.add_item(row2)

        self.add_item(self.container)


# ---------- cog ----------

class Tela(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await asyncio.to_thread(_criar_tabelas_sync)
        if not (ZOOM_ACCOUNT_ID and ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET and ZOOM_USER_ID):
            print("⚠️  ZOOM_ACCOUNT_ID / ZOOM_CLIENT_ID / ZOOM_CLIENT_SECRET / ZOOM_USER_ID não configurados nas Variáveis do app!")

    async def _checar_mediador(self, ctx: commands.Context) -> bool:
        return await self._checar_mediador_membro(ctx.author)

    async def _checar_mediador_membro(self, membro: discord.Member) -> bool:
        if membro.guild_permissions.administrator:
            return True

        cargos_autorizados = await asyncio.to_thread(_buscar_cargos_sync, membro.guild.id)
        if not cargos_autorizados:
            return membro.guild_permissions.manage_guild

        ids_do_autor = {r.id for r in membro.roles}
        return bool(ids_do_autor & set(cargos_autorizados))

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
        if not (ZOOM_ACCOUNT_ID and ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET and ZOOM_USER_ID):
            return await ctx.send(
                "❌ O bot ainda não tem as variáveis da Zoom configuradas "
                "(ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_USER_ID).", delete_after=10
            )

        if not await self._checar_mediador(ctx):
            return await ctx.send("❌ Você não tem permissão pra usar esse comando.", delete_after=8)

        if alvo is None and ctx.message.reference:
            msg_respondida = ctx.message.reference.resolved
            if isinstance(msg_respondida, discord.Message) and isinstance(msg_respondida.author, discord.Member):
                alvo = msg_respondida.author

        if alvo is not None and alvo.bot:
            return await ctx.send("❌ Não dá pra verificar um bot.", delete_after=8)

        topico = f"Verificação de Tela - {alvo.display_name}" if alvo else f"Verificação de Tela - {ctx.guild.name}"

        async with ctx.typing():
            try:
                reuniao = await _criar_reuniao_zoom(topico)

                meeting_id = str(reuniao["id"])
                join_url = reuniao["join_url"]

                await asyncio.to_thread(
                    _salvar_sessao_sync, meeting_id, ctx.guild.id, ctx.author.id,
                    alvo.id if alvo else None,
                )

                painel = PainelTela(self, meeting_id, join_url, topico, ctx.guild.id, ctx.author.id, alvo)
            except Exception as e:
                return await ctx.send(f"❌ Erro ao criar sala: `{e}`", delete_after=20)

        await ctx.send(view=painel)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tela(bot))
