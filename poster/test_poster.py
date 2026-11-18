"""Run inside the poster image: python -m pytest test_poster.py"""

from datetime import datetime, timedelta, timezone

import poster

AT = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def post(i, provider, hours, **extra):
    return {"id": f"{provider}-{i}", "provider": provider, "status": "pending", "attempts": 0,
            "scheduled_for": (AT + timedelta(hours=hours)).isoformat(), **extra}


def ids(chosen):
    return [p["id"] for p in chosen]


def test_youtube_uploads_ahead_within_the_daily_budget():
    queue = [post(i, "youtube", 10 + i) for i in range(8)]
    assert ids(poster.pick(queue, AT)) == [f"youtube-{i}" for i in range(5)]


def test_recent_uploads_and_due_posts_use_the_budget():
    queue = [post(0, "youtube", -1)] + [post(i, "youtube", 10 + i) for i in range(1, 6)]
    queue += [dict(post(9, "youtube", -30), status="published", uploaded_at=(AT - timedelta(hours=2)).isoformat())] * 3
    # 3 uploads in the last 24h + 1 due post leaves room for 1 early upload.
    assert ids(poster.pick(queue, AT)) == ["youtube-0", "youtube-1"]


def test_due_youtube_posts_go_even_over_budget():
    queue = [dict(post(9, "youtube", -30), status="published", uploaded_at=(AT - timedelta(hours=1)).isoformat())] * 6
    queue.append(post(0, "youtube", -1))
    assert ids(poster.pick(queue, AT)) == ["youtube-0"]


def test_instagram_posts_only_when_due_and_one_at_a_time():
    queue = [post(0, "instagram", -5), post(1, "instagram", -3), post(2, "instagram", 2)]
    assert ids(poster.pick(queue, AT)) == ["instagram-0"]


def test_missed_instagram_posts_are_spaced_out():
    queue = [dict(post(0, "instagram", -5), status="published", uploaded_at=(AT - timedelta(minutes=5)).isoformat()),
             post(1, "instagram", -3)]
    assert poster.pick(queue, AT) == []
    later = AT + poster.INSTAGRAM_MIN_GAP
    assert ids(poster.pick(queue, later)) == ["instagram-1"]


def test_posts_waiting_to_retry_are_skipped():
    queue = [post(0, "instagram", -1, retry_at=(AT + timedelta(minutes=5)).isoformat())]
    assert poster.pick(queue, AT) == []


def test_merged_upload_records_count_against_the_budget(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(poster, "INCOMING", tmp_path)
    monkeypatch.setattr(poster, "MEDIA", tmp_path)
    (tmp_path / "a.mp4").write_bytes(b"x")
    recent = (AT - timedelta(hours=3)).isoformat()
    batch = [dict(post(i, "youtube", 10 + i), file="a.mp4") for i in range(6)]
    batch += [{"id": f"supoclip-{i}", "provider": "youtube", "status": "published",
               "scheduled_for": recent, "uploaded_at": recent} for i in range(4)]
    (tmp_path / "b.json").write_text(json.dumps(batch))
    queue = []
    assert poster.merge_incoming(queue) == 10
    assert ids(poster.pick(queue, AT)) == ["youtube-0"]


def test_each_account_has_its_own_youtube_budget_and_instagram_gap():
    recent = (AT - timedelta(hours=2)).isoformat()
    queue = [dict(post(9, "youtube", -30), status="published", uploaded_at=recent)] * 5  # viralpods used up
    queue += [post(i, "youtube", 10 + i) for i in range(2)]
    queue += [dict(post(10 + i, "youtube", 10 + i), account="lossantosbhai") for i in range(2)]
    queue += [dict(post(20, "instagram", -1), status="published", uploaded_at=(AT - timedelta(minutes=1)).isoformat()),
              post(21, "instagram", -1), dict(post(22, "instagram", -1), account="lossantosbhai")]
    assert sorted(ids(poster.pick(queue, AT))) == ["instagram-22", "youtube-10", "youtube-11"]
