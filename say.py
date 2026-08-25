"""
Say Feature — lets an admin make the bot send a plain-text message, and
edit it later.
====================================================================
Separate module on purpose, so it can be edited without touching
track4.py.

Usage in the main file (track4.py):

    from say import setup_say_commands
    setup_say_commands(tree, is_admin)

This registers two slash commands:

    /say <message> [channel]  — sends `message` to the given channel,
                                 or the current channel if omitted,
                                 posted by the bot itself. Works for text
                                 channels, voice-channel text chat, stage
                                 channels, and threads. The admin's own
                                 invocation of /say stays ephemeral, so
                                 only they see a confirmation — the posted
                                 message just shows up as the bot talking.

    /sayedit <message> [channel] [message_id]
                               — edits a message the bot sent. With no
                                 message_id, it edits the last message
                                 /say sent in the given (or current)
                                 channel. Pass message_id to edit a
                                 specific bot message instead — useful
                                 if the bot restarted since the original
                                 /say (the "last message" memory is
                                 in-process only and doesn't survive a
                                 restart) or if you want to fix an older
                                 message.

Gated behind the main file's own is_admin() check, same as /setchannel,
/unlock, /join, and /leave, so only server admins can make the bot talk.
"""

import discord
from discord import app_commands

DISCORD_MSG_LIMIT = 2000  # Discord's hard cap on a single message's length

# Every channel type that actually supports .send() — i.e. has a text chat
# attached. VoiceChannel and StageChannel both have their own text chat in
# Discord now, same as a regular TextChannel; Thread does too. The type hint
# below is what tells Discord's slash-command picker which channel types to
# even show as options, so this list has to match the isinstance check
# further down or the picker and the runtime check disagree with each other.
SendableChannel = discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread

# Remembers the last message /say sent in each channel, so /sayedit can
# find it without the caller having to pass a message ID. Keyed by channel
# ID. This is in-memory only — it resets if the bot restarts, which is why
# /sayedit also accepts an explicit message_id as a fallback.
_last_sent_message: dict[int, discord.Message] = {}


def setup_say_commands(tree: app_commands.CommandTree, is_admin) -> None:
    """Registers /say and /sayedit on the given CommandTree.

    Args:
        tree:     the bot's app_commands.CommandTree instance
        is_admin: callable(interaction) -> bool — the main file's own
                   admin check, reused here so both files agree on
                   who's allowed to make the bot talk.
    """

    @tree.command(name="say", description="(Admin) Make the bot send a message")
    @app_commands.describe(
        message="The text for the bot to send",
        channel="Channel to send it in (defaults to the current channel)",
    )
    async def cmd_say(
        interaction: discord.Interaction,
        message: str,
        channel: SendableChannel | None = None,
    ):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "🚫 Only server administrators can use this command.", ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "🚫 This command can only be used in a server.", ephemeral=True
            )
            return

        if len(message) > DISCORD_MSG_LIMIT:
            await interaction.response.send_message(
                f"⚠️ That message is {len(message)} characters — Discord's limit "
                f"is {DISCORD_MSG_LIMIT}. Trim it and try again.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.VoiceChannel,
                                    discord.StageChannel, discord.Thread)):
            await interaction.response.send_message(
                "🚫 That's not a channel I can send messages in.", ephemeral=True
            )
            return

        perms = target.permissions_for(interaction.guild.me)
        # Threads are gated by their own "send messages in threads" permission
        # rather than the regular send_messages flag.
        can_send = perms.send_messages_in_threads if isinstance(target, discord.Thread) else perms.send_messages
        if not can_send:
            await interaction.response.send_message(
                f"⚠️ I don't have permission to send messages in {target.mention}.",
                ephemeral=True,
            )
            return

        try:
            sent = await target.send(message)
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"⚠️ Couldn't send that message: {e}", ephemeral=True
            )
            return

        # Remember it so /sayedit can find it without a message ID.
        _last_sent_message[target.id] = sent

        await interaction.response.send_message(
            f"✅ Sent to {target.mention}.", ephemeral=True
        )

    @tree.command(name="sayedit", description="(Admin) Edit a message the bot sent with /say")
    @app_commands.describe(
        message="The new text for the message",
        channel="Channel whose message to edit (defaults to the current channel)",
        message_id="Optional: edit this specific bot message instead of the last /say",
    )
    async def cmd_sayedit(
        interaction: discord.Interaction,
        message: str,
        channel: SendableChannel | None = None,
        message_id: str | None = None,
    ):
        if not is_admin(interaction):
            await interaction.response.send_message(
                "🚫 Only server administrators can use this command.", ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "🚫 This command can only be used in a server.", ephemeral=True
            )
            return

        if len(message) > DISCORD_MSG_LIMIT:
            await interaction.response.send_message(
                f"⚠️ That message is {len(message)} characters — Discord's limit "
                f"is {DISCORD_MSG_LIMIT}. Trim it and try again.",
                ephemeral=True,
            )
            return

        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.VoiceChannel,
                                    discord.StageChannel, discord.Thread)):
            await interaction.response.send_message(
                "🚫 That's not a channel I can edit messages in.", ephemeral=True
            )
            return

        # Work out which message we're editing: an explicit message_id wins,
        # otherwise fall back to the last message /say sent in this channel.
        target_message: discord.Message | None = None

        if message_id is not None:
            try:
                mid = int(message_id)
            except ValueError:
                await interaction.response.send_message(
                    "🚫 That doesn't look like a valid message ID.", ephemeral=True
                )
                return

            try:
                target_message = await target.fetch_message(mid)
            except discord.NotFound:
                await interaction.response.send_message(
                    "⚠️ Couldn't find a message with that ID in that channel.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                await interaction.response.send_message(
                    f"⚠️ Couldn't fetch that message: {e}", ephemeral=True
                )
                return

            if target_message.author.id != interaction.client.user.id:
                await interaction.response.send_message(
                    "🚫 That message wasn't sent by me, so I can't edit it.",
                    ephemeral=True,
                )
                return
        else:
            target_message = _last_sent_message.get(target.id)
            if target_message is None:
                await interaction.response.send_message(
                    f"⚠️ I haven't sent a /say message in {target.mention} yet "
                    f"(or the bot restarted since then). Pass a message_id "
                    f"instead to edit a specific message.",
                    ephemeral=True,
                )
                return

        try:
            edited = await target_message.edit(content=message)
        except discord.NotFound:
            _last_sent_message.pop(target.id, None)
            await interaction.response.send_message(
                "⚠️ That message seems to have been deleted.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"⚠️ Couldn't edit that message: {e}", ephemeral=True
            )
            return

        # Keep the cache fresh in case it's edited again without a message_id.
        _last_sent_message[target.id] = edited

        await interaction.response.send_message(
            f"✅ Edited message in {target.mention}.", ephemeral=True
        )