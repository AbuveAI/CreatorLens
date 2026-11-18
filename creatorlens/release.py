"""Explicit-file source export; never archive the working tree recursively."""
import json
from pathlib import Path
import re
import zipfile
from . import __version__
from .storage import digest, ensure, file_hash, read, safe_path, write
from .workspace import SOURCE_ROOT


def package(root, source=SOURCE_ROOT):
    source = Path(source)
    names = read(source / 'release-files.json')
    ensure(isinstance(names, list) and names and len(names) == len(set(names)), 'release_whitelist_invalid')
    files = {}
    for name in names:
        ensure(isinstance(name, str) and not any(p in ('.git', '.venv', 'vendor', 'dependencies', 'private', 'data', 'tmp')
                                               for p in Path(name).parts), 'private_release_path_forbidden')
        path = safe_path(source, name)
        ensure(path.is_file(), 'release_file_missing')
        content = path.read_text(encoding='utf-8')
        personal = '/' + 'Users/'
        ensure(personal not in content and not re.search(r'https://[^\s]+\.(?:feishu|larksuite)\.cn/(?:base|wiki)/[A-Za-z0-9]{15,}', content)
               and not re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', content), 'release_contains_private_material_review_required')
        files[name] = file_hash(path)
    directory = safe_path(root, 'reports/releases')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = safe_path(root, f'reports/releases/creatorlens-{__version__}.zip')
    temporary = safe_path(root, f'reports/releases/creatorlens-{__version__}.partial.zip')
    manifest = {'version': __version__, 'files': files, 'manifest_hash': digest(files),
                'third_party_source_included': False, 'private_workspace_included': False}
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(source / name, 'creatorlens/' + name)
        archive.writestr('creatorlens/RELEASE-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(target)
    write(directory / f'creatorlens-{__version__}.manifest.json', {**manifest, 'zip_sha256': file_hash(target)})
    return {'package': str(target), 'file_count': len(files), 'sha256': file_hash(target), 'published': False}
