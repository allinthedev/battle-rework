import logging
import random
import sys
from typing import TYPE_CHECKING, Dict
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

import asyncio
import io

from ballsdex.core.models import Ball, Player
from ballsdex.core.models import balls as countryballs
from ballsdex.settings import settings

from ballsdex.core.utils.transformers import BallEnabledTransform
from ballsdex.packages.battle.xe_battle_lib import (
    BattleBall,
    BattleInstance,
    gen_battle,
)

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
log = logging.getLogger("ballsdex.packages.battle")

@dataclass
class GuildBattle:
    author: discord.Member
    opponent: discord.Member
    author_ready: bool = False
    opponent_ready: bool = False
    battle: BattleInstance = field(default_factory=BattleInstance)
    deck_size: int = 4


def gen_deck(balls) -> str:
    """Generates a text representation of the player's deck with live status."""
    if not balls:
        return "Empty"

    lines = []
    for ball in balls:
        if ball.dead:
            status = "💀"
        else:
            status = f"❤️ {ball.health} | ⚔️ {ball.attack}"
        lines.append(f"- {ball.emoji} {ball.name} ({status})")
    
    return "\n".join(lines)


def update_embed(
    author_balls, opponent_balls, author, opponent, author_ready, opponent_ready, max_size: int
) -> discord.Embed:
    """Creates an embed for the battle setup phase."""
    embed = discord.Embed(
        title="Battle Plan",
        description=f"Add or remove balls you want to propose to the other player using the '/battle add' and '/battle remove' commands. Remember, you may add up to **{max_size}** balls in a deck for this battle. Once you've finished, click the tick button to start the battle.",
        color=discord.Colour.blurple(),
    )

    author_emoji = ":white_check_mark:" if author_ready else ""
    opponent_emoji = ":white_check_mark:" if opponent_ready else ""

    embed.add_field(
        name=f"{author_emoji} {author}'s deck:",
        value=gen_deck(author_balls),
        inline=True,
    )
    embed.add_field(
        name=f"{opponent_emoji} {opponent}'s deck:",
        value=gen_deck(opponent_balls),
        inline=True,
    )
    return embed


def create_disabled_buttons() -> discord.ui.View:
    """Creates a view with disabled start and cancel buttons."""
    view = discord.ui.View()
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.success, emoji="✔", label="Ready", disabled=True
        )
    )
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.danger, emoji="✖", label="Cancel", disabled=True
        )
    )


