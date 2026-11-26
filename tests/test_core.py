"""Unit tests for ctxcore, on small made-up session files.

Run: python3 -m unittest discover -s tests
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ctxcore  # noqa: E402
from ctxcore import Editor, Session  # noqa: E402

SID = '11111111-1111-4111-8111-111111111111'


class Builder(object):
    """Writes a made-up session in the layout Claude Code 2.1.330 uses."""

    def __init__(self):
        self.lines = []
        self.last = None
        self.n = 0

    def _uuid(self, name=None):
        self.n += 1
        return name or 'u%04d' % self.n

    def raw(self, **e):
        e.setdefault('sessionId', SID)
        self.lines.append(e)
        return e

    def chain(self, type_, name=None, parent='__last__', **extra):
        u = self._uuid(name)
        e = {'parentUuid': self.last if parent == '__last__' else parent, 'isSidechain': False,
             'type': type_, 'uuid': u, 'timestamp': '2026-01-01T00:00:%02dZ' % (self.n % 60), 'sessionId': SID,
             'cwd': '/proj', 'version': '2.1.330'}
        e.update(extra)
        self.lines.append(e)
        self.last = u
        return u

    def user(self, text, name=None, **extra):
        return self.chain('user', name, message={'role': 'user', 'content': text}, promptId='p1', **extra)

    def asst(self, blocks, name=None, mid='m1', parent='__last__', **extra):
        return self.chain('assistant', name, parent=parent, message={
            'id': mid, 'role': 'assistant', 'type': 'message', 'content': blocks,
            'usage': {'input_tokens': 10, 'output_tokens': 5}}, **extra)

    def result(self, tool_id, content, name=None, parent='__last__', **extra):
        return self.chain('user', name, parent=parent, message={'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': tool_id, 'content': content}]},
            toolUseResult={'stdout': content if isinstance(content, str) else ''}, **extra)

    def write(self, folder, sid=SID):
        path = os.path.join(folder, sid + '.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for e in self.lines:
                f.write(json.dumps(e) + '\n')
        return path


def text(t):
    return {'type': 'text', 'text': t}


def tool(tid, name='Read', **inp):
    return {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}


def thinking():
    return {'type': 'thinking', 'thinking': '', 'signature': 'c2lnbmVk' * 8}


def plain_chat(b):
    b.raw(type='ai-title', aiTitle='Chat about colours')
    b.raw(type='mode', mode='normal')
    b.user('My favourite colour is teal. Remember that.', 'q1')
    b.asst([text('Noted. Teal it is.')], 'a1', mid='m1')
    b.user('What is two plus two?', 'q2')
    b.asst([text('Four.')], 'a2', mid='m2')
    b.user('Thanks.', 'q3')
    b.asst([text('Any time. Ask again whenever you like.')], 'a3', mid='m3')
    b.raw(type='last-prompt', lastPrompt='Thanks.', leafUuid='a3')
    b.raw(type='api-request-blob', hash='abc', message={'role': 'user', 'content': 'My favourite colour is teal.'})
    return b


def multi_tool_one_entry(b):
    """Older layout: one assistant entry holds several tool calls."""
    b.user('Read both files.', 'q1')
    b.asst([text('Reading.'), tool('t1', file='a.txt'), tool('t2', file='b.txt')], 'a1', mid='m1')
    b.chain('user', 'r1', message={'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 't1', 'content': 'alpha contents'},
        {'type': 'tool_result', 'tool_use_id': 't2', 'content': 'beta contents'}]})
    b.asst([text('Both read.')], 'a2', mid='m2')
    return b


def multi_tool_parallel(b):
    """Current layout: one entry per block; the first result is a side leaf."""
    b.user('Read both files.', 'q1')
    b.asst([thinking()], 'th', mid='m1')
    b.asst([tool('t1', file='a.txt')], 'c1', mid='m1')
    b.asst([tool('t2', file='b.txt')], 'c2', mid='m1')
    b.result('t1', 'alpha contents', 'r1', parent='c1')
    b.result('t2', 'line 1\nline 2\nline 3\nline 4\nline 5\nline 6', 'r2', parent='c2')
    b.asst([text('Both read. The first says alpha. The second has six lines.')], 'a2', mid='m2')
    return b


def compacted(b):
    b.user('Old question about otters.', 'old1')
    b.asst([text('Old answer about otters.')], 'old2', mid='m1')
    b.user('Keep this one.', 'keep1')
    b.asst([text('Kept answer.')], 'keep2', mid='m2')
    b.chain('system', 'bound', parent=None, subtype='compact_boundary', content='Conversation compacted',
            logicalParentUuid='keep2', compactMetadata={
                'trigger': 'manual',
                'preservedSegment': {'headUuid': 'keep1', 'anchorUuid': 'sum', 'tailUuid': 'keep2'},
                'preservedMessages': {'anchorUuid': 'sum', 'uuids': ['keep1', 'keep2'], 'allUuids': ['keep1', 'keep2']}})
    b.user('This session is being continued. Summary: the user asked about otters.', 'sum', isCompactSummary=True)
    b.user('New question after compaction.', 'new1', parent='keep2')
    b.asst([text('New answer.')], 'new2', mid='m3')
    return b


def unknown_types(b):
    b.raw(type='future-thing', payload={'a': [1, 2, 3]}, sessionId=SID)
    b.user('Hello.', 'q1')
    b.chain('attachment', 'att', attachment={'type': 'date', 'date': '2026-01-01'},
            rendered=[{'content': '<system-reminder>Today is 2026-01-01.</system-reminder>'}], renderedRole='system')
    b.chain('hologram', 'holo', beam={'x': 1})
    b.asst([text('Hi.'), {'type': 'quantum_block', 'q': 7}], 'a1', mid='m1')
    return b


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def open(self, make, **kw):
        path = make(Builder()).write(self.tmp.name)
        with open(path, 'rb') as f:
            self.digest = hashlib.sha256(f.read()).hexdigest()
        self.path = path
        s = Session(path)
        return s, Editor(s, **kw)

    def sid(self, s, uuid, block=0):
        return '%s:%d' % (uuid, block)

    def built(self, ed):
        entries, report = ed.build(new_id='NEW')
        return entries, {e['uuid']: e for e in entries if 'uuid' in e}, report


class PlainChat(Base):
    def test_steps_in_order(self):
        s, ed = self.open(plain_chat)
        live = [x for x in s.steps if x.group == 'live']
        self.assertEqual([x.kind for x in live], ['user', 'assistant'] * 3)
        self.assertEqual(live[0].text, 'My favourite colour is teal. Remember that.')
        self.assertEqual(s.title, 'Chat about colours')

    def test_drop_relinks_to_nearest_kept_ancestor(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'drop', 'ids': ['q2:0', 'a2:0']})
        entries, by, _ = self.built(ed)
        self.assertNotIn('q2', by)
        self.assertNotIn('a2', by)
        self.assertEqual(by['q3']['parentUuid'], 'a1')
        self.assertEqual(ctxcore.check(ed), ([], []))

    def test_first_kept_entry_has_null_parent(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'drop', 'ids': ['q1:0', 'a1:0']})
        _, by, _ = self.built(ed)
        self.assertIsNone(by['q2']['parentUuid'])

    def test_session_id_replaced_everywhere_and_log_entries_left_out(self):
        s, ed = self.open(plain_chat)
        entries, by, _ = self.built(ed)
        self.assertTrue(all(e.get('sessionId') in (None, 'NEW') for e in entries))
        types = [e['type'] for e in entries]
        self.assertNotIn('api-request-blob', types)
        self.assertNotIn('last-prompt', types)
        self.assertEqual(by['a1']['requestSessionId'], SID)

    def test_fork_head_carries_edited_title(self):
        s, ed = self.open(plain_chat)
        entries, _, report = self.built(ed)
        head = {e['type']: e for e in entries[:6]}
        self.assertEqual(head['custom-title']['customTitle'], 'Chat about colours (edited)')
        self.assertEqual(head['agent-name']['agentName'], 'Chat about colours (edited)')
        self.assertEqual(head['ai-title']['aiTitle'], 'Chat about colours')
        self.assertEqual(head['derived-from']['sessions'][-1], {'type': 'claude_code_session', 'id': SID, 'via': 'fork'})
        self.assertEqual(report['title'], 'Chat about colours (edited)')

    def test_save_writes_new_file_and_never_touches_original(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'drop', 'ids': ['a2:0']})
        edits = os.path.join(self.tmp.name, 'edits')
        out = ctxcore.save(ed, edits_dir=edits)
        self.assertTrue(out['ok'])
        self.assertEqual(os.path.dirname(out['path']), os.path.dirname(self.path))
        self.assertNotEqual(out['path'], self.path)
        with open(self.path, 'rb') as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), self.digest)
        saved = Session(out['path'])
        self.assertEqual([x.text for x in saved.steps if x.group == 'live'],
                         [x.text for x in s.steps if x.id != 'a2:0'])
        with open(out['record']) as f:
            rec = json.load(f)
        self.assertEqual(rec['source_session_id'], SID)
        self.assertEqual([d['id'] for d in rec['dropped']], ['a2:0'])
        self.assertEqual(out['command'], 'claude --resume ' + out['new_id'])
        self.assertEqual(out['slash_command'], '/resume ' + out['new_id'])

    def test_header_counter_matches_saved_file(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'drop', 'ids': ['a2:0']})
        ed.apply({'op': 'edit', 'id': 'a3:0', 'text': 'Sure.'})
        out = ctxcore.save(ed)
        saved = Session(out['path'])
        self.assertEqual(ed.counters()['after'], sum(x.tokens for x in saved.steps if x.group == 'live'))
        self.assertEqual(ed.counters()['dropped'], 1)
        self.assertEqual(ed.counters()['edited'], 1)


class ToolCalls(Base):
    def test_drop_one_call_in_shared_entry_is_block_level(self):
        s, ed = self.open(multi_tool_one_entry)
        ed.apply({'op': 'drop', 'ids': ['a1:1']})
        self.assertTrue(ed.is_dropped('r1:0'))       # its result goes too
        self.assertFalse(ed.is_dropped('a1:2'))      # the other call stays
        _, by, _ = self.built(ed)
        self.assertEqual([b.get('id', b['type']) for b in by['a1']['message']['content']], ['text', 't2'])
        self.assertEqual([b['tool_use_id'] for b in by['r1']['message']['content']], ['t2'])
        self.assertEqual(ctxcore.check(ed), ([], []))

    def test_dropping_result_drops_call(self):
        s, ed = self.open(multi_tool_one_entry)
        ed.apply({'op': 'drop', 'ids': ['r1:1']})
        self.assertTrue(ed.is_dropped('a1:2'))

    def test_entry_with_no_blocks_left_is_removed(self):
        s, ed = self.open(multi_tool_one_entry)
        ed.apply({'op': 'drop', 'ids': ['a1:1', 'a1:2']})
        _, by, _ = self.built(ed)
        self.assertNotIn('r1', by)
        self.assertEqual(by['a2']['parentUuid'], 'a1')
        self.assertEqual(len(by['a1']['message']['content']), 1)

    def test_parallel_side_leaf_is_live_and_ordered(self):
        s, ed = self.open(multi_tool_parallel)
        live = [(x.kind, x.pair) for x in s.steps if x.group == 'live']
        self.assertEqual(live, [('user', None), ('thinking', None), ('tool_use', 't1'), ('tool_use', 't2'),
                                ('tool_result', 't1'), ('tool_result', 't2'), ('assistant', None)])
        self.assertFalse([x for x in s.steps if x.group != 'live'])

    def test_drop_parallel_call_relinks_sibling(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'drop', 'ids': ['c1:0']})
        _, by, _ = self.built(ed)
        self.assertNotIn('c1', by)
        self.assertNotIn('r1', by)
        self.assertEqual(by['c2']['parentUuid'], 'th')
        self.assertEqual(ctxcore.check(ed), ([], []))

    def test_restore_brings_back_pair(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'drop', 'ids': ['r2:0']})
        ed.apply({'op': 'drop', 'ids': ['c2:0'], 'value': False})
        self.assertEqual(ed.state, {})

    def test_bulk_drop_large_results(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'bulk_results', 'min_tokens': 5})
        self.assertTrue(ed.is_dropped('r2:0') and ed.is_dropped('c2:0'))
        self.assertFalse(ed.is_dropped('r1:0'))


class ThinkingBlocks(Base):
    def test_thinking_is_not_editable(self):
        s, ed = self.open(multi_tool_parallel)
        self.assertFalse(s.step_by_id['th:0'].editable)
        with self.assertRaises(ValueError):
            ed.apply({'op': 'edit', 'id': 'th:0', 'text': 'x'})

    def test_thinking_can_be_dropped_whole(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'drop', 'ids': ['th:0']})
        _, by, _ = self.built(ed)
        self.assertNotIn('th', by)
        self.assertEqual(by['c1']['parentUuid'], 'q1')
        self.assertEqual(ctxcore.check(ed), ([], []))

    def test_thinking_is_locked_to_its_turn(self):
        s, ed = self.open(multi_tool_parallel)
        changed = ed.apply({'op': 'drop', 'ids': ['c1:0']})
        self.assertFalse(ed.is_dropped('th:0'))
        changed = ed.apply({'op': 'drop', 'ids': ['c2:0']})
        self.assertIn('th:0', changed)
        self.assertTrue(ed.is_dropped('th:0'))
        _, by, _ = self.built(ed)
        self.assertNotIn('th', by)
        self.assertEqual(by['a2']['parentUuid'], 'q1')
        ed.undo()
        self.assertFalse(ed.is_dropped('th:0'))

    def test_signature_survives_untouched(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'drop', 'ids': ['c1:0']})
        _, by, _ = self.built(ed)
        self.assertEqual(by['th']['message']['content'], [thinking()])


class Compaction(Base):
    def test_groups_and_token_count(self):
        s, ed = self.open(compacted)
        groups = {x.uuid: x.group for x in s.steps}
        self.assertEqual(groups['old1'], 'precompact')
        self.assertEqual(groups['old2'], 'precompact')
        self.assertEqual([x.uuid for x in s.steps if x.group == 'live'], ['bound', 'sum', 'keep1', 'keep2', 'new1', 'new2'])
        self.assertEqual(s.step_by_id['sum:0'].kind, 'summary')
        live_tokens = sum(x.tokens for x in s.steps if x.group == 'live')
        self.assertEqual(ed.counters()['before'], live_tokens)

    def test_precompact_entries_are_locked_and_copied_unchanged(self):
        s, ed = self.open(compacted)
        self.assertEqual(ed.apply({'op': 'drop', 'ids': ['old1:0']}), [])
        with self.assertRaises(ValueError):
            ed.apply({'op': 'edit', 'id': 'old2:0', 'text': 'x'})
        _, by, _ = self.built(ed)
        self.assertEqual(by['old2']['message']['content'], [text('Old answer about otters.')])
        self.assertEqual(by['old2']['parentUuid'], 'old1')

    def test_summary_can_be_rewritten_but_not_dropped(self):
        s, ed = self.open(compacted)
        self.assertEqual(ed.apply({'op': 'drop', 'ids': ['sum:0']}), [])
        ed.apply({'op': 'edit', 'id': 'sum:0', 'text': 'Summary: otters.'})
        _, by, _ = self.built(ed)
        self.assertEqual(by['sum']['message']['content'], 'Summary: otters.')

    def test_dropping_preserved_head_updates_metadata(self):
        s, ed = self.open(compacted)
        ed.apply({'op': 'drop', 'ids': ['keep1:0']})
        _, by, _ = self.built(ed)
        meta = by['bound']['compactMetadata']
        self.assertEqual(meta['preservedSegment'], {'headUuid': 'keep2', 'anchorUuid': 'sum', 'tailUuid': 'keep2'})
        self.assertEqual(meta['preservedMessages']['uuids'], ['keep2'])
        self.assertEqual(by['keep2']['parentUuid'], 'sum')
        self.assertEqual(ctxcore.check(ed), ([], []))

    def test_dropping_whole_preserved_segment(self):
        s, ed = self.open(compacted)
        ed.apply({'op': 'drop', 'ids': ['keep1:0', 'keep2:0']})
        out = ctxcore.save(ed)
        self.assertTrue(out['ok'])
        saved = Session(out['path'])
        self.assertEqual([x.uuid for x in saved.steps if x.group == 'live'], ['bound', 'sum', 'new1', 'new2'])
        self.assertNotIn('preservedSegment', saved.entries[saved.by_uuid['bound']]['compactMetadata'])

    def test_untouched_compacted_session_round_trips(self):
        s, ed = self.open(compacted)
        _, by, _ = self.built(ed)
        self.assertEqual(by['keep1']['parentUuid'], 'old2')
        self.assertIsNone(by['bound']['parentUuid'])


class UnknownEntries(Base):
    def test_unknown_types_and_fields_are_copied_through(self):
        s, ed = self.open(unknown_types)
        entries, by, _ = self.built(ed)
        future = [e for e in entries if e['type'] == 'future-thing'][0]
        self.assertEqual(future, {'type': 'future-thing', 'payload': {'a': [1, 2, 3]}, 'sessionId': 'NEW'})
        self.assertEqual(by['holo']['beam'], {'x': 1})
        self.assertEqual(by['a1']['message']['content'][1], {'type': 'quantum_block', 'q': 7})

    def test_unknown_shows_as_locked_other_card(self):
        s, ed = self.open(unknown_types)
        for sid in ('holo:0', 'att:0', 'a1:1'):
            step = s.step_by_id[sid]
            self.assertEqual((step.kind, step.editable, step.droppable), ('other', False, False), sid)
        self.assertEqual(ed.apply({'op': 'drop', 'ids': ['holo:0', 'att:0', 'a1:1']}), [])

    def test_unparseable_line_is_kept_and_reported(self):
        path = unknown_types(Builder()).write(self.tmp.name)
        with open(path, 'a') as f:
            f.write('{"type": "user", "uuid": "half\n')
        s = Session(path)
        ed = Editor(s)
        errors, warnings = ctxcore.check(ed)
        self.assertEqual(errors, [])
        self.assertEqual([w['code'] for w in warnings], ['json'])

    def test_drop_text_next_to_unknown_block_keeps_entry(self):
        s, ed = self.open(unknown_types)
        ed.apply({'op': 'drop', 'ids': ['a1:0']})
        _, by, _ = self.built(ed)
        self.assertEqual(by['a1']['message']['content'], [{'type': 'quantum_block', 'q': 7}])


class Sentences(Base):
    def test_split_is_lossless(self):
        for t in ['One. Two! Three?\n\nNew para: with e.g. an abbreviation. End',
                  '1. First item.\n2. Second item.\n\n```py\nx = 1. + 2.\n\ny = 3\n```\nAfter code. Done.\n',
                  '', 'no punctuation', '   leading and trailing   \n\n', 'v2.1.330 is out. See a.b.c for more.']:
            units = ctxcore.split_sentences(t)
            self.assertEqual(''.join(b + s for b, s, _ in units), t, t)

    def test_sentences(self):
        units = ctxcore.split_sentences('One. Two! Is v2.1.330 out? Yes, e.g. here.')
        self.assertEqual([u[0] for u in units], ['One.', 'Two!', 'Is v2.1.330 out?', 'Yes, e.g. here.'])

    def test_code_block_is_one_unit(self):
        t = 'Before. Here:\n```py\na = 1. \n\nb = 2.\n```\nAfter.'
        units = ctxcore.split_sentences(t)
        code = [u for u in units if u[2] == 'code']
        self.assertEqual(len(code), 1)
        self.assertEqual(code[0][0], '```py\na = 1. \n\nb = 2.\n```')

    def test_list_markers_stay_with_their_item(self):
        units = ctxcore.split_sentences('1. First.\n2. Second.\n- Third.')
        self.assertEqual([u[0] for u in units], ['1. First.', '2. Second.', '- Third.'])

    def test_strike_sentence_and_restore(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'unit', 'id': 'a2:0', 'unit': 1})
        self.assertEqual(ed.effective_text('a2:0'), 'Both read. The second has six lines.')
        self.assertTrue(ed.is_edited('a2:0'))
        ed.apply({'op': 'unit', 'id': 'a2:0', 'unit': 1})
        self.assertEqual(ed.state, {})

    def test_removed_paragraph_keeps_paragraph_break(self):
        units = ctxcore.split_sentences('A one. A two.\n\nB one.\n\nC one.')
        self.assertEqual(ctxcore.join_sentences(units, {2}), 'A one. A two.\n\nC one.')
        self.assertEqual(ctxcore.join_sentences(units, {1}), 'A one.\n\nB one.\n\nC one.')

    def test_all_sentences_struck_counts_as_dropped(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'unit', 'id': 'a2:0', 'unit': 0})
        self.assertTrue(ed.is_dropped('a2:0'))
        _, by, _ = self.built(ed)
        self.assertNotIn('a2', by)
        ed.apply({'op': 'drop', 'ids': ['a2:0'], 'value': False})
        self.assertEqual(ed.state, {})


class ToolResultLines(Base):
    def test_keep_first_and_last_lines_with_marker(self):
        s, ed = self.open(multi_tool_parallel)
        ed.apply({'op': 'keep_lines', 'id': 'r2:0', 'head': 2})
        self.assertEqual(ed.effective_text('r2:0'), 'line 1\nline 2\n[... trimmed]')
        ed.apply({'op': 'keep_lines', 'id': 'r2:0', 'tail': 1})
        self.assertEqual(ed.effective_text('r2:0'), '[... trimmed]\nline 6')
        ed.apply({'op': 'keep_lines', 'id': 'r2:0', 'head': 1, 'tail': 1})
        self.assertEqual(ed.effective_text('r2:0'), 'line 1\n[... trimmed]\nline 6')
        _, by, _ = self.built(ed)
        self.assertEqual(by['r2']['message']['content'][0]['content'], 'line 1\n[... trimmed]\nline 6')
        self.assertEqual(by['r2']['message']['content'][0]['tool_use_id'], 't2')

    def test_marker_once_per_gap(self):
        s, ed = self.open(multi_tool_parallel)
        for k in (1, 2, 4):
            ed.apply({'op': 'unit', 'id': 'r2:0', 'unit': k})
        self.assertEqual(ed.effective_text('r2:0'), 'line 1\n[... trimmed]\nline 4\n[... trimmed]\nline 6')

    def test_marker_can_be_switched_off(self):
        s, ed = self.open(multi_tool_parallel, marker=False)
        ed.apply({'op': 'keep_lines', 'id': 'r2:0', 'head': 1, 'tail': 1})
        self.assertEqual(ed.effective_text('r2:0'), 'line 1\nline 6')

    def test_fully_trimmed_result_without_marker_blocks_save(self):
        s, ed = self.open(multi_tool_parallel, marker=False)
        ed.apply({'op': 'keep_lines', 'id': 'r1:0'})
        out = ctxcore.save(ed)
        self.assertFalse(out['ok'])
        self.assertEqual(out['errors'][0]['code'], 'empty')
        self.assertEqual(out['errors'][0]['steps'], ['r1:0'])

    def test_list_content_with_image(self):
        b = Builder()
        b.user('Screenshot please.', 'q1')
        b.asst([tool('t1', name='Shot')], 'c1')
        b.result('t1', [{'type': 'text', 'text': 'shot taken\nsecond line'},
                        {'type': 'image', 'source': {'type': 'base64', 'data': 'AAAA'}}], 'r1')
        s = Session(b.write(self.tmp.name))
        ed = Editor(s)
        self.assertEqual(s.step_by_id['r1:0'].tokens, ctxcore.approx_tokens('shot taken\nsecond line') + ctxcore.IMAGE_TOKENS)
        ed.apply({'op': 'media', 'id': 'r1:0', 'unit': 1})
        ed.apply({'op': 'unit', 'id': 'r1:0', 'unit': 1})
        entries, _ = ed.build('NEW')
        content = [e for e in entries if e.get('uuid') == 'r1'][0]['message']['content'][0]['content']
        self.assertEqual(content, [{'type': 'text', 'text': 'shot taken\n[... trimmed]\n[... trimmed]'}])


class Rewriting(Base):
    def test_edit_and_reset(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'edit', 'id': 'q1:0', 'text': 'My favourite colour is red.'})
        self.assertTrue(ed.view('q1:0')['edited'])
        _, by, _ = self.built(ed)
        self.assertEqual(by['q1']['message']['content'], 'My favourite colour is red.')
        ed.apply({'op': 'reset', 'id': 'q1:0'})
        self.assertEqual(ed.state, {})

    def test_edit_text_block_keeps_other_fields(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'edit', 'id': 'a1:0', 'text': 'Red it is.'})
        _, by, _ = self.built(ed)
        self.assertEqual(by['a1']['message']['content'], [text('Red it is.')])
        self.assertEqual(by['a1']['message']['usage'], {'input_tokens': 10, 'output_tokens': 5})
        self.assertEqual(s.entries[s.by_uuid['a1']]['message']['content'], [text('Noted. Teal it is.')])

    def test_emptied_edit_blocks_save_and_names_the_step(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'edit', 'id': 'a1:0', 'text': '   '})
        out = ctxcore.save(ed)
        self.assertFalse(out['ok'])
        self.assertEqual([(e['code'], e['steps']) for e in out['errors']], [('empty', ['a1:0'])])
        self.assertEqual(len([f for f in os.listdir(self.tmp.name) if f.endswith('.jsonl')]), 1)

    def test_sentences_can_be_struck_in_rewritten_text(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'edit', 'id': 'a1:0', 'text': 'Red. Blue. Green.'})
        ed.apply({'op': 'unit', 'id': 'a1:0', 'unit': 1})
        self.assertEqual(ed.effective_text('a1:0'), 'Red. Green.')


class UndoRedo(Base):
    def test_every_action_can_be_undone_and_redone(self):
        s, ed = self.open(multi_tool_parallel)
        ops = [
            {'op': 'drop', 'ids': ['c1:0']},
            {'op': 'unit', 'id': 'a2:0', 'unit': 0},
            {'op': 'keep_lines', 'id': 'r2:0', 'head': 1},
            {'op': 'edit', 'id': 'q1:0', 'text': 'Read them.'},
            {'op': 'reset', 'id': 'q1:0'},
            {'op': 'drop', 'ids': ['c1:0'], 'value': False},
            {'op': 'bulk_results', 'min_tokens': 0},
        ]
        snapshots = [json.dumps(ed.build('N')[0], sort_keys=True)]
        for op in ops:
            self.assertTrue(ed.apply(op), op)
            snapshots.append(json.dumps(ed.build('N')[0], sort_keys=True))
        for k in range(len(ops) - 1, -1, -1):
            self.assertTrue(ed.undo())
            self.assertEqual(json.dumps(ed.build('N')[0], sort_keys=True), snapshots[k])
        self.assertEqual(ed.state, {})
        self.assertEqual(ed.undo(), [])
        for k in range(1, len(ops) + 1):
            self.assertTrue(ed.redo())
            self.assertEqual(json.dumps(ed.build('N')[0], sort_keys=True), snapshots[k])
        self.assertEqual(ed.redo(), [])

    def test_new_action_clears_redo(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'drop', 'ids': ['a1:0']})
        ed.undo()
        ed.apply({'op': 'drop', 'ids': ['a2:0']})
        self.assertEqual(ed.redo(), [])

    def test_no_op_is_not_recorded(self):
        s, ed = self.open(plain_chat)
        ed.apply({'op': 'drop', 'ids': ['a1:0'], 'value': False})
        self.assertEqual(ed.counters()['undo'], 0)


class SaveChecks(unittest.TestCase):
    def line(self, **e):
        return json.dumps(e)

    def codes(self, lines):
        return sorted(i['code'] for i in ctxcore.check_lines(lines))

    def msg(self, uuid, parent, role, content):
        return self.line(type=role, uuid=uuid, parentUuid=parent, message={'role': role, 'content': content})

    def test_clean_file_passes(self):
        self.assertEqual(self.codes([
            self.msg('a', None, 'user', 'hi'),
            self.msg('b', 'a', 'assistant', [tool('t1')]),
            self.msg('c', 'b', 'user', [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'x'}]),
            self.line(type='mode', mode='normal')]), [])

    def test_bad_json(self):
        self.assertEqual(self.codes([self.msg('a', None, 'user', 'hi'), '{"type": "user", ']), ['json'])

    def test_result_without_call_and_call_without_result(self):
        self.assertEqual(self.codes([
            self.msg('a', None, 'assistant', [tool('t1')]),
            self.msg('b', 'a', 'user', [{'type': 'tool_result', 'tool_use_id': 't9', 'content': 'x'}])]),
            ['tool-pair', 'tool-pair'])

    def test_parent_must_be_kept_or_null(self):
        self.assertEqual(self.codes([self.msg('a', None, 'user', 'hi'), self.msg('b', 'gone', 'assistant', [text('x')])]), ['parent'])

    def test_uuid_must_be_unique(self):
        self.assertEqual(self.codes([self.msg('a', None, 'user', 'hi'), self.msg('a', None, 'user', 'again')]), ['duplicate-uuid'])

    def test_no_empty_content(self):
        self.assertEqual(self.codes([self.msg('a', None, 'user', ' '), self.msg('b', 'a', 'assistant', []),
                                     self.msg('c', 'b', 'assistant', [text('')])]), ['empty', 'empty', 'empty'])

    def test_issues_already_in_the_original_are_warnings_not_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            b = Builder()
            b.user('Run it.', 'q1')
            b.asst([tool('t1')], 'c1')          # interrupted: no result
            b.user('Never mind. What is 1+1?', 'q2')
            b.asst([text('Two.')], 'a2', mid='m2')
            s = Session(b.write(tmp))
            ed = Editor(s)
            errors, warnings = ctxcore.check(ed)
            self.assertEqual(errors, [])
            self.assertEqual([(w['code'], w['steps']) for w in warnings], [('tool-pair', ['c1:0'])])
            ed.apply({'op': 'drop', 'ids': ['c1:0']})
            self.assertEqual(ctxcore.check(ed), ([], []))


if __name__ == '__main__':
    unittest.main()
