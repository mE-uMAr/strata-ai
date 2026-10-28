"""Running one CLI process for one answer. Shared by every CLI adapter.

This is machinery, not an adapter: it knows nothing about any tool's flags or output.
The rules it enforces are the ones the brief sets for every CLI path:

- **No interactive prompt can hang the call.** stdin is either the prompt, written and
  then closed, or the null device. A tool that stops to ask "trust this folder?" reads
  end-of-file and fails at once instead of waiting for a human who is not there.
- **A hard timeout that actually ends the work.** On Windows the CLIs are often ``.cmd``
  shims that start node; killing the shim leaves node running and holding the pipes.
  So the whole process tree is killed (``taskkill /T``, or the process group on POSIX).
- **No inherited working directory.** Each call runs in a fresh, empty temporary
  directory, removed afterwards, so a coding CLI cannot read the repository it was
  launched from unless a caller deliberately passes a directory.
- **A scrubbed environment.** Every variable shaped like a credential is removed. This
  is the guard against the most expensive silent failure on the subscription path: a
  CLI that finds an API key in its environment uses it, and bills per token, while the
  call still looks like a subscription call from outside.
- **stderr is kept** and goes into the error raised on failure.

On Windows a ``.cmd``/``.bat`` shim is run by ``cmd.exe``, which re-parses its command
line; an argument containing ``&``, ``|``, ``%`` and the like could run a command.
Untrusted text therefore never goes on the argv of a shim, and any argument holding a
metacharacter is refused before the process starts.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..protocol import ProviderError, Unavailable

IS_WINDOWS = sys.platform == "win32"

_CREDENTIAL_NAME = re.compile(r"(API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SECRET|PASSWORD)$|^(AIPROVIDER_|ANTHROPIC_|CLAUDE_CODE_USE_)")
_NESTING = {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"}
"""Set by a parent Claude Code session; inherited, they make a child think it is nested."""
_SHIM_METACHARACTERS = set('&|<>^%!"\r\n')

QUIET = {"CI": "1", "NO_COLOR": "1", "TERM": "dumb", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}


@dataclass(frozen=True)
class Run:
    stdout: str
    stderr: str
    returncode: int
    latency_ms: int
    workdir: str


def child_env(base: Mapping[str, str] | None = None, *, extra: Mapping[str, str] | None = None,
              keep: Sequence[str] = ()) -> dict[str, str]:
    """The parent's environment minus credentials, plus non-interactive settings.

    ``keep`` names variables to pass through even though they look like credentials;
    a config entry has to ask for each by name.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items()
           if k in keep or not (_CREDENTIAL_NAME.search(k.upper()) or k.upper() in _NESTING)}
    env.update(QUIET)
    env.update(extra or {})
    return env


def resolve(binary: Sequence[str]) -> list[str]:
    """The command prefix with its executable resolved on PATH. Missing is `Unavailable`."""
    if not binary:
        raise Unavailable("no binary configured")
    first = binary[0]
    found = first if Path(first).is_file() else shutil.which(first)
    if found is None:
        raise Unavailable(f"{first!r} is not on PATH")
    return [found, *binary[1:]]


def is_shim(executable: str) -> bool:
    return IS_WINDOWS and Path(executable).suffix.lower() in (".cmd", ".bat")


def guard_shim(argv: Sequence[str]) -> None:
    if not is_shim(argv[0]):
        return
    for arg in argv[1:]:
        bad = sorted(_SHIM_METACHARACTERS & set(arg))
        if bad:
            raise ProviderError(
                f"refusing to pass an argument containing {bad} to {Path(argv[0]).name}: it is a "
                "cmd.exe shim, which would re-parse it. Put untrusted text on stdin."
            )


def _kill_tree(proc: subprocess.Popen) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def run(argv: Sequence[str] | Callable[[Path], Sequence[str]], *, stdin_text: str | None, timeout_s: float,
        env: Mapping[str, str], files: Mapping[str, str] | None = None, cwd: Path | None = None) -> Run:
    """Run once, in a fresh temporary directory with ``files`` written into it first (a
    schema, a system prompt). ``argv`` may be a function of that directory, for
    arguments that name a file in it."""
    workdir = Path(tempfile.mkdtemp(prefix="aiprovider-call-")) if cwd is None else cwd
    try:
        for name, content in (files or {}).items():
            (workdir / name).write_text(content, encoding="utf-8")
        args = list(argv(workdir) if callable(argv) else argv)
        guard_shim(args)
        kwargs: dict = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                args, cwd=workdir, env=dict(env),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
            )
        except OSError as e:
            raise Unavailable(f"could not start {Path(args[0]).name}: {e}") from None
        payload = stdin_text.encode("utf-8") if stdin_text is not None else None
        try:
            out, err = proc.communicate(payload, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = b"", b"(process tree did not exit after kill)"
            raise Unavailable(
                f"{Path(args[0]).name} timed out after {timeout_s:g}s and was killed; "
                f"stderr: {_tail(err.decode('utf-8', 'replace')) or '(empty)'}"
            ) from None
        latency = int((time.perf_counter() - started) * 1000)
        return Run(stdout=out.decode("utf-8", "replace"), stderr=err.decode("utf-8", "replace"),
                   returncode=proc.returncode, latency_ms=latency, workdir=str(workdir))
    finally:
        if cwd is None:
            shutil.rmtree(workdir, ignore_errors=True)


def describe_failure(tool: str, result: Run, detail: str = "") -> str:
    parts = [f"{tool} exited with status {result.returncode}"]
    if detail:
        parts.append(detail)
    parts.append(f"stderr: {_tail(result.stderr) or '(empty)'}")
    return "; ".join(parts)


def matches(patterns: Sequence[str], *texts: str) -> bool:
    joined = "\n".join(texts).lower()
    return any(p.lower() in joined for p in patterns)
