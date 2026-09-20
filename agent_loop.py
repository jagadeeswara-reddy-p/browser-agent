"""Orchestrator: planner decomposes -> per-step Jev grounding -> Playwright
executes -> replan on grounding failure. `on_event` is called with every
event dict so a CLI or web UI can render live progress/logs identically."""
import asyncio
import re

from browser_bridge import BrowserSession
from jev_grounding import ground_target
from jev_client import JevClient, JevError
from planner_client import PlannerClient, PlannerError


class AgentError(Exception):
    pass


class AgentRun:
    def __init__(self, instruction: str, headless: bool = True, on_event=None,
                 max_replans: int = 6, slow_mo: int = 0, pause_after_step: float = 0.0,
                 hold_before_close: float = 0.0):
        self.instruction = instruction
        self.headless = headless
        self.slow_mo = slow_mo
        self.pause_after_step = pause_after_step
        self.hold_before_close = hold_before_close
        self.on_event = on_event or (lambda event: None)
        self.max_replans = max_replans
        self.variables: dict[str, str] = {}
        self.completed_steps: list[dict] = []

    async def emit(self, event_type: str, **payload):
        result = self.on_event({"type": event_type, **payload})
        if hasattr(result, "__await__"):
            await result

    def _substitute_vars(self, text: str) -> str:
        def repl(m):
            return str(self.variables.get(m.group(1), m.group(0)))
        return re.sub(r"\{(\w+)\}", repl, text)

    async def run(self):
        browser = BrowserSession()
        jev = JevClient()
        planner = PlannerClient()
        try:
            await browser.start(headless=self.headless, slow_mo=self.slow_mo)

            await self.emit("planning_start", instruction=self.instruction)
            plan_result = await planner.decompose(self.instruction)
            steps = plan_result["steps"]
            await self.emit("plan_ready", steps=steps, usage=plan_result["usage"],
                             raw_request=plan_result["raw_request"], raw_response=plan_result["raw_response"])

            i = 0
            replans_used = 0
            while i < len(steps):
                step = steps[i]
                step_type = step.get("type")
                await self.emit("step_start", index=i, total=len(steps), step=step)

                if step_type == "navigate":
                    url = self._substitute_vars(step["value"])
                    await browser.navigate(url)
                    await self.emit("step_done", index=i, step=step, detail={"navigated_to": url})

                elif step_type == "done":
                    await self.emit("step_done", index=i, step=step, detail={})
                    break

                elif step_type == "code":
                    detail = await self._run_code_step(step)
                    await self.emit("step_done", index=i, step=step, detail=detail)

                elif step_type in ("click", "type", "extract"):
                    snap = await browser.snapshot()
                    bucket = "TYPE_TEXT" if step_type in ("type",) else "CLICK"
                    candidates = snap.get(bucket, {})
                    target_desc = step.get("target_description", "")
                    grounding = await ground_target(jev, candidates, target_desc, await browser.current_url())
                    await self.emit("grounding", index=i, step=step, log=grounding.log, found=grounding.found)

                    # Grounding is stochastic per-call, not deterministic:
                    # verified live that the SAME single-candidate question
                    # against the SAME page gives found=True roughly a third
                    # of the time and found=False (choosing __none__) the
                    # rest, purely from sampling variance in a genuinely
                    # close call - not page-state drift. When exactly one
                    # real candidate exists, a full replan can't help (the
                    # candidate never changes, only the wording does), but
                    # simply re-asking the identical question a few times
                    # resolves it cheaply without spending a planner call.
                    if not grounding.found and len(candidates) == 1:
                        for _ in range(3):
                            grounding = await ground_target(jev, candidates, target_desc, await browser.current_url())
                            await self.emit("grounding_retry", index=i, step=step, log=grounding.log, found=grounding.found)
                            if grounding.found:
                                break

                    failure_reason = None
                    if not grounding.found:
                        failure_reason = (
                            "Target element not found on the current page (Jev grounding returned "
                            "none-of-these or low confidence)."
                        )
                    elif step_type == "click" and not await browser.is_enabled(grounding.cand_id):
                        # Found the right element, but it's disabled - seen
                        # live on yopmail's Send button, which stays disabled
                        # until more of the compose form is filled in. This
                        # is a genuinely different, more actionable failure
                        # than "not found": the planner needs an extra step
                        # (fill another field), not a reworded description.
                        failure_reason = (
                            f"Found the right element ({candidates.get(grounding.cand_id)!r}), but it is "
                            "currently disabled. This usually means another required field must be filled "
                            "in first - add the missing step(s) before retrying this click."
                        )

                    if failure_reason:
                        if replans_used >= self.max_replans:
                            raise AgentError(
                                f"Step {i} ({step_type} -> {target_desc!r}) failed and replan budget "
                                f"({self.max_replans}) exhausted. Last reason: {failure_reason}"
                            )
                        replans_used += 1
                        await self.emit("replanning", index=i, step=step, reason=failure_reason)
                        full_snap = await browser.snapshot()
                        all_candidates = {**full_snap.get("CLICK", {}), **full_snap.get("TYPE_TEXT", {})}
                        # Cap what goes to the planner: an unbounded candidate
                        # list (plus growing step history over several
                        # replans) can push the request large enough that the
                        # model spends its whole completion budget reasoning
                        # and returns no actual answer.
                        capped = list(all_candidates.items())[:60]
                        replan_result = await planner.replan(
                            self.instruction, self.completed_steps, step, failure_reason,
                            [{"id": k, "description": v} for k, v in capped],
                        )
                        new_steps = replan_result["steps"]
                        await self.emit("replan_ready", steps=new_steps, usage=replan_result["usage"],
                                         raw_request=replan_result["raw_request"],
                                         raw_response=replan_result["raw_response"])
                        steps = steps[:i] + new_steps
                        continue  # re-process step i, now the first replanned step

                    if step_type == "click":
                        await browser.click(grounding.cand_id)
                        detail = {"clicked_candidate": grounding.cand_id}
                    elif step_type == "type":
                        value = self._substitute_vars(step.get("value", ""))
                        await browser.fill(grounding.cand_id, value)
                        detail = {"typed_into": grounding.cand_id, "value": value}
                    else:  # extract
                        text = await browser.extract_text(grounding.cand_id)
                        var_name = step.get("var", f"extracted_{i}")
                        self.variables[var_name] = text
                        detail = {"extracted": text, "var": var_name}

                    await self.emit("step_done", index=i, step=step, detail=detail)

                else:
                    await self.emit("step_error", index=i, step=step,
                                     message=f"unknown step type {step_type!r}")

                self.completed_steps.append(step)
                if self.pause_after_step:
                    await asyncio.sleep(self.pause_after_step)
                i += 1

            await self.emit("run_complete", variables=self.variables)
            return {"success": True, "variables": self.variables}

        except (AgentError, PlannerError, JevError) as e:
            await self.emit("run_failed", message=str(e))
            return {"success": False, "error": str(e)}
        except Exception as e:
            # Anything else (a Playwright error, an unexpected API shape, ...)
            # should end the run cleanly, not crash the CLI with a raw
            # traceback - the browser/client cleanup in `finally` still runs.
            await self.emit("run_failed", message=f"{type(e).__name__}: {e}")
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            if self.hold_before_close:
                await self.emit("holding", seconds=self.hold_before_close)
                await asyncio.sleep(self.hold_before_close)
            await jev.close()
            await planner.close()
            await browser.close()

    async def _run_code_step(self, step: dict) -> dict:
        """Sandboxed-ish exec for pure-computation steps. No filesystem/network
        builtins exposed; only the extracted variables are visible. `code` must
        be literal Python from the planner, not natural language."""
        code = step.get("code", "")
        safe_globals = {"__builtins__": {"len": len, "str": str, "int": int, "float": float,
                                          "sum": sum, "min": min, "max": max, "round": round}}
        local_vars = dict(self.variables)
        try:
            exec(code, safe_globals, local_vars)  # noqa: S102 - POC-scoped, restricted builtins
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        for k, v in local_vars.items():
            if k not in self.variables or self.variables[k] != v:
                self.variables[k] = v
        return {"executed": code, "variables_after": dict(self.variables)}
