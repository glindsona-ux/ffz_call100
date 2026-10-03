"""
Cog: Verificação de Tela / Call (!t / !tela / !tela @user) + Configuração (!config)
-------------------------------------------------------------------------------------
Usa Google Meet (via Meet API do google_meet.py -- recurso "spaces", accessType
OPEN) -- cada !tela cria uma sala de verdade na conta Google configurada nas
variáveis de ambiente GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET /
GOOGLE_REFRESH_TOKEN. Com conta Google comum (sem Workspace), a call encerra
sozinha em DURACAO_CALL_MINUTOS quando tem 3+ pessoas (limite do Google, não
do bot).

Acesso ao link da call:
    - A sala já nasce PÚBLICA -- qualquer pessoa que clicar em "Link da
      Call" recebe o link na hora, sem precisar de liberação de um
      mediador. O texto muda conforme quem clica é o alvo, um mediador
      configurado, ou qualquer outra pessoa -- mas o link é sempre o
      mesmo (o Meet não separa link por papel).
    - O Meet em si já é criado com accessType OPEN (ninguém bate numa
      "sala de espera" do Google depois de conseguir o link).
    - Só um dos cargos configurados em !config (ou admin) pode ENCERRAR a
      sessão -- inclusive quem criou a sala, se não for mais um desses
      cargos.

Sistema de espectador: a sessão gera um código curto (ex: FQQ96K). Quem tiver
o código pode entrar no canal configurado (#ver-tela), clicar no painel fixo
"Assistir Análise", digitar o código num formulário e recebe o link da call
(ephemeral, só ele vê) -- isso funciona independente da sala estar pública
ou não.

!config -> painel com:
    - cargos autorizados a usar !t / !tela (RoleSelect)
    - cor de destaque dos painéis (hex, via modal)
    - canal onde fica o painel fixo de espectador (ChannelSelect)
  Ao salvar, o painel de espectador é publicado/atualizado automaticamente
  no canal escolhido.

!t / !tela [@alguem] -> cria a call e o painel do mediador (V2, com thumbnail
  do ícone do servidor, cor da org, separadores). O link do Meet é único (o
  Meet não separa por papel), então o botão só muda o texto conforme quem
  clica é o alvo ou o mediador. O painel também mostra, ao vivo, quantas
  pessoas estão dentro da call agora (consulta a Meet API a cada ~25s
  enquanto a sessão está ativa).
"""

import os
import json
import string
import random
import sqlite3
import asyncio

import discord
from discord.ext import commands
from discord.ui import (
    LayoutView, Container, TextDisplay, ActionRow, Button, Separator,
    Section, Thumbnail, Modal, TextInput,
)

import google_meet
import dedupe

DB_PATH = "ffz_data.db"

MAX_CARGOS = 10
COR_PADRAO = 0x2B2D31  # cinza escuro discord, usado se a org não configurar cor
CARACTERES_CODIGO = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"

