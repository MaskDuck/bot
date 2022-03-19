import asyncio
import socket

import aiohttp

import bot
from bot import api, constants
from bot.async_stats import AsyncStatsClient
from bot.bot import Bot, StartupError
from bot.log import get_logger, setup_sentry

setup_sentry()
LOCALHOST = "127.0.0.1"


async def main(bot: Bot) -> None:
    """Entry Async method for starting the bot."""
    statsd_url = constants.Stats.statsd_host
    if constants.DEBUG_MODE:
        # Since statsd is UDP, there are no errors for sending to a down port.
        # For this reason, setting the statsd host to 127.0.0.1 for development
        # will effectively disable stats.
        statsd_url = LOCALHOST
    bot._resolver = aiohttp.AsyncResolver()
    bot._connector = aiohttp.TCPConnector(
        resolver=bot._resolver,
        family=socket.AF_INET,
    )
    bot.http.connector = bot._connector
    async with aiohttp.ClientSession(connector=bot._connector) as session:
        async with bot:
            bot.http_session = session

            bot._connect_statsd(statsd_url)
            bot.stats = AsyncStatsClient(asyncio.get_running_loop(), LOCALHOST)

            bot.api_client = api.APIClient(connector=bot._connector)
            try:
                await bot.ping_services()
            except Exception as e:
                raise StartupError(e)
            bot._guild_available = asyncio.Event()
            await bot.start(constants.Bot.token)


try:
    bot.instance = Bot.create()
    asyncio.run(main(bot.instance))
except StartupError as e:
    message = "Unknown Startup Error Occurred."
    if isinstance(e.exception, (aiohttp.ClientConnectorError, aiohttp.ServerDisconnectedError)):
        message = "Could not connect to site API. Is it running?"
    elif isinstance(e.exception, OSError):
        message = "Could not connect to Redis. Is it running?"

    # The exception is logged with an empty message so the actual message is visible at the bottom
    log = get_logger("bot")
    log.fatal("", exc_info=e.exception)
    log.fatal(message)

    exit(69)
