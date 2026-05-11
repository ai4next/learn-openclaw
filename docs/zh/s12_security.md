# s12: 安全与沙箱


> **"安全不是一个特性。它是控制框架的结构性属性。"**
>
> 控制框架层：安全

## 问题

一个具备 bash、读取、写入和网络访问权限的智能体功能强大——但也很危险。如果没有边界，模型可能会：

- **读取工作区之外的文件**（`/etc/passwd`、`~/.ssh/id_rsa`）
- **访问内部服务**（云元数据端点、内部仪表板）
- **执行破坏性命令**（`rm -rf /`、fork 炸弹）
- **写入系统路径**（`/usr/bin/`、`/etc/`）

这些并非恶意行为。模型只是在创造性地遵循指令。控制框架必须以结构性方式强制实施边界——而不是要求模型"小心"。

## 解决方案

```
+------------------+
| 路径解析器        | 所有文件系统工具都经过 resolve_path()
+------------------+
| SSRF 防护器       | validate_url_target() 阻止私有 IP
+------------------+
| Shell 沙箱        | 用于命令隔离的 bwrap 包装
+------------------+
| 错误分类器        | PermissionError 与 SystemError —— 清晰的信号
+------------------+
```

三个独立的安全层，每一层都在控制框架层面强制实施。模型从不决定某个操作是否安全——控制框架直接拒绝不安全操作，并给出明确、不可商量的错误消息。

## 工作原理

### 1. 工作区路径限制

一个统一的 `resolve_path()` 函数确保所有文件系统访问都限制在允许的目录内。每个读取、写入和编辑工具都经过同一个检查点：

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

关键设计：错误消息明确说明"硬策略边界"，以训练模型不要尝试用技巧重试。

### 2. SSRF（服务端请求伪造）防护

`validate_url_target()` 函数在任何 HTTP 请求发出之前，阻止对私有和保留 IP 范围的请求：

```python
BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 云元数据
    # ... IPv6 等价地址
]
```

白名单机制允许特定范围（例如 Tailscale 的 `100.64.0.0/10`）绕过阻止：

```python
configure_ssrf_whitelist(["100.64.0.0/10"])
```

### 3. Shell 沙箱

`ShellSandbox` ABC 抽象了命令隔离。在 Linux 上，`BubblewrapSandbox` 使用 `bwrap` 创建一个最小的文件系统命名空间：

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

在其他平台上，`NoSandbox` 直接透传命令，仅以工作区限制作为唯一的防护手段。

### 4. 安全架构总结

| 层 | 防止的内容 | 返回给模型的错误 |
|-------|-----------------|----------------|
| `resolve_path` | 访问工作区之外的文件 | `PermissionError` —— 硬边界 |
| `validate_url_target` | 请求私有/元数据 IP | `Blocked: private IP` —— 不重试 |
| `ShellSandbox` | 权限提升的 shell 访问 | 沙箱透明地包装命令 |

## s12 相较于 s11 的变化

| 组件 | 之前（s11） | 之后（s12） |
|-----------|--------------|-------------|
| 路径解析 | 直接 `Path(file_path).read_text()` | `resolve_path()` 强制工作区边界 |
| URL 验证 | 无 | `validate_url_target()` 阻止私有 IP 范围 |
| Shell 安全 | 原始 `subprocess.run()` | `ShellSandbox` 在 Linux 上使用 bwrap 包装 |
| 错误消息 | 通用 "Error: ..." | 带类型：`PermissionError` 与 `SystemError` 附策略说明 |
| SSRF 白名单 | 无 | `configure_ssrf_whitelist()` 用于 Tailscale、VPN |

## 试试看

```bash
python chapters/13_security.py
```

可测试的命令：

- `/check http://169.254.169.254/latest/meta-data/` —— 被阻止的云元数据
- `/check http://192.168.1.1/` —— 被阻止的私有 IP
- `/check https://api.github.com/` —— 允许的公开 URL
- `/resolve /etc/passwd` —— 被阻止（在工作区之外）
- `/resolve chapters/13_security.py` —— 允许（在工作区之内）

## 关键设计决策

1. **集中检查点，而非分布式检查。** 所有路径解析都经过一个函数。这便于审计——只需验证一个函数，而非每个工具。

2. **硬边界语言。** 权限错误明确说明"硬策略边界"。这教导模型不要重试，从而防止无限循环和对抗性提示链。

3. **沙箱是可选的，路径限制不可选。** 即使没有 bwrap，`resolve_path` 也提供了有意义的安全边界。沙箱是纵深防御。

## 参考

该模式遵循参考实现的安全层：`resolve_path()`（工作区限制）、`validate_url_target()`（SSRF 防护）和沙箱机制（shell 隔离）。错误消息格式使用了硬边界模式，教导模型不要尝试用其他方式重试。