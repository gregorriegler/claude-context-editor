"""ctxedit "Suggest cuts": ask a model which steps look safe to drop or trim.

This is the only part of ctxedit that sends session content off the machine.
It calls the installed Claude Code in headless mode, from a temporary folder,
with tools off and session persistence off.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

from ctxcore import LABELS

BATCH = 200
PREVIEW_HEAD = 300
PREVIEW_TAIL = 100
MAX_BATCH_CHARS = 300000   # full text: keep one request well inside the model's context
MAX_NUMBERED_CHARS = 150000  # a step larger than this is sent as a preview even in full-text mode
TRIMMABLE = ('tool_result', 'assistant')  # lines of tool output, sentences of Claude's text
REWRITE_TAG = '[summarised] '

SYSTEM = """You review the transcript of a coding-assistant session to find steps that can be removed to save context, without losing anything the assistant still needs to continue the work.

You get a JSON list of steps in chronological order. Each has: "step" (its position), "type", "tokens" (approximate size) and "preview" (the first and last characters of its content, or the full text).

Suggest dropping:
- failed attempts that were later replaced by a working one
- repeated reads of the same file (keep the latest read)
- long tool output that Claude already summed up in its next message
- small talk

Always keep:
- the user's instructions and decisions
- the latest state of each file
- errors that are still open
- anything a later step refers to

Use "trim" for a step that is worth keeping in part, such as a long tool result where only the start or end matters. Use "drop" for a step that can go entirely. Tool calls and their results are removed together, so name either one of the pair. Do not list steps that should simply stay.

Some steps have "numbered_text" instead of "preview": the full content, split into parts, each on its own row starting with its number and "|". Only for these steps you can say exactly what to trim:
- add "keep": a list of [first, last] number ranges (inclusive) of the parts to keep. Everything else in the step is removed. Keep what the assistant still needs: the lines with the actual finding, error or value, and enough around them to make sense. Prefer this form.
- or, only for a "Tool result" where keeping parts cannot do the job (the useful content is spread thinly over many lines), use the action "rewrite" and add "text": a much shorter replacement that states the facts of the output and invents nothing. Do not rewrite any other type of step.
A "trim" without "keep" is allowed when you cannot tell which parts matter.

