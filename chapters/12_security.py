#!/usr/bin/env python3
"""Harness layer: Security & Sandbox

    +------------------+
    | Path Resolver    |  workspace enforcement (_resolve_path)
    +------------------+
    | SSRF Protector   |  IP range blocking + whitelist
    +------------------+
    | Shell Sandbox    |  bwrap / command wrapping
    +------------------+
    | Error Classifier |  PermissionError vs SystemError
    +------------------+

Key insight: Security is not a feature. It is a structural property
of the harness. Every tool, every provider, every channel must
respect the same boundaries. The model should never be able to
escape the workspace, hit internal services, or run destructive
commands — not because it's malicious, but because it's creative.
"""

import ipaddress
import json
import os
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")
PROMPT_COLOR = "\033[36ms12 >> \033[0m"


# ---------------------------------------------------------------------------
# Workspace path restriction
# ---------------------------------------------------------------------------
def resolve_path(path: str, workspace: Path, allowed_dirs: list[Path] = None) -> Path:
    """Resolve a path and enforce it stays within allowed directories.

    This is the single choke point for all filesystem tools.
    Every read/write/edit tool must go through this function.

    The model receives a clear permission-denied message that explicitly
    says "this is a hard policy boundary" — teaching it not to retry
    with shell tricks or alternative paths.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace / p

    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = p.absolute()

    allowed = allowed_dirs or [workspace]
    for ad in allowed:
        try:
            resolved.relative_to(ad.resolve())
            return resolved
        except ValueError:
            continue

    raise PermissionError(
        f"Path '{path}' resolves outside the allowed workspace.\n"
        "This is a hard policy boundary, not a transient failure. "
        "Do not retry with shell commands or alternative paths. "
        "Ask the user to specify an allowed path."
    )


# ---------------------------------------------------------------------------
# SSRF Protection
# ---------------------------------------------------------------------------
BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

SSRF_WHITELIST: list = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges (e.g. Tailscale 100.64.0.0/10)."""
    global SSRF_WHITELIST
    SSRF_WHITELIST = []
    for cidr in cidrs:
        try:
            SSRF_WHITELIST.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            pass


