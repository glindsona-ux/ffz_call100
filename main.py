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


async def main():
    async with bot:
        await bot.load_extension("painel_tela")
        await bot.start(os.getenv("DISCORD_TOKEN"))


if __name__ == "__main__":
    asyncio.run(main())
