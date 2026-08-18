"""
Cog: Verificação de Tela / Call (!t / !tela / !tela @user) + Configuração (!config)
-------------------------------------------------------------------------------------
Usa o Jitsi Meet público (meet.jit.si) — sem API, sem token, sem limite de tempo.
Cada sala é um slug aleatório; não precisa renovar nada.

Sistema de espectador: a sessão gera um código curto (ex: FQQ96K). Quem tiver
o código pode entrar no canal configurado (#ver-tela), clicar no painel fixo
"Assistir Análise", digitar o código num formulário e recebe um link pessoal
pra assistir (ephemeral, só ele vê).

!config -> painel com:
    - cargos autorizados a usar !t / !tela (RoleSelect)
    - cor de destaque dos painéis (hex, via modal)
    - canal onde fica o painel fixo de espectador (ChannelSelect)
  Ao salvar, o painel de espectador é publicado/atualizado automaticamente
  no canal escolhido.

!t / !tela [@alguem] -> cria a sala e o painel do mediador (V2, com thumbnail
  do ícone do servidor, cor da org, separadores). Botão "Gerar URL Pessoal"
  entrega um link diferente pra quem é o alvo (compartilha tela) e pra quem é
  mediador/adm (assiste como espectador "oficial").
"""

import os
import json
import string
import random
import sqlite3
import asyncio
from urllib.parse import quote

import discord
from discord.ext import commands
from discord.ui import (
    LayoutView, Container, TextDisplay, ActionRow, Button, Separator,
    Section, Thumbnail, Modal, TextInput,
)

DB_PATH = "ffz_data.db"

MAX_CARGOS = 10
COR_PADRAO = 0x2B2D31  # cinza escuro discord, usado se a org não configurar cor
CARACTERES_CODIGO = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


# ---------- camada de dados (sqlite síncrono, rodado em thread) ----------

def _colunas_de(conn, tabela) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({tabela})").fetchall()]


def _criar_tabelas_sync():
    conn = sqlite3.connect(DB_PATH)

    # migração: tabela tela_sessoes de versões anteriores (LiveKit/Zoom)
    colunas = _colunas_de(conn, "tela_sessoes")
    if colunas and "sala" not in colunas:
        conn.execute("DROP TABLE tela_sessoes")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tela_sessoes (
            sala TEXT PRIMARY KEY,
            codigo TEXT UNIQUE NOT NULL,
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
            cargos_autorizados TEXT NOT NULL DEFAULT '[]',
            cor INTEGER,
            canal_espectador_id INTEGER,
            painel_espectador_msg_id INTEGER
        )
    """)
    # migração: adiciona colunas novas em bancos que só tinham cargos_autorizados
    colunas_cfg = _colunas_de(conn, "guild_config")
    for coluna, tipo in (("cor", "INTEGER"), ("canal_espectador_id", "INTEGER"),
                         ("painel_espectador_msg_id", "INTEGER")):
        if coluna not in colunas_cfg:
            conn.execute(f"ALTER TABLE guild_config ADD COLUMN {coluna} {tipo}")

    conn.commit()
    conn.close()


def _gerar_codigo_unico_sync() -> str:
    conn = sqlite3.connect(DB_PATH)
    while True:
        codigo = "".join(random.choices(CARACTERES_CODIGO, k=6))
        existe = conn.execute(
            "SELECT 1 FROM tela_sessoes WHERE codigo = ? AND status = 'ativa'", (codigo,)
        ).fetchone()
        if not existe:
            conn.close()
            return codigo


def _salvar_sessao_sync(sala: str, codigo: str, guild_id: int, adm_id: int, alvo_id: int = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tela_sessoes (sala, codigo, guild_id, adm_id, alvo_id) VALUES (?, ?, ?, ?, ?)",
        (sala, codigo, guild_id, adm_id, alvo_id),
    )
    conn.commit()
    conn.close()


def _encerrar_sessao_sync(sala: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tela_sessoes SET status = 'encerrada' WHERE sala = ?", (sala,))
    conn.commit()
    conn.close()


def _buscar_sessao_por_codigo_sync(guild_id: int, codigo: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT sala, adm_id, alvo_id FROM tela_sessoes "
        "WHERE guild_id = ? AND codigo = ? AND status = 'ativa'",
        (guild_id, codigo.strip().upper()),
    ).fetchone()
    conn.close()
    return row  # (sala, adm_id, alvo_id) ou None


def _buscar_config_sync(guild_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT cargos_autorizados, cor, canal_espectador_id, painel_espectador_msg_id "
        "FROM guild_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {"cargos": [], "cor": None, "canal_espectador_id": None, "painel_msg_id": None}
    return {
        "cargos": json.loads(row[0]) if row[0] else [],
        "cor": row[1],
        "canal_espectador_id": row[2],
        "painel_msg_id": row[3],
    }


def _salvar_config_sync(guild_id: int, **campos):
    """Faz upsert parcial: só atualiza os campos passados em campos."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO guild_config (guild_id, cargos_autorizados) VALUES (?, '[]') "
        "ON CONFLICT(guild_id) DO NOTHING", (guild_id,)
    )
    for chave, valor in campos.items():
        conn.execute(f"UPDATE guild_config SET {chave} = ? WHERE guild_id = ?", (valor, guild_id))
    conn.commit()
    conn.close()


