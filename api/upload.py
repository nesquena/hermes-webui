"""
Hermes Web UI -- File upload: multipart parser and upload handler.
"""
import mimetypes
import re as _re
import email.parser
import tempfile
from pathlib import Path

from api.config import MAX_UPLOAD_BYTES
from api.helpers import j, bad
from api.models import get_session
from api.workspace import safe_resolve_ws


def parse_multipart(rfile, content_type, content_length) -> tuple:
    import re as _re, email.parser as _ep
    m = _re.search(r'boundary=([^;\s]+)', content_type)
    if not m:
        raise ValueError('No boundary in Content-Type')
    boundary = m.group(1).strip('"').encode()
    raw = rfile.read(content_length)
    fields = {}
    files = {}
    delimiter = b'--' + boundary
    end_marker = b'--' + boundary + b'--'
    parts = raw.split(delimiter)
    for part in parts[1:]:
        stripped = part.lstrip(b'\r\n')
        if stripped.startswith(b'--'):
            break
        sep = b'\r\n\r\n' if b'\r\n\r\n' in part else b'\n\n'
        if sep not in part:
            continue
        header_raw, body = part.split(sep, 1)
        if body.endswith(b'\r\n'):
            body = body[:-2]
        elif body.endswith(b'\n'):
            body = body[:-1]
        header_text = header_raw.lstrip(b'\r\n').decode('utf-8', errors='replace')
        msg = _ep.HeaderParser().parsestr(header_text)
        disp = msg.get('Content-Disposition', '')
        name_m = _re.search(r'name="([^"]*)"', disp)
        file_m = _re.search(r'filename="([^"]*)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        if file_m:
            files[name] = (file_m.group(1), body)
        else:
            fields[name] = body.decode('utf-8', errors='replace')
    return fields, files


def _sanitize_upload_name(filename: str) -> str:
    safe_name = _re.sub(r'[^\w.\-]', '_', Path(filename).name)[:200]
    if not safe_name or safe_name.strip('.') == '':
        raise ValueError('Invalid filename')
    return safe_name


def handle_upload(handler):
    import traceback as _tb
    try:
        content_type = handler.headers.get('Content-Type', '')
        content_length = int(handler.headers.get('Content-Length', 0) or 0)
        if content_length > MAX_UPLOAD_BYTES:
            return j(handler, {'error': f'File too large (max {MAX_UPLOAD_BYTES//1024//1024}MB)'}, status=413)
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
        session_id = fields.get('session_id', '')
        if 'file' not in files:
            return j(handler, {'error': 'No file field in request'}, status=400)
        filename, file_bytes = files['file']
        if not filename:
            return j(handler, {'error': 'No filename in upload'}, status=400)
        try:
            s = get_session(session_id)
        except KeyError:
            return j(handler, {'error': 'Session not found'}, status=404)
        workspace = Path(s.workspace)
        safe_name = _sanitize_upload_name(filename)
        dest = safe_resolve_ws(workspace, safe_name)
        dest.write_bytes(file_bytes)
        mime = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
        return j(handler, {
            'filename': safe_name,
            'path': str(dest),
            'size': dest.stat().st_size,
            'mime': mime,
            'is_image': mime.startswith('image/'),
        })
    except ValueError as e:
        return j(handler, {'error': str(e)}, status=400)
    except Exception:
        print('[webui] upload error: ' + _tb.format_exc(), flush=True)
        return j(handler, {'error': 'Upload failed'}, status=500)


# Maximum total extracted bytes — guards against zip/tar bombs.
# Set to 10x the upload limit; a legitimate archive rarely exceeds 3-4x.
_MAX_EXTRACTED_BYTES = 10 * 20 * 1024 * 1024  # 200 MB


def extract_archive(file_bytes: bytes, filename: str, workspace: Path):
    """Extract a zip or tar archive into the workspace.

    Returns a dict with ``extracted`` (int), ``files`` (list[str]).
    Raises ValueError on zip-slip or unsupported format.
    """
    import zipfile, tarfile, io, os, shutil

    name = Path(filename).name
    stem = Path(filename).stem  # strip .zip / .tar.gz etc.

    if name.lower().endswith(('.zip',)):
        _mode = 'zip'
    elif name.lower().endswith(('.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2', '.tar.xz', '.txz')):
        _mode = 'tar'
    else:
        raise ValueError(f'Unsupported archive format: {filename}')

    # Determine destination directory — use archive stem as folder name
    dest_dir = safe_resolve_ws(workspace, stem)
    # Avoid overwriting existing files by appending a suffix
    if dest_dir.exists():
        import string, random
        while dest_dir.exists():
            suffix = ''.join(random.choices(string.digits, k=3))
            dest_dir = dest_dir.with_name(stem + '_' + suffix)
    dest_dir.mkdir(parents=True, exist_ok=True)

    extracted_files = []
    total_extracted = 0

    try:
        if _mode == 'zip':
            with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
                for member in zf.infolist():
                    # Skip directories
                    if member.is_dir():
                        continue
                    # Zip-slip protection
                    member_path = (dest_dir / member.filename).resolve()
                    if not member_path.is_relative_to(dest_dir.resolve()):
                        raise ValueError(f'Zip-slip blocked: {member.filename}')
                    # Zip-bomb protection: track actual extracted bytes (not declared file_size)
                    if total_extracted > _MAX_EXTRACTED_BYTES:
                        raise ValueError(
                            f'Extraction too large ({total_extracted // (1024*1024)} MB > '
                            f'{_MAX_EXTRACTED_BYTES // (1024*1024)} MB limit). '
                            f'Possible zip bomb.'
                        )
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(member_path, 'wb') as dst:
                        _chunk_size = 65536
                        while True:
                            chunk = src.read(_chunk_size)
                            if not chunk:
                                break
                            total_extracted += len(chunk)
                            if total_extracted > _MAX_EXTRACTED_BYTES:
                                raise ValueError(
                                    f'Extraction too large (> '
                                    f'{_MAX_EXTRACTED_BYTES // (1024*1024)} MB limit). '
                                    f'Possible zip bomb.'
                                )
                            dst.write(chunk)
                    extracted_files.append(str(member_path.relative_to(workspace.resolve())))

        elif _mode == 'tar':
            with tarfile.open(fileobj=io.BytesIO(file_bytes)) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    # Tar-slip protection
                    member_path = (dest_dir / member.name).resolve()
                    if not member_path.is_relative_to(dest_dir.resolve()):
                        raise ValueError(f'Tar-slip blocked: {member.name}')
                    # Tar-bomb protection: track actual extracted bytes (not declared size)
                    if total_extracted > _MAX_EXTRACTED_BYTES:
                        raise ValueError(
                            f'Extraction too large ({total_extracted // (1024*1024)} MB > '
                            f'{_MAX_EXTRACTED_BYTES // (1024*1024)} MB limit). '
                            f'Possible zip bomb.'
                        )
                    member_path.parent.mkdir(parents=True, exist_ok=True)
                    src_obj = tf.extractfile(member)
                    if src_obj:
                        with src_obj as src, open(member_path, 'wb') as dst:
                            _chunk_size = 65536
                            while True:
                                chunk = src.read(_chunk_size)
                                if not chunk:
                                    break
                                total_extracted += len(chunk)
                                if total_extracted > _MAX_EXTRACTED_BYTES:
                                    raise ValueError(
                                        f'Extraction too large (> '
                                        f'{_MAX_EXTRACTED_BYTES // (1024*1024)} MB limit). '
                                        f'Possible zip bomb.'
                                    )
                                dst.write(chunk)
                    extracted_files.append(str(member_path.relative_to(workspace.resolve())))
    except Exception:
        # Clean up partially-extracted directory to avoid orphaned folders
        try:
            shutil.rmtree(dest_dir, ignore_errors=True)
        except Exception:
            pass
        raise

    return {'extracted': len(extracted_files), 'files': extracted_files, 'dest': str(dest_dir)}


def handle_upload_extract(handler):
    """Handle archive upload and extraction."""
    import traceback as _tb
    try:
        content_type = handler.headers.get('Content-Type', '')
        content_length = int(handler.headers.get('Content-Length', 0) or 0)
        if content_length > MAX_UPLOAD_BYTES:
            return j(handler, {'error': f'File too large (max {MAX_UPLOAD_BYTES//1024//1024}MB)'}, status=413)
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
        session_id = fields.get('session_id', '')
        if 'file' not in files:
            return j(handler, {'error': 'No file field in request'}, status=400)
        filename, file_bytes = files['file']
        if not filename:
            return j(handler, {'error': 'No filename in upload'}, status=400)
        try:
            s = get_session(session_id)
        except KeyError:
            return j(handler, {'error': 'Session not found'}, status=404)
        workspace = Path(s.workspace)
        result = extract_archive(file_bytes, filename, workspace)
        return j(handler, {'ok': True, **result})
    except ValueError as e:
        return j(handler, {'error': str(e)}, status=400)
    except Exception:
        print('[webui] upload extract error: ' + _tb.format_exc(), flush=True)
        return j(handler, {'error': 'Archive extraction failed'}, status=500)


def handle_transcribe(handler):
    import traceback as _tb
    temp_path = None
    try:
        content_type = handler.headers.get('Content-Type', '')
        content_length = int(handler.headers.get('Content-Length', 0) or 0)
        if content_length > MAX_UPLOAD_BYTES:
            return j(handler, {'error': f'File too large (max {MAX_UPLOAD_BYTES//1024//1024}MB)'}, status=413)
        fields, files = parse_multipart(handler.rfile, content_type, content_length)
        if 'file' not in files:
            return j(handler, {'error': 'No file field in request'}, status=400)
        filename, file_bytes = files['file']
        if not filename:
            return j(handler, {'error': 'No filename in upload'}, status=400)
        safe_name = _sanitize_upload_name(filename)
        suffix = Path(safe_name).suffix or '.webm'
        with tempfile.NamedTemporaryFile(prefix='webui-stt-', suffix=suffix, delete=False) as tmp:
            temp_path = tmp.name
            tmp.write(file_bytes)
        try:
            from tools.transcription_tools import transcribe_audio
        except ImportError:
            return j(handler, {'error': 'Speech-to-text is unavailable on this server'}, status=503)
        result = transcribe_audio(temp_path)
        if not result.get('success'):
            msg = str(result.get('error') or 'Transcription failed')
            status = 503 if 'unavailable' in msg.lower() or 'not configured' in msg.lower() else 400
            return j(handler, {'error': msg}, status=status)
        transcript = str(result.get('transcript') or '').strip()
        return j(handler, {'ok': True, 'transcript': transcript})
    except ValueError as e:
        return j(handler, {'error': str(e)}, status=400)
    except Exception:
        print('[webui] transcribe error: ' + _tb.format_exc(), flush=True)
        return j(handler, {'error': 'Transcription failed'}, status=500)
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass
