import discord
from discord.ext import commands
import aiohttp
import json
import os
import asyncio
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
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL")
OLLAMA_URL = "http://localhost:11434/api/chat"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
PERSONALITIES_FILE = Path(__file__).parent / "personalities.json"
ACTIVE_PERSONALITIES_FILE = Path(__file__).parent / "active_personalities.json"
SERVER_PROMPT_FILE = Path(__file__).parent / "server_prompt.txt"
PREFIX = "/"
MAX_HISTORY = 60
SOCKET_PATH = "/tmp/knapikette.sock"

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
cli_clients: set = set()

MESSAGE_CACHE_SIZE = 200
message_cache: dict[int, discord.Message] = {}
_message_cache_order: list[int] = []

def cache_message(msg: discord.Message):
    if msg.id not in message_cache:
        _message_cache_order.append(msg.id)
        if len(_message_cache_order) > MESSAGE_CACHE_SIZE:
            old_id = _message_cache_order.pop(0)
            message_cache.pop(old_id, None)
    message_cache[msg.id] = msg


# CLI socket server

async def handle_cli_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    cli_clients.add(writer)
    # Send channels + members on connect
    try:
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    writer.write(f"CHANNEL|{channel.id}|{channel.name}|{guild.id}\n".encode())
            for member in guild.members:
                if not member.bot:
                    writer.write(f"MEMBER|{member.id}|{member.display_name}|{member.name}|{guild.id}\n".encode())
                    writer.write(f"PRESENCE|{member.id}|{str(member.status)}\n".encode())
        await writer.drain()
    except Exception as e:
        logger.warning(f"[CLI] Could not send initial data: {e}")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode(errors="replace").rstrip()
            if cmd.startswith("SEND|"):
                parts = cmd.split("|", 2)
                if len(parts) == 3:
                    _, channel_id_str, content = parts
                    try:
                        channel = bot.get_channel(int(channel_id_str))
                        if channel:
                            has_everyone = "@everyone" in content or "@here" in content
                            sent = await channel.send(
                                content,
                                allowed_mentions=discord.AllowedMentions(
                                    everyone=has_everyone, users=True, roles=True
                                ),
                            )
                            add_to_history(int(channel_id_str), "assistant", content)
                            logger.info(f"[CLI] Sent to #{channel.name}: {content[:60]}")
                            asyncio.create_task(broadcast_to_cli(
                                f"MSG|{channel.id}|{channel.name}|{bot.user.display_name}|{sent.id}||{content}"
                            ))
                    except Exception as e:
                        logger.error(f"[CLI] Send error: {e}")
            elif cmd.startswith("CMD|"):
                parts = cmd.split("|", 3)
                command = parts[1] if len(parts) > 1 else ""
                reply = ""
                if command == "clear_history":
                    channel_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                    if channel_id:
                        channel_histories.pop(channel_id, None)
                        reply = "[OK] Historique effacé"
                    else:
                        reply = "[Erreur] channel_id manquant"
                elif command == "shut_up":
                    global bot_muted
                    bot_muted = True
                    reply = "[OK] Bot silencieux"
                elif command == "unshut_up":
                    bot_muted = False
                    reply = "[OK] Bot peut parler à nouveau"
                elif command == "list_personalities":
                    guild_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                    current = active_personalities.get(guild_id, "default")
                    reply = f"Disponibles : {', '.join(personalities.keys())} | Active : {current}"
                elif command == "use_personality":
                    if len(parts) == 4:
                        guild_id_str, name = parts[2], parts[3]
                        gid = int(guild_id_str) if guild_id_str.isdigit() else 0
                        if name in personalities:
                            active_personalities[gid] = name
                            save_active_personalities(active_personalities)
                            reply = f"[OK] Personnalité '{name}' activée"
                        else:
                            reply = f"[Erreur] Personnalité inconnue : {name}"
                    else:
                        reply = "[Erreur] Usage : /use_personality <nom>"
                else:
                    reply = f"[Erreur] Commande inconnue : {command}"
                writer.write(f"REPLY|{reply}\n".encode())
                await writer.drain()
            elif cmd.startswith("REPLY_TO|"):
                parts = cmd.split("|", 3)
                if len(parts) == 4:
                    _, channel_id_str, message_id_str, content = parts
                    try:
                        channel = bot.get_channel(int(channel_id_str))
                        if channel:
                            msg = message_cache.get(int(message_id_str))
                            if msg is None:
                                msg = await channel.fetch_message(int(message_id_str))
                            has_everyone = "@everyone" in content or "@here" in content
                            sent_reply = await msg.reply(
                                content,
                                allowed_mentions=discord.AllowedMentions(
                                    everyone=has_everyone, users=True, roles=True
                                ),
                            )
                            add_to_history(int(channel_id_str), "assistant", content)
                            logger.info(f"[CLI] Replied to {message_id_str} in #{channel.name}: {content[:60]}")
                            asyncio.create_task(broadcast_to_cli(
                                f"MSG|{channel.id}|{channel.name}|{bot.user.display_name}|{sent_reply.id}|{message_id_str}|{content}"
                            ))
                    except Exception as e:
                        logger.error(f"[CLI] Reply error: {e}")
                        writer.write(f"REPLY|[Erreur] {e}\n".encode())
                        await writer.drain()
            elif cmd.startswith("REACT|"):
                parts = cmd.split("|", 3)
                if len(parts) == 4:
                    _, channel_id_str, message_id_str, emoji = parts
                    try:
                        channel = bot.get_channel(int(channel_id_str))
                        if channel:
                            msg = message_cache.get(int(message_id_str))
                            if msg is None:
                                msg = await channel.fetch_message(int(message_id_str))
                            await msg.add_reaction(emoji)
                            logger.info(f"[CLI] Reacted {emoji} to {message_id_str}")
                    except Exception as e:
                        logger.error(f"[CLI] React error: {e}")
                        writer.write(f"REPLY|[Erreur] {e}\n".encode())
                        await writer.drain()
    finally:
        cli_clients.discard(writer)
        try:
            writer.close()
        except Exception:
            pass

