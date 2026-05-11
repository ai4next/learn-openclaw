# s03: Skills System


> **"Skills are portable knowledge packages. Load them on demand."**
>
> Harness layer: Knowledge Injection

## Problem

A general-purpose system prompt gives the agent broad capability but shallow expertise. To handle specialized tasks -- writing documentation in a company's style, debugging a specific framework, following compliance rules -- the agent needs domain knowledge that changes depending on the task.

Hardcoding all possible knowledge into the system prompt would blow past the context window and dilute the agent's focus. The solution is a **skills system**: portable markdown files with YAML frontmatter that the agent can load on demand. Skills live on disk, are discovered at startup, and are injected into the conversation only when needed.

## Solution

```
                   +-----------+
                   | Skills    |
                   | Directory |
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
            | Skill Registry |
            | (dict: name -> Skill) |
            +----------------+
                     |
            +--------+--------+
            |                  |
            v                  v
    +----------------+  +-------------------+
    | get_summary()  |  | load_skill tool   |
    | (always in     |  | (on-demand by     |
    |  system prompt)|  |  model request)   |
    +----------------+  +-------------------+
```

The agent starts with only skill summaries in its system prompt. When it encounters a task that requires specific expertise, it calls the `load_skill` tool to retrieve the full skill body. Always-loaded skills (marked with `always: true`) are injected at startup.

## How It Works

### 1. Define the Skill data model

Each skill has a name, description, body (the full text), and an `always` flag that controls whether it is loaded at startup or on demand.

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str
    always: bool = False
```

### 2. Create skills as markdown files with YAML frontmatter

Skills live in a `skills/` directory. Each skill is a subdirectory containing a `SKILL.md` file with YAML frontmatter:

```markdown
---
name: python-debugging
description: Expert guidance for debugging Python applications
always: false
---

# Python Debugging

## Common Patterns
- Use `pdb.set_trace()` for interactive debugging
- Use `--pdb` with pytest to drop into debugger on failure
...
```

### 3. Discover and parse skills at startup

The `SkillLoader` scans multiple skill directories for `SKILL.md` files and parses their frontmatter. **Later directories override earlier ones** — this lets users customize skills without modifying the builtin set:

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
                        # Later dirs override earlier (workspace > builtin)
                        self._skills[skill.name] = skill
```

This gives you a priority chain:

1. **Workspace skills** (`workspace/skills/`) — highest priority, user-customizable
2. **Builtin skills** (`skills/`) — defaults shipped with the project

### 4. Inject summaries into the system prompt

The loader's `get_summary()` method generates a condensed listing for the system prompt. This gives the agent awareness of what skills exist without consuming token budget on their full content:

```python
def get_summary(self) -> str:
    lines = ["Available skills:"]
    for name, skill in self._skills.items():
        always = " [ALWAYS]" if skill.always else ""
        lines.append(f"  - {name}: {skill.description}{always}")
    return "\n".join(lines)
```

### 5. Add a load_skill tool for on-demand retrieval

A new `LoadSkillTool` lets the agent request full skill bodies when needed. Its parameters are dynamically generated from the list of loadable skill names:

```python
class LoadSkillTool(Tool):
    def __init__(self, loader: SkillLoader):
        self._loader = loader

    @property
    def name(self): return "load_skill"

    @property
    def description(self): return "Load a skill's full content by name"

    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"One of: {', '.join(self._loader.get_loadable_names())}",
                }
            },
            "required": ["name"],
        }

    def execute(self, name=""):
        skill = self._loader.get_skill(name)
        return f"# Skill: {skill.name}\n\n{skill.body}"
```

### 6. Always-loaded skills are injected at startup

Skills with `always: true` are injected directly into the system prompt at startup, so the agent always has that knowledge without needing to request it:

```python
always_skills = loader.get_always_skills()
always_bodies = "\n\n".join(f"# {s.name}\n{s.body}" for s in always_skills)

SYSTEM_PROMPT = f"""You are an OpenClaw agent with skill-based knowledge injection.

{skill_summary}

Use load_skill to load a skill's full content when you need its expertise.
{"Always loaded skills:\n" + always_bodies if always_bodies else ""}"""
```

## What Changed From s03

| Component | Before (s01) | After (s02) |
|-----------|--------------|-------------|
| Knowledge injection | Flat system prompt only | Skill files with YAML frontmatter; summary in prompt, full body loaded on demand |
| Tool set | Fixed: `bash`, `read`, `write` | Added `load_skill` tool for on-demand knowledge retrieval |
| Startup behavior | Static system prompt | `SkillLoader.discover()` scans multiple `skills/` directories |
| Skill priority | N/A | Later directories override earlier (workspace > builtin) |
| Dynamic content | None | Skill summaries and bodies injected via system prompt and tool results |
| Extensibility model | Hardcoded tools | Portable skill files -- anyone can add a skill by creating a SKILL.md |

## Try It

```bash
python chapters/04_skills.py
```

First, create a skill file to load:

```bash
mkdir -p skills/flask-api
cat > skills/flask-api/SKILL.md << 'SKILLEOF'
---
name: flask-api
description: Guidance for building REST APIs with Flask
---

# Flask API Best Practices

## Project Structure
- Use application factories (create_app pattern)
- Organize routes in Blueprints
- Use Flask-Smorest for OpenAPI docs
SKILLEOF
```

Then restart the agent and try:

- "What skills do you have available?"
- "Load the flask-api skill and read it to me."
- "Create a Flask REST API with a health check endpoint and a users resource."

## Skill Lifecycle

| Phase | What Happens |
|-------|-------------|
| **Discovery** | `SkillLoader.discover()` scans all subdirectories of `skills/` for `SKILL.md` |
| **Parsing** | YAML frontmatter is extracted: `name`, `description`, `always` |
| **Registration** | Each `Skill` object is stored in `_skills` dict keyed by name |
| **Summary injection** | `get_summary()` lists all skills in the system prompt with descriptions |
| **Always injection** | Skills with `always: true` have their full body embedded at startup |
| **On-demand loading** | Agent calls `load_skill` tool to retrieve full body of a specific skill |
| **Result feedback** | Skill body returned as `tool_result`, model reads and acts on it |

## Why YAML Frontmatter?

Markdown is the most natural format for knowledge documentation. YAML frontmatter adds structured metadata (name, description, load policy) without requiring a separate database or config file. A skill is a directory you can `cp -r`, `git push`, or share as a tarball. No registration step, no database migration, no config entry.