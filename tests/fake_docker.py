"""A stand-in for `docker` (no daemon needed): `info`, `compose up -d/stop/logs`.

`up` starts a detached TCP listener on the configured port (the "container"); `stop`
kills it. Settings come from fake_docker.json next to the script.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

here = Path(__file__).parent
settings = json.loads((here / "fake_docker.json").read_text())
args = sys.argv[1:]
with Path(settings["log"]).open("a") as log:
    log.write(json.dumps(args) + "\n")
pidfile = here / "container.pid"
if args[:1] == ["info"]:
    print("27.0.0")
elif args[:1] == ["compose"] and "up" in args:
    server = (
        "import socket,time\n"
        "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {int(settings['port'])})); s.listen(8)\n"
        "while True:\n    c,_=s.accept(); c.close()\n"
    )
    proc = subprocess.Popen(  # like a container: detached, not holding the caller's pipes
        [sys.executable, "-c", server],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pidfile.write_text(str(proc.pid))
elif args[:1] == ["compose"] and ("stop" in args or "down" in args):
    if pidfile.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pidfile.read_text()), signal.SIGTERM)
        pidfile.unlink()
elif args[:1] == ["compose"] and "logs" in args:
    print("queue  | listening")
elif len(args) > 1 and args[1] == "prune":
    print("Total reclaimed space: 0B")
else:
    sys.exit(2)
