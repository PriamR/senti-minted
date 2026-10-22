"""The agent controller: a bounded, grounded tool-calling loop over DeepSeek.

Sends the question + tool schemas, executes whatever tools the model calls,
feeds results back, and repeats until the model answers or the step cap is hit.
Returns the answer plus full provenance (every tool call + result), so any claim
is auditable back to a deterministic tool output.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from ..clients import deepseek
from ..clients.deepseek import UsageLedger
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
expensive but works for any window since launch; if it returns available:false, rely on the \
approval curve numbers and say why the detail is missing.
- GROUNDING: state only numbers returned by tools. Never invent or interpolate a figure. If a tool \
returns nothing useful, say so plainly.
- Keep it tight: end with a clear, specific answer. State any assumption you made (e.g. how you \
interpreted the timeframe). Express approval as percentages.
"""


async def run_agent(question: str, *, verbose: bool = False) -> dict[str, Any]:
    if not settings.deepseek_api_key:
        return {"answer": "DEEPSEEK_API_KEY not set.", "provenance": [], "steps": 0, "usage": None}

    client = deepseek.client()
    model, _ = deepseek.tier("fast")
    ledger = UsageLedger()
    token = tools.current_ledger.set(ledger)
    try:
        return await _loop(client, model, question, ledger, verbose=verbose)
    finally:
        tools.current_ledger.reset(token)


def _record(ledger: UsageLedger, resp: Any, model: str, latency: float) -> None:
    u = resp.usage.model_dump() if resp.usage else {}
    usage = {
        "prompt_tokens": u.get("prompt_tokens", 0),
        "prompt_cache_hit_tokens": u.get("prompt_cache_hit_tokens", 0),
        "prompt_cache_miss_tokens": u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0)),
        "completion_tokens": u.get("completion_tokens", 0),
        "reasoning_tokens": 0,
    }
    at = datetime.now(tz=timezone.utc)
    ledger.record({**usage, "model": resp.model or model, "thinking": False, "stage": "agent",
                   "cached": False, "cost_usd": deepseek.cost_usd(resp.model or model, usage, at),
                   "peak": deepseek.is_peak(at), "latency_s": round(latency, 2)})


async def _loop(client: Any, model: str, question: str, ledger: UsageLedger, *, verbose: bool) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    provenance: list[dict[str, Any]] = []

    for step in range(MAX_STEPS):
        t0 = time.perf_counter()
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools.TOOLS,
            tool_choice="auto",
            temperature=0.0,
            # thinking + tools would need reasoning_content replayed every turn
            extra_body={"thinking": {"type": "disabled"}},
        )
        _record(ledger, resp, model, time.perf_counter() - t0)
        msg = resp.choices[0].message

        if not msg.tool_calls:
            return {"answer": msg.content or "", "provenance": provenance, "steps": step,
                    "usage": ledger.summary()}

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

    return {"answer": "(stopped: reached step limit)", "provenance": provenance, "steps": MAX_STEPS,
            "usage": ledger.summary()}
