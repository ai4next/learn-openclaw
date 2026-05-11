# s12: Security & Sandbox


> **"Security is not a feature. It is a structural property of the harness."**
>
> Harness layer: Security

## Problem

An agent with bash, read, write, and web access is powerful — and dangerous. Without boundaries, the model might:

- **Read files outside the workspace** (`/etc/passwd`, `~/.ssh/id_rsa`)
- **Hit internal services** (cloud metadata endpoints, internal dashboards)
- **Run destructive commands** (`rm -rf /`, fork bombs)
- **Write to system locations** (`/usr/bin/`, `/etc/`)

These are not malicious acts. The model is following instructions creatively. The harness must enforce boundaries structurally — not by asking the model to be "careful."

## Solution

```
+------------------+
| Path Resolver    |  All filesystem tools go through resolve_path()
+------------------+
| SSRF Protector   |  validate_url_target() blocks private IPs
+------------------+
| Shell Sandbox    |  bwrap wrapping for command isolation
+------------------+
| Error Classifier |  PermissionError vs SystemError — clear signals
+------------------+
```

Three independent security layers, each enforced at the harness level. The model never decides whether an operation is safe — the harness simply refuses unsafe operations with clear, non-negotiable error messages.

## How It Works

### 1. Workspace path restriction

A single `resolve_path()` function enforces that all filesystem access stays within allowed directories. Every read, write, and edit tool goes through this same choke point:

```python
def resolve_path(path: str, workspace: Path, allowed_dirs=None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = workspace / p
    resolved = p.resolve(strict=False)

    for ad in (allowed_dirs or [workspace]):
        try:
            resolved.relative_to(ad.resolve())
            return resolved
        except ValueError:
            continue

    raise PermissionError(
        f"Path '{path}' resolves outside the allowed workspace.\n"
        "This is a hard policy boundary, not a transient failure. "
        "Do not retry with shell commands or alternative paths."
    )
```

Key design: the error message explicitly says "hard policy boundary" to train the model not to retry with tricks.

### 2. SSRF (Server-Side Request Forgery) protection

The `validate_url_target()` function blocks requests to private and reserved IP ranges before any HTTP request is made:

```python
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # cloud metadata
    # ... IPv6 equivalents
]
```

A whitelist mechanism allows specific ranges (e.g., Tailscale's `100.64.0.0/10`) to bypass the block:

```python
configure_ssrf_whitelist(["100.64.0.0/10"])
```

### 3. Shell sandbox

The `ShellSandbox` ABC abstracts command isolation. On Linux, `BubblewrapSandbox` uses `bwrap` to create a minimal filesystem namespace:

```python
class BubblewrapSandbox(ShellSandbox):
    def wrap(self, command, workspace, cwd=None):
        return " ".join([
            "bwrap",
            "--ro-bind", "/usr", "/usr",
            "--proc", "/proc",
            "--bind", str(workspace), str(workspace),
            "--unshare-net",
            "--die-with-parent",
            "--", "sh", "-c", command,
        ])
```

On other platforms, `NoSandbox` passes commands through directly, with workspace restriction as the only guard.

### 4. Security architecture summary

| Layer | What It Prevents | Error to Model |
|-------|-----------------|----------------|
| `resolve_path` | File access outside workspace | `PermissionError` — hard boundary |
| `validate_url_target` | Requests to private/metadata IPs | `Blocked: private IP` — no retry |
| `ShellSandbox` | Escalated shell access | Sandbox wraps the command transparently |

## What Changed From s12

| Component | Before (s11) | After (s12) |
|-----------|--------------|-------------|
| Path resolution | Direct `Path(file_path).read_text()` | `resolve_path()` enforces workspace boundary |
| URL validation | None | `validate_url_target()` blocks private IP ranges |
| Shell safety | Raw `subprocess.run()` | `ShellSandbox` wrap with bwrap on Linux |
| Error messages | Generic "Error: ..." | Typed: `PermissionError` vs `SystemError` with policy note |
| SSRF whitelist | N/A | `configure_ssrf_whitelist()` for Tailscale, VPNs |

## Try It

```bash
python chapters/13_security.py
```

Commands to test:

- `/check http://169.254.169.254/latest/meta-data/` — blocked cloud metadata
- `/check http://192.168.1.1/` — blocked private IP
- `/check https://api.github.com/` — allowed public URL
- `/resolve /etc/passwd` — blocked (outside workspace)
- `/resolve chapters/13_security.py` — allowed (inside workspace)

## Key Design Decisions

1. **Choke point, not distributed checks.** All path resolution goes through one function. This is audit-proof — you verify one function, not every tool.

2. **Hard boundary language.** Permission errors explicitly say "hard policy boundary." This teaches the model not to retry, which prevents infinite loops and adversarial prompt chains.

3. **Sandbox is optional, path restriction is not.** Even without bwrap, `resolve_path` provides a meaningful security boundary. The sandbox is defense-in-depth.

## Reference

This pattern follows the reference implementation's security layer: `resolve_path()` for workspace restriction, `validate_url_target()` for SSRF protection, and sandboxing for shell isolation. The error message format uses a hard-boundary pattern that teaches the model not to retry with alternative approaches.