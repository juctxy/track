"""
Auto-react feature — /autoreact add | remove | list
====================================================
Pick a member and one or more emojis; from then on, every time that member
sends a message in the server, the bot reacts to it with all of those emojis
at once.

Commands (server admins, or members with a role in ALLOWED_ROLE_IDS):
  /autoreact add    user:@Someone emojis:"😂 🔥 💀"  — start reacting to a member
  /autoreact remove user:@Someone                    — stop
  /autoreact list                                    — show who's being reacted to

Emojis can be separated by spaces/commas or typed straight together
(😂🔥💀 works too).

Wire it into the main bot file with ONE line, next to the other setup_*
calls (no on_ready hook needed — settings load at import/setup time):

    from autoreact import setup_autoreact_commands
    setup_autoreact_commands(tree, is_admin, client)

The bot needs the "Add Reactions" and "Read Message History" permissions in
the channels where it should react. Custom emojis only work if the bot
shares a server with that emoji (it's usually enough that the bot is in the
server the emoji belongs to).

Settings are stored per server in autoreact.json (atomic write, same pattern
as the other data files).
"""

import asyncio
import json
import os
import re
import tempfile

import discord
from discord import app_commands

# Role IDs (besides server administrators) allowed to use /autoreact.
# Right-click a role in Server Settings → Roles → "Copy Role ID"
# (needs Developer Mode on). Example: ALLOWED_ROLE_IDS = {123456789012345678}
ALLOWED_ROLE_IDS: set[int] = {
    1515355773348090037,
    1556231128166309989,
    1515356462396735598
}

AUTOREACT_PATH      = "autoreact.json"
MAX_EMOJIS_PER_USER = 10     # Discord allows up to 20 reactions on one message

# autoreact_config[guild_id][user_id] = ["😂", "<:name:123456789>", ...]
autoreact_config: dict[str, dict[str, list[str]]] = {}


# ── Permissions ──────────────────────────────────────────────────────────────

def _can_manage_autoreact(interaction: discord.Interaction, is_admin=None) -> bool:
    """True if the user is a server administrator or has any role listed in
    ALLOWED_ROLE_IDS."""
    user = interaction.user
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    if is_admin is not None and is_admin(interaction):
        return True
    return any(r.id in ALLOWED_ROLE_IDS for r in getattr(user, "roles", []))


# ── Persistence ──────────────────────────────────────────────────────────────

def _atomic_write_json(path: str, data) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _load_config() -> None:
    global autoreact_config
    try:
        with open(AUTOREACT_PATH, "r", encoding="utf-8") as f:
            autoreact_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        autoreact_config = {}


def _save_config() -> None:
    try:
        _atomic_write_json(AUTOREACT_PATH, autoreact_config)
    except Exception as e:
        print(f"⚠️  Failed to save {AUTOREACT_PATH}: {e!r}")


# ── Emoji parsing ────────────────────────────────────────────────────────────

_CUSTOM_RE = re.compile(r"<a?:\w+:\d+>")


def _split_chunk(chunk: str) -> list[str]:
    """Splits a whitespace-free string into individual emojis, so "😂🔥💀"
    becomes three tokens. Handles custom emojis, skin tones, ZWJ sequences
    (👨‍👩‍👧), variation selectors, keycaps (1️⃣) and flags (🇹🇭). Anything that
    isn't an emoji is returned as a plain-text token so it can be rejected."""
    tokens: list[str] = []
    i, n = 0, len(chunk)
    while i < n:
        m = _CUSTOM_RE.match(chunk, i)
        if m:
            tokens.append(m.group())
            i = m.end()
            continue

        c = chunk[i]
        if c.isascii():
            # keycap emoji: digit/#/* + optional FE0F + 20E3
            if c in "0123456789#*" and chunk[i + 1:i + 2] == "\u20e3":
                tokens.append(chunk[i:i + 2]); i += 2; continue
            if c in "0123456789#*" and chunk[i + 1:i + 3] == "\ufe0f\u20e3":
                tokens.append(chunk[i:i + 3]); i += 3; continue
            # plain ASCII text — collect the run so it gets rejected later
            j = i
            while j < n and chunk[j].isascii() and not _CUSTOM_RE.match(chunk, j):
                j += 1
            tokens.append(chunk[i:j])
            i = j
            continue

        j = i + 1
        if 0x1F1E6 <= ord(c) <= 0x1F1FF and j < n and 0x1F1E6 <= ord(chunk[j]) <= 0x1F1FF:
            j += 1  # flag = two regional indicators
        while j < n:
            ch, o = chunk[j], ord(chunk[j])
            if ch in "\ufe0e\ufe0f\u20e3" or 0x1F3FB <= o <= 0x1F3FF or 0xE0020 <= o <= 0xE007F:
                j += 1          # variation selector / keycap / skin tone / tag
            elif ch == "\u200d" and j + 1 < n:
                j += 2          # zero-width joiner + next emoji
            else:
                break
        tokens.append(chunk[i:j])
        i = j
    return tokens


def parse_emojis(raw: str) -> list[str]:
    """Returns the emoji tokens from the input, de-duplicated, in order.
    Works with or without spaces between emojis. Custom emojis look like
    <:name:id> or <a:name:id>; anything else is treated as a unicode emoji."""
    seen, result = set(), []
    for chunk in raw.replace(",", " ").split():
        for tok in _split_chunk(chunk):
            if tok in seen:
                continue
            seen.add(tok)
            result.append(tok)
    return result


