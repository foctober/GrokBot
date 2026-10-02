# Grok IRC Bot

Async IRC bot for EFnet that answers channel and private queries through the xAI API.

**Author:** foctober
**Version:** 4.0.0

It connects with TLS, answers `GrokBot:` and `!grok`, keeps a short channel memory, rate-limits so EFnet does not flood-kick it, and reconnects on its own.

## What it does

- Joins a channel and answers `GrokBot: ...`, `GrokBot, ...`, and `!grok ...`
- Also answers private messages
- Replies to EFnet proxy-monitor CTCP VERSION / PING (the drones on connect)
- Falls back to an alt nick if `GrokBot` is taken (EFnet nick length is 9)
- Rejoins after a kick
- Chunks replies under the IRC line limit and caps how many lines go out
- Strips markdown so answers stay readable in a client
- Caches near-duplicate questions
- Serializes writes and paces them (`FLOOD_DELAY`, default 0.7s)

## Install

Python 3.10 or newer. An xAI API key from [console.x.ai](https://console.x.ai/).

```bash
git clone https://github.com/foctober/grok-irc-bot.git
cd grok-irc-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `XAI_API_KEY`. Channel, nick, and server are already set for `#crackheads` on `irc.efnet.org:6697`.

```bash
python grok_irc_bot.py
```

Stop with Ctrl-C. Logs go to stdout and `grok_irc_bot.log`. The API key is never written to the log.

### screen / tmux

```bash
screen -S grokbot
source .venv/bin/activate
python grok_irc_bot.py
# detach with Ctrl-A D
```

### systemd (optional)

`/etc/systemd/system/grok-irc-bot.service`:

```ini
[Unit]
Description=Grok IRC Bot
After=network-online.target

[Service]
Type=simple
User=foctober
WorkingDirectory=/home/foctober/grok-irc-bot
ExecStart=/home/foctober/grok-irc-bot/.venv/bin/python grok_irc_bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now grok-irc-bot
```

## Talking to it

```
GrokBot: what's a good Metallica track, with a working youtube link
!grok name a few restaurants in Port Dover Ontario
/msg GrokBot how do I set a channel topic
```

Empty `GrokBot:` gets a nudge to ask something. Similar questions within the cache window reuse the last answer.

## Config

| Variable | Default | What it does |
| --- | --- | --- |
| `XAI_API_KEY` |  | Required |
| `GROK_MODEL` | `grok-4.7` | Model id. `grok-3` still works |
| `GROK_MAX_TOKENS` | `500` | Cap so replies stay short |
| `IRC_SERVER` | `irc.efnet.org` | Round-robin host |
| `IRC_PORT` | `6697` | TLS port. Do not point TLS at 6667 |
| `IRC_USE_TLS` | `1` | Set `0` only for a plain port |
| `IRC_NICK` | `GrokBot` | 9 characters max on EFnet |
| `IRC_CHANNEL` | `#crackheads` | Joined after the welcome numeric |
| `FLOOD_DELAY` | `0.7` | Seconds between outgoing lines |
| `MAX_REPLY_LINES` | `6` | Hard cap so a long answer cannot flood |
| `HISTORY_TURNS` | `4` | Recent Q/A pairs sent back as context |
| `LOG_LEVEL` | `INFO` | `DEBUG` to see raw server lines |

TLS certificate checks are off on purpose. EFnet's round-robin serves certificates that do not match `irc.efnet.org`, and verifying them drops the connection.

## Troubleshooting

- **Disconnects / reset by peer.** Normal on EFnet. The bot backs off and reconnects. Check the log for `Connection error`.
- **`WRONG_VERSION_NUMBER`.** TLS is on but the port is plain (6667), or the reverse. Use 6697 with `IRC_USE_TLS=1`.
- **No replies.** Confirm the key, and that the line starts with the nick or `!grok`. API 401 means the key is bad. 429 means back off; the bot already retries.
- **Flood kick.** Raise `FLOOD_DELAY` or lower `MAX_REPLY_LINES`.
- **Nick in use.** It tries `IRC_ALT_NICK` once, then reconnects.

## Layout

```
grok_irc_bot.py    bot
requirements.txt   aiohttp, python-dotenv
.env.example       config template
LICENSE            MIT
```

## License

MIT. Copyright foctober.
