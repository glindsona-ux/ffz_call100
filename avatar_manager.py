"""
========================================================================
 AVATAR_MANAGER.PY — FOTO E BANNER DO BOT POR SERVIDOR  (versão FFZ Call)
========================================================================
Comando: /avatarbot  (só administrador do servidor)

Troca a foto de perfil e o banner do bot SÓ no servidor onde o comando é
usado (perfil do bot dentro do servidor), sem mexer no perfil global nem nos
outros servidores. Usa PATCH /guilds/{guild_id}/members/@me via bot.http.

Uso:
  • /avatarbot                      -> abre o painel (botões de foto e banner)
  • /avatarbot imagem:<anexo>       -> troca a foto direto
  • /avatarbot banner:<anexo>       -> troca o banner direto
  • os dois juntos também funcionam

Guarda só o horário da última troca por servidor (arquivo ffz_data.db, o
mesmo do painel_tela), pra aplicar cooldown e evitar 429 do Discord.

Observação: o banner por servidor depende do Discord liberar o recurso pra
conta de bot. Se ele recusar, o bot avisa com mensagem clara (a foto continua
funcionando normal).
========================================================================
"""

import base64
import sqlite3
import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands, ui
from discord.ext import commands
from discord.http import Route

DB_PATH = "ffz_data.db"
COOLDOWN_MINUTOS = 10          # evita 429 do Discord entre trocas
LIMITE_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------- banco (sqlite em thread)
def _garantir_tabela_sync():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS avatar_bot_config (
            guild_id INTEGER PRIMARY KEY,
            ultima_troca TEXT,
            ultima_troca_banner TEXT
        )
    """)
    conn.commit()
    conn.close()


def _ultima_troca_sync(guild_id: int, tipo: str):
    coluna = "ultima_troca" if tipo == "avatar" else "ultima_troca_banner"
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(f"SELECT {coluna} FROM avatar_bot_config WHERE guild_id = ?", (guild_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def _registrar_troca_sync(guild_id: int, tipo: str):
    coluna = "ultima_troca" if tipo == "avatar" else "ultima_troca_banner"
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        f"INSERT INTO avatar_bot_config (guild_id, {coluna}) VALUES (?, ?) "
        f"ON CONFLICT(guild_id) DO UPDATE SET {coluna} = excluded.{coluna}",
        (guild_id, agora),
    )
    conn.commit()
    conn.close()


async def _checar_cooldown(guild_id: int, tipo: str = "avatar") -> tuple[bool, int]:
    """(pode_trocar, minutos_restantes). Avatar e banner têm cooldowns independentes."""
    ultima = await asyncio.to_thread(_ultima_troca_sync, guild_id, tipo)
    if not ultima:
        return True, 0
    liberado_em = datetime.strptime(ultima, "%Y-%m-%d %H:%M:%S") + timedelta(minutes=COOLDOWN_MINUTOS)
    agora = datetime.now()
    if agora >= liberado_em:
        return True, 0
    return False, int((liberado_em - agora).total_seconds() // 60) + 1


async def _registrar_troca(guild_id: int, tipo: str = "avatar"):
    await asyncio.to_thread(_registrar_troca_sync, guild_id, tipo)


# ---------------------------------------------------------------- utilidades
def _limpar_url(url: str) -> str | None:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def _data_uri(image_bytes: bytes) -> str:
    """Imagem -> 'data:image/...;base64,...' (formato que a API do Discord espera)."""
    if image_bytes.startswith(b"\x89PNG"):
        mime = "image/png"
    elif image_bytes.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif image_bytes.startswith(b"GIF8"):
        mime = "image/gif"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"


async def _checar_admin(interaction: discord.Interaction) -> bool:
    """Só administrador do servidor usa o /avatarbot."""
    if interaction.guild and interaction.user.guild_permissions.administrator:
        return True
    msg = "❌ Só administradores do servidor podem usar esse comando."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    return False


async def _baixar_imagem(url: str) -> tuple[bytes | None, str | None]:
    """(bytes, None) se deu certo; (None, mensagem_de_erro) se não."""
    import aiohttp  # já vem junto com o discord.py
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as resp:
                if resp.status != 200:
                    return None, f"❌ Não consegui baixar essa imagem (status {resp.status}). Verifica o link."
                return await resp.read(), None
    except Exception as e:
        return None, f"❌ Erro ao baixar a imagem: {e}"


async def _patch_perfil_guild(bot: commands.Bot, guild_id: int, campo: str, image_bytes: bytes | None):
    """campo: 'avatar' ou 'banner'. image_bytes=None remove o customizado do servidor."""
    valor = _data_uri(image_bytes) if image_bytes else None
    route = Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id)
    return await bot.http.request(route, json={campo: valor})


# ---------------------------------------------------------------- aplicar troca
async def _aplicar(interaction: discord.Interaction, image_bytes: bytes, tipo: str) -> bool:
    """Valida cooldown e tamanho, aplica a troca e avisa se der erro. A interação
    já precisa estar deferida (ephemeral). Retorna True se aplicou."""
    nome = "a foto" if tipo == "avatar" else "o banner"
    pode, minutos = await _checar_cooldown(interaction.guild_id, tipo)
    if not pode:
        await interaction.followup.send(
            f"⏱️ Calma! Você já trocou {nome} recentemente. Tenta de novo em **{minutos} min**.",
            ephemeral=True,
        )
        return False

    if len(image_bytes) > LIMITE_BYTES:
        await interaction.followup.send("❌ Imagem muito grande (máx 10MB pro Discord aceitar).", ephemeral=True)
        return False

    try:
        await _patch_perfil_guild(interaction.client, interaction.guild_id, tipo, image_bytes)
        await _registrar_troca(interaction.guild_id, tipo)
    except discord.HTTPException as e:
        if e.status == 429:
            msg = "❌ O Discord recusou por limite de trocas (429). Aguarda alguns minutos e tenta de novo."
        elif tipo == "banner" and e.status in (400, 403):
            msg = ("❌ O Discord recusou a troca de banner (esse recurso pode ainda não estar liberado "
                   "pra conta de bot). A foto continua funcionando normal.")
        else:
            msg = f"❌ Erro do Discord ao trocar {nome}: {e}"
        await interaction.followup.send(msg, ephemeral=True)
        return False
    except Exception as e:
        await interaction.followup.send(f"❌ Erro inesperado: {e}", ephemeral=True)
        return False
    return True


async def _restaurar(interaction: discord.Interaction, tipo: str):
    """Volta foto/banner do servidor pro padrão global do bot."""
    pode, minutos = await _checar_cooldown(interaction.guild_id, tipo)
    if not pode:
        return await interaction.response.send_message(
            f"⏱️ Calma! Tenta de novo em **{minutos} min**.", ephemeral=True
        )
    await interaction.response.defer(ephemeral=True)
    try:
        await _patch_perfil_guild(interaction.client, interaction.guild_id, tipo, None)
        await _registrar_troca(interaction.guild_id, tipo)
    except discord.HTTPException as e:
        return await interaction.followup.send(f"❌ Erro do Discord: {e}", ephemeral=True)

    nome = "Avatar" if tipo == "avatar" else "Banner"
    await interaction.followup.send(f"✅ {nome} deste servidor restaurado pro padrão.", ephemeral=True)
    await _atualizar_painel(interaction)


async def _atualizar_painel(interaction: discord.Interaction):
    try:
        await interaction.message.edit(embed=_embed_painel(interaction.guild), view=PainelAvatar())
    except (discord.HTTPException, AttributeError):
        pass


# ---------------------------------------------------------------- painel
def _embed_painel(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="Foto e Banner do Bot — Este Servidor",
        description=(
            "Troque a foto de perfil **e o banner** do bot só neste servidor, sem mexer "
            "no que aparece no resto do Discord.\n\n"
            "**Foto de perfil:**\n"
            "• Rode `/avatarbot` de novo anexando uma imagem direto (galeria/câmera), **ou**\n"
            "• Clique em **Enviar Foto por Link** abaixo\n\n"
            "**Banner:**\n"
            "• Clique em **Enviar Banner por Link** abaixo (recomendado 16:9)\n\n"
            f"Cooldown entre trocas: **{COOLDOWN_MINUTOS} minutos** (limite do próprio Discord; "
            "foto e banner têm cooldown independente)."
        ),
        color=0x5865F2,
    )
    if guild.me.display_avatar:
        embed.set_thumbnail(url=guild.me.display_avatar.url)
    banner = getattr(guild.me, "guild_banner", None) or getattr(guild.me, "banner", None)
    if banner:
        embed.set_image(url=banner.url)
    embed.set_footer(text=f"{guild.name}")
    return embed


class _ModalImagem(ui.Modal):
    """Base dos modais de link: baixa a imagem e aplica (avatar ou banner)."""
    tipo = "avatar"
    url_imagem: ui.TextInput

    async def on_submit(self, interaction: discord.Interaction):
        url = _limpar_url(self.url_imagem.value)
        if not url:
            return await interaction.response.send_message(
                "❌ Link inválido. Precisa ser um link direto começando com http:// ou https://",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)

        image_bytes, erro = await _baixar_imagem(url)
        if erro:
            return await interaction.followup.send(erro, ephemeral=True)
        if not await _aplicar(interaction, image_bytes, self.tipo):
            return

        texto = ("✅ Foto de perfil atualizada só neste servidor!" if self.tipo == "avatar"
                 else "✅ Banner atualizado só neste servidor!")
        await interaction.followup.send(texto, ephemeral=True)
        await _atualizar_painel(interaction)


class ModalNovoAvatar(_ModalImagem, title="Nova Foto de Perfil"):
    tipo = "avatar"
    url_imagem = ui.TextInput(
        label="Link direto da imagem",
        placeholder="https://exemplo.com/imagem.png",
        max_length=500,
    )


class ModalNovoBanner(_ModalImagem, title="Novo Banner"):
    tipo = "banner"
    url_imagem = ui.TextInput(
        label="Link direto da imagem (recomendado 16:9)",
        placeholder="https://exemplo.com/banner.png",
        max_length=500,
    )


class PainelAvatar(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # o painel é ephemeral (só quem rodou o comando vê), mas confere de novo
        return await _checar_admin(interaction)

    @ui.button(label="Enviar Foto por Link", style=discord.ButtonStyle.blurple, custom_id="avatar_enviar", row=0)
    async def btn_enviar(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ModalNovoAvatar())

    @ui.button(label="Restaurar Foto Padrão", style=discord.ButtonStyle.grey, custom_id="avatar_resetar", row=0)
    async def btn_resetar(self, interaction: discord.Interaction, button: ui.Button):
        await _restaurar(interaction, "avatar")

    @ui.button(label="Enviar Banner por Link", style=discord.ButtonStyle.blurple, custom_id="banner_enviar", row=1)
    async def btn_enviar_banner(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(ModalNovoBanner())

    @ui.button(label="Restaurar Banner Padrão", style=discord.ButtonStyle.grey, custom_id="banner_resetar", row=1)
    async def btn_resetar_banner(self, interaction: discord.Interaction, button: ui.Button):
        await _restaurar(interaction, "banner")


# ---------------------------------------------------------------- comando
class AvatarManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="avatarbot",
        description="Troca ou abre o painel da foto de perfil e do banner do bot neste servidor",
    )
    @app_commands.describe(
        imagem="Anexe uma imagem pra trocar a FOTO DE PERFIL direto. Deixe vazio pra abrir o painel.",
        banner="Anexe uma imagem pra trocar o BANNER direto (pode usar junto com 'imagem').",
    )
    @app_commands.guild_only()
    async def avatarbot(self, interaction: discord.Interaction,
                        imagem: discord.Attachment = None, banner: discord.Attachment = None):
        if not await _checar_admin(interaction):
            return

        if imagem is None and banner is None:
            return await interaction.response.send_message(
                embed=_embed_painel(interaction.guild), view=PainelAvatar(), ephemeral=True
            )

        for anexo, rotulo in ((imagem, "foto de perfil"), (banner, "banner")):
            if anexo is not None and not (anexo.content_type or "").startswith("image/"):
                return await interaction.response.send_message(
                    f"❌ O anexo de {rotulo} não parece ser uma imagem. Anexa um png, jpg ou webp.",
                    ephemeral=True,
                )

        await interaction.response.defer(ephemeral=True)
        resultados = []
        if imagem is not None and await _aplicar(interaction, await imagem.read(), "avatar"):
            resultados.append("✅ Foto de perfil atualizada")
        if banner is not None and await _aplicar(interaction, await banner.read(), "banner"):
            resultados.append("✅ Banner atualizado")
        if resultados:
            await interaction.followup.send(" e ".join(resultados) + " só neste servidor!", ephemeral=True)


async def setup(bot):
    await asyncio.to_thread(_garantir_tabela_sync)
    bot.add_view(PainelAvatar())   # botões do painel continuam funcionando após restart
    await bot.add_cog(AvatarManager(bot))
