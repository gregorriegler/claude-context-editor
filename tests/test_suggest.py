"""Unit tests for the suggestion plumbing. No model is called."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ctxcore  # noqa: E402
import ctxsuggest  # noqa: E402
from test_core import Builder, multi_tool_parallel, text  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
