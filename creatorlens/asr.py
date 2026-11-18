"""Optional Doubao ASR. Persist request identity BEFORE any paid submission."""
import base64
import json
import os
import subprocess
import urllib.request
from urllib.parse import urlparse
import uuid

from .collection import verify
from .research import transcript
from .storage import StopCapture, digest, ensure, file_hash, write_text
from .tasks import now
from .workspace import load_settings

ENDPOINT = 'https://openspeech.bytedance.com/api/v3/auc/bigmodel/'
RESOURCE = 'volc.seedasr.auc'
OPTIONS = {'model_name': 'bigmodel', 'enable_itn': True, 'enable_punc': True,
           'enable_ddc': False, 'show_utterances': True}


def key(root):
    config = load_settings(root)['asr']
    ensure(config['enabled'], 'asr_disabled_in_workspace')
    secret = os.environ.get(config['key_env'], '').strip()
    if not secret and config['key_file']:
        from pathlib import Path
        try:
            for line in Path(config['key_file']).expanduser().read_text().splitlines():
                name, sep, value = line.partition('=')
                if sep and name.strip() == config['key_env']:
                    secret = value.strip().strip('\"').strip("'")
                    break
        except OSError:
            raise StopCapture('asr_key_file_unreadable') from None
    ensure(secret and '\n' not in secret and '\r' not in secret, 'asr_key_missing')
    return secret


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post(root, action, payload, task_id):
    ensure(action in ('submit', 'query'), 'invalid_asr_action')
    request = urllib.request.Request(ENDPOINT + action, data=json.dumps(payload).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'X-Api-Key': key(root), 'X-Api-Resource-Id': RESOURCE,
        'X-Api-Request-Id': task_id, 'X-Api-Sequence': '-1'})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=180) as response:
            return response.headers.get('X-Api-Status-Code'), json.loads(response.read())
    except Exception:
        raise StopCapture('asr_network_or_response_error_no_automatic_resubmit') from None


def context(task, work_id):
    verify(task)
    ensure(work_id in [w['id'] for w in task.get('capture.json')['selected']], 'work_not_selected')
    work = task.get(f'works/{work_id}/work.json')
    ensure(work['type'] == 'video', 'asr_requires_video')
    return work, f'processing/asr/{work_id}'


def extract(task, work, folder):
    audio = task.p(folder + '/audio.mp3')
    record = task.get(folder + '/audio.json')
    if record:
        ensure(record.get('video_hash') == work['video']['sha256'] and audio.is_file()
               and record.get('sha256') == file_hash(audio), 'asr_audio_changed')
        return audio
    audio.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = task.p(folder + '/audio.partial.mp3')
    try:
        proc = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', str(task.p(work['video']['path'])),
                               '-vn', '-ac', '1', '-ar', '16000', '-codec:a', 'libmp3lame', '-b:a', '64k', str(partial)],
                              capture_output=True, timeout=300)
        ensure(proc.returncode == 0 and partial.is_file() and partial.stat().st_size > 0, 'audio_extract_failed')
    except (OSError, subprocess.TimeoutExpired):
        raise StopCapture('audio_extract_failed') from None
    partial.chmod(0o600)
    partial.replace(audio)
    task.save(folder + '/audio.json', {'video_hash': work['video']['sha256'], 'sha256': file_hash(audio)})
    return audio


def export(task, work_id, folder, job, payload):
    result = payload.get('result')
    parts = result if isinstance(result, list) else [result]
    ensure(parts and all(isinstance(p, dict) for p in parts), 'asr_result_schema_invalid')
    text = '\n'.join(p.get('text', '') for p in parts)
    segments = [{'start_ms': s.get('start_time'), 'end_ms': s.get('end_time'), 'text': s.get('text')}
                for p in parts for s in p.get('utterances', [])]
    data = transcript(task, work_id, {'provenance': 'doubao:' + job['request_id'], 'text': text, 'segments': segments})
    target = f'research/transcripts/{work_id}.json'
    existing = task.get(target)
    ensure(existing is None or existing == data, 'transcript_exists_do_not_overwrite_evidence')
    task.save(folder + '/raw-result.json', payload)
    task.save(target, data)
    write_text(task.p(folder + '/transcript.txt'), text + '\n')
    job.update(status='completed', completed_at=now(), result_hash=digest(payload), transcript_hash=digest(data))
    task.save(folder + '/job.json', job)
    return {'work_id': work_id, 'status': 'completed', 'reviewed': False}