# ---------- Jitsi: só monta a URL, não tem API nem token ----------

def _gerar_link_jitsi(sala: str, nome_exibicao: str, espectador: bool = False) -> str:
    nome = quote(nome_exibicao)
    partes = [f'userInfo.displayName="{nome}"']
    if espectador:
        # sem controle real de "somente visualização" no Jitsi público,
        # então pelo menos entra mudo pra não atrapalhar
        partes.append("config.startWithAudioMuted=true")
        partes.append("config.startWithVideoMuted=true")
    frag = "&".join(partes)
    return f"https://meet.jit.si/{sala}#{frag}"


# ---------- utilitário de cor/permissão compartilhados pelo cog ----------

async def _cor_da_guild(guild_id: int) -> int:
    cfg = await asyncio.to_thread(_buscar_config_sync, guild_id)
    return cfg["cor"] if cfg["cor"] is not None else COR_PADRAO


# ---------- painel de configuração (!config) ----------

class ModalCor(Modal, title="Cor do painel"):
    cor_hex = TextInput(
        label="Cor em hexadecimal (sem #)",
        placeholder="Ex: F2A900",
        min_length=6, max_length=6,
        required=True,
    )

    def __init__(self, config_view: "ConfigView"):
        super().__init__()
        self.config_view = config_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            valor = int(self.cor_hex.value, 16)
            if not (0 <= valor <= 0xFFFFFF):
                raise ValueError
        except ValueError:
            return await interaction.response.send_message(
                "❌ Cor inválida. Manda um hex de 6 dígitos, tipo `F2A900`.", ephemeral=True
            )
        self.config_view.cor_escolhida = valor
        await interaction.response.send_message(f"✅ Cor definida: `#{self.cor_hex.value.upper()}`", ephemeral=True)


class ConfigView(discord.ui.View):
    def __init__(self, cog: "Tela", guild_id: int, cfg_atual: dict):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.cargos_selecionados: list[int] = list(cfg_atual["cargos"])
        self.cor_escolhida: int | None = cfg_atual["cor"]
        self.canal_escolhido: int | None = cfg_atual["canal_espectador_id"]

        self.select_cargos = discord.ui.RoleSelect(
            placeholder=f"Cargos autorizados a usar !t (até {MAX_CARGOS})",
            min_values=1, max_values=MAX_CARGOS, row=0,
        )
        self.select_cargos.callback = self._on_cargos
        self.add_item(self.select_cargos)

        self.select_canal = discord.ui.ChannelSelect(
            placeholder="Canal do painel de espectador (#ver-tela)",
            channel_types=[discord.ChannelType.text], row=1,
        )
        self.select_canal.callback = self._on_canal
        self.add_item(self.select_canal)

    async def _on_cargos(self, interaction: discord.Interaction):
        self.cargos_selecionados = [r.id for r in self.select_cargos.values]
        await interaction.response.defer()

    async def _on_canal(self, interaction: discord.Interaction):
        self.canal_escolhido = self.select_canal.values[0].id
        await interaction.response.defer()

    @discord.ui.button(label="Definir cor", style=discord.ButtonStyle.secondary, emoji="🎨", row=2)
    async def definir_cor(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalCor(self))

    @discord.ui.button(label="Salvar configuração", style=discord.ButtonStyle.success, row=2)
    async def salvar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cargos_selecionados:
            return await interaction.response.send_message(
                "Selecione pelo menos 1 cargo antes de salvar.", ephemeral=True
            )

        campos = {"cargos_autorizados": json.dumps(self.cargos_selecionados)}
        if self.cor_escolhida is not None:
            campos["cor"] = self.cor_escolhida
        if self.canal_escolhido is not None:
            campos["canal_espectador_id"] = self.canal_escolhido

        await asyncio.to_thread(_salvar_config_sync, self.guild_id, **campos)

        aviso_canal = ""
        if self.canal_escolhido:
            canal = interaction.guild.get_channel(self.canal_escolhido)
            if canal:
                try:
                    await self.cog.publicar_painel_espectador(canal)
                    aviso_canal = f"\n📺 Painel de espectador publicado em {canal.mention}."
                except discord.HTTPException as e:
                    aviso_canal = f"\n⚠️ Não consegui publicar o painel de espectador: {e}"

        mencoes = ", ".join(f"<@&{r}>" for r in self.cargos_selecionados)
        await interaction.response.edit_message(
            content=f"✅ Configuração salva. Cargos autorizados: {mencoes}{aviso_canal}",
            view=None,
        )


