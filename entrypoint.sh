#!/bin/sh
# Start the API server, plus the WhatsApp agent when AGENT_ENABLED=1.
# The agent is restarted if it exits; the container lives and dies with gunicorn.
if [ "${AGENT_ENABLED:-0}" = "1" ]; then
  (
    while true; do
      python /app/wa_agent.py
      echo "[agent] exited with code $?, restarting in 10s"
      sleep 10
    done
  ) &
fi
exec gunicorn --config gunicorn.config.py api:app
