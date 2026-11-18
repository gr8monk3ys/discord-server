"""Every file the running bot reads from outside bot/ and server/ ships in the Docker image.

The tests run from the full checkout, so they pass even when .dockerignore leaves such a file
out of the image; the bot then runs without it in production (as the own-invite allowlist once
did). This imports what the bot imports, in a fresh interpreter, and checks every module-level
Path that points into the repo but outside bot/ and server/.
"""

import json
import subprocess
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parents[1]
ROOT = BOT.parent

PROBE = """
import importlib, json, sys
from pathlib import Path
import main
for ext, _ in main.MODULES:
    importlib.import_module(ext)
bot = Path.cwd().resolve()
root = bot.parent
found = set()
for module in list(sys.modules.values()):
    file = getattr(module, "__file__", None)
    if not file or not Path(file).resolve().is_relative_to(bot):
        continue
    for name, value in vars(module).items():
        if isinstance(value, Path):
            p = value.resolve()
            if p.is_relative_to(root) and not (p.is_relative_to(bot) or p.is_relative_to(root / "server")):
                found.add((module.__name__ + "." + name, p.relative_to(root).as_posix()))
print(json.dumps(sorted(found)))
"""


def outside_files() -> list[tuple[str, str]]:
    run = subprocess.run([sys.executable, "-c", PROBE], cwd=BOT, capture_output=True, text=True)
    assert run.returncode == 0, f"importing the bot's modules failed:\n{run.stderr}"
    return [tuple(pair) for pair in json.loads(run.stdout)]


def test_files_the_bot_reads_from_outside_bot_and_server_are_in_the_image():
    allowed = {line.strip() for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    for where, rel in outside_files():
        assert f"!{rel}" in allowed, (
            f"{where} reads {rel}, which .dockerignore leaves out of the image: "
            f"add the line !{rel} to .dockerignore and COPY {rel} {rel} to the Dockerfile"
        )
        assert f"COPY {rel} {rel}" in dockerfile, (
            f"{where} reads {rel}, which the Dockerfile does not copy into /app: "
            f"add COPY {rel} {rel} next to COPY bot/ bot/"
        )
