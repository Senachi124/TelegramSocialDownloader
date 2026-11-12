"""Replace Instagram discovery only; preserve gallery-dl's media/archive pipeline."""
import copy
from importlib.metadata import version
import requests
import time
from . import core as c

from .instagram_web_client import WebClient, WebStop
from .instagram_web_timeline import pages, TimelineError
from .instagram_policy import RequestWindow


def _optional_post_errors(payload, items, shortcode):
    """Accept only the observed AI-decoration failure on an identified post.

    The transport has already rejected authentication/rate-limit responses.
    Never treat an arbitrary GraphQL error or partial media list as success.
    """
    errors = payload.get('errors')
    if (not isinstance(errors, list) or not errors or
            payload.get('status') not in (None, 'ok') or
            not isinstance(items, list) or len(items) != 1):
        return False
    item = items[0]
    if (not isinstance(item, dict) or item.get('code') != shortcode or
            not str(item.get('pk') or '').isdigit()):
        return False
    media_type = item.get('media_type')
    if type(media_type) is not int or media_type not in (1, 2, 8):
        return False
    children = item.get('carousel_media') if media_type == 8 else [item]
    if (not isinstance(children, list) or not children or
            (media_type == 8 and (type(item.get('carousel_media_count')) is not int or
                                item['carousel_media_count'] != len(children)))):
        return False
    identities = set()
    for child in children:
        if not isinstance(child, dict):
            return False
        identity = str(child.get('pk') or '')
        kind = child.get('media_type')
        if (not identity.isdigit() or identity in identities or
                type(kind) is not int or kind not in (1, 2)):
            return False
        identities.add(identity)
        images = child.get('image_versions2')
        variants = (child.get('video_versions') if kind == 2 else
                    images.get('candidates') if isinstance(images, dict) else None)
        if not isinstance(variants, list) or not any(
                isinstance(v, dict) and isinstance(v.get('url'), str) and v['url']
                for v in variants):
            return False
    field = 'ai_interactive_embodiment_attachment_style_info'
    allowed_paths = (
        ['_xdt_api__v1__media__shortcode__web_info_items', 0, 'node', field],
        ['xdt_api__v1__media__shortcode__web_info', 'items', 0, field],
    )
    for error in errors:
        if not isinstance(error, dict):
            return False
        path = error.get('path')
        # bool and float compare equal to int, but are not GraphQL list indices.
        if (not isinstance(path, list) or path not in allowed_paths or
                type(path[1] if path[0] == allowed_paths[0][0] else path[2]) is not int or
                error.get('severity') != 'ERROR' or
                error.get('message') !=
                'A server error field_exception occured. Check server logs for details.'):
            return False
    return True


