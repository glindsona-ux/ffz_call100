import os
import asyncio
import discord
from discord.ext import commands

from keep_alive import keep_alive

intents = discord.Intents.default()
intents.message_content = True  # necessário pra ler comando de prefixo (!t, !tela)
intents.members = True          # necessário pra resolver @menção de membro

bot = commands.Bot(command_prefix="!", intents=intents)

EXTENSOES = ("painel_tela", "avatar_manager")


async def _setup_hook():
    # carrega os módulos e registra os slash commands (/avatarbot) no Discord.
    # O sync global pode levar um tempo pra aparecer em todos os servidores.
    for ext in EXTENSOES:
        await bot.load_extension(ext)
    try:
        sincronizados = await bot.tree.sync()
        print(f"Slash commands sincronizados: {[c.name for c in sincronizados]}")
    except discord.HTTPException as e:
        print(f"Falha ao sincronizar slash commands: {e}")


bot.setup_hook = _setup_hook


@bot.event
async def on_ready():
    print(f"Logado como {bot.user} ({bot.user.id})")


@bot.event
async def on_command_error(ctx, error):
    # rede de segurança: garante que erro nunca fica só no console --
    # sempre aparece pro usuário no chat, mesmo que o comando não trate.
    if isinstance(error, (commands.CommandNotFound,)):
        return
    import traceback
    traceback.print_exception(type(error), error, error.__traceback__)
    try:
        await ctx.send(f"❌ Erro inesperado: `{type(error).__name__}: {error}`", delete_after=30)
    except discord.HTTPException:
        pass


async def main():
    async with bot:
        await bot.start(os.getenv("DISCORD_TOKEN"))


if __name__ == "__main__":
    keep_alive()  # sobe o servidor web (Render/UptimeRobot) antes do bot
    asyncio.run(main())
