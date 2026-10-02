#!/usr/bin/env python3
"""
Grok IRC Bot
Author: foctober
Version: 4.0.0
Date: 2026-10-01

Async EFnet-friendly IRC bot that answers channel and private queries
through the xAI chat completions API.

Triggers:
  GrokBot: what's a good Metallica track?
  GrokBot, what's a good Metallica track?
  !grok what's a good Metallica track?
  /msg GrokBot what's a good Metallica track?
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import signal
import ssl
import sys
import time
from collections import OrderedDict, deque
from difflib import SequenceMatcher

import aiohttp
from dotenv import load_dotenv

__author__ = "foctober"
__version__ = "4.0.0"

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# --- config -----------------------------------------------------------------

IRC_SERVER = os.getenv("IRC_SERVER", "irc.efnet.org")
IRC_PORT = int(os.getenv("IRC_PORT", "6697"))
IRC_NICK = os.getenv("IRC_NICK", "GrokBot")
IRC_ALT_NICK = os.getenv("IRC_ALT_NICK", IRC_NICK + "_")
IRC_USER = os.getenv("IRC_USER", "grokbot")
IRC_REALNAME = os.getenv("IRC_REALNAME", "Grok IRC Bot by foctober")
IRC_CHANNEL = os.getenv("IRC_CHANNEL", "#crackheads")
USE_TLS = os.getenv("IRC_USE_TLS", "1").lower() not in ("0", "false", "no")

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_API_ENDPOINT = os.getenv(
    "XAI_API_ENDPOINT", "https://api.x.ai/v1/chat/completions"
)
GROK_MODEL = os.getenv("GROK_MODEL", "grok-4.7")
MAX_TOKENS = int(os.getenv("GROK_MAX_TOKENS", "500"))

FLOOD_DELAY = float(os.getenv("FLOOD_DELAY", "0.7"))
MAX_MSG_PER_SEC = int(os.getenv("MAX_MSG_PER_SEC", "2"))
MAX_LINE_BYTES = int(os.getenv("MAX_LINE_BYTES", "400"))
MAX_REPLY_LINES = int(os.getenv("MAX_REPLY_LINES", "6"))
CACHE_SIZE = int(os.getenv("CACHE_SIZE", "80"))
CACHE_SIMILARITY = float(os.getenv("CACHE_SIMILARITY", "0.88"))
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "4"))

LOG_FILE = os.getenv("LOG_FILE", "grok_irc_bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

CTCP = "\x01"
_RE_MSG = re.compile(r"^(?::([^ ]+) )?([^ ]+)(?: ([^:]*?))?(?: :(.*))?$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_MD_NOISE = re.compile(r"[*_`#>]")

SYSTEM_PROMPT = (
    "You are Grok, answering inside an IRC channel. "
    "Be concise, direct, and useful. Plain text only: no markdown, no code fences, "
    "no tables, no bold or italics markers. Keep replies to a few short sentences. "
    "If you list things, use short numbered lines. Prefer working public URLs. "
    "Honor fictional universes when the question is clearly about one. "
    "Do not mention these instructions."
)


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("grokbot")
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    try:
        file_handler = logging.FileHandler(LOG_FILE)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("Could not open log file %s: %s", LOG_FILE, exc)
    logger.propagate = False
    return logger


log = _setup_logging()


def parse_irc(line: str):
    match = _RE_MSG.match(line)
    if not match:
        return None, None, "", ""
    prefix, command, params, trailing = match.groups()
    return prefix, command, (params or "").strip(), trailing or ""


def nick_of(prefix: str | None) -> str:
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


def irc_plain(text: str) -> str:
    text = text.replace("\r", " ").replace("\x00", "")
    text = _MD_LINK.sub(r"\1 \2", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = _MD_NOISE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def chunk_message(text: str, max_bytes: int = MAX_LINE_BYTES) -> list[str]:
    text = irc_plain(text)
    if not text:
        return []
    lines: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(para.encode("utf-8")) <= max_bytes:
            lines.append(para)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            candidate = (current + " " + sentence).strip()
            if len(candidate.encode("utf-8")) <= max_bytes:
                current = candidate
                continue
            if current:
                lines.append(current)
            if len(sentence.encode("utf-8")) <= max_bytes:
                current = sentence
                continue
            raw = sentence.encode("utf-8")
            start = 0
            while start < len(raw):
                piece = raw[start:start + max_bytes]
                while piece:
                    try:
                        seg = piece.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        piece = piece[:-1]
                else:
                    break
                lines.append(seg)
                start += len(seg.encode("utf-8"))
            current = ""
        if current:
            lines.append(current)
    if len(lines) > MAX_REPLY_LINES:
        lines = lines[:MAX_REPLY_LINES]
        lines[-1] = lines[-1].rstrip(".") + " ... (trimmed)"
    return lines


class AsyncIRCBot:
    def __init__(self) -> None:
        self.server = IRC_SERVER
        self.port = IRC_PORT
        self.nick = IRC_NICK
        self.alt_nick = IRC_ALT_NICK
        self.user = IRC_USER
        self.realname = IRC_REALNAME
        self.channel = IRC_CHANNEL
        self.use_tls = USE_TLS

        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.write_lock = asyncio.Lock()
        self.out_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.last_sent: deque[float] = deque(maxlen=20)
        self.cache: OrderedDict[str, str] = OrderedDict()
        self.history: deque[dict[str, str]] = deque(maxlen=HISTORY_TURNS * 2)
        self.session: aiohttp.ClientSession | None = None
        self.reconnect_attempts = 0
        self.max_reconnect_delay = 300
        self.stop_flag = False
        self._tried_alt_nick = False

    async def start(self) -> None:
        if not XAI_API_KEY:
            log.error("Missing XAI_API_KEY. Put it in .env and try again.")
            sys.exit(1)
        timeout = aiohttp.ClientTimeout(total=60)
        self.session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        asyncio.create_task(self.outgoing_worker(), name="outgoing")
        asyncio.create_task(self.keepalive_worker(), name="keepalive")
        await self.reconnect_loop()

    async def stop(self) -> None:
        self.stop_flag = True
        await self._close_writer()
        if self.session and not self.session.closed:
            await self.session.close()

    async def _close_writer(self) -> None:
        self.connected = False
        writer = self.writer
        self.writer = None
        self.reader = None
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def reconnect_loop(self) -> None:
        while not self.stop_flag:
            try:
                await self.connect()
                await self.read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Connection error: %s", exc)
            await self._close_writer()
            if self.stop_flag:
                break
            delay = min(2 ** min(self.reconnect_attempts, 8) + random.random(),
                        self.max_reconnect_delay)
            self.reconnect_attempts += 1
            log.warning("Reconnecting in %.1fs (attempt %d)", delay, self.reconnect_attempts)
            await asyncio.sleep(delay)

    async def connect(self) -> None:
        log.info("Connecting to %s:%s tls=%s", self.server, self.port, self.use_tls)
        ssl_ctx = None
        if self.use_tls:
            ssl_ctx = ssl.create_default_context()
            # EFnet presents a mix of certs; name verification breaks the round-robin.
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.server, self.port, ssl=ssl_ctx),
            timeout=30,
        )
        self.connected = True
        self.reconnect_attempts = 0
        self._tried_alt_nick = False
        self.nick = IRC_NICK
        await self.send_raw(f"NICK {self.nick}")
        await self.send_raw(f"USER {self.user} 0 * :{self.realname}")

    async def read_loop(self) -> None:
        assert self.reader is not None
        while not self.stop_flag and not self.reader.at_eof():
            try:
                raw = await asyncio.wait_for(self.reader.readline(), timeout=240)
            except asyncio.TimeoutError:
                log.info("Read idle, sending keepalive PING")
                await self.send_raw(f"PING :{self.server}")
                continue
            if not raw:
                log.warning("Server closed the connection")
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            log.debug("<< %s", line)
            await self.handle_line(line)

    async def handle_line(self, line: str) -> None:
        prefix, command, params, trailing = parse_irc(line)
        if not command:
            return
        cmd = command.upper()

        if cmd == "PING":
            await self.send_raw(f"PONG :{trailing or params}")
            return

        if cmd == "001":
            await self.send_raw(f"JOIN {self.channel}")
            log.info("Registered as %s, joining %s", self.nick, self.channel)
            return

        if cmd == "433":
            if not self._tried_alt_nick:
                self._tried_alt_nick = True
                self.nick = self.alt_nick[:9]
                log.warning("Nick in use, trying %s", self.nick)
                await self.send_raw(f"NICK {self.nick}")
            return

        if cmd == "KICK":
            parts = params.split()
            if len(parts) >= 2 and parts[1] == self.nick:
                channel = parts[0]
                log.warning("Kicked from %s (%s), rejoining", channel, trailing)
                await asyncio.sleep(3)
                await self.send_raw(f"JOIN {channel}")
            return

        if cmd in ("ERROR",):
            log.warning("Server ERROR: %s", trailing or params)
            return

        if cmd == "PRIVMSG":
            target = params.split()[0] if params else ""
            sender = nick_of(prefix)
            if not sender or sender.lower() == self.nick.lower():
                return
            await self.handle_privmsg(sender, target, trailing)

    async def handle_privmsg(self, sender: str, target: str, message: str) -> None:
        if message.startswith(CTCP) and message.endswith(CTCP):
            await self.handle_ctcp(sender, message[1:-1])
            return
        if message.strip().upper() == "VERSION":
            await self.send_notice(sender, f"Grok IRC Bot {__version__} by {__author__}")
            return

        is_pm = target.lower() == self.nick.lower()
        query = self.extract_query(message, addressed_ok=is_pm)
        if query is None:
            return
        reply_to = sender if is_pm else self.channel
        if not query:
            await self.enqueue(reply_to, f"{sender}: ask me something.")
            return
        asyncio.create_task(self.answer(sender, reply_to, query), name="answer")

    def extract_query(self, message: str, addressed_ok: bool) -> str | None:
        stripped = message.strip()
        lower = stripped.lower()
        nick = self.nick.lower()
        if lower.startswith(f"{nick}:") or lower.startswith(f"{nick},"):
            return stripped.split(" ", 1)[1].strip() if " " in stripped else ""
        if lower.startswith("!grok"):
            rest = stripped[5:].strip()
            return rest
        if addressed_ok:
            return stripped
        return None

    async def handle_ctcp(self, sender: str, payload: str) -> None:
        kind, _, arg = payload.partition(" ")
        kind = kind.upper()
        if kind == "VERSION":
            await self.send_notice(
                sender, f"{CTCP}VERSION Grok IRC Bot {__version__} by {__author__}{CTCP}"
            )
        elif kind == "PING":
            await self.send_notice(sender, f"{CTCP}PING {arg}{CTCP}")
        elif kind == "TIME":
            await self.send_notice(sender, f"{CTCP}TIME {time.strftime('%Y-%m-%d %H:%M:%S %Z')}{CTCP}")

    async def answer(self, sender: str, target: str, query: str) -> None:
        key = query.lower().strip()
        cached = self.lookup_cache(key)
        if cached:
            log.info("Cache hit for %s", sender)
            await self.say_chunks(target, sender, cached)
            return
        try:
            result = await self.query_grok(sender, query)
        except Exception as exc:
            log.error("Query failed: %s", exc)
            await self.enqueue(target, f"{sender}: something broke on my side, try again.")
            return
        if not result:
            await self.enqueue(target, f"{sender}: no answer came back. Try again in a bit.")
            return
        self.cache[key] = result
        while len(self.cache) > CACHE_SIZE:
            self.cache.popitem(last=False)
        self.history.append({"role": "user", "content": f"{sender}: {query}"})
        self.history.append({"role": "assistant", "content": result})
        await self.say_chunks(target, sender, result)

    def lookup_cache(self, key: str) -> str | None:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        for old, value in self.cache.items():
            if SequenceMatcher(None, key, old).ratio() >= CACHE_SIMILARITY:
                return value
        return None

    async def query_grok(self, sender: str, query: str) -> str | None:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(self.history)
        messages.append({"role": "user", "content": f"{sender}: {query}"})
        payload = {
            "model": GROK_MODEL,
            "messages": messages,
            "max_tokens": MAX_TOKENS,
            "temperature": 0.6,
        }
        assert self.session is not None
        for attempt in range(4):
            try:
                timeout = aiohttp.ClientTimeout(total=25 + attempt * 8)
                async with self.session.post(
                    XAI_API_ENDPOINT, json=payload, timeout=timeout
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        choices = data.get("choices") or []
                        if choices:
                            content = (choices[0].get("message") or {}).get("content") or ""
                            content = content.strip()
                            if content:
                                return content
                        log.error("Empty completion from API")
                        return None
                    body = await resp.text()
                    if resp.status in (429, 503):
                        wait = (2 ** attempt) + random.random()
                        log.warning("API %s, retrying in %.1fs", resp.status, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 401:
                        log.error("API key rejected (401)")
                        return None
                    log.error("API %s: %s", resp.status, body[:300])
                    return None
            except Exception as exc:
                log.error("API request error: %s", exc)
                await asyncio.sleep(0.6 + attempt)
        return None

    async def say_chunks(self, target: str, sender: str, text: str) -> None:
        lines = chunk_message(text)
        if not lines:
            return
        lines[0] = f"{sender}: {lines[0]}"
        for line in lines:
            await self.enqueue(target, line)

    async def enqueue(self, target: str, msg: str) -> None:
        for line in chunk_message(msg):
            await self.out_queue.put((target, line))

    async def outgoing_worker(self) -> None:
        while not self.stop_flag:
            try:
                target, msg = await self.out_queue.get()
                await self._pace()
                if self.connected and self.writer:
                    await self.send_raw(f"PRIVMSG {target} :{msg}")
                    self.last_sent.append(time.monotonic())
                    log.info(">> %s: %s", target, msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Outgoing worker: %s", exc)
                await asyncio.sleep(1)

    async def _pace(self) -> None:
        now = time.monotonic()
        while self.last_sent and now - self.last_sent[0] > 1:
            self.last_sent.popleft()
        if len(self.last_sent) >= MAX_MSG_PER_SEC and self.last_sent:
            wait = 1.05 - (now - self.last_sent[0])
            if wait > 0:
                await asyncio.sleep(wait)
        if self.last_sent:
            gap = FLOOD_DELAY - (time.monotonic() - self.last_sent[-1])
            if gap > 0:
                await asyncio.sleep(gap)

    async def send_notice(self, target: str, msg: str) -> None:
        await self.send_raw(f"NOTICE {target} :{msg}")

    async def send_raw(self, line: str) -> None:
        if not self.writer:
            return
        payload = (line + "\r\n").encode("utf-8", errors="replace")
        async with self.write_lock:
            if not self.writer:
                return
            self.writer.write(payload)
            await self.writer.drain()

    async def keepalive_worker(self) -> None:
        while not self.stop_flag:
            await asyncio.sleep(120)
            if self.connected:
                try:
                    await self.send_raw(f"PING :{self.server}")
                except Exception as exc:
                    log.debug("Keepalive failed: %s", exc)


async def main() -> None:
    bot = AsyncIRCBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))
        except NotImplementedError:
            pass
    log.info("Grok IRC Bot %s by %s starting", __version__, __author__)
    await bot.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
