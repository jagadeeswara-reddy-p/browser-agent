"""CLI test runner: prints every agent event as it happens, no web UI.

Usage:
  python3 run_cli.py [--headed] [--slowmo MS] [--pause SECS] "<instruction>"
  python3 run_cli.py                     # runs the built-in demo, headless
"""
import argparse
import asyncio
import json

import env_loader
env_loader.load_all()

from agent_loop import AgentRun


def on_event(event):
    t = event["type"]
    if t == "planning_start":
        print(f"\n[PLAN] decomposing: {event['instruction'][:100]}...")
    elif t == "plan_ready":
        print(f"[PLAN] {len(event['steps'])} steps, {event['usage'].get('total_tokens', '?')} tokens:")
        for i, s in enumerate(event["steps"]):
            print(f"   {i}. {s}")
    elif t == "step_start":
        print(f"\n[STEP {event['index'] + 1}/{event['total']}] {event['step']}")
    elif t == "grounding":
        log = event["log"]
        status = "FOUND" if event["found"] else "NOT FOUND"
        print(f"   [JEV] {status} cand={log['choice']} conf={log['confidence']:.2f} "
              f"({log['num_candidates']} candidates, {log['latency_ms']:.0f}ms)")
    elif t == "grounding_retry":
        log = event["log"]
        status = "FOUND" if event["found"] else "still not found"
        print(f"   [JEV RETRY] single-candidate re-ask -> {status} (cand={log['choice']} conf={log['confidence']:.2f})")
    elif t == "replanning":
        print(f"   [REPLAN] {event['reason']}")
    elif t == "replan_ready":
        print(f"   [REPLAN] new steps: {event['steps']}")
    elif t == "step_done":
        print(f"   [DONE] {event['detail']}")
    elif t == "step_error":
        print(f"   [ERROR] {event['message']}")
    elif t == "run_complete":
        print(f"\n[COMPLETE] variables={event['variables']}")
    elif t == "run_failed":
        print(f"\n[FAILED] {event['message']}")
    elif t == "holding":
        print(f"\n[HOLD] keeping browser open for {event['seconds']}s so you can check the final page state...")


DEFAULT_INSTRUCTION = (
    "Go to https://www.selenium.dev/selenium/web/web-form.html, "
    "type \"Jev Agent Test\" into the Text input field, "
    "type \"hunter2\" into the Password field, "
    "type \"hello world\" into the Textarea, "
    "then click the Submit button."
)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("instruction", nargs="*", help="natural-language task; omit for the built-in demo")
    parser.add_argument("--headed", action="store_true", help="show a visible browser window")
    parser.add_argument("--slowmo", type=int, default=0, help="ms of artificial delay per Playwright action (for --headed)")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds to pause after each agent step (for --headed)")
    parser.add_argument("--hold", type=float, default=5.0,
                         help="seconds to keep the browser open after the run finishes (success or failure), "
                              "so you can check the final page state before it closes. Default 5s; use 0 to disable.")
    args = parser.parse_args()

    instruction = " ".join(args.instruction) or DEFAULT_INSTRUCTION
    run = AgentRun(
        instruction,
        headless=not args.headed,
        on_event=on_event,
        slow_mo=args.slowmo,
        pause_after_step=args.pause,
        hold_before_close=args.hold if args.headed else 0.0,
    )
    result = await run.run()
    print("\n---")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