# ---------- emojis: puxados do Developer Portal (aba "Emojis" do seu app) ----------
# Sobe um emoji lá com cada um desses nomes; o bot busca sozinho no cog_load.
# Se algum ainda não tiver sido subido, usa o padrão (unicode) como fallback.
EMOJIS = {
    "call": "🎥",
    "alvo": "🎯",
    "status_ativa": "🟢",
    "status_encerrada": "🔴",
    "codigo": "🔑",
    "espectador": "👁️",
    "escudo": "🛡️",
    "link": "🔗",
    "aviso": "⚠️",
}
NOME_NO_PORTAL_PARA_CHAVE = {
    "ffz_call": "call",
    "ffz_alvo": "alvo",
    "ffz_status_ativa": "status_ativa",
    "ffz_status_encerrada": "status_encerrada",
    "ffz_codigo": "codigo",
    "ffz_espectador": "espectador",
    "ffz_escudo": "escudo",
    "ffz_link": "link",
    "ffz_aviso": "aviso",
}
DURACAO_CALL_MINUTOS = 60  # limite do Meet grátis pra 3+ pessoas na call


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
            status TEXT DEFAULT 'ativa',
            link_meet TEXT,
            evento_id TEXT,
            publica INTEGER NOT NULL DEFAULT 0
        )
    """)
    # migração: bancos antigos (era feito só pro Jitsi, sem essas colunas)
    colunas_sessoes = _colunas_de(conn, "tela_sessoes")
    for coluna in ("link_meet", "evento_id"):
        if colunas_sessoes and coluna not in colunas_sessoes:
            conn.execute(f"ALTER TABLE tela_sessoes ADD COLUMN {coluna} TEXT")
    if colunas_sessoes and "publica" not in colunas_sessoes:
        conn.execute("ALTER TABLE tela_sessoes ADD COLUMN publica INTEGER NOT NULL DEFAULT 0")

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


def _salvar_sessao_sync(sala: str, codigo: str, guild_id: int, adm_id: int, alvo_id: int = None,
                         link_meet: str = None, evento_id: str = None):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tela_sessoes (sala, codigo, guild_id, adm_id, alvo_id, link_meet, evento_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sala, codigo, guild_id, adm_id, alvo_id, link_meet, evento_id),
    )
    conn.commit()
    conn.close()


def _buscar_evento_id_sync(sala: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT evento_id FROM tela_sessoes WHERE sala = ?", (sala,)).fetchone()
    conn.close()
    return row[0] if row else None


def _encerrar_sessao_sync(sala: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE tela_sessoes SET status = 'encerrada' WHERE sala = ?", (sala,))
    conn.commit()
    conn.close()


def _buscar_sessao_por_codigo_sync(guild_id: int, codigo: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT sala, adm_id, alvo_id, link_meet FROM tela_sessoes "
        "WHERE guild_id = ? AND codigo = ? AND status = 'ativa'",
        (guild_id, codigo.strip().upper()),
    ).fetchone()
    conn.close()
    return row  # (sala, adm_id, alvo_id, link_meet) ou None


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

        sala, adm_id, alvo_id, link = sessao

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Entrar na call", style=discord.ButtonStyle.link, url=link))
        await interaction.response.send_message(
            "**Seu acesso foi liberado.**\n"
            "-# Entre com o microfone e a câmera desligados para não atrapalhar a análise.",
            view=view, ephemeral=True,
        )


class PainelEspectador(LayoutView):
    """Painel fixo, sem estado próprio -> pode ser reregistrado com bot.add_view()."""

    def __init__(self, cog: "Tela", guild: discord.Guild = None, cor: int = COR_PADRAO):
        super().__init__(timeout=None)
        self.cog = cog
        container = Container(accent_color=discord.Color(cor))

        titulo = TextDisplay("## Assistir Análise")
        icone_url = guild.icon.url if guild and guild.icon else None
        if icone_url:
            container.add_item(Section(titulo, accessory=Thumbnail(icone_url)))
        else:
            container.add_item(titulo)
        container.add_item(Separator())

        container.add_item(TextDisplay(
            "Recebeu um **código de análise**? Toque no botão abaixo e informe o código "
            "para liberar o acesso à sessão.\n"
            "-# Entre sempre com o microfone e a câmera desligados."
        ))
        container.add_item(Separator())
        row = ActionRow()
        row.add_item(BotaoEntrarEspectador(cog))
        container.add_item(row)
        self.add_item(container)


class BotaoEntrarEspectador(Button):
    def __init__(self, cog: "Tela"):
        super().__init__(
            label="Entrar na Análise", style=discord.ButtonStyle.secondary,
            custom_id="ffz_call:entrar_espectador",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModalCodigoEspectador(self.cog))


# ---------- painel do mediador (!t / !tela) ----------

class BotaoGerarUrl(Button):
    """A call nasce PÚBLICA desde a criação -- qualquer pessoa que clicar
    aqui recebe o link (o gate de privado/mediador foi removido, ver
    docstring do callback abaixo). Sem emoji de propósito no rótulo, a
    pedido -- fica mais limpo."""
    def __init__(self, painel: "PainelTela"):
        super().__init__(label="Link da Call", style=discord.ButtonStyle.primary, row=0)
        self.painel = painel

    async def callback(self, interaction: discord.Interaction):
        painel = self.painel
        eh_alvo = painel.alvo_id and interaction.user.id == painel.alvo_id
        eh_mediador = interaction.user.id == painel.adm_id or await painel.cog._checar_mediador_membro(interaction.user)

        # A call nasce PÚBLICA -- qualquer pessoa que clicar aqui recebe o
        # link, sem precisar de liberação de um mediador (antes existia um
        # toggle "Deixar Público"; removido porque agora já é pública desde
        # a criação). O texto muda conforme o papel de quem clica, mas o
        # link é o mesmo pra todo mundo -- o Meet não separa por papel.
        if eh_alvo:
            texto = (
                "Entre e **compartilhe sua tela** assim que possível.\n"
                "-# No celular, abra pelo **app do Google Meet** — no iPhone, compartilhar "
                "tela não funciona pelo Safari/navegador."
            )
        elif eh_mediador:
            texto = "Entre para acompanhar a análise (mediador):"
        else:
            texto = "Entre para acompanhar a análise:"

        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Entrar na call", style=discord.ButtonStyle.link, url=painel.link_meet))
        await interaction.response.send_message(texto, view=view, ephemeral=True)


class BotaoEncerrarSessao(Button):
    def __init__(self, painel: "PainelTela"):
        super().__init__(label="Encerrar", style=discord.ButtonStyle.danger, row=0)
        self.painel = painel

    async def callback(self, interaction: discord.Interaction):
        painel = self.painel
        # PEDIDO: só os cargos configurados em !config podem encerrar --
        # antes quem criou a sala (adm_id) também podia, mesmo que não
        # fosse mais um cargo autorizado (ex: perdeu o cargo depois de criar
        # a sessão). _checar_mediador_membro já cobre admin + os até
        # MAX_CARGOS cargos escolhidos -- não precisa de exceção pro dono.
        if not await painel.cog._checar_mediador_membro(interaction.user):
            return await interaction.response.send_message(
                "❌ Só um dos cargos configurados em `!config` pode encerrar a sessão.", ephemeral=True
            )

        await interaction.response.defer()
        painel.parar_atualizacao()
        await asyncio.to_thread(_encerrar_sessao_sync, painel.sala)
        evento_id = await asyncio.to_thread(_buscar_evento_id_sync, painel.sala)
        if evento_id:
            await google_meet.excluir_reuniao(evento_id)
        painel._montar_conteudo(encerrada=True)
        await interaction.message.edit(view=painel)


# tempo entre consultas do contador de participantes -- não precisa ser muito
# curto, é só pra dar a sensação de "ao vivo" sem estourar a cota da API
INTERVALO_CONTADOR_SEGUNDOS = 25
# corta o loop sozinho depois desse tempo, mesmo que ninguém clique em
# "Encerrar" -- rede de segurança pra não deixar tasks penduradas pra sempre
LIMITE_LOOP_MINUTOS = DURACAO_CALL_MINUTOS + 15


class PainelTela(LayoutView):
    def __init__(self, cog: "Tela", guild: discord.Guild, sala: str, codigo: str,
                 adm_id: int, alvo: discord.Member = None, cor: int = COR_PADRAO,
                 link_meet: str = None, nome_espaco: str = None):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild = guild
        self.sala = sala
        self.codigo = codigo
        self.adm_id = adm_id
        self.alvo = alvo
        self.alvo_id = alvo.id if alvo else None
        self.cor = cor
        self.link_meet = link_meet
        self.nome_espaco = nome_espaco

        # None = ainda não consultou / última consulta falhou (mostra
        # "Consultando..." em vez de fingir que é 0 -- ver google_meet.py).
        # 0 é só quando a API confirmou de verdade que não tem ninguém.
        self.participantes: int | None = None
        self.encerrada = False
        self.message: discord.Message | None = None

        self.container = Container(accent_color=discord.Color(cor))
        self._montar_conteudo()  # já adiciona self.container à view (necessário pro re-render no encerrar)

        self._task_participantes = asyncio.create_task(self._loop_participantes())

    def parar_atualizacao(self):
        self.encerrada = True
        if self._task_participantes and not self._task_participantes.done():
            self._task_participantes.cancel()

    async def _loop_participantes(self):
        """Atualiza o contador de "participantes na call agora" periodicamente
        enquanto a sessão estiver ativa. Só edita a mensagem quando o número
        muda, pra não ficar re-editando à toa. MELHORIA: faz a primeira
        consulta quase na hora (2s) em vez de esperar o intervalo inteiro
        (25s) pra sair do "Consultando..." inicial -- painel fica com dado
        real bem mais rápido depois de o mediador criar a sala."""
        if not self.nome_espaco:
            return

        await asyncio.sleep(2)
        if not self.encerrada:
            await self._atualizar_contador()

        decorridos = 0
        while decorridos < LIMITE_LOOP_MINUTOS * 60:
            await asyncio.sleep(INTERVALO_CONTADOR_SEGUNDOS)
            decorridos += INTERVALO_CONTADOR_SEGUNDOS
            if self.encerrada:
                return
            await self._atualizar_contador()

    async def _atualizar_contador(self):
        novo_total = await google_meet.contar_participantes(self.nome_espaco)
        if novo_total != self.participantes:
            self.participantes = novo_total
            self._montar_conteudo()
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass

    def _montar_conteudo(self, encerrada=False):
        self.container.clear_items()
        self.clear_items()

        # Título "ANÁLISE <NOME DO SERVIDOR>" (sem emoji), com o ícone do servidor
        # como thumbnail ao lado -- pedido depois de tirar tudo antes.
        # Nome da org/servidor onde o painel está (em maiúsculas, igual ao
        # visual antigo "ANÁLISE FFZ"); escape_markdown evita que um nome
        # com * _ ~ ` quebre a formatação do título.
        nome_org = discord.utils.escape_markdown(self.guild.name).upper()
        titulo = TextDisplay(f"# **ANÁLISE {nome_org}**")
        icone_url = self.guild.icon.url if self.guild.icon else None
        if icone_url:
            self.container.add_item(Section(titulo, accessory=Thumbnail(icone_url)))
        else:
            self.container.add_item(titulo)
        self.container.add_item(Separator())

        if encerrada:
            self.container.add_item(TextDisplay("**Sessão encerrada.** Link e código não valem mais."))
            self.add_item(self.container)
            return

        # ---- bloco 1: quem/o quê (direto, sem emoji nem parágrafo longo) ----
        alvo_linha = (f"**Alvo:** {self.alvo.mention}" if self.alvo else "**Sessão:** aberta")
        self.container.add_item(TextDisplay(
            f"{alvo_linha} · **pública** — qualquer pessoa entra pelo link a qualquer hora.\n"
            f"-# No iPhone, compartilhar tela só funciona pelo app do Meet (não pelo Safari)."
        ))
        self.container.add_item(Separator())

        # ---- bloco 2: status ao vivo ----
        if self.participantes is None:
            contador_txt = "consultando…"
        elif self.participantes == 0:
            contador_txt = "ninguém entrou ainda"
        else:
            contador_txt = f"`{self.participantes}` na call agora"
        self.container.add_item(TextDisplay(
            f"**Encerra em:** {DURACAO_CALL_MINUTOS} min (automático) · **Participantes:** {contador_txt}"
        ))
        self.container.add_item(Separator())

        # ---- bloco 3: código de espectador ----
        self.container.add_item(TextDisplay(
            f"**Código:** `{self.codigo}`\n"
            f"-# Use em #ver-tela para liberar acesso a espectadores."
        ))
        self.container.add_item(Separator())

        # ---- ações: link + encerrar lado a lado ----
        row_acoes = ActionRow()
        row_acoes.add_item(BotaoGerarUrl(self))
        row_acoes.add_item(BotaoEncerrarSessao(self))
        self.container.add_item(row_acoes)

        self.add_item(self.container)


# ---------- cog ----------

class Tela(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._emojis_carregados = False

    async def cog_load(self):
        await asyncio.to_thread(_criar_tabelas_sync)
        # painel de espectador não tem estado -> pode ser registrado direto,
        # sobrevive a restart porque o custom_id bate
        self.bot.add_view(PainelEspectador(self))

    @commands.Cog.listener()
    async def on_ready(self):
        # só dá pra buscar emojis do app depois do login (precisa do
        # application_id, que só existe depois que o bot conecta) --
        # por isso isso não pode ficar no cog_load, que roda antes do login
        if not self._emojis_carregados:
            await self._carregar_emojis_do_portal()
            self._emojis_carregados = True

    async def _carregar_emojis_do_portal(self):
        """Busca os emojis do aplicativo (Developer Portal > Emojis) e preenche
        o dicionário EMOJIS pelas chaves em NOME_NO_PORTAL_PARA_CHAVE. Qualquer
        emoji que ainda não tiver sido subido lá simplesmente mantém o fallback
        unicode -- não trava o bot."""
        try:
            emojis_do_app = await self.bot.fetch_application_emojis()
        except discord.HTTPException as e:
            print(f"⚠️ Não consegui buscar emojis do Developer Portal: {e}")
            return

        encontrados = 0
        for emoji in emojis_do_app:
            chave = NOME_NO_PORTAL_PARA_CHAVE.get(emoji.name)
            if chave:
                EMOJIS[chave] = str(emoji)
                encontrados += 1
        print(f"✅ {encontrados}/{len(NOME_NO_PORTAL_PARA_CHAVE)} emojis carregados do Developer Portal.")

    async def publicar_painel_espectador(self, canal: discord.TextChannel):
        cfg = await asyncio.to_thread(_buscar_config_sync, canal.guild.id)
        cor = cfg["cor"] if cfg["cor"] is not None else COR_PADRAO
        painel = PainelEspectador(self, canal.guild, cor=cor)

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
        # FIX BUG REAL (".config duplicando"): mesmo cenário documentado em
        # dedupe.py/ranking.py/painelmediador.py -- o Discord às vezes
        # entrega a MESMA mensagem duas vezes pro on_message (reconexão de
        # gateway, app mobile). Sem essa trava, ".config" abria o painel
        # 2x seguidas pra um único comando digitado.
        if dedupe.ja_processado(ctx.message.id):
            return
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
        # mesma trava de duplicidade do ".config" acima -- sem isso, ".t"/
        # ".tela" numa redelivery de mensagem criava DUAS salas/painéis
        # pro mesmo comando.
        if dedupe.ja_processado(ctx.message.id):
            return
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

                titulo_reuniao = f"Verificação de Tela — {ctx.guild.name} — {codigo}"
                link_meet, evento_id = await google_meet.criar_reuniao(
                    titulo_reuniao, minutos_duracao=DURACAO_CALL_MINUTOS
                )

                await asyncio.to_thread(
                    _salvar_sessao_sync, sala, codigo, ctx.guild.id, ctx.author.id,
                    alvo.id if alvo else None, link_meet, evento_id,
                )

                cor = await _cor_da_guild(ctx.guild.id)
                painel = PainelTela(self, ctx.guild, sala, codigo, ctx.author.id, alvo, cor,
                                     link_meet, nome_espaco=evento_id)
                painel.message = await ctx.send(view=painel)
            except RuntimeError as e:
                # normalmente é falta de variável de ambiente do Google
                return await ctx.send(f"❌ {e}", delete_after=30)
            except Exception as e:
                import traceback
                traceback.print_exc()
                return await ctx.send(f"❌ Erro ao criar sala: `{type(e).__name__}: {e}`", delete_after=30)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tela(bot))
