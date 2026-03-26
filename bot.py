import discord
from discord.ext import commands
import aiohttp
import json
import os
import logging
from pathlib import Path
from dotenv import load_dotenv
import random
from datetime import timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL")
OLLAMA_URL = "http://localhost:11434/api/chat"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PERSONALITIES_FILE = Path(__file__).parent / "personalities.json"
ACTIVE_PERSONALITIES_FILE = Path(__file__).parent / "active_personalities.json"
SERVER_PROMPT_FILE = Path(__file__).parent / "server_prompt.txt"
PREFIX = "/"
MAX_HISTORY = 60

USE_OLLAMA = bool(OLLAMA_MODEL)

if USE_OLLAMA:
    logger.info(f"Backend: Ollama (model={OLLAMA_MODEL})")
elif GROQ_API_KEY:
    logger.info(f"Backend: Groq (model={GROQ_MODEL})")
else:
    raise RuntimeError("No AI backend configured: set OLLAMA_MODEL or GROQ_API_KEY in .env")

class SilentTree(discord.app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        if isinstance(error.__cause__, discord.NotFound) and error.__cause__.code == 10062:
            return
        await super().on_error(interaction, error)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, tree_cls=SilentTree)

channel_histories: dict[int, list] = {}

def load_active_personalities() -> dict[int, str]:
    if ACTIVE_PERSONALITIES_FILE.exists():
        with open(ACTIVE_PERSONALITIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): v for k, v in data.items()}
    return {}

def save_active_personalities(data: dict[int, str]):
    with open(ACTIVE_PERSONALITIES_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in data.items()}, f, indent=2)

active_personalities: dict[int, str] = load_active_personalities()

def load_server_prompt() -> str:
    if SERVER_PROMPT_FILE.exists():
        prompt = SERVER_PROMPT_FILE.read_text(encoding="utf-8").strip()
        logger.info(f"Server prompt loaded ({len(prompt)} chars)")
        return prompt
    logger.info("No server_prompt.txt found, skipping")
    return ""

SERVER_PROMPT = load_server_prompt()


# Personnalities

