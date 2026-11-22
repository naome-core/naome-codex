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

from routing import choose_lab, choose_project, configured_labs

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
        self.config_path = Path(os.environ.get("NAOME_CODEX_CONFIG", str(SUPPORT / "config.json"))).resolve()
        self.config_bytes = self.config_path.read_bytes() if self.config_path.is_file() else None
        self.config = json.loads(self.config_bytes) if self.config_bytes is not None else {}
        self.state = Path(os.environ.get("NAOME_CODEX_STATE", str(SUPPORT / "journal.sqlite")))
        self.projects = {}
        self.project_names = {}
        self.project_scopes = {}
        for name, project in self.config.get("projects", {}).items():
            if not isinstance(project, dict):
                raise ValueError("Each configured Naome project must be an object.")
            scope = project.get("archive_scope", name)
            if (not isinstance(scope, str) or not scope or scope in self.projects
                    or name.casefold() in self.project_scopes):
                raise ValueError("Configured Naome project names and archive scopes must be distinct.")
            self.projects[scope] = project
            self.project_names[scope] = name
            self.project_scopes[name.casefold()] = scope
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
                    CREATE TABLE IF NOT EXISTS conversation (
                        session TEXT PRIMARY KEY, project TEXT NOT NULL, default_lab TEXT, active_turn TEXT
                    );
                    CREATE TABLE IF NOT EXISTS turn_route (
                        session TEXT NOT NULL, turn TEXT NOT NULL, destination TEXT, lab_name TEXT,
                        job TEXT, token TEXT UNIQUE, issue TEXT, PRIMARY KEY(session, turn)
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

    def configured_lab(self, binding):
        project = self.projects.get(binding["project"])
        if project is not None:
            return next((lab for lab in configured_labs(project)[0].values() if lab["path"] == binding["lab"]), None)

    def lab_allowed(self, binding):
        return self.configured_lab(binding) is not None

    @staticmethod
    def route(db, where, parameters):
        # The binding session identifies an enduring local journal and report stream.
        # A turn route supplies the actual Codex session and its fixed assignment.
        return db.execute(
            "SELECT b.session, b.project, b.lab, t.session AS codex_session, t.turn, "
            "t.lab_name, t.token, t.job, c.project AS workspace_project FROM turn_route t "
            "JOIN conversation c ON t.session=c.session JOIN binding b ON t.destination=b.session "
            f"WHERE t.token IS NOT NULL AND {where}", parameters,
        ).fetchone()

    def turn_binding(self, project, session, turn, active=False):
        with self.open() as db:
            row = self.route(db, "c.project=? AND t.session=? AND t.turn=? AND (NOT ? OR c.active_turn=t.turn)",
                             (project, session, turn, active))
        return self.resolve_binding(row)

    def authorized(self, token):
        if not isinstance(token, str) or not self.projects:
            return None
        with self.open() as db:
            row = self.route(db, "t.token=? AND c.active_turn=t.turn", (token,))
        return self.resolve_binding(row)

    def resolve_binding(self, row):
        if row and row["workspace_project"] in self.projects and self.lab_allowed(row):
            return {**dict(row), "project_name": self.project_names[row["project"]],
                    "read_only": row["project"] != row["workspace_project"]}

    def begin_turn(self, project, session, turn, prompt):
        labs = {}
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            legacy = db.execute("SELECT project, lab FROM binding WHERE session=?", (session,)).fetchone()
            owner = legacy["project"] if legacy else project
            db.execute("INSERT OR IGNORE INTO conversation(session,project) VALUES (?,?)", (session, owner))
            chat = db.execute("SELECT * FROM conversation WHERE session=?", (session,)).fetchone()
            if chat["project"] != project:
                issue = "This Codex chat is enrolled in a different project. Open a chat in this workspace to record evidence."
                self.note_turn(db, session, turn, issue=issue)
                return None, issue, {}
            row = db.execute("SELECT * FROM turn_route WHERE session=? AND turn=?", (session, turn)).fetchone()
            if not row:
                labs = self.select_turn(db, chat, turn, prompt, legacy)
                row = db.execute("SELECT * FROM turn_route WHERE session=? AND turn=?", (session, turn)).fetchone()
            if not row["destination"]:
                return None, row["issue"], labs
            binding = db.execute("SELECT * FROM binding WHERE session=?", (row["destination"],)).fetchone()

        lab = self.configured_lab(binding)
        if not lab:
            return None, "This turn's destination lab is no longer configured.", labs
        if not row["token"]:
            self.finalize_turn(session, turn, binding, lab["job"], read_only=binding["project"] != project)
        bound = self.turn_binding(project, session, turn, active=True)
        return bound, None if bound else "This is no longer the active Codex turn.", labs if bound else {}

    def select_turn(self, db, chat, turn, prompt, legacy):
        session, workspace = chat["session"], chat["project"]
        target, prompt, issue = choose_project(prompt, self.project_scopes, workspace)
        if target is None:
            self.note_turn(db, session, turn, issue=issue)
            return {}
        labs, default = configured_labs(self.projects[target])
        if target == workspace and chat["default_lab"]:
            default = next((name for name, lab in labs.items() if lab["path"] == chat["default_lab"]), None)
        lab, persistent, issue = choose_lab(prompt, labs, default)
        if persistent and target != workspace:
            lab, issue = None, "Use `/lab <name>` to select a lab for a temporary project switch."
        if lab is None:
            self.note_turn(db, session, turn, issue=issue)
            return labs

        path = lab["path"]
        if legacy and legacy["project"] == target and legacy["lab"] == path:
            destination = session
        else:
            identity = [session, path] if target == workspace else [session, target, path]
            destination = "lab:" + digest(identity)
        db.execute("INSERT OR IGNORE INTO binding VALUES (?, ?, NULL, ?, ?)",
                   (destination, target, path, destination))
        if persistent:
            db.execute("UPDATE conversation SET default_lab=? WHERE session=?", (path, session))
        self.note_turn(db, session, turn, destination, lab["name"])
        return labs

    @staticmethod
    def note_turn(db, session, turn, destination=None, lab_name=None, issue=None):
        inserted = db.execute(
            "INSERT OR IGNORE INTO turn_route(session,turn,destination,lab_name,issue) VALUES (?,?,?,?,?)",
            (session, turn, destination, lab_name, issue),
        ).rowcount
        if inserted:
            db.execute("UPDATE conversation SET active_turn=? WHERE session=?", (turn, session))

    def leave_project(self, session, turn):
        if self.state.is_file():
            with self.open() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM conversation WHERE session=?", (session,)).fetchone():
                    self.note_turn(db, session, turn, issue="This turn has no enrolled project.")

    def finalize_turn(self, session, turn, binding, preferred, *, read_only=False):
        """Pin an assignment and a capability to the turn before exposing its route."""
        if read_only:
            with self.open() as db:
                db.execute("UPDATE turn_route SET token=? WHERE session=? AND turn=? AND token IS NULL",
                           (secrets.token_urlsafe(24), session, turn))
            return
        try:
            self.require_lab(binding["lab"])
            jobs = self.call(binding["lab"], "research", "jobs", timeout=1.5)
        except (DeliveryError, OSError):
            jobs = None
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT token FROM turn_route WHERE session=? AND turn=?", (session, turn)).fetchone()[0]:
                return
            destination, path = binding["session"], binding["lab"]
            current = db.execute("SELECT job FROM binding WHERE session=?", (destination,)).fetchone()[0]
            if preferred and current != preferred:
                current = None
            if jobs is not None:
                claimed = {r[0] for r in db.execute(
                    "SELECT job FROM binding WHERE lab=? AND session<>? UNION "
                    "SELECT e.job FROM entry e JOIN binding b USING(session) WHERE b.lab=? AND b.session<>? "
                    "UNION SELECT t.job FROM turn_route t JOIN binding b ON t.destination=b.session "
                    "WHERE b.lab=? AND b.session<>?", (path, destination, path, destination, path, destination),
                )}
                eligible = [
                    item["id"] for item in jobs
                    if item["assignment"]["project"] == binding["project"] and item["input_ready"]
                    and item["phase"] in ACTIVE and item["id"] not in claimed
                    and (not preferred or item["id"] == preferred)
                ]
                if current not in eligible:
                    current = eligible[0] if len(eligible) == 1 else None
            db.execute("UPDATE binding SET job=? WHERE session=?", (current, destination))
            db.execute("UPDATE turn_route SET job=?,token=? WHERE session=? AND turn=?",
                       (current, secrets.token_urlsafe(24), session, turn))

    @staticmethod
    def require_lab(lab):
        directory = Path(lab)
        if not (directory / "archive.sqlite").is_file():
            raise DeliveryError("The selected laboratory archive is unavailable.")
        try:
            role = json.loads((directory / "node.json").read_text())["role"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise DeliveryError("The selected laboratory node configuration is unavailable.") from error
        if role != "lab":
            raise DeliveryError("The selected node must have the lab role.")

    def enqueue(self, binding, event, category, subject, fields, references=()):
        document = {
            "kind": KINDS[category],
            "subject": subject,
            "scope": binding["project"],
            "fields": {**fields, "category": category, "codex_session": binding["codex_session"],
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
            job = binding["job"]
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

    def status(self, binding, offset=None, *, include_chat=False):
        session = binding["session"]
        with self.open() as db:
            if include_chat:
                db.execute("BEGIN")
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
            progress = {"pending_local": local, "pending_reports": reports,
                        "local_record": record[0] if record else None}
            if include_chat:
                destinations = self.pending_destinations(db, binding)
                progress["chat_pending_local"] = sum(row["pending_local"] for row in destinations)
                progress["chat_pending_reports"] = sum(row["pending_reports"] for row in destinations)
        return progress

    def recent_research(self, binding, limit):
        """List locally acknowledged Codex research receipts for this project and lab."""
        self.require_lab(binding["lab"])
        with self.open() as db:
            rows = db.execute(
                "SELECT e.record, e.document, e.captured_at FROM binding b JOIN entry e USING(session) "
                "WHERE b.project=? AND b.lab=? AND e.record IS NOT NULL AND e.event GLOB 'research:*' "
                "ORDER BY e.captured_at DESC, e.rowid DESC LIMIT ?",
                (binding["project"], binding["lab"], limit),
            ).fetchall()
        receipts = []
        for row in rows:
            document = json.loads(row["document"])
            receipts.append({
                "record_id": row["record"], "title": document["subject"],
                "category": document["fields"]["category"],
                "captured_at": row["captured_at"].replace(" ", "T") + "Z",
                "summary": document["fields"].get("summary"),
            })
        return receipts

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
        if rows:
            try:
                self.require_lab(binding["lab"])
            except DeliveryError as error:
                return {**self.status(binding, offset), "local_retry": str(error)}
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

    @staticmethod
    def pending_destinations(db, binding):
        session = binding["codex_session"]
        return db.execute(
            "SELECT b.session, b.project, b.lab, "
            "count(CASE WHEN e.record IS NULL THEN 1 END) AS pending_local, "
            "count(CASE WHEN e.job IS NOT NULL AND e.head IS NULL THEN 1 END) AS pending_reports "
            "FROM binding b JOIN entry e USING(session) "
            "WHERE b.project=? AND (e.record IS NULL OR (e.job IS NOT NULL AND e.head IS NULL)) "
            "AND b.session IN (SELECT destination FROM turn_route WHERE session=? UNION SELECT ?) "
            "GROUP BY b.session, b.project, b.lab ORDER BY b.session", (binding["project"], session, session),
        ).fetchall()

    def flush(self, binding):
        """Retry this chat's accepted records using their original lab and assignment."""
        with self.open() as db:
            destinations = [row for row in self.pending_destinations(db, binding) if self.lab_allowed(row)]
        destinations.sort(key=lambda row: row["session"] != binding["session"])
        deadline = time.monotonic() + 2
        progress = {}
        for destination in destinations:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            local = self.archive(destination, budget=remaining)
            if "local_retry" in local:
                progress["local_retry"] = local["local_retry"]
        if not destinations:
            return self.status(binding, include_chat=True)

        lock = self.state.with_suffix(".delivery.lock")
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**self.status(binding, include_chat=True), **progress, "report_retry": "Delivery is in progress."}
            deadline = time.monotonic() + 5
            for destination in destinations:
                with self.open() as db:
                    jobs = db.execute(
                        "SELECT job FROM entry WHERE session=? AND job IS NOT NULL AND head IS NULL "
                        "GROUP BY job ORDER BY min(offset)", (destination["session"],),
                    ).fetchall()
                for (job,) in jobs:
                    if time.monotonic() >= deadline:
                        return {**self.status(binding, include_chat=True), **progress, "report_retry": "Delivery budget expired."}
                    try:
                        self.require_lab(destination["lab"])
                        progress.update(self.deliver(destination["lab"], job, destination["session"], deadline))
                    except DeliveryError as error:
                        progress["report_retry"] = str(error)
        return {**self.status(binding, include_chat=True), **progress}

    def deliver(self, lab, job, session, deadline):
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
        def call(*args, data=None):
            remaining = deadline - time.monotonic()
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
