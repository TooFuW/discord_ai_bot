# discord_ai_bot

AI-powered Discord bot with customizable personalities, conversation history, mute and rename capabilities.

## Features

- Responds to mentions and joins conversations randomly (30% chance)
- Maintains per-channel conversation history (up to 60 messages)
- Configurable AI personalities per server
- Moderation: can mute or rename users on request or when it feels like it
- Supports two AI backends: **Groq** (cloud) or **Ollama** (local)
- Automatic fallback to a secondary model on Groq rate limits

## Commands

| Command | Description | Permissions |
|---------|-------------|-------------|
| `/add_personality <name> <prompt>` | Create or update a personality | Everyone |
| `/use_personality <name>` | Activate a personality for the server | Everyone |
| `/list_personalities` | List available personalities | Everyone |
| `/clear_history` | Clear the current channel's history | `manage_messages` |

## Installation

**Requirements:** Python 3.8+

```bash
pip install discord.py aiohttp python-dotenv
```

Create a `.env` file at the project root:

```env
DISCORD_TOKEN=your_discord_bot_token
GROQ_API_KEY=your_groq_api_key
GROQ_MODEL=moonshotai/kimi-k2-instruct
GROQ_FALLBACK_MODEL=llama-3.3-70b-versatile

# Optional: local backend via Ollama
# OLLAMA_MODEL=llama3
```

Run the bot:

```bash
python bot.py
```

## Configuration

| File | Purpose |
|------|---------|
| `.env` | Tokens and API keys |
| `personalities.json` | Personality definitions |
| `active_personalities.json` | Active personality per server (Guild ID) |
| `server_prompt.txt` | Global system prompt injected into every request |

## AI Backend

- If `OLLAMA_MODEL` is set → uses Ollama (local, `http://localhost:11434`)
- Else if `GROQ_API_KEY` is set → uses Groq (cloud)
- Otherwise → error on startup

## Response Format

The bot expects JSON responses from the AI:

```json
{ "reply": "response message" }
```

With optional moderation actions (can be combined):

```json
{ "reply": "message", "mute": { "user": "@username", "reason": "reason" } }
```

```json
{ "reply": "message", "rename": { "user": "@username", "new_name": "new nickname" } }
```

```json
{ "reply": "message", "mute": { "user": "@username", "reason": "reason" }, "rename": { "user": "@username", "new_name": "new nickname" } }
```

## Project Structure

```
discord_ai_bot/
├── bot.py                     # Main bot code
├── server_prompt.txt          # Global system prompt
├── personalities.json         # Available personalities
├── active_personalities.json  # Active personality per server
└── .env                       # Environment variables (not versioned)
```

## Required Discord Permissions

- `message_content` — read message content
- `members` — access the member list
- `moderate_members` — timeout (mute) users
- `manage_nicknames` — rename users
