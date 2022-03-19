import asyncio

import aiohttp
import discord

import bot
from bot import constants
from bot.bot import Bot
from bot.log import get_logger, setup_sentry
from botcore import StartupError

setup_sentry()
LOCALHOST = "127.0.0.1"


async def main() -> None:
    """Entry Async method for starting the bot."""
    allowed_roles = list({discord.Object(id_) for id_ in constants.MODERATION_ROLES})

    intents = discord.Intents.all()
    intents.presences = False
    intents.dm_typing = False
    intents.dm_reactions = False
    intents.invites = False
    intents.webhooks = False
    intents.integrations = False

    bot.instance = await Bot.create(allowed_roles, intents)

    async with aiohttp.ClientSession(connector=bot.instance._connector) as session:
        async with bot.instance:
            bot.instance.http_session = session

            statsd_url = constants.Stats.statsd_host
            if constants.DEBUG_MODE:
                # Since statsd is UDP, there are no errors for sending to a down port.
                # For this reason, setting the statsd host to 127.0.0.1 for development
                # will effectively disable stats.
                statsd_url = LOCALHOST
            bot.instance.statsd_url = statsd_url

            await bot.instance.start(constants.Bot.token)

try:
    asyncio.run(main())
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
