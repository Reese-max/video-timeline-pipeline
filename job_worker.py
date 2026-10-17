"""One operation per process: parent death, cancellation and deadline kill descendants."""
from __future__ import annotations
import os
from pathlib import Path
import signal
import sys
import threading
import time
import json

from job_queue import atomic_json, report_progress


def terminate_self():
    if os.name != "nt":
        os.killpg(os.getpgrp(), signal.SIGKILL)
    else:
        import subprocess
        subprocess.run(["taskkill", "/PID", str(os.getpid()), "/T", "/F"], capture_output=True)
        os._exit(1)


def watch_parent():
    # Avoid holding BufferedReader's lock during interpreter shutdown.
    while os.read(sys.stdin.fileno(), 1):
        pass
    terminate_self()


def watch_deadline():
    remaining = float(os.environ["VIDEO_JOB_DEADLINE"]) - time.time()
    if remaining > 0: time.sleep(remaining)
    terminate_self()


def main():
    work = Path(sys.argv[1]).resolve()
    threading.Thread(target=watch_parent, daemon=True).start()
    threading.Thread(target=watch_deadline, daemon=True).start()
    import mcp_server as server
    task = json.loads((work / "input.json").read_text(encoding="utf-8"))
    try:
        operation, args = task["operation"], task["inputs"]
        delegation = (work, task["parent_writer_lease"]) if "parent_writer_lease" in task else None
        def run(operation):
            return server.run_job(operation, _parent_writer=delegation) if delegation is not None else server.run_job(operation)
        report_progress("starting", 1, "開始處理")
        if operation == "prepare":
            result = run(lambda: server.tool_payload(server.prepare_video_sync(**args)))
        elif operation == "analyze":
            result = run(lambda: server.analyze_prepared_sync(**args))
        elif operation == "drive":
            result = run(lambda: server.upload_prepared_drive_sync(**args))
        elif operation == "report":
            result = run(lambda: server.upload_analysis_report_sync(**args))
        else: raise ValueError("Unknown operation")
        report_progress("completed", 100, "處理完成")
        atomic_json(work / "output.json", {"ok": True, "result": result})
    except Exception as exc:
        atomic_json(work / "output.json", {"ok": False, "error": server.analysis_error(exc),
                    "expired": type(exc).__name__ == "DeadlineExceeded"})


if __name__ == "__main__": main()
