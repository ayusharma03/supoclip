"""Always-on social poster: publishes queued clips to YouTube and Instagram.

Runs on a small always-on machine (e.g. a homelab) so posting does not depend
on the machine that renders clips. It reuses SupoClip's provider code
(``src.social``) and keeps its state in one JSON file:

    /data/queue.json      every post and its state
    /data/incoming/*.json batches dropped by ``handoff.py``; merged, then deleted
    /data/media/<file>    the clip files those batches name

YouTube posts are uploaded early (private, with ``publishAt``), at most
``YOUTUBE_DAILY_UPLOADS`` per rolling 24 hours, so YouTube makes them public on
time itself. Instagram cannot schedule through its API: each reel is published
at its time, at most one per ``INSTAGRAM_MIN_GAP_MINUTES`` so missed posts
never go out in a burst.

    python poster.py          run forever
    python poster.py status   print the queue
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import Config
from src.social.base import PublishRequest, SocialProviderError
from src.social.instagram import FACEBOOK_GRAPH_API, SYSTEM_USER_LOGIN, InstagramProvider
from src.social.youtube import YouTubeProvider

DATA = Path(os.getenv("POSTER_DATA", "/data"))
QUEUE = DATA / "queue.json"
INCOMING = DATA / "incoming"
MEDIA = DATA / "media"

TICK_SECONDS = int(os.getenv("POSTER_TICK_SECONDS", "60"))
YOUTUBE_DAILY_UPLOADS = int(os.getenv("YOUTUBE_DAILY_UPLOADS", "5"))
YOUTUBE_LEAD = timedelta(hours=float(os.getenv("YOUTUBE_LEAD_HOURS", "720")))
INSTAGRAM_MIN_GAP = timedelta(minutes=float(os.getenv("INSTAGRAM_MIN_GAP_MINUTES", "20")))
RETRY_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 5

log = logging.getLogger("poster")

# pending -> published | failed | unknown (platform may have it; never re-sent automatically)
PENDING, PUBLISHED, FAILED, UNKNOWN = "pending", "published", "failed", "unknown"


def now() -> datetime:
    return datetime.now(timezone.utc)


def parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_queue() -> List[Dict[str, Any]]:
    if not QUEUE.exists():
        return []
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save_queue(queue: List[Dict[str, Any]]) -> None:
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(QUEUE)


def merge_incoming(queue: List[Dict[str, Any]]) -> int:
    """Add posts from dropped batch files; a post id already queued is ignored."""
    known = {post["id"] for post in queue}
    added = 0
    for batch in sorted(INCOMING.glob("*.json")):
        for post in json.loads(batch.read_text(encoding="utf-8")):
            if post["id"] in known:
                continue
            # Already-published entries only record past uploads (quota budget).
            if post.get("status") != PUBLISHED and not (MEDIA / post["file"]).is_file():
                raise RuntimeError(f"{batch.name}: missing media {post['file']}")
            post.setdefault("status", PENDING)
            post.setdefault("attempts", 0)
            queue.append(post)
            known.add(post["id"])
            added += 1
        batch.unlink()
    return added


DEFAULT_ACCOUNT = "viralpods"


def account_prefixes() -> Dict[str, str]:
    """``POSTER_ACCOUNTS="viralpods=,lossantosbhai=LSB_"``: account -> env var prefix."""
    raw = os.getenv("POSTER_ACCOUNTS", f"{DEFAULT_ACCOUNT}=")
    pairs = (item.split("=", 1) for item in raw.split(",") if item.strip())
    return {name.strip(): prefix.strip() for name, prefix in pairs}


class Account:
    """One brand: its YouTube channel (own Google Cloud project) and Instagram account.

    Env vars are the plain names (YOUTUBE_REFRESH_TOKEN, INSTAGRAM_ACCOUNT_ID, ...)
    with the account's prefix; a platform without its vars is not set up.
    """

    def __init__(self, name: str, prefix: str):
        self.name, self.prefix = name, prefix
        config = Config()
        config.youtube_oauth_client_id = self.env("YOUTUBE_OAUTH_CLIENT_ID") or ""
        config.youtube_oauth_client_secret = self.env("YOUTUBE_OAUTH_CLIENT_SECRET") or ""
        self.youtube = YouTubeProvider(config)
        self.instagram = InstagramProvider(config)
        self._yt_token: Optional[str] = None
        self._yt_expires = now()

    def env(self, key: str) -> Optional[str]:
        return os.getenv(self.prefix + key) or None

    def has(self, provider: str) -> bool:
        keys = ("YOUTUBE_REFRESH_TOKEN",) if provider == "youtube" else ("INSTAGRAM_SYSTEM_USER_TOKEN", "INSTAGRAM_ACCOUNT_ID")
        return all(self.env(key) for key in keys)

    def youtube_token(self) -> str:
        if not self._yt_token or self._yt_expires - now() < timedelta(minutes=5):
            tokens = self.youtube.refresh_tokens(self.env("YOUTUBE_REFRESH_TOKEN"))
            self._yt_token = tokens.access_token
            self._yt_expires = tokens.expires_at or now() + timedelta(minutes=50)
        return self._yt_token

    def instagram_token(self) -> str:
        return self.env("INSTAGRAM_SYSTEM_USER_TOKEN")

    def instagram_metadata(self) -> Dict[str, Any]:
        return {"login": SYSTEM_USER_LOGIN, "ig_user_id": self.env("INSTAGRAM_ACCOUNT_ID")}


class Accounts(dict):
    def __init__(self):
        super().__init__({name: Account(name, prefix) for name, prefix in account_prefixes().items()})

    def of(self, post: Dict[str, Any]) -> Account:
        return self[post.get("account") or DEFAULT_ACCOUNT]


def ready(post: Dict[str, Any], at: datetime) -> bool:
    retry = parse(post.get("retry_at"))
    return post["status"] == PENDING and (retry is None or retry <= at)


def account_of(post: Dict[str, Any]) -> str:
    return post.get("account") or DEFAULT_ACCOUNT


def pick(queue: List[Dict[str, Any]], at: datetime) -> List[Dict[str, Any]]:
    """Posts to publish in this tick, in order. Budgets and gaps are per account:
    each YouTube channel has its own Google Cloud project and quota."""
    chosen: List[Dict[str, Any]] = []
    day_ago = at - timedelta(hours=24)
    for account in sorted({account_of(p) for p in queue}):
        mine = [p for p in queue if account_of(p) == account]

        youtube = sorted((p for p in mine if p["provider"] == "youtube" and ready(p, at)), key=lambda p: p["scheduled_for"])
        used = sum(
            1 for p in mine
            if p["provider"] == "youtube" and p.get("uploaded_at") and parse(p["uploaded_at"]) > day_ago
        )
        # Due posts always go; early ones only within the rolling 24h budget.
        due = [p for p in youtube if parse(p["scheduled_for"]) <= at]
        early = [p for p in youtube if at < parse(p["scheduled_for"]) <= at + YOUTUBE_LEAD]
        chosen.extend(due + early[: max(0, YOUTUBE_DAILY_UPLOADS - used - len(due))])

        last_ig = max(
            (parse(p["uploaded_at"]) for p in mine if p["provider"] == "instagram" and p.get("uploaded_at")),
            default=None,
        )
        if last_ig is None or at - last_ig >= INSTAGRAM_MIN_GAP:
            due_ig = sorted(
                (p for p in mine if p["provider"] == "instagram" and ready(p, at) and parse(p["scheduled_for"]) <= at),
                key=lambda p: p["scheduled_for"],
            )
            chosen.extend(due_ig[:1])
    return chosen


def publish(post: Dict[str, Any], accounts: Accounts, at: datetime) -> None:
    scheduled = parse(post["scheduled_for"])
    account = accounts.of(post)
    if not account.has(post["provider"]):
        post.update(status=FAILED, error=f"{account.name} has no {post['provider']} login on this poster")
        log.warning("%s %s: %s", post["provider"], post["id"], post["error"])
        return
    if post["provider"] == "youtube":
        provider, token, metadata = account.youtube, account.youtube_token(), {}
    else:
        provider, token, metadata = account.instagram, account.instagram_token(), account.instagram_metadata()
    request = PublishRequest(
        file_path=str(MEDIA / post["file"]),
        title=post.get("title") or "",
        caption=post.get("caption") or "",
        hashtags=list(post.get("hashtags") or []),
        privacy_level=post.get("privacy_level") or "public",
        account_metadata=metadata,
        publish_at=scheduled if post["provider"] == "youtube" and scheduled > at else None,
    )
    post["attempts"] = int(post.get("attempts") or 0) + 1
    try:
        result = provider.publish(token, request)
    except SocialProviderError as exc:
        post["error"] = str(exc)[:500]
        if exc.outcome_unknown:
            post["status"] = UNKNOWN
        elif exc.retryable and post["attempts"] < MAX_ATTEMPTS:
            post["retry_at"] = (at + RETRY_AFTER).isoformat()
        else:
            post["status"] = FAILED
        log.warning("%s %s: %s (%s)", post["provider"], post["id"], post["error"], post["status"])
        return
    post.update(
        status=PUBLISHED,
        external_id=result.external_post_id,
        url=result.external_url,
        uploaded_at=at.isoformat(),
        error=None,
        retry_at=None,
    )
    live = "goes public " + post["scheduled_for"] if request.publish_at else "live now"
    log.info("%s/%s %s: %s (%s)", account.name, post["provider"], post.get("title"), result.external_url, live)


def tick(accounts: Accounts) -> None:
    queue = load_queue()
    added = merge_incoming(queue)
    if added:
        log.info("Queued %d new posts", added)
        save_queue(queue)
    at = now()
    for post in pick(queue, at):
        try:
            publish(post, accounts, at)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            post["error"] = f"{type(exc).__name__}: {exc}"[:500]
            post["retry_at"] = (at + RETRY_AFTER).isoformat()
            log.exception("Unexpected error for %s", post["id"])
        save_queue(queue)


def status() -> None:
    for post in sorted(load_queue(), key=lambda p: p["scheduled_for"]):
        when = parse(post["scheduled_for"]).astimezone(timezone(timedelta(hours=5, minutes=30)))
        print(f"{when:%d %b %H:%M}  {account_of(post):<14} {post['provider']:<9} {post['status']:<9} {(post.get('title') or '')[:50]}"
              + (f"  ! {post['error'][:60]}" if post.get("error") and post["status"] != PUBLISHED else ""))


def check() -> None:
    """Prove every configured login works without posting anything."""
    for account in Accounts().values():
        if account.has("youtube"):
            account.youtube_token()
            print(f"{account.name} YouTube: token refreshed")
        if account.has("instagram"):
            meta = account.instagram_metadata()
            response = account.instagram.http.get(
                f"{FACEBOOK_GRAPH_API}/{meta['ig_user_id']}",
                params={"fields": "username"},
                headers={"Authorization": f"Bearer {account.instagram_token()}"},
                timeout=30,
            )
            response.raise_for_status()
            print(f"{account.name} Instagram: @" + response.json().get("username", "?"))


def main() -> None:
    if sys.argv[1:] == ["status"]:
        status()
        return
    if sys.argv[1:] == ["check"]:
        check()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for directory in (INCOMING, MEDIA):
        directory.mkdir(parents=True, exist_ok=True)
    accounts = Accounts()
    log.info("Poster running: YouTube %d/day up to %s ahead, Instagram gap %s",
             YOUTUBE_DAILY_UPLOADS, YOUTUBE_LEAD, INSTAGRAM_MIN_GAP)
    while True:
        try:
            tick(accounts)
        except Exception:  # noqa: BLE001
            log.exception("Tick failed")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
