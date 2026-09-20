"""Jev-based element grounding, Third Hand-style: one Choice call per grounding
request, with an explicit 'none of these' escape hatch so Jev can honestly say
the target isn't on screen instead of being forced to pick something wrong."""
import json

NONE_KEY = "__none__"


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
    # Trust Jev's explicit choice as-is (Third Hand's own pattern): the
    # `__none__` option above is how it says "not found" - a real, non-none
    # choice IS its answer, regardless of confidence. A low score here often
    # just means the target_description used different words than the
    # element's actual label (e.g. "the inbox field" vs "Enter your inbox
    # here"), not that the match is wrong. Rejecting on confidence caused a
    # real bug: a correct single-candidate match got treated as "not found"
    # and burned the whole replan budget on a target that was there all along.
    return GroundingResult(True, choice, confidence, probabilities, log)
