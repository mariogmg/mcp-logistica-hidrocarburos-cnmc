"""Lista las herramientas de un servidor MCP por HTTP. Uso: listar_herramientas.py URL."""

import asyncio
import sys

from mcp import Client


async def main(url: str) -> None:
    async with Client(url) as cliente:
        herramientas = (await cliente.list_tools()).tools
        print(f"{len(herramientas)} herramientas: " + ", ".join(h.name for h in herramientas))


asyncio.run(main(sys.argv[1]))
