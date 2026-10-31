"""Starts, watches and stops the 3D simulation service (simulation3d/) for the web app.

The 3D Simulator page asks for the service (POST /api/simulator3d/start) when it opens, so the service runs only while
somebody uses the tab and nothing is started when the app itself starts, in tests, or in the reloader's watcher process.

What this module guarantees:
  * One service per web app process. Repeated requests find the one already starting or running.
  * It never takes over a port. If the wanted port is taken by something that is not a 3D service it starts on a free
    port instead, and tells the page which. A 3D service that is already running there (started with simulation3d/run.sh)
    is recognised by its endpoints and used as it is; it is not ours, so it is never stopped.
  * Ready means the child answered /api/identity with the instance id given to it, not just that the port opened.
  * Only the child this module started is ever signalled, by pid and process group, never by port or name. The child
    shuts its simulations and their SUMO down on SIGTERM; it also exits by itself if this process dies (see
    simulation3d/atcs3d/managed.py), so a crash of the web app cannot leave it behind.
"""
import atexit
import http.client
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

POSIX = os.name == "posix"  # the service uses process groups and signals
HOST = "127.0.0.1"
DEFAULT_PORT = 8766  # the Monitor service uses 8765
STARTUP_TIMEOUT_S = 20.0
STOP_TIMEOUT_S = 10.0
LOG_TAIL_LINES = 8
PERF_KEYS = {"step_roundtrip_ms", "frame_bytes", "clients"}


def _get_json(port, path, timeout=1.0):
    conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read(65536)
        return json.loads(body) if response.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def identify(port, instance=None):
    """Who answers on the port: "ours" (the child started with this instance id), "atcs3d" (a 3D service that was not
    started by this process) or None (nobody, or some other program)."""
    ident = _get_json(port, "/api/identity")
    if isinstance(ident, dict) and ident.get("service") == "atcs3d":
        return "ours" if instance and ident.get("instance") == instance else "atcs3d"
    # A service started by simulation3d/run.sh has no /api/identity: recognise it by its two documented endpoints.
    if _get_json(port, "/api/health") == {"ok": True}:
        perf = _get_json(port, "/api/perf")
        if isinstance(perf, dict) and PERF_KEYS <= perf.keys():
            return "atcs3d"
    return None


