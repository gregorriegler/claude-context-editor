"""ctxedit core: session parsing, editing, re-linking, fork building, save checks.

No UI and no network code lives here. See FORMAT.md for the session format
this module was written against (Claude Code 2.1.288 - 2.1.330).
"""
import copy
import json
import os
import re
import uuid as uuidlib

TRIM_MARKER = '[... trimmed]'
IMAGE_TOKENS = 1500  # flat estimate for one image; base64 length says nothing

# Session-local bookkeeping the built-in fork does not carry over.
FORK_SKIP = {
    'api-request', 'api-request-blob', 'api-request-shape', 'last-prompt',
    'cost-state', 'queue-operation', 'data-flow-records',
    'data-flow-incomplete', 'continued-in',
}
# Written once at the top of a fork (last value wins), in this order.
FORK_HEAD = [
    'custom-title', 'ai-title', 'agent-name', 'mode', 'permission-mode',
    'session-owner', 'atis-latch', 'tools-used', 'derived-from',
]
CHAIN_TYPES = {'user', 'assistant', 'attachment', 'system'}

LABELS = {
    'user': 'You', 'assistant': 'Claude', 'tool_use': 'Tool call',
    'tool_result': 'Tool result', 'thinking': 'Thinking',
    'summary': 'Summary', 'other': 'Other',
}


def _derive_id(new_id, old):
    return str(uuidlib.uuid5(uuidlib.NAMESPACE_OID, '%s/%s' % (new_id, old)))


def approx_tokens(text):
    return (len(text) + 3) // 4 if text else 0


# ---------------------------------------------------------------- splitting

_FENCE = re.compile(r'^\s{0,3}(```|~~~)')
_SENT = re.compile(r'.+?(?:[.!?]["\')\]*_`]*(?=\s)|$)', re.S)
_LIST_MARK = re.compile(r'^\s*(?:\d+[.)]|[-*+•]|#+)$')
_ABBR = re.compile(r'(?:\b(?:e\.g|i\.e|etc|vs|cf|approx|no|fig|mr|mrs|ms|dr|st)\.|\b[A-Za-z]\.)$', re.I)


def _split_ws(s):
    body = s.rstrip()
    return body, s[len(body):]


def split_sentences(text):
    """Split text into [body, separator] units. ''.join(b + s) == text.

    Fenced code blocks are one unit. Outside code, units end at sentence
    punctuation followed by whitespace, and at line breaks.
    """
    units = []
    lines = text.split('\n')
    lines = [l + '\n' for l in lines[:-1]] + ([lines[-1]] if lines[-1] else [])
    i = 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if m:
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(m.group(1)):
                j += 1
            block = ''.join(lines[i:j + 1])
            units.append(list(_split_ws(block)) + ['code'])
            i = j + 1
            continue
        line = lines[i]
        i += 1
        body, sep = _split_ws(line)
        if not body:
            if units:
                units[-1][1] += line
            else:
                units.append(['', line, 'text'])
            continue
        lead = body[:len(body) - len(body.lstrip())]
        parts = []
        pos = len(lead)
        for sm in _SENT.finditer(body, pos):
            if sm.end() == sm.start():
                continue
            seg = body[pos:sm.end()]
            # swallow following whitespace into the separator
            k = sm.end()
            while k < len(body) and body[k].isspace():
                k += 1
            parts.append([seg, body[sm.end():k]])
            pos = k
        if pos < len(body):
            parts.append([body[pos:], ''])
        merged = []
        for seg, s in parts:
            if merged and (_LIST_MARK.match(merged[-1][0]) or _ABBR.search(merged[-1][0])):
                merged[-1][0] += merged[-1][1] + seg
                merged[-1][1] = s
            else:
                merged.append([seg, s])
        if merged:
            merged[0][0] = lead + merged[0][0]
            merged[-1][1] += sep
        for seg, s in merged:
            units.append([seg, s, 'text'])
    return units


def split_lines(text):
    parts = text.split('\n')
    return [[p, '\n' if k < len(parts) - 1 else '', 'line'] for k, p in enumerate(parts)]


def join_sentences(units, removed):
    out = []
    pending = []
    last_sep = ''
    for k, (body, sep, _kind) in enumerate(units):
        if k in removed:
            pending.append(sep)
            continue
        if out:
            pending.append(last_sep)
            out.append(max(pending, key=lambda s: (s.count('\n'), len(s))))
        out.append(body)
        pending = []
        last_sep = sep
    if out and units and (len(units) - 1) not in removed:
        out.append(units[-1][1])
    return ''.join(out)


def join_lines(units, removed, marker):
    out = []
    in_gap = False
    for k, (body, _sep, _kind) in enumerate(units):
        if k in removed:
            if marker and not in_gap:
                out.append(TRIM_MARKER)
            in_gap = True
        else:
            out.append(body)
            in_gap = False
    return '\n'.join(out)


