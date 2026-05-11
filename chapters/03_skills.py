#!/usr/bin/env python3
"""Harness layer: Skills System

    +-----------+
    | Skills    |  skill-1/SKILL.md
    | Loader    |  skill-2/SKILL.md
    +-----------+  ...
         |
    +-----------+
    | System    |  "Available skills: ..."
    | Prompt    |  (injected as tool_result)
    +-----------+

Key insight: Skills are YAML-frontmatter markdown files loaded
on demand. The agent sees a summary in the system prompt and can
request full skill bodies when needed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

WORKDIR = Path(__file__).parent.resolve()
MODEL = os.getenv("MODEL_ID", "claude-sonnet-4-6")

from anthropic import Anthropic
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── Skill System ──────────────────────────────────────────────────────

@dataclass
class Skill:
    name: str
    description: str
    body: str
    always: bool = False


class SkillLoader:
    """Discovers and loads skills from skill directories.

    Supports multiple directories with priority:
    - Later directories override earlier ones (workspace > builtin).
    - This lets users customize skills without modifying the builtin set.
    """

    def __init__(self, *skills_dirs: Path):
        self.skills_dirs = skills_dirs
        self._skills: dict[str, Skill] = {}

    def discover(self):
        """Scan all skills_dirs for SKILL.md files. Later dirs override earlier."""
        self._skills = {}
        for skills_dir in self.skills_dirs:
            if not skills_dir.exists():
                continue
            for skill_dir in sorted(skills_dir.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if skill_file.exists():
                    skill = self._parse_skill(skill_file)
                    if skill:
                        # Later directories override earlier (workspace overrides builtin)
                        self._skills[skill.name] = skill
        return self._skills

    def _parse_skill(self, path: Path) -> Skill | None:
        content = path.read_text(encoding="utf-8")
        # Parse YAML frontmatter
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        frontmatter = parts[1]
        body = parts[2].strip()
        name = self._extract(frontmatter, "name")
        description = self._extract(frontmatter, "description")
        always = self._extract(frontmatter, "always", "false").lower() == "true"
        if not name:
            name = path.parent.name
        return Skill(name=name, description=description or "", body=body, always=always)

    @staticmethod
    def _extract(frontmatter: str, key: str, default: str = "") -> str:
        for line in frontmatter.splitlines():
            if line.strip().startswith(f"{key}:"):
                return line.split(":", 1)[1].strip().strip('"').strip("'")
        return default

    def get_summary(self) -> str:
        """Return a condensed summary for the system prompt."""
        if not self._skills:
            return "No skills available."
        lines = ["Available skills:"]
        for name, skill in self._skills.items():
            always = " [ALWAYS]" if skill.always else ""
            lines.append(f"  - {name}: {skill.description}{always}")
        return "\n".join(lines)

    def get_skill(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def get_always_skills(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.always]

    def get_loadable_names(self) -> list[str]:
        return [n for n, s in self._skills.items() if not s.always]


# ── Tools (same as s02) ───────────────────────────────────────────────

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
    @property
    def name(self): raise NotImplementedError
    @property
    def description(self): raise NotImplementedError
    @property
    def parameters(self): raise NotImplementedError
    def to_api_schema(self):
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}
    def execute(self, **kwargs): raise NotImplementedError

class BashTool(Tool):
    name = "bash"
    description = "Execute a shell command"
    parameters = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
    def execute(self, command=""):
        try:
            r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            out = r.stdout or ""
            if r.stderr: out += f"\nSTDERR:\n{r.stderr}"
            if r.returncode != 0: out += f"\nExit code: {r.returncode}"
            return out or "(no output)"
        except Exception as e: return f"Error: {e}"

class ReadTool(Tool):
    name = "read"
    description = "Read a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}
    def execute(self, file_path=""):
        try: return Path(file_path).resolve().read_text(encoding="utf-8")
        except Exception as e: return f"Error: {e}"

class WriteTool(Tool):
    name = "write"
    description = "Write content to a file"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path", "content"]}
    def execute(self, file_path="", content=""):
        try:
            p = Path(file_path).resolve(); p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8"); return f"Written {len(content)} bytes"
        except Exception as e: return f"Error: {e}"

class LoadSkillTool(Tool):
    """Tool that lets the agent load a skill by name."""
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
        if not skill:
            return f"Error: unknown skill '{name}'. Available: {', '.join(self._loader.get_loadable_names())}"
        return f"# Skill: {skill.name}\n\n{skill.body}"


# ── Setup ─────────────────────────────────────────────────────────────

builtin_skills_dir = WORKDIR / "skills"
workspace_skills_dir = WORKDIR / "workspace" / "skills"
builtin_skills_dir.mkdir(exist_ok=True)
workspace_skills_dir.mkdir(parents=True, exist_ok=True)

loader = SkillLoader(builtin_skills_dir, workspace_skills_dir)
loader.discover()

registry = ToolRegistry()
for t in [BashTool(), ReadTool(), WriteTool(), LoadSkillTool(loader)]:
    registry.register(t)

always_skills = loader.get_always_skills()
always_bodies = "\n\n".join(f"# {s.name}\n{s.body}" for s in always_skills)
skill_summary = loader.get_summary()

SYSTEM_PROMPT = f"""You are an OpenClaw agent with skill-based knowledge injection.

{skill_summary}

Use load_skill to load a skill's full content when you need its expertise.
{"Always loaded skills:\n" + always_bodies if always_bodies else ""}"""


# ── Agent Loop ────────────────────────────────────────────────────────

def agent_loop(messages):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM_PROMPT,
            messages=messages,
            max_tokens=4096,
            tools=registry.list_schemas(),
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "end_turn":
            break
        if response.stop_reason == "tool_use":
            for block in response.content:
                if block.type == "tool_use":
                    result = registry.execute(block.name, **block.input)
                    messages.append({
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": block.id, "content": result}],
                    })
    return response.content


def repl():
    messages = []
    print("=== OpenClaw s03: Skills System ===")
    print(f"Skills directories: {builtin_skills_dir}, {workspace_skills_dir}")
    print("Type 'q' to quit. Create SKILL.md files in skills/ folders.\n")

    while True:
        try:
            user_input = input("\033[36ms03 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.lower() in ("q", "quit", "exit"):
            break
        messages.append({"role": "user", "content": user_input})
        content = agent_loop(messages)
        for block in content:
            if block.type == "text":
                print(block.text)


if __name__ == "__main__":
    repl()