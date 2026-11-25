"""Publish translation commands while preserving inherited Telegram menus."""
from functools import lru_cache

from . import bot_ui


@lru_cache(maxsize=128)
def publish(owner, chat, send):
    if not bot_ui.is_owner(owner) or type(chat) is not int or chat >= 0:
        return False
    member = send('getChatMember', {'chat_id':chat, 'user_id':owner})
    if member['status'] in ('left','kicked') or member.get('is_member') is False:
        return False
    scopes = [{'type':'chat_member','chat_id':chat,'user_id':owner}]
    admin = member['status'] in ('administrator','creator')
    if admin:
        scopes.append({'type':'chat_administrators','chat_id':chat})
    scopes.append({'type':'chat','chat_id':chat})
    if admin:
        scopes.append({'type':'all_chat_administrators'})
    scopes.extend([{'type':'all_group_chats'}, {'type':'default'}])
    return _append_translate(scopes, send)


def publish_chat(chat, send):
    """Append to the private or group chat scope selected by the rollout."""
    if type(chat) is not int or chat == 0:
        raise ValueError('Invalid Telegram chat')
    inherited = 'all_private_chats' if chat > 0 else 'all_group_chats'
    return _append_translate([{'type':'chat','chat_id':chat},
                              {'type':inherited}, {'type':'default'}], send)


def _append_translate(scopes, send):
    command = next(row for row in bot_ui.OWNER_COMMANDS if row['command'] == 'translate')
    # Resolve the effective menu before writing either language override.
    menus = {}
    for lang in ('','zh'):
        commands = []
        for scope in scopes:
            for code in ([lang,''] if lang else ['']):
                commands = send('getMyCommands', {'scope':scope,'language_code':code})
                if commands:
                    break
            if commands:
                break
        menus[lang] = [r for r in commands if r['command'] != 'translate'] + [command]
    # Write Chinese first so a retry cannot shadow an inherited Chinese menu.
    for lang in ('zh',''):
        commands = menus[lang]
        target = {'scope':scopes[0], 'language_code':lang}
        if send('getMyCommands',target) != commands:
            send('setMyCommands',{**target,'commands':commands})
        if send('getMyCommands',target) != commands:
            raise RuntimeError('Telegram menu readback mismatch')
    return True
