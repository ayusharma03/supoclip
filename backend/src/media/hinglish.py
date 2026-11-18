"""Roman Hinglish for Hindi transcripts, keeping every word's timing.

Whisper in Hindi mode writes Hindi in Devanagari and English words in Latin.
Each distinct Devanagari word is transliterated once (by the configured LLM)
into casual Hinglish spelling and remembered in a lexicon, so spellings stay
consistent and later videos only ask about words never seen before.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List

from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEVANAGARI_WORD = re.compile(r"[ऀ-ॣ०-ॿ]+")
DANDA = re.compile(r"[।॥]")
DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
ROMAN_WORD = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")
BATCH_SIZE = 120
LEXICON_FILE = "hinglish_lexicon.json"

SYSTEM_PROMPT = """You convert Hindi words written in Devanagari into casual Roman Hinglish,
spelled the way Indians type in chats and Instagram captions.

Rules:
- Spell common words the usual chat way: है -> hai, हैं -> hain, में -> mein, नहीं -> nahi,
  क्या -> kya, हूँ -> hu, मैं -> main, रहे -> rahe, करते -> karte, होते -> hote, यार -> yaar,
  बहुत -> bahut, क्योंकि -> kyunki, अच्छा -> accha, ज़िंदगी -> zindagi, चाहिए -> chahiye.
- Use no accents or diacritics, only plain a-z letters. Lowercase.
- If the word is an English word written in Devanagari, return the normal English spelling:
  गिटार -> guitar, बिज़नेस -> business, लाइफ -> life, प्रॉब्लम -> problem.
- Return exactly one entry per numbered input word, with the same number. One word in, one word out.
/no_think"""


class RomanWord(BaseModel):
    index: int
    roman: str


class RomanBatch(BaseModel):
    words: List[RomanWord]


Transliterate = Callable[[List[str]], Dict[str, str]]


def devanagari_words(texts: Iterable[str]) -> List[str]:
    seen: Dict[str, None] = {}
    for text in texts:
        for word in DEVANAGARI_WORD.findall(text or ""):
            seen.setdefault(word, None)
    return list(seen)


def romanize_text(text: str, lexicon: Dict[str, str]) -> str:
    # Whisper can split one Devanagari character across two word tokens, leaving U+FFFD.
    text = DANDA.sub(".", (text or "").replace("�", "").translate(DEVANAGARI_DIGITS))
    return DEVANAGARI_WORD.sub(lambda m: lexicon.get(m.group(0), m.group(0)), text)


def _valid_roman(source: str, roman: str) -> bool:
    return bool(ROMAN_WORD.match(roman)) and len(roman) <= 3 * len(source) + 6


def fill_lexicon(words: List[str], lexicon: Dict[str, str], transliterate: Transliterate) -> int:
    """Add romanizations for unknown words; returns how many were added."""
    missing = [w for w in words if w not in lexicon]
    added = 0
    for start in range(0, len(missing), BATCH_SIZE):
        batch = missing[start : start + BATCH_SIZE]
        try:
            result = transliterate(batch)
        except Exception as exc:  # noqa: BLE001 - unconverted words stay readable Devanagari
            logger.warning("Hinglish batch of %d words failed (%s)", len(batch), type(exc).__name__)
            continue
        for word in batch:
            roman = (result.get(word) or "").strip().lower()
            if _valid_roman(word, roman):
                lexicon[word] = roman
                added += 1
    return added


def romanize_whisper_result(result: dict, lexicon: Dict[str, str], transliterate: Transliterate) -> dict:
    """Rewrite a Whisper result's text, segments and words into Roman Hinglish in place."""
    segments = result.get("segments") or []
    texts = [result.get("text", "")]
    for segment in segments:
        texts.append(segment.get("text", ""))
        texts.extend(w.get("word", "") for w in segment.get("words") or [])
    words = devanagari_words(texts)
    added = fill_lexicon(words, lexicon, transliterate)
    for segment in segments:
        segment["text"] = romanize_text(segment.get("text", ""), lexicon)
        for word in segment.get("words") or []:
            word["word"] = romanize_text(word.get("word", ""), lexicon)
    result["text"] = romanize_text(result.get("text", ""), lexicon)
    unconverted = sum(1 for w in words if w not in lexicon)
    logger.info(
        "Hinglish: %d distinct Hindi words, %d newly transliterated, %d left in Devanagari",
        len(words), added, unconverted,
    )
    return result


def load_lexicon(directory: Path) -> Dict[str, str]:
    path = directory / LEXICON_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def save_lexicon(directory: Path, lexicon: Dict[str, str]) -> None:
    # A per-machine cache: losing it only costs re-asking the model.
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / LEXICON_FILE
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(lexicon, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _to_mapping(words: List[str], output: RomanBatch) -> Dict[str, str]:
    return {words[item.index - 1]: item.roman for item in output.words if 1 <= item.index <= len(words)}


def _prompt(words: List[str]) -> str:
    return "\n".join(f"{i}. {w}" for i, w in enumerate(words, 1))


def llm_transliterator(model_name: str) -> Transliterate:
    from ..config import get_config

    config = get_config()
    provider, _, ollama_model = model_name.partition(":")
    if provider == "ollama":
        return _ollama_transliterator(config, ollama_model)

    from pydantic_ai import Agent

    from ..ai import _build_transcript_model

    agent = Agent[None, RomanBatch](
        model=_build_transcript_model(config, model_name),
        output_type=RomanBatch,
        system_prompt=SYSTEM_PROMPT,
        output_retries=2,
    )
    return lambda words: _to_mapping(words, agent.run_sync(_prompt(words)).output)


def _ollama_transliterator(config, model: str) -> Transliterate:
    # Ollama's native API can switch reasoning off; through the OpenAI-compatible
    # endpoint qwen3 thinks before every batch and runs ~10x slower.
    import httpx

    base = config.resolve_ollama_base_url().rstrip("/").removesuffix("/v1")
    headers = {"Authorization": f"Bearer {config.ollama_api_key}"} if config.ollama_api_key else {}
    client = httpx.Client(base_url=base, headers=headers, timeout=600)

    def transliterate(words: List[str]) -> Dict[str, str]:
        response = client.post(
            "/api/chat",
            json={
                "model": model,
                "stream": False,
                "think": False,
                "format": RomanBatch.model_json_schema(),
                # A batch fits in 4K tokens; a small context keeps a 14B model fully on a 12GB GPU.
                "options": {"temperature": 0, "num_ctx": 4096},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _prompt(words)},
                ],
            },
        )
        response.raise_for_status()
        return _to_mapping(words, RomanBatch.model_validate_json(response.json()["message"]["content"]))

    return transliterate
