---
name: ctxedit
description: Open the current session in the ctxedit context editor (user-typed only).
disable-model-invocation: true
---

The user typed /ctxedit. The context editor for this session has already been started in the background by the line below; you do not need to run anything.

!`__CTXEDIT__ ${CLAUDE_SESSION_ID} --idle-exit 10 --detach 2>&1 || echo FAILED`

Reply with one or two short lines and nothing else, and call no tools:

- If the output above says "running in the background": say the editor opened in the browser with a snapshot of this session; that after "Save as fork" they should /exit and run the `claude --resume <new-id>` command it shows; and that if no tab appeared, they can open the URL shown above. The editor stops by itself 10 minutes after its tab is closed.
- Otherwise: show the output text as is.
