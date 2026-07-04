"""Discord bot — the long-running half of the monolith. Run with `python -m rbga.bot`.

Shares the db layer with the API (imports models directly rather than calling
the HTTP API), so a key taken via a slash command and one taken via REST hit the
same table. This is the Discord front-end Owen's README always intended.

Features:
  * keys — `/keys`, `/whohas`, `/take`, `/return`, `/addkey`, `/removekey` (here)
  * board games — the `/game` group (rbga/bot/boardgames.py)
  * complaints — routing + ticket handling (rbga/bot/complaints.py). Handled via
    the API (reviewer token), never direct DB access; metadata only in Discord.

Reads are open to everyone; mutations are gated to the exec role named by
`DISCORD_KEYS_ROLE` (see rbga/bot/common.py). If that var is unset we fail closed
and deny all mutations.

The SQLAlchemy session is synchronous, so every DB touch runs in a worker thread
(`_in_thread`) to keep the event loop free — a blocked loop would blow past
Discord's 3-second interaction-token window and silently drop commands.
"""
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from sqlalchemy import select

from ..db.database import SessionLocal
from ..db.models import Key
from . import boardgames, complaints
from .common import EXEC_ROLE, _in_thread, require_exec_role

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")  # set for instant command sync in one guild

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def colour_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest existing key colours, filtered by what's typed so far."""
    def query() -> list[str]:
        with SessionLocal() as db:
            return list(db.scalars(select(Key.colour).order_by(Key.colour)).all())

    colours = await _in_thread(query)
    lowered = current.lower()
    return [
        app_commands.Choice(name=c, value=c)
        for c in colours
        if lowered in c.lower()
    ][:25]


@tree.command(name="keys", description="List all cabinet keys and who holds them")
async def keys_cmd(interaction: discord.Interaction):
    await interaction.response.defer()

    def query() -> list[Key]:
        with SessionLocal() as db:
            return list(db.scalars(select(Key)).all())

    rows = await _in_thread(query)
    if not rows:
        await interaction.followup.send("No keys are registered yet.")
        return
    lines = [f"**{k.colour}** ({k.campus}) — {k.holder or 'nobody'}" for k in rows]
    await interaction.followup.send("\n".join(lines))


@tree.command(name="whohas", description="Who currently holds a given key?")
@app_commands.describe(colour="The key colour")
@app_commands.autocomplete(colour=colour_autocomplete)
async def whohas_cmd(interaction: discord.Interaction, colour: str):
    await interaction.response.defer()

    def query() -> Key | None:
        with SessionLocal() as db:
            return db.scalar(select(Key).where(Key.colour == colour))

    key = await _in_thread(query)
    if not key:
        await interaction.followup.send(f"There is no {colour} key.")
    elif key.holder:
        await interaction.followup.send(f"The {colour} key is held by {key.holder}.")
    else:
        await interaction.followup.send(f"The {colour} key is not held by anybody.")


@tree.command(name="take", description="Record that you (or someone) took a key")
@app_commands.describe(colour="The key colour", holder="Who now holds it (defaults to you)")
@app_commands.autocomplete(colour=colour_autocomplete)
@app_commands.check(require_exec_role)
async def take_cmd(interaction: discord.Interaction, colour: str, holder: str | None = None):
    await interaction.response.defer(ephemeral=True)
    holder = holder or interaction.user.display_name

    def mutate() -> tuple[bool, str | None]:
        with SessionLocal() as db:
            key = db.scalar(select(Key).where(Key.colour == colour))
            if not key:
                return False, None
            prev = key.holder
            key.prev_holder = prev
            key.holder = holder
            key.transfer_time = datetime.now(timezone.utc)
            db.commit()
            return True, prev

    found, prev = await _in_thread(mutate)
    if not found:
        await interaction.followup.send(f"There is no {colour} key.", ephemeral=True)
    elif prev:
        await interaction.followup.send(f"{holder} took the {colour} key from {prev}.", ephemeral=True)
    else:
        await interaction.followup.send(f"{holder} now holds the {colour} key.", ephemeral=True)


@tree.command(name="return", description="Hand a key back — clears its holder")
@app_commands.describe(colour="The key colour")
@app_commands.autocomplete(colour=colour_autocomplete)
@app_commands.check(require_exec_role)
async def return_cmd(interaction: discord.Interaction, colour: str):
    await interaction.response.defer(ephemeral=True)

    def mutate() -> tuple[bool, str | None]:
        with SessionLocal() as db:
            key = db.scalar(select(Key).where(Key.colour == colour))
            if not key:
                return False, None
            prev = key.holder
            key.prev_holder = prev
            key.holder = None
            key.transfer_time = datetime.now(timezone.utc)
            db.commit()
            return True, prev

    found, prev = await _in_thread(mutate)
    if not found:
        await interaction.followup.send(f"There is no {colour} key.", ephemeral=True)
    elif prev:
        await interaction.followup.send(f"The {colour} key was returned by {prev}.", ephemeral=True)
    else:
        await interaction.followup.send(f"The {colour} key was already not held by anybody.", ephemeral=True)


@tree.command(name="addkey", description="Register a new cabinet key")
@app_commands.describe(colour="The key colour", campus="Which campus it belongs to")
@app_commands.check(require_exec_role)
async def addkey_cmd(interaction: discord.Interaction, colour: str, campus: str):
    await interaction.response.defer(ephemeral=True)

    def mutate() -> bool:
        with SessionLocal() as db:
            if db.scalar(select(Key).where(Key.colour == colour)):
                return False
            db.add(Key(colour=colour, campus=campus))
            db.commit()
            return True

    created = await _in_thread(mutate)
    if created:
        await interaction.followup.send(f"Registered the {colour} key ({campus}).", ephemeral=True)
    else:
        await interaction.followup.send(f"A {colour} key already exists.", ephemeral=True)


@tree.command(name="removekey", description="Delete a cabinet key")
@app_commands.describe(colour="The key colour")
@app_commands.autocomplete(colour=colour_autocomplete)
@app_commands.check(require_exec_role)
async def removekey_cmd(interaction: discord.Interaction, colour: str):
    await interaction.response.defer(ephemeral=True)

    def mutate() -> bool:
        with SessionLocal() as db:
            key = db.scalar(select(Key).where(Key.colour == colour))
            if not key:
                return False
            db.delete(key)
            db.commit()
            return True

    removed = await _in_thread(mutate)
    if removed:
        await interaction.followup.send(f"Deleted the {colour} key.", ephemeral=True)
    else:
        await interaction.followup.send(f"There is no {colour} key.", ephemeral=True)


@tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    """Reply with a friendly message; never leak a traceback to the channel."""
    if isinstance(error, app_commands.CheckFailure):
        need = f"the {EXEC_ROLE} role" if EXEC_ROLE else "a role that isn't configured yet"
        message = f"You need {need} to do that."
    else:
        message = "Something went wrong handling that command. Please try again."
        # Logged server-side (stderr) for the exec who maintains the bot; not shown to users.
        print(f"App command error: {error!r}")

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


boardgames.setup(tree)  # register the /game command group
complaints.setup(client, tree)  # complaint-action buttons + /complaints-setup


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    complaints.start_polling(client)  # begin routing new complaints to Discord
    print(f"Logged in as {client.user}")


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — see .env.example")
    if not EXEC_ROLE:
        print("Warning: DISCORD_KEYS_ROLE is not set — all mutations will be denied.")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