Answer with a JSON array only, no prose and no code fence. One item per suggested step:
[{"step": <position>, "action": "drop" | "trim" | "rewrite" | "keep", "reason": "<one line>", "keep": [[1, 12], [40, 44]], "text": "<replacement>"}]
"keep" (the list) and "text" are optional as described above. If nothing should be cut, answer []."""


def preview(text, full):
    if full or len(text) <= PREVIEW_HEAD + PREVIEW_TAIL:
        return text
    return text[:PREVIEW_HEAD] + ' [...] ' + text[-PREVIEW_TAIL:]


def numbered(editor, sid):
    """The parts of a step that are still there, as (1-based number, text)."""
    gone = editor._st(sid).get('removed') or ()
    return [(k + 1, u[0]) for k, u in enumerate(editor.units(sid)) if k not in gone]


def build_items(editor, full=False):
    """What is sent: position, type, token count and a preview per live step.
    With full=True, steps that can be trimmed carry numbered parts instead."""
    items = []
    for st in editor.s.steps:
        if st.group != 'live' or editor.is_dropped(st.id):
            continue
        if st.kind == 'other':
            continue  # harness context: cannot be edited, so not worth sending
        if st.kind == 'thinking' and st.text.startswith('[thinking text not stored'):
            continue  # nothing readable to judge
        text = editor.effective_text(st.id) if st.editable else st.text
        item = {'step': st.index, 'type': LABELS[st.kind], 'tokens': editor.tokens(st.id)}
        parts = numbered(editor, st.id) if full and st.editable and st.kind in TRIMMABLE else []
        if len(parts) > 1 and len(text) <= MAX_NUMBERED_CHARS:
            item['numbered_text'] = '\n'.join('%d| %s' % p for p in parts)
        else:
            item['preview'] = preview(text, full and len(text) <= MAX_NUMBERED_CHARS)
        if st.tool:
            item['tool'] = st.tool
        items.append(item)
    return items


def _chars(item):
    return len(item.get('preview') or item.get('numbered_text') or '')


def batches(items, size=BATCH, max_chars=MAX_BATCH_CHARS):
    out, cur, chars = [], [], 0
    for item in items:
        n = _chars(item)
        if cur and (len(cur) >= size or chars + n > max_chars):
            out.append(cur)
            cur, chars = [], 0
        cur.append(item)
        chars += n
    if cur:
        out.append(cur)
    return out


def build_prompt(batch, n, total):
    head = 'Steps %d to %d of the session' % (batch[0]['step'], batch[-1]['step'])
    if total > 1:
        head += ' (part %d of %d; earlier and later steps exist that you cannot see, so be careful with anything that looks referenced elsewhere)' % (n + 1, total)
    return head + ':\n\n' + json.dumps(batch, ensure_ascii=False)


def parse_reply(text, valid_steps):
    """Model text -> list of {'step', 'action', 'reason'}. Tolerant of fences and prose."""
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if m:
        text = m.group(1).strip()
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end < start:
        raise ValueError('no JSON array in the model reply')
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError('model reply is not a list')
    out, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            step = int(item.get('step'))
        except (TypeError, ValueError):
            continue
        action = str(item.get('action', '')).lower()
        if step not in valid_steps or action not in ('drop', 'trim', 'rewrite', 'keep') or step in seen:
            continue
        seen.add(step)
        reason = ' '.join(str(item.get('reason', '')).split())[:240]
        sg = {'step': step, 'action': action, 'reason': reason}
        if action == 'trim' and isinstance(item.get('keep'), list):
            sg['keep'] = item['keep']
        if action == 'rewrite' and isinstance(item.get('text'), str):
            sg['text'] = item['text']
        out.append(sg)
    return out


def kept_parts(ranges, n):
    """[[first, last], ...] (1-based, inclusive; a bare number is one part) ->
    set of 0-based indices, or None if any of it is out of range."""
    kept = set()
    for r in ranges:
        if isinstance(r, int) and not isinstance(r, bool):
            r = [r, r]
        if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in r)):
            return None
        a, b = r
        if not 1 <= a <= b <= n:
            return None
        kept.update(range(a - 1, b))
    return kept


def resolve(editor, st, sg, was_numbered):
    """Turn one model suggestion into what the page can apply, or None.
    Anything the model got wrong falls back to a plain trim suggestion."""
    out = {'id': st.id, 'step': sg['step'], 'action': sg['action'], 'reason': sg['reason']}
    if sg['action'] == 'drop':
        return out
    if not st.editable:
        return None
    if sg['action'] == 'rewrite':
        text = (sg.get('text') or '').strip()
        current = editor.effective_text(st.id)
        if was_numbered and st.kind == 'tool_result' and text and len(REWRITE_TAG + text) < len(current):
            out['text'] = REWRITE_TAG + text
            out['tokens_after'] = max(1, len(out['text']) // 4)
            return out
        out['action'] = 'trim'
        return out
    if was_numbered and 'keep' in sg:
        n = len(editor.units(st.id))
        already = set(editor._st(st.id).get('removed') or ())
        kept = kept_parts(sg['keep'], n)
        if kept:
            removed = set(range(n)) - (kept - already)
            if removed == already:
                return None  # the model kept everything: nothing to suggest
            if len(removed) < n:
                out['removed'] = sorted(removed)
                out['cut'] = len(removed - already)
                out['parts'] = n - len(already)
    return out


def _result_text(stdout):
    """`claude -p --output-format json` prints one result object, or (newer
    versions) a list of events ending in it."""
    data = json.loads(stdout)
    if isinstance(data, list):
        results = [d for d in data if isinstance(d, dict) and d.get('type') == 'result']
        if not results:
            raise ValueError('no result in Claude Code output')
        data = results[-1]
    if data.get('is_error'):
        raise RuntimeError(str(data.get('result') or data.get('subtype') or 'Claude Code reported an error'))
    return data.get('result') or ''


def call_claude(prompt, model=None, timeout=600, env=None, system=None):
    exe = shutil.which('claude')
    if not exe:
        raise RuntimeError('the `claude` command was not found on PATH')
    cmd = [exe, '-p', '--output-format', 'json', '--no-session-persistence',
           '--tools', '', '--strict-mcp-config', '--system-prompt', system or SYSTEM]
    if model:
        cmd += ['--model', model]
    tmp = tempfile.mkdtemp(prefix='ctxedit-suggest-')
    try:
        # always UTF-8: the default on Windows is a legacy code page that cannot hold emoji or arrows
        p = subprocess.run(cmd, input=prompt, capture_output=True, encoding='utf-8', errors='replace', cwd=tmp,
                           timeout=timeout, env=env if env is not None else os.environ.copy())
    except subprocess.TimeoutExpired:
        raise RuntimeError('Claude Code did not answer within %d seconds' % timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        return _result_text(p.stdout)
    except ValueError:
        raise RuntimeError((p.stderr or p.stdout or 'no output from Claude Code').strip()[-400:])


def call_with_fallback(prompt, model=None, system=None):
    """Use Claude Code as configured. If that fails and ANTHROPIC_API_KEY is
    set, retry once on the Claude Code login instead (a key without credit
    otherwise shadows a working login). Returns (text, note)."""
    try:
        return call_claude(prompt, model, system=system), None
    except RuntimeError as first:
        if not os.environ.get('ANTHROPIC_API_KEY'):
            raise
        env = os.environ.copy()
        env.pop('ANTHROPIC_API_KEY', None)
        try:
            text = call_claude(prompt, model, env=env, system=system)
        except RuntimeError:
            raise first
        return text, 'ANTHROPIC_API_KEY failed (%s); used the Claude Code login instead.' % str(first)[:120]


def suggest(editor, full=False, model=None, caller=call_with_fallback):
    items = build_items(editor, full)
    if not items:
        return {'suggestions': [], 'errors': [], 'sent_steps': 0, 'batches': 0, 'notes': []}
    parts = batches(items)
    prompts = [build_prompt(b, n, len(parts)) for n, b in enumerate(parts)]

    def run(n):
        try:
            text, note = caller(prompts[n], model)
            return parse_reply(text, {i['step'] for i in parts[n]}), None, note
        except Exception as ex:  # one bad batch must not lose the others
            return [], 'steps %d-%d: %s' % (parts[n][0]['step'], parts[n][-1]['step'], ex), None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(len(parts))))
    suggestions, errors, notes = [], [], []
    for found, err, note in results:
        suggestions += found
        if err:
            errors.append(err)
        if note and note not in notes:
            notes.append(note)
    by_index = {st.index: st for st in editor.s.steps}
    was_numbered = {i['step'] for i in items if 'numbered_text' in i}
    out = []
    for sg in suggestions:
        st = by_index[sg['step']]
        if sg['action'] == 'keep' or not st.droppable:
            continue
        r = resolve(editor, st, sg, sg['step'] in was_numbered)
        if r:
            out.append(r)
    return {'suggestions': out, 'errors': errors, 'sent_steps': len(items), 'batches': len(parts),
            'sent_chars': sum(len(p) for p in prompts), 'notes': notes}


# ---------------------------------------------------------------- detours
# "Find detours": one request over an outline of the whole session. The model
# splits it into phases and names those that are finished and whose details the
# later work no longer needs (a yak shave, but also a finished refactoring), and
# writes a short note to leave in their place.

DETOUR_TAG = '[detour removed] '
OUTLINE_USER_CHARS = 2000
OUTLINE_CALL_CHARS = 200
OUTLINE_OVERLAP = 40
FILE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path', 'NotebookEdit': 'notebook_path'}

DETOUR_SYSTEM = """You review the outline of a coding-assistant session to find stretches of it that can be replaced by a short note without the rest of the work losing anything.

