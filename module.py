# rtk module for OpenLumara — integrates RTK (https://github.com/rtk-ai/rtk)
# Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Integrates RTK ("Rust Token Killer") by RTK AI Labs — https://github.com/rtk-ai/rtk
# RTK is licensed under the Apache License 2.0 (see LICENSE-rtk in this folder).
# See CREDITS.md in this folder for full attribution.
import asyncio
import functools
import inspect
import os
import re
import shutil
import sys

import core
import channels.webui.api as webui


class Rtk(core.module.Module):
    """Routes shell commands through rtk (https://github.com/rtk-ai/rtk), a token-optimizing CLI proxy that condenses command output before it reaches your AI's context"""

    category = "system"

    # the rtk_exec tool runs commands on the host (with rtk filtering), so mark this honestly
    unsafe = True

    settings = {
        "rtk_path": {
            "default": "rtk",
            "description": "Path to the host rtk binary used for everything (exec tool, command rewriting, and the binary copied into the sandbox). A bare name like 'rtk' is looked up on your PATH, then common locations like ~/.local/bin/rtk. An explicit path (absolute or starting with ~/) is used as-is and must be an executable file: if it is not, the module reports an error instead of silently using another rtk. Takes effect after the module reloads"
        },
        "inject_instructions": {
            "default": True,
            "description": "Injects rtk's usage instructions into the system prompt, so your AI knows command output is condensed and how to recover output that was elided"
        },
        "wrap_host_shell": {
            "default": True,
            "description": "Transparently rewrite commands run through the host_shell module with rtk's hook engine (e.g. 'ls -la' becomes 'rtk ls -la', 'cat file' becomes 'rtk read file'). This is rtk's intended workflow: your AI runs commands normally, output arrives condensed. Only does something if the host_shell module is enabled."
        },
        "wrap_sandboxed_shell": {
            "default": True,
            "description": "Same rewrite for the sandboxed shell module. rtk is automatically copied into the sandbox home folder (which the sandbox mounts from your host) and verified INSIDE the container before any wrapping happens. If verification fails (e.g. tmpfs sandbox home, or a custom image that can't run rtk), the sandboxed shell is left completely untouched."
        },
        "execution_timeout": {
            "default": 0,
            "description": "Maximum amount of time (in seconds) the rtk_exec tool may let a command run. Default 0 means unlimited; set a positive number to enforce a timeout. Note: with the default, OpenLumara's core 'tool_timeout' setting is what caps tool calls at the framework level, so raise it there too if you want truly unbounded commands"
        },
        "output_limit": {
            "default": 0,
            "description": "Maximum amount of characters rtk_exec and rtk_recall return. Default 0 means unlimited (rtk output is already heavily condensed); set a positive number to enforce a safety cap"
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.binary = None
        self._patches = [] # [(module, method_name, original_callable), ...]
        self._sandbox_state = None # None = not probed yet, "ready", "unavailable"
        self._sandbox_prefix = None

    # ------------------------- binary resolution -------------------------

    def _resolve_binary(self):
        """resolves the host rtk executable from the rtk_path setting.

        An explicit path (absolute, ~/..., or anything containing a path
        separator) is authoritative: if it is not an executable file, that is
        reported as an error instead of silently using a different rtk found
        elsewhere on the system. A bare command name is looked up on PATH
        first, and the stock "rtk" name additionally falls back to common
        install locations.
        """
        configured = str(self.config.get("rtk_path") or "rtk").strip() or "rtk"
        expanded = os.path.expanduser(configured)

        if os.sep in expanded or (os.altsep and os.altsep in expanded):
            # explicit path wins or errors out. never silently swap binaries.
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded

            self.log("rtk", f"configured rtk_path '{configured}' is not an executable file. Update the rtk_path setting to your host rtk binary (or set it back to 'rtk' to auto-detect). rtk integration stays passive until then.")
            return None

        found = shutil.which(expanded)
        if found:
            return found

        if expanded == "rtk":
            # convenience: common install locations for the default name only
            for candidate in ("~/.local/bin/rtk", "~/.cargo/bin/rtk", "/usr/local/bin/rtk", "/usr/bin/rtk"):
                path = os.path.expanduser(candidate)
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    return path

        return None

    def _child_env(self):
        """env for child processes, with rtk's own folder added to PATH so rewritten commands can find it even if openlumara was started with a stripped PATH"""
        env = os.environ.copy()
        if not self.binary:
            return env

        rtk_dir = os.path.dirname(os.path.abspath(self.binary))
        if rtk_dir not in env.get("PATH", "").split(os.pathsep):
            env["PATH"] = rtk_dir + os.pathsep + env.get("PATH", "")

        return env

    # ------------------------- hook-engine rewriting -------------------------

    async def _rewrite(self, command):
        """asks rtk's hook engine how it would rewrite a command (same engine rtk's Claude Code / Codex hooks use).

        Returns the rewritten command, or the original command when rtk has no
        rewrite for it (compound commands are rewritten segment by segment).
        """
        if not self.binary or not command.strip():
            return command

        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "hook", "check", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            return command

        rewritten = stdout.decode("utf-8", errors="replace").strip()

        # exit code 0 + output = rewrite. exit 1 ("No rewrite for: ...") = keep original
        if proc.returncode == 0 and rewritten and rewritten != command:
            return rewritten

        return command

    # ------------------------- execution -------------------------

    def _timeout_value(self):
        """execution_timeout setting as seconds; 0 or below (the default) means unlimited (None)"""
        try:
            val = float(self.config.get("execution_timeout", default=0))
        except (TypeError, ValueError):
            return None # unparseable -> unlimited, matching the default
        return val if val > 0 else None

    def _limit_value(self):
        """output_limit setting as characters; 0 or below (the default) means unlimited (None)"""
        try:
            val = int(self.config.get("output_limit", default=0))
        except (TypeError, ValueError):
            return None # unparseable -> unlimited, matching the default
        return val if val > 0 else None

    async def _run_argv(self, argv, timeout, limit):
        """runs an argv list directly (no shell). returns (stdout, stderr, exit_code, error_or_None)"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env()
            )
        except Exception as e:
            return "", "", -1, f"failed to start command: {e}"

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = await proc.communicate()
            except Exception:
                stdout, stderr = b"", b""

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        truncated = False
        if limit and len(stdout_text) > limit:
            stdout_text = stdout_text[:limit]
            truncated = True
        if limit and len(stderr_text) > limit:
            stderr_text = stderr_text[:limit]
            truncated = True

        error = None
        if timed_out:
            error = f"command timed out after {timeout}s and was killed"
        elif truncated:
            error = f"output truncated to {limit} chars"

        return stdout_text, stderr_text, proc.returncode, error

    async def _run_shell(self, command, timeout, limit):
        """runs a command string through a shell. returns (stdout, stderr, exit_code, error_or_None)"""
        shell = ["cmd", "/c"] if sys.platform == "win32" else ["sh", "-c"]
        return await self._run_argv([*shell, command], timeout, limit)

    # ------------------------- tools -------------------------

    async def exec(self, cmd: str):
        """Runs a shell command on the host through rtk. The command is first rewritten by rtk's hook engine (e.g. 'ls -la' -> 'rtk ls -la', 'cat x' -> 'rtk read x', 'grep -r foo .' -> 'rtk grep -r foo .') so its output arrives condensed and token-cheap. Commands rtk has no filter for run unchanged. If a result seems empty or garbled despite a clean exit code, re-run it with 'rtk bypass ' in front.

        Args:
        cmd (str): The shell command to run
        """
        if not self.binary:
            return self.result("rtk binary not found. Install it from https://github.com/rtk-ai/rtk or set the rtk_path setting.", success=False)

        command = await self._rewrite(cmd)

        stdout, stderr, code, error = await self._run_shell(
            command,
            timeout=self._timeout_value(),
            limit=self._limit_value()
        )

        results = {"stdout": stdout, "stderr": stderr, "exit_code": code}
        if error:
            results["error"] = error

        return self.result(results, success=(code == 0 and error is None))

    async def recall(self, hash: str, full: bool = False, grep: str = ""):
        """Recovers output that an rtk filter elided from condensed command output, using the content hash from a recovery hint.

        Args:
        hash (str): Hash from a recovery hint in condensed output (a unique prefix is enough)
        full (bool): Return the complete original output instead of just the elided part
        grep (str): Optional regex to filter the recalled lines
        """
        if not self.binary:
            return self.result("rtk binary not found. Install it from https://github.com/rtk-ai/rtk or set the rtk_path setting.", success=False)

        argv = [self.binary, "recall", hash]
        if full:
            argv.append("--full")
        if grep:
            argv.extend(["--grep", grep])

        stdout, stderr, code, error = await self._run_argv(argv, timeout=15, limit=self._limit_value())

        if code != 0 and not stdout:
            return self.result(stderr or error or f"rtk recall failed (exit code {code})", success=False)

        return self.result({"stdout": stdout, "stderr": stderr, "exit_code": code})

    async def gain(self):
        """Shows rtk's token-savings report: how many tokens rtk's output filters saved so far, and recent command stats."""
        if not self.binary:
            return self.result("rtk binary not found. Install it from https://github.com/rtk-ai/rtk or set the rtk_path setting.", success=False)

        stdout, stderr, code, error = await self._run_argv([self.binary, "gain"], timeout=15, limit=8000)

        if code != 0 and not stdout:
            return self.result(stderr or error or f"rtk gain failed (exit code {code})", success=False)

        return self.result({"stdout": stdout, "stderr": stderr, "exit_code": code})

    # ------------------------- shell module wrapping -------------------------

    # sandbox home inside the container is a mount of the shell module's host workspace,
    # so a binary dropped into the workspace from the host side shows up at $HOME/.rtk/rtk
    SANDBOX_PATH_PREFIX = 'export PATH="$HOME/.rtk:$PATH"; '

    def _wrap(self, module_name, method, prefix=None):
        """installs the rewriting wrapper on module.method (once). returns True if wrapped now.

        The wrapper binds through the original method's real signature (inspect +
        functools.wraps), because the shells use different parameter names
        (host_shell.exec uses 'cmd', shell.run uses 'command') and callers -
        including the tool layer - may pass it positionally or by keyword.
        """
        mod = self.manager.modules.get(module_name)
        if mod is None or getattr(mod, "_rtk_wrapped", False):
            return False

        original = getattr(mod, method)

        try:
            sig = inspect.signature(original)
            cmd_param = next(iter(sig.parameters)) # both shells take the command as their first parameter
        except (TypeError, ValueError, StopIteration):
            self.log("rtk", f"could not introspect {module_name}.{method}(), leaving it untouched")
            return False

        @functools.wraps(original)
        async def wrapper(*args, _original=original, _sig=sig, _cmd_param=cmd_param, _prefix=prefix, **kwargs):
            try:
                bound = _sig.bind(*args, **kwargs)
            except TypeError:
                return await _original(*args, **kwargs) # unexpected call shape: pass through untouched

            command = bound.arguments.get(_cmd_param)
            if isinstance(command, str):
                rewritten = await self._rewrite(command)
                if _prefix and rewritten != command:
                    rewritten = _prefix + rewritten
                bound.arguments[_cmd_param] = rewritten

            return await _original(*bound.args, **bound.kwargs)

        setattr(mod, method, wrapper)
        mod._rtk_wrapped = True
        self._patches.append((mod, method, original))
        self.log("rtk", f"wrapping {module_name}.{method}() through rtk")
        return True

    def _provision_sandbox_binary(self, shell_mod):
        """copies the rtk binary into the sandbox workspace (mounted as $HOME inside the container). returns the host-side path, or None."""
        workspace = getattr(shell_mod, "host_workspace", None)
        if not workspace or not os.path.isdir(workspace):
            return None

        # a tmpfs sandbox home is RAM-only inside the container: a copied file would never reach it
        if not shell_mod.config.get("persistent_data", default=True):
            return None

        dest_dir = os.path.join(workspace, ".rtk")
        dest = os.path.join(dest_dir, "rtk")
        try:
            os.makedirs(dest_dir, exist_ok=True)
            if not (os.path.exists(dest) and os.path.getsize(dest) == os.path.getsize(self.binary)):
                shutil.copy2(self.binary, dest)
                os.chmod(dest, 0o755)
                self.log("rtk", f"installed rtk into sandbox home: {dest}")
            return dest
        except Exception as e:
            self.log("rtk", f"could not copy rtk into the sandbox workspace: {e}")
            return None

    async def _verify_in_sandbox(self, shell_mod, prefix):
        """runs 'rtk --version' through the (still unwrapped) sandboxed shell to prove rtk works inside the container"""
        try:
            res = await shell_mod.run(f"{prefix}rtk --version")
        except Exception:
            return False

        content = res.get("content") if isinstance(res, dict) else None
        stdout = content.get("stdout", "") if isinstance(content, dict) else str(content or "")
        return "rtk" in stdout

    async def _prepare_sandbox_shell(self):
        """brings rtk into the sandboxed shell environment, verifies it, and only then wraps"""
        shell_mod = self.manager.modules.get("shell")
        if shell_mod is None or getattr(shell_mod, "_rtk_wrapped", False):
            return

        if self._sandbox_state is None:
            if getattr(shell_mod, "passthrough", False):
                # openlumara itself runs in a container and the shell module executes directly there,
                # so rtk must already be on that container's PATH
                self._sandbox_prefix = ""
                if await self._verify_in_sandbox(shell_mod, ""):
                    self._sandbox_state = "ready"
                else:
                    self._sandbox_state = "unavailable"
                    self.log("rtk", "sandbox shell runs in passthrough mode but rtk is not on its PATH. Sandboxed commands will not be rewritten.")
            else:
                dest = self._provision_sandbox_binary(shell_mod)
                if dest is None:
                    self._sandbox_state = "unavailable"
                    self.log("rtk", "could not place rtk inside the sandbox home (is persistent_data on and sandbox_path valid?). Sandboxed commands will not be rewritten.")
                elif await self._verify_in_sandbox(shell_mod, self.SANDBOX_PATH_PREFIX):
                    self._sandbox_prefix = self.SANDBOX_PATH_PREFIX
                    self._sandbox_state = "ready"
                else:
                    self._sandbox_state = "unavailable"
                    self.log("rtk", "rtk was copied into the sandbox home but failed to run inside the container. Sandboxed commands will not be rewritten.")

        if self._sandbox_state == "ready":
            self._wrap("shell", "run", prefix=self._sandbox_prefix or None)

    async def _apply_wrapping(self):
        """patches enabled shell modules so their commands are rewritten through rtk's hook engine before execution, which is rtk's intended (hook-based) workflow. originals are restored on shutdown/reload."""
        if self.config.get("wrap_host_shell", default=True):
            self._wrap("host_shell", "exec")
        if self.config.get("wrap_sandboxed_shell", default=True):
            await self._prepare_sandbox_shell()

    def _remove_wrapping(self):
        for mod, method, original in self._patches:
            try:
                setattr(mod, method, original)
                mod._rtk_wrapped = False
            except Exception:
                pass
        self._patches = []
        self._sandbox_state = None
        self._sandbox_prefix = None

    # ------------------------- lifecycle -------------------------

    async def on_ready(self):
        self.binary = self._resolve_binary()

        if not self.binary:
            self.log("rtk", "rtk binary not found. Install it from https://github.com/rtk-ai/rtk (or set the rtk_path setting), then reload this module. Staying passive for now.")
            return

        self.log("rtk", f"using rtk binary: {self.binary}")
        await self._apply_wrapping()

    async def on_shutdown(self):
        self._remove_wrapping()

    async def on_system_prompt(self):
        if not self.binary:
            return None

        # re-apply lazily: shell modules enabled after this module started get wrapped too
        await self._apply_wrapping()

        if not self.config.get("inject_instructions", default=True):
            return None

        wrapped = sorted({f"{m.name}.{meth}" for m, meth, _ in self._patches})

        lines = [
            "# rtk token-optimized command output",
            "",
            "Command output is condensed by rtk to save tokens, keeping every signal and dropping costly noise. Treat it as the complete result: run commands normally, and batch related commands into one call to avoid extra turns. Truncated results state their recovery path in their own output; use rtk_recall with the hash from the hint to get elided output back. Re-run a command as 'rtk bypass <cmd>' only when its result is unusable: empty when output was clearly expected, contradicting its exit code, or garbled."
        ]

        if wrapped:
            lines.append("")
            lines.append(f"Commands run through these shell tools are routed through rtk automatically: {', '.join(wrapped)}. Do not add the 'rtk' prefix yourself there.")

        lines.append("")
        lines.append("Use rtk_exec to run host shell commands through rtk, rtk_recall to recover elided output, and rtk_gain to check token savings.")

        return "\n".join(lines)

    # ------------------------- webui extension -------------------------

    async def _rtk_version(self):
        """`rtk --version` output, or empty string"""
        if not self.binary:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return stdout.decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    @staticmethod
    def _parse_gain(raw):
        """extracts numbers from `rtk gain` human-readable output; unparseable fields stay None"""
        def _re(pattern):
            match = re.search(pattern, raw)
            return match.group(1) if match else None

        parsed = {
            "commands": _re(r"Total commands:\s*(\d+)"),
            "input_tokens": _re(r"Input tokens:\s*([0-9.]+\w*)"),
            "output_tokens": _re(r"Output tokens:\s*([0-9.]+\w*)"),
            "saved_tokens": _re(r"Tokens saved:\s*([0-9.]+\w*)"),
            "saved_percent": _re(r"Tokens saved:\s*[0-9.]+\w*\s*\(([0-9.]+)%"),
            "exec_time": _re(r"Total exec time:\s*(\S+)"),
        }

        filled = raw.count("\u2588") # █
        empty = raw.count("\u2591") # ░
        parsed["efficiency"] = round(100 * filled / (filled + empty), 1) if filled + empty else None

        return parsed

    @webui.route("stats")
    async def _route_stats(self, body=None, query=None):
        """rtk integration status + token savings for the sidebar panel (GET /api/ext/rtk/stats)"""
        gain_result = await self.gain()
        content = gain_result.get("content")
        gain_raw = content.get("stdout", "") if isinstance(content, dict) else ""

        return {
            "binary": self.binary,
            "configured_path": str(self.config.get("rtk_path") or "rtk"),
            "version": await self._rtk_version(),
            "instructions": bool(self.config.get("inject_instructions", default=True)),
            "wrapped": sorted({f"{m.name}.{meth}" for m, meth, _ in self._patches}),
            "sandbox": self._sandbox_state or "not probed",
            "settings": {
                "execution_timeout": self.config.get("execution_timeout", default=0),
                "output_limit": self.config.get("output_limit", default=0)
            },
            "gain": self._parse_gain(gain_raw),
            "gain_raw": gain_raw
        }

    # ------------------------- user commands -------------------------

    @core.module.command("rtk", help={
        "": "shows rtk integration status (binary, wrapping, savings)",
        "gain": "shows rtk's token savings report",
        "recall <hash>": "recovers output an rtk filter elided"
    })
    async def cmd_rtk(self, args):
        sub = args[0].lower() if args else ""

        if sub == "gain":
            result = await self.gain()
            content = result.get("content")
            if isinstance(content, dict):
                return content.get("stdout") or content.get("stderr") or "no output"
            return str(content)

        if sub == "recall":
            if len(args) < 2:
                return "usage: rtk recall <hash>"
            result = await self.recall(args[1])
            content = result.get("content")
            if isinstance(content, dict):
                return content.get("stdout") or content.get("stderr") or "no output"
            return str(content)

        if not self.binary:
            return "rtk binary not found. Install it from https://github.com/rtk-ai/rtk or set the rtk_path setting."

        version = await self._rtk_version()

        wrapped = sorted({f"{m.name}.{meth}" for m, meth, _ in self._patches})

        sandbox_states = {"ready": "ready", "unavailable": "unavailable (commands left untouched)", None: "not probed yet"}

        lines = [
            f"binary: {version or self.binary}",
            f"configured rtk_path: {self.config.get('rtk_path') or 'rtk'}",
            f"system prompt instructions: {'on' if self.config.get('inject_instructions', default=True) else 'off'}",
            f"wrapped shells: {', '.join(wrapped) if wrapped else 'none'}",
            f"  (wrap_host_shell={self.config.get('wrap_host_shell', default=True)}, wrap_sandboxed_shell={bool(self.config.get('wrap_sandboxed_shell', default=True))})",
            f"sandbox rtk: {sandbox_states.get(self._sandbox_state)}" + (f" via $HOME/.rtk/rtk" if self._sandbox_state == "ready" and self._sandbox_prefix else ""),
        ]

        result = await self.gain()
        content = result.get("content")
        if isinstance(content, dict) and content.get("stdout"):
            lines.append("")
            lines.append(content["stdout"])

        return "\n".join(lines)
