"""Schedule clips from several podcasts as a shuffled daily rotation.

Each day gets the next-best clip from every podcast, in a shuffled order, at
jittered daily slots; every clip goes to each connected platform at once.

    python -m src.social.batch_schedule --user-id U --task T1 --task T2 [--apply]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import random
import re
import sys
import time as clock
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx

DEFAULT_SLOTS = "12:00,17:00,20:00,22:00"
DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_HASHTAGS = ("podcast", "podcastclips", "viralpods")
DEFAULT_FOOTER = "The best moments from the best podcasts, daily on Viral Pods."
QUOTE_MAX_CHARS = 180
INACTIVE_POST_STATUSES = {"failed", "cancelled"}


@dataclass(frozen=True)
class Podcast:
    task_id: str
    title: str
    clips: List[dict]
    posted: Dict[str, set] = field(default_factory=dict)


@dataclass(frozen=True)
class Slot:
    when: datetime
    podcast: Podcast
    clip: dict


def parse_slots(value: str) -> List[time]:
    slots = sorted(time.fromisoformat(part.strip()) for part in value.split(",") if part.strip())
    if not slots:
        raise ValueError("at least one slot time is required")
    return slots


def spread(slots: Sequence[time], count: int) -> List[time]:
    """Pick ``count`` slots spaced evenly across the day's slot list."""
    if count > len(slots):
        raise ValueError(f"{count} podcasts need at least {count} daily slots, got {len(slots)}")
    if count == 1:
        return [slots[len(slots) // 2]]
    last = len(slots) - 1
    return [slots[round(i * last / (count - 1))] for i in range(count)]


def plan_schedule(
    podcasts: Sequence[Podcast],
    *,
    start: date,
    slots: Sequence[time],
    tz: ZoneInfo,
    jitter_minutes: int,
    rng: random.Random,
) -> List[Slot]:
    """Day N posts each podcast's Nth clip, shuffled across that day's slots."""
    plan: List[Slot] = []
    days = max((len(p.clips) for p in podcasts), default=0)
    for day in range(days):
        todays = [(p, p.clips[day]) for p in podcasts if day < len(p.clips)]
        rng.shuffle(todays)
        for slot, (podcast, clip) in zip(spread(slots, len(todays)), todays):
            base = datetime.combine(start + timedelta(days=day), slot, tzinfo=tz)
            offset = timedelta(minutes=rng.randint(-jitter_minutes, jitter_minutes))
            plan.append(Slot(base + offset, podcast, clip))
    return sorted(plan, key=lambda s: s.when)


def pick_clips(clips: Sequence[dict], posted_clip_ids: set, limit: int) -> List[dict]:
    fresh = [c for c in clips if c["id"] not in posted_clip_ids and c.get("video_url")]
    fresh.sort(key=lambda c: c.get("virality_score") or 0, reverse=True)
    return fresh[:limit]


def quote_from(text: str) -> str:
    text = " ".join((text or "").split())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    quote = ""
    for sentence in sentences:
        if quote and len(quote) + len(sentence) + 1 > QUOTE_MAX_CHARS:
            break
        quote = f"{quote} {sentence}".strip()
        if len(quote) >= 60:
            break
    if len(quote) > QUOTE_MAX_CHARS:
        quote = quote[:QUOTE_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return quote


def build_caption(clip: dict, podcast_title: str, provider: str, footer: str) -> str:
    parts = []
    # Instagram has no title field, so the hook leads the caption there.
    if provider == "instagram" and clip.get("hook_title"):
        parts.append(clip["hook_title"])
    quote = quote_from(clip.get("text", ""))
    if quote:
        parts.append(f"“{quote}”")
    if podcast_title:
        parts.append(f"From: {podcast_title}")
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


class Api:
    def __init__(self, base_url: str, user_id: str, secret: str):
        self.user_id = user_id
        self.secret = secret
        self.http = httpx.Client(base_url=base_url, timeout=60)

    def _headers(self) -> Dict[str, str]:
        ts = str(int(clock.time()))
        payload = f"{self.user_id}:{ts}".encode("utf-8")
        signature = hmac.new(self.secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return {
            "x-supoclip-user-id": self.user_id,
            "x-supoclip-ts": ts,
            "x-supoclip-signature": signature,
        }

    def get(self, path: str, **params) -> dict:
        response = self.http.get(path, params=params, headers=self._headers())
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict) -> dict:
        response = self.http.post(path, json=body, headers=self._headers())
        if response.status_code >= 400:
            raise RuntimeError(f"POST {path} failed ({response.status_code}): {response.text}")
        return response.json()


def load_podcasts(api: Api, task_ids: Sequence[str], providers: Sequence[str], per_podcast: int) -> List[Podcast]:
    podcasts = []
    for task_id in task_ids:
        task = api.get(f"/tasks/{task_id}")
        if task.get("status") != "completed":
            raise RuntimeError(f"Task {task_id} is {task.get('status')}, not completed")
        posts = api.get("/social/posts", task_id=task_id, limit=500)["posts"]
        # A clip counts as used once every chosen platform has (or will get) it.
        used: Dict[str, set] = {}
        for post in posts:
            if post["status"] not in INACTIVE_POST_STATUSES:
                used.setdefault(post["clip_id"], set()).add(post["provider"])
        done = {clip_id for clip_id, provs in used.items() if set(providers) <= provs}
        clips = pick_clips(task.get("clips") or [], done, per_podcast)
        podcasts.append(Podcast(task_id, task.get("source_title") or task_id, clips, used))
    return podcasts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-id", default=os.getenv("SCHEDULE_USER_ID"), required=not os.getenv("SCHEDULE_USER_ID"))
    parser.add_argument("--task", action="append", required=True, help="Completed task id; repeat per podcast")
    parser.add_argument("--start", type=date.fromisoformat, help="First posting day (default: tomorrow)")
    parser.add_argument("--slots", default=DEFAULT_SLOTS, help=f"Daily times, default {DEFAULT_SLOTS}")
    parser.add_argument("--tz", default=DEFAULT_TZ)
    parser.add_argument("--jitter", type=int, default=30, help="Random +/- minutes per post")
    parser.add_argument("--per-podcast", type=int, default=6)
    parser.add_argument("--providers", help="Comma list, default: every connected platform")
    parser.add_argument("--hashtags", default=",".join(DEFAULT_HASHTAGS))
    parser.add_argument("--footer", default=DEFAULT_FOOTER)
    parser.add_argument("--seed", type=int, help="Repeat a previous shuffle")
    parser.add_argument("--api-url", default=os.getenv("SCHEDULE_API_URL", "http://localhost:8000"))
    parser.add_argument("--apply", action="store_true", help="Create the posts (default is a dry run)")
    args = parser.parse_args(argv)

    tz = ZoneInfo(args.tz)
    start = args.start or (datetime.now(tz).date() + timedelta(days=1))
    if start <= datetime.now(tz).date():
        parser.error("--start must be a future day")
    slots = parse_slots(args.slots)
    if len(args.task) > len(slots):
        parser.error(f"{len(args.task)} podcasts need at least {len(args.task)} --slots")

    api = Api(args.api_url, args.user_id, os.environ["BACKEND_AUTH_SECRET"])
    accounts = {
        c["provider"]: c
        for c in api.get("/social/connections")["connections"]
        if c.get("status") == "active"
    }
    providers = [p.strip() for p in args.providers.split(",")] if args.providers else sorted(accounts)
    missing = [p for p in providers if p not in accounts]
    if missing or not providers:
        parser.error(f"Not connected: {', '.join(missing) or 'no platforms'}")

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(10**6)
    podcasts = load_podcasts(api, args.task, providers, args.per_podcast)
    plan = plan_schedule(podcasts, start=start, slots=slots, tz=tz, jitter_minutes=args.jitter, rng=random.Random(seed))
    if not plan:
        print("Nothing to schedule: every clip is already posted.")
        return 0

    hashtags = [h for h in args.hashtags.split(",") if h.strip()]
    print(f"{'APPLY' if args.apply else 'DRY RUN'} seed={seed} -> {', '.join(providers)}")
    for item in plan:
        targets = [p for p in providers if p not in item.podcast.posted.get(item.clip["id"], set())]
        print(f"{item.when:%a %d %b %H:%M}  {item.podcast.title[:28]:<28}  "
              f"{item.clip.get('virality_score') or 0:>3}  {item.clip.get('hook_title') or ''}"
              f"  [{'+'.join(targets)}]")
        if not args.apply:
            continue
        for provider in targets:
            api.post("/social/posts", {
                "task_id": item.podcast.task_id,
                "clip_id": item.clip["id"],
                "social_account_id": accounts[provider]["id"],
                "title": item.clip.get("hook_title"),
                "caption": build_caption(item.clip, item.podcast.title, provider, args.footer),
                "hashtags": hashtags,
                "privacy_level": "public",
                "scheduled_for": item.when.isoformat(),
            })
    print(f"{len(plan)} clips over {len({i.when.date() for i in plan})} days"
          + ("" if args.apply else ". Re-run with --apply (and --seed) to schedule."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
