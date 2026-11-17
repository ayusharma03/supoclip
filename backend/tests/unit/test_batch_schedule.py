import random
from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.social import batch_schedule as bs

IST = ZoneInfo("Asia/Kolkata")
SLOTS = bs.parse_slots("12:00,17:00,20:00,22:00")


def podcast(name, count):
    return bs.Podcast(name, name, [{"id": f"{name}-{i}", "hook_title": f"{name} {i}"} for i in range(count)])


def plan(podcasts, jitter=30, seed=1):
    return bs.plan_schedule(
        podcasts, start=date(2026, 11, 20), slots=SLOTS, tz=IST, jitter_minutes=jitter, rng=random.Random(seed)
    )


def test_each_day_posts_the_next_clip_from_every_podcast():
    result = plan([podcast("a", 3), podcast("b", 3), podcast("c", 3), podcast("d", 3)])

    by_day = {}
    for item in result:
        by_day.setdefault(item.when.date(), []).append(item.clip["id"])
    assert len(by_day) == 3
    for day, (date_key, ids) in enumerate(sorted(by_day.items())):
        assert sorted(ids) == [f"{name}-{day}" for name in "abcd"]


def test_daily_order_is_shuffled():
    result = plan([podcast(name, 8) for name in "abcd"], jitter=0)

    orders = set()
    for day in range(8):
        todays = [i.podcast.task_id for i in result if i.when.date() == date(2026, 11, 20) + timedelta(days=day)]
        orders.add(tuple(todays))
    assert len(orders) > 1


def test_times_stay_within_jitter_of_their_slot():
    result = plan([podcast(name, 5) for name in "abcd"], jitter=30)

    for item in result:
        nearest = min(
            abs((item.when - item.when.replace(hour=s.hour, minute=s.minute)).total_seconds()) for s in SLOTS
        )
        assert nearest <= 30 * 60


def test_podcasts_with_fewer_clips_drop_out_and_slots_spread_evenly():
    result = plan([podcast("a", 3), podcast("b", 1)], jitter=0)

    day_two = [i for i in result if i.when.date() == date(2026, 11, 22)]
    assert [i.clip["id"] for i in day_two] == ["a-2"]
    assert day_two[0].when.time() in (time(17, 0), time(20, 0))
    first_day = sorted(i.when.time() for i in result if i.when.date() == date(2026, 11, 20))
    assert first_day == [time(12, 0), time(22, 0)]


def test_more_podcasts_than_slots_is_rejected():
    with pytest.raises(ValueError):
        bs.spread(SLOTS, 5)


def test_pick_clips_skips_posted_and_unrendered_and_ranks_by_virality():
    clips = [
        {"id": "low", "virality_score": 40, "video_url": "/c/1"},
        {"id": "posted", "virality_score": 99, "video_url": "/c/2"},
        {"id": "high", "virality_score": 90, "video_url": "/c/3"},
        {"id": "missing", "virality_score": 95, "video_url": None},
    ]

    assert [c["id"] for c in bs.pick_clips(clips, {"posted"}, 5)] == ["high", "low"]
    assert [c["id"] for c in bs.pick_clips(clips, set(), 1)] == ["posted"]


def test_quote_takes_whole_sentences_and_caps_length():
    assert bs.quote_from("Short one. This second sentence makes it long enough to stop here. Third.") == (
        "Short one. This second sentence makes it long enough to stop here."
    )
    long_quote = bs.quote_from("word " * 100)
    assert len(long_quote) <= bs.QUOTE_MAX_CHARS + 1 and long_quote.endswith("…")


def test_instagram_caption_leads_with_hook_title():
    clip = {"hook_title": "Decide once", "text": "Decide once what you want and stop playing the game twice."}

    instagram = bs.build_caption(clip, "Some Podcast", "instagram", "Footer")
    youtube = bs.build_caption(clip, "Some Podcast", "youtube", "Footer")

    assert instagram.startswith("Decide once\n\n“")
    assert youtube.startswith("“Decide once what you want")
    assert "From: Some Podcast" in youtube and youtube.endswith("Footer")
