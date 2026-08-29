"""
ai_feature.py — Gemini-powered auto chat for the Discord bot
==============================================================
Gives the bot a conversational AI mode, separate from AUTO_RESPONSES and
the slash commands. The bot replies (like a normal chat member) when:

  1. It is @mentioned                     -> "@Lumina what's up?"
  2. Someone replies to one of its messages -> keeps the thread going
  3. Its name is said as a whole word      -> "hey lumina, how are you"
  4. It's a DM                             -> every message is a reply

Uses Google's Gemini API (Interactions API, the current recommended
interface as of mid-2026) via the official `google-genai` SDK, which keeps
conversation history server-side per `previous_interaction_id` — so we
don't have to resend the whole chat history ourselves.

Setup
-----
  1. pip install google-genai
  2. Add to your .env file (same folder as track4.py):
       GEMINI_API_KEY=your-real-gemini-key-here
     Get a key at https://aistudio.google.com/apikey
  3. (Optional) override the model or persona via .env:
       GEMINI_MODEL=gemini-3.5-flash

Wiring it into track4.py
-------------------------
track4.py already has an on_message handler (for AUTO_RESPONSES), and
discord.Client only fires one on_message per bot, so this file does NOT
register its own event — you call it from the existing one. In track4.py:

    from ai_feature import handle_ai_message

    @client.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        text = message.content.strip().lower()
        reply = AUTO_RESPONSES.get(text)
        if reply:
            await message.channel.send(reply)
            return  # don't also let the AI chime in on an auto-response hit

        await handle_ai_message(message, client, is_allowed_channel=is_allowed_channel_for_message)

`is_allowed_channel_for_message` is a tiny adapter around track4.py's
existing channel-lock dict, since that one is written for slash-command
Interactions, not raw Messages. Add this next to `is_allowed_channel()`
in track4.py:

    def is_allowed_channel_for_message(message: discord.Message) -> bool:
        if message.guild is None:
            return True
        locked_channel_id = guild_channel_locks.get(str(message.guild.id))
        return locked_channel_id is None or message.channel.id == locked_channel_id

That param is optional — if you don't pass it, the AI feature ignores the
channel lock and works everywhere.
"""

import os
import re
import time

import discord

try:
    from google import genai
except ImportError:  # pragma: no cover - surfaced clearly at runtime instead
    genai = None

# ── Settings ──────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

# Extra words (besides the bot's own Discord display name) that count as
# "calling the bot by name" in a normal sentence. Lowercase, no spaces
# needed — matched as a whole word so "lumina" won't false-positive on
# "illuminate". Add nicknames here freely.
AI_NAME_ALIASES = ["Dumpling"]

# What the bot's personality/instructions are. Edit freely.
SYSTEM_INSTRUCTION = (
    "You are Dumpling, a friendly and witty member of this Discord server. "
    "Reply the way a helpful friend in a group chat would: casual, warm, "
    "and concise (usually a sentence or two, more only if the question "
    "genuinely needs it). Discord markdown (bold, italics, code blocks) is "
    "fine when it helps. Don't mention that you're an AI model or bring up "
    "Gemini/Anthropic/etc. unless the user directly asks what you are."
)

# Minimum seconds between AI replies to the same person, so a burst of
# messages (or someone spamming the bot's name) doesn't burn API quota.
COOLDOWN_SECONDS = 3

# Discord's hard cap on a single message.
DISCORD_MSG_LIMIT = 2000

# ── Gemini client ─────────────────────────────────────────────────────────────

_client = genai.Client(api_key=GEMINI_API_KEY) if (genai and GEMINI_API_KEY) else None

# conversation_state[key] = last Gemini interaction id for that thread, so
# the next call can pass previous_interaction_id and Gemini remembers the
# conversation without us re-sending history. Keyed per (channel, author)
# so two people talking to the bot in the same channel don't bleed into
# each other's context. Lost on restart — that's fine, it just starts a
# fresh conversation.
conversation_state: dict[tuple[int, int], str] = {}

# last_reply_at[user_id] = time.monotonic() of their last AI reply, for the
# cooldown above.
_last_reply_at: dict[int, float] = {}

