import discord
from discord.ext import commands
import aiohttp
import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MODEL = os.getenv("OLLAMA_MODEL")
OLLAMA_URL = "http://localhost:11434/api/chat"
PERSONALITIES_FILE = "personalities.json"
PREFIX = "/"
MAX_HISTORY = 60

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

channel_histories: dict[int, list] = {}
active_personalities: dict[int, str] = {}


# Personnalities

def load_personalities() -> dict:
    if Path(PERSONALITIES_FILE).exists():
        with open(PERSONALITIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"default": "You are a helpful Discord assistant."}

def save_personalities(data: dict):
    with open(PERSONALITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

personalities = load_personalities()

def get_system_prompt(guild_id: int) -> str:
    name = active_personalities.get(guild_id, "default")
    return personalities.get(name, personalities["default"])


# Ollama

async def query_ollama(messages: list) -> str:
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": MODEL,
            "messages": messages,
            "stream": False
        }
        async with session.post(OLLAMA_URL, json=payload) as resp:
            data = await resp.json()
            return data["message"]["content"]


# History

# role is either "user" or "assistant"
def add_to_history(channel_id: int, role: str, content: str):
    if channel_id not in channel_histories:
        channel_histories[channel_id] = []
    channel_histories[channel_id].append({"role": role, "content": content})
    if len(channel_histories[channel_id]) > MAX_HISTORY:
        channel_histories[channel_id] = channel_histories[channel_id][-MAX_HISTORY:]


# Events

@bot.event
async def on_ready():
    print(f"Connected as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    # Ignore bot messages
    if message.author.bot:
        return

    await bot.process_commands(message)

    # Save the message to the history
    add_to_history(
        message.channel.id,
        "user",
        f"{message.author.display_name}: {message.content}"
    )

    # Respond only if mentioned
    if bot.user not in message.mentions:
        return

    guild_id = message.guild.id if message.guild else 0
    system_prompt = get_system_prompt(guild_id)

    messages_payload = [{"role": "system", "content": system_prompt}] + channel_histories[message.channel.id]

    async with message.channel.typing():
        try:
            response = await query_ollama(messages_payload)
        except Exception as e:
            await message.reply(f"Error with Ollama : {e}")
            return

    add_to_history(message.channel.id, "assistant", response)
    await message.reply(response)


# Commands

@bot.command(name="add_personality")
@commands.has_permissions(manage_guild=True)
async def add_personality(ctx: commands.Context, name: str, *, prompt: str):
    """Create or modify a personality. Usage : !add_personality <name> <prompt>"""
    personalities[name] = prompt
    save_personalities(personalities)
    await ctx.send(f"Personality `{name}` saved.")

@bot.command(name="use_personality")
async def use_personality(ctx: commands.Context, name: str):
    """Activate a personality on this server. Usage : !use_personality <name>"""
    if name not in personalities:
        liste = ", ".join(f"`{k}`" for k in personalities.keys())
        await ctx.send(f"Personality not found. Available : {liste}")
        return
    active_personalities[ctx.guild.id] = name
    await ctx.send(f"Personality `{name}` activated.")

@bot.command(name="list_personalities")
async def list_personalities(ctx: commands.Context):
    """List available personalities."""
    current = active_personalities.get(ctx.guild.id, "default")
    liste = ", ".join(f"`{k}`" for k in personalities.keys())
    await ctx.send(f"Available : {liste}\nActive : `{current}`")

@bot.command(name="clear_history")
@commands.has_permissions(manage_messages=True)
async def clear_history(ctx: commands.Context):
    """Clear the context history of the channel."""
    channel_histories.pop(ctx.channel.id, None)
    await ctx.send("History cleared.")

# Start the bot
if __name__ == "__main__":
    bot.run(TOKEN)