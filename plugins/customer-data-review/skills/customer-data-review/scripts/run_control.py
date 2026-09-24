"""Durable, single-writer run budget and checkpoint ledger. No model calls."""

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

VERSION = 1
MAX_IN_FLIGHT = 4
MAX_ATTEMPTS = 3  # initial work plus two repairs for any key
REQUIRED_STEPS = ("phase-1", "phase-2-merge", "audit", "phase-3", "report", "opportunities")


def validate_execution_mode(request):
    """Reject misspelled new modes instead of silently choosing combined work."""
    from skill.scripts.bounded_audit import min_pass_percent
    min_pass_percent(request)
    profile = request.get("execution_profile", "interactive")
    if profile not in ("interactive", "direct_batches_v1"):
        raise ValueError("Unknown execution_profile.")
    report_style = request.get("report_style", "legacy")
    if report_style not in ("legacy", "concise_v1"):
        raise ValueError("Unknown report_style; use concise_v1 or legacy.")
    mode = request.get("downstream_execution", "combined")
    if mode not in ("combined", "question_partitions_v1"):
        raise ValueError("Unknown downstream_execution; use combined or question_partitions_v1.")
    if report_style == "concise_v1" and (profile != "interactive" or mode != "combined"
                                        or request.get("writing_mode") != "section_packets_v1"):
        raise ValueError("concise_v1 requires interactive combined section_packets_v1 workflow.")
    if mode == "question_partitions_v1" and request.get("writing_mode") != "section_packets_v1":
        raise ValueError("question_partitions_v1 requires section_packets_v1 writing mode.")
    if mode == "question_partitions_v1":
        limits = request.get("partition_capacity")
        if (not isinstance(limits, dict) or set(limits) != {"context_window_tokens", "max_output_tokens"}
                or any(type(v) is not int or v <= 0 for v in limits.values())):
            raise ValueError("Lock positive integer partition_capacity context_window_tokens and max_output_tokens before init.")
    return mode


def default_minutes(source_count):
    """Soft time budget: a warning threshold that grows with the dataset."""
    return 15 + max(0, int(source_count))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def package_hash():
    root = Path(__file__).resolve().parents[1]
    files = [p for p in root.rglob("*") if p.is_file()
             and "__pycache__" not in p.parts and "tests" not in p.parts
             and p.suffix in (".py", ".md", ".json", ".html", ".css", ".js")]
    return digest({str(p.relative_to(root)): file_hash(p) for p in sorted(files)})


