import os
import asyncio
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True  # necessário pra ler comando de prefixo (!t, !tela)
intents.members = True          # necessário pra resolver @menção de membro

bot = commands.Bot(command_prefix="!", intents=intents)


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
        await bot.load_extension("painel_tela")
        await bot.start(os.getenv("DISCORD_TOKEN"))


if __name__ == "__main__":
    asyncio.run(main())
