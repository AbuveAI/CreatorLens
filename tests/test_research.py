from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from creatorlens import asr, research
from creatorlens.collection import collect
from creatorlens.demo import run as demo
from creatorlens.feishu import plan
from creatorlens.storage import StopCapture, read, write, file_hash
from creatorlens.tasks import Task, create, now
from creatorlens.workspace import initialize
from test_collection import FakeProvider, note, nid, ACCOUNT


class ResearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / 'workspace'
        initialize(self.root)

    def sample(self):
        result = demo(self.root)
        return Task(self.root, result['task'])

    def video(self):
        task = create(self.root, url=f'https://www.xiaohongshu.com/user/profile/{ACCOUNT}', name='Self-authored', batch='video', count=1)
        with task.lock():
            collect(task, FakeProvider([note(1, 'video')]))
        return task

    def test_demo_report_complete_and_real_png_no_network(self):
        task = self.sample()
        checked = research.report_status(task)
        self.assertTrue(checked['research_complete'])
        self.assertIn('自制', task.p('report/report.html').read_text())
        self.assertFalse(task.p('private').exists())
        for file in task.p('works').rglob('*.png'):
            self.assertTrue(file.read_bytes().startswith(b'\x89PNG'))

    def test_new_analysis_invalid_until_reviewed_and_prepare_preserves_edits(self):
        task = self.sample()
        value = task.get('research/analysis.json')
        value['status'] = 'draft'
        value['summary'] = 'Agent in-progress edits'
        task.save('research/analysis.json', value)
        research.prepare(task)
        self.assertEqual(task.get('research/analysis.json')['summary'], value['summary'])
        with self.assertRaisesRegex(StopCapture, 'analysis_not_reviewed'):
            research.report(task)

    def test_one_unread_image_blocks_report(self):
        task = self.sample()
        wid = task.get('capture.json')['selected'][0]['id']
        review = task.get(f'research/reviews/{wid}.json')
        review['images'][1]['viewed'] = False
        task.save(f'research/reviews/{wid}.json', review)
        with self.assertRaisesRegex(StopCapture, 'image_review_incomplete'):
            research.validate(task)

    def test_missing_comment_read_blocks_report(self):
        task = self.sample()
        wid = task.get('capture.json')['selected'][0]['id']
        review = task.get(f'research/reviews/{wid}.json')
        review['comments_read'] = False
        task.save(f'research/reviews/{wid}.json', review)
        with self.assertRaisesRegex(StopCapture, 'work_not_reviewed'):
            research.validate(task)

    def test_unknown_citation_blocks_report(self):
        task = self.sample()
        value = task.get('research/analysis.json')
        value['sections'][0]['insights'][0]['evidence'] = ['nonexistent:comment:99']
        task.save('research/analysis.json', value)
        with self.assertRaisesRegex(StopCapture, 'evidence_reference'):
            research.validate(task)

    def test_changed_review_stales_report_and_feishu_plan(self):
        task = self.sample()
        value = task.get('research/analysis.json')
        value['summary'] = 'New analysis'
        task.save('research/analysis.json', value)
        for operation in (research.report_status, plan):
            with self.assertRaisesRegex(StopCapture, 'report_missing_or_stale'):
                operation(task)

    def test_report_tampering_detected(self):
        task = self.sample()
        task.p('report/report.md').write_text('changed')
        with self.assertRaisesRegex(StopCapture, 'report_file_changed'):
            research.report_status(task)

    def test_untrusted_html_is_escaped(self):
        task = self.sample()
        value = task.get('research/analysis.json')
        value['summary'] = '<script>alert(1)</script> ![leak](https://example.invalid/)'
        task.save('research/analysis.json', value)
        research.report(task)
        html = task.p('report/report.html').read_text()
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<img', html)

    def test_feishu_plan_has_all_images_and_no_raw_or_video(self):
        task = self.sample()
        result = plan(task)
        self.assertFalse(result['cloud_written'])
        value = task.get('exports/feishu-plan.json')
        self.assertEqual(len(value['attachments']), 5)  # Two covers plus all three originals.
        self.assertTrue(all(x['path'].endswith('.png') for x in value['attachments']))
        self.assertNotIn('xsec_token', json.dumps(value))

    def test_video_waits_for_transcript_and_rejects_bad_timestamps(self):
        task = self.video()
        self.assertEqual(research.prepare(task)['video_transcripts_missing'], [nid(1)])
        with self.assertRaisesRegex(StopCapture, 'timestamp'):
            research.transcript(task, nid(1), {'provenance': 'test', 'text': 'x', 'segments': [{'start_ms': 0, 'end_ms': 999999, 'text': 'x'}]})

    def test_video_correction_requires_reason_and_original_untouched(self):
        task = self.video()
        data = research.transcript(task, nid(1), {'provenance': 'authored fixture', 'text': 'Hello',
                                                'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': 'Hello'}]})
        task.save(f'research/transcripts/{nid(1)}.json', data)
        research.prepare(task)
        path = f'research/reviews/{nid(1)}.json'
        review = task.get(path)
        review.update(status='reviewed', reviewed_by='Test', reviewed_at=now(), summary='Read', comments_read=True, full_text_read=True)
        review['segments'][0].update(corrected='Hello world', reviewed=True)
        task.save(path, review)
        with self.assertRaisesRegex(StopCapture, 'correction_reason'):
            research.validate(task)
        self.assertEqual(task.get(f'research/transcripts/{nid(1)}.json'), data)

    def test_asr_authorization_required_before_any_io(self):
        task = self.video()
        with self.assertRaisesRegex(StopCapture, 'authorization'):
            asr.submit(task, nid(1))
        self.assertFalse(task.p('processing').exists())

    def test_asr_uncertain_submit_reuses_id_queries_only(self):
        task = self.video()
        calls = []
        def transport(root, action, payload, request_id):
            calls.append((action, request_id))
            self.assertEqual(task.get(f'processing/asr/{nid(1)}/job.json')['request_id'], request_id)
            if action == 'submit':
                raise OSError('private-detail-must-not-escape')
            return '20000001', {}
        with patch('creatorlens.asr.key', return_value='fake-key'):
            with self.assertRaisesRegex(StopCapture, 'submit_uncertain'):
                asr.submit(task, nid(1), allow_paid=True, audio_url='https://example.invalid/a.mp3', transport=transport)
            result = asr.submit(task, nid(1), allow_paid=True, transport=transport)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual([x[0] for x in calls], ['submit', 'query'])
        self.assertEqual(calls[0][1], calls[1][1])

    def test_asr_success_saves_raw_timestamped_unreviewed_text(self):
        task = self.video()
        payload = {'result': {'text': 'Hello', 'utterances': [{'start_time': 0, 'end_time': 1000, 'text': 'Hello'}]}}
        def transport(root, action, body, request_id):
            return '20000000', {} if action == 'submit' else payload
        with patch('creatorlens.asr.key', return_value='fake-key'):
            asr.submit(task, nid(1), allow_paid=True, audio_url='https://example.invalid/a.mp3', transport=transport)
        asr.query(task, nid(1), transport=transport)
        tx = task.get(f'research/transcripts/{nid(1)}.json')
        self.assertEqual(tx['review_status'], 'not_reviewed')
        self.assertEqual(tx['segments'][0]['end_ms'], 1000)
        self.assertEqual(task.get(f'processing/asr/{nid(1)}/raw-result.json'), payload)
        self.assertTrue(asr.query(task, nid(1), transport=lambda *_: self.fail('completed job must be local'))['reused'])

    def test_asr_no_speech_does_not_claim_completed(self):
        task = self.video()
        with patch('creatorlens.asr.key', return_value='fake-key'):
            asr.submit(task, nid(1), allow_paid=True, audio_url='https://example.invalid/a.mp3', transport=lambda *_: ('20000000', {}))
        with self.assertRaises(StopCapture):
            asr.query(task, nid(1), transport=lambda *_: ('20000000', {'result': {'text': '', 'utterances': []}}))
        self.assertFalse(task.p(f'research/transcripts/{nid(1)}.json').exists())

    def test_transcript_replacement_changes_source_hash_and_blocks_old_review(self):
        task = self.video()
        data = research.transcript(task, nid(1), {'provenance': 'fixture', 'text': 'Hello', 'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': 'Hello'}]})
        task.save(f'research/transcripts/{nid(1)}.json', data)
        research.prepare(task)
        data['segments'][0]['text'] = 'Changed'
        task.save(f'research/transcripts/{nid(1)}.json', data)
        with self.assertRaisesRegex(StopCapture, 'stale'):
            research.prepare(task)


if __name__ == '__main__':
    unittest.main()
