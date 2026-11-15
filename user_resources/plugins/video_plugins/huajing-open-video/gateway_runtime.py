# -*- coding: utf-8 -*-
"""MIT licensed TypeTale gateway adapter. Ads and model traffic use separate sessions."""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

_compat_path = Path(__file__).with_name('huajing_compat.py')
_spec = importlib.util.spec_from_file_location('huajing_compat_' + hashlib.sha256(str(_compat_path).encode()).hexdigest()[:16], _compat_path)
compat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compat)
PluginError = compat.PluginError
fail = compat.fail
VERSION = '1.0.0'
PROMOTION_URL = 'https://huajingart.cn/api/plugins/promotion'
PROMOTION_DEFAULT = {'enabled': True, 'title': '画境 · 图片与视频创作',
                     'body': '支持图片、视频创作，欢迎了解画境。', 'cta': '访问画境官网',
                     'url': 'https://huajingart.cn', 'cache_seconds': 60}
PROVIDERS = {'newapi', 'sub2api', 'openai', 'huajing'}
DONE = {'succeeded', 'success', 'completed', 'done', 'finished', 'SUCCESS'}
FAILED = {'failed', 'cancelled', 'canceled', 'expired', 'error', 'FAILURE'}


def origin(url):
    p = urlsplit(url)
    return p.scheme.lower(), (p.hostname or '').lower(), p.port or (443 if p.scheme == 'https' else 80)


def base_url(value):
    raw = str(value or '').strip().rstrip('/')
    if not raw:
        fail('请填写接口地址，例如 https://你的站点/v1')
    p = urlsplit(raw)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password or p.query or p.fragment:
        fail('接口地址需要完整的 HTTP(S) 地址，且不包含密钥、查询参数或用户名')
    try:
        p.port
    except ValueError:
        fail('接口端口格式错误')
    suffix = p.path.rstrip('/')
    if not suffix:
        suffix = '/v1'
    if suffix.endswith(('/models', '/images/generations', '/images/edits', '/videos')):
        fail('请填写 API 基础地址，例如 https://你的站点/v1')
    return urlunsplit((p.scheme, p.netloc, suffix, '', ''))


def request_url(base, path):
    if str(path).startswith(('http://', 'https://')):
        target = str(path)
    elif str(path).startswith('/v1/'):
        p = urlsplit(base)
        # Preserve an installation prefix, e.g. /openai/v1.
        prefix = p.path[:-3] if p.path.endswith('/v1') else ''
        target = urlunsplit((p.scheme, p.netloc, prefix + str(path), '', ''))
    else:
        target = base.rstrip('/') + '/' + str(path).lstrip('/')
    if origin(target) != origin(base) or urlsplit(target).username:
        fail('接口返回了不同站点的任务地址，请联系服务商核对')
    return target


def media_type(model):
    explicit = str(model.get('type') or model.get('modality') or '').lower()
    routes = json.dumps(model.get('supported_endpoint_types') or model.get('endpoints') or model.get('output_modalities') or [], ensure_ascii=False).lower()
    ident = str(model.get('id') or '').lower()
    if explicit == 'video' or 'video' in routes or re.search(r'sora|veo|seedance|kling|wan[-_. ]|hailuo|minimax-h|grok.*video', ident):
        return 'video'
    if explicit == 'image' or 'image' in routes or re.search(r'image|dall-e|imagen|banana|flux|stable-diffusion', ident):
        return 'image'
    return 'unknown'


def promotion_data(value):
    if not isinstance(value, dict) or not isinstance(value.get('enabled'), bool):
        raise ValueError('invalid promotion')
    p = {k: str(value.get(k) or '')[:limit] for k, limit in [('title', 80), ('body', 240), ('cta', 32), ('url', 2048)]}
    target = urlsplit(p['url'])
    if value['enabled'] and (target.scheme != 'https' or not target.hostname or target.username or target.password):
        raise ValueError('invalid promotion URL')
    p['enabled'] = value['enabled']
    p['cache_seconds'] = max(30, min(3600, int(value.get('cache_seconds', 60))))
    return p


