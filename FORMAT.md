# Claude Code session format: what is actually on this machine

Inspected 2026-11-22: 39 session files under `~/.claude/projects/` written by
Claude Code 2.1.288 to 2.1.330, plus test sessions made for this purpose with
2.1.330 (tool use, built-in fork, `/compact`, subagent). One real interactive
fork (2.1.310-312) was also compared with its source.

The format is undocumented. Everything here is observation, and each claim
about resuming was checked by resuming a real file (see "Tested behaviour").

## Files

| Path | What |
|---|---|
| `<project>/<session-id>.jsonl` | the session, one JSON object per line |
| `<project>/<session-id>/subagents/agent-<id>.jsonl` (+ `.meta.json`) | subagent transcripts |
| `<project>/<session-id>/tool-results/*` | large tool outputs and images, referenced by absolute path from the session |
| `<project>/memory/` | project memory, not per session |

The project folder name is the working directory with every non-alphanumeric
character replaced by `-`.

## Entry types found

Entries fall in two families.

**Chain entries** carry `uuid` and `parentUuid` and form the conversation tree.

| `type` | Count | Notes |
|---|---|---|
| `assistant` | 7262 | `message` is the API message. One entry per content block (see below). |
| `user` | 4495 | prompts (`content` is a string), tool results and pasted images (`content` is a list) |
| `attachment` | 4409 | context injected by the harness: `environment`, `model`, `skill_listing`, `deferred_tools_delta`, `agent_listing_delta`, `mcp_instructions_delta`, `total_tokens_reminder`, `session_context`, `date`, `credential_org`, `remote_session_change`, `prompt_snapshot`, `instructions`, `file`, ... The text sent to the model is in `rendered[].content`; `renderedRole` says as which role. |
| `system` | 1251 | subtypes `turn_duration`, `away_summary`, `local_command`, `informational`, `compact_boundary` |

**Bookkeeping entries** have no `uuid` and are not part of the tree.

| `type` | Count | Notes |
|---|---|---|
| `api-request`, `api-request-blob`, `api-request-shape` | 2917 / 3286 / 60 | a delta-coded log of every API request. Blobs hold full copies of message content, keyed by hash. Not read on resume (the built-in fork drops them and rebuilds them). |
| `last-prompt` | 1460 | `lastPrompt`, `leafUuid` |
| `mode`, `permission-mode`, `atis-latch`, `session-owner`, `tools-used` | | repeated state; last value wins |
| `ai-title`, `custom-title`, `agent-name` | 1401 / 14 / 622 | titles; `custom-title` wins over `ai-title` in the resume picker |
| `file-history-snapshot`, `file-history-delta` | 1119 / 72 | checkpoint data; `messageId` refers to a chain entry |
| `queue-operation`, `cost-state`, `pr-link`, `data-flow-records`, `data-flow-incomplete` | | |
| `derived-from` | | lineage: list of `{type, id, via}` with `via` = `subprocess`, `fork`, `compact` |
| `continued-in` | 6 | appended to the *source* session by an interactive fork |

## Block types found

| Where | Block | Fields |
|---|---|---|
| assistant | `text` | `text` |
| assistant | `thinking` | `thinking`, `signature`. The `thinking` text is often empty (only the signature is stored). |
| assistant | `tool_use` | `id`, `name`, `input`, `caller` |
| user | `tool_result` | `tool_use_id`, `content`, `is_error`. `content` is a string or a list of `text`, `image`, `tool_reference`. |
| user | `text`, `image` | `image` has `source` |

Not seen: `redacted_thinking`, `server_tool_use`, `document`. The tool treats
any block it does not know as an uneditable "Other" card.

## The spec's "expected" items, checked

| Expected | Found |
|---|---|
| One JSON object per line | Yes. No unparseable line in 39 files. |
| Common fields `type`, `uuid`, `parentUuid`, `sessionId`, `timestamp`, `isSidechain`, `message` | Only on chain entries. Bookkeeping entries have `type` and usually `sessionId`, nothing else in common. Chain entries also carry `cwd`, `version`, `gitBranch`, `userType`, `entrypoint` and many per-type fields. |
| `content` is a string or a list of blocks | Yes. User prompts are strings; everything else is a list. |
| Blocks `text`, `thinking`, `tool_use`, `tool_result`, `image` | Yes, plus `tool_reference` inside tool results. |
| One assistant entry can hold several tool calls | **Not any more.** Since at least 2.1.288 each content block of an API response is its own `assistant` entry; the entries share `message.id` and are numbered by `apiBlockIndex`. Six entries in older files still hold two blocks, so the tool handles both. |
| Sidechain entries in the session file | **No.** `isSidechain` is `false` on every chain entry in every main file. Subagent work lives in `<session-id>/subagents/` with `isSidechain: true` and an `agentId`. The tool still groups any sidechain entry it finds and copies it unchanged. |