# ---------- painel fixo de espectador (#ver-tela) ----------

class ModalCodigoEspectador(Modal, title="Assistir Análise"):
    codigo = TextInput(
        label="Código da análise",
        placeholder="Ex: A7F3K9",
        min_length=6, max_length=6,
        required=True,
    )

    def __init__(self, cog: "Tela"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        sessao = await asyncio.to_thread(
            _buscar_sessao_por_codigo_sync, interaction.guild_id, self.codigo.value
        )
        if not sessao:
            return await interaction.response.send_message(
                "❌ Código inválido ou a análise já foi encerrada.", ephemeral=True
            )

        sala, adm_id, alvo_id = sessao
        link = _gerar_link_jitsi(sala, f"👁 {interaction.user.display_name}", espectador=True)

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Entrar e assistir", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message(
            "✅ Seu link está pronto. É pessoal e de uso único — não repasse, "
            "cada espectador deve pegar o próprio código.",
            view=view, ephemeral=True,
        )


class PainelEspectador(LayoutView):
    """Painel fixo, sem estado próprio -> pode ser reregistrado com bot.add_view()."""

    def __init__(self, cog: "Tela"):
        super().__init__(timeout=None)
        self.cog = cog
        container = Container(accent_color=discord.Color.blurple())
        container.add_item(TextDisplay("## 📺 Assistir Análise"))
        container.add_item(Separator())
        container.add_item(TextDisplay(
            "Recebeu um **código de análise**? Clique no botão abaixo, digite o código "
            "e você recebe um link exclusivo pra assistir à transmissão ao vivo.\n\n"
            "-# Cada link é individual e de uso único — não repasse, gere o seu."
        ))
        container.add_item(Separator())
        row = ActionRow()
        row.add_item(BotaoEntrarEspectador(cog))
        container.add_item(row)
        self.add_item(container)


class BotaoEntrarEspectador(Button):
    def __init__(self, cog: "Tela"):
        super().__init__(
            label="Entrar na Análise", style=discord.ButtonStyle.primary,
            custom_id="ffz_call:entrar_espectador",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModalCodigoEspectador(self.cog))


# ---------- painel do mediador (!t / !tela) ----------

class BotaoGerarUrl(Button):
    def __init__(self, painel: "PainelTela"):
        super().__init__(label="Gerar URL Pessoal", style=discord.ButtonStyle.primary, row=0)
        self.painel = painel

    async def callback(self, interaction: discord.Interaction):
        painel = self.painel
        eh_alvo = painel.alvo_id and interaction.user.id == painel.alvo_id
        eh_mediador = interaction.user.id == painel.adm_id or await painel.cog._checar_mediador_membro(interaction.user)

        if not (eh_alvo or eh_mediador):
            return await interaction.response.send_message(
                "❌ Essa sala não é sua. Se você tem um código de espectador, use o painel "
                "de #ver-tela em vez desse botão.", ephemeral=True
            )

        if eh_alvo:
            link = _gerar_link_jitsi(painel.sala, interaction.user.display_name, espectador=False)
            texto = "🔗 Seu link pessoal — **entre e compartilhe sua tela**:"
        else:
            link = _gerar_link_jitsi(painel.sala, f"🛡 {interaction.user.display_name}", espectador=False)
            texto = "🔗 Seu link de moderador — entre pra acompanhar a análise:"

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Entrar e compartilhar tela", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message(texto, view=view, ephemeral=True)


class BotaoEncerrarSessao(Button):
    def __init__(self, painel: "PainelTela"):
        super().__init__(label="Encerrar", style=discord.ButtonStyle.danger, row=0)
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
        await asyncio.to_thread(_encerrar_sessao_sync, painel.sala)
        painel._montar_conteudo(encerrada=True)
        await interaction.message.edit(view=painel)


