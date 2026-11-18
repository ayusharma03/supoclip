from src.media import hinglish

TABLE = {"मेरा": "mera", "नाम": "naam", "है": "hai", "और": "aur", "गिटार": "guitar"}


def fake_transliterate(calls):
    def transliterate(words):
        calls.append(list(words))
        return {w: TABLE[w] for w in words if w in TABLE}

    return transliterate


def whisper_result():
    return {
        "text": " मेरा नाम Prakhar है। और guitar",
        "language": "hi",
        "segments": [
            {
                "text": " मेरा नाम Prakhar है।",
                "start": 0.0,
                "end": 2.0,
                "words": [
                    {"word": " मेरा", "start": 0.0, "end": 0.4},
                    {"word": " नाम", "start": 0.4, "end": 0.8},
                    {"word": " Prakhar", "start": 0.8, "end": 1.4},
                    {"word": " है।", "start": 1.4, "end": 2.0},
                ],
            },
            {
                "text": " और गिटार",
                "start": 2.0,
                "end": 3.0,
                "words": [{"word": " और", "start": 2.0, "end": 2.4}, {"word": " गिटार", "start": 2.4, "end": 3.0}],
            },
        ],
    }


def test_words_become_hinglish_with_timings_unchanged():
    result = whisper_result()
    lexicon = {}

    hinglish.romanize_whisper_result(result, lexicon, fake_transliterate([]))

    first = result["segments"][0]
    assert [w["word"] for w in first["words"]] == [" mera", " naam", " Prakhar", " hai."]
    assert [(w["start"], w["end"]) for w in first["words"]] == [(0.0, 0.4), (0.4, 0.8), (0.8, 1.4), (1.4, 2.0)]
    assert first["text"] == " mera naam Prakhar hai."
    assert result["segments"][1]["text"] == " aur guitar"
    assert result["text"] == " mera naam Prakhar hai. aur guitar"


def test_each_distinct_word_is_asked_once_and_remembered():
    calls = []
    lexicon = {"है": "hai"}

    hinglish.romanize_whisper_result(whisper_result(), lexicon, fake_transliterate(calls))
    hinglish.romanize_whisper_result(whisper_result(), lexicon, fake_transliterate(calls))

    assert len(calls) == 1
    assert sorted(calls[0]) == sorted(["मेरा", "नाम", "और", "गिटार"])
    assert lexicon["गिटार"] == "guitar"


def test_bad_or_missing_answers_leave_the_word_in_devanagari():
    def unreliable(words):
        return {"मेरा": "mérā", "नाम": "naam with spaces", "है": "hai"}

    lexicon = {}
    assert hinglish.romanize_text("मेरा नाम है", lexicon) == "मेरा नाम है"
    hinglish.fill_lexicon(["मेरा", "नाम", "है", "और"], lexicon, unreliable)

    assert lexicon == {"है": "hai"}
    assert hinglish.romanize_text("मेरा नाम है", lexicon) == "मेरा नाम hai"


def test_failed_batch_is_skipped_not_fatal():
    def broken(words):
        raise RuntimeError("model down")

    lexicon = {}
    assert hinglish.fill_lexicon(["है"], lexicon, broken) == 0
    assert lexicon == {}


def test_large_inputs_are_sent_in_batches(monkeypatch):
    monkeypatch.setattr(hinglish, "BATCH_SIZE", 2)
    calls = []

    hinglish.fill_lexicon(["मेरा", "नाम", "है", "और", "गिटार"], {}, fake_transliterate(calls))

    assert [len(c) for c in calls] == [2, 2, 1]


def test_devanagari_digits_and_danda_are_normalized():
    assert hinglish.romanize_text("२०२६ में।", {"में": "mein"}) == "2026 mein."


def test_split_character_replacement_marks_are_dropped():
    assert hinglish.romanize_text(" � umar", {}) == "  umar"


def test_lexicon_round_trips_through_disk(tmp_path):
    hinglish.save_lexicon(tmp_path, {"है": "hai"})

    assert hinglish.load_lexicon(tmp_path) == {"है": "hai"}
    assert hinglish.load_lexicon(tmp_path / "missing") == {}
