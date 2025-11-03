from typing import TYPE_CHECKING

from ballsdex.packages.map.cog import Map

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Map(bot))
