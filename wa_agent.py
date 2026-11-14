#!/usr/bin/env python3
"""Minimal WhatsApp agent for the RKLLM API server.

Built for a slow NPU with a single KV cache, so every design choice keeps the
prompt small and the conversation append-only:

- one request at a time: chat messages and scheduled tasks share one queue
- a short system prompt (constant within a conversation) and one `shell` tool
- history is only ever appended to, so the server can stay in KV-incremental
  mode and prefill just the new turn instead of the whole prompt
- tool output is truncated, because every 30 tokens of it costs ~1s of prefill

Standing knowledge lives in files, not in the conversation:

- notes.md           remembered facts, included in the system prompt
- tasks/<name>.md    scheduled jobs: a "schedule:" line plus instructions
- tasks/<name>.notes what the agent learned while running that job

Configuration is by environment variable (see AGENT.md).  Run with --cli to
chat on stdin without WhatsApp.
"""
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("wa_agent")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


API_URL = os.environ.get("AGENT_API_URL", "http://127.0.0.1:8000/v1").rstrip("/")
MODEL = os.environ.get("AGENT_MODEL", "")
DATA_DIR = os.environ.get("AGENT_DATA_DIR", "/root/agent")
WORKDIR = os.environ.get("AGENT_WORKDIR", "/root")
ALLOW_FROM = {n.strip().lstrip("+") for n in os.environ.get("AGENT_ALLOW_FROM", "").split(",") if n.strip()}
PREFIX = os.environ.get("AGENT_PREFIX", "").strip()
NOTIFY_TO = os.environ.get("AGENT_NOTIFY_TO", "").strip().lstrip("+")
MAX_STEPS = _env_int("AGENT_MAX_STEPS", 8)
TOOL_TIMEOUT = _env_int("AGENT_TOOL_TIMEOUT", 120)
TOOL_OUTPUT_CHARS = _env_int("AGENT_TOOL_OUTPUT_CHARS", 3000)
HISTORY_CHARS = _env_int("AGENT_HISTORY_CHARS", 24000)
NOTES_CHARS = _env_int("AGENT_NOTES_CHARS", 2000)
SESSION_IDLE = _env_int("AGENT_SESSION_IDLE", 3600)
REQUEST_TIMEOUT = _env_int("AGENT_REQUEST_TIMEOUT", 3600)
PROGRESS = os.environ.get("AGENT_PROGRESS", "1") == "1"
BASE_PROMPT = os.environ.get("AGENT_SYSTEM_PROMPT") or (
    "You are a Linux ops assistant answering over WhatsApp. "
    "Use the shell tool to inspect the system, one command at a time. "
    "When you have the answer, reply in short plain text."
)

NOTES_FILE = os.path.join(DATA_DIR, "notes.md")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
STATE_FILE = os.path.join(TASKS_DIR, ".state.json")

TOOLS = [{
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a shell command and return its output.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}]

HELP = (
    "/new - start a new conversation\n"
    "/status - model and history size\n"
    "/note <text> - remember something\n"
    "/notes - show what is remembered\n"
    "/task list\n"
    "/task add <name> <schedule> | <instructions>\n"
    "/task show|run|rm <name>\n"
    "Schedules: every 30m, every 6h, every 2d, daily 08:00"
)


def strip_prefix(text):
    """Return the message without AGENT_PREFIX, or None if it is not addressed
    to the agent.  With no prefix configured every message is."""
    if not PREFIX:
        return text
    if text[:len(PREFIX)].lower() != PREFIX.lower():
        return None
    rest = text[len(PREFIX):]
    if rest and not rest[0].isspace() and rest[0] not in ",:" and PREFIX[-1].isalnum():
        return None  # "botany ..." is not "bot ..."
    return rest.lstrip(" \t,:").strip() or None