def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def free_port():
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def stop_process(proc, timeout=STOP_TIMEOUT_S):
    """SIGTERM lets the service close its simulations and their SUMO; if it does not exit in time its process group is
    killed, and the simulation workers, whose stdin then closes, end by themselves."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    try:
        proc.stdin.close()
    except (OSError, AttributeError):
        pass


class Simulator3DService:
    def __init__(self, root, port=DEFAULT_PORT, disabled_reason=None, python=None,
                 startup_timeout=STARTUP_TIMEOUT_S, stop_timeout=STOP_TIMEOUT_S):
        self.root = root
        self.preferred_port = port
        self.python = python or os.environ.get("ATCS3D_PYTHON") or sys.executable
        self.startup_timeout = startup_timeout
        self.stop_timeout = stop_timeout
        self.log_path = os.path.join(root, "output", "service.log")
        self._lock = threading.Lock()
        self._proc = None  # the child this process started; the only thing it ever signals
        self._instance = None
        self._state = "disabled" if disabled_reason else "idle"  # disabled | idle | starting | running | failed
        self._port = None
        self._message = disabled_reason or ""
        self._detail = ""

    @property
    def enabled(self):
        return self._state != "disabled"

    # ------------------------------------------------------------------ what the page asks
    def status(self):
        with self._lock:
            self._reap()
            return self._snapshot()

    def ensure(self, retry=False):
        """Start the service unless it is already starting or running; return the status. A failed start is not tried
        again unless asked to (`retry`), so a page that polls cannot start a failing service in a loop."""
        with self._lock:
            self._reap()
            if self._state == "running" and self._proc is None and identify(self._port) is None:
                self._idle()  # the service that was running outside ATCS is gone
            if self._state == "idle" or (self._state == "failed" and retry):
                self._start()
            return self._snapshot()

    def stop(self):
        """Stop the child this process started, if any. A service started elsewhere is left running."""
        with self._lock:
            proc, self._proc = self._proc, None
            if proc is not None and self._state in ("starting", "running"):
                self._idle()
        if proc is not None:
            stop_process(proc, self.stop_timeout)

    # ------------------------------------------------------------------ internals (the lock is held)
    def _snapshot(self):
        return {"state": self._state, "port": self._port, "owned": self._proc is not None,
                "message": self._message, "detail": self._detail}

    def _idle(self):
        self._state, self._port, self._message, self._detail = "idle", None, "", ""

    def _fail(self, message, detail=""):
        self._state, self._message, self._detail = "failed", message, detail
        log.warning("3D service: %s %s", message, detail)

    def _reap(self):
        if self._proc is None or self._proc.poll() is None:
            return
        code = self._proc.returncode
        was_running = self._state == "running"
        self._proc = None
        if was_running:
            self._idle()  # it starts again when the page next asks
            log.warning("3D service exited (code %s); it will be started again when the 3D page asks", code)
        elif self._state == "starting":
            self._fail(f"The 3D service stopped while starting (exit code {code}).", self._log_tail())

    def _log_tail(self):
        try:
            with open(self.log_path, "rb") as f:
                lines = f.read()[-4000:].decode(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join([line for line in lines if line.strip()][-LOG_TAIL_LINES:])

    def _preflight(self):
        if not os.path.isdir(self.root):
            return "The 3D service files (simulation3d/) are missing."
        if shutil.which("sumo") is None:
            return "SUMO is not installed (the 'sumo' command was not found). On Ubuntu: sudo apt install sumo sumo-tools"
        return None

    def _start(self):
        problem = self._preflight()
        if problem:
            return self._fail(problem)
        found = identify(self.preferred_port)
        if found:
            self._state, self._port, self._message, self._detail = "running", self.preferred_port, "", ""
            log.info("3D service: using the one already running on port %s (not started by this app)", self._port)
            return
        port = self.preferred_port
        if port_in_use(port):
            port = free_port()
            log.warning("3D service: port %s is used by another program, starting on port %s instead", self.preferred_port, port)
        instance = secrets.token_hex(16)
        env = dict(os.environ, ATCS3D_INSTANCE=instance, PYTHONDONTWRITEBYTECODE="1")  # nothing is cached inside simulation/
        env.setdefault("SUMO_HOME", "/usr/share/sumo")
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "wb") as out:
                proc = subprocess.Popen(
                    [self.python, "-m", "atcs3d.managed", "--port", str(port)], cwd=self.root, env=env,
                    stdin=subprocess.PIPE,  # held open while this process lives; its closing tells the service to exit
                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            return self._fail(f"The 3D service could not be started: {exc.strerror or exc}.")
        self._proc, self._instance, self._port = proc, instance, port
        self._state, self._message, self._detail = "starting", "Starting the 3D simulation service…", ""
        log.info("3D service: started pid %s on port %s", proc.pid, port)
        threading.Thread(target=self._await_ready, args=(proc, port, instance), daemon=True).start()

    def _await_ready(self, proc, port, instance):
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline and proc.poll() is None:
            if identify(port, instance) == "ours":
                with self._lock:
                    if self._proc is proc and self._state == "starting":
                        self._state, self._message = "running", ""
                return
            time.sleep(0.2)
        timed_out = proc.poll() is None
        with self._lock:
            if self._proc is proc and self._state == "starting":
                self._proc = None  # (so that nothing else reports the exit that follows)
                self._fail(f"The 3D service did not answer within {self.startup_timeout:g} seconds." if timed_out
                           else f"The 3D service stopped while starting (exit code {proc.returncode}).", self._log_tail())
        if timed_out:
            stop_process(proc, self.stop_timeout)


def init_app(app):
    """Create the service object for a Flask app; nothing is started until the page asks."""
    config = app.config
    disabled = None
    try:
        port = int(config.get("SIMULATOR3D_PORT") or DEFAULT_PORT)
        if not 1024 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        port, disabled = DEFAULT_PORT, "ATCS3D_PORT must be a number from 1024 to 65535."
    if disabled is None and not config.get("SIMULATOR3D_AUTOSTART", True):
        disabled = "Automatic start is switched off (ATCS3D_AUTOSTART=0). Start the service yourself: simulation3d/run.sh"
    if disabled is None and config.get("TESTING"):
        disabled = "Automatic start is off while testing."
    if disabled is None and not POSIX:
        disabled = "The 3D service runs on Linux or WSL. Start ATCS from WSL."
    service = Simulator3DService(config["SIMULATOR3D_DIR"], port, disabled)
    app.extensions["simulator3d"] = service
    if service.enabled:
        atexit.register(service.stop)  # Ctrl+C, normal exit, the reloader restarting its worker
        _stop_on_sigterm(service)
    return service


def _stop_on_sigterm(service):
    """`kill` on the web app ends it without running atexit handlers, unless something handles SIGTERM."""
    if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return  # (Flask's reloader installs its own handler, which exits normally)

    def on_sigterm(signum, frame):
        service.stop()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    signal.signal(signal.SIGTERM, on_sigterm)
