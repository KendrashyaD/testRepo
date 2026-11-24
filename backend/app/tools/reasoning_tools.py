"""Explicit reasoning tools: think (private reasoning step) and update_plan (task checklist)."""
from __future__ import annotations

from typing import Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from .workspace import SCRATCHPAD, thread_id_from

PLAN_KEY = "plan"
MARK = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]", "blocked": "[!]", "skipped": "[-]"}


class ThinkArgs(BaseModel):
    thought: str = Field(..., description="Your reasoning: what you know, what is unclear, options, decision")


class PlanStep(BaseModel):
    step: str
    status: Literal["pending", "in_progress", "done", "blocked", "skipped"] = "pending"


class PlanArgs(BaseModel):
    goal: str = Field(..., description="The user's goal in one sentence, incl. the expected deliverable")
    steps: list[PlanStep] = Field(..., description="Full ordered checklist (send ALL steps every time)")
    notes: str = Field("", description="Open questions, assumptions, success criteria")


async def _think(config: RunnableConfig, thought: str) -> str:
    SCRATCHPAD.write(thread_id_from(config), "thoughts", thought, append=True)
    return "Noted. Continue with the next concrete action."


def render_plan(goal: str, steps: list[PlanStep], notes: str = "") -> str:
    lines = [f"Goal: {goal}"] + [f"{MARK[s.status]} {i + 1}. {s.step}" for i, s in enumerate(steps)]
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


async def _plan(config: RunnableConfig, goal: str, steps: list[PlanStep], notes: str = "") -> str:
    steps = [s if isinstance(s, PlanStep) else PlanStep(**s) for s in steps]
    text = render_plan(goal, steps, notes)
    SCRATCHPAD.write(thread_id_from(config), PLAN_KEY, text)
    left = sum(s.status in ("pending", "in_progress") for s in steps)
    return text + (f"\n{left} step(s) remaining." if left else "\nAll steps complete - verify against the goal, "
                                                                 "then give the final answer.")


def reasoning_tools() -> list[BaseTool]:
    return [
        StructuredTool.from_function(
            coroutine=_think, name="think", args_schema=ThinkArgs,
            description="Think step by step before acting when the task is complex, ambiguous, after a failure, or "
                        "before producing a deliverable. Does not change anything; keep it concise."),
        StructuredTool.from_function(
            coroutine=_plan, name="update_plan", args_schema=PlanArgs,
            description="Create or update the task checklist for multi-step tasks (3+ steps). Mark steps "
                        "in_progress/done as you go. The current plan is always shown to you."),
    ]
