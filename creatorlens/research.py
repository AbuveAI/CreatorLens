"""Agent-authored research with source-bound review contracts; no invented analysis."""
from datetime import datetime
import html
import math
import re

from .collection import verify
from .storage import digest, ensure, file_hash, read, write_text
from .tasks import now

TOPICS = ('定位与受众', '选题与内容结构', '表达与呈现', '评论反馈', '可借鉴做法', '下一步创作实验')


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def identity(value):
    ensure(nonempty(value.get('reviewed_by')), 'reviewer_required')
    try:
        stamp = datetime.fromisoformat(value.get('reviewed_at', ''))
        ensure(stamp.tzinfo is not None, 'review_time_requires_timezone')
    except (TypeError, ValueError):
        ensure(False, 'review_time_required_iso8601')


def transcript(task, work_id, value):
    work = task.get(f'works/{work_id}/work.json')
    ensure(work and work['type'] == 'video', 'video_work_required')
    ensure(isinstance(value, dict) and nonempty(value.get('provenance')) and nonempty(value.get('text')),
           'transcript_text_and_provenance_required')
    segments = value.get('segments')
    ensure(isinstance(segments, list) and segments, 'timestamped_segments_required')
    previous = -1
    for seg in segments:
        ensure(isinstance(seg, dict), 'invalid_transcript_segment')
        start, end = seg.get('start_ms'), seg.get('end_ms')
        ensure(type(start) in (int, float) and type(end) in (int, float)
               and math.isfinite(start) and math.isfinite(end)
               and 0 <= start < end <= work['video']['duration_seconds'] * 1000 + 1000
               and start >= previous and nonempty(seg.get('text')), 'invalid_transcript_timestamp_or_text')
        previous = start
    return {'schema_version': 1, 'work_id': work_id, 'video_hash': work['video']['sha256'],
            'provenance': value['provenance'], 'text': value['text'],
            'segments': [{'start_ms': s['start_ms'], 'end_ms': s['end_ms'], 'text': s['text']} for s in segments],
            'review_status': 'not_reviewed'}


def import_transcript(task, work_id, file):
    verify(task)
    ensure(work_id in [w['id'] for w in task.get('capture.json')['selected']], 'work_not_selected')
    data = transcript(task, work_id, read(file))
    target = f'research/transcripts/{work_id}.json'
    existing = task.get(target)
    ensure(existing is None or existing == data, 'transcript_exists_do_not_overwrite_evidence')
    task.save(target, data)
    return {'work_id': work_id, 'imported': True, 'reviewed': False}


def sources(task):
    verify(task)
    capture = task.get('capture.json')
    works = [task.get(f"works/{w['id']}/work.json") for w in capture['selected']]
    return capture, works


def source_hash(task, work):
    value = task.get(f"research/transcripts/{work['id']}.json")
    if value:
        checked = transcript(task, work['id'], value)
        ensure(checked == value, 'transcript_source_changed')
    return digest({'work': work, 'comments': task.get(work['comments_file']), 'transcript': value})


def prepare(task):
    capture, works = sources(task)
    ensure(works, 'no_works_for_research')
    pending = []
    for work in works:
        wid = work['id']
        tx = task.get(f'research/transcripts/{wid}.json')
        if work['type'] == 'video' and tx is None:
            pending.append(wid)
            continue
        path = f'research/reviews/{wid}.json'
        content = {'schema_version': 1, 'work_id': wid, 'source_hash': source_hash(task, work),
                   'status': 'draft', 'reviewed_by': '', 'reviewed_at': '', 'summary': '',
                   'doubts': [], 'comments_read': False}
        if work['type'] == 'image':
            content['images'] = [{'number': i, 'path': row['path'], 'sha256': row['sha256'],
                                  'viewed': False, 'ocr_text': '', 'visual_description': '', 'interpretation': ''}
                                 for i, row in enumerate(work['images'], 1)]
        else:
            content['segments'] = [{'number': i, 'start_ms': s['start_ms'], 'end_ms': s['end_ms'],
                                    'original': s['text'], 'corrected': s['text'], 'reason': '', 'reviewed': False}
                                   for i, s in enumerate(tx['segments'], 1)]
            content['full_text_read'] = False
        existing = task.get(path)
        if existing is not None:
            ensure(existing.get('source_hash') == content['source_hash'], 'review_stale_do_not_overwrite')
        else:
            task.save(path, content)
    if task.get('research/analysis.json') is None:
        task.save('research/analysis.json', {'schema_version': 1, 'capture_hash': capture['manifest_hash'],
            'status': 'draft', 'reviewed_by': '', 'reviewed_at': '', 'question': '', 'summary': '',
            'limitations': [], 'sections': [{'title': title, 'insights': []} for title in TOPICS]})
    catalog = {}
    for work in works:
        wid = work['id']
        catalog[f'{wid}:work'] = {'type': 'work', 'file': f'works/{wid}/work.json'}
        for i, _ in enumerate(work.get('images', []), 1):
            catalog[f'{wid}:image:{i}'] = {'type': 'image', 'number': i, 'file': work['images'][i-1]['path']}
        tx = task.get(f'research/transcripts/{wid}.json')
        for i, _ in enumerate((tx or {}).get('segments', []), 1):
            catalog[f'{wid}:segment:{i}'] = {'type': 'segment', 'number': i,
                                          'file': f'research/transcripts/{wid}.json'}
        for row in task.get(work['comments_file'])['rows']:
            catalog[f"{wid}:comment:{row['id']}"] = {'type': 'comment', 'file': work['comments_file'], 'id': row['id']}
    task.save('research/evidence-index.json', catalog)
    return {'research_folder': str(task.p('research')), 'video_transcripts_missing': pending,
            'review_templates_ready': len(works) - len(pending), 'research_complete': False}


