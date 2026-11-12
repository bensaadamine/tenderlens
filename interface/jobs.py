"""Long commands started from the page: one at a time, each in its own
process exactly as if typed by hand (python tenderlens.py ...). The output
goes to reports/jobs/<stamp>_<name>.log, which the page shows as it grows.
"""

import os
import re
import subprocess
import sys
import threading
import time

LOG_DIR = "reports/jobs"
_job = {}            # the command started last: id, args, proc


def running():
    return bool(_job) and _job["proc"].poll() is None


def current():
    return _job.get("id") if running() else None


def start(name, args):
    """Start `python tenderlens.py <args>`; its id, or None if a command is
    already running."""
    if running():
        return None
    os.makedirs(LOG_DIR, exist_ok=True)
    job_id = time.strftime("%Y%m%d-%H%M%S") + "_" + name
    log = os.path.join(LOG_DIR, job_id + ".log")
    with open(log, "w", encoding="utf-8") as out:
        out.write("> python tenderlens.py " + " ".join(args) + "\n\n")
        out.flush()
        proc = subprocess.Popen(
            [sys.executable, "tenderlens.py", *args], stdout=out,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED="1",
                     PYTHONIOENCODING="utf-8"))
    _job.clear()
    _job.update(id=job_id, args=args, proc=proc)
    threading.Thread(target=_finish, args=(proc, log), daemon=True).start()
    return job_id


def _finish(proc, log):
    code = proc.wait()
    with open(log, "a", encoding="utf-8") as f:
        f.write("\n[finished]\n" if code == 0 else
                f"\n[stopped: exit code {code}]\n")


def stop():
    """Stop the running command and the commands it started."""
    if not running():
        return
    pid = _job["proc"].pid
    if os.name == "nt":       # terminate() would leave its children running
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True)
    else:
        _job["proc"].terminate()


def output(job_id, tail=40000):
    """(the end of a command's output, whether it is still running); None
    for an unknown id."""
    path = os.path.join(LOG_DIR, job_id + ".log")
    if not re.fullmatch(r"[\w-]+", job_id) or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return text[-tail:], current() == job_id


def recent(n=8):
    """The last commands started, newest first."""
    if not os.path.isdir(LOG_DIR):
        return []
    return sorted((f[:-4] for f in os.listdir(LOG_DIR) if f.endswith(".log")),
                  reverse=True)[:n]
