"""Planner LLM client: FissionLabs coding gateway (OpenAI-compatible), dev-model-alpha.

The planner is the ONLY thing allowed to author free text (literal values to
type, search phrases, etc). Jev never generates text - see jev_grounding.py.
"""
import json
import os
import re
import httpx

DEFAULT_BASE_URL = "https://coding-gateway.fissionlabs.com/gateway/openai/v1"
DEFAULT_MODEL = "dev-model-alpha"

DECOMPOSE_SYSTEM_PROMPT = """You turn a natural-language browser task into a strict JSON list of atomic steps.

Each step is one of these shapes:
{"type": "navigate", "value": "<absolute URL>"}
{"type": "type", "target_description": "<short description of the field to type into, e.g. 'the Password field'>", "value": "<exact literal text to type>"}
{"type": "click", "target_description": "<short description of the element to click, e.g. 'the Submit button'>"}
{"type": "extract", "target_description": "<short description of the element to read>", "var": "<variable name to store the result under>"}
{"type": "code", "code": "<literal, directly-executable Python statements>"}
{"type": "done"}

Rules:
- Output ONLY a JSON array of step objects. No prose, no markdown fences, no explanation.
- target_description must describe the element by its VISIBLE label/purpose, not a CSS selector or DOM path.
- For "type" steps, `value` must be the exact literal text implied by the user's instruction - never invent content the user didn't specify.
- For "code" steps, `code` MUST be real, directly-executable Python (never English prose describing what to do). Every variable named in an earlier "extract" step's `var` field is already bound as a Python string in scope - reference it by its bare name (e.g. `num`), not as `{num}`. Assign every result you want kept to a new variable name (e.g. `double_num = int(num) * 2`) so it can be extracted from the execution scope afterward. Only plain Python expressions/statements are allowed - no imports, no I/O, no builtins beyond len/str/int/float/sum/min/max/round.
- Always end with a {"type": "done"} step.
- Keep target_description short (a few words), matching how a human would describe the control."""


class PlannerError(Exception):
    pass


class PlannerClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = DEFAULT_MODEL, timeout: float = 60.0):
        self.api_key = api_key or os.environ.get("GATEWAY_API_KEY")
        self.base_url = (base_url or os.environ.get("GATEWAY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not self.api_key:
            raise PlannerError("GATEWAY_API_KEY not set")
        self.model = model
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()

    async def _chat(self, messages: list[dict]) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/chat/completions",
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json={"model": self.model, "messages": messages},
        )
        if resp.status_code != 200:
            raise PlannerError(f"HTTP {resp.status_code}: {resp.text}")
        data = resp.json()
        return {
            "content": data["choices"][0]["message"]["content"],
            "reasoning": data["choices"][0]["message"].get("reasoning_content"),
            "usage": data.get("usage", {}),
            "raw_request": {"model": self.model, "messages": messages},
            "raw_response": data,
        }

    @staticmethod
    def _extract_json_array(text: str):
        text = text.strip()
        fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1:
            raise PlannerError(f"No JSON array found in planner output: {text[:300]}")
        return json.loads(text[start:end + 1])

    async def decompose(self, instruction: str) -> dict:
        """Turn a natural-language instruction into a step list. Returns dict with
        `steps`, `usage`, and the raw request/response for logging."""
        messages = [
            {"role": "system", "content": DECOMPOSE_SYSTEM_PROMPT},
            {"role": "user", "content": instruction},
        ]
        result = await self._chat(messages)
        steps = self._extract_json_array(result["content"])
        return {"steps": steps, "usage": result["usage"],
                "raw_request": result["raw_request"], "raw_response": result["raw_response"]}

    async def replan(self, original_instruction: str, completed_steps: list[dict],
                      failed_step: dict, failure_reason: str, candidates: list[dict]) -> dict:
        """Called when Jev grounding fails (BLOCKED / none-of-these) for a step.
        Gives the planner the current page candidates and asks for a corrected
        step list to replace the remaining plan."""
        context = {
            "original_instruction": original_instruction,
            "completed_steps": completed_steps,
            "failed_step": failed_step,
            "failure_reason": failure_reason,
            "currently_visible_elements": candidates,
        }
        messages = [
            {"role": "system", "content": DECOMPOSE_SYSTEM_PROMPT + "\n\nYou are REPLANNING: a "
             "prior step failed because its target element could not be found on the current "
             "page. Use `currently_visible_elements` to pick a better target_description, or "
             "change strategy (e.g. scroll, or click something else first). Output the "
             "replacement steps only (from this point forward), same JSON step format."},
            {"role": "user", "content": json.dumps(context)},
        ]
        result = await self._chat(messages)
        steps = self._extract_json_array(result["content"])
        return {"steps": steps, "usage": result["usage"],
                "raw_request": result["raw_request"], "raw_response": result["raw_response"]}
