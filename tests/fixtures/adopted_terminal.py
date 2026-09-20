"""A raw terminal harness for adoption tests; executes only the fixed chat CLI."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import termios
import tty

root = Path(sys.argv[1])
old = termios.tcgetattr(0)
tty.setraw(0)
print("\x1b[?2004hREADY", flush=True)
buffer = ""
try:
    while True:
        chunk = os.read(0, 65536).decode()
        if not chunk:
            break
        buffer += chunk
        if "\r" not in buffer:
            continue
        prompt, buffer = buffer.split("\r", 1)
        prompt = prompt.replace("\x1b[200~", "").replace("\x1b[201~", "")
        with (root / "notifications.jsonl").open("a") as log:
            log.write(json.dumps(prompt) + "\n")
        command = prompt.split("Read the addressed conversation using: ", 1)[1].split(". Respond in that conversation", 1)[0]
        argv = shlex.split(command)
        assert argv[:4] == [sys.argv[2], "-m", "agentchattr", "chat"], argv
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=True)
        (root / "read-result").write_text(result.stdout)
        argv[argv.index("read")] = "send"
        subprocess.run([*argv, "--message", "Adopted terminal replied"], check=True, capture_output=True, timeout=10)
finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
