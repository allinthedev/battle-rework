from typing import TYPE_CHECKING, cast

import discord
import logging
from discord import app_commands
from discord.ext import commands
from tortoise.functions import Count

from ballsdex.core.models import Player, balls, BallInstance
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

log = logging.getLogger("ballsdex.packages.map")

bot_countryballs = {
    x: y for x, y in balls.items()
    if y.enabled
}

# If you're adding this, theres a few things to take note of:
# 1 - You need a currency system. If you have any other ways of giving rewards, code it yourself!
# 2 - You also need these properties in both your Player models, yes, for tortoise and Django. I have it for you right here:
# @property
#    def claimed_map_rewards(self) -> set[str]:
#        return set(self.extra_data.get("claimed_map_rewards", []))

#    async def mark_map_reward_claimed(self, regime_name: str) -> None:
#        claimed = self.extra_data.get("claimed_map_rewards", [])
#        if regime_name not in claimed:
#            claimed.append(regime_name)
#            self.extra_data["claimed_map_rewards"] = claimed
#            await self.save()

# This will allow completed regimes to be saved in the Database. This can also be a susbtitute for achievement titles, which is rlly neat!

class MapView(discord.ui.View):
    def __init__(self, bot, player, balls, ball_counts):
        super().__init__(timeout=None)
        self.bot = bot
        self.player = player
        self.balls = balls
        self.ball_counts = ball_counts
        self.current_regime = None

        self.add_item(self.build_regime_select())
        self.add_item(self.build_quit_button())

    def build_intro_embed(self):
        claimed = sorted(self.player.claimed_map_rewards) 
        claimed_text = "\n".join(f"✅ {name}" for name in claimed) if claimed else "None yet."

        embed = discord.Embed(
            title="🗺️ The Dex Map",
            description="Select an area to view your progress and claim rewards.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="🎖️ Completed Areas", value=claimed_text, inline=False)
        return embed

    def build_regime_select(self):
        regime_names = sorted({
            str(ball.cached_regime.name)
            for ball in self.balls.values()
            if ball.enabled
        })

        options = [discord.SelectOption(label=name, value=name) for name in regime_names]
        select = discord.ui.Select(placeholder="Choose an area", options=options)

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.player.discord_id:
                await interaction.response.send_message("This isn't your map!", ephemeral=True)
                return
            self.current_regime = select.values[0]
            self.clear_items()
            self.add_item(self.build_regime_select())
            self.add_item(self.build_quit_button())
            embed = self.build_regime_embed(self.current_regime)
            await interaction.response.edit_message(embed=embed, view=self)

        select.callback = callback
        return select

    def build_regime_embed(self, regime_name):
        ball_ids = [
            ball_id for ball_id, ball in self.balls.items()
            if ball.enabled and str(ball.cached_regime.name) == regime_name
        ]
        owned = [
            ball_id for ball_id in ball_ids
            if self.ball_counts.get(ball_id, 0) >= 1
        ]
        completion = round(len(owned) / len(ball_ids) * 100, 1) if ball_ids else 0.0

        if owned:
            rarest = min(owned, key=lambda b: self.balls[b].rarity or 9999)
            most_owned = max(owned, key=lambda b: self.ball_counts.get(b, 0))
        else:
            rarest = most_owned = None

        embed = discord.Embed(
            title=f"{regime_name} Stats",
            description=f"🗺️ Completion: {completion}%\nRemaining: {100 - completion:.1f}%",
            color=discord.Color.blurple()
        )

        if rarest:
            embed.add_field(name="Rarest Owned", value=self.balls[rarest].country, inline=True)
        else:
            embed.add_field(name="Rarest Owned", value="None", inline=True)

        if most_owned:
            embed.add_field(
                name="Most Owned",
                value=f"{self.balls[most_owned].country} — x{self.ball_counts.get(most_owned, 0)}",
                inline=True
            )
        else:
            embed.add_field(name="Most Owned", value="None", inline=True)

        embed.add_field(name="Completed Entries", value=str(len(owned)), inline=True)

        if completion == 100 and regime_name not in self.player.claimed_map_rewards:
            self.add_item(self.claim_reward_button(regime_name))

        return embed

    def claim_reward_button(self, regime_name):
        button = discord.ui.Button(
            label="Claim Reward",
            style=discord.ButtonStyle.success,
            custom_id=f"claim_{regime_name}_{id(self)}"
        )

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.player.discord_id:
                await interaction.response.send_message("This isn't your map!", ephemeral=True)
                return

            button.disabled = True
            await self.player.mark_map_reward_claimed(regime_name)
            # currency logic, beware!!
            self.player.coins += 750
            await self.player.save()
            await interaction.response.send_message(
                f"🎉 You claimed the reward for **{regime_name}**! You have earned **750** Coins.",
                ephemeral=True
            )
            self.clear_items()
            self.add_item(self.build_regime_select())
            self.add_item(button)
            self.add_item(self.build_quit_button())
            embed = self.build_regime_embed(regime_name)
            await interaction.message.edit(embed=embed, view=self)

        button.callback = callback
        return button

    def build_quit_button(self):
        button = discord.ui.Button(
            label="Quit",
            style=discord.ButtonStyle.danger,
            custom_id=f"quit_{id(self)}"
        )

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.player.discord_id:
                await interaction.response.send_message("This isn't your map!", ephemeral=True)
                return
            await interaction.response.edit_message(content="Map closed.", embed=None, view=None)

        button.callback = callback
        return button

class Map(commands.GroupCog, group_name="map"):
    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot

    @app_commands.command(name="view", description="View your area stats and claim rewards")
    async def view(self, interaction: discord.Interaction):
        player = await Player.get(discord_id=interaction.user.id)
        balls = bot_countryballs

        filters = {"player__discord_id": player.discord_id, "ball__enabled": True}
        ball_counts_raw = await BallInstance.filter(**filters) \
            .annotate(count=Count("id")) \
            .group_by("ball_id") \
            .values("ball_id", "count")

        ball_counts = {
            int(x["ball_id"]): x["count"]
            for x in ball_counts_raw
        }

        view = MapView(
            bot=self.bot,
            player=player,
            balls=balls,
            ball_counts=ball_counts
        )

        await interaction.response.send_message(
            embed=view.build_intro_embed(),
            view=view
        )
