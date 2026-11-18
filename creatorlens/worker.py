"""Isolated Spider_XHS runtime. Imports never start a capture."""
import argparse
import json
import os
import sys

from .collection import collect, verify
from .dependency import validate
from .spider import connect
from .storage import StopCapture, ensure
from .tasks import Task


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    output = sys.stdout

    def emit(event, **fields):
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), file=output, flush=True)

    task = None
    os.umask(0o077)
    try:
        task = Task(args.workspace, args.task)
        with task.lock():
            state = task.get("state.json")
            ensure(not state or args.resume, "existing_task_requires_explicit_resume")
            ensure(not state or state.get("task_hash") == task.fingerprint, "task_changed_after_capture_started")
            if task.p("capture.json").exists():
                emit("collection_verified", **verify(task))
                return 0
            source, _, source_hash = validate(task.workspace)
            previous = task.get("runtime.json")
            ensure(not previous or previous.get("source_hash") == source_hash, "dependency_changed_within_batch")
            task.save("runtime.json", {"provider": "spider_xhs", "source_hash": source_hash})
            with connect(task, source, emit) as provider:
                result = collect(task, provider, resume=args.resume)
            emit("collection_verified", **result)
        return 0
    except KeyboardInterrupt:
        emit("stopped", reason="interrupted_check_status_before_resume")
        return 130
    except Exception as exc:
        reason = str(exc) if isinstance(exc, StopCapture) else "external_runtime_error"
        # No upstream exception bodies, auth values, signed URLs or response text.
        emit("stopped", reason=reason, error_type=type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
