import asyncio
import logging
import typing as t
from enum import Enum

import discord
from discord.ext.commands import Cog

from bot.bot import Bot
from bot.constants import Channels, Emojis, Roles

log = logging.getLogger(__name__)


class Signal(Enum):
    """Recognized incident status signals."""

    ACTIONED = Emojis.incident_actioned
    NOT_ACTIONED = Emojis.incident_unactioned
    INVESTIGATING = Emojis.incident_investigating


ALLOWED_ROLES: t.Set[int] = {Roles.moderators, Roles.admins, Roles.owners}
ALLOWED_EMOJI: t.Set[str] = {signal.value for signal in Signal}


class Incidents(Cog):
    """Automation for the #incidents channel."""

    def __init__(self, bot: Bot) -> None:
        """Schedule `crawl_task` on start-up."""
        self.bot = bot
        self.crawl_task = self.bot.loop.create_task(self.crawl_incidents())

    async def crawl_incidents(self) -> None:
        """
        Crawl #incidents and add missing emoji where necessary.

        This is to catch-up should an incident be reported while the bot wasn't listening.
        Internally, we simply walk the channel history and pass each message to `on_message`.

        In order to avoid drowning in ratelimits, we take breaks after each message.

        Once this task is scheduled, listeners should await it. The crawl assumes that
        the channel history doesn't change as we go over it.
        """
        await self.bot.wait_until_guild_available()
        incidents: discord.TextChannel = self.bot.get_channel(Channels.incidents)

        # Limit the query at 50 as in practice, there should never be this many messages,
        # and if there are, something has likely gone very wrong
        limit = 50

        # Seconds to sleep after each message
        sleep = 2

        log.debug(f"Crawling messages in #incidents: {limit=}, {sleep=}")
        async for message in incidents.history(limit=limit):
            await self.on_message(message)
            await asyncio.sleep(sleep)

        log.debug("Crawl task finished!")

    @staticmethod
    async def add_signals(incident: discord.Message) -> None:
        """Add `Signal` member emoji to `incident` as reactions."""
        for signal_emoji in Signal:
            log.debug(f"Adding reaction: {signal_emoji.value}")
            await incident.add_reaction(signal_emoji.value)

    @Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """
        Pass each incident sent in #incidents to `add_signals`.

        We recognize several exceptions. The following will be ignored:
            * Messages sent outside of #incidents
            * Messages Sent by bots
            * Messages starting with the hash symbol #
            * Pinned (header) messages

        Prefix message with # in situations where a verbal response is necessary.
        Each such message must be deleted manually.
        """
        if message.channel.id != Channels.incidents or message.author.bot:
            return

        if message.content.startswith("#"):
            log.debug(f"Ignoring comment message: {message.content=}")
            return

        if message.pinned:
            log.debug(f"Ignoring header message: {message.pinned=}")
            return

        await self.add_signals(message)
