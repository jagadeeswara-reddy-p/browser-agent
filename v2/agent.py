"""The orchestrator: sequential goal list (from decomposition) x per-goal
single-call Jev policy (from jev-ultrafast) x replanning-on-stuck (from
browser-agent v1). `on_event` gets every event so a CLI can render live
progress identically to v1's run_cli.py."""
import asyncio
import time

from .browser import Browser, StalePage
from .llm import LlmClient, LlmError
from .model import JevClient, JevError, choose
from .questions import MAX_STEPS


class AgentError(Exception):
    pass


class AgentRun:
    def __init__(self, url: str, instruction: str, *, headless: bool = True, on_event=None,
                 use_planner: bool = True, max_replans: int = 4, slow_mo: int = 0, cdp_url: str | None = None):
        self.url = url
        self.instruction = instruction
        self.headless = headless
        self.slow_mo = slow_mo
        self.use_planner = use_planner
        self.max_replans = max_replans
        self.cdp_url = cdp_url
        self.on_event = on_event or (lambda event: None)

    async def emit(self, event_type: str, **payload):
        result = self.on_event({"type": event_type, **payload})
        if hasattr(result, "__await__"):
            await result

    async def run(self) -> dict:
        browser = Browser()
        jev = JevClient()
        llm = LlmClient()
        try:
            await browser.start(headless=self.headless, slow_mo=self.slow_mo, cdp_url=self.cdp_url)
            await browser.navigate(self.url)

            goals = [self.instruction]
            if self.use_planner:
                try:
                    await self.emit("planning_start", instruction=self.instruction)
                    decomposed = await llm.decompose(self.instruction)
                    if decomposed:
                        goals = decomposed
                    await self.emit("plan_ready", goals=goals)
                except LlmError as e:
                    await self.emit("planning_skipped", reason=str(e))

            completed_goals: list[str] = []
            history: list[dict] = []
            replans_used = 0
            started = time.perf_counter()

            goal_index = 0
            while goal_index < len(goals):
                goal = goals[goal_index]
                await self.emit("goal_start", index=goal_index, total=len(goals), goal=goal)
                status = await self._run_goal(browser, jev, llm, goal, history)

                if status == "done":
                    completed_goals.append(goal)
                    await self.emit("goal_done", goal=goal)
                    goal_index += 1
                    continue

                # status == "blocked": ask the planner for replacement goal(s)
                # instead of failing the whole run outright.
                if replans_used >= self.max_replans:
                    raise AgentError(f"Goal {goal_index} ({goal!r}) is stuck and the replan budget is exhausted.")
                replans_used += 1
                page = await browser.snapshot()
                candidates = [{"label": a["label"], "role": a["role"], "disabled": a.get("disabled", False)}
                              for a in page["actions"]][:60]
                await self.emit("replanning", goal=goal)
                try:
                    replacement = await llm.replan(self.instruction, completed_goals, goal,
                                                     "The policy declared BLOCKED on this goal.", candidates)
                except LlmError as e:
                    raise AgentError(f"Replan failed: {e}") from e
                if not replacement:
                    raise AgentError(f"Goal {goal_index} ({goal!r}) is stuck and the planner had no replacement.")
                goals = goals[:goal_index] + replacement + goals[goal_index + 1:]
                await self.emit("replan_ready", goals=replacement)

            elapsed_ms = round((time.perf_counter() - started) * 1000)
            await self.emit("run_complete", elapsed_ms=elapsed_ms, history=history)
            return {"success": True, "elapsed_ms": elapsed_ms, "history": history, "final_url": await browser.current_url()}

        except (AgentError, JevError, LlmError) as e:
            await self.emit("run_failed", message=str(e))
            return {"success": False, "error": str(e)}
        except Exception as e:
            await self.emit("run_failed", message=f"{type(e).__name__}: {e}")
            return {"success": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            await jev.close()
            await llm.close()
            await browser.close()

    async def _run_goal(self, browser: Browser, jev: JevClient, llm: LlmClient, goal: str,
                         history: list[dict]) -> str:
        """Runs the single-call operation+target policy against one goal until
        DONE, BLOCKED, or the per-run step budget. Returns 'done' or 'blocked'."""
        consecutive_no_progress = 0
        stale_streak = 0
        for _ in range(MAX_STEPS):
            page = await browser.snapshot()
            decision = await choose(jev, page, goal, history)
            await self.emit("decision", goal=goal, operation=decision["operation"],
                             confidence=decision["confidence"], latency_ms=decision["latency_ms"])

            operation = decision["operation"]
            if operation == "DONE":
                return "done"
            if operation == "BLOCKED":
                # Grounding/policy calls are stochastic, not deterministic -
                # verified live in browser-agent v1 that an identical
                # single-candidate question can flip found/not-found purely
                # from sampling noise. Re-ask once before trusting BLOCKED,
                # since a full replan can't help if the page hasn't changed.
                retry = await choose(jev, page, goal, history)
                await self.emit("decision_retry", goal=goal, operation=retry["operation"])
                if retry["operation"] not in ("BLOCKED",):
                    decision = retry
                    operation = decision["operation"]
                else:
                    return "blocked"

            action = decision["action"]
            if operation == "SCROLL_DOWN":
                await browser.scroll(560)
                continue
            if operation == "SCROLL_UP":
                await browser.scroll(-560)
                continue
            if operation == "WAIT":
                await browser.wait()
                continue

            # Freshness guard: the Jev call (and, for TYPE_TEXT, the text-
            # authoring call) is real network latency during which the page
            # can change. Re-check the specific target immediately before
            # touching it rather than trusting the snapshot it was chosen from.
            frame = browser.frame_for(action)
            try:
                recorded_guard = await frame.guard(action["node"])
            except Exception:
                recorded_guard = None

            text = None
            if operation == "TYPE_TEXT":
                context = {"goal": goal, "field": {"label": action["label"], "role": action["role"]},
                           "page": {"title": page["title"], "text": page["text"][:4000]},
                           "recent_actions": [{"action": h["action"], "text": h.get("text")} for h in history[-6:]]}
                try:
                    text = await llm.author_text(context)
                except LlmError as e:
                    await self.emit("text_failed", goal=goal, message=str(e))
                    consecutive_no_progress += 1
                    if consecutive_no_progress >= 3:
                        return "blocked"
                    continue

            if recorded_guard is not None and not await browser.guard_ok(action, recorded_guard):
                stale_streak += 1
                await self.emit("stale_retry", goal=goal, action=action["label"], streak=stale_streak)
                # Seen live: a reCAPTCHA-style widget can regenerate itself on
                # every attempted interaction, defensively, forever - that's
                # its anti-automation job working as intended, not transient
                # DOM drift the guard should keep chasing. A tight immediate
                # retry loop against that both burns the whole step budget
                # and hammers the paid Jev API for nothing. Fail fast with a
                # clear reason instead, with a short backoff before giving up.
                if stale_streak >= 5:
                    await self.emit("stale_giveup", goal=goal, action=action["label"])
                    return "blocked"
                await asyncio.sleep(0.3)
                continue  # re-observe fresh next loop iteration, no run budget spent

            try:
                if operation == "CLICK":
                    await browser.click(action)
                elif operation == "TYPE_TEXT":
                    await browser.fill(action, text)
                elif operation == "SELECT":
                    await browser.select(action)
            except StalePage:
                stale_streak += 1
                await self.emit("stale_retry", goal=goal, action=action["label"], streak=stale_streak)
                if stale_streak >= 5:
                    await self.emit("stale_giveup", goal=goal, action=action["label"])
                    return "blocked"
                await asyncio.sleep(0.3)
                continue

            stale_streak = 0
            new_page = await browser.snapshot()
            page_changed = new_page["fingerprint"] != page["fingerprint"]
            history.append({
                "goal": goal, "action": action["label"], "kind": operation, "text": text,
                "page_changed": page_changed, "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"], "usage": decision["usage"],
            })
            await self.emit("step_done", goal=goal, action=action["label"], kind=operation,
                             text=text, page_changed=page_changed)
            consecutive_no_progress = 0 if page_changed else consecutive_no_progress + 1
            if consecutive_no_progress >= 3:
                return "blocked"
        return "blocked"