def query(task, work_id, *, transport=post):
    work, folder = context(task, work_id)
    job = task.get(folder + '/job.json', {})
    ensure(job.get('request_id') and job.get('video_hash') == work['video']['sha256'], 'asr_existing_job_required')
    if job.get('status') == 'completed':
        raw = task.get(folder + '/raw-result.json')
        data = task.get(f'research/transcripts/{work_id}.json')
        ensure(raw and digest(raw) == job.get('result_hash') and data and digest(data) == job.get('transcript_hash'),
               'asr_result_changed')
        return {'work_id': work_id, 'status': 'completed', 'reused': True, 'reviewed': False}
    raw = task.get(folder + '/raw-result.json')
    if raw is not None:
        return export(task, work_id, folder, job, raw)
    status, payload = transport(task.workspace, 'query', {}, job['request_id'])
    job.update(last_query_at=now(), query_status=status if isinstance(status, str) and status.isdigit() else 'unknown')
    if status == '20000000':
        task.save(folder + '/raw-result.json', payload)
        return export(task, work_id, folder, job, payload)
    job['status'] = 'pending' if status in ('20000001', '20000002') else 'query_unresolved'
    task.save(folder + '/job.json', job)
    ensure(job['status'] == 'pending', 'asr_query_unresolved_preserve_request_id_do_not_resubmit')
    return {'work_id': work_id, 'status': 'pending', 'retry_query_after_seconds': 5, 'resubmitted': False}


def submit(task, work_id, *, allow_paid=False, local_audio=False, audio_url=None, transport=post, extractor=extract):
    ensure(allow_paid, 'explicit_paid_asr_authorization_required')
    work, folder = context(task, work_id)
    if task.get(folder + '/job.json'):
        # Existing request IDs are reconciled, regardless of uncertain submission status.
        return query(task, work_id, transport=transport)
    ensure(task.get(f'research/transcripts/{work_id}.json') is None, 'transcript_already_available_no_paid_submission')
    key(task.workspace)  # Check configuration before extraction and submission checkpoints.
    ensure(bool(local_audio) != bool(audio_url), 'choose_local_audio_or_audio_url')
    if audio_url:
        parsed = urlparse(audio_url)
        ensure(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password,
               'asr_audio_url_requires_https')
        audio = {'format': 'mp3', 'url': audio_url}
        input_hash = digest(audio_url)
    else:
        path = extractor(task, work, folder)
        ensure(path.stat().st_size <= 32 * 1024 * 1024, 'inline_asr_audio_too_large')
        audio = {'format': 'mp3', 'data': base64.b64encode(path.read_bytes()).decode()}
        input_hash = file_hash(path)
    job = {'schema_version': 1, 'work_id': work_id, 'request_id': str(uuid.uuid4()),
           'provider': 'doubao', 'resource': RESOURCE, 'video_hash': work['video']['sha256'],
           'input_hash': input_hash, 'input_mode': 'local_base64_compatibility' if local_audio else 'https_url',
           'options': OPTIONS, 'paid_authorized_at': now(), 'status': 'submitting'}
    task.save(folder + '/job.json', job)
    try:
        status, _ = transport(task.workspace, 'submit', {'user': {'uid': 'creatorlens'}, 'audio': audio, 'request': OPTIONS}, job['request_id'])
    except BaseException:
        job['status'] = 'submit_uncertain'
        task.save(folder + '/job.json', job)
        raise StopCapture('asr_submit_uncertain_query_existing_id_only') from None
    job.update(status='submitted' if status == '20000000' else 'submit_unresolved',
               submit_status=status if isinstance(status, str) and status.isdigit() else 'unknown')
    task.save(folder + '/job.json', job)
    ensure(status == '20000000', 'asr_submit_unresolved_query_existing_id_only')
    return {'work_id': work_id, 'status': 'submitted', 'next_command': 'asr-query', 'reviewed': False}
