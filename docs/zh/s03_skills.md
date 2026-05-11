# s03: 技能系统


> **"技能是可移植的知识包。按需加载它们。"**
>
> 底座层：知识注入

## 问题

通用型系统提示赋予智能体广泛的能力，但专业知识浅薄。为了处理专业任务 —— 以公司风格撰写文档、调试特定框架、遵循合规规则 —— 智能体需要随任务变化的领域知识。

将所有可能的知识硬编码到系统提示中会超出上下文窗口的限制，并稀释智能体的专注力。解决方案是**技能系统**：可移植的 markdown 文件，带有 YAML 前置元数据，智能体可以按需加载。技能存储在磁盘上，在启动时被发现，仅在需要时注入到对话中。

## 解决方案

```
                   +-----------+
                   | 技能       |
                   | 目录       |
                   +-----------+
                   |           |
       skill-1/SKILL.md   skill-2/SKILL.md
           |                     |
           v                     v
   +--------------+     +--------------+
   | SkillLoader  |     | SkillLoader  |
   | discover()   |     | discover()   |
   +--------------+     +--------------+
           |                     |
           +---------+-----------+
                     |
                     v
            +----------------+
            | 技能注册表      |
            | (字典: name -> Skill) |
            +----------------+
                     |
            +--------+--------+
            |                  |
            v                  v
    +----------------+  +-------------------+
    | get_summary()  |  | load_skill 工具   |
    | (始终存在于     |  | (由模型请求        |
    |  系统提示中)    |  |  按需加载)         |
    +----------------+  +-------------------+
```

智能体启动时，系统提示中仅包含技能摘要。当遇到需要特定专业知识的任务时，它会调用 `load_skill` 工具来获取完整的技能正文。标记为 `always: true` 的始终加载技能在启动时即被注入。

## 工作原理

### 1. 定义技能数据模型

每个技能拥有名称、描述、正文（完整文本）以及一个控制其是在启动时加载还是按需加载的 `always` 标志。

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str
    always: bool = False
```

### 2. 创建带 YAML 前置元数据的 markdown 技能文件

技能存储在 `skills/` 目录中。每个技能是一个子目录，包含一个带有 YAML 前置元数据的 `SKILL.md` 文件：

```markdown
---
name: python-debugging
description: Python 应用程序调试专家指导
always: false
---

# Python 调试

## 常见模式
- 使用 `pdb.set_trace()` 进行交互式调试
- 使用 `--pdb` 结合 pytest 在失败时进入调试器
...
```

### 3. 在启动时发现并解析技能

`SkillLoader` 扫描多个技能目录以查找 `SKILL.md` 文件，并解析它们的前置元数据。**后面的目录会覆盖前面的** —— 这使用户无需修改内置技能集即可自定义技能：

```python
class SkillLoader:
    def __init__(self, *skills_dirs: Path):
        self.skills_dirs = skills_dirs
        self._skills = {}

    def discover(self):
        for skills_dir in self.skills_dirs:
            if not skills_dir.exists():
                continue
            for skill_dir in sorted(skills_dir.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    skill = self._parse_skill(skill_file)
                    if skill:
                        # 后面的目录覆盖前面的（工作空间 > 内置）
                        self._skills[skill.name] = skill
```

这为您提供了优先级链：

1. **工作空间技能**（`workspace/skills/`）—— 最高优先级，用户可自定义
2. **内置技能**（`skills/`）—— 项目自带的默认技能

### 4. 将摘要注入系统提示

加载器的 `get_summary()` 方法生成一个简洁的列表，用于系统提示。这使智能体知道存在哪些技能，而无需消耗令牌预算来加载它们的完整内容：

```python
def get_summary(self) -> str:
    lines = ["可用技能:"]
    for name, skill in self._skills.items():
        always = " [始终加载]" if skill.always else ""
        lines.append(f"  - {name}: {skill.description}{always}")
    return "\n".join(lines)
```

### 5. 添加 load_skill 工具用于按需检索

`LoadSkillTool` 让智能体可以在需要时请求完整的技能正文。其参数根据可加载技能名称列表动态生成：

```python
class LoadSkillTool(Tool):
    def __init__(self, loader: SkillLoader):
        self._loader = loader

    @property
    def name(self): return "load_skill"

    @property
    def description(self): return "按名称加载技能的完整内容"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"以下之一: {', '.join(self._loader.get_loadable_names())}",
                }
            },
            "required": ["name"],
        }

    def execute(self, name=""):
        skill = self._loader.get_skill(name)
        return f"# 技能: {skill.name}\n\n{skill.body}"
