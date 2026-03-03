"""Swarm orchestrator, dependency-graph task runner, and demo mode."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from .agents import AgentRegistry
from .providers import get_provider


class TaskStatus(Enum):
    PENDING = "pending"
    THINKING = "thinking"
    RESPONDING = "responding"
    DONE = "done"
    ERROR = "error"


@dataclass
class SubTask:
    id: str
    agent_name: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str = ""


@dataclass
class SwarmPlan:
    subtasks: list[SubTask]

    @classmethod
    def from_json(cls, raw: str) -> "SwarmPlan":
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        tasks = []
        for item in data["plan"]:
            tasks.append(SubTask(
                id=item["id"],
                agent_name=item["agent"],
                task=item["task"],
                depends_on=item.get("depends_on", []),
            ))
        return cls(subtasks=tasks)

    def validate(self, available_agents: list[str]) -> list[str]:
        errors = []
        task_ids = {t.id for t in self.subtasks}
        for t in self.subtasks:
            if t.agent_name not in available_agents:
                errors.append(f"Task {t.id} references unknown agent '{t.agent_name}'")
            for dep in t.depends_on:
                if dep not in task_ids:
                    errors.append(f"Task {t.id} depends on unknown task '{dep}'")
        # Cycle detection via topological sort
        visited: set[str] = set()
        in_stack: set[str] = set()
        task_map = {t.id: t for t in self.subtasks}

        def has_cycle(tid: str) -> bool:
            if tid in in_stack:
                return True
            if tid in visited:
                return False
            visited.add(tid)
            in_stack.add(tid)
            for dep in task_map.get(tid, SubTask(id="", agent_name="", task="")).depends_on:
                if has_cycle(dep):
                    return True
            in_stack.discard(tid)
            return False

        for t in self.subtasks:
            if has_cycle(t.id):
                errors.append("Cycle detected in task dependencies")
                break
        return errors


# Callback type: (task_id, agent_name, status, detail) -> None
StatusCallback = Callable[[str, str, TaskStatus, str], Awaitable[None]]


class SwarmRunner:
    def __init__(self, registry: AgentRegistry, on_status: StatusCallback | None = None):
        self.registry = registry
        self.on_status = on_status

    async def _notify(self, task_id: str, agent: str, status: TaskStatus, detail: str = ""):
        if self.on_status:
            await self.on_status(task_id, agent, status, detail)

    async def plan(self, user_task: str) -> SwarmPlan:
        orchestrator = self.registry.get("orchestrator")
        provider = get_provider(orchestrator.provider)

        available = self.registry.available_for_swarm()
        agent_list = "\n".join(
            f"- {a.name}: {a.description} (provider: {a.provider}, model: {a.model})"
            for a in available
        )
        user_message = f"Available agents:\n{agent_list}\n\nUser task: {user_task}"

        await self._notify("planning", "orchestrator", TaskStatus.THINKING)
        full_response = ""
        async for token in provider.chat_stream(
            messages=[{"role": "user", "content": user_message}],
            model=orchestrator.model,
            system_prompt=orchestrator.system_prompt(),
        ):
            full_response += token
        await self._notify("planning", "orchestrator", TaskStatus.DONE, full_response)

        plan = SwarmPlan.from_json(full_response)
        available_names = [a.name for a in available]
        errors = plan.validate(available_names)
        if errors:
            raise ValueError(f"Invalid plan: {'; '.join(errors)}")
        return plan

    async def _run_subtask(self, subtask: SubTask, dep_results: dict[str, str]) -> str:
        agent = self.registry.get(subtask.agent_name)
        if not agent:
            raise ValueError(f"Agent '{subtask.agent_name}' not found")

        provider = get_provider(agent.provider)

        context_parts = []
        if dep_results:
            context_parts.append("Context from prior tasks:")
            for dep_id, dep_text in dep_results.items():
                context_parts.append(f"\n--- Result from {dep_id} ---\n{dep_text}")
            context_parts.append("\n--- End of context ---\n")

        full_prompt = "\n".join(context_parts) + "\n" + subtask.task if context_parts else subtask.task

        subtask.status = TaskStatus.THINKING
        await self._notify(subtask.id, subtask.agent_name, TaskStatus.THINKING)

        full_response = ""
        async for token in provider.chat_stream(
            messages=[{"role": "user", "content": full_prompt}],
            model=agent.model,
            system_prompt=agent.system_prompt(),
        ):
            if not full_response:
                subtask.status = TaskStatus.RESPONDING
                await self._notify(subtask.id, subtask.agent_name, TaskStatus.RESPONDING)
            full_response += token

        subtask.status = TaskStatus.DONE
        subtask.result = full_response
        await self._notify(subtask.id, subtask.agent_name, TaskStatus.DONE, full_response)
        return full_response

    async def execute(self, plan: SwarmPlan) -> dict[str, str]:
        results: dict[str, str] = {}
        events: dict[str, asyncio.Event] = {t.id: asyncio.Event() for t in plan.subtasks}

        async def run_with_deps(subtask: SubTask):
            try:
                for dep_id in subtask.depends_on:
                    await events[dep_id].wait()
                dep_results = {
                    dep_id: results[dep_id]
                    for dep_id in subtask.depends_on
                    if dep_id in results
                }
                result = await self._run_subtask(subtask, dep_results)
                results[subtask.id] = result
            except Exception as e:
                subtask.status = TaskStatus.ERROR
                subtask.error = str(e)
                results[subtask.id] = f"[ERROR: {e}]"
                await self._notify(subtask.id, subtask.agent_name, TaskStatus.ERROR, str(e))
            finally:
                events[subtask.id].set()

        await asyncio.gather(*(run_with_deps(t) for t in plan.subtasks))
        return results

    def _build_synthesis_prompt(self, user_task: str, results: dict[str, str],
                                plan: SwarmPlan) -> tuple[str, str]:
        """Build the synthesis prompt and system message."""
        results_text = ""
        for subtask in plan.subtasks:
            results_text += f"\n--- {subtask.id} ({subtask.agent_name}): {subtask.task} ---\n"
            results_text += results.get(subtask.id, "[no result]")
            results_text += "\n"

        synthesis_prompt = (
            f"Original user task: {user_task}\n\n"
            f"Subtask results:\n{results_text}\n\n"
            f"Synthesize these results into a single, coherent, well-structured response. "
            f"Do not mention the agents or internal process."
        )
        synthesis_system = (
            "You are the Orchestrator synthesizing results from multiple specialist agents. "
            "Produce a clear, unified response."
        )
        return synthesis_prompt, synthesis_system

    async def synthesize(self, user_task: str, results: dict[str, str], plan: SwarmPlan) -> str:
        """Non-streaming synthesis (collects full response)."""
        orchestrator = self.registry.get("orchestrator")
        provider = get_provider(orchestrator.provider)
        prompt, system = self._build_synthesis_prompt(user_task, results, plan)

        await self._notify("synthesis", "orchestrator", TaskStatus.THINKING)
        full_response = ""
        async for token in provider.chat_stream(
            messages=[{"role": "user", "content": prompt}],
            model=orchestrator.model,
            system_prompt=system,
        ):
            full_response += token
        await self._notify("synthesis", "orchestrator", TaskStatus.DONE, full_response)
        return full_response

    async def synthesize_stream(self, user_task: str, results: dict[str, str],
                                plan: SwarmPlan):
        """Streaming synthesis — yields tokens as they arrive."""
        orchestrator = self.registry.get("orchestrator")
        provider = get_provider(orchestrator.provider)
        prompt, system = self._build_synthesis_prompt(user_task, results, plan)

        await self._notify("synthesis", "orchestrator", TaskStatus.THINKING)
        async for token in provider.chat_stream(
            messages=[{"role": "user", "content": prompt}],
            model=orchestrator.model,
            system_prompt=system,
        ):
            yield token
        await self._notify("synthesis", "orchestrator", TaskStatus.DONE, "")

    async def run_swarm(self, user_task: str) -> tuple[SwarmPlan, dict[str, str], str]:
        plan = await self.plan(user_task)
        results = await self.execute(plan)
        synthesis = await self.synthesize(user_task, results, plan)
        return plan, results, synthesis


# ═══ Demo Mode ═══════════════════════════════════════════════════════════════

async def demo_swarm(
    user_task: str,
    registry: AgentRegistry,
    on_status: StatusCallback | None = None,
) -> tuple[SwarmPlan, dict[str, str], str]:
    available = registry.available_for_swarm()
    if not available:
        names = ["assistant"]
    else:
        names = [a.name for a in available]

    # Build demo plan
    subtasks = []
    for i, name in enumerate(names[:3]):
        subtasks.append(SubTask(
            id=f"task_{i+1}",
            agent_name=name,
            task=f"Analyze aspect {i+1} of: {user_task}",
        ))
    if len(subtasks) >= 2:
        subtasks.append(SubTask(
            id=f"task_{len(subtasks)+1}",
            agent_name=names[0],
            task="Combine findings from previous analyses",
            depends_on=[t.id for t in subtasks],
        ))

    plan = SwarmPlan(subtasks=subtasks)
    results = {}

    for t in plan.subtasks:
        if on_status:
            await on_status(t.id, t.agent_name, TaskStatus.THINKING, "")
        await asyncio.sleep(0.4)
        if on_status:
            await on_status(t.id, t.agent_name, TaskStatus.RESPONDING, "")
        await asyncio.sleep(0.3)
        result = f"**Demo result from {t.agent_name}** for: {t.task}\n\nThis is simulated output."
        results[t.id] = result
        t.status = TaskStatus.DONE
        t.result = result
        if on_status:
            await on_status(t.id, t.agent_name, TaskStatus.DONE, result)

    synthesis = (
        f"**Swarm synthesis (demo mode)**\n\n"
        f"Task: *{user_task}*\n\n"
        f"Results from {len(subtasks)} subtasks have been combined.\n\n"
        f"In production, this would contain a coherent merged response from all agents."
    )
    return plan, results, synthesis
