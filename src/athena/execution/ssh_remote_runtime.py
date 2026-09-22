"""Authenticated remote runtime programs used by the SSH backend."""

from __future__ import annotations


_REMOTE_RELAY = r"""
import json, os, select, socket, sys
socket_path, token_path = sys.argv[1:]
with open(token_path, encoding="utf-8") as handle:
    token = handle.read().strip()
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect(socket_path)
connection.sendall((json.dumps({"token": token, "op": "stream", "protocol": "athena-ssh-supervisor", "version": 1}, separators=(",", ":")) + "\n").encode())
while True:
    readable, _, _ = select.select([0, connection], [], [])
    if 0 in readable:
        chunk = os.read(0, 65536)
        if not chunk:
            break
        connection.sendall(chunk)
    if connection in readable:
        chunk = connection.recv(65536)
        if not chunk:
            break
        os.write(1, chunk)
connection.close()
"""

# The control process never executes user source. It owns a separate worker
# process and handles each authenticated socket connection in its own thread,
# so a blocked worker cannot prevent describe/shutdown from being serviced.
_REMOTE_PYTHON_SUPERVISOR_V2 = r"""
import base64, hashlib, json, os, re, signal, socket, subprocess, sys, threading, time

PROTOCOL_NAME = "athena-ssh-supervisor"
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_LENGTH_LINE_BYTES = 64

socket_path, token_path, metadata_path, session_id, task_id, runtime, remote_cwd, authority_digest = sys.argv[1:]
if runtime not in {"python", "node", "shell"}:
    raise SystemExit("unsupported remote supervisor runtime")

def identity(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return f"{pid}:{handle.read().split()[21]}"
    except (OSError, IndexError):
        return f"{pid}:unknown"

WORKER = r'''import contextlib, json, os, pty, queue, secrets, signal, subprocess, sys, termios, threading, time, traceback
runtime = sys.argv[1] if len(sys.argv) > 1 else "python"
remote_cwd = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
state = {"__name__": "__main__"}

NODE_SOURCE = r"const vm=require('vm');const context=vm.createContext({});function send(o){process.stdout.write(JSON.stringify(o)+'\n');}context.console={log:(...a)=>send({type:'out',data:a.map(String).join(' ')}),error:(...a)=>send({type:'err',data:a.map(String).join(' ')}),warn:(...a)=>send({type:'err',data:a.map(String).join(' ')})};context.globalThis=context;context.process={env:Object.freeze({...process.env})};let buf=Buffer.alloc(0);function consume(){const nl=buf.indexOf(10);if(nl===-1)return false;const length=parseInt(buf.slice(0,nl).toString('utf8').trim(),10);if(isNaN(length)||buf.length<nl+1+length)return false;const payload=buf.slice(nl+1,nl+1+length).toString('utf8');buf=buf.slice(nl+1+length);let message;try{message=JSON.parse(payload);}catch(_){send({type:'err',data:'invalid node execution request'});send({type:'done',ok:false});return true;}let ok=true;try{new vm.Script(String(message.source||'')).runInContext(context,{timeout:10000});}catch(error){ok=false;send({type:'err',data:error&&error.stack?error.stack:String(error)});}send({type:'done',ok});return true;}process.stdin.on('data',(chunk)=>{buf=Buffer.concat([buf,chunk]);while(consume()){} });"

node_process = None
shell_process = None
shell_master = None
shell_output = queue.Queue()
shell_threads = []

def _read_shell(stream, kind):
    try:
        for line in iter(stream.readline, ''):
            if not line:
                break
            shell_output.put((kind, line))
    except (OSError, ValueError):
        pass

def _read_pty(fd):
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            shell_output.put(chunk.decode("utf-8", "replace"))
    except OSError:
        pass

def ensure_runtime():
    global node_process, shell_process, shell_master, shell_threads
    if runtime == "python":
        return None
    if runtime == "node":
        if node_process is None or node_process.poll() is not None:
            node_process = subprocess.Popen(
                ["node", "-e", NODE_SOURCE], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=remote_cwd,
                start_new_session=True, text=True, encoding="utf-8", errors="replace",
                bufsize=0,
            )
        return node_process
    if shell_process is None or shell_process.poll() is not None:
        shell_master, slave = pty.openpty()
        terminal = termios.tcgetattr(slave)
        terminal[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, terminal)
        shell_env = os.environ.copy()
        shell_env.update({"PS1": "", "PS2": "", "TERM": "dumb"})
        shell_process = subprocess.Popen(
            ["bash", "--norc", "--noprofile", "-i"], stdin=slave,
            stdout=slave, stderr=slave, cwd=remote_cwd, env=shell_env,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)
        shell_thread = threading.Thread(target=_read_pty, args=(shell_master,), daemon=True)
        shell_threads = [shell_thread]
        shell_thread.start()
        time.sleep(0.05)
        while True:
            try:
                shell_output.get_nowait()
            except queue.Empty:
                break
    return shell_process

def stop_runtime():
    global node_process, shell_process, shell_master
    process = node_process if runtime == "node" else shell_process
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    if runtime == "node":
        node_process = None
    else:
        shell_process = None
        if shell_master is not None:
            try:
                os.close(shell_master)
            except OSError:
                pass
            shell_master = None

def run_node(source):
    process = ensure_runtime()
    if process is None or process.stdin is None or process.stdout is None:
        return [], ["node worker unavailable"], False
    payload = json.dumps({"source": source}, separators=(",", ":"))
    process.stdin.write(str(len(payload)) + "\n" + payload)
    process.stdin.flush()
    out, err, ok = [], [], True
    while True:
        line = process.stdout.readline()
        if not line:
            return out, err + ["node worker exited"], False
        frame = json.loads(line)
        if frame.get("type") == "done":
            ok = bool(frame.get("ok", True))
            return out, err, ok
        if frame.get("type") == "out": out.append(str(frame.get("data") or ""))
        elif frame.get("type") == "err": err.append(str(frame.get("data") or ""))

def run_shell(source):
    process = ensure_runtime()
    if process is None or shell_master is None:
        return [], ["shell worker unavailable"], False
    marker = "__ATHENA_REMOTE_SHELL_" + secrets.token_hex(16) + "__"
    wrapped = (
        "{\n" + source + "\n} ; athena_rc=$?\n"
        + "printf '%s%s\n' '" + marker + "' \"$athena_rc\"\n"
    )
    os.write(shell_master, wrapped.encode("utf-8"))
    out = []
    pending = ""
    deadline = time.monotonic() + 30.0
    while True:
        try:
            chunk = shell_output.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None:
                return out, ["shell worker exited"], False
            if time.monotonic() >= deadline:
                return out, ["shell worker completion marker timed out"], False
            continue
        pending += chunk
        marker_at = pending.find(marker)
        if marker_at >= 0:
            before = pending[:marker_at]
            status_line = pending[marker_at + len(marker):].split("\n", 1)[0]
            try:
                return out + [before], [], int(status_line.strip()) == 0
            except ValueError:
                return out + [before], ["shell worker returned an invalid status"], False

while True:
    line = sys.stdin.readline()
    if not line:
        break
    try:
        if len(line.encode()) > 64 or not line.endswith("\n"):
            raise ValueError("invalid frame length line")
        length = int(line.strip())
        if length < 0 or length > 8 * 1024 * 1024:
            raise ValueError("frame exceeds maximum size")
        payload = json.loads(sys.stdin.read(length))
        source = str(payload.get("source") or "")
    except Exception:
        sys.stdout.write(json.dumps({"type": "err", "data": "bad execution request"}) + "\n")
        sys.stdout.write(json.dumps({"type": "done", "ok": False}) + "\n")
        sys.stdout.flush()
        continue
    out, err = [], []
    class Capture:
        def __init__(self, target): self.target = target
        def write(self, value):
            if value: self.target.append(str(value))
        def flush(self): pass
    ok = True
    old_out, old_err = sys.stdout, sys.stderr
    try:
        if runtime == "python":
            sys.stdout, sys.stderr = Capture(out), Capture(err)
            ns = dict(state)
            exec(source, ns)
            state.update({k: v for k, v in ns.items() if not k.startswith("__")})
        elif runtime == "node":
            out, err, ok = run_node(source)
        else:
            out, err, ok = run_shell(source)
    except BaseException:
        ok = False
        err.append(traceback.format_exc())
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    if out:
        sys.stdout.write(json.dumps({"type": "out", "data": "".join(out)}) + "\n")
    if err:
        sys.stdout.write(json.dumps({"type": "err", "data": "".join(err)}) + "\n")
    sys.stdout.write(json.dumps({"type": "done", "ok": ok}) + "\n")
    sys.stdout.flush()
stop_runtime()
'''

os.makedirs(os.path.dirname(socket_path), exist_ok=True)
for path in (socket_path, token_path, metadata_path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
token = __import__("secrets").token_urlsafe(32)
with open(token_path, "w", encoding="utf-8") as handle:
    os.chmod(token_path, 0o600)
    handle.write(token)
    handle.flush()
    os.fsync(handle.fileno())
controller_pid = os.getpid()
metadata = {
    "protocol": PROTOCOL_NAME,
    "protocol_version": PROTOCOL_VERSION,
    "session_id": session_id,
    "task_id": task_id,
    "runtime": runtime,
    "pid": controller_pid,
    "start_identity": identity(controller_pid),
    "socket_path": socket_path,
    "token_path": token_path,
    "metadata_path": metadata_path,
    "authority_digest": authority_digest,
}
session_nonce = __import__("secrets").token_urlsafe(24)
runtime_identity = f"{runtime}:{sys.executable}:{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
metadata.update(
    {
        "session_nonce": session_nonce,
        "runtime_identity": runtime_identity,
        "worker_source_sha256": hashlib.sha256(WORKER.encode("utf-8")).hexdigest(),
    }
)
with open(metadata_path, "w", encoding="utf-8") as handle:
    os.chmod(metadata_path, 0o600)
    json.dump(metadata, handle, separators=(",", ":"))
    handle.flush()
    os.fsync(handle.fileno())

worker = None
worker_identity = None
session_env = {}
worker_lock = threading.Lock()
stop = threading.Event()
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(socket_path)
os.chmod(socket_path, 0o600)
server.listen(16)
server.settimeout(0.2)

def send(connection, value):
    value = dict(value)
    value.setdefault("protocol", PROTOCOL_NAME)
    value.setdefault("protocol_version", PROTOCOL_VERSION)
    connection.sendall((json.dumps(value, separators=(",", ":")) + "\n").encode())

def dependency_inventory(target, manager, requested_name):
    target = os.path.realpath(target)
    packages = []
    if manager == "python":
        for root, _, files in os.walk(target):
            for filename in files:
                if not filename.endswith(".dist-info/RECORD") and filename != "RECORD":
                    continue
                record_path = os.path.join(root, filename)
                if not record_path.endswith(".dist-info/RECORD"):
                    continue
                dist_info = os.path.basename(os.path.dirname(record_path))
                package_name = dist_info[:-10].rsplit("-", 1)[0]
                package_version = dist_info[:-10].rsplit("-", 1)[-1]
                entries = []
                with open(record_path, encoding="utf-8") as handle:
                    for line in handle:
                        parts = line.rstrip("\n").split(",", 2)
                        if len(parts) >= 2 and parts[1].startswith("sha256="):
                            entries.append((parts[0], parts[1]))
                entries.sort()
                digest = hashlib.sha256()
                for path, record_hash in entries:
                    digest.update(path.encode())
                    digest.update(b"\0")
                    digest.update(record_hash.encode())
                    digest.update(b"\n")
                if package_name.casefold().replace("-", "_") == requested_name.casefold().replace("-", "_"):
                    packages.append({
                        "name": package_name,
                        "resolved_version": package_version,
                        "record_hashes": [
                            path + ":" + record_hash for path, record_hash in entries[:10000]
                        ],
                        "record_entry_count": len(entries),
                        "record_manifest_sha256": digest.hexdigest(),
                    })
    elif manager == "node":
        def file_hash(path):
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            return digest.hexdigest()

        files = []
        for root, _, names in os.walk(target):
            for filename in names:
                path = os.path.realpath(os.path.join(root, filename))
                if path.startswith(target + os.sep):
                    files.append((os.path.relpath(path, target), file_hash(path)))
        files.sort()
        digest = hashlib.sha256()
        for path, file_digest in files:
            digest.update(path.encode())
            digest.update(b"\0")
            digest.update(file_digest.encode())
            digest.update(b"\n")
        package_json = os.path.join(target, "node_modules", *requested_name.split("/"), "package.json")
        package = {}
        if os.path.isfile(package_json):
            with open(package_json, encoding="utf-8") as handle:
                package = json.load(handle)
        package_lock = os.path.join(target, "package-lock.json")
        packages.append({
            "name": requested_name,
            "resolved_version": str(package.get("version") or ""),
            "record_entry_count": len(files),
            "record_manifest_sha256": digest.hexdigest(),
            "package_lock_sha256": file_hash(package_lock) if os.path.isfile(package_lock) else "",
        })
    canonical = {
        "manager": manager,
        "target": target,
        "packages": packages,
    }
    canonical["environment_fingerprint"] = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return canonical

def dependency_operation(request):
    manager = str(request.get("manager") or "")
    name = str(request.get("name") or "")
    version = str(request.get("version") or "")
    operation = str(request.get("operation") or "inventory")
    environment_id = str(request.get("environment_id") or "")
    if manager not in {"python", "node"}:
        return {"ok": False, "error": "unsupported dependency manager"}
    if not re.fullmatch(r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]{1,128}", name):
        return {"ok": False, "error": "invalid dependency name"}
    if environment_id and not re.fullmatch(r"[0-9a-f]{64}", environment_id):
        return {"ok": False, "error": "invalid dependency environment id"}
    if len(version) > 128 or any(char in version for char in "\x00\n\r"):
        return {"ok": False, "error": "invalid dependency version"}
    target = os.path.join(
        remote_cwd, ".athena", "environments", environment_id or "legacy", manager
    )
    os.makedirs(target, mode=0o700, exist_ok=True)
    if operation == "install":
        package = name + ("==" + version if manager == "python" and version else "")
        if manager == "node" and version:
            package += "@" + version
        command = (
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--no-input", "--target", target, package]
            if manager == "python"
            else ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund",
                  "--prefix", target, package]
        )
        completed = subprocess.run(
            command, cwd=remote_cwd, capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            return {
                "ok": False,
                "error": (completed.stderr or completed.stdout or "dependency install failed")[-4000:],
                "exit_code": completed.returncode,
                "target": target,
            }
    inventory = dependency_inventory(target, manager, name)
    expected = request.get("expected")
    if operation == "verify" and isinstance(expected, dict):
        package = inventory.get("packages", [{}])
        package = package[0] if isinstance(package, list) and package else {}
        for key in ("environment_fingerprint", "record_entry_count", "record_manifest_sha256"):
            actual = inventory.get(key, package.get(key) if isinstance(package, dict) else None)
            if key in expected and expected.get(key) not in (None, "") and actual != expected.get(key):
                return {"ok": False, "error": "remote dependency manifest mismatch", **inventory}
    return {"ok": True, "operation": operation, **inventory}

def ensure_worker(env=None):
    global worker, worker_identity
    if worker is not None and worker.poll() is None:
        return worker
    merged = os.environ.copy()
    merged.update({str(k): str(v) for k, v in session_env.items()})
    merged.update({str(k): str(v) for k, v in (env or {}).items()})
    worker = subprocess.Popen(
        [sys.executable, "-u", "-c", WORKER, runtime, remote_cwd],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=remote_cwd,
        env=merged,
        start_new_session=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    worker_identity = identity(worker.pid)
    metadata["worker_pid"] = worker.pid
    metadata["worker_start_identity"] = worker_identity
    return worker

def stop_worker():
    global worker
    current = worker
    if current is None:
        return {"confirmed": True, "worker_pid": None, "worker_start_identity": None, "alive_after": False}
    pid = current.pid
    start = worker_identity
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        current.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            current.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
    alive = current.poll() is None
    if not alive:
        try:
            alive = identity(pid) == start and os.kill(pid, 0) == 0
        except (OSError, ProcessLookupError):
            alive = False
    return {
        "confirmed": not alive,
        "worker_pid": pid,
        "worker_start_identity": start,
        "alive_after": alive,
    }

def handle(connection):
    global worker
    stream = None
    try:
        stream = connection.makefile("rb")
        auth_line = stream.readline()
        auth = json.loads(auth_line.decode())
        if (
            auth.get("token") != token
            or auth.get("protocol") != PROTOCOL_NAME
            or auth.get("version") != PROTOCOL_VERSION
        ):
            send(connection, {"kind": "error", "error": "remote supervisor authentication failed"})
            return
        operation = auth.get("op")
        if operation == "describe":
            described = dict(metadata)
            described["start_identity"] = identity(controller_pid)
            described["worker_alive"] = bool(worker is not None and worker.poll() is None)
            if worker is not None and worker.poll() is None:
                described["worker_start_identity"] = identity(worker.pid)
            send(connection, {"kind": "response", **described})
            return
        if operation == "shutdown":
            receipt = stop_worker()
            receipt.update({"kind": "response", "controller_pid": controller_pid})
            send(connection, receipt)
            stop.set()
            return
        if operation == "interrupt":
            current = worker
            interrupted = bool(current is not None and current.poll() is None)
            if interrupted:
                try:
                    os.killpg(current.pid, signal.SIGINT)
                except ProcessLookupError:
                    interrupted = False
            send(
                connection,
                {
                    "kind": "response",
                    "interrupted": interrupted,
                    "worker_pid": current.pid if current is not None else None,
                },
            )
            return
        if operation == "dependency":
            send(connection, {"kind": "response", **dependency_operation(auth)})
            return
        while True:
            length_line = stream.readline()
            if not length_line:
                break
            if len(length_line) > MAX_LENGTH_LINE_BYTES or not length_line.endswith(b"\n"):
                raise ValueError("invalid frame length line")
            length = int(length_line.strip())
            if length < 0 or length > MAX_FRAME_BYTES:
                raise ValueError("frame exceeds maximum size")
            payload = stream.read(length)
            if len(payload) != length:
                break
            request = json.loads(payload.decode())
            if request.get("op") == "configure":
                session_env.update(
                    {str(key): str(value) for key, value in (request.get("env") or {}).items()}
                )
                ensure_worker()
                continue
            current = ensure_worker()
            if current.stdin is None or current.stdout is None:
                break
            with worker_lock:
                current.stdin.write(length_line.decode())
                current.stdin.write(payload.decode())
                current.stdin.flush()
                while True:
                    frame = current.stdout.readline()
                    if not frame:
                        break
                    connection.sendall(frame.encode())
                    try:
                        if json.loads(frame).get("type") == "done":
                            break
                    except Exception:
                        pass
                if current.poll() is not None:
                    break
    except (BrokenPipeError, ConnectionError, ValueError, json.JSONDecodeError, OSError):
        pass
    finally:
        if stream is not None:
            try: stream.close()
            except Exception: pass
        try: connection.close()
        except Exception: pass

threads = []
try:
    while not stop.is_set():
        try:
            connection, _ = server.accept()
        except socket.timeout:
            continue
        thread = threading.Thread(target=handle, args=(connection,), daemon=True)
        threads.append(thread)
        thread.start()
finally:
    try: server.close()
    except Exception: pass
    for thread in threads:
        thread.join(timeout=2.0)
    try: os.unlink(socket_path)
    except FileNotFoundError: pass
    try: os.unlink(token_path)
    except FileNotFoundError: pass
    try: os.unlink(metadata_path)
    except FileNotFoundError: pass
"""

__all__ = [
    "_REMOTE_RELAY",
    "_REMOTE_PYTHON_SUPERVISOR_V2",
]
