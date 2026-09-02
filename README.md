# cc-manager

A terminal UI for managing, searching and resuming Claude Code sessions.

Claude Code writes every CLI session to `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`.
`cc-manager` reads those transcripts and gives you a searchable list with the branch,
last activity, and a mini-summary of each one — plus a one-key resume.

```
 cc-manager  alice                      12/17 shown  ●4  ⚠1  ⏸2   view:active
 search  (branch, title, prompt, id)…
   Branch               Last activity   Size    Summary
 ● main                 3s ago          186.0MB kicad-autorouter  [busy]
 ⚠ main                 2d ago          515.3MB Optimize Raspberry Pi cost and performance
 ◐ feature/audio        Aug 10           20.5MB Develop retro Japanese car audio visualizers
 · detached             Aug 23           73.0KB Autorouter directory setup
```

## Requirements

- Python 3.10+
- [`textual`](https://textual.textualize.io/) — `pip install textual`
- The `claude` CLI on your `PATH`

## Install

The tool is a plain package; it does not need to be pip-installed. It currently lives at:

```
~/.claude/tools/cc-manager/
```

Verify it works:

```sh
cd ~/.claude/tools/cc-manager
python tests/test_cc_manager.py   # parser, crash detection, park round-trip
python tests/test_tui.py          # headless UI checks
```

## Alias it to `cc-manager`

### PowerShell (Windows)

Open your profile:

```powershell
notepad $PROFILE      # create it first if missing: New-Item -ItemType File -Force $PROFILE
```

Add:

```powershell
function cc-manager { & "$HOME\.claude\tools\cc-manager\cc-manager.ps1" @args }
```

Reload with `. $PROFILE`. A function is used rather than `Set-Alias` because aliases
cannot forward arguments.

### bash / zsh / git-bash

Add to `~/.bashrc` or `~/.zshrc`:

```sh
alias cc-manager='~/.claude/tools/cc-manager/cc-manager'
```

Reload with `source ~/.bashrc`. If the launcher is not executable:
`chmod +x ~/.claude/tools/cc-manager/cc-manager`.

### Or put it on your PATH

```sh
ln -s ~/.claude/tools/cc-manager/cc-manager ~/.local/bin/cc-manager
```

## Usage

```sh
cc-manager                # TUI for the current directory's project
cc-manager --all          # every project, not just this directory
cc-manager --list         # plain listing, no TUI
cc-manager --json         # machine-readable metadata
cc-manager --doctor       # report sessions that ended uncleanly
cc-manager --tail 5c157aa # read a session's recent output without its terminal
cc-manager --tail circuit -n 30   # ...by title, showing more turns
cc-manager --resume-last  # jump straight back into the newest session
cc-manager --safe         # never write to transcripts (see Parking)
cc-manager -C /some/path  # act as if run from another directory
```

### Keys

| Key     | Action |
| ------- | ------ |
| `↑` `↓` | Move between sessions |
| `Enter` | Resume — runs `claude --resume <id>` in the session's original directory |
| `f`     | Fork instead of resume (`--fork-session`), leaving the original untouched |
| `p`     | Park / unpark the highlighted session |
| `n`     | Rename the session |
| `v`     | Cycle view: active → parked → all |
| `a`     | Toggle "all projects" |
| `r`     | Rescan from disk |
| `/`     | Focus the search box |
| `c`     | Copy the session id |
| `Esc`   | Clear the search |
| `q`     | Quit |

Search matches the title, the first prompt, the branch, the session id and the path,
and all terms must match (`main gemini` finds the Gemini session on `main`).

## Session state

Every row carries a state glyph. This is the part that matters after your machine
dies without a clean `/quit`:

| Glyph | Meaning |
| ----- | ------- |
| `●` | **Running right now** in another terminal. Resuming will collide with the live copy, so cc-manager asks first and suggests `f` to fork instead. |
| `⚠` | **Ended uncleanly** — crash, killed terminal, or power loss. |
| `◐` | **Stopped mid-turn** — a prompt was never answered, or a tool call never returned. |
| `·` | Closed normally. |
| `⏸` | Parked. |

Unclean sessions are detected two ways, both of which survive a power cut:

- Claude Code registers each live session as `~/.claude/sessions/<pid>.json` and deletes
  it on a clean exit. A leftover file whose process is gone means that session never shut
  down. PID reuse is accounted for: the process must still actually be `claude`.
- A transcript whose final line is half-written JSON was being appended to at the moment
  the power went. That partial line is detected, reported, and never treated as data.

`cc-manager --doctor` lists these with the exact `claude --resume` command to recover each
one, and `--doctor --prune-stale` clears the orphaned registry files once you are done.

Nothing is cached between runs: every launch, and every press of `r`, re-reads the
directory from scratch, so sessions written by your other terminals always show up with
their current titles and timestamps.

## Parking

Parking shelves a session you do not want in your working list without deleting anything.

There is **no `claude --rename` flag**, so cc-manager renames the way Claude Code does
internally — by appending a record to the transcript:

```json
{"type":"custom-title","customTitle":"parked-hiking-trip","sessionId":"..."}
```

The last such record wins, so this is a pure append: no existing byte is ever rewritten,
and the new name also shows up in Claude Code's own `/resume` picker. If the transcript
was left half-written by a crash, a newline is inserted first so the damaged line is never
merged with the new one.

cc-manager also keeps its own list at `~/.claude/cc-manager/parked.json`, which records
the previous title so unparking restores it. Use `--safe` if you would rather cc-manager
never touch the transcripts at all — parking then lives only in that sidecar file, and
the park still shows up in the UI.

## How the metadata is derived

| Field | Source |
| ----- | ------ |
| Session id | The transcript filename |
| Last activity | Newest `timestamp` in the file, falling back to file mtime (which stays correct through a power cut) |
| Branch | The `gitBranch` field recorded on each message. Claude Code writes a bare `HEAD` both for a detached head *and* for a directory that is not a repo, so cc-manager checks for a `.git` work tree to tell those apart and shows `-` when there is no repository |
| Summary | The first thing you actually typed, cut to one or two sentences. A `custom-title` or `ai-title` record is preferred when present |

"The first thing you typed" is narrower than "the first user record": transcripts also
store tool results, hook output, slash-command expansions, task notifications, caveats
and pasted-image markers as user turns. Those are filtered out, so the summary is a real
prompt rather than machinery.

## Performance

Transcripts get large — half a gigabyte for a long session is normal. Nothing here ever
reads a whole file. Each session is summarised from a **head scan** that stops at the
first human prompt and a **tail scan** of the last few hundred kilobytes, run across a
thread pool.

Scanning 35 transcripts totalling 1.3 GB takes about one second.

## Layout

```
cc_manager/
  paths.py      cwd → project-directory encoding, transcript discovery
  parser.py     bounded head/tail JSONL parsing → SessionMeta, end-state detection
  registry.py   ~/.claude/sessions/<pid>.json → which sessions are live or crashed
  scanner.py    combines transcripts + liveness + park state + git context
  park.py       park / unpark / rename via appended custom-title records
  launcher.py   spawning `claude --resume`
  app.py        the Textual UI
  cli.py        argument parsing and the non-TUI output modes
tests/
  test_cc_manager.py   fixtures for parsing, crash detection, park round-trip
  test_tui.py          headless UI checks
```

## Environment variables

| Variable | Effect |
| -------- | ------ |
| `CLAUDE_CONFIG_DIR` | Use a different Claude Code config directory (default `~/.claude`) |
| `CC_MANAGER_CLAUDE_BIN` | Full path to the `claude` executable, if it is not on `PATH` |
| `CC_MANAGER_PYTHON` | Interpreter the launcher scripts should use |