_MENTION_RE = re.compile(r"<@!?(\d+)>")


# ── Trigger detection ─────────────────────────────────────────────────────────

def _name_mentioned(text: str, client: discord.Client) -> bool:
    names = set(AI_NAME_ALIASES)
    if client.user:
        names.add(client.user.name.lower())
        if client.user.display_name:
            names.add(client.user.display_name.lower())
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in names if name)


def _is_reply_to_bot(message: discord.Message, client: discord.Client) -> bool:
    ref = message.reference
    if ref is None:
        return False
    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == client.user.id
    # resolved can be None if discord.py didn't fetch it (e.g. an older
    # message) — treat "unknown" as "not a reply to us" rather than
    # guessing, to avoid replying in threads that aren't ours.
    return False


def is_ai_trigger(message: discord.Message, client: discord.Client) -> bool:
    """True if this message should get an AI reply."""
    if message.guild is None:
        return True  # DMs: every message is "talking to the bot"
    if client.user in message.mentions:
        return True
    if _is_reply_to_bot(message, client):
        return True
    if _name_mentioned(message.content, client):
        return True
    return False


def _clean_prompt_text(message: discord.Message, client: discord.Client) -> str:
    """Strip the bot's own @mention out of the message so it doesn't
    confuse the model, and resolve other @mentions to plain names."""
    text = message.content

    def _sub(m: re.Match) -> str:
        uid = int(m.group(1))
        if client.user and uid == client.user.id:
            return ""
        member = message.guild.get_member(uid) if message.guild else None
        return f"@{member.display_name}" if member else "@someone"

    text = _MENTION_RE.sub(_sub, text)
    return text.strip()


# ── Gemini call ───────────────────────────────────────────────────────────────

async def _ask_gemini(prompt: str, key: tuple[int, int]) -> str:
    previous_id = conversation_state.get(key)
    interaction = await _client.aio.interactions.create(
        model=GEMINI_MODEL,
        input=prompt,
        system_instruction=SYSTEM_INSTRUCTION,
        previous_interaction_id=previous_id,
    )
    conversation_state[key] = interaction.id
    return (interaction.output_text or "").strip()


def _chunk_text(text: str, limit: int = DISCORD_MSG_LIMIT - 20) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, remaining = [], text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_ai_message(
    message: discord.Message,
    client: discord.Client,
    is_allowed_channel=None,
) -> bool:
    """Call this from track4.py's on_message for every non-bot message
    that AUTO_RESPONSES didn't already handle. Returns True if this
    function sent a reply (so the caller can short-circuit if it wants)."""
    if not is_ai_trigger(message, client):
        return False

    if is_allowed_channel is not None and not is_allowed_channel(message):
        return False

    if _client is None:
        # Fail quietly-ish: this only fires when someone actually tried to
        # talk to the bot, so a short heads-up is more useful than silence.
        if not GEMINI_API_KEY:
            await message.channel.send(
                "⚠️ AI chat isn't set up yet — add `GEMINI_API_KEY` to the .env file."
            )
        else:
            await message.channel.send(
                "⚠️ AI chat is missing a dependency — run `pip install google-genai`."
            )
        return True

    now = time.monotonic()
    last = _last_reply_at.get(message.author.id, 0)
    if now - last < COOLDOWN_SECONDS:
        return False  # quietly drop, no need to spam "slow down" messages
    _last_reply_at[message.author.id] = now

    prompt = _clean_prompt_text(message, client)
    if not prompt:
        return False  # e.g. a bare mention with no text — nothing to answer

    key = (message.channel.id, message.author.id)

    try:
        async with message.channel.typing():
            reply_text = await _ask_gemini(prompt, key)
    except Exception as e:
        print(f"⚠️  Gemini call failed: {e!r}")
        await message.reply(
            "❌ Sorry, I couldn't think of a reply just now — try again in a bit.",
            mention_author=False,
        )
        return True

    if not reply_text:
        reply_text = "…I'm not sure what to say to that, actually."

    chunks = _chunk_text(reply_text)
    await message.reply(chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await message.channel.send(chunk)

    return True