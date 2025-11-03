import enum
import logging
import random
import asyncio
import time
import os
import json
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, button
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import RawSQL
from tortoise.functions import Count
from collections import defaultdict

from ballsdex.core.models import (
    BallInstance,
    Ball,
    DonationPolicy,
    Player,
    Special,
    Trade,
    TradeObject,
    balls,
)
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import FieldPageSource, Pages
from ballsdex.core.utils.sorting import FilteringChoices, SortingChoices, filter_balls, sort_balls
from ballsdex.core.utils.transformers import (
    BallEnabledTransform,
    BallInstanceTransform,
    SpecialEnabledTransform,
    TradeCommandType,
)
from ballsdex.core.utils.utils import can_mention, inventory_privacy, is_staff
from ballsdex.packages.balls.countryballs_paginator import DuplicateViewMenu, RegimeJournalView
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.journal")

class JournalView(discord.ui.View):
    def __init__(self, bot, user_obj, balls, owned_countryballs, ball_counts):
        super().__init__(timeout=None)
        self.bot = bot
        self.user_obj = user_obj
        self.player = user_obj
        self.balls = balls
        self.owned_countryballs = owned_countryballs
        self.ball_counts = ball_counts
        self.regime_embeds = []
        self.regime_ball_ids = []
        self.current_index = 0

        self.build_embeds_and_ids()
        self.update_view()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_obj.id:
            await interaction.response.send_message(
                "You are not allowed to interact with this menu.",
                ephemeral=True
            )
            return False
        return True

    def build_embeds_and_ids(self):
        def chunk_lines(lines, max_chars=1024):
            chunks = []
            current = ""
            for line in lines:
                if len(current) + len(line) + 1 > max_chars:
                    chunks.append(current)
                    current = line
                else:
                    current += ("\n" if current else "") + line
            if current:
                chunks.append(current)
            return chunks

        regime_to_ball_ids = defaultdict(set)
        for ball_id, ball in self.balls.items():
            if not ball.enabled:
                continue
            regime_name = str(ball.cached_regime.name).strip()
            regime_to_ball_ids[regime_name].add(ball_id)

        ordered_regimes = sorted(regime_to_ball_ids.items())

        for regime, ball_ids in ordered_regimes:
            owned = []
            missing = []
            for ball_id in sorted(ball_ids):
                ball = self.balls[ball_id]
                emoji = self.bot.get_emoji(ball.emoji_id)
                if not emoji:
                    continue
                count = self.ball_counts.get(ball_id, 0)
                status = "✅" if count >= 1 else "❌"
                line = f"{status} {emoji} **{ball.country}** — x{count}"
                if count >= 1:
                    owned.append(line)
                else:
                    missing.append(line)

            completion = round(len(owned) / len(ball_ids) * 100, 1) if ball_ids else 0.0
            embed = discord.Embed(
                title=f"{regime} | {completion}%",
                color=discord.Color.blurple()
            )
            owned_chunks = chunk_lines(owned)
            missing_chunks = chunk_lines(missing)

            for i in range(max(len(owned_chunks), len(missing_chunks))):
                owned_value = owned_chunks[i] if i < len(owned_chunks) else "\u200b"
                missing_value = missing_chunks[i] if i < len(missing_chunks) else "\u200b"
                embed.add_field(name="• __Owned entries__ •" if i == 0 else "\u200b", value=owned_value, inline=True)
                embed.add_field(name="• __Missing entries__ •" if i == 0 else "\u200b", value=missing_value, inline=True)

            self.regime_embeds.append(embed)
            self.regime_ball_ids.append(sorted(ball_ids))

    def update_view(self):
        self.clear_items()
        self.add_item(self.build_ball_select())
        self.add_item(self.first_button)
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.add_item(self.last_button)

    def build_ball_select(self):
        ball_ids = self.regime_ball_ids[self.current_index]
        options = []
        for ball_id in ball_ids:
            ball = self.balls[ball_id]
            emoji = self.bot.get_emoji(ball.emoji_id)
            count = self.ball_counts.get(ball_id, 0)
            status = "" if count >= 1 else "🔒"
            label = f"{status} {ball.country}"
            description = f"Caught x{count} — {'Unlocked' if count >= 1 else 'Locked'}"
            options.append(discord.SelectOption(label=label, value=str(ball_id), emoji=emoji, description=description))

        select = discord.ui.Select(placeholder="Choose an entry from this page", options=options)

        async def callback(interaction: discord.Interaction):
            ball_id = int(select.values[0])
            ball = self.balls[ball_id]
            count = self.ball_counts.get(ball_id, 0)
            economy = ball.cached_economy
            variant = getattr(economy, "name", None)

            if count >= 1:
                emoji = self.bot.get_emoji(ball.emoji_id)
                embed = discord.Embed(
                    title=f"{emoji} {ball.country}",
                    description=f"•-~ [ **{ball.capacity_description}** ] ~-•" if ball.capacity_description else "No biography available.",
                    color=discord.Color.blurple()
                )
                embed.add_field(name="Rarity", value=ball.rarity or "Unknown", inline=False)
                embed.add_field(name="Health", value=ball.health or "Unknown", inline=False)
                embed.add_field(name="Attack", value=ball.attack or "Unknown", inline=False)
                embed.add_field(name="Variant", value=f"**{str(variant)}**" if variant else "Unknown", inline=False)
                embed.set_footer(text=f"Caught x{count}")
                await interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"You haven't unlocked the journal entry for **{ball.country}** yet. "
                    f"Catch 1 more to unlock it.",
                    ephemeral=True
                )

        select.callback = callback
        return select

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary, custom_id="first")
    async def first_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = 0
        self.update_view()
        await interaction.response.edit_message(embed=self.regime_embeds[self.current_index], view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = (self.current_index - 1) % len(self.regime_embeds)
        self.update_view()
        await interaction.response.edit_message(embed=self.regime_embeds[self.current_index], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = (self.current_index + 1) % len(self.regime_embeds)
        self.update_view()
        await interaction.response.edit_message(embed=self.regime_embeds[self.current_index], view=self)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary, custom_id="last")
    async def last_button(self, interaction: discord.Interaction, button: Button):
        self.current_index = len(self.regime_embeds) - 1
        self.update_view()
        await interaction.response.edit_message(embed=self.regime_embeds[self.current_index], view=self)

class Journal(commands.GroupCog, group_name="journal"):
    """
    View and manage your journal stats.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @app_commands.command()
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def view(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
    ):
        """Show your current completion of the dex."""
        user_obj = user or interaction.user
        await interaction.response.defer(thinking=True)

        try:
            player = await Player.get(discord_id=user_obj.id)
        except DoesNotExist:
            await interaction.followup.send(
                f"{user_obj.name} doesn't have any {settings.plural_collectible_name} yet."
            )
            return

        interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        if await player.is_blocked(interaction_player) and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot view the journal of a user that has blocked you.",
                ephemeral=True,
            )
            return

        if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
            return

        bot_countryballs = {
            x: y for x, y in balls.items()
            if y.enabled
        }

        if not bot_countryballs:
            await interaction.followup.send(
                f"There are no {settings.plural_collectible_name} registered on this bot yet.",
                ephemeral=True,
            )
            return

        filters = {"player__discord_id": user_obj.id, "ball__enabled": True}

        owned_countryballs = set(
            x[0]
            for x in await BallInstance.filter(**filters)
            .distinct()
            .values_list("ball_id")
        )

        ball_counts_raw = await BallInstance.filter(**filters) \
            .annotate(count=Count("id")) \
            .group_by("ball_id") \
            .values("ball_id", "count")

        ball_counts = {
            int(x["ball_id"]): x["count"]
            for x in ball_counts_raw
        }

        view = JournalView(
            bot=self.bot,
            user_obj=user_obj,
            balls=bot_countryballs,
            owned_countryballs=owned_countryballs,
            ball_counts=ball_counts,
        )

        await interaction.followup.send(
            embed=view.regime_embeds[view.current_index],
            view=view
        )
