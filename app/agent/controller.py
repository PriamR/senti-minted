"""The agent controller: a bounded, grounded tool-calling loop over DeepSeek.

Sends the question + tool schemas, executes whatever tools the model calls,
feeds results back, and repeats until the model answers or the step cap is hit.
Returns the answer plus full provenance (every tool call + result), so any claim
is auditable back to a deterministic tool output.
"""
from __future__ import annotations

import json
from typing import Any

from ..clients import deepseek
from ..config import settings
from . import tools

MAX_STEPS = 8

SYSTEM_PROMPT = """You are the query agent for a retrospective product-sentiment tool. You answer \
questions about how a product's public reception moved over time, using ONLY the provided tools.

Rules:
- Call resolve_product FIRST to get the appid, launch_date, and confidence.
- For ANY time phrase ("2 years in", "at launch", "recently"), call resolve_timeframe with the \
launch_date — never compute dates yourself.
- Use get_approval_curve (free) for approval level and trend, and get_events for what happened.
- Call analyze_window ONLY when the user needs the WHY (drivers / how divided opinion was). It is \
expensive and only works for recent windows; if it returns available:false, rely on the approval \
curve numbers and say the driver-level detail needs the historical backfill.
- GROUNDING: state only numbers returned by tools. Never invent or interpolate a figure. If a tool \
returns nothing useful, say so plainly.
- Keep it tight: end with a clear, specific answer. State any assumption you made (e.g. how you \
interpreted the timeframe). Express approval as percentages.
"""


async def run_agent(question: str, *, verbose: bool = False) -> dict[str, Any]:
    if not settings.deepseek_api_key:
        return {"answer": "DEEPSEEK_API_KEY not set.", "provenance": [], "steps": 0}

    client = deepseek.client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    provenance: list[dict[str, Any]] = []

    for step in range(MAX_STEPS):
        resp = await client.chat.completions.create(
            model=settings.deepseek_chat_model,
            messages=messages,
            tools=tools.TOOLS,
            tool_choice="auto",
            temperature=0.0,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return {"answer": msg.content or "", "provenance": provenance, "steps": step}

        # record the assistant turn (with its tool calls) before the results
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            try:
                result = await tools.dispatch(name, args)
            except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            provenance.append({"step": step, "tool": name, "args": args, "result": result})
            if verbose:
                print(f"  → {name}({args}) → {json.dumps(result)[:180]}")
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    return {"answer": "(stopped: reached step limit)", "provenance": provenance, "steps": MAX_STEPS}
