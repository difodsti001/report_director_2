"""
run.py

Punto de arranque para Windows. Reemplaza al comando `uvicorn app:app`.

uvicorn.run() internamente llama a asyncio.run(), que en Windows crea su
propio ProactorEventLoop sin importar la policy que hayamos fijado antes --
por eso set_event_loop_policy() solo, o incluso uvicorn.run() con la policy
ya fijada, no bastan.

La forma que sí funciona: no usar uvicorn.run() en absoluto. En vez de eso,
se construye un asyncio.Runner (o loop) con SelectorEventLoop explícito, y
dentro de ESE loop se corre uvicorn.Server().serve() -- así Uvicorn nunca
llega a decidir su propio loop.

Uso:
    python run.py
"""

import sys
import asyncio

import uvicorn


async def main():
    config = uvicorn.Config("app:app", host="127.0.0.1", port=8000, reload=False)
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Loop explícito con SelectorEventLoop, corrido con Runner para
        # que ni asyncio.run() ni uvicorn tengan oportunidad de crear un
        # ProactorEventLoop por defecto.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(main())
    else:
        asyncio.run(main())