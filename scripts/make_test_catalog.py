"""Create clearly labelled synthetic audio fixtures for offline browser tests."""
import json
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

directory = Path(sys.argv[1])
(directory / "media").mkdir(parents=True, exist_ok=True)
media = "test-tone.wav"
rate = 8000
with wave.open(str(directory / "media" / "test-tone.wav"), "wb") as output:
    output.setparams((1, 2, rate, 0, "NONE", "not compressed"))
    output.writeframes(b"".join(struct.pack("<h", int(1200 * math.sin(2 * math.pi * (330 if i // rate % 2 else 440) * i / rate))) for i in range(rate * 40)))
if shutil.which("ffmpeg"):
    media = "test-video.webm"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
        "-i", str(directory / "media" / "test-tone.wav"), "-t", "40",
        "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "80k",
        "-c:a", "libopus", "-b:a", "16k", str(directory / "media" / media)], check=True)
catalog = [
    {"anime": "Cowboy Bebop", "aliases": ["Bebop"], "song": "Offline test tone A", "artist": "Synthetic QA fixture", "type": "OP", "media": media, "year": 1998},
    {"anime": "Attack on Titan", "aliases": ["Shingeki no Kyojin"], "song": "Offline test tone B", "artist": "Synthetic QA fixture", "type": "ED", "media": media, "year": 2013},
]
(directory / "catalog.json").write_text(json.dumps(catalog))