def validate(task):
    capture, works = sources(task)
    ensure(works, 'no_works_for_research')
    catalog = task.get('research/evidence-index.json', {})
    expected_refs = set()
    reviews = []
    for work in works:
        wid = work['id']
        review = task.get(f'research/reviews/{wid}.json', {})
        ensure(review.get('schema_version') == 1 and review.get('work_id') == wid
               and review.get('source_hash') == source_hash(task, work), 'review_missing_or_stale')
        ensure(review.get('status') == 'reviewed' and nonempty(review.get('summary'))
               and review.get('comments_read') is True, 'work_not_reviewed')
        identity(review)
        ensure(isinstance(review.get('doubts'), list) and all(nonempty(x) for x in review['doubts']), 'invalid_review_doubts')
        expected_refs.add(f'{wid}:work')
        if work['type'] == 'image':
            rows = review.get('images', [])
            ensure(len(rows) == len(work['images']), 'all_images_must_be_reviewed')
            for i, (row, media) in enumerate(zip(rows, work['images']), 1):
                ensure(row.get('number') == i and row.get('path') == media['path']
                       and row.get('sha256') == media['sha256'] and row.get('viewed') is True
                       and nonempty(row.get('visual_description')) and nonempty(row.get('interpretation'))
                       and isinstance(row.get('ocr_text'), str), 'image_review_incomplete')
                expected_refs.add(f'{wid}:image:{i}')
        else:
            tx = task.get(f'research/transcripts/{wid}.json')
            rows = review.get('segments', [])
            ensure(tx and review.get('full_text_read') is True and len(rows) == len(tx['segments']),
                   'full_transcript_review_required')
            for i, (row, segment) in enumerate(zip(rows, tx['segments']), 1):
                ensure(row.get('number') == i and row.get('original') == segment['text']
                       and row.get('start_ms') == segment['start_ms'] and row.get('end_ms') == segment['end_ms']
                       and row.get('reviewed') is True and nonempty(row.get('corrected')), 'segment_review_incomplete')
                ensure(row['original'] == row['corrected'] or nonempty(row.get('reason')), 'correction_reason_required')
                expected_refs.add(f'{wid}:segment:{i}')
        for row in task.get(work['comments_file'])['rows']:
            expected_refs.add(f"{wid}:comment:{row['id']}")
        reviews.append(review)
    ensure(set(catalog) == expected_refs, 'evidence_index_stale_run_research_prepare')
    analysis = task.get('research/analysis.json', {})
    ensure(analysis.get('schema_version') == 1 and analysis.get('capture_hash') == capture['manifest_hash'], 'analysis_stale')
    ensure(analysis.get('status') == 'reviewed' and nonempty(analysis.get('question'))
           and nonempty(analysis.get('summary')), 'analysis_not_reviewed')
    identity(analysis)
    ensure(isinstance(analysis.get('limitations'), list) and analysis['limitations']
           and all(nonempty(x) for x in analysis['limitations']), 'research_limitations_required')
    sections = analysis.get('sections', [])
    ensure(isinstance(sections, list) and len(sections) == len(TOPICS)
           and [s.get('title') for s in sections] == list(TOPICS), 'research_sections_required')
    for section in sections:
        ensure(isinstance(section.get('insights'), list) and section['insights'], 'section_insights_required')
        for insight in section['insights']:
            ensure(isinstance(insight, dict) and insight.get('kind') in ('fact', 'author_statement', 'inference', 'recommendation')
                   and nonempty(insight.get('text')), 'insight_kind_and_text_required')
            refs = insight.get('evidence')
            ensure(isinstance(refs, list) and refs and all(isinstance(r, str) and r in expected_refs for r in refs),
                   'unknown_or_missing_evidence_reference')
    snapshot = digest({'capture_hash': capture['manifest_hash'], 'reviews': reviews, 'analysis': analysis})
    return {'research_complete': True, 'reviewed_works': len(works), 'research_hash': snapshot,
            'validation_scope': 'file_integrity_and_agent_attestations_not_independent_semantic_audit'}


def esc(value):
    # Untrusted platform text remains text in both Markdown and HTML outputs.
    value = html.escape(str(value), quote=True)
    return re.sub(r'([\\`*_{}\[\]()#+.!|>-])', r'\\\1', value)