You get a JSON list of steps in chronological order. Each has "step" (its position), "type" ("You" is the user, "Claude" the assistant, "Tool call" a tool the assistant ran) and "text". Tool output is not shown and the assistant's longer messages are shortened.

Work in two passes.

1. Split the session into phases. A phase is a run of consecutive steps that pursues one goal: tracking down a bug, a refactoring done in several steps, a feature, a repair of the working environment, a side task, a discussion that ends in a decision. Go by what happened: what the user asked for, what the assistant says it is doing, which files and commands the tool calls touch, and where that changes. Do not go by particular words, and do not expect anyone to have announced that a phase began or ended.

2. Judge each phase by what the later steps do with it. Ask: after this phase was over, did the work that followed need how it went, or only how it came out? A phase can be replaced by a note when both hold:
- it is finished: its goal was reached or given up, and the session moved on to something else
- what follows uses at most its outcome (the files as they are now, a decision, a fact that was found), and two or three sentences can state that outcome
This is true of a side task or yak shave that the work came back from, and just as much of a finished refactoring, clean-up, investigation or environment fix that belonged to the main task. Whether a phase was on or off the main task does not matter; whether its details are still in use does.

Do not report:
- the phase the session ends in, or anything still in progress, undecided or failing
- a phase whose details later steps keep working with: a design that is still being implemented, an error that is still being chased, code that is still being changed in the way discussed there
- the steps where the user sets the task or gives instructions that still apply, unless the note can carry them in full
- a phase of fewer than four steps