def install_web_graphql(result):
    from gallery_dl.extractor.instagram import InstagramAPI, InstagramExtractor, QUERIES
    if getattr(InstagramAPI, '_bot_web_graphql', False):
        return
    if version('gallery-dl') != '1.32.17':
        raise RuntimeError('Review web adapter before changing gallery-dl version')
    limiter = RequestWindow(c.DATA/'instagram-api-rate.sqlite3', policy={'windowMinutes':1,'maxRequests':10,'minIntervalSeconds':6})
    def block(seconds=900):
        until=max(c.get_setting('instagram_cooldown',0), time.time()+max(900,seconds))
        limiter.block(until-time.time())
        c.setting('instagram_cooldown',until)
        result.update(retry_at=until, wait_reason='platform')
    def reserve():
        until=c.get_setting('instagram_cooldown',0)
        if until>time.time():
            result.update(retry_at=until, wait_reason='platform')
            raise WebStop('platform_cooldown_active')
        try:limiter.reserve()
        except ValueError:
            with limiter.connect() as db:
                until=db.execute('SELECT blocked_until FROM state WHERE id=1').fetchone()[0]
            if until<=time.time():raise
            c.setting('instagram_cooldown',until)
            result.update(retry_at=until, wait_reason='platform')
            raise WebStop('platform_cooldown_active') from None
    original_init = InstagramExtractor._init
    original_finalize = InstagramExtractor.finalize

    def record_stop(error):
        if isinstance(error, WebStop) and error.diagnostic:
            result['instagram_diagnostic'] = dict(error.diagnostic)
            if error.diagnostic.get('kind') == 'upstream_redirect':
                result['failure'] = 'upstream_redirect'

    def initialize(extractor):
        original_cookies = [copy.copy(c) for c in extractor.cookies]
        original_init(extractor)
        extractor.cookies.clear()
        for cookie in original_cookies:
            extractor.cookies.set_cookie(cookie)
        extractor.csrf_token = extractor.cookies.get_dict().get('csrftoken', '')

    def client(api):
        if not hasattr(api, '_bot_web_client'):
            cookie_values = api.extractor.cookies.get_dict()
            if not cookie_values.get('sessionid') or not cookie_values.get('csrftoken'):
                raise api.exc.AbortExtraction('AuthRequired: complete Cookie snapshot required')
            session=requests.Session()
            session.cookies.clear()
            for cookie in api.extractor.cookies:
                session.cookies.set_cookie(copy.copy(cookie))
            session.headers.update({
                'Accept-Encoding':'gzip, deflate', 'Accept-Language':'en-US,en;q=0.8',
                'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36',
                'Referer':'https://www.instagram.com/', 'x-ig-app-id':'936619743392459',
                'X-CSRFToken':cookie_values['csrftoken'],
            })
            api._bot_web_session=session
            api._bot_web_client = WebClient(session, reserve, block)

        return api._bot_web_client

    def call(api, doc_id, variables, web_api=True):
        try:
            return client(api).doc_id_graphql_query(doc_id, variables, web_api=web_api)
        except WebStop as error:
            record_stop(error)
            raise api.exc.AbortExtraction(str(error)) from None

    def feed(api, handle):
        if handle.startswith('id:'):
            handle = api.user_by_id(handle[3:])['username']
        cursor = api.extractor._init_cursor()
        seen = {cursor} if cursor else set()
        try:
            while True:
                for page in pages(client(api), handle, max_pages=10, start_cursor=cursor):
                    if page['optional_location_errors']:
                        api.extractor.log.info('略過 %s 個地點頭像選填欄位錯誤', page['optional_location_errors'])
                    yield from page['nodes']
                    cursor = page['next_cursor']
                    if cursor is None:
                        return
                    if cursor in seen:
                        raise TimelineError('repeated_cursor')
                    seen.add(cursor)
                    api.extractor._cursor = cursor
        except (WebStop, TimelineError) as error:
            record_stop(error)
            raise api.exc.AbortExtraction(str(error)) from None

    def by_web(api, username):
        # Only accept a user node whose username matches; collaboration owners
        # may differ from the account whose timeline was requested.
        try:
            first = next(pages(client(api), username, max_pages=1))
            owner = next((node.get('user') for node in first['nodes']
                if (node.get('user') or {}).get('username', '').lower() == username.lower()), None)
            if not owner or not str(owner.get('pk', '')).isdigit():
                raise TimelineError('target_identity_unresolved')
            return {**owner, 'id': str(owner['pk'])}
        except (WebStop, TimelineError) as error:
            record_stop(error)
            raise api.exc.AbortExtraction(str(error)) from None

    def by_id(api, user_id):
        variables = {'id':str(user_id),'render_surface':'PROFILE','enable_integrity_filters':True,
            '__relay_internal__pv__PolarisCannesGuardianExperienceEnabledrelayprovider':True,
            '__relay_internal__pv__PolarisCASB976ProfileEnabledrelayprovider':False,
            '__relay_internal__pv__PolarisRepostsConsumptionEnabledrelayprovider':False,
            '__relay_internal__pv__PolarisWebSchoolsEnabledrelayprovider':False}
        # Stories resolves the owner's profile before requesting its reel. Use
        # the authenticated web route, as for post/media and timeline queries;
        # the legacy /graphql/query endpoint redirects valid sessions to home.
        payload = call(api, '27937681195819736', variables, web_api=True)
        user = (payload.get('data') or {}).get('user')
        if payload.get('errors') or not isinstance(user, dict) or str(user.get('pk') or user.get('id')) != str(user_id):
            raise api.exc.AbortExtraction('profile_identity_or_schema_error')
        return {**user, 'id': str(user_id)}

    def graphql(api, opname, variables, **kwargs):
        payload = call(api, QUERIES[opname][0], variables)
        if payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise api.exc.AbortExtraction('graphql_schema_error')
        return payload['data']

    def paginate(api, opname, variables, fieldname, route=None):
        seen = set()
        variables = dict(variables)
        variables['after'] = api.extractor._init_cursor()
        while True:
            data = graphql(api, opname, variables).get(fieldname)
            if isinstance(data, dict) and 'edges' not in data:
                data = next((value for value in data.values() if isinstance(value, dict) and 'edges' in value), None)
            if not isinstance(data, dict) or not isinstance(data.get('edges'), list):
                raise api.exc.AbortExtraction('missing_scope_connection')
            for edge in data['edges']:
                yield edge['node']
            info = data.get('page_info') or {}
            if info.get('has_next_page') is False:
                return
            cursor = info.get('end_cursor')
            if info.get('has_next_page') is not True or not cursor or cursor in seen or not data['edges']:
                raise api.exc.AbortExtraction('invalid_scope_pagination')
            seen.add(cursor)
            variables['after'] = api.extractor._update_cursor(cursor)

    def media(api, shortcode):
        # Use the authenticated web endpoint and its session tokens. The legacy
        # /graphql/query route redirects despite valid saved browser cookies.
        payload = call(api, '27128499623469141', {'shortcode':shortcode,
            '__relay_internal__pv__PolarisAIGMMediaWebLabelEnabledrelayprovider':False}, web_api=True)
        items = ((payload.get('data') or {}).get('xdt_api__v1__media__shortcode__web_info') or {}).get('items')
        if not items or (payload.get('errors') and
                         not _optional_post_errors(payload, items, shortcode)):
            raise api.exc.AbortExtraction('missing_post_media')
        if payload.get('errors'):
            api.extractor.log.info('略過 %s 個 AI 裝飾選填欄位錯誤', len(payload['errors']))
        return iter(items)

    def finalize(extractor, status):
        try:
            return original_finalize(extractor, status)
        finally:
            session = getattr(getattr(extractor, 'api', None), '_bot_web_session', None)
            if session:
                session.close()

    methods=('user_feed','_user_by_web_impl','_user_by_id_impl','_call_graphql','_pagination_graphql','media')
    previous={name:getattr(InstagramAPI,name) for name in methods}
    def restore():
        for name,value in previous.items():setattr(InstagramAPI,name,value)
        InstagramExtractor._init=original_init
        InstagramExtractor.finalize=original_finalize
        del InstagramAPI._bot_web_graphql
    InstagramExtractor._init = initialize
    InstagramExtractor.finalize = finalize
    InstagramAPI.user_feed = feed
    InstagramAPI._user_by_web_impl = by_web
    InstagramAPI._user_by_id_impl = by_id
    InstagramAPI._call_graphql = graphql
    InstagramAPI._pagination_graphql = paginate
    InstagramAPI.media = media
    InstagramAPI._bot_web_graphql = True
    return restore
