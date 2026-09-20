"""Jev grounding: one TypeSafe request per tick, deciding both the operation
(CLICK/TYPE_TEXT/SELECT/SCROLL/WAIT/DONE/BLOCKED) and its target in a single
round trip - jev-ultrafast's key speed idea. Response shape is validated
before anything executes (that check did not exist in browser-agent v1)."""
import math
import os
import time

import httpx

from .questions import NEXT_ACTION, TARGET

BASE_URL = "https://api.typesafe.ai/v1/systemone"


class JevError(Exception):
    pass


class JevClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.api_key:
            raise JevError("TYPESAFE_API_KEY not set")
        self.model = model or os.environ.get("TYPESAFE_MODEL", "jev-latest")
        self._client = httpx.AsyncClient(timeout=timeout)
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_calls = 0

    async def close(self):
        await self._client.aclose()

    async def _post(self, body: dict) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_err = None
        for attempt in range(3):
            try:
                resp = await self._client.post(BASE_URL, json=body, headers=headers)
            except httpx.HTTPError as e:
                last_err = e
                continue
            if resp.status_code in (429, 503, 529) and attempt < 2:
                last_err = JevError(f"HTTP {resp.status_code}: {resp.text}")
                continue
            if resp.status_code != 200:
                raise JevError(f"HTTP {resp.status_code}: {resp.text}")
            data = resp.json()
            usage = data.get("usage", {})
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_calls += 1
            return data
        raise JevError(f"Jev request failed after 3 attempts: {last_err}")


def validate_choice(answer: dict, valid_ids) -> dict:
    """Reject a malformed/self-inconsistent TypeSafe answer instead of trusting
    it blind - a real gap in browser-agent v1, which never checked the shape
    of what came back before acting on it."""
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in valid_ids
            and set(probabilities) == set(valid_ids)
            and all(isinstance(n, (int, float)) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise JevError(f"Invalid TypeSafe response for this question: {answer!r}")
    return answer


def action_space(actions: list[dict]):
    """One index per observed (node, frame) pair; each operation gets its own
    valid-target list. A node offering both click and type (e.g. a combobox
    input) still gets one index shared across operations."""
    elements, indices, targets = [], {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            continue
        key = (action["frame_index"], action["node"])
        if key not in indices:
            index = str(len(elements) + 1)
            indices[key] = index
            element = {
                "index": index,
                "label": action["label"],
                "role": action["role"],
                "disabled": action.get("disabled", False),
            }
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[key]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        target = index
        if kind == "select":
            element = elements[int(index) - 1]
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets


async def choose(client: JevClient, page: dict, goal: str, history: list[dict]) -> dict:
    elements, targets = action_space(page["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, or autocomplete suggestion.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM supplies the value from the goal.",
        "SELECT": "Choose an observed dropdown option.",
    }
    operations = {key: labels[key] for key in targets}
    operations["SCROLL_DOWN"] = "Scroll the page down to reveal more content."
    operations["SCROLL_UP"] = "Scroll the page up."
    operations["WAIT"] = "Wait briefly for the page to update or finish loading."
    operations["DONE"] = "Every requirement of the current goal is visibly satisfied."
    operations["BLOCKED"] = "No supported operation can progress the current goal."

    questions = {
        "operation": {
            "type": "choice",
            "criteria": operations,
            "instructions": {"goal": goal, "rules": NEXT_ACTION},
        }
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "disabled": a.get("disabled", False),
                    "current_value": a.get("current_value", a.get("value", "")),
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }

    body = {
        "model": client.model,
        "state": {
            "page": {"url": page["url"], "title": page["title"], "text": page["text"]},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = await client._post(body)
    answers = result["answers"]
    operation_answer = validate_choice(answers.get("operation", {}), operations)
    operation = operation_answer["choice"]

    target_answer, target, action = None, None, None
    if operation in targets:
        target_answer = validate_choice(answers.get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        action = targets[operation][target]

    return {
        "operation": operation,
        "target": target,
        "action": action,
        "confidence": operation_answer["confidence"],
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }
