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

log = logging.getLogger("ballsdex.packages.countryballs")

COOLDOWN_FILE = os.path.join(os.path.dirname(__file__), "cooldown.json")
COOLDOWN_SECONDS = 28800

def a_or_an(word: str) -> str:
    vowels = "aeiou"
    word = word.strip()
    if not word:
        return "a"

    lowered = word.lower()
    special_cases = ("npc", "enemy")

    if lowered.startswith(special_cases) or lowered[0] in vowels:
        return "an"

    if word[0] == "N" and word.isupper():
        return "an"

    return "a"

def load_cooldowns():
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    with open(COOLDOWN_FILE, "r") as f:
        try:
            return json.load(f)
        except Exception:
            return {}

def save_cooldowns(cooldowns):
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cooldowns, f)

class ClaimConfirmationView(discord.ui.View):
    def __init__(self, player: Player):
        super().__init__(timeout=None)
        self.player = player
        self.result = None

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, emoji=discord.PartialEmoji(name="geo", id=1416755005637132469))
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.geo < 75:
            await interaction.response.send_message("You don't have enough geo to claim an enemy, sorry pal!.", ephemeral=True)
            self.result = False
            self.stop()
            return

        self.player.geo -= 75
        await self.player.save()
        await interaction.response.edit_message(
            content="Confirmed! Processing...",
            view=None
        )
        self.result = True
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.red)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Cancelled successfully..",
            view=None
        )
        self.result = False
        self.stop()

