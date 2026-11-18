"""Optional Agent handoff for Feishu: local allowlisted payloads, no cloud writes."""
from .research import report_status
from .storage import digest, ensure


def plan(task):
    checked = report_status(task)
    capture = task.get('capture.json')
    records, attachments = [], []
    for selected in capture['selected']:
        wid = selected['id']
        work = task.get(f'works/{wid}/work.json')
        review = task.get(f'research/reviews/{wid}.json')
        stable = f"xiaohongshu:{task.config['account_id']}:{task.config['batch_id']}:{wid}"
        record = {'key': stable, 'work_id': wid, 'account_id': task.config['account_id'],
                  'batch_id': task.config['batch_id'], 'title': work['title'], 'type': work['type'],
                  'source_url': work['source_url'], 'published_ms': work['published_ms'],
                  'metrics': work['metrics'], 'summary': review['summary'], 'doubts': review['doubts'],
                  'comments': task.get(work['comments_file']), 'research_hash': checked['research_hash']}
        if work['type'] == 'video':
            record['transcript'] = '\n'.join(s['corrected'] for s in review['segments'])
        else:
            record['image_analysis'] = review['images']
            record['image_analysis_kind'] = 'Agent逐图描述与分析，非ASR、非作者逐字原文'
        records.append(record)
        candidates = [('cover', 1, work['cover'])]
        if work['type'] == 'image':
            candidates += [('original_images', i, asset) for i, asset in enumerate(work['images'], 1)]
        for field, number, media in candidates:
            ensure(media['kind'] == 'image', 'only_image_attachments_allowed')
            attachments.append({'key': stable, 'field': field, 'number': number,
                                'path': media['path'], 'sha256': media['sha256'], 'bytes': media['bytes']})
    result = {'schema_version': 1, 'research_hash': checked['research_hash'],
              'target': None, 'records': records, 'attachments': attachments,
              'report_file': 'report/report.md', 'cloud_written': False,
              'instructions': 'Agent先确认用户目标及权限，读取实际表结构，按key查重后写入；封面及原图定向上传，禁止上传视频。写入不确定先查记录，逐项回读后记录验收。'}
    result['plan_hash'] = digest(result)
    task.save('exports/feishu-plan.json', result)
    return {'plan': str(task.p('exports/feishu-plan.json')), 'records': len(records),
            'image_attachments': len(attachments), 'cloud_written': False}
