"""Persist Codex research records and deliver them using the NAOME lab CLI."""

import fcntl
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support/NAOME Codex"
ACTIVE = {"assigned", "accepted", "running", "progress"}
ID = re.compile(r"1:[012]:[0-9a-f]{64}\Z")
KINDS = {
    "question": "question",
    "hypothesis": "claim",
    "method": "plan",
    "observation": "occurrence",
    "result": "derivation",
    "limitation": "occurrence",
    "correction": "occurrence",
}


class DeliveryError(Exception):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def git_common_dir(directory):
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, timeout=1,
    )
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


class Bridge:
    def __init__(self):
        config = Path(os.environ.get("NAOME_CODEX_CONFIG", str(SUPPORT / "config.json")))
        self.config = json.loads(config.read_text()) if config.is_file() else {}
        self.state = Path(os.environ.get("NAOME_CODEX_STATE", str(SUPPORT / "journal.sqlite")))
        self.projects = self.config.get("projects", {})
        self.naome = Path(self.config.get("naome", str(Path.home() / ".cargo/bin/naome")))
        if not self.naome.is_absolute():
            raise ValueError("The naome path must be absolute.")

    def project_at(self, cwd):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not self.projects:
            return None
        location = Path(cwd).resolve()
        candidate = git_common_dir(location)
        matched = []
        for name, project in self.projects.items():
            root = Path(project["workspace"])
            if not root.is_absolute():
                raise ValueError("Workspace paths must be absolute.")
            root = root.resolve()
            owner = git_common_dir(root)
            # A nested Git repository is a different workspace.
            if (owner is None and (root == location or root in location.parents)) or (
                owner is not None and owner == candidate
            ):
                matched.append(name)
        return matched[0] if len(matched) == 1 else None

    @contextmanager
    def open(self):
        self.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.state, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS binding (
                        session TEXT PRIMARY KEY, project TEXT NOT NULL, job TEXT,
                        lab TEXT NOT NULL, token TEXT NOT NULL UNIQUE, UNIQUE(lab, job)
                    );
                    CREATE TABLE IF NOT EXISTS entry (
                        session TEXT NOT NULL, offset INTEGER NOT NULL, event TEXT NOT NULL,
                        document TEXT NOT NULL, job TEXT, stream_offset INTEGER, record TEXT, head TEXT,
                        captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(session, offset), UNIQUE(session, event)
                    );
                """)
                yield db
        finally:
            db.close()

    def call(self, lab, *args, data=None, timeout=2):
        try:
            result = subprocess.run(
                [str(self.naome), "--directory", lab, "--json", *args],
                input=data, capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeliveryError("The local NAOME command is unavailable or timed out.") from error
        if result.returncode:
            raise DeliveryError("NAOME did not accept the command. Queued records are retained.")
        try:
            lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        except ValueError as error:
            raise DeliveryError("NAOME returned an unreadable response.") from error
        return lines if args[:2] == ("research", "stream") else (lines[0] if lines else None)

    def binding(self, session):
        if not self.projects:
            return None
        with self.open() as db:
            row = db.execute("SELECT * FROM binding WHERE session=?", (session,)).fetchone()
        if row is None:
            return None
        configured = self.projects.get(row["project"])
        if not configured or str(Path(configured["lab"]).resolve()) != row["lab"]:
            return None
        return dict(row)

    def authorized(self, token):
        if not isinstance(token, str) or not self.projects:
            return None
        with self.open() as db:
            row = db.execute("SELECT session FROM binding WHERE token=?", (token,)).fetchone()
        return self.binding(row["session"]) if row else None

    def bind(self, project, session):
        bound = self.binding(session)
        if bound:
            return bound if bound["project"] == project else None
        settings = self.projects[project]
        lab_path = Path(settings["lab"])
        if not lab_path.is_absolute():
            raise ValueError("Lab directories must be absolute.")
        lab_path = lab_path.resolve()
        if json.loads((lab_path / "node.json").read_text())["role"] != "lab":
            raise ValueError("The enrolled node must have the lab role.")
        lab = str(lab_path)
        with self.open() as db:
            db.execute("INSERT OR IGNORE INTO binding VALUES (?, ?, NULL, ?, ?)",
                       (session, project, lab, secrets.token_urlsafe(24)))
        bound = self.binding(session)
        return bound if bound and bound["project"] == project else None

    def associate_job(self, binding):
        """Select an eligible assignment for future records. Keep past records local."""
        try:
            jobs = self.call(binding["lab"], "research", "jobs", timeout=1.5)
        except DeliveryError:
            return binding
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT job FROM binding WHERE session=?",
                                 (binding["session"],)).fetchone()[0]
            if current and not any(item["id"] == current and item["phase"] in ACTIVE for item in jobs):
                current = None
            if not current:
                claimed = {r[0] for r in db.execute("SELECT job FROM binding WHERE lab=?", (binding["lab"],))}
                preferred = self.projects[binding["project"]].get("job")
                eligible = [
                    item["id"] for item in jobs
                    if item["assignment"]["project"] == binding["project"] and item["input_ready"]
                    and item["phase"] in ACTIVE and item["id"] not in claimed
                    and (not preferred or item["id"] == preferred)
                ]
                current = eligible[0] if len(eligible) == 1 else None
            db.execute("UPDATE binding SET job=? WHERE session=?", (current, binding["session"]))
        return {**binding, "job": current}

    def enqueue(self, binding, event, category, subject, fields, references=()):
        document = {
            "kind": KINDS[category],
            "subject": subject,
            "scope": binding["project"],
            "fields": {**fields, "category": category, "codex_session": binding["session"],
                       "event_key": event},
            "inputs": [{"role": "references", "target": value} for value in references],
        }
        body = encode(document)
        if len(body.encode()) > 60000:
            raise ValueError("The research record exceeds the local stream segment budget.")
        session = binding["session"]
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT offset, document FROM entry WHERE session=? AND event=?",
                             (session, event)).fetchone()
            if old:
                if old["document"] != body:
                    raise ValueError("This event identity already refers to different content.")
                return old["offset"]
            offset = db.execute("SELECT COALESCE(MAX(offset)+1,0) FROM entry WHERE session=?",
                                (session,)).fetchone()[0]
            job = db.execute("SELECT job FROM binding WHERE session=?", (session,)).fetchone()[0]
            stream_offset = None
            if job:
                stream_offset = db.execute(
                    "SELECT COALESCE(MAX(stream_offset)+1,0) FROM entry WHERE session=? AND job=?",
                    (session, job),
                ).fetchone()[0]
            db.execute("INSERT INTO entry(session,offset,event,document,job,stream_offset) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (session, offset, event, body, job, stream_offset))
        return offset

    @staticmethod
    def document(row):
        document = json.loads(row["document"])
        document["fields"]["captured_at"] = row["captured_at"].replace(" ", "T") + "Z"
        if row["job"]:
            document["fields"]["naome_job"] = row["job"]
        return document

    def status(self, binding, offset=None):
        session = binding["session"]
        with self.open() as db:
            local, reports = db.execute(
                "SELECT count(CASE WHEN record IS NULL THEN 1 END), "
                "count(CASE WHEN job IS NOT NULL AND head IS NULL THEN 1 END) "
                "FROM entry WHERE session=?", (session,),
            ).fetchone()
            record = db.execute(
                "SELECT record FROM entry WHERE session=? AND record IS NOT NULL "
                "AND (? IS NULL OR offset=?) ORDER BY offset DESC LIMIT 1",
                (session, offset, offset),
            ).fetchone()
        return {"pending_local": local, "pending_reports": reports,
                "local_record": record[0] if record else None}

    def archive(self, binding, offset=None, budget=2):
        """Deposit evidence locally even when there is no assigned job."""
        session = binding["session"]
        with self.open() as db:
            rows = db.execute(
                "SELECT * FROM entry WHERE session=? AND record IS NULL "
                "AND (? IS NULL OR offset=?) ORDER BY offset LIMIT 16",
                (session, offset, offset),
            ).fetchall()
        deadline = time.monotonic() + budget
        retry = {}
        for row in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with tempfile.NamedTemporaryFile(mode="w", dir=self.state.parent, suffix=".json",
                                             encoding="utf-8") as evidence:
                evidence.write(encode(self.document(row)))
                evidence.flush()
                try:
                    record = self.call(binding["lab"], "archive", "put", evidence.name,
                                       timeout=min(1.5, remaining))
                    if not isinstance(record, str) or not ID.fullmatch(record):
                        raise DeliveryError("NAOME did not acknowledge the local record.")
                except DeliveryError as error:
                    retry = {"local_retry": str(error)}
                    continue
            with self.open() as db:
                db.execute("UPDATE entry SET record=? WHERE session=? AND offset=?",
                           (record, session, row["offset"]))
        return {**self.status(binding, offset), **retry}

    def flush(self, binding):
        """Archive locally first. Retry reports without changing their offsets."""
        local = self.archive(binding)
        lab, job, session = (binding[k] for k in ("lab", "job", "session"))
        if not job or not local["pending_reports"]:
            return local
        lock = self.state.with_suffix(".delivery.lock")
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**local, "report_retry": "Delivery is in progress."}
            try:
                delivery = self.deliver(lab, job, session)
            except DeliveryError as error:
                delivery = {"report_retry": str(error)}
        return {**local, **self.status(binding), **delivery}

    def deliver(self, lab, job, session):
        with self.open() as db:
            candidates = db.execute(
                "SELECT * FROM entry WHERE session=? AND job=? AND head IS NULL "
                "ORDER BY stream_offset LIMIT 16", (session, job),
            ).fetchall()
        pending = []
        for row in candidates:
            if row["record"] is None:
                break
            pending.append(row)
        if not pending:
            return {}
        started = time.monotonic()

        def call(*args, data=None):
            remaining = 5 - (time.monotonic() - started)
            if remaining <= 0:
                raise DeliveryError("Delivery budget expired. Queued records are retained.")
            return self.call(lab, *args, data=data, timeout=min(3, remaining))

        jobs = call("research", "jobs")
        job_info = next((item for item in jobs if item["id"] == job), None)
        if not job_info or not job_info["input_ready"] or job_info["phase"] not in ACTIVE:
            raise DeliveryError("The assigned lab job is no longer ready for reports.")
        if job_info["phase"] == "assigned":
            call("research", "report", job, "accepted")
        if job_info["phase"] in {"assigned", "accepted"}:
            call("research", "report", job, "running")

        segments = [
            encode({"offset": row["stream_offset"], "records": [self.document(row)]})
            for row in pending
        ]
        payload = "\n".join(segments) + "\n"
        receipts = call("research", "stream", job, "codex-" + digest(session)[:20], data=payload)
        expected = [row["stream_offset"] for row in pending]
        if [receipt.get("offset") for receipt in receipts] != expected or not all(
            isinstance(receipt.get("head"), str) and ID.fullmatch(receipt["head"])
            for receipt in receipts
        ):
            raise DeliveryError("NAOME did not acknowledge the full batch.")
        with self.open() as db:
            db.executemany("UPDATE entry SET head=? WHERE session=? AND offset=?",
                           ((receipt["head"], session, row["offset"])
                            for row, receipt in zip(pending, receipts)))
        return {"last_head": receipts[-1]["head"]}
