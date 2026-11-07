# nyrasview

The control center for **AI-driven development**: one place to see, steer and coordinate every AI coding agent you run.

- **See:** what each agent is doing right now, down to the file and the part of the page it's editing; its CPU, RAM and network; MCP servers and skills; tokens, cost and your real plan limits.
- **Steer:** prompt any agent from one box, compare several side by side, leave notes on its work while it runs, and let it fix failing checks on its own.
- **Coordinate:** split a goal across agents with a planner that runs on your cheapest model, have several agents co-write one document, and hand work to another agent (or the same one on an API key) when a limit hits.

![nyrasview demo: overview, watching Claude edit a website and leaving it a note, then Claude and Codex answering the same prompt side by side](docs/media/demo.gif)

<sub>[Full-quality MP4](docs/media/demo.mp4) · the demo uses fake agents and data from <code>tools/demo.py</code></sub>

Works with Claude Code, Codex, Gemini CLI, opencode, Kimi CLI, Antigravity, Cursor, Qwen Code, Copilot CLI, Crush, Amp, Factory Droid, Goose, Aider, Windsurf, Ollama, LM Studio and OpenRouter. You can add more in a config file.

## A look around

| | |
|---|---|
| ![Overview](docs/media/overview.png) **Overview:** every agent, session, limit, alert and token at a glance | ![Watch](docs/media/watch.png) **Watch:** the file and the part of the page an agent is editing, with its cursor; hover to leave a note |
| ![Compare](docs/media/prompt-compare.png) **Prompt and compare:** ask several agents at once and watch them answer side by side | ![Self-heal](docs/media/self-heal.png) **Self-heal:** a check fails, an agent fixes it, the check passes |
| ![Hand off](docs/media/handoff.png) **Hand off:** pass the work to another agent, or keep the same session going on an API key | ![MCP manager](docs/media/mcp.png) **MCP manager:** every server against every agent, one click to add or remove |
| ![Collab](docs/media/collab.png) **Collab:** different AIs writing different sections of one document | ![Settings](docs/media/settings.png) **Settings:** everything in one place, plus the raw config |
| ![Usage](docs/media/usage.png) **Usage:** 14 days of tokens per agent and model | ![Terminal UI](docs/media/tui.png) **Terminal UI:** the same data in your terminal |

![On a phone](docs/media/mobile.png)

**On your phone:** scan a QR code and you have it in your pocket.

## Install

You need **Python 3.11 or newer**. The only dependencies are `psutil`, plus `windows-curses` on Windows, and they're installed automatically. These steps use [pipx](https://pipx.pypa.io), which puts `nyrasview` on your PATH in its own environment.

### Windows

In PowerShell:

```powershell
winget install Python.Python.3.13
py -m pip install --user pipx
py -m pipx ensurepath
```

Close and reopen the terminal, then:

```powershell
pipx install https://github.com/nyrasdev/nyrasview/archive/refs/heads/main.zip
nyrasview gui
```

Windows Terminal is recommended for the TUI (`nyrasview`).

### macOS

```sh
brew install pipx
pipx ensurepath
pipx install https://github.com/nyrasdev/nyrasview/archive/refs/heads/main.zip
nyrasview gui
```

### Linux

```sh
sudo pacman -S python-pipx      # Arch / CachyOS
sudo apt install pipx           # Debian / Ubuntu
sudo dnf install pipx           # Fedora
pipx ensurepath
pipx install https://github.com/nyrasdev/nyrasview/archive/refs/heads/main.zip
nyrasview gui
```

**With uv instead of pipx** (any OS): `uv tool install https://github.com/nyrasdev/nyrasview/archive/refs/heads/main.zip`

**Update:** run the same `pipx install` (or `uv tool install`) command again with `--force`.

**From a clone:** `git clone https://github.com/nyrasdev/nyrasview && cd nyrasview && python -m nyrasview`

### Where things live

