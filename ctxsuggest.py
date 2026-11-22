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


def call_claude(prompt, model=None, timeout=600, env=None):
    exe = shutil.which('claude')
    if not exe:
        raise RuntimeError('the `claude` command was not found on PATH')
    cmd = [exe, '-p', '--output-format', 'json', '--no-session-persistence',
           '--tools', '', '--strict-mcp-config', '--system-prompt', SYSTEM]
    if model:
        cmd += ['--model', model]
    tmp = tempfile.mkdtemp(prefix='ctxedit-suggest-')
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tmp,
                           timeout=timeout, env=env if env is not None else os.environ.copy())
    except subprocess.TimeoutExpired:
        raise RuntimeError('Claude Code did not answer within %d seconds' % timeout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        return _result_text(p.stdout)
    except ValueError:
        raise RuntimeError((p.stderr or p.stdout or 'no output from Claude Code').strip()[-400:])


def call_with_fallback(prompt, model=None):
    """Use Claude Code as configured. If that fails and ANTHROPIC_API_KEY is
    set, retry once on the Claude Code login instead (a key without credit
    otherwise shadows a working login). Returns (text, note)."""
    try:
        return call_claude(prompt, model), None
    except RuntimeError as first:
        if not os.environ.get('ANTHROPIC_API_KEY'):
            raise
        env = os.environ.copy()
        env.pop('ANTHROPIC_API_KEY', None)
        try:
            text = call_claude(prompt, model, env=env)
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
