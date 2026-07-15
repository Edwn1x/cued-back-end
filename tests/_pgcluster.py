"""
Disposable native PostgreSQL cluster for the test suite.

No Docker in this environment, but Homebrew's postgresql@18 is installed and the
local server major (18.4) matches Railway prod (18.4) exactly — so tier-1 runs
against the same locking / JSON semantics production uses (SELECT ... FOR UPDATE
and flag_modified are real, not the SQLite no-ops the invariants exist to catch).

Lifecycle: initdb into a temp dir -> pg_ctl start on an ephemeral 127.0.0.1 port
with fsync off (throwaway data, speed) -> create a test database -> hand back a
DSN -> stop -m immediate and delete the temp dir at teardown.

Stdlib + subprocess only; safe to import before any project module.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time


# Preferred Homebrew location for the pinned major; fall back to PATH.
_BREW_PG18_BIN = "/opt/homebrew/opt/postgresql@18/bin"


def _find_bindir() -> str:
    if os.path.isdir(_BREW_PG18_BIN) and os.path.exists(os.path.join(_BREW_PG18_BIN, "initdb")):
        return _BREW_PG18_BIN
    initdb = shutil.which("initdb")
    if initdb:
        return os.path.dirname(initdb)
    raise RuntimeError(
        "Could not locate PostgreSQL binaries (initdb). Install postgresql@18 "
        "(`brew install postgresql@18`) or put initdb on PATH."
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PgCluster:
    """A throwaway single-database PostgreSQL server."""

    def __init__(self, dbname: str = "cued_test") -> None:
        self.bindir = _find_bindir()
        self.datadir = tempfile.mkdtemp(prefix="cued-pgtest-")
        self.port = _free_port()
        self.dbname = dbname
        self.user = os.environ.get("USER", "postgres")
        self._started = False

    def _bin(self, name: str) -> str:
        return os.path.join(self.bindir, name)

    def start(self) -> None:
        # init the cluster with trust auth (local, throwaway)
        subprocess.run(
            [self._bin("initdb"), "-D", self.datadir, "-U", self.user,
             "--auth=trust", "--no-sync", "-E", "UTF8"],
            check=True, capture_output=True, text=True,
        )
        # start: TCP on 127.0.0.1 only, durability off for speed.
        # NOTE: pass -l <logfile>. Without it the daemonized postmaster inherits
        # pg_ctl's stdout/stderr pipe and never closes it, so a capture_output
        # subprocess.run() blocks forever waiting for EOF even though the server
        # is up. -l redirects the postmaster's output to a file instead.
        opts = (
            f"-p {self.port} -c listen_addresses=127.0.0.1 "
            f"-c fsync=off -c full_page_writes=off -c synchronous_commit=off"
        )
        logfile = os.path.join(self.datadir, "postmaster.log")
        subprocess.run(
            [self._bin("pg_ctl"), "-D", self.datadir, "-l", logfile,
             "-o", opts, "-w", "-t", "30", "start"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self._started = True
        self._wait_ready()
        # create the test database
        subprocess.run(
            [self._bin("createdb"), "-h", "127.0.0.1", "-p", str(self.port),
             "-U", self.user, self.dbname],
            check=True, capture_output=True, text=True,
        )

    def _wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = subprocess.run(
                [self._bin("pg_isready"), "-h", "127.0.0.1", "-p", str(self.port)],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                return
            time.sleep(0.2)
        raise RuntimeError("Postgres cluster did not become ready in time")

    def dsn(self) -> str:
        # plain postgresql:// — config.py rewrites the scheme to postgresql+psycopg://
        return f"postgresql://{self.user}@127.0.0.1:{self.port}/{self.dbname}"

    def stop(self) -> None:
        if self._started:
            subprocess.run(
                [self._bin("pg_ctl"), "-D", self.datadir, "-m", "immediate", "-w", "stop"],
                capture_output=True, text=True,
            )
            self._started = False
        shutil.rmtree(self.datadir, ignore_errors=True)
