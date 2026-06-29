import asyncio

from nxs.db.listener import start_listener

if __name__ == "__main__":
    asyncio.run(start_listener())