async def start_cli_server():
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = await asyncio.start_unix_server(handle_cli_client, SOCKET_PATH)
    logger.info(f"CLI socket ready at {SOCKET_PATH}")
    async with server:
        await server.serve_forever()

async def broadcast_to_cli(msg: str):
    if not cli_clients:
        return
    dead = set()
    for writer in list(cli_clients):
        try:
            writer.write((msg + "\n").encode())
            await writer.drain()
        except Exception:
            dead.add(writer)
    cli_clients.difference_update(dead)

async def _auto_react(message: discord.Message, system_prompt: str):
    prompt = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message.content},
    ]
    try:
        raw = (await query_ai(prompt)).strip()
        data = json.loads(raw)
        emoji = data.get("reaction", "")
        if emoji:
            await message.add_reaction(emoji)
            logger.info(f"[AutoReact] {emoji} in #{getattr(message.channel, 'name', 'dm')}")
    except Exception as e:
        logger.warning(f"[AutoReact] Failed: {e}")

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

async def _query_groq_model(session: aiohttp.ClientSession, model: str, messages: list) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages}
    async with session.post(GROQ_URL, json=payload, headers=headers) as resp:
        if resp.status in (429, 503):
            raise RateLimitError(model)
        data = await resp.json()
        logger.debug(f"Groq raw response: {data}")
        if "choices" not in data:
            error = data.get("error", {})
            error_msg = error.get("message", "") if isinstance(error, dict) else str(error)
            if "over capacity" in error_msg.lower() or "overloaded" in error_msg.lower():
                raise RateLimitError(model)
            logger.error(f"Unexpected Groq response structure: {data}")
            raise ValueError(f"Unexpected response: {data}")
        choice = data["choices"][0]
        response = choice["message"]["content"]
        if not response:
            finish_reason = choice.get("finish_reason", "unknown")
            logger.error(f"Groq returned empty content (finish_reason={finish_reason}), trying fallback")
            raise RateLimitError(model)
        return response

class RateLimitError(Exception):
    def __init__(self, model: str):
        super().__init__(f"Rate limit reached for model '{model}'")
        self.model = model