def _read(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def _tail(text, limit):
    return text if len(text) <= limit else "[...]\n" + text[-limit:]


def _http_json(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def resolve_model():
    """Return AGENT_MODEL, or the first model the server lists (waits for it)."""
    if MODEL:
        return MODEL
    while True:
        try:
            models = _http_json(f"{API_URL}/models").get("data", [])
            if models:
                return models[0]["id"]
            log.warning("API lists no models yet")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("Waiting for the API at %s (%s)", API_URL, exc)
        time.sleep(5)


def run_shell(command):
    try:
        proc = subprocess.run(
            command, shell=True, cwd=WORKDIR, timeout=TOOL_TIMEOUT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace",
        )
        out = proc.stdout.strip()
        if proc.returncode != 0:
            out = f"{out}\n[exit code {proc.returncode}]".strip()
    except subprocess.TimeoutExpired:
        out = f"[command timed out after {TOOL_TIMEOUT}s]"
    if not out:
        out = "[no output]"
    if len(out) > TOOL_OUTPUT_CHARS:
        half = TOOL_OUTPUT_CHARS // 2
        out = f"{out[:half]}\n[... {len(out) - 2 * half} chars cut ...]\n{out[-half:]}"
    return out


def build_system_prompt():
    """Base prompt plus the notes file.  Built once per conversation so the
    prompt stays constant (and the KV cache valid) while the agent edits notes."""
    prompt = (f"{BASE_PROMPT}\nTo remember a fact for later conversations, append a line to "
              f"{NOTES_FILE}.")
    notes = _read(NOTES_FILE).strip()
    if notes:
        prompt += f"\n\nYour notes:\n{_tail(notes, NOTES_CHARS)}"
    return prompt


class Agent:
    """One append-only conversation against the chat completions API."""

    def __init__(self, model):
        self.model = model
        self.history = []
        self.system = None
        self.last_used = 0.0

    def reset(self):
        self.history = []
        self.system = None

    def history_chars(self):
        return sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or ""))
                   for m in self.history)

    def _complete(self):
        if self.system is None:
            self.system = build_system_prompt()
        body = {
            "model": self.model,
            "stream": False,
            "tools": TOOLS,
            "messages": [{"role": "system", "content": self.system}] + self.history,
        }
        resp = _http_json(f"{API_URL}/chat/completions", body, timeout=REQUEST_TIMEOUT)
        return resp["choices"][0]["message"]

    def handle(self, text, notify=lambda _msg: None):
        """Answer one user message; returns the reply text."""
        now = time.time()
        notes = []
        if self.history and SESSION_IDLE and now - self.last_used > SESSION_IDLE:
            self.reset()
        elif self.history_chars() > HISTORY_CHARS:
            self.reset()
            notes.append("(context was full, started a new conversation)")
        self.last_used = now

        self.history.append({"role": "user", "content": text})
        for _step in range(MAX_STEPS):
            msg = self._complete()
            calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            if not calls:
                self.history.append({"role": "assistant", "content": content})
                self.last_used = time.time()
                return "\n".join(notes + [content or "(empty answer)"])

            self.history.append({"role": "assistant", "content": content, "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                command = args.get("command") if isinstance(args, dict) else None
                if fn.get("name") != "shell" or not isinstance(command, str) or not command.strip():
                    result = "[error: call the shell tool with a 'command' string]"
                else:
                    log.info("shell: %s", command)
                    if PROGRESS:
                        notify(f"$ {command}")
                    result = run_shell(command)
                self.history.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        self.last_used = time.time()
        return "\n".join(notes + [f"Stopped after {MAX_STEPS} steps without a final answer. "
                                  "Send a follow-up to continue or /new to start over."])


# --- Scheduled tasks -------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_EVERY_RE = re.compile(r"^every\s+(\d+)\s*([mhd])$")
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$")


def parse_schedule(text):
    """'every 6h' -> ('every', 21600); 'daily 08:00' -> ('daily', (8, 0)); else None."""
    text = text.strip().lower()
    m = _EVERY_RE.match(text)
    if m and int(m.group(1)) > 0:
        return "every", int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    m = _DAILY_RE.match(text)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return "daily", (int(m.group(1)), int(m.group(2)))
    return None


def next_run(schedule, last):
    """Earliest time a task should run again, given when it last ran."""
    kind, value = schedule
    if kind == "every":
        return last + value
    t = time.localtime(last)
    due = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, value[0], value[1], 0, 0, 0, -1))
    return due if due > last else due + 86400


def _task_path(name, ext=".md"):
    return os.path.join(TASKS_DIR, name + ext)


def load_task(name):
    """Return (schedule or None, instructions) for a task file, or None if missing."""
    raw = _read(_task_path(name), None)
    if raw is None:
        return None
    first, _, body = raw.partition("\n")
    if first.lower().startswith("schedule:"):
        return parse_schedule(first.split(":", 1)[1]), body.strip()
    return None, raw.strip()


def list_tasks():
    try:
        return sorted(f[:-3] for f in os.listdir(TASKS_DIR)
                      if f.endswith(".md") and _NAME_RE.match(f[:-3]))
    except OSError:
        return []


def _load_state():
    try:
        return json.loads(_read(STATE_FILE, "{}"))
    except ValueError:
        return {}


