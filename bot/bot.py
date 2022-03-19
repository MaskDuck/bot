import asyncio
import socket
from contextlib import suppress

import aiohttp
import discord
from async_rediscache import RedisSession
from discord.ext import commands
from sentry_sdk import push_scope

from bot import constants
from bot.log import get_logger
from botcore import BotBase, StartupError
from botcore.site_api import APIClient

log = get_logger('bot')


class Bot(BotBase):
    """A subclass of `botcore.BotBase` with Python-bot specific implementations."""

    async def cache_filter_list_data(self) -> None:
        """Cache all the data in the FilterList on the site."""
        full_cache = await self.api_client.get('bot/filter-lists')

        for item in full_cache:
            self.insert_item_into_filter_list_cache(item)

    async def ping_services(self) -> None:
        """A helper to make sure all the services the bot relies on are available on startup."""
        # Connect Site/API
        attempts = 0
        while True:
            try:
                log.info(f"Attempting site connection: {attempts + 1}/{constants.URLs.connect_max_retries}")
                await self.api_client.get("healthcheck")
                break

            except (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError):
                attempts += 1
                if attempts == constants.URLs.connect_max_retries:
                    raise
                await asyncio.sleep(constants.URLs.connect_cooldown)

    @classmethod
    async def create(cls, allowed_roles: list, intents: discord.Intents) -> "Bot":
        """Create and return an instance of a Bot."""
        instance = cls(
            guild_id=constants.Guild.id,
            redis_session=await _create_redis_session(),
            command_prefix=commands.when_mentioned_or(constants.Bot.prefix),
            activity=discord.Game(name=f"Commands: {constants.Bot.prefix}help"),
            case_insensitive=True,
            max_messages=10_000,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=allowed_roles),
            intents=intents,
        )
        instance._resolver = aiohttp.AsyncResolver()
        instance._connector = aiohttp.TCPConnector(
            resolver=instance._resolver,
            family=socket.AF_INET,
        )
        instance.http.connector = instance._connector
        instance._guild_available = asyncio.Event()
        return instance

    async def close(self) -> None:
        """Close the Discord connection and the aiohttp session, connector, statsd client, and resolver."""
        # Done before super().close() to allow tasks finish before the HTTP session closes.
        for ext in list(self.extensions):
            with suppress(Exception):
                await self.unload_extension(ext)

        for cog in list(self.cogs):
            with suppress(Exception):
                await self.remove_cog(cog)

        # Wait until all tasks that have to be completed before bot is closing is done
        log.trace("Waiting for tasks before closing.")
        await asyncio.gather(*self.closing_tasks)

        # Now actually do full close of bot
        await super().close()

        if self.api_client:
            await self.api_client.close()

        if self.http_session:
            await self.http_session.close()

        if self._connector:
            await self._connector.close()

        if self._resolver:
            await self._resolver.close()

        if self.stats._transport:
            self.stats._transport.close()

        if self.redis_session:
            await self.redis_session.close()

        if self._statsd_timerhandle:
            self._statsd_timerhandle.cancel()

    def insert_item_into_filter_list_cache(self, item: dict[str, str]) -> None:
        """Add an item to the bots filter_list_cache."""
        type_ = item["type"]
        allowed = item["allowed"]
        content = item["content"]

        self.filter_list_cache[f"{type_}.{allowed}"][content] = {
            "id": item["id"],
            "comment": item["comment"],
            "created_at": item["created_at"],
            "updated_at": item["updated_at"],
        }

    async def setup_hook(self) -> None:
        """Default Async initialisation method for Discord.py."""
        self.api_client = APIClient(
            constants.URLs.site_api_schema+constants.URLs.site_api,
            constants.Keys.site_api,
            connector=self._connector
        )
        await super().setup_hook()
        if self.redis_session.closed:
            # If the RedisSession was somehow closed, we try to reconnect it
            # here. Normally, this shouldn't happen.
            await self.redis_session.connect()

        # Build the FilterList cache
        await self.cache_filter_list_data()

        await self.stats.create_socket()

        # Must be done here to avoid a circular import.
        from bot.utils.extensions import EXTENSIONS
        extensions = set(EXTENSIONS)  # Create a mutable copy.
        if not constants.HelpChannels.enable:
            extensions.remove("bot.exts.help_channels")
        await self.load_extensions(extensions)

    async def on_guild_available_but_cache_empty(self, message: str) -> None:
        """Log a message when a guild is available but the cache is empty and send error."""
        log.warning(message)

        try:
            webhook = await self.fetch_webhook(constants.Webhooks.dev_log)
        except discord.HTTPException as e:
            log.error(f"Failed to fetch webhook to send empty cache warning: status {e.status}")
        else:
            await webhook.send(f"<@&{constants.Roles.admins}> {message}")

    async def on_error(self, event: str, *args, **kwargs) -> None:
        """Log errors raised in event listeners rather than printing them to stderr."""
        self.stats.incr(f"errors.event.{event}")

        with push_scope() as scope:
            scope.set_tag("event", event)
            scope.set_extra("args", args)
            scope.set_extra("kwargs", kwargs)

            log.exception(f"Unhandled exception in {event}.")


async def _create_redis_session() -> RedisSession:
    """
    Create and connect to a redis session.

    Ensure the connection is established before returning to prevent race conditions.
    `loop` is the event loop on which to connect. The Bot should use this same event loop.
    """
    redis_session = RedisSession(
        address=(constants.Redis.host, constants.Redis.port),
        password=constants.Redis.password,
        minsize=1,
        maxsize=20,
        use_fakeredis=constants.Redis.use_fakeredis,
        global_namespace="bot",
    )
    try:
        await redis_session.connect()
    except OSError as e:
        raise StartupError(e)
    return redis_session
