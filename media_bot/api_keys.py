"""Translation key management. The web interface receives metadata only."""
import secrets


def config(data):
    value = data.setdefault('translation', {})
    value.setdefault('openai_keys', [])
    value.setdefault('primary_key', '')
    return value


def selected(data):
    pool = config(data)
    ordered = sorted(pool['openai_keys'], key=lambda item: item['id'] != pool['primary_key'])
    return [item['key'] for item in ordered if item.get('enabled') and item.get('key')][:2]


def summary(data):
    pool = config(data)
    return [{'id': item['id'], 'label': item['label'], 'enabled': item['enabled'],
             'primary': item['id'] == pool['primary_key']} for item in pool['openai_keys']]


def _text(request, field, limit):
    value = request.get(field, '')
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('設定格式無效')
    value = value.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError('設定格式無效')
    return value


def edit(data, request):
    value = config(data)
    action = request.get('action')
    key_id = _text(request, 'id', 64)
    item = next((item for item in value['openai_keys'] if item['id'] == key_id), None)
    if action == 'save':
        label = _text(request, 'label', 60)
        key = _text(request, 'key', 2048)
        if not label or (key_id and item is None):
            raise ValueError('請提供名稱並選擇有效金鑰')
        if key and (not key.startswith('sk-') or any(ch.isspace() for ch in key)):
            raise ValueError('OpenAI API key 格式無效')
        if item is None:
            if not key or len(value['openai_keys']) >= 32:
                raise ValueError('新增時須提供 API key；最多 32 組')
            item = {'id': secrets.token_hex(12), 'label': label, 'key': key, 'enabled': True}
            value['openai_keys'].append(item)
            if not value['primary_key']:
                value['primary_key'] = item['id']
        else:
            item['label'] = label
            if key:
                item['key'] = key
    elif action in ('enable', 'disable', 'delete', 'primary'):
        if item is None:
            raise ValueError('找不到金鑰')
        if action == 'primary':
            if not item['enabled']:
                raise ValueError('請先啟用金鑰')
            value['primary_key'] = item['id']
        elif action == 'delete':
            if item['enabled'] or value['primary_key'] == item['id']:
                raise ValueError('請先停用並取消主要設定')
            value['openai_keys'].remove(item)
        else:
            item['enabled'] = action == 'enable'
            if action == 'disable' and value['primary_key'] == item['id']:
                value['primary_key'] = ''
    else:
        raise ValueError('Invalid key operation')
    return {'ok': True, 'id': item['id']}