def _save_state(state):
    os.makedirs(TASKS_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def due_tasks(now=None):
    """Names of tasks whose schedule has come up; marks them as run.

    A task seen for the first time is only registered, so it first runs at its
    next scheduled time rather than the moment the file appears."""
    now = now or time.time()
    state = _load_state()
    due = []
    changed = False
    for name in list_tasks():
        schedule = load_task(name)[0]
        if schedule is None:
            continue
        if name not in state:
            state[name] = now
            changed = True
        elif next_run(schedule, state[name]) <= now:
            state[name] = now
            changed = True
            due.append(name)
    if changed:
        _save_state(state)
    return due


def task_prompt(name):
    _schedule, body = load_task(name)
    prompt = f"Scheduled task '{name}'. Carry it out now.\n\nInstructions:\n{body}"
    learned = _read(_task_path(name, ".notes")).strip()
    if learned:
        prompt += f"\n\nWhat you learned on earlier runs:\n{_tail(learned, NOTES_CHARS)}"
    prompt += (
        f"\n\nIf you find a faster or more reliable way to do this task, append one line "
        f"about it to {_task_path(name, '.notes')}. "
        "Finish with a short report. If nothing needs the user's attention, reply exactly: NOTHING"
    )
    return prompt


class Core:
    """Routes chat messages, slash commands and scheduled tasks to the model."""

    def __init__(self, model):
        self.chat = Agent(model)

    def run_task(self, name, notify=lambda _msg: None):
        """Run a task in its own short conversation; returns the report."""
        if load_task(name) is None:
            return f"No task named {name}."
        log.info("Running task %s", name)
        report = Agent(self.chat.model).handle(task_prompt(name), notify)
        # Let the user follow up on the report in the normal chat.
        self.chat.history.append({"role": "user", "content": f"(scheduled task '{name}' ran)"})
        self.chat.history.append({"role": "assistant", "content": report})
        self.chat.last_used = time.time()
        return report

    def command(self, text, notify=lambda _msg: None):
        """Handle slash commands without the model; None if not a command."""
        cmd, _, rest = text.strip().partition(" ")
        cmd, rest = cmd.lower(), rest.strip()
        if cmd not in ("/new", "/status", "/note", "/notes", "/task", "/help"):
            return None  # includes messages that merely start with a path
        if cmd == "/new":
            self.chat.reset()
            return "Started a new conversation."
        if cmd == "/status":
            return (f"model: {self.chat.model}\nhistory: {len(self.chat.history)} messages, "
                    f"{self.chat.history_chars()} chars (limit {HISTORY_CHARS})\n"
                    f"tasks: {', '.join(list_tasks()) or 'none'}")
        if cmd == "/note" and rest:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(NOTES_FILE, "a", encoding="utf-8") as fh:
                fh.write(f"- {rest}\n")
            return "Noted. It applies from the next conversation (/new)."
        if cmd == "/notes":
            return _read(NOTES_FILE).strip() or "No notes yet."
        if cmd == "/task":
            return self._task_command(rest, notify)
        return HELP

    def _task_command(self, rest, notify):
        sub, _, arg = rest.partition(" ")
        sub, arg = sub.lower(), arg.strip()
        if sub == "list":
            state = _load_state()
            lines = []
            for name in list_tasks():
                schedule = load_task(name)[0]
                if schedule is None:
                    lines.append(f"{name}: no valid schedule (manual only)")
                else:
                    nxt = next_run(schedule, state.get(name, time.time()))
                    lines.append(f"{name}: next {time.strftime('%a %H:%M', time.localtime(nxt))}")
            return "\n".join(lines) or "No tasks."
        if sub == "add":
            head, sep, body = arg.partition("|")
            name, _, schedule = head.strip().partition(" ")
            if not sep or not body.strip() or not _NAME_RE.match(name) or not parse_schedule(schedule):
                return ("Usage: /task add <name> <schedule> | <instructions>\n"
                        "Name: lowercase letters, digits, - and _. "
                        "Schedules: every 30m, every 6h, every 2d, daily 08:00")
            os.makedirs(TASKS_DIR, exist_ok=True)
            with open(_task_path(name), "w", encoding="utf-8") as fh:
                fh.write(f"schedule: {schedule.strip().lower()}\n{body.strip()}\n")
            state = _load_state()
            state[name] = time.time()
            _save_state(state)
            return f"Task {name} saved. Try it now with: /task run {name}"
        if sub in ("show", "run", "rm") and not (_NAME_RE.match(arg) and load_task(arg) is not None):
            return f"No task named {arg or '?'}."
        if sub == "show":
            learned = _read(_task_path(arg, ".notes")).strip()
            return _read(_task_path(arg)).strip() + (f"\n\nLearned:\n{learned}" if learned else "")
        if sub == "run":
            return f"[{arg}] {self.run_task(arg, notify)}"
        if sub == "rm":
            for ext in (".md", ".notes"):
                if os.path.exists(_task_path(arg, ext)):
                    os.remove(_task_path(arg, ext))
            return f"Task {arg} removed."
        return HELP

    def dispatch(self, text, notify=lambda _msg: None):
        """Answer one inbound message (command or chat)."""
        try:
            reply = self.command(text, notify)
            if reply is None:
                reply = self.chat.handle(text, notify)
            return reply
        except Exception as exc:
            log.exception("Request failed")
            return f"Error: {exc}"


def run_cli(core):
    print("wa_agent CLI mode (no scheduler; use /task run). /help lists commands, Ctrl-D quits.")
    for line in sys.stdin:
        if line.strip():
            print(core.dispatch(line.strip(), notify=lambda m: print(f"  {m}", flush=True)), flush=True)


def run_whatsapp(core):
    import segno
    from neonize.client import NewClient
    from neonize.events import ConnectedEv, MessageEv
    from neonize.utils.jid import build_jid
    from neonize.utils.message import extract_text

    os.makedirs(DATA_DIR, exist_ok=True)
    qr_path = os.path.join(DATA_DIR, "qr.png")
    client = NewClient(os.path.join(DATA_DIR, "whatsapp.db"))
    inbox = queue.Queue()
    connected = threading.Event()
    seen_ids = set()
    sent_ids = set()
    sent_texts = set()
    started = time.time()

    def on_qr(_client, data):
        qr = segno.make_qr(data)
        qr.terminal(compact=True)
        try:
            qr.save(qr_path, scale=8)
        except OSError as exc:
            log.warning("Could not write %s: %s", qr_path, exc)
        log.info("Scan the QR code above (WhatsApp > Linked devices), or open %s", qr_path)

    client.event.qr(on_qr)

    def me_users():
        # get_me() crashes the process before login, so only call it once connected.
        if not connected.is_set():
            return set()
        try:
            me = client.get_me()
            return {u for u in (me.JID.User, me.LID.User) if u}
        except Exception:
            return set()

    def home_chat():
        """Where unprompted task reports go: AGENT_NOTIFY_TO or the self chat."""
        if NOTIFY_TO:
            return build_jid(NOTIFY_TO)
        return build_jid(client.get_me().JID.User)

    def send(chat, text):
        try:
            resp = client.send_message(chat, text)
            sent_ids.add(resp.ID)
            sent_texts.add(text)
        except Exception as exc:
            log.error("Sending to %s failed: %s", chat.User, exc)

    @client.event(ConnectedEv)
    def _on_connected(_client, _ev):
        connected.set()
        if os.path.exists(qr_path):
            os.remove(qr_path)
        log.info("WhatsApp connected as %s", ", ".join(sorted(me_users())) or "?")

    @client.event(MessageEv)
    def _on_message(_client, ev):
        src = ev.Info.MessageSource
        msg_id = ev.Info.ID
        if src.IsGroup or msg_id in seen_ids or msg_id in sent_ids:
            return
        seen_ids.add(msg_id)
        ts = ev.Info.Timestamp
        if ts > 1e12:
            ts /= 1000
        if ts and ts < started - 600:
            return  # backlog delivered on reconnect
        text = strip_prefix((extract_text(ev.Message) or "").strip())
        if not text:
            return
        if src.IsFromMe:
            # Only the "message yourself" chat, and never our own replies.
            if src.Chat.User not in me_users() or text in sent_texts:
                return
        else:
            senders = {u for u in (src.Sender.User, src.SenderAlt.User) if u}
            if not senders & ALLOW_FROM:
                log.warning("Ignoring message from %s (not in AGENT_ALLOW_FROM)", sorted(senders))
                return
        inbox.put(("msg", src.Chat, text))

    def worker():
        while True:
            kind, chat, text = inbox.get()
            if kind == "task":
                try:
                    report = core.run_task(text)
                except Exception as exc:
                    log.exception("Task %s failed", text)
                    report = f"failed: {exc}"
                if report.strip().upper().startswith("NOTHING"):
                    log.info("Task %s: nothing to report", text)
                else:
                    send(home_chat(), f"[{text}] {report}")
                continue
            if PROGRESS and not inbox.empty():
                send(chat, f"({inbox.qsize()} more queued)")
            send(chat, core.dispatch(text, notify=lambda m: send(chat, m)))

    def scheduler():
        connected.wait()
        while True:
            try:
                for name in due_tasks():
                    inbox.put(("task", None, name))
            except Exception:
                log.exception("Scheduler error")
            time.sleep(30)

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    log.info("Connecting to WhatsApp (session in %s)", DATA_DIR)
    client.connect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [agent] %(message)s")
    core = Core(resolve_model())
    log.info("Using model %s at %s", core.chat.model, API_URL)
    if "--cli" in sys.argv:
        run_cli(core)
    else:
        run_whatsapp(core)


if __name__ == "__main__":
    main()
