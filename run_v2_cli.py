"""CLI test runner for v2 - the from-scratch redesign combining browser-agent
v1's fixes (glyph-safe labels, iframe support, disabled-annotated candidates,
replanning) with jev-ultrafast's single-call operation+target policy.

Usage:
  python3 run_v2_cli.py --url URL "<instruction>" [--headed] [--no-planner]
"""
import argparse
import asyncio
import json

import env_loader
env_loader.load_all()

from v2 import AgentRun


def on_event(event):
    t = event["type"]
    if t == "planning_start":
        print(f"\n[PLAN] decomposing: {event['instruction'][:100]}...")
    elif t == "plan_ready":
        print(f"[PLAN] {len(event['goals'])} goal(s):")
        for i, g in enumerate(event["goals"]):
            print(f"   {i}. {g}")
    elif t == "planning_skipped":
        print(f"[PLAN] skipped ({event['reason']}) - running the instruction as one goal")
    elif t == "goal_start":
        print(f"\n[GOAL {event['index'] + 1}/{event['total']}] {event['goal']}")
    elif t == "decision":
        print(f"   [JEV] {event['operation']} conf={event['confidence']:.2f} ({event['latency_ms']}ms)")
    elif t == "decision_retry":
        print(f"   [JEV RETRY] re-asked -> {event['operation']}")
    elif t == "stale_retry":
        print(f"   [STALE {event['streak']}/5] {event['action']!r} changed before execution - re-observing")
    elif t == "stale_giveup":
        print(f"   [STALE GIVEUP] {event['action']!r} kept changing on every attempt - treating as BLOCKED")
    elif t == "text_failed":
        print(f"   [TEXT ERROR] {event['message']}")
    elif t == "step_done":
        text = f" text={event['text']!r}" if event.get("text") else ""
        print(f"   [DONE] {event['kind']} -> {event['action']!r}{text} (page_changed={event['page_changed']})")
    elif t == "goal_done":
        print(f"   [GOAL COMPLETE] {event['goal']}")
    elif t == "replanning":
        print(f"   [REPLAN] stuck on: {event['goal']}")
    elif t == "replan_ready":
        print(f"   [REPLAN] new goals: {event['goals']}")
    elif t == "run_complete":
        print(f"\n[COMPLETE] {event['elapsed_ms']}ms, {len(event['history'])} actions")
    elif t == "run_failed":
        print(f"\n[FAILED] {event['message']}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("instruction", nargs="+")
    parser.add_argument("--url", required=True)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--no-planner", action="store_true", help="run the instruction as one atomic goal")
    parser.add_argument("--slowmo", type=int, default=0)
    parser.add_argument("--cdp", metavar="URL", default=None,
                         help="attach to your real, already-running Chrome instead of launching a fresh "
                              "throwaway one, e.g. --cdp http://127.0.0.1:9222 (start Chrome with "
                              "--remote-debugging-port=9222 first). A real profile looks far less bot-like "
                              "to sites like yopmail/Google than a blank Playwright-launched browser.")
    args = parser.parse_args()

    run = AgentRun(
        args.url,
        " ".join(args.instruction),
        headless=not args.headed,
        on_event=on_event,
        use_planner=not args.no_planner,
        slow_mo=args.slowmo,
        cdp_url=args.cdp,
    )
    result = await run.run()
    print("\n---")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
