"""Agent definitions, registry, and persistence."""

import json
from dataclasses import dataclass, asdict
from pathlib import Path

CONFIG_DIR = Path.home() / ".hivemind"
AGENTS_FILE = CONFIG_DIR / "agents.json"

AGENT_COLORS = [
    "bright_magenta",   # 0: reserved for orchestrator
    "bright_cyan",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_red",
    "bright_white",
    "magenta",
    "cyan",
    "green",
]

ORCHESTRATOR_SYSTEM_PROMPT = """\
You are the Orchestrator agent for Hivemind. Your role is to decompose user tasks \
into subtasks and assign them to specialist agents.

When given a task, you will also receive a list of available agents with their \
specialties. You must output a JSON execution plan.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
    "plan": [
        {
            "id": "task_1",
            "agent": "agent_name",
            "task": "Description of what this agent should do",
            "depends_on": []
        },
        {
            "id": "task_2",
            "agent": "another_agent",
            "task": "Description that builds on task_1 results",
            "depends_on": ["task_1"]
        }
    ]
}

Rules:
- Each task has a unique string id (task_1, task_2, etc.)
- "agent" must be one of the available agent names listed below
- "depends_on" is a list of task ids that must complete first. Empty means no dependencies (runs immediately)
- Tasks with no dependencies run in parallel
- Tasks with dependencies receive those results as context
- Use 2-6 subtasks typically
- Output ONLY the JSON object, nothing else"""


@dataclass
class Agent:
    name: str
    description: str
    provider: str       # "ollama" | "anthropic" | "openai"
    model: str
    color: str = ""
    is_orchestrator: bool = False

    def system_prompt(self) -> str:
        if self.is_orchestrator:
            return ORCHESTRATOR_SYSTEM_PROMPT
        return self.description


class AgentRegistry:
    def __init__(self):
        self.agents: dict[str, Agent] = {}
        self._color_idx = 1
        self._ensure_config_dir()
        self._load()
        self._ensure_orchestrator()

    def _ensure_config_dir(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def _ensure_orchestrator(self):
        if "orchestrator" not in self.agents:
            self.agents["orchestrator"] = Agent(
                name="orchestrator",
                description="Task decomposition and result synthesis",
                provider="ollama",
                model="llama3.2",
                color=AGENT_COLORS[0],
                is_orchestrator=True,
            )
            self._save()

    def _load(self):
        if AGENTS_FILE.exists():
            data = json.loads(AGENTS_FILE.read_text())
            for d in data:
                agent = Agent(**d)
                self.agents[agent.name] = agent
                if agent.color in AGENT_COLORS:
                    idx = AGENT_COLORS.index(agent.color)
                    if idx >= self._color_idx:
                        self._color_idx = idx + 1

    def _save(self):
        data = [asdict(a) for a in self.agents.values()]
        AGENTS_FILE.write_text(json.dumps(data, indent=2))

    def _next_color(self) -> str:
        color = AGENT_COLORS[self._color_idx % len(AGENT_COLORS)]
        self._color_idx += 1
        return color

    def create(self, name: str, description: str, provider: str, model: str) -> Agent:
        if name == "orchestrator":
            raise ValueError("Cannot create agent named 'orchestrator'")
        if name in self.agents:
            raise ValueError(f"Agent '{name}' already exists")
        agent = Agent(
            name=name,
            description=description,
            provider=provider,
            model=model,
            color=self._next_color(),
        )
        self.agents[name] = agent
        self._save()
        return agent

    def delete(self, name: str) -> None:
        if name == "orchestrator":
            raise ValueError("Cannot delete the orchestrator agent")
        if name not in self.agents:
            raise ValueError(f"Agent '{name}' not found")
        del self.agents[name]
        self._save()

    def get(self, name: str) -> Agent | None:
        return self.agents.get(name)

    def list_agents(self) -> list[Agent]:
        orch = self.agents.get("orchestrator")
        others = sorted(
            [a for a in self.agents.values() if not a.is_orchestrator],
            key=lambda a: a.name,
        )
        return ([orch] if orch else []) + others

    def available_for_swarm(self) -> list[Agent]:
        return [a for a in self.agents.values() if not a.is_orchestrator]

    def create_from_template(self, template_name: str, provider: str, model: str) -> "Agent":
        """Create an agent from a predefined template."""
        tmpl = AGENT_TEMPLATES.get(template_name)
        if not tmpl:
            raise ValueError(f"Unknown template: '{template_name}'. "
                             f"Available: {', '.join(AGENT_TEMPLATES)}")
        return self.create(
            name=tmpl["name"],
            description=tmpl["description"],
            provider=provider,
            model=model,
        )


# ═══ Agent Templates ════════════════════════════════════════════════════════

AGENT_TEMPLATES = {
    "coder": {
        "name": "coder",
        "description": (
            "You are an expert software engineer. Write clean, efficient, "
            "well-documented code. Follow best practices and common conventions. "
            "Always consider edge cases, error handling, and security. "
            "Explain your design decisions briefly."
        ),
    },
    "reviewer": {
        "name": "reviewer",
        "description": (
            "You are a senior code reviewer. Analyze code for bugs, security "
            "vulnerabilities, performance issues, and style violations. "
            "Suggest specific improvements with code examples. Be thorough "
            "but constructive. Prioritize security and correctness over style."
        ),
    },
    "researcher": {
        "name": "researcher",
        "description": (
            "You are a technical researcher and analyst. Gather, organize, "
            "and synthesize information clearly. Compare alternatives with "
            "pros and cons. Cite sources when possible. Focus on accuracy "
            "and objectivity. Present findings in a structured format."
        ),
    },
    "writer": {
        "name": "writer",
        "description": (
            "You are a technical writer. Produce clear, well-structured "
            "documentation, tutorials, and explanations. Use appropriate "
            "formatting (headings, lists, code blocks). Write for the target "
            "audience's skill level. Be concise but thorough."
        ),
    },
}
