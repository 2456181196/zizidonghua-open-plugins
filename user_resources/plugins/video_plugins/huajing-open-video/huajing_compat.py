# -*- coding: utf-8 -*-
"""画境字字动画生成插件。仅使用画境公开接口和宿主公开契约。"""
import base64
import contextlib
import hashlib
import json
import math
import mimetypes
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
import requests

VERSION = '2.0.2'
BASE = 'https://api.huajingart.cn/v1'
SITE = 'https://api.huajingart.cn'
TERMINAL = {'succeeded', 'success', 'completed', 'done'}
LOCK = threading.RLock()


class PluginError(Exception):
    pass


def fail(message, uncertain=False):
    error = PluginError('PLUGIN_ERROR:::' + str(message))
    error.uncertain = uncertain
    raise error


def safe_error(body, status=0):
    if status >= 500:
        return '生成服务暂时异常，请稍后查询原任务'
    error = body.get('error', {}) if isinstance(body, dict) else {}
    value = error.get('message', error.get('code', '')) if isinstance(error, dict) else str(error)
    value = value or ('请求返回 HTTP %s，请稍后查询原任务' % status)
    value = re.sub(r'https?://\S+|Bearer\s+\S+|mk-[A-Za-z0-9_-]+', '[已隐藏]', value, flags=re.I)
    return value[:350]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with open(str(temp), 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        try:
            os.chmod(str(temp), 0o600)
        except OSError:
            pass
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def unwrap(value):
    if isinstance(value, dict):
        for key in ('path', 'file_path', 'local_path', 'image_path', 'url', 'src'):
            if value.get(key):
                return unwrap(value[key])
        return ''
    return str(value or '').strip()


def refs(value, nested=None):
    if isinstance(value, dict):
        if nested and nested in value:
            return refs(value[nested])
        ordered = sorted(value, key=lambda k: (0, int(k)) if str(k).isdigit() else (1, str(k)))
        return [unwrap(value[k]) for k in ordered if unwrap(value[k])]
    if isinstance(value, (list, tuple)):
        return [unwrap(v) for v in value if unwrap(v)]
    return [unwrap(value)] if unwrap(value) else []


def distinct(values):
    return list(dict.fromkeys(v for v in values if v))


def name_part(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(value)).strip('. ')
    return value[:100] or 'result'


class Runtime:
    def __init__(self, plugin_file, kind):
        self.file = Path(plugin_file).resolve()
        self.kind = kind
        self.title = '画境视频插件' if kind == 'video' else '画境图像插件'
        self.root = Path(os.environ.get('HUAJING_PLUGIN_DATA_DIR') or Path.cwd() / 'user_resources' / 'plugins').resolve()
        self.config = self.root / (kind + '_plugins') / self.file.parent.name / 'config.json'
        self.shared = self.root / 'huajing-account.json'
        self.defaults = {'api_key': '', 'model': 'auto', 'ratio': '16:9' if kind == 'video' else '',
                         'resolution': '768p' if kind == 'video' else '2k', 'reference_mode': 'auto',
                         'duration_seconds': '5', 'duration_mode': 'fixed', 'generate_audio': False,
                         'concurrency': '2', 'poll_interval': '4', 'task_timeout': '1800'}
        self.session_factory = requests.Session
        self.sleep = time.sleep
        # Only concurrent calls share in-memory work; completed calls are not retained.
        self._active = {}
        self._active_lock = threading.RLock()

    def get_info(self):
        return {'name': self.title, 'display_name': self.title, 'version': VERSION, 'author': '画境',
                'api_version': 2, 'type': self.kind, 'images_per_batch': 1,
                'description': '填写画境 API Key，直接在字字动画生成并保存' + ('视频' if self.kind == 'video' else '图片')}

    def get_params(self):
        params = dict(self.defaults)
        local = read_json(self.config)
        # Compatible with the former image package; never migrate an unrelated endpoint's key.
        if not local:
            legacy = read_json(self.file.parent / 'config.json')
            if str(legacy.get('base_url', BASE)).rstrip('/') in (BASE, SITE):
                local = legacy
        params.update({k: v for k, v in local.items() if k in self.defaults})
        params['api_key'] = str(params.get('api_key') or read_json(self.shared).get('api_key') or '').strip()
        params['model'] = {'gpt-image': 'gpt-image-2.5', 'nano-banana': 'nano-banana-pro'}.get(params['model'], params['model'])
        return params

    def settings(self, supplied=None):
        params = self.get_params()
        params.update({k: v for k, v in (supplied or {}).items() if k in self.defaults and (v != '' or k == 'ratio')})
        params['api_key'] = str(params.get('api_key') or '').strip()
        if not params['api_key']:
            fail('请先在插件设置里填写画境 API Key')
        return params

    def api(self, method, path, params, **kwargs):
        # API credentials only go to the built-in HTTPS API origin; no redirect forwarding.
        with self.session_factory() as session:
            response = session.request(method, BASE + path, headers={'Authorization': 'Bearer ' + params['api_key'],
                                       'User-Agent': 'huajing-typetale/' + VERSION}, timeout=(15, 180 if method == 'POST' else 35),
                                       allow_redirects=False, **kwargs)
            try:
                body = response.json()
            except ValueError:
                fail('接口暂未返回有效数据，请到画境生成历史核对，避免重复提交', uncertain=method == 'POST')
            if response.status_code >= 300:
                fail(safe_error(body, response.status_code), uncertain=method == 'POST' and response.status_code >= 500)
            if not isinstance(body, dict):
                fail('接口数据格式异常，请稍后查询原任务', uncertain=method == 'POST')
            return body

    def catalog(self, params):
        listing = self.api('GET', '/models', params)
        models = listing.get('models') or listing.get('data') or []
        allowed = [m for m in models if (m.get('type') == 'video' or m.get('id') == 'minimax-h3') == (self.kind == 'video')]
        if not allowed:
            fail('这把密钥未开放%s模型，请使用不限模型且不限用途的密钥，或对应%s用途的密钥' %
                 (('视频', '视频') if self.kind == 'video' else ('图片', '图片')))
        pricing = self.api('GET', '/pricing', params)
        return {'models': allowed, 'default_model': listing.get('default_model'), 'pricing': pricing,
                'video': self.api('GET', '/videos/config', params) if self.kind == 'video' else {}}

    def connect(self, params):
        catalog = self.catalog(params)
        balance = self.api('GET', '/balance', params)
        return {'ok': True, 'credits': balance.get('credits'), 'catalog': catalog,
                'message': '连接成功；仅查询模型、参数与余额，没有创建生成任务'}


    def media(self, context, params):
        mode = str(params.get('reference_mode') or 'auto')
        if mode == 'off':
            return {}
        images = distinct(refs(context.get('reference_images'), '参考图片MAP'))
        videos = distinct(refs(context.get('reference_videos'), '参考视频MAP'))
        audios = distinct(refs(context.get('reference_audios'), '参考音频MAP'))
        first, last = unwrap(context.get('first_frame_path')), unwrap(context.get('end_frame_path'))
        if self.kind == 'image':
            return {'images': images or ([first] if first else [])}
        if mode == 'frames' or mode == 'auto' and last:
            if not first:
                fail('首尾帧模式需要设置首帧图片')
            if videos or audios:
                fail('首尾帧模式请移除视频和音频参考，或切换为普通参考模式')
            return {'first_frame': [first], **({'last_frame': [last]} if last else {})}
        images = images or ([first] if first else [])
        if audios and not images and not videos:
            fail('音频参考需要搭配图片或视频参考')
        return {'images': images, 'videos': videos, 'audios': audios}

    def file_info(self, value, role):
        if value.startswith('https://') or value.startswith('data:'):
            return None
        if value.startswith('file:'):
            value = unquote(urlparse(value).path).lstrip('/') if os.name == 'nt' else unquote(urlparse(value).path)
        path = Path(value)
        if not path.is_file():
            fail('参考素材文件不存在，请重新选择：' + path.name)
        cap = (64 if role == 'videos' else 20) * 1024 * 1024
        if not 0 < path.stat().st_size <= cap:
            fail('参考素材大小超出范围：' + path.name)
        mime = mimetypes.guess_type(str(path))[0] or ('image/png' if role in ('images', 'first_frame', 'last_frame') else 'application/octet-stream')
        return path, mime

    def plan(self, context, params, catalog):
        prompt = str(context.get('prompt') or '').strip()
        if not prompt:
            fail('请填写生成提示词')
        allowed = [m['id'] for m in catalog['models']]
        model = params.get('model')
        if model == 'auto' or not model:
            model = catalog['default_model'] if catalog['default_model'] in allowed else allowed[0]
        model = {'gpt-image': 'gpt-image-2.5', 'nano-banana': 'nano-banana-pro'}.get(model, model)
        if model not in allowed:
            fail('当前密钥未开放所选模型，请在插件中测试连接并重新选择模型')
        media = self.media(context, params)
        limits = {'images': int(catalog['pricing'].get('limits', {}).get('max_reference_images', 9)), 'videos': 0, 'audios': 0}
        body = {'prompt': prompt, 'model': model, 'n': 1, 'resolution': params['resolution']}
        if self.kind == 'image':
            if params.get('ratio'):
                body['ratio'] = str(params['ratio'])
            path = '/images/edits-async' if media.get('images') else '/images/generations-async'
        else:
            config = catalog['video']
            limits.update(config.get('reference_limits') or {'images': 9, 'videos': 3, 'audios': 3})
            limits.update({'first_frame': 1, 'last_frame': 1})
            duration_mode = params.get('duration_mode')
            if duration_mode == 'scene':
                seconds = max(5, min(15, math.ceil(float(context.get('scene_duration') or 5))))
            elif duration_mode == 'audio':
                seconds = max(5, min(15, math.ceil(float(context.get('software_audio_duration') or context.get('scene_duration') or 5))))
            else:
                seconds = float(params['duration_seconds'])
            if seconds not in config.get('durations', list(range(5, 16))):
                fail('视频时长请选择服务当前支持的5至15秒整数')
            if params['ratio'] not in config.get('aspect_ratios', ['16:9', '9:16']):
                fail('视频比例请选择16:9或9:16')
            if params['resolution'] not in config.get('resolutions', ['768p', '1080p']):
                fail('视频分辨率请选择服务支持的768p或1080p')
            advanced = bool(media.get('first_frame') or media.get('videos') or media.get('audios'))
            audio = params.get('generate_audio') in (True, 'true', 1, '1')
            if audio and not advanced:
                fail('生成声音选项适用于首尾帧、视频或音频参考模式；仅文字和图片参考请关闭')
            features = config.get('features', {})
            for role, feature in [('images', 'reference_images'), ('videos', 'reference_videos'), ('audios', 'reference_audios'), ('first_frame', 'first_frame'), ('last_frame', 'last_frame')]:
                if media.get(role) and features.get(feature) is False:
                    fail('所选参考类型当前暂未开放，请调整参考素材')
            if audio and features.get('generate_audio') is False:
                fail('生成声音选项当前暂未开放')
            body.pop('n')
            body.update(duration_seconds=int(seconds), aspect_ratio=params['ratio'], input_mode='frames' if media.get('first_frame') else 'references',
                        enable_video_reference=bool(media.get('videos')), enable_audio_reference=bool(media.get('audios')), generate_audio=audio)
            path = '/videos/generations'
        family = context.get('model_family') or {}
        family_limits = family.get('limits') or {}
        count = sum(len(items) for items in media.values())
        if self.kind == 'video' and count > int(limits.get('total', 12)):
            fail('视频参考素材合计最多12个')
        total = 0
        for role, items in media.items():
            cap = min(int(limits.get(role, 0)), int(family_limits.get(role) or limits.get(role, 0)))
            if len(items) > cap:
                fail('参考素材数量超过当前模型或账户上限：' + role)
            for item in items:
                info = self.file_info(item, role)
                if info:
                    total += info[0].stat().st_size
        if total > 300 * 1024 * 1024:
            fail('参考素材合计最多300MiB')
        return path, body, media

    @contextlib.contextmanager
    def request_data(self, body, media):
        fields = {'images': 'reference_images' if self.kind == 'video' else 'images', 'videos': 'reference_videos', 'audios': 'reference_audios', 'first_frame': 'first_frame', 'last_frame': 'last_frame'}
        all_local = all(self.file_info(item, role) is not None for role, values in media.items() for item in values)
        if not any(media.values()):
            yield {'json': body}
        elif all_local:
            with contextlib.ExitStack() as stack:
                files = []
                for role, values in media.items():
                    for value in values:
                        path, mime = self.file_info(value, role)
                        files.append((fields[role], (path.name, stack.enter_context(open(str(path), 'rb')), mime)))
                yield {'data': {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in body.items()}, 'files': files}
        else:
            payload = dict(body)
            for role, values in media.items():
                encoded = []
                for value in values:
                    info = self.file_info(value, role)
                    if info:
                        value = 'data:' + info[1] + ';base64,' + base64.b64encode(info[0].read_bytes()).decode('ascii')
                    encoded.append(value)
                if encoded:
                    payload[fields[role]] = encoded[0] if role.endswith('_frame') else encoded
            yield {'json': payload}

    def output_slot(self, context, params, plan, position):
        directory = Path(context.get('project_path') or context.get('output_dir') or '').resolve()
        if not context.get('project_path') and not context.get('output_dir'):
            fail('宿主未提供项目输出目录，请先打开作品')
        directory.mkdir(parents=True, exist_ok=True)
        filename = '%04d_%s_%d_%s' % (int(context.get('viewer_index') or 0), name_part(context.get('unique_name') or 'result'), int(context.get('generation_round') or 0), name_part(position))
        owner = hashlib.sha256(params['api_key'].encode()).hexdigest()
        identity = [owner, self.kind, str(directory), filename, plan]
        stamps = []
        for role, values in plan[2].items():
            for value in values:
                info = self.file_info(value, role)
                if info:
                    stat = info[0].stat(); stamps.append([str(info[0].resolve()), stat.st_size, stat.st_mtime_ns])
        ident = hashlib.sha256(json.dumps([identity, stamps], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return ident, str(directory / filename)

    def download(self, url, output, params):
        original = urljoin(SITE + '/', str(url))
        if urlparse(original).scheme != 'https' or urlparse(original).username:
            fail('成品下载地址格式异常，请查询原任务')
        for attempt in range(3):
            temp = Path(output + '.' + uuid.uuid4().hex + '.part')
            try:
                with self.session_factory() as session:
                    current = original
                    for hop in range(6):
                        parsed = urlparse(current)
                        if parsed.scheme != 'https' or parsed.username:
                            fail('成品跳转地址格式异常')
                        headers = {'Authorization': 'Bearer ' + params['api_key']} if parsed.netloc == urlparse(SITE).netloc else {}
                        response = session.get(current, headers=headers, stream=True, allow_redirects=False, timeout=(15, 90))
                        if response.status_code in (301, 302, 303, 307, 308):
                            current = urljoin(current, response.headers.get('Location', '')); response.close(); continue
                        break
                    else:
                        fail('成品下载跳转过多，请到画境生成历史下载成品')
                    if response.status_code != 200:
                        raise requests.RequestException('download status')
                    size, head = 0, b''
                    with open(str(temp), 'wb') as handle:
                        for chunk in response.iter_content(128 * 1024):
                            if not chunk: continue
                            size += len(chunk)
                            if size > (1024 if self.kind == 'video' else 80) * 1024 * 1024:
                                fail('下载文件超出插件大小上限')
                            head = (head + chunk)[:64]; handle.write(chunk)
                    expected = response.headers.get('Content-Length')
                    if expected and not response.headers.get('Content-Encoding') and int(expected) != size:
                        raise requests.RequestException('partial download')
                    response.close()
                if self.kind == 'video':
                    ext = 'mp4' if len(head) > 12 and head[4:8] == b'ftyp' else None
                else:
                    ext = 'png' if head.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if head.startswith(b'\xff\xd8\xff') else 'webp' if head[:4] == b'RIFF' and head[8:12] == b'WEBP' else 'gif' if head[:6] in (b'GIF87a', b'GIF89a') else None
                if not ext or size < 16:
                    raise requests.RequestException('invalid media')
                final = Path(output + '.' + ext); os.replace(str(temp), str(final)); return str(final.resolve())
            except (requests.RequestException, OSError, ValueError):
                if attempt == 2:
                    fail('成品已生成，下载暂时中断。请到画境生成历史下载，无需再次生成')
                self.sleep(2 + attempt)
            finally:
                if temp.exists(): temp.unlink()

    def await_result(self, task_id, output, params, report):
        interval = max(2, min(30, float(params.get('poll_interval', 4))))
        timeout = max(60, min(7200, float(params.get('task_timeout', 1800))))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                task = self.api('GET', ('/videos/tasks/' if self.kind == 'video' else '/images/tasks/') + task_id, params)
            except requests.RequestException:
                report('正在重新获取任务状态'); self.sleep(interval); continue
            status = task.get('status')
            if status in ('failed', 'cancelled', 'canceled', 'expired'):
                fail(safe_error(task) if task.get('error') else '生成任务已失败或结束')
            if status in TERMINAL:
                urls = [task.get('video_url')] if self.kind == 'video' else task.get('images') or [item.get('url') for item in task.get('data', [])]
                if not urls or not urls[0]:
                    report('正在确认成品'); self.sleep(interval); continue
                report('成品已就绪，正在下载')
                file = self.download(urls[0], output, params)
                report(task.get('warning_message') or '已完成并保存到作品', 100)
                return file
            report({'queued': '正在排队', 'running': '正在生成', 'in_progress': '正在生成'}.get(status, '正在查询任务状态'))
            self.sleep(interval)
        fail('等待已结束，请到画境生成历史查看结果，确认原任务状态后再操作，避免重复扣费')

    def generate(self, context):
        params = self.settings(context.get('plugin_params'))
        callback = context.get('progress_callback')
        def report(message, percent=None):
            if not callback: return
            try:
                if percent is None: callback(message)
                else: callback(message, percent)
            except TypeError:
                callback(message)
        report('正在读取画境模型与参数')
        catalog = self.catalog(params); plan = self.plan(context, params, catalog)
        positions = context.get('output_position') or [0]
        if not isinstance(positions, list): positions = [positions]
        if len(set(map(str, positions))) != len(positions): fail('宿主输出槽位重复，请重新发起')
        def one(position):
            ident, output = self.output_slot(context, params, plan, position)
            with self._active_lock:
                pending = self._active.get(ident)
                made = pending is None
                if made:
                    pending = Future(); self._active[ident] = pending
            if not made:
                return pending.result()
            try:
                report('正在提交画境任务')
                with self.request_data(plan[1], plan[2]) as request:
                    task = self.api('POST', plan[0], params, **request)
                task_id = task.get('id') or task.get('task_id')
                if not task_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(task_id)):
                    fail('提交回执尚未确认，请先到画境生成历史核对，避免重复扣费')
                result = self.await_result(str(task_id), output, params, report)
            except requests.RequestException:
                error = PluginError('PLUGIN_ERROR:::生成连接中断，请先到画境生成历史核对；插件不会自动重复提交')
                pending.set_exception(error)
                raise error from None
            except BaseException as error:
                pending.set_exception(error)
                raise
            else:
                pending.set_result(result)
                return result
            finally:
                with self._active_lock:
                    if self._active.get(ident) is pending:
                        del self._active[ident]
        with ThreadPoolExecutor(max_workers=max(1, min(8, int(params.get('concurrency', 2))))) as pool:
            return list(pool.map(one, positions))

    def handle_action(self, action, data=None):
        data = data or {}
        try:
            if action in ('load_settings', 'load_params'):
                return {'ok': True, 'params': self.get_params(), 'kind': self.kind, 'version': VERSION}
            if action == 'save_settings':
                supplied = data.get('params') or {}
                params = self.get_params(); params.update({k: v for k, v in supplied.items() if k in self.defaults})
                with LOCK:
                    write_json(self.config, params)
                    if str(supplied.get('api_key') or '').strip(): write_json(self.shared, {'api_key': str(supplied['api_key']).strip()})
                return {'ok': True, 'params': params}
            params = self.settings(data.get('params'))
            if action in ('test_connection', 'check_balance', 'list_models'):
                return self.connect(params)
            return {'ok': False, 'error': '未识别的插件操作'}
        except PluginError as error:
            return {'ok': False, 'error': str(error).replace('PLUGIN_ERROR:::', '')}
        except (requests.RequestException, ValueError, OSError):
            return {'ok': False, 'error': '连接或本地设置暂时异常，请检查网络和保存目录；提交中断时请先到画境生成历史核对'}
