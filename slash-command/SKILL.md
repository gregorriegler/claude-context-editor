---
name: ctxedit
description: Open the current session in the ctxedit context editor (user-typed only).
disable-model-invocation: true
---

The user typed /ctxedit. The context editor for this session has already been started in the background by the line below; you do not need to run anything.

!`s=; if command -v setsid >/dev/null 2>&1; then s=setsid; fi; ($s nohup __CTXEDIT__ ${CLAUDE_SESSION_ID} --idle-exit 10 > /tmp/ctxedit-${CLAUDE_SESSION_ID}.log 2>&1 &) ; sleep 1; if grep -q "is running" /tmp/ctxedit-${CLAUDE_SESSION_ID}.log; then echo "STARTED. Log with URL: /tmp/ctxedit-${CLAUDE_SESSION_ID}.log"; else echo "FAILED:"; cat /tmp/ctxedit-${CLAUDE_SESSION_ID}.log; fi`

Reply with one or two short lines and nothing else, and call no tools:

- If the output above says STARTED: say the editor opened in the browser with a snapshot of this session; that after "Save as fork" they should /exit and run the `claude --resume <new-id>` command it shows; and that if no tab appeared, the URL is in the log file named above. The editor stops by itself 10 minutes after its tab is closed.
- If it says FAILED: show the error text as is.
