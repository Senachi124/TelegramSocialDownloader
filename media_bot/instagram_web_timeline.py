"""Strict username-based web timeline adapter; transport owns auth and pacing."""
import re

POSTS_DOC_ID = '29104886249145907'
CONNECTION = 'xdt_api__v1__feed__user_timeline_graphql_connection'
PROVIDER = '__relay_internal__pv__Polaris'


class TimelineError(ValueError):
    pass


def media_tasks(node, expected_ids):
    """Preserve raw carousel IDs: Instaloader 4.16rc1's normalized edges omit them."""
    children = node.get('carousel_media') or [node]
    identities = [str(child.get('pk') or child.get('id') or '') for child in children]
    if (not identities or len(identities) > 20 or len(set(identities)) != len(identities)
            or set(identities) != set(expected_ids) or any(not identity.isdigit() for identity in identities)):
        raise TimelineError('media_membership_changed_or_budget')
    tasks = []
    for identity, child in zip(identities, children):
        media_type = child.get('media_type')
        if media_type not in (1, 2):
            raise TimelineError('unsupported_media_type')
        kind = 'video' if media_type == 2 else 'image'
        variants = child.get('video_versions') if kind == 'video' else (child.get('image_versions2') or {}).get('candidates')
        if not variants:
            raise TimelineError('missing_media_variants')
        best = max(variants, key=lambda v: int(v.get('width') or 0) * int(v.get('height') or 0))
        if not best.get('url'):
            raise TimelineError('missing_media_url')
        tasks.append((identity, kind, best['url']))
    return tasks


def variables(username, cursor=None):
    if not re.fullmatch(r'[a-z0-9_.]{1,30}', username) or username in ('.', '..'):
        raise TimelineError('invalid_username')
    result = {
        'data': {'count': 12, 'include_reel_media_seen_timestamp': True,
                 'include_relationship_info': True, 'latest_besties_reel_media': True,
                 'latest_reel_media': True},
        'first': 12,
        'include_multi_captions': True, 'username': username,
    }
    for name, enabled in (
        ('MultiCaptionCarouselEnabled', True), ('ShortDramaEnabled', True),
        ('SEOIsCrawler', False), ('ThreadsStyleCommentsEnabled', False),
        ('ReelsRecoDebugOverlayEnabled', False),
    ):
        result[PROVIDER + name + 'relayprovider'] = enabled
    if cursor is not None:
        result.update(after=cursor, before=None, last=None)
    return result


def parse_page(payload):
    def optional_location_error(error):
        path = error.get('path') if isinstance(error, dict) else None
        return (isinstance(path, list) and len(path) == 6 and
                path[:2] == [CONNECTION, 'edges'] and type(path[2]) is int and
                path[3:] == ['node', 'location', 'profile_pic_url'])
    if not isinstance(payload, dict) or any(not optional_location_error(error) for error in payload.get('errors') or []):
        error = TimelineError('graphql_errors')
        if isinstance(payload, dict):
            data = payload.get('data') or {}
            connection = data.get(CONNECTION) if isinstance(data, dict) else None
            error.diagnostics = {'error_count': len(payload.get('errors') or []),
                'connection_present': isinstance(connection, dict),
                'edge_count': len(connection.get('edges') or []) if isinstance(connection, dict) else 0,
                'errors_have_paths': [bool(e.get('path')) for e in (payload.get('errors') or []) if isinstance(e, dict)],
                'error_paths': [[('timeline' if part == CONNECTION else '<index>' if isinstance(part, int)
                    else part if isinstance(part, str) and re.fullmatch(r'[a-z_]{1,60}', part)
                    and not any(secret in part for secret in ('token', 'cookie', 'session', 'password')) else '<redacted>')
                    for part in e.get('path', [])[:12]] for e in (payload.get('errors') or [])[:3] if isinstance(e, dict)]}
        raise error
    data = payload.get('data')
    connection = data.get(CONNECTION) if isinstance(data, dict) else None
    if not isinstance(connection, dict):
        raise TimelineError('missing_connection')
    edges, page = connection.get('edges'), connection.get('page_info')
    if not isinstance(edges, list) or not isinstance(page, dict) or type(page.get('has_next_page')) is not bool:
        raise TimelineError('invalid_page_schema')
    nodes = []
    for edge in edges:
        node = edge.get('node') if isinstance(edge, dict) else None
        if not isinstance(node, dict) or not str(node.get('pk', '')).isdigit() or not node.get('code'):
            raise TimelineError('invalid_post_identity')
        nodes.append(node)
    cursor = page.get('end_cursor')
    if page['has_next_page'] and (not nodes or not isinstance(cursor, str) or not cursor):
        raise TimelineError('invalid_next_page')
    return nodes, cursor if page['has_next_page'] else None


def pages(context, username, max_pages=2, start_cursor=None):
    if not 1 <= max_pages <= 10:
        raise TimelineError('page_budget')
    cursor = start_cursor
    seen_cursors = {cursor} if cursor else set()
    seen_posts = set()
    for page_number in range(1, max_pages + 1):
        payload = context.doc_id_graphql_query(POSTS_DOC_ID, variables(username, cursor),
            referer=f'https://www.instagram.com/{username}/', web_api=True)
        nodes, next_cursor = parse_page(payload)
        if not nodes and page_number == 1:
            raise TimelineError('empty_first_page_unverified')
        if next_cursor and next_cursor in seen_cursors:
            raise TimelineError('repeated_cursor')
        fresh = [node for node in nodes if str(node['pk']) not in seen_posts]
        if nodes and not fresh:
            raise TimelineError('repeated_page')
        seen_posts.update(str(node['pk']) for node in nodes)
        yield {'number': page_number, 'nodes': fresh, 'next_cursor': next_cursor,
               'optional_location_errors': len(payload.get('errors') or []),
               'complete': next_cursor is None}
        if next_cursor is None:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor
