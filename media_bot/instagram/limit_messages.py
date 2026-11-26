"""User-facing descriptions of structured Instagram limit reasons."""
import re


_MESSAGES = {
    'zh': {
        'local_media_quota': '已達 Bot 設定的 Instagram 每分鐘媒體配額',
        'local_request_budget': '已達 Bot 的單次 Instagram 掃描請求上限',
        'upstream_restriction': 'Instagram 在回應內容中要求暫停請求，已進入冷卻',
        'http_status': 'Instagram 回應 HTTP {status}，已進入冷卻',
        'unknown': 'Instagram 冷卻中，原因未記錄',
    },
    'en': {
        'local_media_quota': "The bot reached its per-minute Instagram media limit",
        'local_request_budget': "The bot reached its Instagram request limit for this scan",
        'upstream_restriction': 'Instagram requested a cooldown in its response body',
        'http_status': 'Instagram returned HTTP {status} and the bot entered cooldown',
        'unknown': 'The Instagram cooldown cause was not recorded',
    },
}


def describe(reason, lang='zh'):
    messages = _MESSAGES[lang]
    if isinstance(reason, str) and re.fullmatch(r'http_[45][0-9]{2}', reason):
        return messages['http_status'].format(status=reason[5:])
    return messages.get(reason, messages['unknown'])