# ------------------------------------------------------------------ parsing

class Step(object):
    __slots__ = ('id', 'entry', 'block', 'kind', 'ts', 'text', 'tokens',
                 'editable', 'droppable', 'pair', 'group', 'tool', 'note',
                 'media', 'msg', 'uuid', 'index')

    def to_dict(self, full=True, preview=600):
        d = {
            'id': self.id, 'index': self.index, 'kind': self.kind,
            'label': LABELS[self.kind], 'ts': self.ts, 'tokens': self.tokens,
            'editable': self.editable, 'droppable': self.droppable,
            'pair': self.pair, 'group': self.group, 'tool': self.tool,
            'note': self.note, 'media': len(self.media), 'chars': len(self.text),
        }
        d['text'] = self.text if full else self.text[:preview]
        return d


def _result_parts(content):
    """tool_result content -> (text, media sub-block indices, other count)."""
    if isinstance(content, str):
        return content, []
    texts, media = [], []
    for k, sub in enumerate(content or []):
        if isinstance(sub, dict) and sub.get('type') == 'text':
            texts.append(sub.get('text', ''))
        elif isinstance(sub, dict) and sub.get('type') == 'image':
            media.append(k)
    return '\n'.join(texts), media


class Session(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.session_id = os.path.basename(path)[:-len('.jsonl')] if path.endswith('.jsonl') else os.path.basename(path)
        self.entries = []
        self.bad_lines = []
        with open(path, 'r', encoding='utf-8') as f:  # read-only, once
            for n, line in enumerate(f):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    if not isinstance(e, dict):
                        raise ValueError('not an object')
                    self.entries.append(e)
                except ValueError:
                    self.bad_lines.append(n + 1)
                    self.entries.append({'type': '__unparsed__', '__raw__': line.rstrip('\n')})
        self.mtime = os.path.getmtime(path)
        self._index()
        self._steps()

    # -- structure ---------------------------------------------------------
    def _index(self):
        E = self.entries
        self.by_uuid = {}
        for i, e in enumerate(E):
            u = e.get('uuid')
            if isinstance(u, str) and u not in self.by_uuid:
                self.by_uuid[u] = i
        # logical parent = file parent, except preserved segments after a
        # compaction: the segment head hangs off the summary (anchor).
        self.parent = {}
        for u, i in self.by_uuid.items():
            self.parent[u] = E[i].get('parentUuid')
        self.segments = {}  # boundary uuid -> [uuids head..tail]
        for i, e in enumerate(E):
            if e.get('type') == 'system' and e.get('subtype') == 'compact_boundary':
                seg = (e.get('compactMetadata') or {}).get('preservedSegment') or {}
                head, tail, anchor = seg.get('headUuid'), seg.get('tailUuid'), seg.get('anchorUuid')
                if head in self.by_uuid and tail in self.by_uuid and anchor in self.by_uuid:
                    chain, cur, guard = [], tail, 0
                    while cur and cur in self.by_uuid and guard < 100000:
                        chain.append(cur)
                        if cur == head:
                            break
                        cur = E[self.by_uuid[cur]].get('parentUuid')
                        guard += 1
                    if chain and chain[-1] == head:
                        chain.reverse()
                        self.segments[e['uuid']] = chain
                        self.parent[head] = anchor
        # leaf: last main-chain entry in the file
        leaf = None
        for e in reversed(E):
            if e.get('type') in CHAIN_TYPES and e.get('uuid') and not e.get('isSidechain'):
                leaf = e['uuid']
                break
        chain, seen, cur = [], set(), leaf
        while cur and cur in self.by_uuid and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self.parent.get(cur)
        chain.reverse()
        chain = self._recover_parallel(chain)
        self.live = chain
        self.live_set = set(chain)
        self.boundary = None
        if chain:
            root = E[self.by_uuid[chain[0]]]
            if root.get('subtype') == 'compact_boundary':
                self.boundary = root['uuid']
        bidx = self.by_uuid[self.boundary] if self.boundary else -1
        self.group = {}
        for u, i in self.by_uuid.items():
            e = E[i]
            if e.get('isSidechain'):
                g = 'sidechain'
            elif u in self.live_set:
                g = 'live'
            elif i < bidx:
                g = 'precompact'
            else:
                g = 'branch'
            self.group[u] = g
        self.title = None
        for e in E:
            if e.get('type') == 'custom-title' and e.get('customTitle'):
                self.title = e['customTitle']
        if not self.title:
            for e in E:
                if e.get('type') == 'ai-title' and e.get('aiTitle'):
                    self.title = e['aiTitle']
        self.cwd = next((e['cwd'] for e in E if e.get('cwd')), None)
        self.version = next((e['version'] for e in reversed(E) if e.get('version')), None)

    def _recover_parallel(self, chain):
        """Parallel tool calls hang off the parent chain as side leaves.

        One API response is written as one entry per block, all sharing
        message.id. With parallel calls only the last call (and its result)
        is on the leaf's parent chain; the other calls and results are
        siblings. They are in the live context, so splice them back in after
        the last chain entry of their response, as Claude Code does on load.
        """
        E = self.entries
        on_chain = set(chain)
        groups = {}
        for u, i in sorted(self.by_uuid.items(), key=lambda kv: kv[1]):
            e = E[i]
            m = e.get('message')
            if e.get('type') == 'assistant' and not e.get('isSidechain') and isinstance(m, dict) and m.get('id'):
                groups.setdefault(m['id'], []).append(u)
        pos = {u: n for n, u in enumerate(chain)}
        extra_after = {}
        member = {}
        anchor_of = {}
        for mid, uuids in groups.items():
            inside = [u for u in uuids if u in pos]
            if not inside:
                continue
            anchor = max(inside, key=pos.get)
            anchor_of[mid] = anchor
            for u in uuids:
                member[u] = mid
            sibs = [u for u in uuids if u not in pos]
            if sibs:
                extra_after[anchor] = sibs
        results = {}
        for u, i in sorted(self.by_uuid.items(), key=lambda kv: kv[1]):
            e = E[i]
            if u in on_chain or e.get('type') != 'user' or e.get('isSidechain'):
                continue
            c = (e.get('message') or {}).get('content') if isinstance(e.get('message'), dict) else None
            if not (isinstance(c, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in c)):
                continue
            mid = member.get(e.get('parentUuid'))
            if mid is not None:
                results.setdefault(anchor_of[mid], []).append(u)
        if not extra_after and not results:
            return chain
        out = []
        for u in chain:
            out.append(u)
            out.extend(extra_after.get(u, ()))
            out.extend(results.get(u, ()))
        return out

    def _steps(self):
        E = self.entries
        steps = []
        order = [(u, 'live') for u in self.live]
        for g in ('precompact', 'branch', 'sidechain'):
            order += [(u, g) for u, i in sorted(self.by_uuid.items(), key=lambda kv: kv[1]) if self.group[u] == g]
        for u, g in order:
            e = E[self.by_uuid[u]]
            for st in self._entry_steps(e):
                st.group = g
                st.uuid = u
                st.entry = self.by_uuid[u]
                st.ts = e.get('timestamp')
                st.msg = (e.get('message') or {}).get('id') if isinstance(e.get('message'), dict) else None
                if g != 'live':
                    st.editable = st.droppable = False
                steps.append(st)
        for n, st in enumerate(steps):
            st.index = n
        self.steps = steps
        self.step_by_id = {s.id: s for s in steps}
        self.tool_steps = {}
        self.msg_steps = {}
        for s in steps:
            if s.group == 'live' and s.pair:
                self.tool_steps.setdefault(s.pair, []).append(s.id)
            if s.group == 'live' and s.msg and s.kind in ('assistant', 'thinking', 'tool_use'):
                self.msg_steps.setdefault(s.msg, []).append(s)
        self.first_prompt = next((s.text for s in steps if s.kind == 'user' and not s.note and s.text.strip()
                                  and not s.text.lstrip().startswith('<')), '')

    def _mk(self, e, block, kind, text, tokens=None, editable=False, droppable=False, pair=None, tool=None, note=None, media=None):
        s = Step()
        s.id = '%s:%s' % (e.get('uuid'), 0 if block is None else block)
        s.block = block
        s.kind = kind
        s.text = text or ''
        s.media = media or []
        s.tokens = approx_tokens(s.text) + IMAGE_TOKENS * len(s.media) if tokens is None else tokens
        s.editable, s.droppable, s.pair, s.tool, s.note = editable, droppable, pair, tool, note
        return s

    def _entry_steps(self, e):
        t = e.get('type')
        msg = e.get('message')
        if t in ('user', 'assistant') and isinstance(msg, dict):
            content = msg.get('content')
            if e.get('isCompactSummary'):
                if isinstance(content, str):
                    return [self._mk(e, None, 'summary', content, editable=True)]
                return [self._mk(e, None, 'summary', json.dumps(content, ensure_ascii=False))]
            base = 'user' if t == 'user' else 'assistant'
            note = 'meta' if e.get('isMeta') else ('api error' if e.get('isApiErrorMessage') else None)
            if isinstance(content, str):
                return [self._mk(e, None, base, content, editable=True, droppable=True, note=note)]
            if not isinstance(content, list):
                return [self._mk(e, None, 'other', json.dumps(msg, ensure_ascii=False)[:2000])]
            out = []
            for k, b in enumerate(content):
                bt = b.get('type') if isinstance(b, dict) else None
                if bt == 'text':
                    out.append(self._mk(e, k, base, b.get('text', ''), editable=True, droppable=True, note=note))
                elif bt == 'thinking':
                    txt = b.get('thinking') or ''
                    tok = approx_tokens(txt) or approx_tokens(b.get('signature') or '')
                    out.append(self._mk(e, k, 'thinking', txt or '[thinking text not stored; signed block]', tokens=tok, droppable=True))
                elif bt == 'tool_use':
                    body = json.dumps(b.get('input'), ensure_ascii=False, indent=2)
                    out.append(self._mk(e, k, 'tool_use', body, droppable=True, pair=b.get('id'), tool=b.get('name')))
                elif bt == 'tool_result':
                    text, media = _result_parts(b.get('content'))
                    out.append(self._mk(e, k, 'tool_result', text, editable=True, droppable=True,
                                        pair=b.get('tool_use_id'), media=media,
                                        note='error' if b.get('is_error') else None))
                elif bt == 'image':
                    out.append(self._mk(e, k, base, '[image]', tokens=IMAGE_TOKENS, droppable=True, note='image'))
                else:
                    out.append(self._mk(e, k, 'other', json.dumps(b, ensure_ascii=False)[:2000], note=str(bt)))
            return out
        if t == 'attachment':
            att = e.get('attachment') or {}
            rendered = e.get('rendered')
            text = ''
            if isinstance(rendered, list):
                text = '\n'.join(r.get('content', '') for r in rendered if isinstance(r, dict) and isinstance(r.get('content'), str))
            shown = text or json.dumps(att, ensure_ascii=False)[:2000]
            return [self._mk(e, None, 'other', shown, tokens=approx_tokens(text), note='attachment: %s' % att.get('type'))]
        if t == 'system':
            c = e.get('content')
            text = c if isinstance(c, str) else ''
            in_ctx = e.get('subtype') in ('local_command',)
            return [self._mk(e, None, 'other', text or '[%s]' % e.get('subtype'),
                             tokens=approx_tokens(text) if in_ctx else 0, note='system: %s' % e.get('subtype'))]
        return [self._mk(e, None, 'other', json.dumps({k: v for k, v in e.items() if k != 'uuid'}, ensure_ascii=False)[:2000],
                         tokens=0, note=str(t))]

    def other_counts(self):
        counts = {}
        for e in self.entries:
            if not e.get('uuid'):
                counts[e.get('type')] = counts.get(e.get('type'), 0) + 1
        return counts


# ------------------------------------------------------------------ editing

class Editor(object):
    """Edit state for one session, with undo/redo. Nothing touches disk."""

    def __init__(self, session, marker=True, lock_thinking=True):
        self.s = session
        self.marker = marker
        self.lock_thinking = lock_thinking
        self.state = {}  # step id -> {'dropped': bool, 'removed': set, 'media': set, 'text': str|None}
        self.undo_stack = []
        self.redo_stack = []
        self._units = {}

    # -- reads -------------------------------------------------------------
    def _st(self, sid):
        return self.state.get(sid) or {}

    def base_text(self, sid):
        t = self._st(sid).get('text')
        return self.s.step_by_id[sid].text if t is None else t

    def units(self, sid):
        step = self.s.step_by_id[sid]
        base = self.base_text(sid)
        key = (sid, base)
        if self._units.get('k') != key:
            fn = split_lines if step.kind == 'tool_result' else split_sentences
            self._units = {'k': key, 'v': fn(base)}
        return self._units['v']

    def effective_text(self, sid):
        st = self._st(sid)
        removed = st.get('removed')
        if not removed:
            return self.base_text(sid)
        units = self.units(sid)
        if self.s.step_by_id[sid].kind == 'tool_result':
            return join_lines(units, removed, self.marker)
        return join_sentences(units, removed)

    def is_dropped(self, sid):
        if self._gone(sid):
            return True
        step = self.s.step_by_id[sid]
        if self.lock_thinking and step.kind == 'thinking' and step.msg and step.group == 'live':
            # thinking is locked to its turn: when every other block of the
            # same API response is dropped, the thinking block goes with it
            rest = [x for x in self.s.msg_steps.get(step.msg, ()) if x.kind != 'thinking']
            if rest and all(self._gone(x.id) for x in rest):
                return True
        return False

    def _gone(self, sid):
        st = self._st(sid)
        if st.get('dropped'):
            return True
        step = self.s.step_by_id[sid]
        # a text step with every sentence struck out is as good as dropped
        if st.get('removed') and step.kind != 'tool_result' and not self.effective_text(sid).strip():
            return True
        return False

    def is_edited(self, sid):
        st = self._st(sid)
        return (not self.is_dropped(sid)) and bool(st.get('text') is not None or st.get('removed') or st.get('media'))

    def tokens(self, sid):
        step = self.s.step_by_id[sid]
        if self.is_dropped(sid):
            return 0
        st = self._st(sid)
        if st.get('text') is None and not st.get('removed') and not st.get('media'):
            return step.tokens
        return approx_tokens(self.effective_text(sid)) + IMAGE_TOKENS * (len(step.media) - len(st.get('media') or ()))

    def view(self, sid):
        st = self._st(sid)
        return {
            'id': sid, 'dropped': self.is_dropped(sid), 'edited': self.is_edited(sid),
            'auto': self.is_dropped(sid) and not self._gone(sid),
            'rewritten': st.get('text') is not None,
            'removed': sorted(st.get('removed') or ()), 'media_removed': sorted(st.get('media') or ()),
            'tokens': self.tokens(sid),
            'preview': self.effective_text(sid)[:400] if st.get('text') is not None or st.get('removed') else None,
        }

    def detail(self, sid):
        step = self.s.step_by_id[sid]
        d = self.view(sid)
        d['units'] = [[b, s, k] for b, s, k in self.units(sid)] if step.editable else []
        d['base'] = self.base_text(sid)
        d['original'] = step.text
        d['effective'] = self.effective_text(sid)
        d['media'] = step.media
        return d

    def counters(self):
        before = after = dropped = edited = 0
        for s in self.s.steps:
            if s.group != 'live':
                continue
            before += s.tokens
            after += self.tokens(s.id)
            if self.is_dropped(s.id):
                dropped += 1
            elif self.is_edited(s.id):
                edited += 1
        return {'before': before, 'after': after, 'dropped': dropped, 'edited': edited,
                'steps': sum(1 for s in self.s.steps if s.group == 'live'),
                'undo': len(self.undo_stack), 'redo': len(self.redo_stack)}

    # -- writes ------------------------------------------------------------
    def _linked(self, ids):
        """Expand a set of step ids with the steps that must share their fate."""
        out = set()
        for sid in ids:
            step = self.s.step_by_id.get(sid)
            if not step or not step.droppable:
                continue
            out.add(sid)
            if step.pair:
                out.update(self.s.tool_steps.get(step.pair, ()))
        return out

    def _set(self, sid, **kw):
        st = dict(self.state.get(sid) or {})
        st.update(kw)
        if not st.get('dropped') and st.get('text') is None and not st.get('removed') and not st.get('media'):
            self.state.pop(sid, None)
        else:
            self.state[sid] = st

    def apply(self, op):
        """Apply one user action. Returns the ids of steps whose view changed.
        {'op': 'batch', 'ops': [...]} applies several as one undo step."""
        before = copy.deepcopy(self.state)
        try:
            for sub in (op.get('ops') or []) if op.get('op') == 'batch' else [op]:
                self._apply_one(sub)
        except Exception:
            self.state = before
            raise
        return self._commit(before)

    def _apply_one(self, op):
        kind = op.get('op')
        if kind == 'drop':  # {'ids': [...], 'value': bool}
            for sid in self._linked(op.get('ids') or []):
                if op.get('value', True):
                    self._set(sid, dropped=True)
                else:
                    st = self._st(sid)
                    kw = {'dropped': False}
                    step = self.s.step_by_id[sid]
                    if st.get('removed') and step.kind != 'tool_result' and not self.effective_text(sid).strip():
                        kw['removed'] = set()
                    self._set(sid, **kw)
        elif kind == 'unit':  # toggle one sentence / line
            sid, k = op['id'], int(op['unit'])
            step = self._editable(sid)
            if not 0 <= k < len(self.units(sid)):
                raise ValueError('no such unit')
            removed = set(self._st(sid).get('removed') or ())
            removed.symmetric_difference_update({k})
            self._set(sid, removed=removed)
        elif kind == 'media':  # toggle an image inside a tool result
            sid, k = op['id'], int(op['unit'])
            step = self._editable(sid)
            if k not in step.media:
                raise ValueError('no such image')
            media = set(self._st(sid).get('media') or ())
            media.symmetric_difference_update({k})
            self._set(sid, media=media)
        elif kind == 'keep_lines':  # tool results: keep first N and/or last M
            sid = op['id']
            self._editable(sid)
            n = len(self.units(sid))
            head, tail = max(0, int(op.get('head') or 0)), max(0, int(op.get('tail') or 0))
            removed = set(k for k in range(n) if k >= head and k < n - tail)
            self._set(sid, removed=removed)
        elif kind == 'set_removed':  # exactly these sentences / lines are struck
            sid = op['id']
            self._editable(sid)
            n = len(self.units(sid))
            removed = set(int(k) for k in op.get('removed') or [])
            if any(not 0 <= k < n for k in removed):
                raise ValueError('no such unit')
            self._set(sid, removed=removed)
        elif kind == 'edit':
            sid = op['id']
            step = self._editable(sid)
            text = op.get('text')
            if not isinstance(text, str):
                raise ValueError('text required')
            if text == step.text:
                self._set(sid, text=None, removed=set())
            else:
                self._set(sid, text=text, removed=set())
        elif kind == 'reset':
            sid = op['id']
            self._editable(sid)
            self._set(sid, text=None, removed=set(), media=set())
        elif kind == 'bulk_results':  # drop every tool result above a size
            limit = int(op.get('min_tokens') or 0)
            ids = [s.id for s in self.s.steps if s.group == 'live' and s.kind == 'tool_result'
                   and s.droppable and self.tokens(s.id) > limit]
            for sid in self._linked(ids):
                self._set(sid, dropped=True)
        else:
            raise ValueError('unknown op %r' % kind)

    def _editable(self, sid):
        step = self.s.step_by_id.get(sid)
        if not step or not step.editable:
            raise ValueError('step cannot be edited')
        return step

    def _commit(self, before):
        if before == self.state:
            return []
        self.undo_stack.append(before)
        self.redo_stack = []
        return self._diff(before)

    def _diff(self, before):
        ids = set(before) | set(self.state)
        changed = set(i for i in ids if before.get(i) != self.state.get(i))
        for i in list(changed):
            step = self.s.step_by_id.get(i)
            if step is not None and step.msg:
                changed.update(x.id for x in self.s.msg_steps.get(step.msg, ()) if x.kind == 'thinking')
        return sorted(changed)

    def undo(self):
        if not self.undo_stack:
            return []
        cur = self.state
        self.redo_stack.append(cur)
        self.state = self.undo_stack.pop()
        return self._diff(cur)

    def redo(self):
        if not self.redo_stack:
            return []
        cur = self.state
        self.undo_stack.append(cur)
        self.state = self.redo_stack.pop()
        return self._diff(cur)

    # -- output ------------------------------------------------------------
    def build(self, new_id=None, title_suffix=' (edited)'):
        """Return (entries, report) for the fork. Pure: writes nothing."""
        s = self.s
        new_id = new_id or str(uuidlib.uuid4())
        by_entry = {}
        for st in s.steps:
            if st.group == 'live' and (st.id in self.state or (st.kind == 'thinking' and self.is_dropped(st.id))):
                by_entry.setdefault(st.entry, []).append(st)

        removed = set()   # uuids of entries that disappear
        patched = {}      # entry index -> new entry dict
        dropped_steps, edited_steps = [], []
        for idx, steps in by_entry.items():
            e = s.entries[idx]
            msg = e['message']
            content = msg.get('content')
            new = None
            if isinstance(content, str):
                st = steps[0]
                if self.is_dropped(st.id):
                    removed.add(e['uuid'])
                    dropped_steps.append(st)
                    continue
                if self.is_edited(st.id):
                    new = copy.deepcopy(e)
                    new['message']['content'] = self.effective_text(st.id)
                    edited_steps.append(st)
            else:
                blocks = list(content)
                kill = set()
                changed = False
                for st in steps:
                    if self.is_dropped(st.id):
                        kill.add(st.block)
                        dropped_steps.append(st)
                    elif self.is_edited(st.id):
                        blocks[st.block] = self._patch_block(st, content[st.block])
                        edited_steps.append(st)
                        changed = True
                if kill:
                    blocks = [b for k, b in enumerate(blocks) if k not in kill]
                    changed = True
                if not blocks:
                    removed.add(e['uuid'])
                    continue
                if changed:
                    new = copy.deepcopy({k: v for k, v in e.items() if k != 'message'})
                    new['message'] = dict(msg)
                    new['message']['content'] = blocks
            if new is not None:
                patched[idx] = new

        if self.lock_thinking:
            removed |= self._orphan_thinking(removed, patched)

        def new_parent(u):
            p, guard = s.parent.get(u), 0
            while p is not None and p in removed and guard < 1000000:
                p = s.parent.get(p)
                guard += 1
            return p

        # compaction bookkeeping when preserved messages were removed
        seg_fix = {}
        for b_uuid, seg in s.segments.items():
            if not any(u in removed for u in seg):
                continue
            kept = [u for u in seg if u not in removed]
            be = copy.deepcopy(s.entries[s.by_uuid[b_uuid]])
            meta = be.get('compactMetadata') or {}
            if kept:
                meta['preservedSegment']['headUuid'] = kept[0]
                meta['preservedSegment']['tailUuid'] = kept[-1]
            else:
                meta.pop('preservedSegment', None)
            pm = meta.get('preservedMessages')
            if isinstance(pm, dict):
                if kept:
                    for key in ('uuids', 'allUuids'):
                        if isinstance(pm.get(key), list):
                            pm[key] = [u for u in pm[key] if u not in removed]
                else:
                    meta.pop('preservedMessages', None)
            seg_fix[s.by_uuid[b_uuid]] = be
        seg_heads = {seg[0] for seg in s.segments.values()}

        prompt_ids = {}
        head = {}
        body = []
        for idx, e in enumerate(s.entries):
            t = e.get('type')
            if t == '__unparsed__':
                body.append(e)
                continue
            if t in FORK_SKIP:
                continue
            if t in FORK_HEAD and not e.get('uuid'):
                head[t] = e
                continue
            u = e.get('uuid')
            if u in removed:
                continue
            out = patched.get(idx) or seg_fix.get(idx)
            out = dict(out if out is not None else e)
            if u is not None and 'parentUuid' in out:
                if u in seg_heads and u not in removed and out.get('parentUuid') not in removed:
                    pass  # untouched segment head keeps its file parent
                else:
                    out['parentUuid'] = new_parent(u)
            if 'sessionId' in out:
                out['sessionId'] = new_id
            if out.get('type') == 'assistant' and u and 'requestSessionId' not in out:
                out['requestSessionId'] = s.session_id
            pid = out.get('promptId')
            if isinstance(pid, str):
                out['promptId'] = prompt_ids.setdefault(pid, _derive_id(new_id, pid))
            body.append(out)

        base_title = s.title or (s.first_prompt.strip().split('\n')[0][:60] if s.first_prompt else 'session')
        new_title = base_title + title_suffix
        head['custom-title'] = {'type': 'custom-title', 'customTitle': new_title, 'sessionId': new_id}
        head['agent-name'] = {'type': 'agent-name', 'agentName': new_title, 'sessionId': new_id}
        prev = head.get('derived-from')
        sessions = list(prev.get('sessions') or []) if prev else []
        sessions.append({'type': 'claude_code_session', 'id': s.session_id, 'via': 'fork'})
        head['derived-from'] = {'type': 'derived-from', 'v': 1, 'sessionId': new_id, 'sessions': sessions}
        head_entries = []
        for t in FORK_HEAD:
            if t in head:
                h = dict(head[t])
                if 'sessionId' in h:
                    h['sessionId'] = new_id
                head_entries.append(h)

        entries = head_entries + body
        report = {
            'new_id': new_id, 'source_id': s.session_id, 'title': new_title,
            'dropped': [self._record(st) for st in sorted(dropped_steps, key=lambda x: x.index)],
            'edited': [self._record(st) for st in sorted(edited_steps, key=lambda x: x.index)],
            'entries_removed': len(removed),
        }
        return entries, report

    def _record(self, st):
        return {'step': st.index, 'id': st.id, 'kind': st.kind, 'uuid': st.uuid, 'block': st.block,
                'tool': st.tool, 'tokens_before': st.tokens, 'tokens_after': self.tokens(st.id)}

    def _orphan_thinking(self, removed, patched):
        """Thinking entries whose whole API message is otherwise gone go too.

        Claude Code writes one entry per content block; blocks of one API
        response share message.id. A signed thinking block left alone as an
        assistant turn is rejected by the API.
        """
        s = self.s
        groups = {}
        for u in s.live:
            e = s.entries[s.by_uuid[u]]
            m = e.get('message')
            if e.get('type') == 'assistant' and isinstance(m, dict) and m.get('id'):
                groups.setdefault(m['id'], []).append(u)
        extra = set()
        for mid, uuids in groups.items():
            kept = [u for u in uuids if u not in removed]
            if not kept or len(kept) == len(uuids) and not any(s.by_uuid[u] in patched for u in uuids):
                continue
            def only_thinking(u):
                e = patched.get(s.by_uuid[u]) or s.entries[s.by_uuid[u]]
                c = e['message'].get('content')
                return isinstance(c, list) and c and all(isinstance(b, dict) and b.get('type') in ('thinking', 'redacted_thinking') for b in c)
            if all(only_thinking(u) for u in kept):
                extra.update(kept)
        return extra

    def _patch_block(self, st, block):
        b = dict(block)
        text = self.effective_text(st.id)
        if st.kind != 'tool_result':
            b['text'] = text
            return b
        content = block.get('content')
        gone = self._st(st.id).get('media') or set()
        if isinstance(content, str):
            b['content'] = text
            return b
        if gone and self.marker:
            text = (text + '\n' if text else '') + TRIM_MARKER
        new, placed = [], False
        for k, sub in enumerate(content or []):
            if isinstance(sub, dict) and sub.get('type') == 'text':
                if not placed:
                    placed = True
                    if text:
                        new.append(dict(sub, text=text))
                continue
            if k in gone:
                continue
            new.append(sub)
        if not placed and text:
            new.insert(0, {'type': 'text', 'text': text})
        b['content'] = new
        return b


# ------------------------------------------------------------- save + check

def dump_lines(entries):
    lines = []
    for e in entries:
        if e.get('type') == '__unparsed__':
            lines.append(e['__raw__'])
        else:
            lines.append(json.dumps(e, ensure_ascii=False, separators=(',', ':')))
    return lines


def _block_empty(b):
    if not isinstance(b, dict):
        return False
    t = b.get('type')
    if t == 'text':
        return not (b.get('text') or '').strip()
    if t == 'tool_result':
        c = b.get('content')
        return c is None or c == [] or (isinstance(c, str) and not c.strip())
    return False


def check_lines(lines):
    """The save checks, run on the exact lines that would be written.

    Returns a list of issues: {'code', 'msg', 'uuid', 'key'}.
    """
    issues = []
    entries = []
    for n, line in enumerate(lines):
        try:
            e = json.loads(line)
            if not isinstance(e, dict):
                raise ValueError
            entries.append(e)
        except ValueError:
            issues.append({'code': 'json', 'msg': 'line %d is not valid JSON' % (n + 1), 'uuid': None, 'key': 'json:%s' % line[:80]})
    seen = {}
    for e in entries:
        u = e.get('uuid')
        if u is None:
            continue
        if u in seen:
            issues.append({'code': 'duplicate-uuid', 'msg': 'uuid %s appears more than once' % u, 'uuid': u, 'key': 'dup:%s' % u})
        seen[u] = e
    for e in entries:
        u, p = e.get('uuid'), e.get('parentUuid')
        if u is not None and p is not None and p not in seen:
            issues.append({'code': 'parent', 'msg': 'parentUuid of %s points to a missing entry' % u, 'uuid': u, 'key': 'parent:%s' % u})
    uses, results = {}, {}
    for e in entries:
        m = e.get('message')
        if e.get('type') not in ('user', 'assistant') or not isinstance(m, dict) or not e.get('uuid'):
            continue
        c = m.get('content')
        empty = False
        if isinstance(c, str):
            empty = not c.strip()
        elif isinstance(c, list):
            empty = not c or any(_block_empty(b) for b in c)
            for b in c:
                if isinstance(b, dict) and b.get('type') == 'tool_use':
                    uses.setdefault(b.get('id'), e['uuid'])
                elif isinstance(b, dict) and b.get('type') == 'tool_result':
                    results.setdefault(b.get('tool_use_id'), e['uuid'])
        else:
            empty = True
        if empty:
            issues.append({'code': 'empty', 'msg': 'message %s has empty content' % e['uuid'], 'uuid': e['uuid'], 'key': 'empty:%s' % e['uuid']})
    for tid, u in uses.items():
        if tid not in results:
            issues.append({'code': 'tool-pair', 'msg': 'tool call %s has no result' % tid, 'uuid': u, 'key': 'use:%s' % tid})
    for tid, u in results.items():
        if tid not in uses:
            issues.append({'code': 'tool-pair', 'msg': 'tool result %s has no tool call' % tid, 'uuid': u, 'key': 'result:%s' % tid})
    return issues


def check(editor, entries=None):
    """Blocking issues are those the edit introduced; issues the untouched
    session already had are reported as warnings (Claude Code already lives
    with them, and the user could not fix them here)."""
    if entries is None:
        entries, _ = editor.build()
    found = check_lines(dump_lines(entries))
    pristine = Editor(editor.s, marker=editor.marker, lock_thinking=editor.lock_thinking)
    base_entries, _ = pristine.build()
    known = {i['key'] for i in check_lines(dump_lines(base_entries))}
    errors = [i for i in found if i['key'] not in known]
    warnings = [i for i in found if i['key'] in known]
    for i in errors + warnings:
        idx = editor.s.by_uuid.get(i['uuid'])
        i['steps'] = [st.id for st in editor.s.steps if st.entry == idx] if idx is not None else []
    return errors, warnings


def save(editor, edits_dir=None, new_id=None, dest_dir=None):
    """Write the fork next to the original. The original is never opened for writing."""
    s = editor.s
    entries, report = editor.build(new_id=new_id)
    errors, warnings = check(editor, entries)
    if errors:
        return {'ok': False, 'errors': errors, 'warnings': warnings}
    dest_dir = dest_dir or os.path.dirname(s.path)
    dest = os.path.join(dest_dir, report['new_id'] + '.jsonl')
    if os.path.exists(dest) or os.path.abspath(dest) == s.path:
        raise IOError('refusing to overwrite %s' % dest)
    lines = dump_lines(entries)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    record_path = None
    if edits_dir:
        os.makedirs(edits_dir, exist_ok=True)
        record_path = os.path.join(edits_dir, report['new_id'] + '.json')
        c = editor.counters()
        with open(record_path, 'w', encoding='utf-8') as f:
            json.dump({
                'new_session_id': report['new_id'], 'source_session_id': s.session_id,
                'source_path': s.path, 'saved_path': dest, 'title': report['title'],
                'approx_tokens_before': c['before'], 'approx_tokens_after': c['after'],
                'trim_marker': editor.marker,
                'dropped': report['dropped'], 'edited': report['edited'],
            }, f, indent=2)
    return {'ok': True, 'new_id': report['new_id'], 'path': dest, 'record': record_path,
            'warnings': warnings, 'title': report['title'], 'lines': len(lines),
            'command': 'claude --resume %s' % report['new_id']}
