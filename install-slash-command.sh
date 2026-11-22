#!/bin/sh
# Installs the /ctxedit slash command for Claude Code (user level).
# It is a skill file with model invocation disabled: only typing /ctxedit runs it,
# and its description is not loaded into the model's context.
set -e
here=$(dirname "$(readlink -f "$0")")
dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/ctxedit"
mkdir -p "$dest"
sed "s#__CTXEDIT__#$here/ctxedit#g" "$here/slash-command/SKILL.md" > "$dest/SKILL.md"
echo "Installed $dest/SKILL.md"
echo "It points at $here/ctxedit; run this script again if you move the folder."