| | Linux | Windows | macOS |
|---|---|---|---|
| config | `~/.config/nyrasview/config.toml` | `%APPDATA%\nyrasview\config.toml` | `~/.config/nyrasview/config.toml` |
| state (runs, alerts, backups) | `~/.local/state/nyrasview` | `%LOCALAPPDATA%\nyrasview` | `~/Library/Application Support/nyrasview` |
| desktop alerts | `notify-send` | Windows toast notifications | Notification Center |
| handoff opens | kitty, ghostty, wezterm, foot, alacritty, konsole, gnome-terminal or xterm | Windows Terminal, or a new console | Terminal or iTerm |
| `nyrasview service install` | systemd user service | Task Scheduler (runs at logon) | launchd agent |
| clipboard for notes | `wl-copy`, `xclip` or `xsel` | built in | built in |

[![smoke](https://github.com/nyrasdev/nyrasview/actions/workflows/ci.yml/badge.svg)](https://github.com/nyrasdev/nyrasview/actions/workflows/ci.yml)

Every push is tested on Linux, Windows and macOS with Python 3.11 and 3.13.

## Use

| command | what it does |
|---|---|
| `nyrasview` | terminal dashboard with tabs: agents, MCP, skills, usage, network, collab |
| `nyrasview gui` | the same dashboard in your browser (localhost only) |
| `nyrasview status` / `ps` / `mcp` / `skills` / `usage` | one-shot CLI views, most of which take `--json` |
| `nyrasview agents` | every known agent and how collab calls it |
| `nyrasview ask <agent> "…"` | prompt one agent, or `claude,codex` to compare, and stream the answer |
| `nyrasview runs` | prompts started from nyrasview |
| `nyrasview limits` | usage limit gauges and time left at your pace |
| `nyrasview inbox` | recent alerts (`notify-test` sends one) |
| `nyrasview mcp add/copy/rm/test` | manage MCP servers for every agent at once |
| `nyrasview daemon` / `service install` | alerts and relay without a UI, optionally on login |
| `nyrasview plan "<goal>"` | the cheapest model splits a goal into tasks for your agents |
| `nyrasview keys` | connect API keys to keep working past plan limits |
| `nyrasview skill add/new/copy/rm/list` | install and manage skills for every agent |
| `nyrasview heal ["check"]` | run a check and let an agent fix it until it passes (`--live` for Claude sessions) |
| `nyrasview gui --phone` | also open it on your phone (QR code) |
| `nyrasview now` | what every running agent is working on right now |
| `nyrasview local` | local AI servers and their models; `local ask ollama "hi"` to try one |
| `nyrasview handoff` | hand the current task to another agent (`--to codex`, `-w` new window, `-n` dry run) |
| `nyrasview relay` | watch for usage limits and hand off automatically (or add `--relay` to `nyrasview`/`gui`) |
| `nyrasview say "…"` | send a live note to the agent working in this folder |
| `nyrasview hooks install` | let live notes reach Claude Code mid-task |
| `nyrasview obsidian` | your Obsidian vaults |

### What it tracks per agent

- **Processes:** the whole process tree, labelled main / MCP / shell / child, with per-process kill
- **Resources:** CPU, RAM and threads, plus a CPU history sparkline
- **Network:** live connections classified by provider (anthropic, openai, google, openrouter, moonshot…) and listening ports
- **MCP:** configured servers from every agent's config, matched to the processes actually running
- **Skills:** skills, slash commands, subagents, rules, workflows, extensions and memory files
- **Usage:** tokens today / 7 days / all time per model, read from each tool's local logs, plus OpenRouter credit usage via `OPENROUTER_API_KEY`
- **Working on:** for each Claude Code, Codex, Gemini or Qwen session: the project, the session title, your last prompt and the tool it is running right now

## Prompt from inside nyrasview

Press **Ctrl+K** in the GUI (or click ✦ New prompt). Write the prompt, pick one or more agents, a folder and a mode:

| mode | what the agent may do |
|---|---|
| **Ask** | read and look around, no changes |
| **Edit** | create and change files in the folder |
| **Auto** | everything, including commands, without asking |

- **Transcript:** answers stream in live, with tool calls shown as chips (click one to see its output). Each run ends with time, tokens and cost.
- **Follow up:** continues the same conversation; Claude Code resumes its real session.
- **Compare:** pick several agents and they answer the same prompt side by side.
- **Terminal:** opens the agent interactively with your prompt. **Send to running session** delivers it into an agent that is already working in that folder.
- From the terminal: `nyrasview ask claude "why is the build slow?" -d ~/app`, `nyrasview ask claude,codex "…"` to compare, `-c <run>` to follow up, `nyrasview runs` for history. In the TUI, press `p` on an agent.

## Skills: add them once, give them to any agent

The **Skills** page (or `nyrasview skill`) installs skills from:

- a GitHub repo or folder, e.g. [anthropics/skills](https://github.com/anthropics/skills)
- a local folder
- a `SKILL.md` URL

It shows every skill it found so you can pick, and installs them into Claude Code, Codex, opencode, Gemini, Kimi, Copilot or the shared `~/.agents/skills`. You can also write a new skill from a name, a description and instructions. A grid shows which agent has which skill: click a cell to copy or remove one. Removals are backed up.

```sh
nyrasview skill add https://github.com/anthropics/skills --only pdf,mcp-builder --to claude,codex
echo "Write commit messages in the conventional style." | nyrasview skill new commit-style -d "Use for commit messages" --to all
nyrasview skill copy commit-style --from claude --to opencode
nyrasview skill list
```

## Settings

Everything above is editable on the **Settings** page:

- alerts, limits, API keys and fallback, relay order, planner, sandbox, collab, ports
- integrations: Claude hooks, autostart, browser alerts
- a raw `config.toml` editor for custom agents and `[[local]]` servers

## Sandbox: run agents in your own Docker image

Set an image in **Settings → Sandbox** (or `[sandbox] image = "you/agents:latest"`). Prompt runs, and optionally collab tasks, then run as `docker run --rm -i -v <project>:/work …`:

- each agent's login folder is mounted, so it's already signed in
- **Test** lists which agent CLIs your image contains
- network and extra `docker run` flags are configurable
- toggle it per prompt in the composer, or use `nyrasview ask claude "…" --sandbox`

## Watch: see exactly what an agent is touching

Click **👁 Watch** on any Claude Code or Codex session (or a row in the overview's *Right now* list):

- **Code:** the file it last edited, with the changed lines highlighted in the agent's color and its cursor on them. Click any older edit in the timeline to jump back.
- **Website:** if the project runs a dev server (Vite, Next, plain `http.server`…), a live preview appears. nyrasview works out which element the edit touched from its classes, ids, CSS selectors or text, and draws a box and the agent's cursor on it. Hot reload keeps working.
- **Talk to it:** hover the box (or Alt+click anything on the page, or click a line number in the code) and type something like *"soften the corners a bit more"*. The note carries the file, lines and element, and goes to the agent:
  - **Claude Code** gets it after its next tool call, through a hook. Turn it on once with `nyrasview hooks install`; a backup of `~/.claude/settings.json` is saved first.
  - **Other agents** get it copied to your clipboard, ready to paste.
- From the terminal: `nyrasview say "make the header sticky" -f src/Header.tsx`

## Alerts: know when you're needed

nyrasview tells you when:

- an agent **needs your permission** or is waiting for you
- it **finished**
- a prompt run or collab task **is done or failed**
- an agent **hit its limit** or is **about to**

Alerts arrive as desktop notifications, browser notifications (🔔 in the GUI), or on your phone through [ntfy](https://ntfy.sh). They all collect in the GUI inbox and in `nyrasview inbox`.

- With `nyrasview hooks install`, Claude Code reports permission prompts and finishes the moment they happen. Without the hook, nyrasview works it out from the session logs.
- To keep alerts running without the GUI open: `nyrasview service install --relay`. This sets up a systemd user service on Linux, a Task Scheduler logon task on Windows, or a launchd agent on macOS.

```toml
[notify]
desktop = true
ntfy = "my-secret-topic"   # phone alerts via the ntfy app
soon_pct = 90              # warn at 90% of a limit
```

## Limits: see the wall before you hit it

`nyrasview limits` (and the Limits panel in the GUI) shows your real subscription windows.

- **Claude Code:** read live from Anthropic's usage API with the login Claude Code already stores, the same numbers `/usage` shows. You get:
  - your plan (e.g. *Claude Pro*)
  - every window your plan is metered on: session (5h), weekly, and model-scoped weeks when your plan has them
  - when each resets
  - your pace against the clock (↑ over / ↓ under)
  - time left at the current rate

  The token never leaves `api.anthropic.com` and nyrasview never rewrites it. If the API can't be reached, nyrasview falls back to estimating from your local token logs.
- **Codex:** read straight from its own session logs.

An alert fires when a window gets close, and with relay on the handoff file is ready before you hit the wall.

## Keep working when the plan runs out

Connect an API key in the GUI (**API keys**) or with `nyrasview keys anthropic` (`openai`, `openrouter`). Then:

- **Relay** continues the *same* Claude Code or Codex session on API billing (`claude --continue`) the moment the plan limit hits, instead of switching tools.
- **Prompt runs** that fail on a limit retry on the key automatically.
- **opencode can run through OpenRouter** as one more fallback.

Keys live in your nyrasview config file, readable only by you, and are only ever sent to their own provider.

## Plan: let the cheapest model split the work

```sh
nyrasview plan "design a Next.js todo app with auth"          # writes ./todo-app-design.md with tasks
nyrasview plan "launch plan for v2" -o ~/notes/launch.md --run  # and starts the agents
```

nyrasview shows the **smallest model you have** and asks whether to use it or pick another (tick *remember* to keep the choice). Picking the smallest one keeps planning free and offline when you run Ollama, LM Studio, llama.cpp and the like. It falls back to a cheap OpenRouter model, then to the lightest model of an installed CLI (e.g. Claude Haiku). That model splits the goal into sections and gives each to the agent that fits it best, out of the ones you actually have installed. A final review pass makes the document consistent. You can edit the plan before running it.

In the GUI, use **Collab → New document → ✦ Plan it for me**. To pin the planner, set `[planner] agent = "ollama"` and `model = "qwen2.5:3b"` in your config.

## MCP manager

The MCP page shows every server against every agent in one grid. Click a cell to switch a server on or off for that agent.

- **Add:** paste a command, a URL or a JSON snippet, or pick a preset (playwright, context7, fetch, memory…).
- **Test:** starts the server and lists its tools before you commit to it.
- **Add to all:** copies a server, env vars included, into every agent's own format.
- **Formats written:** Claude (`claude mcp`), Codex TOML, opencode, Gemini, Qwen, Cursor, Kimi, Antigravity, Windsurf, Copilot, Droid, Crush, Amp.
- **Backups:** every file is backed up before it changes.

```sh
nyrasview mcp add "npx -y @playwright/mcp@latest" --to all
nyrasview mcp copy context7 --from claude --to codex,gemini
nyrasview mcp test "uvx mcp-server-fetch"
nyrasview mcp rm playwright
```

## Self-heal: agents fix failing checks on their own

nyrasview runs your check: tests, build or typecheck. It suggests one from `package.json`, `pyproject`, `Cargo.toml`, `go.mod` or the `Makefile`. When the check fails:

1. it gives the error output to an agent
2. re-runs the check
3. repeats until it passes, or until N tries are used up, and then alerts you

Each new try continues the same conversation, so the agent knows what it already tried.

```sh
nyrasview heal                      # detect the check and fix until it passes
nyrasview heal "npm test" -n 5      # your check, up to 5 fixes
nyrasview heal --live "pytest -q"   # live Claude Code sessions here fix failures before they stop
```

With **live** mode, whenever Claude Code says it's done in that project, its Stop hook runs the check. If the check fails, Claude gets the errors and keeps going on its own. You only hear about it once it passes, or once the tries are used up. All of this is also on the **Self-heal** page.

## Hand off

Every Claude Code or Codex session has a **⇢ Hand off** button: on its agent page, on the Watch view, and on the overview when it hits a limit. The dialog shows:

- what will be passed on: your prompts, the changed files, the commands it ran, the git diff
- who can take over
- why an option isn't available yet (e.g. "connect your Anthropic API key to keep this same session going"), with a button to fix it

## On your phone

Turn it on from the **Phone** page, or run `nyrasview gui --phone`. nyrasview also listens on your Wi-Fi (and Tailscale) and shows a QR code. Scan it with your phone camera and you're signed in; without that code, nobody can open it.

You get the full app in a phone layout. Use **Add to Home Screen** to open it like an app. **Sign out all phones** makes a new code.

## Relay: when one agent runs out, the next one takes over

When Claude Code or Codex hits its usage limit, nyrasview writes `.nyrasview/HANDOFF.md` into the project with:

- your prompts (the latest one marked)
- what the agent said it did
- every file it changed and the commands it ran
- `git status` and the diff vs HEAD

Then it opens the next installed agent in a new terminal and tells it to read that file and carry on. The default order is codex, claude, gemini, opencode, cursor and so on; set `[handoff] chain = [...]` to change it. `.nyrasview/` is added to `.git/info/exclude` so it is never committed.

```sh
nyrasview relay                 # automatic
nyrasview handoff --to gemini   # manual, right here in this terminal
```

## Local AI

Ollama, LM Studio, llama.cpp, vLLM, Jan and KoboldCpp are detected on their default ports. Any other OpenAI-compatible server can be added:

```toml
[[local]]
name = "Box"
url = "http://192.168.1.20:8000"
model = "qwen3-32b"
```

Local models can be assigned collab sections like any other agent (`collab assign doc.md Intro ollama "..."`), and you can try them from the GUI's Local AI page.

## Collab: several AIs, one document

```sh
nyrasview collab new plan.md -t "Launch plan" -s "Goals,Architecture,Risks"
nyrasview collab assign plan.md Goals claude "Three measurable goals"
nyrasview collab assign plan.md Architecture codex "Components and data flow"
nyrasview collab assign plan.md Risks openrouter "Top risks with mitigations" --model moonshotai/kimi-k2
nyrasview collab whole plan.md gemini "Unify tone and remove repetition"
nyrasview collab run plan.md
nyrasview collab undo plan.md
```

Section tasks run in parallel, and each agent sees the whole document for context. Whole-document tasks run after them, one at a time. Every write is backed up, so `undo` steps back one write at a time.

### Obsidian

Notes in a vault work as collab docs, and you can write them as `Vault:Folder/Note`. You can also assign work from inside Obsidian:

```markdown
## Architecture
> [!ai] codex
> Describe the services and how they talk to each other
```

Then run `nyrasview collab watch`. It picks up the callouts in every vault, runs them, writes the answers into those sections and records `ai-authors` in the note's frontmatter.

## Config

`~/.config/nyrasview/config.toml` (on Windows: `%APPDATA%\nyrasview\config.toml`)

```toml
[openrouter]
api_key = "sk-or-..."
model = "anthropic/claude-sonnet-4.5"

[ollama]
model = "qwen3:14b"

[handoff]
chain = ["codex", "gemini", "opencode"]

[fallback]
auto_retry = true                       # retry limit-hit prompt runs on your API key
openrouter_model = "moonshotai/kimi-k2" # model opencode uses through OpenRouter

[planner]
agent = "ollama"                        # leave out to auto-pick the smallest model
model = "qwen2.5:3b"
terminal = "kitty"

[collab]
timeout = 900

[agents.kimi]
headless = { cmd = ["kimi", "--print", "-c", "{prompt}"] }

[agents.myagent]
name = "My Agent"
hex = "#ff8800"
names = ["myagent"]
headless = { cmd = ["myagent", "--prompt-stdin"], stdin = true }
```
