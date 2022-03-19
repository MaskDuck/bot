import asyncio
import socket
import warnings
from collections import defaultdict
from typing import Optional

import aiohttp
import discord
from async_rediscache import RedisSession
from discord.ext import commands

from botcore import AsyncStatsClient
from botcore.site_api import APIClient
from botcore.utils.logging import get_logger

log = get_logger()


class StartupError(Exception):
    """Exception class for startup errors."""

    def __init__(self, base: Exception):
        super().__init__()
        self.exception = base


class BotBase(commands.Bot):
    """A sub-class that implements many common features that Python Discord bots use."""

    def __init__(self, *args, guild_id: int, redis_session: RedisSession, **kwargs):
        if "connector" in kwargs:
            warnings.warn(
                "If login() is called (or the bot is started), the connector will be overwritten "
                "with an internal one"
            )

        super().__init__(*args, **kwargs)

        self.guild_id = guild_id

        self.http_session: Optional[aiohttp.ClientSession] = None
        self.redis_session = redis_session
        self.api_client: Optional[APIClient] = None
        self.filter_list_cache = defaultdict(dict)

        self._connector: Optional[aiohttp.AsyncResolver] = None
        self._resolver: Optional[aiohttp.TCPConnector] = None
        self.statsd_url: Optional[str] = None
        self._statsd_timerhandle: Optional[asyncio.TimerHandle] = None
        self._guild_available: Optional[asyncio.Event] = None

        self.stats: Optional[AsyncStatsClient] = None

    def _connect_statsd(
        self,
        statsd_url: str,
        loop: asyncio.AbstractEventLoop,
        retry_after: int = 2,
        attempt: int = 1
    ) -> None:
        """Callback used to retry a connection to statsd if it should fail."""
        if attempt >= 8:
            log.error("Reached 8 attempts trying to reconnect AsyncStatsClient. Aborting")
            return

        try:
            self.stats = AsyncStatsClient(loop, statsd_url, 8125, prefix="bot")
        except socket.gaierror:
            log.warning(f"Statsd client failed to connect (Attempt(s): {attempt})")
            # Use a fallback strategy for retrying, up to 8 times.
            self._statsd_timerhandle = loop.call_later(
                retry_after,
                self._connect_statsd,
                statsd_url,
                retry_after * 2,
                attempt + 1
            )

        # All tasks that need to block closing until finished
        self.closing_tasks: list[asyncio.Task] = []

    async def load_extensions(self, extensions_to_load: set) -> None:
        """Load all enabled extensions."""
        for extension in extensions_to_load:
            await self.load_extension(extension)

    def _add_root_aliases(self, command: commands.Command) -> None:
        """Recursively add root aliases for `command` and any of its subcommands."""
        if isinstance(command, commands.Group):
            for subcommand in command.commands:
                self._add_root_aliases(subcommand)

        for alias in getattr(command, "root_aliases", ()):
            if alias in self.all_commands:
                raise commands.CommandRegistrationError(alias, alias_conflict=True)

            self.all_commands[alias] = command

    def _remove_root_aliases(self, command: commands.Command) -> None:
        """Recursively remove root aliases for `command` and any of its subcommands."""
        if isinstance(command, commands.Group):
            for subcommand in command.commands:
                self._remove_root_aliases(subcommand)

        for alias in getattr(command, "root_aliases", ()):
            self.all_commands.pop(alias, None)

    async def add_cog(self, cog: commands.Cog) -> None:
        """Adds a "cog" to the bot and logs the operation."""
        await super().add_cog(cog)
        log.info(f"Cog loaded: {cog.qualified_name}")

    def add_command(self, command: commands.Command) -> None:
        """Add `command` as normal and then add its root aliases to the bot."""
        super().add_command(command)
        self._add_root_aliases(command)

    def remove_command(self, name: str) -> Optional[commands.Command]:
        """
        Remove a command/alias as normal and then remove its root aliases from the bot.

        Individual root aliases cannot be removed by this function.
        To remove them, either remove the entire command or manually edit `bot.all_commands`.
        """
        command = super().remove_command(name)
        if command is None:
            # Even if it's a root alias, there's no way to get the Bot instance to remove the alias.
            return

        self._remove_root_aliases(command)
        return command

    def clear(self) -> None:
        """Not implemented! Re-instantiate the bot instead of attempting to re-use a closed one."""
        raise NotImplementedError("Re-using a Bot object after closing it is not supported.")

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        """Clear the internal guild available event when self.guild_id becomes unavailable."""
        if guild.id != self.guild_id:
            return

        self._guild_available.clear()

    async def on_guild_available(self, guild: discord.Guild) -> None:
        """
        Set the internal guild available event when self.guild_id becomes available.

        If the cache appears to still be empty (no members, no channels, or no roles), the event
        will not be set and `guild_available_but_cache_empty` event will be emitted.
        """
        if guild.id != self.guild_id:
            return

        if not guild.roles or not guild.members or not guild.channels:
            msg = "Guild available event was dispatched but the cache appears to still be empty!"
            self.dispatch("guild_available_but_cache_empty", msg)
            return

        self._guild_available.set()

    async def wait_until_guild_available(self) -> None:
        """
        Wait until the constants.Guild.id guild is available (and the cache is ready).

        The on_ready event is inadequate because it only waits 2 seconds for a GUILD_CREATE
        gateway event before giving up and thus not populating the cache for unavailable guilds.
        """
        await self._guild_available.wait()

    async def setup_hook(self) -> None:
        """Async init generic services."""
        loop = asyncio.get_running_loop()
        self._connect_statsd(self.statsd_url, loop)
        self.stats = AsyncStatsClient(loop, "127.0.0.1")

        try:
            await self.ping_services()
        except Exception as e:
            raise StartupError(e)

    async def ping_services() -> None:
        """
        Ping all services to ensure they are up.

        It's expected sub-classes will overwrite this function to ping their own services.
        """
        pass
