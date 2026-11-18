"""Entirely authored offline demo. No platform requests and no real creator data."""
import struct
import zlib
from .collection import collect
from .research import prepare, report
from .storage import ensure
from .tasks import create, now

ACCOUNT = 'd' * 24


def png(color):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data) & 0xffffffff)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 32, 32, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress((b'\0' + bytes(color) * 32) * 32)) + chunk(b'IEND', b'')


class DemoProvider:
    def profile(self, _):
        return {'success': True, 'data': {'synthetic': True}}

    def page(self, *_):
        return {'success': True, 'data': {'has_more': False, 'notes': [
            {'note_id': f'{i:024x}', 'time': i * 1000, 'type': 'normal', 'xsec_token': 'synthetic'} for i in (1, 2)]}}

    def detail(self, wid, _):
        return {'synthetic': True, 'id': wid}

    def describe(self, raw, wid, account):
        return {'id': wid, 'author_id': account, 'type': 'image', 'published_ms': int(wid, 16) * 1000,
                'title': '自制色块示例 ' + str(int(wid, 16)), 'body': '仅用于离线功能验证，无真实账号或商业结论。',
                'metrics': {'comment_count': 0}, 'image_urls': ['demo:red', 'demo:blue'] if int(wid, 16) == 2 else ['demo:red']}

    def comments(self, *_):
        return {'success': True, 'data': {'comments': [], 'has_more': False}}

    def download(self, url, path):
        path.write_bytes(png((228, 80, 80) if url == 'demo:red' else (75, 120, 235)))

    def inspect(self, path, kind):
        ensure(kind == 'image' and path.read_bytes().startswith(b'\x89PNG'), 'demo_image_invalid')
        return {'width': 32, 'height': 32, 'format': 'PNG', 'extension': '.png'}


def run(root, batch='offline-demo'):
    task = create(root, url='https://www.xiaohongshu.com/user/profile/' + ACCOUNT,
                  name='离线自制样本（非真实研究）', batch=batch, count=2)
    with task.lock():
        task.save('runtime.json', {'provider': 'synthetic_demo', 'network_used': False})
        collect(task, DemoProvider())
        prepare(task)
        for selected in task.get('capture.json')['selected']:
            wid = selected['id']
            path = f'research/reviews/{wid}.json'
            review = task.get(path)
            review.update(status='reviewed', reviewed_by='CreatorLens 自制样本生成器', reviewed_at=now(),
                          comments_read=True, summary='该样本由程序生成色块图片，只验证文件链路。')
            for row in review['images']:
                row.update(viewed=True, visual_description='32×32 的单色方块；颜色由示例生成器定义。',
                           interpretation='自制测试图片，不据此推断创作者风格或受众。', ocr_text='')
            task.save(path, review)
        analysis = task.get('research/analysis.json')
        analysis.update(status='reviewed', reviewed_by='CreatorLens 自制样本生成器', reviewed_at=now(),
                        question='空工作区能否生成一份可追溯的本地报告？',
                        summary='两个自制图文样本包含三张色块图。此报告是软件演示，不是实际内容研究。',
                        limitations=['没有访问平台，没有真实评论、视频、ASR或商业分析；本示例不能证明线上接口有效。'])
        for section in analysis['sections']:
            section['insights'] = [{'kind': 'fact', 'text': '这一节只演示证据引用；没有生成真实创作者结论。',
                                    'evidence': [f"{task.get('capture.json')['selected'][0]['id']}:image:1"]}]
        task.save('research/analysis.json', analysis)
        result = report(task)
    return {**result, 'task': str(task.path), 'synthetic': True, 'network_used': False}
