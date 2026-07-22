"""Entrypoint: ``python -m roger``."""

import asyncio

from roger.bot import main

if __name__ == "__main__":
    asyncio.run(main())
