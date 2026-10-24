# rtk

Integrates [rtk](https://github.com/rtk-ai/rtk) — a token-optimizing CLI proxy that
filters and summarizes command output before it reaches the LLM context — into
OpenLumara.

When enabled, OpenLumara uses rtk as intended (the same workflow `rtk init` sets
up for Claude Code / Codex):

1. **Hook rewriting** — commands run through the `host_shell` and sandboxed
   `shell` modules are transparently rewritten by rtk's own hook engine
   (`rtk hook check`), e.g. `ls -la` → `rtk ls -la`, `cat file` → `rtk read
   file`, `grep -r foo .` → `rtk grep -r foo .`. Commands rtk has no filter
   for run unchanged.
   For the **sandboxed shell**, the module auto-provisions rtk: the binary is
   copied into the sandbox home folder (which the sandbox mounts from your
   host) as `$HOME/.rtk/rtk`, and wrapped commands get a `PATH` prefix so the
   container finds it. Everything is verified *inside the container* before
   any wrapping happens. If provisioning or verification fails (tmpfs sandbox
   home, exotic image, SELinux labels), the sandboxed shell is left completely
   untouched. If OpenLumara itself runs inside a container (the shell module's
   passthrough mode), no copy is made — rtk is verified via `PATH` instead.
2. **System prompt instructions** — injects rtk's canonical usage instructions
   (adapted from the RTK.md that `rtk init` writes), so the AI knows output is
   condensed and how to recover elided data.
3. **Tools** — `rtk_exec` (run host commands through rtk), `rtk_recall` (get
   back output a filter elided), `rtk_gain` (token savings report).
4. **WebUI sidebar panel** — an `RTK` tab in the sidebar showing token savings,
   the efficiency meter, wrapped-shell state and sandbox probe status live,
   fed by the module's `/api/ext/rtk/stats` endpoint. Toggle it with the
   auto-injected `enable_webui_extension` setting.
5. **`/rtk` command** — `/rtk` shows status (resolved + configured binary,
   wrapped shells, sandbox probe state, savings), `/rtk gain` and
   `/rtk recall <hash>` for the two recovery views.

## Requirements

The `rtk` binary must be installed on the host (it is not a pip package):
see the [releases page](https://github.com/rtk-ai/rtk/releases). If it's not on
your PATH, set the `rtk_path` setting to its absolute path. When no rtk binary
can be resolved, the module stays completely passive (no wrapping, no prompt
injection) and logs why.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `rtk_path` | `rtk` | Host rtk binary. Bare name = search PATH, then common locations (like `~/.local/bin/rtk`). An explicit path (absolute or `~/...`) is authoritative — used as-is for rewriting, `rtk_exec`, and the sandbox copy; if it isn't an executable file the module errors instead of silently using another rtk |
| `inject_instructions` | `true` | Inject rtk usage instructions into the system prompt |
| `wrap_host_shell` | `true` | Rewrite `host_shell` commands through rtk (intended workflow) |
| `wrap_sandboxed_shell` | `true` | Same for the sandboxed `shell` module, with rtk auto-installed into the sandbox home and verified in-container first |
| `execution_timeout` | `0` | Timeout (s) for `rtk_exec`; default `0` = unlimited (core `tool_timeout` still applies — see Notes) |
| `output_limit` | `0` | Max chars returned by `rtk_exec` / `rtk_recall`; default `0` = unlimited |
| `enable_webui_extension` | `true` | Auto-injected because this module ships a `webui/` folder; toggles the RTK sidebar panel |

## Notes

- This module is marked **unsafe** because `rtk_exec` runs commands on the host.
  Because of that designation the UI hides it by default: to make the rtk
  module appear in the UI, set `show_unsafe_settings: true` in OpenLumara's
  `config.yml` (it belongs to the `webui` channel settings and defaults to
  `false` — it is deliberately not toggleable from the UI itself).
- `execution_timeout: 0` removes the module's own timeout, but OpenLumara's core
  `tool_timeout` setting (core.tool_timeout) still caps every tool call at the
  framework level — raise it in `config.yml` too for truly unbounded commands.
- One module covers every shell backend (`host_shell`, sandboxed `shell`, and
  its own `rtk_exec` tool) because the rewrite engine, instructions, and
  recovery tooling are identical; the only per-backend question is *where the
  command executes and whether rtk exists there*, which is what the
  `wrap_*` settings and in-container verification handle. Splitting into
  `host_rtk` + `rtk` modules would duplicate all of that and stack two sets of
  system-prompt instructions.
- Wrapping is transparent: the AI never needs to prefix commands with `rtk`
  itself, and double-prefixing is safe (rtk's engine never re-rewrites).
- Wrapping is applied lazily too: if a shell module is enabled after rtk
  started, it gets wrapped on the next system-prompt build. Original shell
  module methods are restored when this module is disabled or reloaded;
  reloading also re-provisions the sandbox copy (so rtk updates propagate).
- The optional `rtk llm-proxy` (transparent LLM API proxy) is licensed
  separately from rtk and is not managed by this module.

## Attribution

**RTK — "Rust Token Killer"** is a separate open-source project by
[RTK AI Labs](https://github.com/rtk-ai) ([repo](https://github.com/rtk-ai/rtk),
[site](https://www.rtk-ai.app)), licensed under the **Apache License 2.0**.
The RTK license text is bundled here as [LICENSE-rtk](LICENSE-rtk) and this
module's own code is GPL-3.0-or-later ([LICENSE](LICENSE)); see
[CREDITS.md](CREDITS.md) for full attribution. This module only invokes the
externally installed `rtk` binary and ships no RTK code or binaries.

---

Module code Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>, licensed under GPL-3.0-or-later.
RTK itself remains © RTK AI Labs (Apache-2.0).
