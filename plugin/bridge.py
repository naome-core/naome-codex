"""Persist Codex research records in local NAOME Protocol archives."""

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from routing import configured_labs, named_lab

SUPPORT = Path.home() / "Library/Application Support/NAOME Codex"
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


def archive_file(lab):
    return Path(lab) / "archive.sqlite"


def archive_command(executable, lab, *args):
    return [str(executable), "--db", str(archive_file(lab)), *args]


def wrong_cli(executable):
    return (f"The executable at {executable} does not provide the standalone NAOME Protocol CLI. "
            "Install nao v0.1.6 or set nao to a compatible standalone executable in the Naome Codex config.")


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
        configured = self.config.get("nao")
        if configured is None:
            executable = shutil.which("nao")
            self.nao = Path(executable).resolve() if executable else Path.home() / ".cargo/bin/nao"
        else:
            self.nao = Path(configured)
            if not self.nao.is_absolute():
                raise ValueError("The nao path must be absolute.")

    def project_at(self, cwd):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not self.projects:
            return None
        location = Path(cwd).resolve()
        candidate = git_common_dir(location)
        matched = [name for name, project in self.projects.items() if self.owns_workspace(project, location, candidate)]
        return matched[0] if len(matched) == 1 else None

    @staticmethod
    def owns_workspace(project, location, candidate):
        root = Path(project["workspace"])
        if not root.is_absolute():
            raise ValueError("Workspace paths must be absolute.")
        root = root.resolve()
        if root == location:
            return True
        owner = git_common_dir(root)
        # A nested Git repository is a different workspace.
        return owner == candidate if owner is not None else root in location.parents

    @contextmanager
    def open(self):
        self.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.state, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS binding (
                        session TEXT PRIMARY KEY, project TEXT NOT NULL,
                        lab TEXT NOT NULL, token TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS entry (
                        session TEXT NOT NULL, offset INTEGER NOT NULL, event TEXT NOT NULL,
                        document TEXT NOT NULL, record TEXT,
                        captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(session, offset), UNIQUE(session, event)
                    );
                    CREATE TABLE IF NOT EXISTS conversation (
                        session TEXT PRIMARY KEY, project TEXT NOT NULL, default_lab TEXT, active_turn TEXT
                    );
                    CREATE TABLE IF NOT EXISTS turn_route (
                        session TEXT NOT NULL, turn TEXT NOT NULL, destination TEXT, lab_name TEXT,
                        token TEXT UNIQUE, issue TEXT, PRIMARY KEY(session, turn)
                    );
                """)
                yield db
        finally:
            db.close()

    def call(self, lab, *args, timeout=2):
        try:
            result = subprocess.run(
                archive_command(self.nao, lab, *args), capture_output=True, text=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DeliveryError(f"The standalone NAOME Protocol CLI at {self.nao} is unavailable or timed out.") from error
        if result.returncode:
            if "unexpected argument '--db'" in result.stderr:
                raise DeliveryError(wrong_cli(self.nao))
            raise DeliveryError("NAOME Protocol did not accept the command. Queued records are retained.")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as error:
            raise DeliveryError("NAOME Protocol returned an unreadable response.") from error

    def configured_lab(self, binding):
        scope = binding["project"]
        if scope in self.projects:
            labs, _ = self.project_labs(scope)
            return next((lab for lab in labs.values() if lab["path"] == binding["lab"]), None)

    def lab_allowed(self, binding):
        return self.configured_lab(binding) is not None

    @staticmethod
    def route(db, where, parameters):
        # The binding session identifies an enduring journal for a chat and lab.
        # The route supplies the Codex session and its selected destination.
        return db.execute(
            "SELECT b.session, COALESCE(b.project,c.project) AS project, b.lab, t.session AS codex_session, t.turn, "
            "t.lab_name, t.token, c.project AS workspace_project FROM turn_route t "
            "JOIN conversation c ON t.session=c.session LEFT JOIN binding b ON t.destination=b.session "
            f"WHERE t.token IS NOT NULL AND {where}", parameters,
        ).fetchone()

    def turn_binding(self, cwd, session, turn):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not self.projects or not self.state.is_file():
            return None
        with self.open() as db:
            row = self.route(db, "t.session=? AND t.turn=?", (session, turn))
        if row and row["lab"] and row["workspace_project"] in self.projects:
            location = Path(cwd).resolve()
            if self.owns_workspace(self.projects[row["workspace_project"]], location, git_common_dir(location)):
                return self.resolve_binding(row)

    def authorized(self, token):
        if not isinstance(token, str) or not self.projects:
            return None
        with self.open() as db:
            row = self.route(db, "t.token=? AND c.active_turn=t.turn", (token,))
        return self.resolve_binding(row)

    def resolve_binding(self, row):
        if row and row["project"] == row["workspace_project"] and row["project"] in self.projects:
            if row["lab"] is None or self.lab_allowed(row):
                return {**dict(row), "project_name": self.project_names[row["project"]]}

    def project_labs(self, scope):
        try:
            return configured_labs(self.projects[scope])
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as error:
            raise ValueError(f"Invalid lab configuration for Naome project {self.project_names[scope]}: {error}") from error

    def project_catalog(self):
        catalog = {}
        for scope, name in self.project_names.items():
            try:
                labs, _ = self.project_labs(scope)
            except ValueError:
                catalog[name] = []
            else:
                catalog[name] = [lab["name"] for lab in labs.values()]
        return catalog

    def read_destination(self, binding, arguments):
        project = binding["project"]
        if "project" in arguments:
            name = arguments["project"]
            if not isinstance(name, str) or name.casefold() not in self.project_scopes:
                raise ValueError("The specified project is not enrolled.")
            project = self.project_scopes[name.casefold()]
        labs, default = self.project_labs(project)
        if "lab" in arguments:
            lab = named_lab(labs, arguments["lab"])
        elif project == binding["project"] and binding["lab"]:
            lab = self.configured_lab(binding)
        else:
            lab = labs.get(default)
        if not lab:
            raise ValueError("Specify the lab argument for this project.")
        return {"project": project, "project_name": self.project_names[project],
                "lab": lab["path"], "lab_name": lab["name"]}

    def begin_turn(self, project, session, turn):
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            legacy = db.execute("SELECT project, lab FROM binding WHERE session=?", (session,)).fetchone()
            owner = legacy["project"] if legacy else project
            db.execute("INSERT OR IGNORE INTO conversation(session,project) VALUES (?,?)", (session, owner))
            chat = db.execute("SELECT * FROM conversation WHERE session=?", (session,)).fetchone()
            if chat["project"] != project:
                issue = "This Codex chat is enrolled in a different workspace project."
                self.note_turn(db, session, turn, issue=issue)
                return None, issue
            row = db.execute("SELECT * FROM turn_route WHERE session=? AND turn=?", (session, turn)).fetchone()
            if not row:
                try:
                    labs, default = self.project_labs(project)
                except ValueError as error:
                    issue = str(error)
                    self.note_turn(db, session, turn, issue=issue)
                    return None, issue
                lab = next((lab for lab in labs.values() if lab["path"] == chat["default_lab"]), labs.get(default))
                dest = self.register_destination(db, session, project, lab) if lab else None
                self.note_turn(db, session, turn, dest, lab["name"] if lab else None)
                row = db.execute("SELECT * FROM turn_route WHERE session=? AND turn=?", (session, turn)).fetchone()
            if row["issue"]:
                return None, row["issue"]
            binding = db.execute("SELECT * FROM binding WHERE session=?", (row["destination"],)).fetchone()
            if binding and (binding["project"] != project or not self.lab_allowed(binding)):
                return None, "This turn's workspace lab is no longer configured."
            if not row["token"]:
                db.execute("UPDATE turn_route SET token=? WHERE session=? AND turn=?",
                           (secrets.token_urlsafe(24), session, turn))
            row = self.route(db, "t.session=? AND t.turn=? AND c.active_turn=t.turn", (session, turn))
        bound = self.resolve_binding(row)
        return bound, None if bound else "This is no longer the active Codex turn."

    @staticmethod
    def register_destination(db, session, project, lab):
        path = lab["path"]
        legacy = db.execute("SELECT project, lab FROM binding WHERE session=?", (session,)).fetchone()
        if legacy and legacy["project"] == project and legacy["lab"] == path:
            return session
        destination = "lab:" + digest([session, path])
        db.execute("INSERT OR IGNORE INTO binding(session,project,lab,token) VALUES (?,?,?,?)",
                   (destination, project, path, destination))
        return destination

    def select_lab(self, binding, name, remember):
        project, session, turn = binding["project"], binding["codex_session"], binding["turn"]
        lab = named_lab(self.project_labs(project)[0], name)
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            active = self.route(db, "t.token=? AND c.active_turn=t.turn", (binding["token"],))
            if not self.resolve_binding(active):
                raise ValueError("The active Codex turn has no valid Naome lab token.")
            destination = self.register_destination(db, session, project, lab)
            if destination != active["session"]:
                captured = db.execute(
                    "SELECT 1 FROM entry WHERE session=? AND json_extract(document, '$.fields.codex_turn')=? LIMIT 1",
                    (active["session"], turn),
                ).fetchone()
                if captured:
                    raise ValueError("Select the lab before recording evidence in this turn.")
                db.execute("UPDATE turn_route SET destination=?,lab_name=? WHERE session=? AND turn=?",
                           (destination, lab["name"], session, turn))
            if remember:
                db.execute("UPDATE conversation SET default_lab=? WHERE session=?", (lab["path"], session))
        return self.authorized(binding["token"])

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

    @staticmethod
    def require_lab(lab):
        if not archive_file(lab).is_file():
            raise DeliveryError("The selected laboratory archive is unavailable.")

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
        session = binding["session"]
        with self.open() as db:
            db.execute("BEGIN IMMEDIATE")
            route = db.execute("SELECT destination FROM turn_route WHERE session=? AND turn=?",
                               (binding["codex_session"], binding["turn"])).fetchone()
            if not route or route["destination"] != session:
                raise DeliveryError("The lab for this turn changed. Retry capture using the selected lab.")
            old = db.execute("SELECT offset, document FROM entry WHERE session=? AND event=?",
                             (session, event)).fetchone()
            if old:
                if old["document"] != body:
                    raise ValueError("This event identity already refers to different content.")
                return old["offset"]
            offset = db.execute("SELECT COALESCE(MAX(offset)+1,0) FROM entry WHERE session=?",
                                (session,)).fetchone()[0]
            db.execute("INSERT INTO entry(session,offset,event,document) VALUES (?,?,?,?)",
                       (session, offset, event, body))
        return offset

    @staticmethod
    def document(row):
        document = json.loads(row["document"])
        document["fields"]["captured_at"] = row["captured_at"].replace(" ", "T") + "Z"
        # Old queued documents used this field in their content-addressed bytes.
        if "job" in row.keys() and row["job"]:
            document["fields"]["naome_job"] = row["job"]
        return document

    def status(self, binding, offset=None, *, include_chat=False):
        session = binding["session"]
        with self.open() as db:
            if include_chat:
                db.execute("BEGIN")
            local = db.execute(
                "SELECT count(*) FROM entry WHERE session=? AND record IS NULL", (session,),
            ).fetchone()[0]
            if offset is None:
                record = db.execute(
                    "SELECT record FROM entry WHERE session=? AND record IS NOT NULL ORDER BY offset DESC LIMIT 1",
                    (session,),
                ).fetchone()
            else:
                record = db.execute("SELECT record FROM entry WHERE session=? AND offset=?", (session, offset)).fetchone()
            progress = {"pending_local": local, "local_record": record[0] if record else None}
            if include_chat:
                destinations = self.pending_destinations(db, binding)
                progress["chat_pending_local"] = sum(row["pending_local"] for row in destinations)
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
            fields = document["fields"]
            receipts.append({
                "record_id": row["record"], "title": document["subject"],
                "category": fields["category"],
                "captured_at": row["captured_at"].replace(" ", "T") + "Z",
                "summary": fields.get("summary"), "sources": fields.get("sources", []),
                "linked_records": [link["target"] for link in document.get("inputs", [])
                                   if link["role"] == "references"],
            })
        return receipts

    def archive(self, binding, offset=None, budget=2):
        """Deposit queued evidence and return the receipt when a specific offset was requested."""
        session = binding["session"]
        with self.open() as db:
            if offset is None:
                rows = db.execute(
                    "SELECT * FROM entry WHERE session=? AND record IS NULL ORDER BY offset LIMIT 16",
                    (session,),
                ).fetchall()
            else:
                row = db.execute("SELECT * FROM entry WHERE session=? AND offset=?", (session, offset)).fetchone()
                if not row or row["record"] is not None:
                    return {"local_record": row["record"] if row else None}
                rows = (row,)
        result = self.archive_rows(binding["lab"], rows, budget)
        if offset is not None:
            with self.open() as db:
                row = db.execute("SELECT record FROM entry WHERE session=? AND offset=?", (session, offset)).fetchone()
            result["local_record"] = row["record"] if row else None
        return result

    def archive_rows(self, lab, rows, budget):
        deadline = time.monotonic() + budget
        retry = {}
        if rows:
            try:
                self.require_lab(lab)
            except DeliveryError as error:
                return {"local_retry": str(error)}
        for row in rows:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with tempfile.NamedTemporaryFile(mode="w", dir=self.state.parent, suffix=".json",
                                             encoding="utf-8") as evidence:
                evidence.write(encode(self.document(row)))
                evidence.flush()
                try:
                    record = self.call(lab, "put", evidence.name,
                                       timeout=min(1.5, remaining))
                    if not isinstance(record, str) or not ID.fullmatch(record):
                        raise DeliveryError("NAOME did not acknowledge the local record.")
                except DeliveryError as error:
                    retry = {"local_retry": str(error)}
                    continue
            with self.open() as db:
                db.execute("UPDATE entry SET record=? WHERE session=? AND offset=?",
                           (record, row["session"], row["offset"]))
        return retry

    @staticmethod
    def pending_destinations(db, binding):
        session = binding["codex_session"]
        return db.execute(
            "SELECT b.session, b.project, b.lab, count(*) AS pending_local "
            "FROM binding b JOIN entry e USING(session) "
            "WHERE b.project=? AND e.record IS NULL "
            "AND b.session IN (SELECT destination FROM turn_route WHERE session=? UNION SELECT ?) "
            "GROUP BY b.session, b.project, b.lab ORDER BY b.session", (binding["project"], session, session),
        ).fetchall()

    def flush(self, binding):
        """Retry this chat's accepted records using their original lab."""
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
        return {**self.status(binding, include_chat=True), **progress}
