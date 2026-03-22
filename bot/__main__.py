from __future__ import annotations

from bot.bot import BebopBot
from bot.config import settings
from bot.log import setup_logging


def main() -> None:
    setup_logging(settings.log_level)
    bot = BebopBot()
    bot.run(settings.bot_token, log_handler=None)


main()