def report(task):
    checked = validate(task)
    capture, works = sources(task)
    analysis = task.get('research/analysis.json')
    lines = ['# ' + esc(task.config['creator_name']) + ' · 内容研究', '',
             '> 本地研究报告。语义内容由署名 Agent 审阅；程序验证证据完整性与审阅声明。', '',
             '## 研究问题', '', esc(analysis['question']), '', '## 概览', '', esc(analysis['summary']), '',
             f"采集窗口：{capture['started_at']} — {capture['completed_at']}", '',
             f"实际样本 {len(works)} / 目标 {capture['requested']}；视频 {capture['counts']['video']}，图文 {capture['counts']['image']}；确认不足 {capture['shortfall']}。", '',
             esc(capture['sampling_note']), '',
             '评论为有限分页快照，不能据此声称完整覆盖或代表全部受众。', '',
             '## 局限与未解决问题', '']
    lines.extend('- ' + esc(x) for x in analysis['limitations'])
    labels = {'fact': '事实', 'author_statement': '作者自述', 'inference': '推断', 'recommendation': '建议'}
    for section in analysis['sections']:
        lines += ['', '## ' + section['title'], '']
        for item in section['insights']:
            lines += [f"- **{labels[item['kind']]}**：{esc(item['text'])}",
                      '  证据：' + '、'.join('`' + esc(x) + '`' for x in item['evidence']), '']
    lines += ['', '## 作品证据与审阅', '']
    for work in works:
        wid = work['id']
        review = task.get(f'research/reviews/{wid}.json')
        comments = task.get(work['comments_file'])
        lines += ['### ' + esc(work['title'] or wid), '', f"作品 ID：`{wid}`；类型：{work['type']}", '',
                  f"原始链接：{work['source_url']}", '', esc(review['summary']), '',
                  f"已保存评论 {len(comments['rows'])} 条；平台总数 {comments['coverage']['total'] if comments['coverage']['total'] is not None else '未知'}。", '']
        if work['type'] == 'image':
            lines += ['图文逐张分析（Agent 描述，不是作者逐字原文）：', '']
            for row in review['images']:
                lines += [f"- 图 {row['number']:02d}：{esc(row['visual_description'])}",
                          f"  分析：{esc(row['interpretation'])}", f"  图片文字：{esc(row['ocr_text']) if row['ocr_text'] else '无／未识别文字，见审阅疑点'}", '']
        else:
            lines += ['校订逐字稿：', '']
            for row in review['segments']:
                lines += [f"[{row['start_ms']/1000:g}–{row['end_ms']/1000:g}s] {esc(row['corrected'])}", '']
                if row['corrected'] != row['original']:
                    lines += [f"修改理由：{esc(row['reason'])}；原稿：{esc(row['original'])}", '']
        lines += ['审阅者：' + esc(review['reviewed_by']) + '；时间：' + esc(review['reviewed_at']), '']
        lines.extend('- 疑点：' + esc(x) for x in review['doubts'])
    text = '\n'.join(lines) + '\n'
    # Render the safe Markdown subset ourselves; no scripts, remote media or raw HTML execution.
    blocks = []
    for line in lines:
        if not line:
            continue
        clean = re.sub(r'\\([\\`*_{}\[\]()#+.!|>-])', r'\1', line)
        if clean.startswith('### '):
            blocks.append('<h3>' + clean[4:] + '</h3>')
        elif clean.startswith('## '):
            blocks.append('<h2>' + clean[3:] + '</h2>')
        elif clean.startswith('# '):
            blocks.append('<h1>' + clean[2:] + '</h1>')
        else:
            blocks.append('<p>' + clean + '</p>')
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CreatorLens 本地报告</title><style>body{max-width:900px;margin:48px auto;padding:0 24px;font:17px/1.8 system-ui;color:#243449;background:#fafbfc}h1,h2,h3{line-height:1.4}h2{margin-top:2em;border-bottom:1px solid #dce3ed;padding-bottom:.4em}p{overflow-wrap:anywhere}</style><main>' + ''.join(blocks) + '</main></html>'
    write_text(task.p('report/report.md'), text)
    write_text(task.p('report/report.html'), page)
    receipt = {**checked, 'created_at': now(), 'files': {name: file_hash(task.p('report/' + name)) for name in ('report.md', 'report.html')}}
    task.save('report/receipt.json', receipt)
    return {**checked, 'markdown': str(task.p('report/report.md')), 'html': str(task.p('report/report.html'))}


def report_status(task):
    result = validate(task)
    receipt = task.get('report/receipt.json', {})
    ensure(receipt.get('research_hash') == result['research_hash'], 'report_missing_or_stale')
    ensure(set(receipt.get('files', {})) == {'report.md', 'report.html'}, 'report_manifest_invalid')
    for name, checksum in receipt['files'].items():
        ensure(file_hash(task.p('report/' + name)) == checksum, 'report_file_changed')
    return result