## Things the spec did not expect

1. **The tree is not a simple list.** With parallel tool calls, the response
   is written as `tool_use A -> tool_use B -> result B -> ...`, and `result A`
   hangs off `tool_use A` as a side leaf. Walking `parentUuid` from the last
   entry misses it. It is still in the live context: Claude Code splices such
   siblings (same `message.id`) and their results back in when loading. The
   tool does the same. Without this, 15 of the 33 real sessions lose steps.
2. **Abandoned branches.** After a rewind or an edited prompt, the old branch
   stays in the file. 15 sessions have entries that are on no live path. They
   are shown in a collapsed "other branches" group and copied unchanged.
3. **Attachments are context.** `skill_listing` alone is about 4,700 tokens.
   They are shown as "Other" cards with their token count and are not editable.
4. **Duplicated content.** A tool result's entry also has `toolUseResult`
   (structured copy for the UI) and assistant tool calls have
   `wireToolInputs`. Neither is sent to the model (tested). An edited tool
   result keeps its stale `toolUseResult`, so the transcript UI of the resumed
   session may render the original output for that one call, while the model
   sees the edited text.

## Compaction

`/compact` appends, in this order:

1. a `system` entry with `subtype: "compact_boundary"`, **`parentUuid: null`**,
   `logicalParentUuid` (the last entry before compaction) and
   `compactMetadata` (`trigger`, `preTokens`, `postTokens`,
   `preservedSegment {headUuid, anchorUuid, tailUuid}`,
   `preservedMessages {anchorUuid, uuids, allUuids}`)
2. a `user` entry with `isCompactSummary: true` whose parent is the boundary.
   Its `content` is the summary text.
3. the entries that follow.

Entries kept verbatim through the compaction (the "preserved segment") are
*not* rewritten. They stay where they were, before the boundary, with their
old `parentUuid`. The first entry after the summary has the segment's tail as
its parent. The loader re-attaches the segment's head to the summary
(`anchorUuid`).

So the live context is: boundary, summary, preserved segment, later entries.
Everything else before the boundary is dead and shown under "before last
compaction". When a preserved entry is dropped, the tool updates
`preservedSegment` and `preservedMessages` to match (tested: resumes).

## Built-in fork, compared

`claude --resume <id> --fork-session` (2.1.330, headless) and one interactive
fork (2.1.310) produce:

- a new `<new-uuid>.jsonl` in the same project folder
- at the top, one each of: `custom-title` (`"<title> (2)"`, interactive only),
  `ai-title`, `agent-name`, `mode`, `permission-mode`, `session-owner`,
  `atis-latch`, `tools-used`, `derived-from` (with a new `{id: <source>, via:
  "fork"}` item)
- every chain entry and `file-history-snapshot`, in file order, with the same
  `uuid` and `parentUuid`. Changed fields: `sessionId` (new id), `promptId`
  (new), and on assistant entries a new `requestSessionId` (the source id).
- **not** copied: `api-request*`, `last-prompt`, `cost-state`,
  `queue-operation`, `data-flow-*`
- **the sibling folder is not copied.** Tool results that point into
  `<source-id>/tool-results/` keep pointing there.
- the interactive fork appends `continued-in` to the source file. ctxedit
  never writes to the source, so it does not do this.

ctxedit writes the same layout, with `custom-title` and `agent-name` set to
`<original title> (edited)`. Other unknown bookkeeping types are copied through
in place.

## Tested behaviour

Each line is a real `claude --resume` of a file written by ctxedit (2.1.330).

| Question | Result |
|---|---|
| Does an unedited ctxedit fork resume? | Yes. Claude answered about the first turn. |
| Two user entries, or two assistant entries, in a row after a drop? | Accepted in both cases. No merging needed. |
| Thinking block dropped from a turn that keeps its tool calls? | Resumes. |
| Tool calls dropped so that a thinking block is alone in its turn? | Resumed in this test, but a lone signed thinking block is an invalid assistant turn for the API, so the tool locks it: when every other block of a response is dropped, its thinking block is dropped too. |
| Tool pair dropped from the middle? | Resumes; Claude no longer knows the content. |
| Tool result and assistant text rewritten? | Resumes; Claude quotes the new wording. |
| Preserved message dropped from a compacted session? | Resumes. |
| Do stale `usage` numbers matter? | The status-line meter reads them (all zeros shows 0%), `/context` re-estimates. A stale value of 99% did not trigger auto-compact, and the numbers are replaced by the first new response. ctxedit leaves them unchanged, as the built-in fork does. |
| Does the fork show in the resume picker? | Yes, under the "(edited)" title. Sessions created with `claude -p` are hidden from the picker, forks of them too; `claude --resume <id>` still works. |
