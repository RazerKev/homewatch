"""Entry point: python -m homewatch_agent"""

import asyncio
import logging
import signal
import sys

import aiohttp

from . import __version__
from .agent import Agent
from .config import ConfigError, load_config
from .logs import configure_logging

log = logging.getLogger("homewatch")


async def _run() -> None:
    cfg = load_config()
    configure_logging(cfg.log_level)
    log.info(
        "homewatch agent starting",
        extra={
            "version": __version__,
            "mode": cfg.mode,
            "cloud": cfg.api_url,
            "heartbeat_s": cfg.heartbeat_interval_s,
            "platforms": ",".join(sorted(cfg.platforms)),
        },
    )
    if cfg.api_url.startswith("http://"):
        log.warning("cloud URL is plain http: fine on a home network for Phase 1, use https anywhere else")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C arrives as KeyboardInterrupt instead

    async with aiohttp.ClientSession(trust_env=True) as session:
        agent = Agent(cfg, session)
        runner = asyncio.create_task(agent.run(), name="agent")
        stopper = asyncio.create_task(stop.wait(), name="stop")
        done, _ = await asyncio.wait({runner, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if runner in done:
            runner.result()  # a task crashed: raise so the process exits non-zero
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    log.info("homewatch agent stopped")


def main() -> None:
    try:
        asyncio.run(_run())
    except ConfigError as exc:
        # Before logging is configured; plain stderr so it shows in the app's Log tab.
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
