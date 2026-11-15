# -*- coding: utf-8 -*-
"""MIT licensed open gateway entry point for TypeTale."""
import hashlib
import importlib.util
from pathlib import Path

_file = Path(__file__).resolve()
_name = 'huajing_open_gateway_' + hashlib.sha256(str(_file).encode()).hexdigest()[:16]
_spec = importlib.util.spec_from_file_location(_name, str(_file.parent / 'gateway_runtime.py'))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
_runtime = _module.Runtime(__file__, 'video')

def get_info():
    return _runtime.get_info()

def get_params():
    return _runtime.get_params()

def load_params(params):
    return None

def handle_action(action, data=None):
    return _runtime.handle_action(action, data)

def generate(context):
    try:
        return _runtime.generate(context)
    except _module.PluginError:
        raise
    except Exception:
        raise _module.PluginError('PLUGIN_ERROR:::生成或保存中断，请在服务商后台核对原任务，避免重复提交') from None
