"""
Voice Feature — lets the bot join a voice channel and stay there.
====================================================================
Separate module on purpose, so it can be edited without touching
track4.py. Requires PyNaCl to be installed for voice support:

    pip install PyNaCl

Usage in the main file (track4.py):

    from voice_feature import setup_voice_commands
    setup_voice_commands(tree, is_admin)

This registers two slash commands:

    /join [channel]  — joins your current voice channel, or the one
                        given. Stays connected indefinitely — it does
                        NOT leave when everyone else does. It only
                        disconnects via /leave, or when the bot process
                        stops.
    /leave            — disconnects from voice in this server.

Both are open to everyone — no admin check, unlike /setchannel and
/unlock in the main file.

Note on staying connected: discord.py's voice client has no built-in
"leave when empty" behavior — a bot only leaves voice if you tell it
to, or if it's kicked/disconnected by Discord itself (e.g. network
issue, channel deleted). This module doesn't add any auto-leave logic,
so the default behavior already matches what you asked for. If you
ever want it to auto-leave when the channel is empty, that'd be a
small addition to on_voice_state_update — just say so.
"""

import asyncio

import discord
from discord import app_commands


def setup_voice_commands(tree: app_commands.CommandTree, is_admin) -> None:
    """Registers /join and /leave on the given CommandTree.

    Args:
        tree:     the bot's app_commands.CommandTree instance
        is_admin: unused by /join and /leave now that they're open to
                   everyone. Kept as a parameter so the call site in
                   track4.py doesn't need to change.
    """

    @tree.command(name="join", description="Join a voice channel and stay connected")
    @app_commands.describe(
        channel="Voice channel to join (defaults to your current voice channel)"
    )
    async def cmd_join(
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "🚫 This command can only be used in a server.", ephemeral=True
            )
            return

        target = channel
        if target is None:
            member = interaction.user
            if isinstance(member, discord.Member) and member.voice and member.voice.channel:
                target = member.voice.channel
            else:
                await interaction.response.send_message(
                    "🚫 You're not in a voice channel — join one first, or specify "
                    "a `channel` for me to join.",
                    ephemeral=True,
                )
                return

        existing = interaction.guild.voice_client
        try:
            if existing is not None:
                if existing.channel.id == target.id:
                    await interaction.response.send_message(
                        f"✅ Already connected to **{target.name}**.", ephemeral=True
                    )
                    return
                await existing.move_to(target)
            else:
                await target.connect(reconnect=True)
        except discord.ClientException as e:
            await interaction.response.send_message(
                f"⚠️ Couldn't join **{target.name}**: {e}", ephemeral=True
            )
            return
        except asyncio.TimeoutError:
            await interaction.response.send_message(
                f"⚠️ Timed out trying to join **{target.name}**. Check my permissions "
                f"(Connect / View Channel) and try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🔊 Joined **{target.name}** — I'll stay here until `/leave` is used "
            f"or the bot is shut down, even if everyone else leaves.",
            ephemeral=True,
        )

    @tree.command(name="leave", description="Disconnect the bot from voice in this server")
    async def cmd_leave(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "🚫 This command can only be used in a server.", ephemeral=True
            )
            return

        vc = interaction.guild.voice_client
        if vc is None:
            await interaction.response.send_message(
                "ℹ️ I'm not currently in a voice channel here.", ephemeral=True
            )
            return

        channel_name = vc.channel.name
        await vc.disconnect(force=True)
        await interaction.response.send_message(
            f"👋 Disconnected from **{channel_name}**.", ephemeral=True
        )