class RunControl:
    """Only the orchestrator writes this ledger; workers return output paths."""

    def __init__(self, root, now=None):
        self.root = Path(root).resolve()
        self.path = self.root / "synthesis/run-state.json"
        self.clock = now or time.time

    def _read(self):
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("version") != VERSION:
            raise ValueError("Unsupported run state; do not infer completion from files.")
        return state

    def _hash_paths(self, paths):
        result = {}
        for name in paths:
            path = (self.root / name).resolve()
            key = str(path.relative_to(self.root))
            if not path.is_file():
                raise ValueError(f"Missing checkpoint input/output: {key}")
            result[key] = file_hash(path)
        return result

    def _identity(self, request, sources):
        mapping = json.loads((self.root / sources).read_text(encoding="utf-8"))
        if not isinstance(mapping, dict) or not mapping or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            raise ValueError("source-paths.json must map source IDs to readable text paths.")
        source_hashes = {fid: file_hash(self.root / path) for fid, path in mapping.items()}
        requested = json.loads((self.root / request).read_text())
        validate_execution_mode(requested)
        return digest({"request": requested,
                       "sources": source_hashes, "pipeline": package_hash()})

    def init(self, request="synthesis/request.json", sources="synthesis/source-paths.json", minutes=None):
        if minutes is None:
            minutes = default_minutes(len(json.loads((self.root / sources).read_text(encoding="utf-8"))))
        if not math.isfinite(minutes) or minutes <= 0:
            raise ValueError("Budget minutes must be positive and finite.")
        if self.path.exists():
            return self.status()  # init never resets budgets or attempts
        if (self.root / "reports/report.html").exists() or (self.root / "synthesis/plan.json").exists():
            raise ValueError("Legacy artifacts found. Use a fresh run folder; do not silently adopt them.")
        state = {"version": VERSION, "status": "active", "request": request,
                 "sources": sources, "identity": self._identity(request, sources),
                 "started_at": self.clock(), "last_tick": self.clock(),
                 "elapsed_seconds": 0.0, "budget_seconds": minutes * 60,
                 "steps": {}, "events": [], "pause_count": 0}
        atomic_json(self.path, state)
        return state

    def _tick(self, state):
        now = self.clock()
        if state["status"] in ("active", "exhausted"):
            # The time budget is a soft warning, never a stop. Large datasets
            # legitimately take longer; only an explicit user stop closes a run.
            state["elapsed_seconds"] += max(0, now - state["last_tick"])
            if state["elapsed_seconds"] >= state["budget_seconds"]:
                state["budget_warning"] = True
            if state["status"] == "exhausted":
                state["status"] = "active"  # legacy hard-stop state becomes a warning
                state["budget_warning"] = True
        state["last_tick"] = now

    def _check(self, state):
        if self._identity(state["request"], state["sources"]) != state["identity"]:
            raise ValueError("Sources, request, or skill changed. Preserve this run and use a new run folder.")
        for anchor in state.get("batch_submissions", {}).values():
            path = self.root / anchor["path"]
            if not path.is_file() or file_hash(path) != anchor["sha256"]:
                raise ValueError("Changed/missing frozen model submission; preserve every attempt’s raw bytes.")
        if state.get("question_parts"):
            from skill.scripts.question_parts import check as check_parts
            check_parts(self.root, state)
        for step in state["steps"].values():
            if step["status"] == "complete":
                for path, expected in {**step["inputs"], **step["outputs"]}.items():
                    if not (self.root / path).is_file() or file_hash(self.root / path) != expected:
                        raise ValueError(f"Changed/missing checkpoint: {path}. Do not reuse stale work.")

    def status(self):
        state = self._read()
        self._tick(state)
        atomic_json(self.path, state)  # persist spent time even if integrity fails
        self._check(state)
        return state

    def claim(self, key, inputs):
        state = self.status()
        if state["status"] != "active":
            raise ValueError(f"No new work permitted: run is {state['status']}. Resume a paused run; a closed run needs a new folder.")
        if state.get("audit_incomplete_reason") and key != "audit":
            raise ValueError("Audit incomplete closure is pending; no new work is permitted.")
        self._validate_key(key, state)
        hashes = self._hash_paths(inputs)
        old = state["steps"].get(key)
        if old and old["inputs"] != hashes:
            raise ValueError(f"Inputs changed for {key}; do not reset its retry budget.")
        if old and old["status"] == "complete":
            return {"action": "reuse", "step": old}
        # The direct coordinator permits four model batches, each containing up
        # to four sources. Other stages and interactive runs retain the old cap.
        request = json.loads((self.root / state["request"]).read_text())
        source_key = key.startswith(("extract:", "audit-initial:"))
        running = [k for k, step in state["steps"].items() if step["status"] == "running"]
        direct_sources = (request.get("execution_profile") == "direct_batches_v1" and source_key
                          and all(k.startswith(("extract:", "audit-initial:")) for k in running))
        limit = 16 if direct_sources else MAX_IN_FLIGHT
        if not old and len(running) >= limit:
            raise ValueError(f"At most {limit} work items can be reserved at once. Finish current work before claiming more; do not pre-reserve the corpus.")
        attempts = old["attempts"] if old else 0
        if key.startswith("audit-repair:") and attempts:
            raise ValueError("Semantic audit correction permits one draft only; no retry or redispatch.")
        # Extraction and initial audit keep one repair: their final attempt
        # deterministically drops unsupported quotes / preserves valid verdicts.
        cap = 2 if key.startswith(("extract:", "audit-initial:")) else MAX_ATTEMPTS
        if attempts >= cap:
            raise ValueError(f"{key}: initial attempt and {cap - 1} repair(s) already consumed.")
        step = {"status": "running", "attempts": attempts + 1, "inputs": hashes,
                "outputs": {}, "usage": [], "started_at": self.clock()}
        if old:
            step["usage"] = old.get("usage", [])
        step["usage"].append({"attempt": step["attempts"], "tokens": None})
        state["steps"][key] = step
        state["events"].append({"event": "claim", "key": key, "attempt": step["attempts"], "at": self.clock()})
        atomic_json(self.path, state)
        return {"action": "run", "attempt": step["attempts"]}

    def _validate_key(self, key, state):
        request = json.loads((self.root / state["request"]).read_text())
        packet_mode = request.get("writing_mode") == "section_packets_v1"
        if packet_mode and key == "report" and state["steps"].get("sections", {}).get("status") != "complete":
            raise ValueError("Complete validated sections before reserving executive work.")
        if key in REQUIRED_STEPS or key == "sections":
            return
        prefix, separator, identifier = key.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_]+", identifier):
            raise ValueError("Unknown work key; do not rename work to reset its budget.")
        if prefix in ("extract", "audit-initial", "audit-repair"):
            mapping = json.loads((self.root / state["sources"]).read_text())
            if identifier in mapping:
                return
        elif prefix in ("themes", "assign", "synthesis"):
            if packet_mode:
                raise ValueError("Use bounded combined stages; legacy keys cannot create extra drafts.")
            if identifier in {q["id"] for q in request["questions"]}:
                return
        raise ValueError("Work key is outside the locked source/question scope.")

    def finish(self, key, outputs, tokens=None):
        state = self.status()
        step = state["steps"].get(key)
        if not step or step["status"] != "running":
            raise ValueError(f"No reserved attempt for {key}.")
        if state["status"] not in ("active", "exhausted"):
            raise ValueError("Cannot finish work on a paused or closed run.")
        if self._hash_paths(step["inputs"]) != step["inputs"]:
            raise ValueError("Inputs changed during work; output cannot become a reusable checkpoint.")
        if not outputs:
            raise ValueError("A completed step needs validated output artifacts.")
        step["outputs"] = self._hash_paths(outputs)
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
            raise ValueError("Tokens must be a measured nonnegative integer, or omitted.")
        step["usage"][-1]["tokens"] = tokens
        step["status"] = "complete"
        state["events"].append({"event": "finish", "key": key, "at": self.clock()})
        atomic_json(self.path, state)
        return state

    def pause(self):
        state = self.status()
        if state["status"] != "active" or any(s["status"] == "running" for s in state["steps"].values()):
            raise ValueError("Pause only an active run with no in-flight work.")
        state["status"] = "paused"
        state["pause_count"] += 1
        atomic_json(self.path, state)
        return state

    def resume(self):
        state = self.status()
        if state["status"] != "paused":
            raise ValueError("Only an explicitly paused run can resume; exhausted/completed runs stay closed.")
        state["status"] = "active"
        state["last_tick"] = self.clock()
        atomic_json(self.path, state)
        return state

    def metrics(self):
        state = self.status()
        usage = [u for step in state["steps"].values() for u in step.get("usage", [])]
        measured = [u["tokens"] for u in usage if u["tokens"] is not None]
        return {"status": state["status"], "active_seconds": state["elapsed_seconds"],
                "elapsed_seconds": max(0, state.get("finished_at", self.clock()) - state["started_at"]),
                "attempts": len(usage), "retries": sum(max(0, step["attempts"] - 1) for step in state["steps"].values()),
                "user_pauses": state["pause_count"], "known_tokens": sum(measured) if measured else None,
                "total_tokens": sum(measured) if usage and len(measured) == len(usage) else None,
                "token_measurements": len(measured)}

    def close(self, complete=False):
        state = self.status()
        if state["status"] in ("complete", "incomplete"):
            if (state["status"] == "complete") != complete:
                raise ValueError("A closed run cannot change its completion status.")
            return state
        if complete:
            from skill.scripts.bounded_audit import verify_gate
            gate = verify_gate(self.root)
            if not gate["complete"]:
                raise ValueError(gate["reason"])
            if any(state["steps"].get(k, {}).get("status") != "complete" for k in REQUIRED_STEPS):
                raise ValueError("Required validated steps are incomplete.")
            request = json.loads((self.root / state["request"]).read_text())
            if request.get("writing_mode") == "section_packets_v1" and state["steps"].get("sections", {}).get("status") != "complete":
                raise ValueError("Validated section packets are required before executive delivery.")
            for name in ("reports/report.html", "reports/report.md", "reports/opportunities.md"):
                if not (self.root / name).is_file():
                    raise ValueError(f"Missing deliverable: {name}")
            if any(s["status"] == "running" for s in state["steps"].values()):
                raise ValueError("Cannot complete with unfinished work.")
        state["status"] = "complete" if complete else "incomplete"
        state["finished_at"] = self.clock()
        atomic_json(self.path, state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "status", "claim", "finish", "pause", "resume", "complete", "stop", "metrics"])
    parser.add_argument("--root", default=".")
    parser.add_argument("--key")
    parser.add_argument("--inputs", nargs="*", default=[])
    parser.add_argument("--outputs", nargs="*", default=[])
    parser.add_argument("--minutes", type=float, default=None,
                        help="Soft warning threshold; default 15 plus one minute per source")
    parser.add_argument("--tokens", type=int)
    args = parser.parse_args()
    control = RunControl(args.root)
    try:
        if args.action == "init":
            result = control.init(minutes=args.minutes)
        elif args.action in ("claim", "finish"):
            if not args.key:
                raise ValueError("--key required")
            result = control.claim(args.key, args.inputs) if args.action == "claim" else control.finish(args.key, args.outputs, args.tokens)
        elif args.action in ("complete", "stop"):
            result = control.close(args.action == "complete")
        else:
            result = getattr(control, args.action)()
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"Run stopped: {exc}\n")


if __name__ == "__main__":
    main()