class Runtime(compat.Runtime):
    def __init__(self, plugin_file, kind):
        super().__init__(plugin_file, kind)
        self.title = '通用视频插件 · 画境' if kind == 'video' else '通用图像插件 · 画境'
        self.defaults.update(provider='newapi', base_url='', api_key='', model='',
                             size='auto', quality='', response_format='', image_transport='sync',
                             video_route='openai', duration_seconds='8' if kind == 'video' else '5', resolution='',
                             extra_json='{}', task_timeout='1800')
        self._promotion_lock = threading.Lock()
        self._promotion = None
        self._promotion_time = 0
        self.promotion_cache = self.config.with_name('promotion-cache.json')

    def get_info(self):
        return {'name': self.title, 'display_name': self.title, 'version': VERSION, 'author': '画境（微信：wllydydyqr）',
                'api_version': 2, 'type': self.kind, 'images_per_batch': 1,
                'description': '自定义接口与密钥；支持 New API、Sub2API 和画境，模型由当前接口提供'}

    def get_params(self):
        p = dict(self.defaults)
        saved = compat.read_json(self.config)
        p.update({k: v for k, v in saved.items() if k in self.defaults})
        return p

    def normalize(self, supplied=None, require_key=True):
        p = self.get_params()
        p.update({k: v for k, v in (supplied or {}).items() if k in self.defaults})
        p['api_key'] = str(p.get('api_key') or '').strip()
        p['provider'] = str(p.get('provider') or 'newapi')
        if p['provider'] not in PROVIDERS:
            fail('请选择有效的接口类型')
        if p.get('base_url'):
            p['base_url'] = base_url(p['base_url'])
        elif require_key:
            fail('请先填写并保存接口地址')
        if require_key and not p['api_key']:
            fail('请填写当前接口对应的 API Key')
        if '\n' in p['api_key'] or '\r' in p['api_key']:
            fail('API Key 格式异常，请重新粘贴')
        for key, low, high in [('concurrency', 1, 8), ('poll_interval', 2, 30), ('task_timeout', 60, 7200)]:
            try:
                number = int(p[key])
                if not low <= number <= high:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                fail('%s 的取值范围是 %d 到 %d' % (key, low, high))
            p[key] = str(number)
        try:
            extra = json.loads(str(p.get('extra_json') or '{}'))
            if not isinstance(extra, dict) or len(json.dumps(extra)) > 32768:
                raise ValueError()
            if {'model', 'prompt', 'n', 'stream'} & set(extra):
                fail('额外参数请勿覆盖 model、prompt、n 或 stream')
        except (ValueError, TypeError):
            fail('额外参数需要 JSON 对象，例如 {"quality":"high"}')
        return p

    def settings(self, supplied=None):
        return self.normalize(supplied)

    def api(self, method, path, params, **kwargs):
        target = request_url(params['base_url'], path)
        timeout = (15, int(params['task_timeout']) if method == 'POST' else 40)
        with self.session_factory() as session:
            try:
                response = session.request(method, target,
                    headers={'Authorization': 'Bearer ' + params['api_key'], 'User-Agent': 'huajing-open-typetale/' + VERSION},
                    timeout=timeout, allow_redirects=False, **kwargs)
            except requests.RequestException:
                fail('连接中断。若已提交生成，请先在服务商后台核对原任务，避免重复付费', uncertain=method == 'POST')
            try:
                data = response.json()
            except ValueError:
                fail('接口返回 HTTP %s，未收到 JSON。请核对地址和服务商任务记录' % response.status_code, uncertain=method == 'POST')
            if response.status_code >= 300:
                if response.status_code == 404 and str(path).endswith('/async'):
                    fail('此站未开放 Sub2API 异步图片接口；请切换“同步图片”后重新发起')
                message = compat.safe_error(data, response.status_code)
                for secret in [params['api_key']]:
                    message = message.replace(secret, '[密钥已隐藏]')
                fail(message, uncertain=method == 'POST' and response.status_code >= 500)
            if not isinstance(data, dict):
                fail('接口数据格式异常，请核对服务商任务记录', uncertain=method == 'POST')
            return data

    def catalog(self, params):
        if params['provider'] == 'huajing':
            return super().catalog(params)
        listing = self.api('GET', '/models', params)
        raw = listing.get('data', listing.get('models', []))
        if isinstance(raw, dict):
            raw = raw.get('models', raw.get('data', []))
        if not isinstance(raw, list):
            fail('模型列表格式异常，请手填服务商提供的模型 ID')
        models, seen = [], set()
        for item in raw:
            item = {'id': item} if isinstance(item, str) else item
            if not isinstance(item, dict):
                continue
            ident = str(item.get('id') or item.get('name') or '')
            if not ident or len(ident) > 256 or ident in seen:
                continue
            seen.add(ident)
            models.append({'id': ident, 'label': str(item.get('name') or ident), 'type': media_type(item)})
        preferred = [m for m in models if m['type'] == self.kind]
        return {'models': models, 'default_model': preferred[0]['id'] if preferred else '',
                'pricing': {}, 'video': {}, 'provider': params['provider']}

    def connect(self, params):
        catalog = self.catalog(params)
        if params['provider'] == 'huajing':
            balance = self.api('GET', '/balance', params)
            return {'ok': True, 'catalog': catalog, 'credits': balance.get('credits'), 'message': '模型和余额查询成功'}
        return {'ok': True, 'catalog': catalog, 'message': '已读取 %d 个模型；生成能力由服务商对应通道决定' % len(catalog['models'])}

    def plan(self, context, params, catalog):
        if params['provider'] == 'huajing':
            huajing = dict(params)
            huajing['resolution'] = huajing.get('resolution') or ('768p' if self.kind == 'video' else '2k')
            return super().plan(context, huajing, catalog)
        prompt = str(context.get('prompt') or '').strip()
        model = str(params.get('model') or catalog.get('default_model') or '').strip()
        if not prompt:
            fail('请填写生成提示词')
        if not model or model == 'auto':
            fail('请先获取并选择模型，或填写服务商提供的模型 ID')
        media = self.media(context, params)
        if self.kind == 'video' and any(media.get(k) for k in ('videos', 'audios', 'last_frame')):
            fail('通用视频接口当前支持文字和一张参考图；音视频参考及尾帧请使用画境接口类型')
        body = {'model': model, 'prompt': prompt}
        if self.kind == 'image':
            body['n'] = 1
            size = str(params.get('size') or 'auto').strip()
            if size != 'auto':
                body['size'] = size
            if params.get('quality'):
                body['quality'] = params['quality']
            if params.get('response_format'):
                body['response_format'] = params['response_format']
            path = '/images/edits' if media.get('images') else '/images/generations'
            if params['provider'] == 'sub2api' and params.get('image_transport') == 'async':
                path += '/async'
        else:
            values = media.get('first_frame') or media.get('images') or []
            if len(values) > 1:
                fail('通用视频接口每次使用一张参考图，请在分镜中保留一张')
            media = {'input_reference': values}
            mode = params.get('duration_mode')
            seconds = context.get('scene_duration') if mode == 'scene' else (context.get('software_audio_duration') or context.get('scene_duration')) if mode == 'audio' else params['duration_seconds']
            try:
                seconds = int(math.ceil(float(seconds or params['duration_seconds'])))
                if not 1 <= seconds <= 300:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                fail('视频时长需要在 1 到 300 秒之间，并符合所选模型限制')
            if params['provider'] == 'sub2api':
                body['duration'] = seconds
                if params.get('ratio'):
                    body['aspect_ratio'] = params['ratio']
                if params.get('resolution'):
                    body['resolution'] = params['resolution']
            else:
                body['seconds'] = str(seconds)
                if params.get('size') and params['size'] != 'auto':
                    body['size'] = params['size']
            path = '/video/generations' if params.get('video_route') == 'legacy' else '/videos'
        body.update(json.loads(params.get('extra_json') or '{}'))
        if self.kind == 'video' and params['provider'] == 'sub2api' and media.get('input_reference'):
            # Sub2API's Grok video path forwards xAI JSON; its image multipart
            # conversion is specific to /images/edits, not /videos.
            value = media['input_reference'][0]
            info = self.file_info(value, 'images')
            if info:
                value = 'data:' + info[1] + ';base64,' + base64.b64encode(info[0].read_bytes()).decode('ascii')
            body['image'] = {'url': value}
            media = {}
        for role, values in media.items():
            for value in values:
                self.file_info(value, 'images' if role == 'input_reference' else role)
        return path, body, media

    @contextlib.contextmanager
    def request_data(self, body, media):
        # The submission carries an internal marker stripped before transport.
        provider = self._request_provider()
        if provider == 'huajing':
            with super().request_data(body, media) as request:
                yield request
            return
        if not any(media.values()):
            yield {'json': body}
            return
        with contextlib.ExitStack() as stack:
            files = []
            for role, values in media.items():
                for value in values:
                    field = 'input_reference' if role == 'input_reference' else 'image' if len(values) == 1 else 'image[]'
                    if value.startswith(('http://', 'https://', 'data:')):
                        raw, mime = self.reference_bytes(value)
                        handle = io.BytesIO(raw)
                        files.append((field, ('reference.' + ('jpg' if mime == 'image/jpeg' else 'png'), handle, mime)))
                    else:
                        path, mime = self.file_info(value, 'images')
                        files.append((field, (path.name, stack.enter_context(open(str(path), 'rb')), mime)))
            yield {'data': {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else str(v) for k, v in body.items()}, 'files': files}

    def _request_provider(self):
        return getattr(self._thread_context, 'provider', 'newapi')

    def reference_bytes(self, value):
        if value.startswith('data:'):
            match = re.fullmatch(r'data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)', value)
            if not match:
                fail('参考图 data URL 格式异常')
            try:
                raw = base64.b64decode(match.group(2), validate=True)
            except ValueError:
                fail('参考图 Base64 格式异常')
            mime = match.group(1)
        else:
            with self.session_factory() as session:
                r = session.get(value, timeout=(15, 45), stream=True, allow_redirects=True)
                r.raise_for_status()
                chunks, total = [], 0
                for chunk in r.iter_content(131072):
                    total += len(chunk)
                    if total > 20 * 1024 * 1024:
                        fail('参考图最大 20 MiB')
                    chunks.append(chunk)
                raw = b''.join(chunks)
                mime = r.headers.get('Content-Type', 'image/png').split(';')[0]
        if not 0 < len(raw) <= 20 * 1024 * 1024:
            fail('参考图大小超出范围')
        self.extension(raw[:64], 'image')
        return raw, mime

    @staticmethod
    def extension(head, kind):
        if kind == 'video':
            ext = 'mp4' if len(head) > 12 and head[4:8] == b'ftyp' else 'webm' if head[:4] == b'\x1aE\xdf\xa3' else None
        else:
            ext = 'png' if head.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if head.startswith(b'\xff\xd8\xff') else 'webp' if head[:4] == b'RIFF' and head[8:12] == b'WEBP' else 'gif' if head[:6] in (b'GIF87a', b'GIF89a') else None
        if not ext:
            fail('服务返回的成品不是有效的%s文件' % ('视频' if kind == 'video' else '图片'))
        return ext

    def save_bytes(self, raw, output):
        if len(raw) < 16 or len(raw) > (1024 if self.kind == 'video' else 80) * 1024 * 1024:
            fail('成品大小超出范围')
        ext = self.extension(raw[:64], self.kind)
        temp = Path(output + '.' + uuid.uuid4().hex + '.part')
        try:
            temp.write_bytes(raw)
            final = Path(output + '.' + ext)
            os.replace(str(temp), str(final))
            return str(final.resolve())
        finally:
            if temp.exists():
                temp.unlink()

    def download(self, url, output, params):
        base = params['base_url']
        target = urljoin(base.rstrip('/') + '/', str(url))
        if str(url).startswith('/v1/'):
            target = request_url(base, str(url))
        for attempt in range(3):
            temp = Path(output + '.' + uuid.uuid4().hex + '.part')
            try:
                with self.session_factory() as session:
                    current = target
                    for _ in range(6):
                        p = urlsplit(current)
                        if p.scheme not in ('https', 'http') or p.username or p.password:
                            fail('成品下载地址格式异常')
                        if urlsplit(target).scheme == 'https' and p.scheme != 'https':
                            fail('成品下载跳转需要保持 HTTPS')
                        # A fresh session contains no cookies or default authorization.
                        headers = {'Authorization': 'Bearer ' + params['api_key']} if origin(current) == origin(base) else {}
                        response = session.get(current, headers=headers, stream=True, allow_redirects=False, timeout=(15, 90))
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('Location')
                            response.close()
                            if not location:
                                fail('成品跳转缺少地址')
                            current = urljoin(current, location)
                            continue
                        break
                    else:
                        fail('成品跳转次数过多')
                    try:
                        response.raise_for_status()
                        size, head = 0, b''
                        with temp.open('wb') as handle:
                            for chunk in response.iter_content(131072):
                                size += len(chunk)
                                if size > (1024 if self.kind == 'video' else 80) * 1024 * 1024:
                                    fail('成品超过插件下载大小上限')
                                head = (head + chunk)[:64]
                                handle.write(chunk)
                        expected = response.headers.get('Content-Length')
                        if expected and not response.headers.get('Content-Encoding') and int(expected) != size:
                            raise requests.RequestException('partial download')
                        if size < 16:
                            raise requests.RequestException('empty download')
                        ext = self.extension(head, self.kind)
                    finally:
                        response.close()
                final = Path(output + '.' + ext)
                os.replace(str(temp), str(final))
                return str(final.resolve())
            except (requests.RequestException, OSError, ValueError):
                if attempt == 2:
                    fail('成品下载中断，请在服务商后台下载原成品，避免重复生成')
                self.sleep(2 + attempt)
            finally:
                if temp.exists():
                    temp.unlink()

    def result_file(self, task, output, params):
        result = task.get('result')
        if isinstance(result, dict):
            found = self.result_file(result, output, params)
            if found:
                return found
        data = task.get('data')
        if isinstance(data, dict):
            found = self.result_file(data, output, params)
            if found:
                return found
        items = data if isinstance(data, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('b64_json'):
                try:
                    raw = base64.b64decode(item['b64_json'], validate=True)
                except (ValueError, TypeError):
                    fail('图片 Base64 数据异常')
                return self.save_bytes(raw, output)
            if item.get('url'):
                return self.download(item['url'], output, params)
        video = task.get('video')
        if isinstance(video, dict) and video.get('url'):
            return self.download(video['url'], output, params)
        for key in (('video_url', 'url', 'result_url', 'download_url') if self.kind == 'video' else ('image_url', 'url')):
            if isinstance(task.get(key), str) and task[key]:
                return self.download(task[key], output, params)
        images = task.get('images')
        if isinstance(images, list) and images:
            item = images[0]
            url = item.get('url') if isinstance(item, dict) else item
            if url:
                return self.download(url, output, params)
        return None

    def await_task(self, task, output, params, report, poll_path=None):
        if isinstance(task.get('data'), dict) and any(k in task['data'] for k in ('status', 'task_id', 'request_id', 'id')):
            task = {**task, **task['data']}
        if params['provider'] == 'newapi' and isinstance(task.get('data'), str) and not task.get('id'):
            task = {**task, 'task_id': task['data']}
        ident = str(task.get('id') or task.get('task_id') or task.get('request_id') or '')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,180}', ident):
            fail('未收到有效任务编号，请在服务商后台核对回执，避免重复生成')
        if not poll_path:
            if params['provider'] == 'huajing':
                poll_path = ('/videos/tasks/' if self.kind == 'video' else '/images/tasks/') + quote(ident, safe='')
            elif self.kind == 'image':
                poll_path = '/images/tasks/' + quote(ident, safe='')
            else:
                prefix = '/video/generations/' if params.get('video_route') == 'legacy' else '/videos/'
                poll_path = prefix + quote(ident, safe='')
        request_url(params['base_url'], poll_path)
        deadline = time.monotonic() + int(params['task_timeout'])
        interval = int(params['poll_interval'])
        while time.monotonic() < deadline:
            status = str(task.get('status') or task.get('state') or '')
            if status in FAILED:
                fail(compat.safe_error(task) if task.get('error') else '任务已结束：' + status)
            result = self.result_file(task, output, params)
            if result:
                report('已完成并保存到作品', 100)
                return result
            if status in DONE and self.kind == 'video' and params['provider'] != 'huajing':
                content = '/videos/' + quote(ident, safe='') + '/content'
                return self.download(request_url(params['base_url'], content), output, params)
            report('任务 %s · %s' % (ident, '正在排队' if status in ('queued', 'pending') else '正在生成'))
            self.sleep(interval)
            try:
                task = self.api('GET', poll_path, params)
                if isinstance(task.get('data'), dict) and any(k in task['data'] for k in ('status', 'task_id', 'request_id', 'id')):
                    task = {**task, **task['data']}
            except PluginError as error:
                if '连接中断' not in str(error):
                    raise
                report('正在重新查询同一个任务')
        fail('等待结束，任务编号：%s。请先在服务商后台查看原任务结果' % ident)

    def generate(self, context):
        params = self.settings(context.get('plugin_params'))
        callback = context.get('progress_callback')
        def report(message, percent=None):
            if not callback:
                return
            try:
                callback(message) if percent is None else callback(message, percent)
            except TypeError:
                callback(message)
        report('正在准备生成参数')
        catalog = self.catalog(params) if params['provider'] == 'huajing' or not params.get('model') or params.get('model') == 'auto' else {'default_model': params['model']}
        plan = self.plan(context, params, catalog)
        positions = context.get('output_position') or [0]
        positions = positions if isinstance(positions, list) else [positions]
        if len(set(map(str, positions))) != len(positions):
            fail('宿主输出槽位重复')
        if not hasattr(self, '_thread_context'):
            with self._active_lock:
                if not hasattr(self, '_thread_context'):
                    self._thread_context = threading.local()
        def one(position):
            ident, output = self.output_slot(context, params, plan, position)
            # Accounts may reuse the same API key string on separate services.
            ident = hashlib.sha256((ident + params['base_url'] + params['provider']).encode()).hexdigest()
            with self._active_lock:
                pending = self._active.get(ident)
                owner = pending is None
                if owner:
                    pending = Future()
                    self._active[ident] = pending
            if not owner:
                return pending.result()
            try:
                self._thread_context.provider = params['provider']
                report('正在提交生成请求')
                with self.request_data(plan[1], plan[2]) as request:
                    task = self.api('POST', plan[0], params, **request)
                result = self.result_file(task, output, params)
                if not result:
                    result = self.await_task(task, output, params, report, task.get('poll_url'))
                pending.set_result(result)
                return result
            except BaseException as error:
                pending.set_exception(error)
                raise
            finally:
                with self._active_lock:
                    if self._active.get(ident) is pending:
                        del self._active[ident]
        with ThreadPoolExecutor(max_workers=int(params['concurrency'])) as pool:
            return list(pool.map(one, positions))

    def promotion(self):
        with self._promotion_lock:
            now = time.time()
            if self._promotion and now - self._promotion_time < self._promotion['cache_seconds']:
                return self._promotion
            try:
                # Never use the generation session, custom base URL, API key or cookies.
                with self.session_factory() as session:
                    session.headers.clear()
                    response = session.get(PROMOTION_URL, headers={'User-Agent': 'huajing-open-typetale/' + VERSION},
                                           timeout=(3, 4), allow_redirects=False)
                    response.raise_for_status()
                    if len(response.content) > 16384:
                        raise ValueError('promotion too large')
                    value = promotion_data(response.json())
                self._promotion, self._promotion_time = value, now
                try:
                    compat.write_json(self.promotion_cache, {'saved_at': now, 'promotion': value})
                except OSError:
                    pass
                return value
            except (requests.RequestException, ValueError, OSError):
                cached = compat.read_json(self.promotion_cache)
                try:
                    value = promotion_data(cached['promotion']) if now - float(cached.get('saved_at', 0)) < 86400 else dict(PROMOTION_DEFAULT)
                except (ValueError, KeyError, TypeError):
                    value = dict(PROMOTION_DEFAULT)
                self._promotion, self._promotion_time = value, now
                return value

    def handle_action(self, action, data=None):
        data = data or {}
        try:
            if action in ('load_settings', 'load_params'):
                return {'ok': True, 'params': self.get_params(), 'kind': self.kind, 'version': VERSION}
            if action == 'load_promotion':
                return {'ok': True, 'promotion': self.promotion()}
            if action == 'save_settings':
                params = self.normalize(data.get('params'), require_key=False)
                with compat.LOCK:
                    compat.write_json(self.config, params)
                return {'ok': True, 'params': params}
            if action in ('test_connection', 'list_models', 'check_balance'):
                return self.connect(self.settings(data.get('params')))
            return {'ok': False, 'error': '未识别的插件操作'}
        except PluginError as error:
            return {'ok': False, 'error': str(error).replace('PLUGIN_ERROR:::', '')}
        except (requests.RequestException, ValueError, OSError, TypeError):
            return {'ok': False, 'error': '请核对接口地址、密钥、设置格式和网络连接'}