def load_personalities() -> dict:
    if Path(PERSONALITIES_FILE).exists():
        with open(PERSONALITIES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info(f"Loaded {len(data)} personalities: {list(data.keys())}")
        return data
    logger.warning(f"{PERSONALITIES_FILE} not found, using default personality")
    return {"default": "You are a helpful Discord assistant."}

def save_personalities(data: dict):
    with open(PERSONALITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {len(data)} personalities to {PERSONALITIES_FILE}")

personalities = load_personalities()

def get_system_prompt(guild_id: int) -> str:
    name = active_personalities.get(guild_id, "default")
    personality = personalities.get(name, personalities["default"])
    if SERVER_PROMPT:
        return f"{SERVER_PROMPT}\n\n{personality}"
    return personality


# AI backends

async def query_ollama(messages: list) -> str:
    logger.info(f"Querying Ollama (model={OLLAMA_MODEL}, {len(messages)} messages)")
    async with aiohttp.ClientSession() as session:
        payload = {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "stream": False
        }
        async with session.post(OLLAMA_URL, json=payload) as resp:
            data = await resp.json()
            logger.debug(f"Ollama raw response: {data}")
            if "message" not in data:
                logger.error(f"Unexpected Ollama response structure: {data}")
                raise ValueError(f"Unexpected response: {data}")
            response = data["message"]["content"]
            if not response:
                logger.error(f"Ollama returned empty content: {data}")
                raise ValueError("Empty response from Ollama")
            logger.info(f"Ollama response received ({len(response)} chars)")
            return response

async def query_groq(messages: list) -> str:
    logger.info(f"Querying Groq (model={GROQ_MODEL}, {len(messages)} messages)")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(GROQ_URL, json=payload, headers=headers) as resp:
            data = await resp.json()
            logger.debug(f"Groq raw response: {data}")
            if "choices" not in data:
                logger.error(f"Unexpected Groq response structure: {data}")
                raise ValueError(f"Unexpected response: {data}")
            choice = data["choices"][0]
            response = choice["message"]["content"]
            if not response:
                finish_reason = choice.get("finish_reason", "unknown")
                logger.error(f"Groq returned empty content (finish_reason={finish_reason})")
                raise ValueError(f"Empty response from Groq (finish_reason={finish_reason})")
            logger.info(f"Groq response received ({len(response)} chars)")
            return response

async def query_ai(messages: list) -> str:
    if USE_OLLAMA:
        return await query_ollama(messages)
    return await query_groq(messages)


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
    await bot.tree.sync()
    logger.info(f"Connected as {bot.user} (ID: {bot.user.id}), slash commands synced")

@bot.event
async def on_message(message: discord.Message):
    # Ignore bot messages
    if message.author.bot:
        return

    await bot.process_commands(message)

    # Save the message to the history
    author_tag = f"{message.author.display_name} (@{message.author.name})"
    if message.reference and isinstance(message.reference.resolved, discord.Message):
        ref = message.reference.resolved
        ref_tag = f"{ref.author.display_name} (@{ref.author.name})"
        entry = f'{author_tag} [en réponse à {ref_tag}: "{ref.content[:150]}"]: {message.content}'
    else:
        entry = f"{author_tag}: {message.content}"
    add_to_history(message.channel.id, "user", entry)

    # Respond only if mentioned or if random chance (20%)
    if (bot.user not in message.mentions) and (random.randint(1, 10) > 2):
        return

    guild_id = message.guild.id if message.guild else 0
    system_prompt = get_system_prompt(guild_id)
    logger.info(f"Mention from {message.author} in #{message.channel} (guild={guild_id})")

    messages_payload = [{"role": "system", "content": system_prompt}] + channel_histories[message.channel.id]

    async with message.channel.typing():
        try:
            response = await query_ai(messages_payload)
        except Exception as e:
            logger.error(f"AI backend error: {e}")
            await message.reply(f"Error : {e}")
            return

    if not response or not response.strip():
        logger.warning("AI returned an empty response, skipping reply.")
        return

    try:
        response_data = json.loads(response)
        response_content = response_data.get("reply", response)
    except json.JSONDecodeError:
        response_content = response
        response_data = {}

    mute_data = response_data.get("mute")
    if mute_data and message.guild:
        username = mute_data.get("user", "").lstrip("@")
        reason = mute_data.get("reason", "mute par le bot")
        member = discord.utils.find(lambda m: m.name == username, message.guild.members)
        if member:
            try:
                await member.timeout(timedelta(minutes=1), reason=reason)
                logger.info(f"Muted {member} for 1min — reason: {reason}")
            except discord.Forbidden:
                logger.warning(f"Missing permission to mute {member}")
        else:
            logger.warning(f"Mute requested but user '{username}' not found in guild")

    add_to_history(message.channel.id, "assistant", response_content)
    await message.reply(response_content)


# Commands

@bot.tree.command(name="add_personality", description="Create or modify a personality")
@discord.app_commands.describe(name="Personality name", prompt="System prompt for this personality")
@discord.app_commands.checks.has_permissions(manage_guild=True)
async def add_personality(interaction: discord.Interaction, name: str, prompt: str):
    logger.info(f"{interaction.user} added/updated personality '{name}' in guild {interaction.guild_id}")
    personalities[name] = prompt
    save_personalities(personalities)
    await interaction.response.send_message(f"Personality `{name}` saved.")

@bot.tree.command(name="use_personality", description="Activate a personality on this server")
@discord.app_commands.describe(name="Personality name to activate")
async def use_personality(interaction: discord.Interaction, name: str):
    if name not in personalities:
        logger.warning(f"{interaction.user} tried unknown personality '{name}' in guild {interaction.guild_id}")
        await interaction.response.send_message(f"Unknown personality `{name}`.", ephemeral=True)
        return
    active_personalities[interaction.guild_id] = name
    save_active_personalities(active_personalities)
    logger.info(f"{interaction.user} activated personality '{name}' in guild {interaction.guild_id}")
    await interaction.response.send_message(f"Personality `{name}` activated.")

@use_personality.autocomplete("name")
async def use_personality_autocomplete(interaction: discord.Interaction, current: str):
    return [
        discord.app_commands.Choice(name=k, value=k)
        for k in personalities
        if current.lower() in k.lower()
    ][:25]


@bot.tree.command(name="list_personalities", description="List available personalities")
async def list_personalities(interaction: discord.Interaction):
    current = active_personalities.get(interaction.guild_id, "default")
    liste = ", ".join(f"`{k}`" for k in personalities.keys())
    logger.info(f"{interaction.user} listed personalities in guild {interaction.guild_id} (active={current})")
    await interaction.response.send_message(f"Available : {liste}\nActive : `{current}`")

@bot.tree.command(name="clear_history", description="Clear the context history of this channel")
@discord.app_commands.checks.has_permissions(manage_messages=True)
async def clear_history(interaction: discord.Interaction):
    logger.info(f"{interaction.user} cleared history in #{interaction.channel} (guild={interaction.guild_id})")
    channel_histories.pop(interaction.channel_id, None)
    await interaction.response.send_message("History cleared.")

# Start the bot
if __name__ == "__main__":
    bot.run(TOKEN)