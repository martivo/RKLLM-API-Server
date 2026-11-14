# WhatsApp agent

`wa_agent.py` is a small agent built into the image. It links to WhatsApp as a
linked device (like WhatsApp Web), answers messages with the local model, and
can run shell commands inside the container to do so.

It is deliberately minimal, because on an RK3588 the prompt size decides
everything (prefill is ~30 tokens/s, generation ~3 tokens/s):

- one request at a time; chat messages and scheduled tasks share one queue
- a short system prompt and one tool, `shell`
- history is append-only, so the server stays in KV-incremental mode and only
  prefills the new turn
- tool output is cut to `AGENT_TOOL_OUTPUT_CHARS`

## Enable

```yaml
  rkllm:
    image: ghcr.io/martivo/rkllm-api-server:latest
    environment:
      - AGENT_ENABLED=1
      - AGENT_MODEL=qwen3-4b-instruct-2507-16k
      - TZ=Europe/Tallinn
    volumes:
      - /home/martivo/rkllm_models:/root/models
      - /home/martivo/rkllm_agent:/root/agent      # WhatsApp session, keep it
```

Start the container and run `docker logs -f rkllm`. A QR code is printed (and
saved as `qr.png` in the agent volume). Scan it in WhatsApp under
*Linked devices*. The session is stored in the volume, so this is needed once.

## Who can talk to it

- The **"message yourself" chat** of the linked account always works.
- Other senders only if their number is in `AGENT_ALLOW_FROM`
  (comma-separated, digits only, with country code). Rejected senders are
  logged with the ID to add.
- Group messages are ignored.
- With `AGENT_PREFIX` set (for example `bot`), only messages starting with
  that word are handled, and the word is removed: `bot /status`,
  `bot check the disk`. Everything else is ignored silently.

The shell tool runs as root inside the container, and this container is
normally privileged. Anyone who can message the agent can run commands on the
host. Keep the allow list short.

## Docker

The image contains the `docker` CLI and the compose plugin, but no daemon. To
let the agent inspect or manage containers, mount the host socket:

```yaml
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
```

## Helper commands

A 4B model cannot work out how to query an image registry, so jobs like that
are shipped as commands the model only has to run and summarise.

- `image-updates [compose-file]` lists newer tags for every `image:` in a
  compose file and every `FROM` / `COPY --from` in its build contexts. Tags
  are only compared like-for-like (`3.3.17` with `3.3.19` and `3.4.2`, never
  with `3.4-alpine`); `latest` and other non-version tags are reported as not
  checked. Default file: `/root/compose/docker-compose.yml`.

Tell the agent about a helper once with `/note`, or name it in a task.

## Commands

Commands are handled by the agent itself, so they are instant and never touch
the model.

- `/new` starts a new conversation
- `/status` shows the model, history size and tasks
- `/note <text>` remembers something; `/notes` shows what is remembered
- `/task list`
- `/task add <name> <schedule> | <instructions>`
- `/task show <name>`, `/task run <name>`, `/task rm <name>`

## Notes

`notes.md` in the agent volume is added to the system prompt of every new
conversation, so standing facts do not have to be repeated. `/note` appends
to it, and the model appends to it when you ask it to remember something. Keep it short:
every 30 tokens of notes cost about a second on each new conversation.

## Scheduled tasks

A task is a file `tasks/<name>.md` in the agent volume: a `schedule:` line and
free-text instructions.

```
/task add image-updates daily 08:00 | Run: image-updates /root/compose/docker-compose.yml and report the updates it lists. If it lists none, reply NOTHING.
```

Schedules are `every 30m`, `every 6h`, `every 2d` or `daily HH:MM` (container
local time, set `TZ`). A new task first runs at its next scheduled time; use
`/task run <name>` to try it at once.

- Each run is a fresh, short conversation, queued behind whatever is running.
- The report goes to the "message yourself" chat, or to `AGENT_NOTIFY_TO`.
  When the model answers `NOTHING`, no message is sent.
- The model is asked to append what it learned (for example the command that
  worked) to `tasks/<name>.notes`. Those lines are added to the next run, so
  the task improves without your instructions being rewritten. `/task show`
  prints both.
- After a report you can reply in the chat to follow up on it.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_ENABLED` | `0` | start the agent next to the API server |
| `AGENT_MODEL` | first model listed | model to use |
| `AGENT_ALLOW_FROM` | empty | extra phone numbers allowed to message the agent |
| `AGENT_PREFIX` | empty | keyword a message must start with (case-insensitive) |
| `AGENT_NOTIFY_TO` | self chat | phone number that receives scheduled task reports |
| `AGENT_SYSTEM_PROMPT` | ops assistant | system prompt; keep it short and constant |
| `AGENT_MAX_STEPS` | `8` | model calls per message |
| `AGENT_TOOL_TIMEOUT` | `120` | seconds per shell command |
| `AGENT_TOOL_OUTPUT_CHARS` | `3000` | shell output kept (head and tail) |
| `AGENT_NOTES_CHARS` | `2000` | notes included in the prompt (most recent part) |
| `AGENT_HISTORY_CHARS` | `24000` | history size that triggers a new conversation |
| `AGENT_SESSION_IDLE` | `3600` | idle seconds after which a new conversation starts |
| `AGENT_PROGRESS` | `1` | send each shell command to the chat as it runs |
| `AGENT_WORKDIR` | `/root` | working directory for shell commands |
| `AGENT_DATA_DIR` | `/root/agent` | WhatsApp session, QR image, notes and tasks |
| `AGENT_API_URL` | `http://127.0.0.1:8000/v1` | API to call |

## Try it without WhatsApp

```
docker exec -it rkllm python /app/wa_agent.py --cli
```
