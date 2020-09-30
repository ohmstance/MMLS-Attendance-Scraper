from datetime import date, datetime, timedelta
from discord.ext import commands
import discord
import asyncio

help_command = commands.DefaultHelpCommand(no_category = 'default')
bot = commands.Bot(command_prefix=commands.when_mentioned_or(), help_command=help_command)

@bot.event
async def on_ready():
    print(f'{datetime.now().isoformat()} Logged in as {bot.user}')
    for guild_num, guild in enumerate(bot.guilds, 1):
        print(f"{datetime.now().isoformat()} -> {guild.id}: {guild.name}")
    print(f"{datetime.now().isoformat()} Bot is in {guild_num} guilds.")

@bot.event
async def on_connect():
    print(f'{datetime.now().isoformat()} Bot connected.')

@bot.event
async def on_disconnect():
    print(f'{datetime.now().isoformat()} Bot disconnected.')

# @bot.check
# async def globally_block_dms(ctx):
#     return ctx.guild is not None

extensions = ('mmlscog', )

if __name__ == '__main__':
    for ext in extensions:
        bot.load_extension('mmlscog')
    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(bot.start('Your bot token here'))
    except KeyboardInterrupt:
        pass
    finally:
        for ext in extensions:
            bot.unload_extension(ext)
        loop.close()
