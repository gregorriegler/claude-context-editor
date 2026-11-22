"""Unit tests for the suggestion plumbing. No model is called."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ctxcore  # noqa: E402
import ctxsuggest  # noqa: E402
from test_core import Builder, multi_tool_parallel, text, tool  # noqa: E402


def yak(b):
    """A main task, interrupted by a side task between a stash and a stash pop."""
    n = [0]

    def call(name, out, **inp):
        n[0] += 1
        tid = 't%d' % n[0]
        b.asst([tool(tid, name, **inp)], 'c%d' % n[0], mid='mc%d' % n[0])
        b.result(tid, out, 'r%d' % n[0])

    b.user('Add a CSV export to the monthly report. Keep the column order of the PDF.', 'q1')
    call('Read', 'class MonthlyReport:\n    def to_pdf(self): ...', file_path='/app/reports/monthly.py')
    call('Edit', 'ok', file_path='/app/reports/monthly.py', old_string='...', new_string='def to_csv(self): ...')
    b.asst([text('I added to_csv with the PDF column order. Next I will wire it into the export view.')], 'a1', mid='m1')
    b.user('Wait. The date helper prints the wrong week number, and the export will need it. Stash this and fix that first.', 'q2')
    call('Bash', 'Saved working directory and index state WIP on main', command='git stash')
    call('Read', 'def week_number(d):\n    return d.isocalendar()[1] - 1', file_path='/app/utils/dates.py')
    call('Edit', 'ok', file_path='/app/utils/dates.py', old_string='- 1', new_string='')
    call('Bash', '12 passed', command='pytest tests/test_dates.py')
    call('Bash', '[main 3f2a1c9] Fix week_number off by one', command='git commit -am "Fix week_number off by one"')
    call('Bash', 'On branch main\nChanges not staged for commit: reports/monthly.py', command='git stash pop')
    b.asst([text('week_number was off by one because of a stray "- 1". Fixed, tests pass, committed, and the stash is restored.')], 'a2', mid='m2')
    b.user('Good. Back to the CSV export.', 'q3')
    call('Read', 'def export_view(request): ...', file_path='/app/views/export.py')
    call('Edit', 'ok', file_path='/app/views/export.py', old_string='...', new_string='csv branch')
    b.asst([text('The export view now offers CSV next to PDF.')], 'a3', mid='m3')
    return b


class Suggest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = ctxcore.Session(multi_tool_parallel(Builder()).write(self.tmp.name))
        self.ed = ctxcore.Editor(self.s)

    def test_items_carry_position_type_tokens_preview(self):
        items = ctxsuggest.build_items(self.ed)
        self.assertEqual([i['type'] for i in items], ['You', 'Tool call', 'Tool call', 'Tool result', 'Tool result', 'Claude'])
        self.assertEqual(sorted(items[0]), ['preview', 'step', 'tokens', 'type'])
        self.assertEqual(items[1]['tool'], 'Read')

    def test_preview_is_first_300_and_last_100_unless_full(self):
        long = 'a' * 500 + 'MIDDLE' + 'z' * 500
        p = ctxsuggest.preview(long, False)
        self.assertTrue(p.startswith('a' * 300) and p.endswith('z' * 100))
        self.assertNotIn('MIDDLE', p)
        self.assertEqual(ctxsuggest.preview(long, True), long)

    def test_dropped_steps_are_not_sent(self):
        self.ed.apply({'op': 'drop', 'ids': ['c1:0']})
        self.assertNotIn('t1', json.dumps(ctxsuggest.build_items(self.ed)))

    def test_batches_of_200(self):
        items = [{'step': n, 'preview': 'x'} for n in range(450)]
        self.assertEqual([len(b) for b in ctxsuggest.batches(items)], [200, 200, 50])

    def test_batches_also_split_by_size(self):
        items = [{'step': n, 'numbered_text': 'x' * 40} for n in range(5)]
        self.assertEqual([len(b) for b in ctxsuggest.batches(items, max_chars=100)], [2, 2, 1])

    def _full(self, reply):
        return ctxsuggest.suggest(self.ed, full=True, caller=lambda prompt, model: (json.dumps(reply), None))

    def _step(self, sid):
        return self.s.step_by_id[sid].index

    def test_full_text_numbers_lines_and_sentences(self):
        items = {i['step']: i for i in ctxsuggest.build_items(self.ed, full=True)}
        self.assertEqual(items[self._step('r2:0')]['numbered_text'].split('\n')[:2], ['1| line 1', '2| line 2'])
        self.assertTrue(items[self._step('a2:0')]['numbered_text'].startswith('1| Both read.'))
        self.assertIn('preview', items[self._step('q1:0')])       # your own messages are never trimmed
        self.assertIn('preview', items[self._step('r1:0')])       # a single line has nothing to trim
        self.assertNotIn('numbered_text', json.dumps(ctxsuggest.build_items(self.ed)))

    def test_struck_lines_keep_their_numbers(self):
        self.ed.apply({'op': 'unit', 'id': 'r2:0', 'unit': 1})
        item = [i for i in ctxsuggest.build_items(self.ed, full=True) if i['step'] == self._step('r2:0')][0]
        self.assertEqual(item['numbered_text'].split('\n')[:2], ['1| line 1', '3| line 3'])

    def test_keep_ranges_become_removed_lines(self):
        out = self._full([{'step': self._step('r2:0'), 'action': 'trim', 'reason': 'r', 'keep': [[1, 2], 6]}])
        sg = out['suggestions'][0]
        self.assertEqual((sg['removed'], sg['cut'], sg['parts']), ([2, 3, 4], 3, 6))
        self.assertEqual(self.ed.state, {})
        self.ed.apply({'op': 'set_removed', 'id': sg['id'], 'removed': sg['removed']})
        self.assertEqual(self.ed.effective_text('r2:0'), 'line 1\nline 2\n[... trimmed]\nline 6')
        self.ed.undo()
        self.assertEqual(self.ed.state, {})

    def test_bad_ranges_fall_back_to_a_plain_trim(self):
        for keep in ([[0, 2]], [[5, 9]], [[3, 2]], ['1-2'], []):
            sg = self._full([{'step': self._step('r2:0'), 'action': 'trim', 'reason': 'r', 'keep': keep}])['suggestions'][0]
            self.assertEqual(sg['action'], 'trim')
            self.assertNotIn('removed', sg)

    def test_keeping_everything_is_no_suggestion(self):
        self.assertEqual(self._full([{'step': self._step('r2:0'), 'action': 'trim', 'reason': 'r', 'keep': [[1, 6]]}])['suggestions'], [])

    def test_ranges_are_ignored_without_full_text(self):
        reply = json.dumps([{'step': self._step('r2:0'), 'action': 'trim', 'reason': 'r', 'keep': [[1, 2]]}])
        sg = ctxsuggest.suggest(self.ed, caller=lambda prompt, model: (reply, None))['suggestions'][0]
        self.assertNotIn('removed', sg)

    def test_rewrite_only_for_tool_results_and_only_if_shorter(self):
        out = self._full([{'step': self._step('r2:0'), 'action': 'rewrite', 'reason': 'r', 'text': 'six lines'},
                          {'step': self._step('a2:0'), 'action': 'rewrite', 'reason': 'r', 'text': 'ok'}])
        by = {sg['id']: sg for sg in out['suggestions']}
        self.assertEqual(by['r2:0']['text'], '[summarised] six lines')
        self.assertEqual(by['a2:0']['action'], 'trim')
        self.assertNotIn('text', by['a2:0'])
        long = self._full([{'step': self._step('r2:0'), 'action': 'rewrite', 'reason': 'r', 'text': 'x' * 500}])['suggestions'][0]
        self.assertEqual(long['action'], 'trim')
        self.assertEqual(self.ed.state, {})

    def test_batch_op_is_one_undo_step_and_all_or_nothing(self):
        self.ed.apply({'op': 'batch', 'ops': [{'op': 'drop', 'ids': ['a2:0']},
                                              {'op': 'set_removed', 'id': 'r2:0', 'removed': [0]},
                                              {'op': 'edit', 'id': 'r1:0', 'text': '[summarised] alpha'}]})
        self.assertEqual(self.ed.counters()['undo'], 1)
        self.ed.undo()
        self.assertEqual(self.ed.state, {})
        with self.assertRaises(ValueError):
            self.ed.apply({'op': 'batch', 'ops': [{'op': 'drop', 'ids': ['a2:0']}, {'op': 'set_removed', 'id': 'r2:0', 'removed': [99]}]})
        self.assertEqual(self.ed.state, {})

    def test_parse_tolerates_fences_and_ignores_junk(self):
        reply = 'Sure:\n```json\n[{"step": 2, "action": "drop", "reason": "dead\\n end"}, {"step": 99, "action": "drop"}, {"step": 3, "action": "explode"}, {"step": 2, "action": "trim"}]\n```'
        self.assertEqual(ctxsuggest.parse_reply(reply, {2, 3}), [{'step': 2, 'action': 'drop', 'reason': 'dead end'}])
        with self.assertRaises(ValueError):
            ctxsuggest.parse_reply('I cannot help with that.', {1})

    def test_suggestions_never_change_the_edit_state(self):
        steps = {st.id: st.index for st in self.s.steps}
        reply = json.dumps([{'step': steps['c1:0'], 'action': 'drop', 'reason': 'old read'},
                            {'step': steps['r2:0'], 'action': 'trim', 'reason': 'long'},
                            {'step': steps['q1:0'], 'action': 'keep', 'reason': 'instruction'},
                            {'step': steps['c2:0'], 'action': 'trim', 'reason': 'calls cannot be trimmed'}])
        out = ctxsuggest.suggest(self.ed, caller=lambda prompt, model: (reply, None))
        self.assertEqual([(s['id'], s['action']) for s in out['suggestions']], [('c1:0', 'drop'), ('r2:0', 'trim')])
        self.assertEqual(self.ed.state, {})
        self.assertEqual(self.ed.counters()['undo'], 0)

    def test_one_failed_batch_is_reported_not_fatal(self):
        def boom(prompt, model):
            raise RuntimeError('rate limited')
        out = ctxsuggest.suggest(self.ed, caller=boom)
        self.assertEqual(out['suggestions'], [])
        self.assertIn('rate limited', out['errors'][0])

    def test_claude_json_output_both_shapes(self):
        self.assertEqual(ctxsuggest._result_text(json.dumps({'type': 'result', 'result': '[]'})), '[]')
        self.assertEqual(ctxsuggest._result_text(json.dumps([{'type': 'system'}, {'type': 'result', 'result': '[1]'}])), '[1]')
        with self.assertRaises(RuntimeError):
            ctxsuggest._result_text(json.dumps([{'type': 'result', 'is_error': True, 'result': 'Credit balance is too low'}]))


class Detours(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = ctxcore.Session(yak(Builder()).write(self.tmp.name))
        self.ed = ctxcore.Editor(self.s)
        self.i = lambda sid: self.s.step_by_id[sid].index

    def _find(self, reply):
        return ctxsuggest.find_detours(self.ed, caller=lambda prompt, model: (json.dumps(reply), None))

    def test_outline_has_messages_and_one_line_calls_but_no_tool_output(self):
        items = ctxsuggest.build_outline(self.ed)
        texts = [i['text'] for i in items]
        self.assertIn('Bash: git stash', texts)
        self.assertIn('Edit: /app/utils/dates.py', texts)
        self.assertTrue(any(t.startswith('Wait. The date helper') for t in texts))
        self.assertNotIn('12 passed', json.dumps(items))
        self.assertEqual(set(i['type'] for i in items), {'You', 'Claude', 'Tool call'})

    def test_detour_becomes_a_range_with_anchor_and_changed_files(self):
        out = self._find([{'first': self.i('q2:0'), 'last': self.i('a2:0'), 'reason': 'stash, side fix, pop', 'note': 'I fixed week_number.'}])
        d = out['detours'][0]
        self.assertEqual(d['anchor'], 'a2:0')
        self.assertEqual(d['files'], ['/app/utils/dates.py'])
        self.assertEqual((d['ids'][0], d['ids'][-1]), ('q2:0', 'a2:0'))
        self.assertEqual(len(d['ids']), 14)  # the request, six calls with results, the closing message
        self.assertEqual(self.ed.state, {})

    def test_accepting_a_detour_is_one_undo_step_and_saves_cleanly(self):
        d = self._find([{'first': self.i('q2:0'), 'last': self.i('a2:0'), 'reason': 'r', 'note': 'I fixed week_number.'}])['detours'][0]
        note = d['tag'] + d['note'] + '\nFiles changed: ' + ', '.join(d['files'])
        self.ed.apply({'op': 'batch', 'ops': [{'op': 'drop', 'ids': [i for i in d['ids'] if i != d['anchor']]},
                                              {'op': 'edit', 'id': d['anchor'], 'text': note}]})
        self.assertEqual(self.ed.counters()['undo'], 1)
        self.assertEqual(ctxcore.check(self.ed)[0], [])
        kept = [st.id for st in self.s.steps if st.group == 'live' and not self.ed.is_dropped(st.id)]
        self.assertEqual(kept[kept.index('a1:0') + 1], 'a2:0')
        self.assertEqual(kept[kept.index('a2:0') + 1], 'q3:0')
        self.assertNotIn('stash', json.dumps(ctxsuggest.build_outline(self.ed)))
        self.ed.undo()
        self.assertEqual(self.ed.state, {})

    def test_bad_and_overlapping_ranges_are_ignored(self):
        hi = max(st.index for st in self.s.steps)
        got = ctxsuggest.parse_detours(json.dumps([{'first': 5, 'last': 3}, {'first': 0, 'last': hi + 5}, {'first': '4', 'last': 9},
                                                   {'first': 4, 'last': 9, 'note': 'a'}, {'first': 8, 'last': 12, 'note': 'b'},
                                                   {'first': 13, 'last': 14, 'note': 'c'}]), 0, hi)
        self.assertEqual([(d['first'], d['last']) for d in got], [(4, 9), (13, 14)])

    def test_range_moved_by_hand_and_user_message_as_anchor(self):
        r = ctxsuggest.resolve_detour(self.ed, self.i('q2:0'), self.i('c3:0'))
        self.assertEqual(r['anchor'], 'q2:0')  # no message from Claude in this range
        self.assertEqual(r['files'], [])

    def test_long_outline_is_split_with_overlap(self):
        items = [{'step': n, 'type': 'You', 'text': 'x' * 10} for n in range(100)]
        parts = ctxsuggest.outline_parts(items, max_chars=400, overlap=10)
        self.assertEqual([(p[0]['step'], p[-1]['step']) for p in parts], [(0, 39), (30, 69), (60, 99)])


if __name__ == '__main__':
    unittest.main()
