"""Move SupoClip's scheduled posts to the always-on poster (run on the clip machine).

    python poster/handoff.py --host ayush@192.168.1.60 [--key ~/.ssh/homelab_ed25519] [--apply]

Dry run by default. With --apply it:
1. cancels the scheduled posts in SupoClip first (so its worker can never post
   them too), keeping the ids it actually cancelled;
2. copies each post's clip into the poster's media folder and drops the posts
   as one batch into its incoming folder;
3. restores the posts to ``scheduled`` in SupoClip if anything in 2 fails.

Needs the SupoClip containers running locally (docker exec/cp) and SSH key access.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

PSQL = ["docker", "exec", "-i", "supoclip-postgres", "psql", "-U", "supoclip", "-d", "supoclip", "-t", "-A", "-q"]
DEFAULT_REMOTE_DATA = "/mnt/data/supoclip-poster"
MOVED = "Moved to the always-on poster."


def sql(query: str) -> str:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    return subprocess.run(PSQL, input=query, capture_output=True, encoding="utf-8", check=True, env=env).stdout.strip()


def select_posts() -> list[dict]:
    rows = sql(
        """
        SELECT coalesce(json_agg(row_to_json(x) ORDER BY x.scheduled_for), '[]') FROM (
          SELECT p.id, p.provider, p.title, p.caption, p.hashtags, p.privacy_level,
                 p.scheduled_for, c.file_path
          FROM social_posts p JOIN generated_clips c ON c.id = p.clip_id
          WHERE p.status = 'scheduled'
        ) x;
        """
    )
    return json.loads(rows)


def recent_youtube_uploads() -> list[dict]:
    """SupoClip's YouTube uploads of the last 24h, so the poster's quota budget counts them."""
    rows = sql(
        """
        SELECT coalesce(json_agg(json_build_object(
          'id', 'supoclip-' || id, 'provider', 'youtube', 'status', 'published',
          'title', title, 'scheduled_for', coalesce(scheduled_for, claimed_at), 'uploaded_at', claimed_at)), '[]')
        FROM social_posts WHERE provider = 'youtube' AND claimed_at > NOW() - interval '24 hours';
        """
    )
    return json.loads(rows)


def set_status(ids: list[str], old: str, new: str, message: str | None) -> list[str]:
    if not ids:
        return []
    id_list = ",".join("'" + i.replace("'", "") + "'" for i in ids)
    note = "NULL" if message is None else "'" + message.replace("'", "''") + "'"
    out = sql(
        f"UPDATE social_posts SET status = '{new}', error_message = {note}, updated_at = NOW() "
        f"WHERE id IN ({id_list}) AND status = '{old}' RETURNING id;"
    )
    return [line for line in out.splitlines() if line]


def run(args: list[str]) -> None:
    subprocess.run(args, check=True, env={**os.environ, "MSYS_NO_PATHCONV": "1"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="user@host of the poster machine")
    parser.add_argument("--key", default=str(Path.home() / ".ssh" / "homelab_ed25519"))
    parser.add_argument("--remote-data", default=DEFAULT_REMOTE_DATA, help="POSTER_DATA_DIR on the poster machine")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    REMOTE_DATA = args.remote_data
    ssh = ["ssh", "-o", "BatchMode=yes", "-i", args.key, args.host]
    scp = ["scp", "-q", "-o", "BatchMode=yes", "-i", args.key]

    posts = select_posts()
    for post in posts:
        print(f"{post['scheduled_for'][:16]}  {post['provider']:<9} {(post['title'] or '')[:60]}")
    print(f"{len(posts)} scheduled posts")
    if not args.apply or not posts:
        if posts:
            print("Dry run. Re-run with --apply to move them.")
        return 0

    moved = set(set_status([p["id"] for p in posts], "scheduled", "cancelled", MOVED))
    posts = [p for p in posts if p["id"] in moved]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            files = {}
            for post in posts:
                source = PurePosixPath(post["file_path"])
                name = f"{source.stem}{source.suffix}"
                if name not in files:
                    run(["docker", "cp", f"supoclip-worker:{source}", str(staging / name)])
                    files[name] = staging / name
                post["file"] = name
            run(ssh + [f"mkdir -p {REMOTE_DATA}/media {REMOTE_DATA}/incoming"])
            for name, path in files.items():
                run(scp + [str(path), f"{args.host}:{REMOTE_DATA}/media/{name}"])
            batch = [
                {k: post[k] for k in ("id", "provider", "title", "caption", "hashtags", "privacy_level", "scheduled_for", "file")}
                for post in posts
            ] + recent_youtube_uploads()
            batch_path = staging / "batch.json"
            batch_path.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
            remote_tmp = f"{REMOTE_DATA}/incoming/.batch-{os.getpid()}.tmp"
            run(scp + [str(batch_path), f"{args.host}:{remote_tmp}"])
            run(ssh + [f"mv {shlex.quote(remote_tmp)} {REMOTE_DATA}/incoming/batch-$(date +%s)-{os.getpid()}.json"])
    except Exception:
        restored = set_status(sorted(moved), "cancelled", "scheduled", None)
        print(f"Handoff failed; {len(restored)} posts are scheduled in SupoClip again.", file=sys.stderr)
        raise
    print(f"Moved {len(posts)} posts ({len(files)} clips) to {args.host}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
