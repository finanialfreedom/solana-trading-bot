"""Render the STONK case study as Egyptian Arabic speech using edge-tts neural voices."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import truststore

# Route TLS through the Windows certificate store so Norton's TLS inspection
# (which swaps in its own root CA) does not break certificate verification.
truststore.inject_into_ssl()

import edge_tts  # noqa: E402  (must import after ssl is patched)

TEXT_PATH = Path.home() / "AppData/Local/Temp/stonk_ar_eg.txt"
OUT_PATH = Path.home() / "AppData/Local/Temp/stonk_ar_eg.mp3"

# Egyptian Arabic (ar-EG) neural voices. Salma is the female one.
PREFERRED = ["ar-EG-SalmaNeural", "ar-EG-ShakirNeural"]


async def list_arabic_voices() -> list[dict]:
    voices = await edge_tts.list_voices()
    return [v for v in voices if str(v.get("Locale", "")).startswith("ar")]


def pick_voice(voices: list[dict]) -> str:
    eg = [v for v in voices if v.get("Locale") == "ar-EG"]
    print("--- ar-EG voices available ---")
    for v in eg:
        print(f"  {v['ShortName']:<26} {v.get('Gender'):<8} {v.get('VoiceTag', {}).get('ContentCategories')}")
    for name in PREFERRED:
        if any(v["ShortName"] == name for v in eg):
            return name
    if eg:
        female = [v for v in eg if v.get("Gender") == "Female"]
        return (female or eg)[0]["ShortName"]
    raise SystemExit("no ar-EG voice available")


async def main() -> None:
    voices = await list_arabic_voices()
    all_ar = sorted({v["Locale"] for v in voices})
    print(f"arabic locales offered by edge-tts: {', '.join(all_ar)}")

    voice = pick_voice(voices)
    print(f"selected voice: {voice}")

    text = TEXT_PATH.read_text(encoding="utf-8")
    print(f"text chars: {len(text)}")

    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(OUT_PATH))

    size = OUT_PATH.stat().st_size
    print(f"audio written: {OUT_PATH}  ({size:,} bytes)")
    if size < 10_000:
        raise SystemExit("audio suspiciously small - render likely failed")

    # Print word boundaries count as a rough sanity check on the synthesis.
    words = 0
    submaker = edge_tts.SubMaker()
    async for chunk in edge_tts.Communicate(text, voice).stream():
        if chunk["type"] == "WordBoundary":
            submaker.feed(chunk)
            words += 1
    print(f"word boundaries: {words}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
