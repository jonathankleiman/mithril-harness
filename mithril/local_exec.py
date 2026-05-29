"""Host-side executor — a drop-in replacement for the Podman ``Sandbox``.

The benchmark documents are trusted fixtures shipped with LAB, so per-task
container isolation is unnecessary overhead for an evaluation run on the
user's own machine. ``LocalSandbox`` subclasses the real ``Sandbox`` and
overrides *only* the three methods that touch Podman (``start``, ``stop``,
``exec``); every filesystem method (``read_file``, ``write_file``,
``exists``, ``list_files``, ``_to_host``) and the entire path-discipline
contract (``assert_sandbox_path``, ``is_writable``) are inherited unchanged,
so tool semantics are identical to the sandboxed harness.

Security note: this trades container isolation for speed. For untrusted
documents, use the stock Podman ``Sandbox`` instead (``run.py --sandbox``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from sandbox.sandbox import (
    DOCUMENTS_PATH,
    OUTPUT_PATH,
    WORKSPACE_PATH,
    ExecResult,
    Sandbox,
)
from harness.tools import ToolExecutor

# Host-side document parsers — identical logic to sandbox/parsers/parse_doc.py
import pandas as pd
import pdfplumber
from markitdown import MarkItDown


class LocalSandbox(Sandbox):
    """Sandbox that runs on the host with no container."""

    def start(self) -> None:
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.container_name = None
        self._started = True

    def stop(self) -> None:
        self._started = False

    # Env vars scrubbed from the agent's bash so it can never read API keys/secrets.
    _SECRET_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                       "ANTHROPIC", "OPENAI", "DEEPSEEK", "AWS_", "GH_", "GITHUB")
    # Substrings that indicate an attempt to reach the grading rubric (contamination).
    _CONTAM_MARKERS = ("task.json", "match_criteria")

    def exec(self, command, *, cwd: str = WORKSPACE_PATH, timeout=None, env=None) -> ExecResult:
        self.assert_sandbox_path(cwd)
        timeout = timeout if timeout is not None else self.default_timeout
        host_cwd = self._to_host(cwd)
        host_cwd.mkdir(parents=True, exist_ok=True)

        # Contamination guard: deny any command that references the grading file.
        low = command.lower()
        if any(m in low for m in self._CONTAM_MARKERS):
            return ExecResult(stdout="", returncode=1, timed_out=False,
                              stderr="Error: command blocked — it references the grading rubric "
                                     "(task.json/criteria), which is off-limits and would be a rule violation.")

        # Scrub secrets from the bash environment, and DON'T source the login
        # profile (-c not -lc) so the host shell can't re-introduce keys. The
        # tools the agent needs (python3, pandoc, etc.) stay on PATH.
        full_env = {k: v for k, v in os.environ.items()
                    if not any(m in k.upper() for m in self._SECRET_MARKERS)}
        full_env.update({
            "DOCUMENTS_DIR": str(self.documents_dir),
            "OUTPUT_DIR": str(self.output_dir),
            "WORKSPACE_DIR": str(self.workspace_dir),
        })
        for k, v in {**self.extra_env, **(env or {})}.items():
            full_env[k] = v

        try:
            result = subprocess.run(
                ["bash", "-c", command],
                cwd=str(host_cwd),
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                env=full_env,
            )
            return ExecResult(stdout=result.stdout, stderr=result.stderr,
                              returncode=result.returncode, timed_out=False)
        except subprocess.TimeoutExpired as e:
            return ExecResult(
                stdout=(e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")) or "",
                stderr=(e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")) or "",
                returncode=None, timed_out=True,
            )
        except OSError as e:
            return ExecResult(stdout="", stderr=f"exec failed: {type(e).__name__}: {e}",
                              returncode=1, timed_out=False)


_DOC_EXTS = {".docx", ".pdf", ".pptx", ".xlsx"}


class LocalToolExecutor(ToolExecutor):
    """ToolExecutor whose document parsing runs on the host (no parse-doc shim).

    Also fixes a real defect in the stock harness: ``grep`` over a binary
    document (``.docx``/``.pdf``/``.xlsx``/``.pptx``) searched the raw zip
    bytes and never matched the readable text — a silent false-negative that
    can make an agent wrongly conclude a term is absent. Here ``grep`` searches
    the *parsed* text for those types (cached per file), so cross-document
    needle-finding actually works.
    """

    def _doc_text(self, host_path: Path) -> str:
        cache = self.__dict__.setdefault("_doc_text_cache", {})
        key = f"{host_path}:{host_path.stat().st_mtime_ns}"
        if key in cache:
            return cache[key]
        ext = host_path.suffix.lower()
        if ext in _DOC_EXTS:
            # Reuse the host parsers via a sandbox-relative round trip.
            sb_path = self._host_to_sandbox_path(host_path)
            text = self._parse_in_sandbox(ext[1:], sb_path)
        else:
            try:
                text = host_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
        cache[key] = text
        return text

    def _host_to_sandbox_path(self, host_path: Path) -> str:
        for host_root, sb_root in (
            (self.documents_dir, "/workspace/documents"),
            (self.output_dir, "/workspace/output"),
            (self.workspace_dir, "/workspace"),
        ):
            try:
                rel = host_path.resolve().relative_to(host_root.resolve())
                return f"{sb_root}/{rel}".replace("\\", "/")
            except ValueError:
                continue
        return str(host_path)

    def _grep(self, pattern_str, search_path, file_glob, output_mode):
        import re as _re
        if not pattern_str:
            return "Error: pattern is required"
        self.grep_count += 1
        sb_path = self._resolve_search_path(search_path)
        if not self.sandbox.exists(sb_path):
            return f"Error: path does not exist: {search_path}"
        try:
            regex = _re.compile(pattern_str)
        except _re.error as e:
            return f"Error: invalid regex: {e}"
        host_root = self._sandbox_to_host_path(sb_path)
        host_root_resolved = host_root.resolve(strict=False)
        glob_pattern = file_glob or "**/*"
        results = []
        targets = [host_root] if host_root.is_file() else list(host_root.glob(glob_pattern))
        for fpath in targets:
            if not fpath.is_file() or not self._is_under(fpath, host_root_resolved):
                continue
            text = self._doc_text(fpath)
            if not text:
                continue
            try:
                rel = str(fpath.relative_to(host_root)) if host_root.is_dir() else fpath.name
            except ValueError:
                rel = fpath.name
            matches = list(regex.finditer(text))
            if not matches:
                continue
            if output_mode == "files_with_matches":
                results.append(rel)
            elif output_mode == "count":
                results.append(f"{rel}: {len(matches)}")
            elif output_mode == "content":
                for i, line in enumerate(text.split("\n")):
                    if regex.search(line):
                        results.append(f"{rel}:{i+1}: {line}")
        return "\n".join(results[:250]) if results else f"No matches for '{pattern_str}'"

    def _parse_in_sandbox(self, ext: str, sb_path: str) -> str:  # noqa: D401 (override)
        """Parse a binary document on the host, mirroring parse_doc.py exactly."""
        host = self._sandbox_to_host_path(sb_path)
        try:
            if ext == "docx":
                r = subprocess.run(
                    ["pandoc", str(host), "-t", "markdown", "--wrap=none"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                )
                if r.returncode != 0:
                    return f"Error: failed to parse {sb_path} (docx): {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'pandoc error'}"
                return r.stdout
            if ext == "pdf":
                parts: list[str] = []
                with pdfplumber.open(str(host)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            parts.append(text)
                        for table in page.extract_tables():
                            for row in table:
                                parts.append("\t".join(c if c else "" for c in row))
                            parts.append("")
                return "\n".join(parts)
            if ext == "pptx":
                return MarkItDown().convert(str(host)).text_content
            if ext == "xlsx":
                sheets = pd.read_excel(str(host), sheet_name=None)
                parts = []
                for name, df in sheets.items():
                    parts.append(f"=== Sheet: {name} ===")
                    parts.append(df.to_string(index=False))
                return "\n".join(parts)
        except Exception as e:  # noqa: BLE001 — surface as tool error, never crash the run
            return f"Error: failed to parse {sb_path} ({ext}): {type(e).__name__}: {e}"
        return f"Error: unsupported extension {ext}"


def make_local_executor(documents_dir: Path, output_dir: Path, workspace_dir: Path,
                        shell_timeout: int = 60) -> tuple[LocalSandbox, LocalToolExecutor]:
    sb = LocalSandbox(documents_dir=documents_dir, output_dir=output_dir,
                      workspace_dir=workspace_dir, default_timeout=shell_timeout)
    sb.start()
    ex = LocalToolExecutor(sandbox=sb, shell_timeout=shell_timeout)
    return sb, ex
