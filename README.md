# pc-agent-pocket — Telegram bridge for OpenCode

A single-file Python bridge that turns Telegram into a pocket remote control
for an AI coding agent on your PC. It forwards Telegram messages into a pinned
OpenCode session and relays answers, permission requests and progress back.
No third-party Python dependencies (stdlib only).

Because it talks to OpenCode, the agent can run on any model provider OpenCode
supports — Kimi (Moonshot), Claude, GPT, GLM, Gemini and others. Switch with
`/model` in Telegram.

## Contents

| File              | Purpose                                            |
|-------------------|----------------------------------------------------|
| `bridge.py`       | The bridge program (the only script it consists of)|
| `setup.sh`        | Linux installer: checks, install, token, service, uninstall |
| `run.sh`          | Foreground launcher for manual/debug runs          |
| `make_installer.sh` | Builds distributable packages into `dist/`       |
| `dist/…-linux.tar.gz` | Archive package: unpack, run `setup.sh`        |
| `dist/…-linux.run`    | Single-file self-extracting Linux installer    |

Runtime data is **not** part of this folder and is never copied here:
the bot token, session state and log live in `~/.config/tg-opencode/`
(`token/token`, `state.json`, `bridge.log`). The token is a secret — keep it
out of shared folders and repositories.

## Requirements

- Linux with Python **3.8+** (walrus operator is used)
- OpenCode installed at `~/.opencode/bin/opencode`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- systemd (user session) for the service mode — optional, `run.sh` works without it

## Install

From this folder:

```bash
cd pc-agent-pocket
./setup.sh            # interactive menu (install / check / status / uninstall)
./setup.sh --check    # only verify prerequisites, change nothing
./setup.sh -y         # non-interactive
```

From a distribution package (copy one file to the target Linux machine):

```bash
./pc-agent-pocket-1.0.0-linux.run            # self-extracting installer
./pc-agent-pocket-1.0.0-linux.run --check    # verify only
./pc-agent-pocket-1.0.0-linux.run --extract-only [dir]   # just unpack
# or the archive variant:
tar xzf pc-agent-pocket-1.0.0-linux.tar.gz
cd pc-agent-pocket && ./setup.sh
```

Rebuild the packages after changing any source file:

```bash
./make_installer.sh     # produces dist/*.tar.gz and dist/*.run
```

## Uninstall

```bash
./setup.sh --uninstall          # stop service, remove unit and installed script
./setup.sh --uninstall --purge  # also delete token, state and log (asks first)
```

The installer:

1. verifies `python3` and the `opencode` binary;
2. installs `bridge.py` to `~/.config/tg-opencode/bridge.py`
   (previous copy is backed up as `bridge.py.bak` if it differs);
3. keeps an existing token, or asks for one and validates it via Telegram
   `getMe` (`--skip-token-check` to bypass);
4. writes `~/.config/systemd/user/tg-opencode.service` and enables/starts it
   (asks before restarting an already running instance);
5. prints the pairing code and the bot @name to send `/pair <code>` (see below).

## First start / pairing

The bot is an interactive shell for this machine, so it serves exactly one
chat, bound once. While no chat is bound, the bridge writes a one-time
six-digit code to its log on start:

```
pairing required, code: 470699
```

`NNNNNN` is only a placeholder for that number. After installation `setup.sh`
prints the real code ready to copy; to read it again:

```bash
journalctl --user -u tg-opencode.service -n 100 | grep 'pairing required'
grep 'pairing required' ~/.config/tg-opencode/bridge.log   # last line wins
```

Then open the bot in Telegram (its @name is printed by the installer and in
the `bridge started as @...` log line) and send `/pair <code>` from the chat
you want to bind. All other chats are ignored. The code survives restarts; it
is regenerated only when `state.json` is deleted (`--uninstall --purge`).

## Operate

```bash
systemctl --user status tg-opencode.service
systemctl --user restart tg-opencode.service
journalctl --user -u tg-opencode.service -f      # live log
./run.sh                                          # foreground (stop the service first)
```

Telegram commands: `/status`, `/context`, `/sessions`, `/model`, `/agent`,
`/history`, `/form`, `/compact`, `/folder`, `/browse`, `/menu`, `/help`.
Tool permissions are never auto-approved — they arrive as inline buttons and
must be pressed in Telegram.

## Notes

- The default workdir is your home directory (`WORKDIR` in `bridge.py`,
  `os.path.expanduser("~")`); change it in Telegram with `/folder` at runtime
  (persisted in `state.json`).
- Internal runtime names are deliberately kept as `tg-opencode`: the service
  `tg-opencode.service` and the data directory `~/.config/tg-opencode/`.
  Renaming them would orphan an existing installation.
- Only one instance may poll the bot at a time; `run.sh` refuses to start
  while the service is active.
- The service wraps the bridge in `systemd-inhibit` so the machine does not
  sleep while the bridge runs.

## License

MIT — see [LICENSE](LICENSE).