Each reported phase is one contiguous range. It starts at the step where the phase begins (usually the user's message that asks for it, or the assistant's first step towards it) and ends at its last step, including the assistant's closing message about it. Ranges must not overlap. When in doubt about a border, report the smaller range.

For each one write a "note": two or three sentences in the assistant's own voice and past tense, to be left in the conversation in place of the whole stretch. It must say what was done and everything the later work relies on (the outcome, renamed things, new dependencies, decisions the user made, work that was put aside and restored). Do not list changed files; they are added automatically. Invent nothing.

Answer with a JSON array only, no prose and no code fence:
[{"first": <step>, "last": <step>, "reason": "<one line: what the phase was, and why the later work only needs its outcome>", "note": "<replacement note>"}]
If no phase qualifies, answer []."""


def _call_line(st):
    """One line for a tool call: the argument that says what it did."""
    try:
        args = json.loads(st.text)
    except ValueError:
        args = None
    if isinstance(args, dict):
        for key in ('command', 'file_path', 'notebook_path', 'pattern', 'url', 'query', 'description', 'prompt', 'skill'):
            if isinstance(args.get(key), str):
                return ('%s: %s' % (st.tool, ' '.join(args[key].split())))[:OUTLINE_CALL_CHARS]
    return ('%s: %s' % (st.tool, ' '.join(st.text.split())))[:OUTLINE_CALL_CHARS]


def build_outline(editor):
    """The whole live session, small: your messages in full, Claude's text as
    a preview, tool calls as one line, tool output and thinking left out."""
    out = []
    for st in editor.s.steps:
        if st.group != 'live' or editor.is_dropped(st.id):
            continue
        if st.kind in ('user', 'summary') and st.editable:
            text = editor.effective_text(st.id)[:OUTLINE_USER_CHARS]
        elif st.kind == 'assistant' and st.editable:
            text = preview(editor.effective_text(st.id), False)
        elif st.kind == 'tool_use':
            text = _call_line(st)
        else:
            continue
        out.append({'step': st.index, 'type': LABELS[st.kind], 'text': text})
    return out


def outline_parts(items, max_chars=MAX_BATCH_CHARS, overlap=OUTLINE_OVERLAP):
    """Normally one part. A very long outline is split, with an overlap so a
    detour near a border is seen whole by one of the two requests."""
    parts, start = [], 0
    while start < len(items):
        end, chars = start, 0
        while end < len(items) and (end == start or chars + len(items[end]['text']) <= max_chars):
            chars += len(items[end]['text'])
            end += 1
        parts.append(items[start:end])
        if end >= len(items):
            break
        start = max(end - overlap, start + 1)
    return parts


def parse_detours(text, lo, hi):
    """Model text -> [{'first', 'last', 'reason', 'note'}], inside [lo, hi],
    sorted, without overlaps (the earlier one wins)."""
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if m:
        text = m.group(1).strip()
    start, end = text.find('['), text.rfind(']')
    if start < 0 or end < start:
        raise ValueError('no JSON array in the model reply')
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError('model reply is not a list')
    found = []
    for item in data:
        if not isinstance(item, dict):
            continue
        first, last = item.get('first'), item.get('last')
        if not all(isinstance(x, int) and not isinstance(x, bool) for x in (first, last)):
            continue
        if not lo <= first <= last <= hi:
            continue
        found.append({'first': first, 'last': last,
                      'reason': ' '.join(str(item.get('reason', '')).split())[:240],
                      'note': str(item.get('note', '')).strip()[:1500]})
    found.sort(key=lambda d: (d['first'], -d['last']))
    out = []
    for d in found:
        if not out or d['first'] > out[-1]['last']:
            out.append(d)
    return out


def resolve_detour(editor, first, last):
    """What removing the steps first..last (positions) means in this session:
    which steps go, which one stays to carry the note, which files changed."""
    ids, files, anchor, fallback, tokens = [], [], None, None, 0
    for st in editor.s.steps:
        if st.group != 'live' or not first <= st.index <= last or editor.is_dropped(st.id):
            continue
        if st.droppable:
            ids.append(st.id)
            tokens += editor.tokens(st.id)
        if st.editable and st.droppable and st.kind == 'assistant':
            anchor = st.id  # the last message from Claude carries the note
        elif st.editable and st.droppable and st.kind == 'user':
            fallback = st.id
        if st.kind == 'tool_use' and st.tool in FILE_TOOLS:
            try:
                path = json.loads(st.text).get(FILE_TOOLS[st.tool])
            except (ValueError, AttributeError):
                path = None
            if isinstance(path, str) and path not in files:
                files.append(path)
    return {'first': first, 'last': last, 'ids': ids, 'anchor': anchor or fallback,
            'files': files, 'tokens': tokens}


def find_detours(editor, model=None, caller=None):
    caller = caller or (lambda prompt, m: call_with_fallback(prompt, m, system=DETOUR_SYSTEM))
    items = build_outline(editor)
    if not items:
        return {'detours': [], 'errors': [], 'notes': [], 'sent_steps': 0, 'requests': 0}
    parts = outline_parts(items)

    def run(n):
        part = parts[n]
        head = 'Outline of the session, steps %d to %d' % (part[0]['step'], part[-1]['step'])
        if len(parts) > 1:
            head += ' (part %d of %d of a longer session; only report detours that both start and end inside this part)' % (n + 1, len(parts))
        try:
            text, note = caller(head + ':\n\n' + json.dumps(part, ensure_ascii=False), model)
            return parse_detours(text, part[0]['step'], part[-1]['step']), None, note
        except Exception as ex:
            return [], 'steps %d-%d: %s' % (part[0]['step'], part[-1]['step'], ex), None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, range(len(parts))))
    found, errors, notes = [], [], []
    for got, err, note in results:
        found += got
        if err:
            errors.append(err)
        if note and note not in notes:
            notes.append(note)
    found.sort(key=lambda d: (d['first'], -d['last']))
    out = []
    for d in found:
        if out and d['first'] <= out[-1]['last']:
            continue  # the same detour seen by two overlapping requests
        r = resolve_detour(editor, d['first'], d['last'])
        if len(r['ids']) < 2:
            continue
        r.update(reason=d['reason'], note=d['note'], tag=DETOUR_TAG)
        out.append(r)
    return {'detours': out, 'errors': errors, 'notes': notes, 'sent_steps': len(items), 'requests': len(parts)}