async def query_groq(messages: list) -> str:
    async with aiohttp.ClientSession() as session:
        logger.info(f"Querying Groq (model={GROQ_MODEL}, {len(messages)} messages)")
        try:
            response = await _query_groq_model(session, GROQ_MODEL, messages)
            logger.info(f"Groq response received ({len(response)} chars)")
            return response
        except RateLimitError:
            if not GROQ_FALLBACK_MODEL:
                raise
            logger.warning(f"Rate limit on {GROQ_MODEL}, falling back to {GROQ_FALLBACK_MODEL}")
            response = await _query_groq_model(session, GROQ_FALLBACK_MODEL, messages)
            logger.info(f"Groq fallback response received ({len(response)} chars)")
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

_cli_server_started = False

@bot.event
async def on_ready():
    global _cli_server_started
    await bot.tree.sync()
    logger.info(f"Connected as {bot.user} (ID: {bot.user.id}), slash commands synced")
    if not _cli_server_started:
        _cli_server_started = True
        asyncio.create_task(start_cli_server())

bot_muted = False

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
    channel_name = getattr(message.channel, "name", "dm")
    cache_message(message)
    cli_content = message.content
    extras = []
    for att in message.attachments:
        ct = att.content_type or ""
        if ct.startswith("image/"):
            extras.append(f"[Image: {att.filename}]")
        elif ct.startswith("video/"):
            extras.append(f"[Vidéo: {att.filename}]")
        elif ct.startswith("audio/"):
            extras.append(f"[Audio: {att.filename}]")
        else:
            extras.append(f"[Fichier: {att.filename}]")
    for sticker in message.stickers:
        extras.append(f"[Autocollant: {sticker.name}]")
    if extras:
        cli_content = (cli_content + " " if cli_content else "") + " ".join(extras)
    ref_id = ""
    if message.reference and isinstance(message.reference.resolved, discord.Message):
        ref_id = str(message.reference.resolved.id)
    asyncio.create_task(broadcast_to_cli(
        f"MSG|{message.channel.id}|{channel_name}|{message.author.display_name}|{message.id}|{ref_id}|{cli_content}"
    ))
    for att in message.attachments:
        if (att.content_type or "").startswith("image/"):
            asyncio.create_task(broadcast_to_cli(
                f"IMAGE|{message.channel.id}|{message.id}|{att.url}"
            ))
    for embed in message.embeds:
        if embed.type in ("gifv", "image"):
            media = embed.image or embed.thumbnail
            img_url = media.proxy_url if media else None
            if img_url:
                asyncio.create_task(broadcast_to_cli(
                    f"IMAGE|{message.channel.id}|{message.id}|{img_url}"
                ))

    guild_id = message.guild.id if message.guild else 0
    will_reply = (bot.user in message.mentions) or (not bot_muted and random.randint(1, 10) == 1)

    if not will_reply and message.content and random.randint(1, 10) <= 1:
        asyncio.create_task(_auto_react(message, get_system_prompt(guild_id)))

    if not will_reply:
        return

    system_prompt = get_system_prompt(guild_id)
    logger.info(f"Mention from {message.author} in #{message.channel} (guild={guild_id})")

    history_note = (
        "L'historique ci-dessous est la conversation du salon Discord. "
        "Chaque message est au format \"Pseudo (@username): contenu\". "
        "Plusieurs personnes différentes peuvent parler. "
        "Tu participes à cette conversation et tu peux répondre même si le dernier message ne t'était pas directement adressé."
    )

    if message.guild and hasattr(message.channel, "members"):
        members_list = ", ".join(
            f"Pseudo : {m.display_name} | Tag : <@{m.id}>"
            for m in message.channel.members
            if not m.bot
        )
        full_system_prompt = f"{system_prompt}\n\n{history_note}\n\nMembres présents dans ce salon : {members_list}"
    else:
        full_system_prompt = f"{system_prompt}\n\n{history_note}"

    messages_payload = [{"role": "system", "content": full_system_prompt}] + channel_histories[message.channel.id]

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

    def find_member(raw: str):
        uid = raw.lstrip("@<").rstrip(">").strip()
        return (
            discord.utils.find(lambda m: str(m.id) == uid, message.guild.members) or
            discord.utils.find(lambda m: m.name == uid, message.guild.members) or
            discord.utils.find(lambda m: m.display_name == uid, message.guild.members)
        )

    mute_data = response_data.get("mute")
    if mute_data and message.guild:
        reason = mute_data.get("reason", "mute par le bot")
        member = find_member(mute_data.get("user", ""))
        if member:
            try:
                await member.timeout(timedelta(seconds=20), reason=reason)
                logger.info(f"Muted {member} for 20 seconds — reason: {reason}")
                await message.channel.send(f"**{member.display_name}** muted. Reason: *{reason}*")
            except discord.Forbidden:
                logger.warning(f"Missing permission to mute {member}")
                await message.channel.send(f"Could not mute **{member.display_name}**. Missing permissions.")
        else:
            logger.warning(f"Mute requested but user '{mute_data.get('user')}' not found in guild")

    rename_data = response_data.get("rename")
    if rename_data and message.guild:
        new_name = rename_data.get("new_name", "")
        member = find_member(rename_data.get("user", ""))
        if member and new_name:
            try:
                await message.channel.send(f"**{member.display_name}** renamed to **{new_name}**")
                await member.edit(nick=new_name)
                logger.info(f"Renamed {member} to '{new_name}'")
            except discord.Forbidden:
                logger.warning(f"Missing permission to rename {member}")
                await message.channel.send(f"Impossible de renommer **{member.display_name}**. Permissions insuffisantes.")
        else:
            logger.warning(f"Rename requested but user '{rename_data.get('user')}' not found or new_name empty")

    reaction = response_data.get("reaction")
    if reaction:
        try:
            await message.add_reaction(reaction)
        except Exception as e:
            logger.warning(f"[Reaction] Could not add {reaction}: {e}")

    add_to_history(message.channel.id, "assistant", response_content)
    sent = await message.reply(response_content)
    channel_name = getattr(message.channel, "name", "dm")
    asyncio.create_task(broadcast_to_cli(
        f"MSG|{sent.channel.id}|{channel_name}|{bot.user.display_name}|{sent.id}|{message.id}|{response_content}"
    ))


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    if before.status != after.status:
        asyncio.create_task(broadcast_to_cli(
            f"PRESENCE|{after.id}|{str(after.status)}"
        ))


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author.bot:
        return
    for embed in after.embeds:
        if embed.type in ("gifv", "image"):
            media = embed.image or embed.thumbnail
            img_url = media.proxy_url if media else None
            if img_url:
                asyncio.create_task(broadcast_to_cli(
                    f"IMAGE|{after.channel.id}|{after.id}|{img_url}"
                ))


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    channel_name = getattr(channel, "name", "dm")
    if payload.member:
        display = payload.member.display_name
    else:
        guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
        member = guild.get_member(payload.user_id) if guild else None
        display = member.display_name if member else str(payload.user_id)
    asyncio.create_task(broadcast_to_cli(
        f"REACTION_ADD|{channel.id}|{channel_name}|{display}|{str(payload.emoji)}|{payload.message_id}"
    ))


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    channel_name = getattr(channel, "name", "dm")
    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    member = guild.get_member(payload.user_id) if guild else None
    display = member.display_name if member else str(payload.user_id)
    asyncio.create_task(broadcast_to_cli(
        f"REACTION_REMOVE|{channel.id}|{channel_name}|{display}|{str(payload.emoji)}|{payload.message_id}"
    ))


# Commands

@bot.tree.command(name="add_personality", description="Create or modify a personality")
@discord.app_commands.describe(name="Personality name", prompt="System prompt for this personality")
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

@bot.tree.command(name="shut_up", description="Shut up the bot")
async def cmd_shut_up(interaction: discord.Interaction):
    global bot_muted
    bot_muted = True
    logger.info(f"{interaction.user} shut up the bot in guild {interaction.guild_id}")
    await interaction.response.send_message("**/unshut_up** to allow me to talk by myself again.")

@bot.tree.command(name="unshut_up", description="Unshut up the bot")
async def cmd_unshut_up(interaction: discord.Interaction):
    global bot_muted
    bot_muted = False
    logger.info(f"{interaction.user} unshut up the bot in guild {interaction.guild_id}")
    await interaction.response.send_message("I can talk by myself again.")

# Start the bot
if __name__ == "__main__":
    bot.run(TOKEN)