def _looks_valid(token: str) -> bool:
    """Cheap sanity check — real validation happens when Discord accepts or
    rejects the reaction. Custom emojis must parse as <:name:id>; unicode
    emojis must at least be non-ASCII-word text (so 'hello' is rejected)."""
    if token.startswith("<") and token.endswith(">"):
        pe = discord.PartialEmoji.from_str(token)
        return pe.id is not None
    return not token.isascii()


# ── Message listener ─────────────────────────────────────────────────────────

_warned: set[tuple[str, str]] = set()  # (guild_id, emoji) combos already logged


async def _add_one_reaction(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.NotFound:
        return  # message deleted (or unknown emoji) — stay quiet
    except discord.Forbidden:
        key = (str(message.guild.id), "forbidden")
        if key not in _warned:
            _warned.add(key)
            print(f"⚠️  autoreact: missing Add Reactions / Read Message History "
                  f"permission in #{message.channel} ({message.guild.name})")
    except discord.HTTPException as e:
        key = (str(message.guild.id), emoji)
        if key not in _warned:
            _warned.add(key)
            print(f"⚠️  autoreact: couldn't add {emoji!r} in {message.guild.name}: {e!r}")


async def _react_to_message(message: discord.Message) -> None:
    if message.guild is None:
        return
    guild_cfg = autoreact_config.get(str(message.guild.id))
    if not guild_cfg:
        return
    emojis = guild_cfg.get(str(message.author.id))
    if not emojis:
        return

    # Fire all reactions at once; discord.py queues them against Discord's
    # rate limit automatically, and one failing emoji doesn't block the rest.
    await asyncio.gather(*(_add_one_reaction(message, e) for e in emojis))


def _install_listener(client: discord.Client) -> None:
    """discord.Client allows only one on_message. If the main file already
    defines one (now or in the future before this runs), keep it and chain
    ours after it instead of overwriting it."""
    previous = getattr(client, "on_message", None)

    async def on_message(message: discord.Message):
        if previous is not None:
            await previous(message)
        await _react_to_message(message)

    client.event(on_message)


# ── Commands ─────────────────────────────────────────────────────────────────

NO_PERMISSION = "🚫 You need to be an admin or have an allowed role to use this."


def setup_autoreact_commands(tree: app_commands.CommandTree, is_admin, client: discord.Client):
    _load_config()
    _install_listener(client)

    group = app_commands.Group(
        name="autoreact",
        description="Make the bot automatically react to a member's messages",
        guild_only=True,
    )

    @group.command(name="add", description="Start reacting to every message from a member")
    @app_commands.describe(
        user="Whose messages to react to",
        emojis=f"One or more emojis (max {MAX_EMOJIS_PER_USER}), e.g. 😂 🔥 💀",
    )
    async def autoreact_add(interaction: discord.Interaction, user: discord.Member, emojis: str):
        if not _can_manage_autoreact(interaction, is_admin):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return

        tokens = parse_emojis(emojis)
        if not tokens:
            await interaction.response.send_message("⚠️ Give me at least one emoji.", ephemeral=True)
            return
        if len(tokens) > MAX_EMOJIS_PER_USER:
            await interaction.response.send_message(
                f"⚠️ Max {MAX_EMOJIS_PER_USER} emojis per person.", ephemeral=True)
            return
        bad = [t for t in tokens if not _looks_valid(t)]
        if bad:
            await interaction.response.send_message(
                f"⚠️ These don't look like emojis: {' '.join(bad)}", ephemeral=True)
            return

        guild_cfg = autoreact_config.setdefault(str(interaction.guild_id), {})
        guild_cfg[str(user.id)] = tokens
        _save_config()
        await interaction.response.send_message(
            f"✅ I'll react to every message from {user.mention} with {' '.join(tokens)}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @group.command(name="remove", description="Stop reacting to a member's messages")
    @app_commands.describe(user="Who to stop reacting to")
    async def autoreact_remove(interaction: discord.Interaction, user: discord.Member):
        if not _can_manage_autoreact(interaction, is_admin):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return

        guild_cfg = autoreact_config.get(str(interaction.guild_id), {})
        if str(user.id) not in guild_cfg:
            await interaction.response.send_message(
                f"{user.mention} isn't on the auto-react list.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return
        del guild_cfg[str(user.id)]
        if not guild_cfg:
            autoreact_config.pop(str(interaction.guild_id), None)
        _save_config()
        await interaction.response.send_message(
            f"🛑 Stopped reacting to {user.mention}.",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @group.command(name="list", description="Show who the bot is auto-reacting to")
    async def autoreact_list(interaction: discord.Interaction):
        if not _can_manage_autoreact(interaction, is_admin):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return

        guild_cfg = autoreact_config.get(str(interaction.guild_id), {})
        if not guild_cfg:
            await interaction.response.send_message(
                "Nobody is on the auto-react list.", ephemeral=True)
            return
        lines = [f"<@{uid}> → {' '.join(emojis)}" for uid, emojis in guild_cfg.items()]
        await interaction.response.send_message(
            "**Auto-react list:**\n" + "\n".join(lines),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    tree.add_command(group)