class Battle(commands.GroupCog):
    """
    Brawl with your balls!!!!
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.battles: Dict[int, GuildBattle] = {}
        self.interactions: Dict[int, discord.Interaction] = {}

    async def start_battle(self, interaction: discord.Interaction):
        guild_battle = self.battles.get(interaction.guild_id)
        if not guild_battle or interaction.user not in (
            guild_battle.author,
            guild_battle.opponent,
        ):
            await interaction.response.send_message(
                "You aren't a part of this battle.", ephemeral=True
            )
            return

        if interaction.user == guild_battle.author:
            guild_battle.author_ready = True
        elif interaction.user == guild_battle.opponent:
            guild_battle.opponent_ready = True

        if guild_battle.author_ready and guild_battle.opponent_ready:
            if not (guild_battle.battle.p1_balls and guild_battle.battle.p2_balls):
                await interaction.response.send_message(
                    "Both players must add balls!"
                )
                return
            new_view = create_disabled_buttons()
            try:
                setup_interaction = self.interactions[interaction.guild_id]
                await setup_interaction.delete_original_response()
            except Exception:
                pass
            await interaction.response.defer()

            battle = guild_battle.battle

            embed = discord.Embed(
                title="Battle in progress",
                color=discord.Color.orange(),
                description="Preparing turns...",
            )
            embed.add_field(
                name=f"{guild_battle.author.display_name}'s Battle Deck",
                value=gen_deck(battle.p1_balls),
                inline=True,
            )            
            embed.add_field(
                name=f"{guild_battle.opponent.display_name}'s Battle Deck",
                value=gen_deck(battle.p2_balls),
                inline=True,
            )
            embed.set_footer(text="Turn 0")

            message = await interaction.followup.send(embed=embed, wait=True)

            for turn_text in gen_battle(battle):
                max_size = guild_battle.deck_size
                embed.description = turn_text
                embed.set_footer(text=f"Max Deck Size: {max_size}")

                updated_p1 = gen_deck(battle.p1_balls)
                updated_p2 = gen_deck(battle.p2_balls)
    
                embed.set_field_at(
                    0,
                    name=f"{guild_battle.author.display_name}'s Battle Deck",
                    value=updated_p1,
                    inline=True,
                )
                embed.set_field_at(
                    1,
                    name=f"{guild_battle.opponent.display_name}'s Battle Deck",
                    value=updated_p2,
                    inline=True,
                )

                await message.edit(embed=embed)
                await asyncio.sleep(3.5) # change the turn shift here if you want i mean idk

            embed.title = "Battle: Complete!"
            embed.color = discord.Color.green()
            embed.description = (
                f"{guild_battle.author.mention} VS {guild_battle.opponent.mention}\n\n"
                f"**Winner**: {battle.winner}\n"
                f"Total Turns: {battle.turns}"
            )
            embed.set_footer(text="Battle concluded.")
            await message.edit(embed=embed, view=new_view)
            
            self.battles[interaction.guild_id] = None

        else:

            await interaction.response.send_message(
                f"Done! Waiting for the other player to press 'Ready'.", ephemeral=True
            )

            author_emoji = (
                ":white_check_mark:" if interaction.user == guild_battle.author else ""
            )
            opponent_emoji = (
                ":white_check_mark:"
                if interaction.user == guild_battle.opponent
                else ""
            )

            embed = discord.Embed(
                title="Battle Plan",
                description="Add or remove balls you want to propose to the other player using the '/battle add' and '/battle remove' commands. Remember, you may add up to **{max_size}** balls in a deck for this battle. Once you've finished, click the tick button to start the battle.",
                color=discord.Colour.blurple(),
            )

            embed.add_field(
                name=f"{author_emoji} {guild_battle.author.name}'s deck:",
                value=gen_deck(guild_battle.battle.p1_balls),
                inline=True,
            )
            embed.add_field(
                name=f"{opponent_emoji} {guild_battle.opponent.name}'s deck:",
                value=gen_deck(guild_battle.battle.p2_balls),
                inline=True,
            )

            await self.interactions[interaction.guild_id].edit_original_response(
                embed=embed
            )

    async def cancel_battle(self, interaction: discord.Interaction):
        guild_battle = self.battles.get(interaction.guild_id)

        if not guild_battle:
            return

        if interaction.user not in (guild_battle.author, guild_battle.opponent):
            await interaction.response.send_message(
                "You aren't a part of this battle!", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="Battle Plan",
            description="The battle has been cancelled.",
            color=discord.Color.red(),
        )
        embed.add_field(
            name=f"{guild_battle.author}'s deck:",
            value=gen_deck(guild_battle.battle.p1_balls),
            inline=True,
        )
        embed.add_field(
            name=f"{guild_battle.opponent}'s deck:",
            value=gen_deck(guild_battle.battle.p2_balls),
            inline=True,
        )

        try:
            await interaction.response.defer()
        except discord.errors.InteractionResponded:
            pass
        await interaction.message.edit(embed=embed, view=create_disabled_buttons())
        self.battles[interaction.guild_id] = None

    @app_commands.command()
    async def start(self, interaction: discord.Interaction, opponent: discord.Member, max_size: int = 4):
        """
        Start a new battle with a chosen user.
        """
        if self.battles.get(interaction.guild_id):
            await interaction.response.send_message(
                "You cannot start a new battle right now, as one is already ongoing in this server.",
                ephemeral=True,
            )
            return
        self.battles[interaction.guild_id] = GuildBattle(
            author=interaction.user, opponent=opponent, deck_size=max_size
        )
        embed = update_embed([], [], interaction.user.name, opponent.name, False, False, max_size)

        start_button = discord.ui.Button(
            style=discord.ButtonStyle.success, emoji="✔", label="Ready"
        )
        cancel_button = discord.ui.Button(
            style=discord.ButtonStyle.danger, emoji="✖", label="Cancel"
        )

        start_button.callback = self.start_battle
        cancel_button.callback = self.cancel_battle

        view = discord.ui.View(timeout=None)
        view.add_item(start_button)
        view.add_item(cancel_button)

        await interaction.response.send_message(
            f"Hey, {opponent.mention}, {interaction.user.name} is proposing a battle with you!",
            embed=embed,
            view=view,
        )

        self.interactions[interaction.guild_id] = interaction

    @app_commands.command()
    async def add(
        self, interaction: discord.Interaction, countryball: BallEnabledTransform
    ):
        """
        Add a ball to a battle.
        """
        guild_battle = self.battles.get(interaction.guild_id)
        if not guild_battle:
            await interaction.response.send_message(
                "There is no ongoing battle in this server!", ephemeral=True
            )

        if (interaction.user == guild_battle.author and guild_battle.author_ready) or (
            interaction.user == guild_battle.opponent and guild_battle.opponent_ready
        ):
            await interaction.response.send_message(
                "You cannot change your balls as you are already ready.", ephemeral=True
            )
            return

        if interaction.user not in (guild_battle.author, guild_battle.opponent):
            await interaction.response.send_message(
                "You aren't a part of this battle!", ephemeral=True
            )
            return

        user_balls = guild_battle.battle.p1_balls if interaction.user == guild_battle.author else guild_battle.battle.p2_balls

        if len(user_balls) >= guild_battle.deck_size:
            await interaction.response.send_message(
                f"You cannot add more than {guild_battle.deck_size} balls!", ephemeral=True
            )
            return

        ball = BattleBall(
            countryball.countryball.country,
            interaction.user.name,
            countryball.health,
            countryball.attack,
            self.bot.get_emoji(countryball.countryball.emoji_id),
        )

        if ball in user_balls:
            await interaction.response.send_message(
                "You cannot add the same ball twice!", ephemeral=True
            )
            return
        user_balls.append(ball)

        attack_sign = "+" if countryball.attack_bonus >= 0 else ""
        health_sign = "+" if countryball.health_bonus >= 0 else ""

        await interaction.response.send_message(
            f"Added `#{countryball.id} {countryball.countryball.country} ({attack_sign}{countryball.attack_bonus}%/{health_sign}{countryball.health_bonus}%)`!",
            ephemeral=True,
        )

        await self.interactions[interaction.guild_id].edit_original_response(
            embed=update_embed(
                guild_battle.battle.p1_balls,
                guild_battle.battle.p2_balls,
                guild_battle.author.name,
                guild_battle.opponent.name,
                guild_battle.author_ready,
                guild_battle.opponent_ready,
                guild_battle.deck_size,
            )
        )

    @app_commands.command()
    async def remove(
        self, interaction: discord.Interaction, countryball: BallEnabledTransform
    ):
        """
        Remove a ball from a battle.
        """
        guild_battle = self.battles.get(interaction.guild_id)
        if not guild_battle:
            await interaction.response.send_message(
                "There is no ongoing battle in this server!", ephemeral=True
            )
            return

        if (interaction.user == guild_battle.author and guild_battle.author_ready) or (
            interaction.user == guild_battle.opponent and guild_battle.opponent_ready
        ):
            await interaction.response.send_message(
                "You cannot change your balls as you are already ready.", ephemeral=True
            )
            return

        if interaction.user not in (guild_battle.author, guild_battle.opponent):
            await interaction.response.send_message(
                "You aren't a part of this battle!", ephemeral=True
            )
            return

        user_balls = (
            guild_battle.battle.p1_balls
            if interaction.user == guild_battle.author
            else guild_battle.battle.p2_balls
        )

        ball_to_remove = BattleBall(
            countryball.countryball.country,
            interaction.user.name,
            countryball.health,
            countryball.attack,
            self.bot.get_emoji(countryball.countryball.emoji_id),
        )

        if ball_to_remove in user_balls:
            user_balls.remove(ball_to_remove)

            attack_sign = "+" if countryball.attack_bonus >= 0 else ""
            health_sign = "+" if countryball.health_bonus >= 0 else ""

            await interaction.response.send_message(
                f"Removed `#{countryball.id} {countryball.countryball.country} ({attack_sign}{countryball.attack_bonus}%/{health_sign}{countryball.health_bonus}%)`!",
                ephemeral=True,
            )

            await self.interactions[interaction.guild_id].edit_original_response(
                embed=update_embed(
                    guild_battle.battle.p1_balls,
                    guild_battle.battle.p2_balls,
                    guild_battle.author.name,
                    guild_battle.opponent.name,
                    guild_battle.author_ready,
                    guild_battle.opponent_ready,
                    guild_battle.deck_size,
                )
            )
        else:
            await interaction.response.send_message(
                f"That ball is not in your deck!", ephemeral=True
            )




