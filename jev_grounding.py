"""Jev-based element grounding, Third Hand-style: one Choice call per grounding
request, with an explicit 'none of these' escape hatch so Jev can honestly say
the target isn't on screen instead of being forced to pick something wrong."""
import json

NONE_KEY = "__none__"
CONFIDENCE_THRESHOLD = 0.55


class GroundingResult:
    def __init__(self, found: bool, cand_id: str | None, confidence: float,
                 probabilities: dict, log: dict):
        self.found = found
        self.cand_id = cand_id
        self.confidence = confidence
        self.probabilities = probabilities
        self.log = log


async def ground_target(client, candidates: dict, target_description: str, page_url: str) -> GroundingResult:
    """`candidates` is {id: description} for ONE operation type (CLICK or TYPE_TEXT),
    as produced by BrowserSession.snapshot()."""
    criteria = dict(candidates)
    criteria[NONE_KEY] = "None of these - the described element is not visible on the current page"

    state = {
        "page_url": page_url,
        "target": target_description,
    }
    questions = {
        "target": {
            "type": "choice",
            "instructions": (
                f"Which visible page element matches this description: \"{target_description}\"? "
                "Each option is [visible label] [tag/type]. Pick the single best match, or "
                "'none of these' if nothing on the page matches."
            ),
            "criteria": criteria,
        }
    }
    result = await client.ask(state, questions)
    answer = result["answers"]["target"]
    choice = answer["choice"]
    confidence = answer.get("confidence", 0.0)
    probabilities = answer.get("probabilities", {})

    log = {
        "target_description": target_description,
        "num_candidates": len(candidates),
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
        "latency_ms": result["latency_ms"],
        "usage": result["usage"],
        "raw_request": result["raw_request"],
        "raw_response": result["raw_response"],
    }

    if choice == NONE_KEY or choice not in candidates:
        return GroundingResult(False, None, confidence, probabilities, log)
    if confidence < CONFIDENCE_THRESHOLD:
        log["note"] = f"confidence {confidence} below threshold {CONFIDENCE_THRESHOLD}; treating as not found"
        return GroundingResult(False, None, confidence, probabilities, log)
    return GroundingResult(True, choice, confidence, probabilities, log)
