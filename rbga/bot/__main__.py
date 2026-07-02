"""Discord bot — the long-running half of the monolith. Run with `python -m rbga.bot`.

Shares the db layer with the API (imports models directly rather than calling
the HTTP API), so a key taken via a slash command and one taken via REST hit the
same table. This is the Discord front-end Owen's README always intended.
"""
import os
from datetime import datetime

import discord
from discord import app_commands
from sqlalchemy import select

from ..db.database import SessionLocal
from ..db.models import Key

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID")  # set for instant command sync in one guild

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="keys", description="List all cabinet keys and who holds them")
async def keys_cmd(interaction: discord.Interaction):
    with SessionLocal() as db:
        rows = db.scalars(select(Key)).all()
    if not rows:
        await interaction.response.send_message("No keys are registered yet.")
        return
    lines = [f"**{k.colour}** ({k.campus}) — {k.holder or 'nobody'}" for k in rows]
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="whohas", description="Who currently holds a given key?")
@app_commands.describe(colour="The key colour")
async def whohas_cmd(interaction: discord.Interaction, colour: str):
    with SessionLocal() as db:
        key = db.scalar(select(Key).where(Key.colour == colour))
    if not key:
        await interaction.response.send_message(f"There is no {colour} key.")
    elif key.holder:
        await interaction.response.send_message(f"The {colour} key is held by {key.holder}.")
    else:
        await interaction.response.send_message(f"The {colour} key is not held by anybody.")


@tree.command(name="take", description="Record that you (or someone) took a key")
@app_commands.describe(colour="The key colour", holder="Who now holds it")
async def take_cmd(interaction: discord.Interaction, colour: str, holder: str):
    with SessionLocal() as db:
        key = db.scalar(select(Key).where(Key.colour == colour))
        if not key:
            await interaction.response.send_message(f"There is no {colour} key.")
            return
        prev = key.holder
        key.prev_holder = prev
        key.holder = holder
        key.transfer_time = datetime.utcnow()
        db.commit()
    if prev:
        await interaction.response.send_message(f"{holder} took the {colour} key from {prev}.")
    else:
        await interaction.response.send_message(f"{holder} now holds the {colour} key.")


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    print(f"Logged in as {client.user}")


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — see .env.example")
    client.run(TOKEN)


if __name__ == "__main__":
    main()