class PainelTela(LayoutView):
    def __init__(self, cog: "Tela", guild: discord.Guild, sala: str, codigo: str,
                 adm_id: int, alvo: discord.Member = None, cor: int = COR_PADRAO):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.sala = sala
        self.codigo = codigo
        self.adm_id = adm_id
        self.alvo = alvo
        self.alvo_id = alvo.id if alvo else None
        self.cor = cor

        self.container = Container(accent_color=discord.Color(cor))
        self._montar_conteudo()
        self.add_item(self.container)

    def _montar_conteudo(self, encerrada=False):
        self.container.clear_items()
        self.clear_items()

        icone_url = self.guild.icon.url if self.guild.icon else None

        if encerrada:
            if icone_url:
                secao = Section(TextDisplay("## Verificação de Tela"), accessory=Thumbnail(icone_url))
                self.container.add_item(secao)
            else:
                self.container.add_item(TextDisplay("## Verificação de Tela"))
            self.container.add_item(Separator())
            self.container.add_item(TextDisplay("**Status:** 🔴 sala encerrada."))
            self.add_item(self.container)
            return

        titulo = TextDisplay("## Verificação de Tela")
        if icone_url:
            self.container.add_item(Section(titulo, accessory=Thumbnail(icone_url)))
        else:
            self.container.add_item(titulo)
        self.container.add_item(Separator())

        alvo_linha = f"**Alvo:** {self.alvo.mention}" if self.alvo else "**Tipo:** sala aberta"
        self.container.add_item(TextDisplay(
            f"{alvo_linha}\n"
            f"**Status:** 🟢 sala ativa (sem limite de tempo)\n\n"
            f"**Código para espectadores:** `{self.codigo}`\n"
            f"-# Divulgue esse código pra galera assistir pelo painel de #ver-tela."
        ))
        self.container.add_item(Separator())

        row = ActionRow()
        row.add_item(BotaoGerarUrl(self))
        row.add_item(BotaoEncerrarSessao(self))
        self.container.add_item(row)

        self.add_item(self.container)


# ---------- cog ----------

class Tela(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        await asyncio.to_thread(_criar_tabelas_sync)
        # painel de espectador não tem estado -> pode ser registrado direto,
        # sobrevive a restart porque o custom_id bate
        self.bot.add_view(PainelEspectador(self))

    async def publicar_painel_espectador(self, canal: discord.TextChannel):
        cfg = await asyncio.to_thread(_buscar_config_sync, canal.guild.id)
        painel = PainelEspectador(self)

        msg = None
        if cfg["painel_msg_id"]:
            try:
                msg = await canal.fetch_message(cfg["painel_msg_id"])
            except discord.NotFound:
                msg = None

        if msg:
            await msg.edit(view=painel)
        else:
            nova_msg = await canal.send(view=painel)
            await asyncio.to_thread(
                _salvar_config_sync, canal.guild.id, painel_espectador_msg_id=nova_msg.id
            )

    async def _checar_mediador(self, ctx: commands.Context) -> bool:
        return await self._checar_mediador_membro(ctx.author)

    async def _checar_mediador_membro(self, membro: discord.Member) -> bool:
        if membro.guild_permissions.administrator:
            return True
        cfg = await asyncio.to_thread(_buscar_config_sync, membro.guild.id)
        if not cfg["cargos"]:
            return membro.guild_permissions.manage_guild
        ids_do_autor = {r.id for r in membro.roles}
        return bool(ids_do_autor & set(cfg["cargos"]))

    @commands.command(name="config", aliases=["configurar"])
    @commands.has_permissions(administrator=True)
    async def config(self, ctx: commands.Context):
        cfg_atual = await asyncio.to_thread(_buscar_config_sync, ctx.guild.id)
        view = ConfigView(self, ctx.guild.id, cfg_atual)
        await ctx.send(
            "⚙️ **Configuração da Verificação de Tela**\n"
            f"Selecione até {MAX_CARGOS} cargos autorizados, o canal do painel de "
            "espectador e (opcional) a cor dos painéis. Depois clique em salvar.",
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

        if alvo is None and ctx.message.reference:
            msg_respondida = ctx.message.reference.resolved
            if isinstance(msg_respondida, discord.Message) and isinstance(msg_respondida.author, discord.Member):
                alvo = msg_respondida.author

        if alvo is not None and alvo.bot:
            return await ctx.send("❌ Não dá pra verificar um bot.", delete_after=8)

        async with ctx.typing():
            try:
                sala = f"ffz-{ctx.guild.id}-{random.randint(100000, 999999)}"
                codigo = await asyncio.to_thread(_gerar_codigo_unico_sync)

                await asyncio.to_thread(
                    _salvar_sessao_sync, sala, codigo, ctx.guild.id, ctx.author.id,
                    alvo.id if alvo else None,
                )

                cor = await _cor_da_guild(ctx.guild.id)
                painel = PainelTela(self, ctx.guild, sala, codigo, ctx.author.id, alvo, cor)
                await ctx.send(view=painel)
            except Exception as e:
                import traceback
                traceback.print_exc()
                return await ctx.send(f"❌ Erro ao criar sala: `{type(e).__name__}: {e}`", delete_after=30)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tela(bot))