def validate_url_target(url: str) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    Returns (ok, error_message).
    """
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e)

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
    if not p.netloc:
        return False, "Missing domain"

    hostname = p.hostname
    if not hostname:
        return False, "Missing hostname"

    # Block cloud metadata endpoints
    if hostname == "169.254.169.254":
        return False, "Blocked: cloud metadata endpoint"

    # Block by resolved IP
    import socket
    try:
        addrs = socket.getaddrinfo(hostname, 80)
        for family, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip in SSRF_WHITELIST:
                continue
            for net in BLOCKED_NETWORKS:
                if ip in net:
                    return False, f"Blocked: {ip} is in a private/reserved range ({net})"
    except OSError:
        return False, f"Could not resolve hostname: {hostname}"

    return True, ""


# ---------------------------------------------------------------------------
# Shell sandbox
# ---------------------------------------------------------------------------
class ShellSandbox(ABC):
    """Abstract base for shell sandbox backends."""

    @abstractmethod
    def wrap(self, command: str, workspace: Path, cwd: Path = None) -> str:
        """Wrap a command string with sandbox restrictions."""
        ...


class BubblewrapSandbox(ShellSandbox):
    """Sandbox using bubblewrap (bwrap). Available on Linux."""

    def wrap(self, command: str, workspace: Path, cwd: Path = None) -> str:
        cmd_parts = [
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--proc", "/proc",
            "--dev", "/dev",
            "--bind", str(workspace), str(workspace),
            "--chdir", str(cwd or workspace),
            "--unshare-net",
            "--die-with-parent",
            "--", "sh", "-c", command,
        ]
        return " ".join(cmd_parts)


class NoSandbox(ShellSandbox):
    """No sandbox — commands run directly. Workspace restriction still applies."""

    def wrap(self, command: str, workspace: Path, cwd: Path = None) -> str:
        return command


def create_sandbox() -> ShellSandbox:
    """Auto-detect the best available sandbox."""
    if sys.platform == "linux":
        try:
            subprocess.run(["bwrap", "--version"], capture_output=True, timeout=5)
            return BubblewrapSandbox()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return NoSandbox()


# ---------------------------------------------------------------------------
# Secure tools
# ---------------------------------------------------------------------------
class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, tool):
        self._tools[tool.name] = tool

    def list_schemas(self):
        return [t.to_api_schema() for t in self._tools.values()]

    def execute(self, name, **kwargs):
        tool = self._tools.get(name)
        if not tool:
            return f"Error: unknown tool '{name}'"
        return tool.execute(**kwargs)


class Tool:
    def __init__(self, name, description, input_schema, execute):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.execute = execute

    def to_api_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def _make_secure_bash_tool(sandbox: ShellSandbox, workspace: Path):
    def execute(**kwargs):
        command = kwargs.get("command", "")
        if not command:
            return "No command provided."
        wrapped = sandbox.wrap(command, workspace)
        try:
            result = subprocess.run(
                wrapped,
                shell=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = result.stdout or ""
            if result.stderr:
                output += "\n[stderr]\n" + result.stderr
            if result.returncode != 0:
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out (60s)."
        except Exception as e:
            return f"Error: {e}"

    return Tool(
        name="bash",
        description="Execute a shell command within sandbox restrictions.",
        input_schema={
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The bash command to run."}},
            "required": ["command"],
        },
        execute=execute,
    )


def _make_secure_read_tool(workspace: Path):
    def execute(**kwargs):
        file_path = kwargs.get("file_path", "")
        if not file_path:
            return "No path provided."
        try:
            resolved = resolve_path(file_path, workspace)
            return resolved.read_text(encoding="utf-8")
        except PermissionError as e:
            return str(e)
        except Exception as e:
            return f"Error: {e}"

    return Tool(
        name="read",
        description="Read a file from the workspace.",
        input_schema={
            "type": "object",
            "properties": {"file_path": {"type": "string", "description": "Path to the file (absolute or relative to workspace)."}},
            "required": ["file_path"],
        },
        execute=execute,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def repl():
    workspace = WORKDIR
    sandbox = create_sandbox()
    sandbox_type = type(sandbox).__name__.replace("Sandbox", "")

    registry = ToolRegistry()
    registry.register(_make_secure_bash_tool(sandbox, workspace))
    registry.register(_make_secure_read_tool(workspace))

    print("=== OpenClaw s12: Security & Sandbox ===")
    print(f"Workspace: {workspace}")
    print(f"Sandbox: {sandbox_type}")
    print(f"SSRF blocked networks: {len(BLOCKED_NETWORKS)} ranges")
    print("Commands:  /check <url>  (test SSRF validation)")
    print("           /resolve <path>  (test path resolution)")
    print("           q  (quit)\n")

    while True:
        try:
            user_input = input(PROMPT_COLOR)
        except (EOFError, KeyboardInterrupt):
            break

        raw = user_input.strip()

        if raw.lower() in ("q", "quit", "exit"):
            break

        if raw.startswith("/check "):
            url = raw[7:].strip()
            ok, err = validate_url_target(url)
            print(f"  URL: {url}")
            print(f"  Safe: {ok}")
            if err:
                print(f"  Reason: {err}")
            continue

        if raw.startswith("/resolve "):
            test_path = raw[9:].strip()
            try:
                resolved = resolve_path(test_path, workspace)
                print(f"  Path: {test_path}")
                print(f"  Resolved: {resolved}")
            except PermissionError as e:
                print(f"  Blocked: {e}")
            continue

        print(f"  Unknown command. Try /check <url> or /resolve <path>")


if __name__ == "__main__":
    repl()