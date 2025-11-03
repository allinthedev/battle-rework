from typing import TYPE_CHECKING

from ballsdex.packagese.journal.cog import Journal

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot


async def setup(bot: "BallsDexBot"):
    await bot.add_cog(Journal(bot))
