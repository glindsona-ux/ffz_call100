"""
Cog: Verificação de Tela / Call (!t / !tela / !tela @user) + Configuração (!config)
-------------------------------------------------------------------------------------
Usa LiveKit (https://livekit.io) em vez de um signaling_server próprio.
Isso elimina o problema de domínio/DNS: o link final abre direto em
https://meet.livekit.io, que é hospedado pelo próprio LiveKit.

Você NÃO precisa mais do app "ffz_signaling" (TYPE=site) no Discloud.
Só precisa desse bot rodando + 3 variáveis de ambiente configuradas
na aba "Variáveis" do app, criadas de graça em https://cloud.livekit.io:

    LIVEKIT_URL          -> ex: wss://seuprojeto.livekit.cloud
    LIVEKIT_API_KEY       -> gerado no painel do LiveKit Cloud
    LIVEKIT_API_SECRET    -> gerado no painel do LiveKit Cloud

!config -> só Administrator. Escolhe até 10 cargos autorizados a usar
           os comandos de análise/call.

!t / !tela [@alguem] -> gera uma sala nova. Painel sem cor, sem emoji
           no título, com separadores, e dois botões:
             - "Entrar na Análise": gera um link PESSOAL (token com a
               identidade de quem clicou) e responde só pra ela (ephemeral).
             - "Copiar link": gera um link convidado (bearer, qualquer
               um que tiver ele entra) e manda ele cru no chat, fácil
               de repassar pra outro mediador.
"""

import os
import json
import uuid
import sqlite3
import asyncio
from datetime import timedelta

import discord
from discord.ext import commands
from discord.ui import LayoutView, Container, TextDisplay, ActionRow, Button, Separator
from livekit import api

DB_PATH = "ffz_data.db"

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

TEMPO_EXPIRACAO = 300       # 5 minutos
INTERVALO_CHECAGEM = 10     # a cada quantos segundos o bot checa se alguém entrou
TTL_TOKEN = timedelta(hours=6)
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


def _salvar_sessao_sync(sala: str, guild_id: int, adm_id: int, alvo_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tela_sessoes (token, guild_id, adm_id, alvo_id) VALUES (?, ?, ?, ?)",
        (sala, guild_id, adm_id, alvo_id),
    )
    conn.commit()
    conn.close()


def _atualizar_status_sync(sala: str, status: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tela_sessoes SET status = ? WHERE token = ?", (status, sala))
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


# ---------- LiveKit: geração de link e checagem de participantes ----------

def _gerar_link_livekit(sala: str, identidade: str, nome: str) -> str:
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identidade)
        .with_name(nome)
        .with_grants(api.VideoGrants(
            room_join=True,
            room=sala,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        ))
        .with_ttl(TTL_TOKEN)
        .to_jwt()
    )
    return f"https://meet.livekit.io/custom?liveKitUrl={LIVEKIT_URL}&token={token}"


async def _tem_participante(sala: str) -> bool:
    lkapi = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        resultado = await lkapi.room.list_rooms(api.ListRoomsRequest(names=[sala]))
        if not resultado.rooms:
            return False
        return resultado.rooms[0].num_participants > 0
    except Exception:
        return False
    finally:
        await lkapi.aclose()


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

class BotaoEntrar(Button):
    def __init__(self, sala: str):
        super().__init__(label="Entrar na Análise", style=discord.ButtonStyle.primary)
        self.sala = sala

    async def callback(self, interaction: discord.Interaction):
        link = await asyncio.to_thread(
            _gerar_link_livekit, self.sala, str(interaction.user.id), interaction.user.display_name
        )
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Abrir análise", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message(
            "🔗 Seu link pessoal (expira em 6h):", view=view, ephemeral=True
        )


class BotaoCopiarLink(Button):
    def __init__(self, sala: str):
        super().__init__(label="Copiar link", style=discord.ButtonStyle.secondary, emoji="🔗")
        self.sala = sala

    async def callback(self, interaction: discord.Interaction):
        identidade = f"convidado-{uuid.uuid4().hex[:8]}"
        link = await asyncio.to_thread(_gerar_link_livekit, self.sala, identidade, "Convidado")
        # manda o link cru, sem embed nem formatação, fácil de segurar-e-copiar no celular
        await interaction.response.send_message(link)


class PainelTela(LayoutView):
    def __init__(self, sala: str, alvo: discord.Member = None):
        super().__init__(timeout=None)
        self.sala = sala
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
        row.add_item(BotaoEntrar(self.sala))
        row.add_item(BotaoCopiarLink(self.sala))
        self.container.add_item(row)

    def marcar_expirada(self):
        self._montar_conteudo(alvo=None, expirado=True)


# ---------- cog ----------

class Tela(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await asyncio.to_thread(_criar_tabelas_sync)
        if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
            print("⚠️  LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET não configurados nas Variáveis do app!")

    async def _checar_mediador(self, ctx: commands.Context) -> bool:
        if ctx.author.guild_permissions.administrator:
            return True

        cargos_autorizados = await asyncio.to_thread(_buscar_cargos_sync, ctx.guild.id)
        if not cargos_autorizados:
            return ctx.author.guild_permissions.manage_guild

        ids_do_autor = {r.id for r in ctx.author.roles}
        return bool(ids_do_autor & set(cargos_autorizados))

    async def _monitorar_sessao(self, painel: PainelTela):
        decorrido = 0
        while decorrido < TEMPO_EXPIRACAO:
            await asyncio.sleep(INTERVALO_CHECAGEM)
            decorrido += INTERVALO_CHECAGEM

            if await _tem_participante(painel.sala):
                await asyncio.to_thread(_atualizar_status_sync, painel.sala, "ativa")
                return

        await asyncio.to_thread(_atualizar_status_sync, painel.sala, "expirada")
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
        if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
            return await ctx.send(
                "❌ O bot ainda não tem as variáveis do LiveKit configuradas "
                "(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET).", delete_after=10
            )

        if not await self._checar_mediador(ctx):
            return await ctx.send("❌ Você não tem permissão pra usar esse comando.", delete_after=8)

        if alvo is None and ctx.message.reference:
            msg_respondida = ctx.message.reference.resolved
            if isinstance(msg_respondida, discord.Message) and isinstance(msg_respondida.author, discord.Member):
                alvo = msg_respondida.author

        if alvo is not None and alvo.bot:
            return await ctx.send("❌ Não dá pra verificar um bot.", delete_after=8)

        sala = f"ffz-{uuid.uuid4().hex[:12]}"

        await asyncio.to_thread(
            _salvar_sessao_sync, sala, ctx.guild.id, ctx.author.id,
            alvo.id if alvo else None,
        )

        painel = PainelTela(sala, alvo)
        painel.message = await ctx.send(view=painel)

        asyncio.create_task(self._monitorar_sessao(painel))


async def setup(bot: commands.Bot):
    await bot.add_cog(Tela(bot))
