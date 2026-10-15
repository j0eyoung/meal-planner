"""Claude Code CLI with the user's subscription (no API fees), same setup as the workout add-on.

Sign-in happens inside the app: `claude auth login --claudeai` prints a link, the user approves it in a
browser and pastes the code back. The login lives in CLAUDE_CONFIG_DIR (/data/claude) so it survives
updates. A token from the Configuration tab (`claude setup-token`) also works."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

CONFIG_DIR = os.getenv("CLAUDE_CONFIG_DIR", "")
_status_cache = (0.0, {})
_login = None
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_AUTH_RE = re.compile(r"\b401\b|invalid api key|please run /login|authentication_error|not logged in|"
                      r"failed to authenticate|oauth (?:token|session)[^.\n]*(?:expired|invalid|revoked)", re.I)


def binary():
    return shutil.which("claude")


def _env():
    env = dict(os.environ, DISABLE_AUTOUPDATER="1", BROWSER="none", NO_COLOR="1")
    env.pop("ANTHROPIC_API_KEY", None)  # an API key would override the subscription login
    env.pop("SUPERVISOR_TOKEN", None)   # the CLI never needs Home Assistant access
    env.pop("HASSIO_TOKEN", None)
    if CONFIG_DIR:
        env["CLAUDE_CONFIG_DIR"] = CONFIG_DIR
    if signed_in():                     # a saved (possibly expired) token beats the in-app login
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


def status(max_age=600):
    """`claude auth status` as a dict ({} if the CLI is missing or fails); cached because it starts a program."""
    global _status_cache
    if time.time() - _status_cache[0] < max_age:
        return _status_cache[1]
    result = {}
    if binary():
        try:
            env = dict(os.environ, NO_COLOR="1")
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("SUPERVISOR_TOKEN", None)
            if CONFIG_DIR:
                env["CLAUDE_CONFIG_DIR"] = CONFIG_DIR
            out = subprocess.run([binary(), "auth", "status"], capture_output=True, text=True, timeout=20,
                                 env=env, stdin=subprocess.DEVNULL).stdout
            result = json.loads(out[out.find("{"):]) if "{" in out else {}
        except Exception:
            result = {}
    _status_cache = (time.time(), result)
    return result


def forget_status():
    global _status_cache
    _status_cache = (0.0, {})


def signed_in():
    s = status()
    return bool(s.get("loggedIn")) and s.get("authMethod") in ("claude.ai", "oauth_token", "claude_ai")


def available():
    """True when the CLI can run: in-app login or a Configuration-tab token."""
    return binary() is not None and (signed_in() or bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN")))


def logout():
    if binary():
        subprocess.run([binary(), "auth", "logout"], capture_output=True, text=True, timeout=20, env=_env(),
                       stdin=subprocess.DEVNULL)
    forget_status()


class Login:
    """One run of `claude auth login --claudeai`, driven from the web page."""

    def __init__(self):
        self.output = ""
        self.started = time.time()
        self.proc = subprocess.Popen([binary(), "auth", "login", "--claudeai"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     env=_env())
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.output += _ANSI.sub("", line)

    @property
    def url(self):
        m = re.search(r"https://\S+", self.output)
        return m.group(0) if m else None

    @property
    def done(self):
        return self.proc.poll() is not None

    @property
    def result(self):
        lines = [ln.strip() for ln in self.output.splitlines() if ln.strip() and "http" not in ln]
        return lines[-1] if lines else ""

    def send_code(self, code):
        if not self.done:
            self.proc.stdin.write("".join(code.split()) + "\n")
            self.proc.stdin.flush()

    def cancel(self):
        if not self.done:
            self.proc.kill()


def login_state():
    global _login
    if _login and not _login.done and time.time() - _login.started > 600:
        _login.cancel()
    if _login and _login.done:
        forget_status()
    return {"signed_in": signed_in(), "token": bool(os.getenv("CLAUDE_CODE_OAUTH_TOKEN")), "cli": binary() is not None,
            "login": None if not _login else {"url": _login.url, "done": _login.done, "result": _login.result}}


def start_login():
    global _login
    if _login:
        _login.cancel()
    _login = Login() if binary() else None
    return login_state()


def send_code(code):
    if _login:
        _login.send_code(code)
        time.sleep(3)  # give the CLI a moment to finish before the page asks again
    return login_state()


def cancel_login():
    global _login
    if _login:
        _login.cancel()
    _login = None
    return login_state()


def run(prompt, model=None, image_b64=None, timeout=600):
    """One `claude -p` call. Raises RuntimeError on failure.

    The prompt goes on stdin (no argv length limit, and no variadic flag can swallow it). Text calls get no
    tools at all. Image calls run in a private temp folder with only the Read tool: reads inside the working
    folder need no approval, anything outside it is refused (print mode never asks), and no transcript is kept."""
    global _status_cache
    args = [binary() or "claude", "-p", "--no-session-persistence", "--permission-mode", "dontAsk"]
    if model:
        args += ["--model", model]
    workdir = None
    try:
        if image_b64:
            import base64
            import binascii
            try:
                data = base64.b64decode(image_b64, validate=True)   # before any file exists, so nothing leaks
            except (binascii.Error, ValueError):
                raise RuntimeError("That photo could not be read. Try again.")
            workdir = tempfile.mkdtemp(prefix="mp-")
            with open(os.path.join(workdir, "photo.jpg"), "wb") as f:
                f.write(data)
            args += ["--tools", "Read"]
            prompt = f"Look at the image file ./photo.jpg in the current folder (use the Read tool), then: {prompt}"
        else:
            args += ["--tools", ""]
        out = subprocess.run(args, input=prompt, capture_output=True, text=True, check=True, timeout=timeout,
                             env=_env(), cwd=workdir)
        return out.stdout.strip()
    except subprocess.CalledProcessError as e:
        detail = _ANSI.sub("", (e.stdout or "").strip() or (e.stderr or "").strip()) or f"exit code {e.returncode}"
        if _AUTH_RE.search(detail):
            _status_cache = (time.time() - 540, {"loggedIn": False})   # re-check with `auth status` in ~1 min
            raise RuntimeError("Claude sign-in expired or invalid. Reconnect on the Plan tab.")
        raise RuntimeError(detail[:500])
    except subprocess.TimeoutExpired:
        raise RuntimeError("Claude took too long. Try again.")
    except OSError as e:
        raise RuntimeError(f"Claude could not start: {e}")
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
