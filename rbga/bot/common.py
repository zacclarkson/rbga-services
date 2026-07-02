"""Shared plumbing for the bot's feature modules (keys, board games).

Kept separate from `__main__` so command modules can import these without a
circular import back into the entry point.
"""
import asyncio
import os

import discord

# The exec/manager role that gates *all* bot mutations (keys and board games).
# Historically named for keys; it now guards board-game edits too. Fail closed:
# unset means nobody can mutate anything.
EXEC_ROLE = os.environ.get("DISCORD_KEYS_ROLE")


async def _in_thread(fn):
    """Run blocking (synchronous SQLAlchemy) work off the event loop."""
    return await asyncio.to_thread(fn)


def require_exec_role(interaction: discord.Interaction) -> bool:
    """app_commands check: caller must hold the exec role named by DISCORD_KEYS_ROLE.

    Fails closed — no role configured, or invoked outside a guild, means denied.
    """
    if not EXEC_ROLE or not isinstance(interaction.user, discord.Member):
        return False
    return discord.utils.get(interaction.user.roles, name=EXEC_ROLE) is not None