class DonationRequest(View):
    def __init__(
        self,
        bot: "BallsDexBot",
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstance,
        new_player: Player,
    ):
        super().__init__(timeout=120)
        self.bot = bot
        self.original_interaction = interaction
        self.countryball = countryball
        self.new_player = new_player

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
        if interaction.user.id != self.new_player.discord_id:
            await interaction.response.send_message(
                "You are not allowed to interact with this menu.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # type: ignore
        try:
            await self.original_interaction.followup.edit_message(
                "@original", view=self  # type: ignore
            )
        except discord.NotFound:
            pass
        await self.countryball.unlock()

    @button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore
        self.countryball.favorite = False
        self.countryball.trade_player = self.countryball.player
        self.countryball.player = self.new_player
        await self.countryball.save()
        trade = await Trade.create(player1=self.countryball.trade_player, player2=self.new_player)
        await TradeObject.create(
            trade=trade, ballinstance=self.countryball, player=self.countryball.trade_player
        )
        await interaction.response.edit_message(
            content=interaction.message.content  # type: ignore
            + "\n\N{WHITE HEAVY CHECK MARK} The donation was accepted!",
            view=self,
        )
        await self.countryball.unlock()

    @button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        self.stop()
        for item in self.children:
            item.disabled = True  # type: ignore
        await interaction.response.edit_message(
            content=interaction.message.content  # type: ignore
            + "\n\N{CROSS MARK} The donation was denied.",
            view=self,
        )
        await self.countryball.unlock()


class DuplicateType(enum.StrEnum):
    countryballs = settings.plural_collectible_name
    specials = "specials"


class Balls(commands.GroupCog, group_name=settings.players_group_cog_name):
    """
    View and manage your countryballs collection.
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @app_commands.command()
    @app_commands.checks.cooldown(1, 10, key=lambda i: i.user.id)
    async def journal(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User | None = None,
    ):
        """Show your current completion of the HollowDex."""
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

        view = RegimeJournalView(
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


    @app_commands.command()
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def info(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Display info from a specific countryball.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to inspect
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        await interaction.response.defer(thinking=True)
        content, file, view = await countryball.prepare_for_message(interaction)
        await interaction.followup.send(content=content, file=file, view=view)
        file.close()

    @app_commands.command()
    @app_commands.checks.cooldown(1, 5, key=lambda i: i.user.id)
    async def last(
        self, interaction: discord.Interaction["BallsDexBot"], user: discord.User | None = None
    ):
        """
        Display info of your or another users last caught countryball.

        Parameters
        ----------
        user: discord.Member
            The user you would like to see
        """
        user_obj = user if user else interaction.user
        await interaction.response.defer(thinking=True)
        try:
            player = await Player.get(discord_id=user_obj.id)
        except DoesNotExist:
            msg = f"{'You do' if user is None else f'{user_obj.display_name} does'}"
            await interaction.followup.send(
                f"{msg} not have any {settings.plural_collectible_name} yet.",
                ephemeral=True,
            )
            return

        if user is not None:
            if await inventory_privacy(self.bot, interaction, player, user_obj) is False:
                return

        interaction_player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        blocked = await player.is_blocked(interaction_player)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                f"You cannot view the last caught {settings.collectible_name} "
                "of a user that has blocked you.",
                ephemeral=True,
            )
            return

        countryball = await player.balls.all().order_by("-id").first().select_related("ball")
        if not countryball:
            msg = f"{'You do' if user is None else f'{user_obj.display_name} does'}"
            await interaction.followup.send(
                f"{msg} not have any {settings.plural_collectible_name} yet.",
                ephemeral=True,
            )
            return

        content, file, view = await countryball.prepare_for_message(interaction)
        if user is not None and user.id != interaction.user.id:
            content = (
                f"You are viewing {user.display_name}'s last caught {settings.collectible_name}.\n"
                + content
            )
        await interaction.followup.send(content=content, file=file, view=view)
        file.close()

    @app_commands.command()
    async def favorite(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Set favorite countryballs.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to set/unset as favorite
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return

        if settings.max_favorites == 0:
            await interaction.response.send_message(
                f"You cannot set favorite {settings.plural_collectible_name} in this bot."
            )
            return

        if not countryball.favorite:
            try:
                player = await Player.get(discord_id=interaction.user.id).prefetch_related("balls")
            except DoesNotExist:
                await interaction.response.send_message(
                    f"You don't have any {settings.plural_collectible_name} yet.", ephemeral=True
                )
                return

            grammar = (
                f"{settings.collectible_name}"
                if settings.max_favorites == 1
                else f"{settings.plural_collectible_name}"
            )
            if await player.balls.filter(favorite=True).count() >= settings.max_favorites:
                await interaction.response.send_message(
                    f"You cannot set more than {settings.max_favorites} favorite {grammar}.",
                    ephemeral=True,
                )
                return

            countryball.favorite = True  # type: ignore
            await countryball.save()
            emoji = self.bot.get_emoji(countryball.countryball.emoji_id) or ""
            await interaction.response.send_message(
                f"{emoji} `#{countryball.pk:0X}` {countryball.countryball.country} "
                f"is now a favorite {settings.collectible_name}!",
                ephemeral=True,
            )

        else:
            countryball.favorite = False  # type: ignore
            await countryball.save()
            emoji = self.bot.get_emoji(countryball.countryball.emoji_id) or ""
            await interaction.response.send_message(
                f"{emoji} `#{countryball.pk:0X}` {countryball.countryball.country} "
                f"isn't a favorite {settings.collectible_name} anymore.",
                ephemeral=True,
            )

    @app_commands.command(extras={"trade": TradeCommandType.PICK})
    async def give(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User,
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Give a countryball to a user.

        Parameters
        ----------
        user: discord.User
            The user you want to give a countryball to
        countryball: BallInstance
            The countryball you're giving away
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        if not countryball.is_tradeable:
            await interaction.response.send_message(
                f"You cannot donate this {settings.collectible_name}.", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message("You cannot donate to bots.", ephemeral=True)
            return
        if await countryball.is_locked():
            await interaction.response.send_message(
                f"This {settings.collectible_name} is currently locked for a trade. "
                "Please try again later.",
                ephemeral=True,
            )
            return
        favorite = countryball.favorite
        if favorite:
            view = ConfirmChoiceView(
                interaction,
                accept_message=f"{settings.collectible_name.title()} donated.",
                cancel_message="This request has been cancelled.",
            )
            await interaction.response.send_message(
                f"This {settings.collectible_name} is a favorite, "
                "are you sure you want to donate it?",
                view=view,
                ephemeral=True,
            )
            await view.wait()
            if not view.value:
                return
            interaction = view.interaction_response
        else:
            await interaction.response.defer()
        await countryball.lock_for_trade()
        new_player, _ = await Player.get_or_create(discord_id=user.id)
        old_player = countryball.player

        if new_player == old_player:
            await interaction.followup.send(
                f"You cannot give a {settings.collectible_name} to yourself.", ephemeral=True
            )
            await countryball.unlock()
            return
        if new_player.donation_policy == DonationPolicy.ALWAYS_DENY:
            await interaction.followup.send(
                "This player does not accept donations. You can use trades instead.",
                ephemeral=True,
            )
            await countryball.unlock()
            return

        friendship = await new_player.is_friend(old_player)
        if new_player.donation_policy == DonationPolicy.FRIENDS_ONLY:
            if not friendship:
                await interaction.followup.send(
                    "This player only accepts donations from friends, use trades instead.",
                    ephemeral=True,
                )
                await countryball.unlock()
                return
        blocked = await new_player.is_blocked(old_player)
        if blocked:
            await interaction.followup.send(
                "You cannot interact with a user that has blocked you.", ephemeral=True
            )
            await countryball.unlock()
            return
        if new_player.discord_id in self.bot.blacklist:
            await interaction.followup.send(
                "You cannot donate to a blacklisted user.", ephemeral=True
            )
            await countryball.unlock()
            return
        elif new_player.donation_policy == DonationPolicy.REQUEST_APPROVAL:
            await interaction.followup.send(
                f"Hey {user.mention}, {interaction.user.name} wants to give you "
                f"{countryball.description(include_emoji=True, bot=self.bot, is_trade=True)}!\n"
                "Do you accept this donation?",
                view=DonationRequest(self.bot, interaction, countryball, new_player),
                allowed_mentions=await can_mention([new_player, old_player]),
            )
            return

        countryball.player = new_player
        countryball.trade_player = old_player
        countryball.favorite = False
        await countryball.save()

        trade = await Trade.create(player1=old_player, player2=new_player)
        await TradeObject.create(trade=trade, ballinstance=countryball, player=old_player)

        cb_txt = (
            countryball.description(short=True, include_emoji=True, bot=self.bot, is_trade=True)
            + f" (`{countryball.attack_bonus:+}%/{countryball.health_bonus:+}%`)"
        )
        if favorite:
            await interaction.followup.send(
                f"{interaction.user.mention}, you just gave the "
                f"{settings.collectible_name} {cb_txt} to {user.mention}!",
                allowed_mentions=await can_mention([new_player, old_player]),
            )
        else:
            await interaction.followup.send(
                f"You just gave the {settings.collectible_name} {cb_txt} to {user.mention}!",
                allowed_mentions=await can_mention([new_player]),
            )
        await countryball.unlock()

    @app_commands.command()
    async def count(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        special: SpecialEnabledTransform | None = None,
        current_server: bool = False,
    ):
        """
        Count how many countryballs you have.

        Parameters
        ----------
        countryball: Ball
            The countryball you want to count
        special: Special
            The special you want to count
        current_server: bool
            Only count countryballs caught in the current server
        """
        if interaction.response.is_done():
            return

        assert interaction.guild
        filters = {}
        if countryball:
            filters["ball"] = countryball
        if special:
            filters["special"] = special
        if current_server:
            filters["server_id"] = interaction.guild.id
        filters["player__discord_id"] = interaction.user.id

        await interaction.response.defer(ephemeral=True, thinking=True)

        balls = await BallInstance.filter(**filters).count()
        country = f"{countryball.country} " if countryball else ""
        plural = "s" if balls > 1 or balls == 0 else ""
        special_str = f"{special.name} " if special else ""
        guild = f" caught in {interaction.guild.name}" if current_server else ""

        await interaction.followup.send(
            f"You have {balls} {special_str}"
            f"{country}{settings.collectible_name}{plural}{guild}."
        )

    @app_commands.command()
    @app_commands.checks.cooldown(1, 20, key=lambda i: i.user.id)
    async def duplicate(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        type: DuplicateType,
        limit: int | None = None,
    ):
        """
        Shows your most duplicated countryballs or specials.

        Parameters
        ----------
        type: DuplicateType
            Type of duplicate to check (countryballs or specials).
        limit: int | None
            The amount of countryballs to show, can only be used with `countryballs`.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)

        player, _ = await Player.get_or_create(discord_id=interaction.user.id)
        is_special = type == DuplicateType.specials
        queryset = BallInstance.filter(player=player)

        if is_special:
            queryset = queryset.filter(special_id__isnull=False).prefetch_related("special")
            annotations = {"name": "special__name", "emoji": "special__emoji"}
            apply_limit = False
        else:
            queryset = queryset.filter(ball__tradeable=True)
            annotations = {"name": "ball__country", "emoji": "ball__emoji_id"}
            apply_limit = True

        query = (
            queryset.annotate(count=Count("id")).group_by(*annotations.values()).order_by("-count")
        )

        if apply_limit and limit is not None:
            query = query.limit(limit)

        query = query.values(*annotations.values(), "count")
        results = await query

        if not results:
            await interaction.followup.send(
                f"You don't have any {type.value} duplicates in your inventory.", ephemeral=True
            )
            return

        entries = [
            {
                "name": item[annotations["name"]],
                "emoji": (
                    self.bot.get_emoji(item[annotations["emoji"]]) or item[annotations["emoji"]]
                ),
                "count": item["count"],
            }
            for item in results
        ]

        source = DuplicateViewMenu(interaction, entries, type.value)
        await source.start(content=f"View your duplicate {type.value}.")

    @app_commands.command()
    @app_commands.checks.cooldown(1, 20, key=lambda i: i.user.id)
    async def compare(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        user: discord.User,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Compare your countryballs with another user.

        Parameters
        ----------
        user: discord.User
            The user you want to compare with
        special: Special
            Filter the results of the comparison to a special event.
        """
        await interaction.response.defer(thinking=True)
        if interaction.user == user:
            await interaction.followup.send("You cannot compare with yourself.", ephemeral=True)
            return

        try:
            player = await Player.get(discord_id=user.id)
        except DoesNotExist:
            await interaction.followup.send(
                f"{user.display_name} doesn't have any {settings.plural_collectible_name} yet."
            )
            return

        if await inventory_privacy(self.bot, interaction, player, user) is False:
            return

        bot_countryballs = {x: y.emoji_id for x, y in balls.items() if y.enabled}
        if special:
            bot_countryballs = {
                x: y.emoji_id
                for x, y in balls.items()
                if y.enabled and (special.end_date is None or y.created_at < special.end_date)
            }

        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)

        blocked = await player.is_blocked(player1)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot compare with a user that has you blocked.", ephemeral=True
            )
            return

        blocked = await player.is_blocked(player2)
        if blocked and not is_staff(interaction):
            await interaction.followup.send(
                "You cannot compare with a user that has you blocked.", ephemeral=True
            )
            return
        queryset = BallInstance.filter(ball__enabled=True).distinct()
        if special:
            queryset = queryset.filter(special=special)
        user1_balls = cast(
            list[int],
            await queryset.filter(player=player1).values_list("ball_id", flat=True),
        )
        user2_balls = cast(
            list[int],
            await queryset.filter(player=player2).values_list("ball_id", flat=True),
        )
        both = set(user1_balls) & set(user2_balls)
        user1_only = set(user1_balls) - set(user2_balls)
        user2_only = set(user2_balls) - set(user1_balls)
        neither = set(bot_countryballs.keys()) - both - user1_only - user2_only

        entries = []

        def fill_fields(title: str, ids: set[int]):
            first_field_added = False
            buffer = ""

            for ball_id in ids:
                emoji = self.bot.get_emoji(bot_countryballs[ball_id])
                if not emoji:
                    continue

                text = f"{emoji} "
                if len(buffer) + len(text) > 1024:
                    # hitting embed limits, adding an intermediate field
                    if first_field_added:
                        entries.append(("\u200b", buffer))
                    else:
                        entries.append((f"__**{title}**__", buffer))
                        first_field_added = True
                    buffer = ""
                buffer += text

            if buffer:  # add what's remaining
                if first_field_added:
                    entries.append(("\u200b", buffer))
                else:
                    entries.append((f"__**{title}**__", buffer))

        if both:
            fill_fields("Both have", both)
        else:
            entries.append(("__**Both have**__", "None"))
        fill_fields(f"{interaction.user.display_name} has", user1_only)
        fill_fields(f"{user.display_name} has", user2_only)
        fill_fields("Neither have", neither)

        source = FieldPageSource(entries, per_page=5, inline=False, clear_description=False)
        special_str = f" ({special.name})" if special else ""
        source.embed.title = (
            f"Comparison of {interaction.user.display_name} and {user.display_name}'s "
            f"{settings.plural_collectible_name}{special_str}"
        )
        source.embed.colour = discord.Colour.blurple()

        pages = Pages(source=source, interaction=interaction, compact=True)
        await pages.start()

    @app_commands.command()
    async def collection(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        ephemeral: bool = False,
    ):
        """
        Show the collection of a specific countryball.

        Parameters
        ----------
        countryball: Ball
            The countryball you want to see the collection of
        ephemeral: bool
            Whether or not to send the command ephemerally.
        """
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        query = (
            BallInstance.filter(player=player)
            .annotate(
                total=RawSQL("COUNT(*)"),
                traded=RawSQL("SUM(CASE WHEN trade_player_id IS NULL THEN 0 ELSE 1 END)"),
                specials=RawSQL("SUM(CASE WHEN special_id IS NULL THEN 0 ELSE 1 END)"),
            )
            .group_by("player_id")
        )
        specials = (
            BallInstance.filter(player=player)
            .exclude(special=None)
            .annotate(count=Count("id"))
            .group_by("special__name")
        )
        if countryball:
            query = query.filter(ball=countryball)
            specials = specials.filter(ball=countryball)
        counts = (await query.values("player_id", "total", "traded", "specials"))[0]
        specials = await specials.values("special__name", "count")

        if not counts["total"]:
            if countryball:
                await interaction.followup.send(
                    f"You don't have any {countryball.country} "
                    f"{settings.plural_collectible_name} yet."
                )
            else:

                await interaction.followup.send(
                    f"You don't have any {settings.plural_collectible_name} yet."
                )
            return
        all_specials = await Special.filter(hidden=False)
        special_emojis = {x.name: x.emoji for x in all_specials}

        desc = (
            f"**Total**: {counts["total"]:,} ({counts["total"] - counts["traded"]:,} caught, "
            f"{counts['traded']:,} received from trade)\n"
            f"**Total Specials**: {counts['specials']:,}\n\n"
        )
        if counts["specials"]:
            desc += "**Specials**:\n"
        for special in sorted(specials, key=lambda x: x["count"], reverse=True):
            emoji = special_emojis.get(special["special__name"], "")
            desc += f"{emoji} {special['special__name']}: {special["count"]:,}\n"

        embed = discord.Embed(
            title=f"Collection of {countryball.country}" if countryball else "Total Collection",
            description=desc,
            color=discord.Color.blurple(),
        )
        embed.set_author(
            name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url
        )
        if countryball:
            emoji = self.bot.get_emoji(countryball.emoji_id)
            if emoji:
                embed.set_thumbnail(url=emoji.url)
        await interaction.followup.send(embed=embed)

    @app_commands.command()
    async def claim(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        Use 75 geo to claim a random Enemy/Boss/NPC every 8 hours.
        """
        await interaction.response.defer(ephemeral=True, thinking=True)

        cooldowns = await asyncio.to_thread(load_cooldowns)
        user_id = str(interaction.user.id)
        now = int(time.time())
        last_claim = int(cooldowns.get(user_id, 0))
        remaining = COOLDOWN_SECONDS - (now - last_claim)
        if remaining > 0:
            hours = remaining // 3600
            minutes = (remaining % 3600) // 60
            seconds = remaining % 60
            await interaction.followup.send(
                f"You have already claimed an {settings.collectible_name}. Try again in {hours}h {minutes}m {seconds}s.",
                ephemeral=True,
            )
            return

        player, _ = await Player.get_or_create(discord_id=interaction.user.id)

        view = ClaimConfirmationView(player)
        await interaction.followup.send(
            f"Claiming a random Enemy/Boss/NPC costs <:geo:1416755005637132469> **75 Geo**. You currently have <:geo:1416755005637132469> **{player.geo}**. Do you want to proceed?",
            view=view,
            ephemeral=True,
        )
        await view.wait()

        if not view.result:
            return 

        available_balls = [ball for ball in balls.values() if ball.enabled and ball.rarity > 0]
        if not available_balls:
            await interaction.followup.send(
                f"There is no {settings.collectible_name} available to claim at the moment.", ephemeral=True
            )
            return

        specials = await Special.all()
        special_weights = [special.rarity for special in specials]
        weights = special_weights + [1] * len(available_balls)
        claimed_ball = random.choices(specials + available_balls, weights=weights, k=1)[0]

        ball_instance = await BallInstance.create(
            ball=claimed_ball if isinstance(claimed_ball, Ball) else None,
            player=player,
            attack_bonus=random.randint(-settings.max_attack_bonus, settings.max_attack_bonus),
            health_bonus=random.randint(-settings.max_health_bonus, settings.max_health_bonus),
            special=None,
        )

        cooldowns[user_id] = now
        await asyncio.to_thread(save_cooldowns, cooldowns)

        economy = await claimed_ball.economy if isinstance(claimed_ball, Ball) else None
        _, file, _ = await ball_instance.prepare_for_message(interaction)
        emoji = self.bot.get_emoji(claimed_ball.emoji_id if isinstance(claimed_ball, Ball) else None)

        an = a_or_an(str(economy))

        embed = discord.Embed(
            title=f"{interaction.user.mention}, you've claimed {an} **{str(economy)}!**",
            description=f"{emoji} **{claimed_ball.country if isinstance(claimed_ball, Ball) else claimed_ball.name}**",
            color=discord.Color.blurple()
        )
        embed.set_footer(text="You can claim again in 8 hours")
        embed.set_image(url=f"attachment://{file.filename}")

        await interaction.followup.send(
            embed=embed,
            file=file,
        )
        file.close()

    @app_commands.command(name="refresh_cooldowns", description="Wipe all user cooldowns")
    @app_commands.checks.has_any_role(*settings.root_role_ids)
    async def refresh_cooldowns(self, interaction: discord.Interaction):   
        await asyncio.to_thread(save_cooldowns, {})

        await interaction.response.send_message("✅ All cooldowns have been refreshed.", ephemeral=True)