```

### 6. 始终加载的技能在启动时注入

标记为 `always: true` 的技能在启动时直接注入到系统提示中，因此智能体始终拥有该知识，无需另行请求：

```python
always_skills = loader.get_always_skills()
always_bodies = "\n\n".join(f"# {s.name}\n{s.body}" for s in always_skills)

SYSTEM_PROMPT = f"""您是一个基于技能知识注入的 OpenClaw 智能体。

{skill_summary}

当需要某项专业技能时，使用 load_skill 来加载技能的完整内容。
{"始终加载的技能:\n" + always_bodies if always_bodies else ""}"""
```

## 从 s03 的变化

| 组件 | 之前 (s01) | 之后 (s02) |
|-----------|--------------|-------------|
| 知识注入 | 仅平面系统提示 | 带 YAML 前置元数据的技能文件；摘要置于提示中，完整正文按需加载 |
| 工具集 | 固定：`bash`、`read`、`write` | 新增 `load_skill` 工具用于按需知识检索 |
| 启动行为 | 静态系统提示 | `SkillLoader.discover()` 扫描多个 `skills/` 目录 |
| 技能优先级 | 无 | 后面的目录覆盖前面的（工作空间 > 内置） |
| 动态内容 | 无 | 通过系统提示和工具结果注入技能摘要和正文 |
| 可扩展性模型 | 硬编码工具 | 可移植的技能文件 —— 任何人只需创建一个 SKILL.md 即可添加技能 |

## 试试看

```bash
python chapters/04_skills.py
```

首先，创建一个要加载的技能文件：

```bash
mkdir -p skills/flask-api
cat > skills/flask-api/SKILL.md << 'SKILLEOF'
---
name: flask-api
description: 使用 Flask 构建 REST API 的指导
---

# Flask API 最佳实践

## 项目结构
- 使用应用工厂模式（create_app 模式）
- 将路由组织在 Blueprints 中
- 使用 Flask-Smorest 生成 OpenAPI 文档
SKILLEOF
```

然后重启智能体并尝试：

- "你有什么可用技能？"
- "加载 flask-api 技能并读给我听。"
- "创建一个带有健康检查端点和用户资源的 Flask REST API。"

## 技能生命周期

| 阶段 | 发生了什么 |
|-------|-------------|
| **发现** | `SkillLoader.discover()` 扫描 `skills/` 的所有子目录以查找 `SKILL.md` |
| **解析** | 提取 YAML 前置元数据：`name`、`description`、`always` |
| **注册** | 每个 `Skill` 对象存储在 `_skills` 字典中，以名称为键 |
| **摘要注入** | `get_summary()` 在系统提示中列出所有技能及其描述 |
| **始终加载注入** | `always: true` 的技能在启动时将其完整正文嵌入 |
| **按需加载** | 智能体调用 `load_skill` 工具来检索特定技能的完整正文 |
| **结果反馈** | 技能正文作为 `tool_result` 返回，模型读取并据此行动 |

## 为什么使用 YAML 前置元数据？

Markdown 是知识文档最自然的格式。YAML 前置元数据添加了结构化元数据（名称、描述、加载策略），无需单独的数据库或配置文件。一个技能就是一个目录，您可以 `cp -r`、`git push` 或将其作为压缩包分享。无需注册步骤，无需数据库迁移，无需配置条目。