"""Review unconfirmed Telegram sends through the Takaneko provider."""
from datetime import datetime, timezone
from html import escape
import re

from .. import core, push
from . import views
from .forms import FormError, ServiceUnavailable


MAX_ID = 2**63 - 1
ATTEMPT = re.compile(r'[0-9a-f]{32}')
KINDS = {'text', 'photo', 'video', 'document', 'audio', 'animation', 'attachment'}
HEADING = '<h3 id="delivery-review" tabindex="-1">Delivery review</h3>'


class ChangedDelivery(FormError):
    status = 409


def number(value, minimum):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        raise FormError('Invalid delivery reference.')
    result = int(value)
    if not minimum <= result <= MAX_ID:
        raise FormError('Invalid delivery reference.')
    return result


def _record(item, hidden, users, subscribed):
    delivery, uid, position, total = (item[key] for key in ('delivery_id', 'user_id', 'position', 'total'))
    if (any(type(value) is not int for value in (delivery, uid, position, total))
            or not 1 <= delivery <= MAX_ID or not 1 <= uid <= MAX_ID or not 0 <= position < total <= MAX_ID
            or item['kind'] not in KINDS or not ATTEMPT.fullmatch(item['attempt'])
            or type(item['created']) not in (float, int)):
        raise ValueError('Invalid provider entry')
    queued = datetime.fromtimestamp(item['created'], timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    name, state = users.get(uid, ('', ''))
    if uid not in subscribed or state not in ('owner', 'approved'):
        return ''
    body = (f'<details class="review-delivery"><summary>Delivery #{delivery} · part {position + 1} '
            f'of {total} ({item["kind"]}) · {escape(name)}</summary>'
            f'<p>Telegram ID {uid}</p><p class="muted">Queued {queued}</p>')

    fields = (hidden + views.hidden_field('provider', 'takaneko')
              + views.hidden_field('delivery_id', delivery) + views.hidden_field('position', position)
              + views.hidden_field('attempt', item['attempt']))
    controls = (f'<fieldset><legend>Recipient confirmation for delivery #{delivery}, part {position + 1}</legend>'
                '<label><input type="radio" name="decision" value="sent" required> '
                'Received — mark this part as delivered</label>'
                '<label><input type="radio" name="decision" value="retry" required> '
                'Missing — queue this part for another send</label>'
                '<label><input type="checkbox" name="confirmed" value="1" required> '
                'I checked this specific part with the recipient.</label>'
                + views.button('Save decision', aria_label=f'Save decision for delivery {delivery}, part {position + 1}')
                + '</fieldset>')
    return body + views.form('delivery-review', fields, controls) + '</details>'


def unavailable():
    return HEADING + '<p>Live delivery review is unavailable. Reload before making a decision.</p>'


def panel(entry, hidden, users, subscribed, after):
    try:
        listing = push.control(entry, 'review_deliveries', after=after)
        items, next_after = listing['items'], listing['next_after']
        if type(items) is not list or len(items) > 20:
            raise ValueError('Invalid provider page')
        records = []
        prev = after
        for item in items:
            record = _record(item, hidden, users, subscribed)
            if item['delivery_id'] <= prev:
                raise ValueError('Invalid provider order')
            prev = item['delivery_id']
            if record:
                records.append(record)
        if next_after is not None and (not items or type(next_after) is not int or next_after != prev):
            raise ValueError('Invalid provider cursor')
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return unavailable()

    base = core.BASE + 'subscriptions'
    if records:
        explanation = ('<p>Telegram may have accepted these parts before the bot could save a receipt. '
                       'The queue time can be earlier than the send. Check the indicated part with the recipient '
                       'and leave it on hold if the outcome is unclear. '
                       'After a decision, later pending parts can continue.</p>')
    else:
        explanation = ('<p>No parts are currently available for review' + (' on this page.' if after else '.')
                       + ' Sends still in progress stay off this list.</p>')
    links = []
    if after:
        links.append(f'<a href="{base}#delivery-review">First review page</a>')
    if next_after is not None:
        links.append(f'<a href="{base}?review_after={next_after}#delivery-review">Next review page</a>')
    navigation = '<nav aria-label="Delivery review pages">' + ' · '.join(links) + '</nav>' if links else ''
    return HEADING + explanation + ''.join(records) + navigation


def submit(form):
    if form.keys() - {'csrf', 'provider', 'delivery_id', 'position', 'attempt', 'decision', 'confirmed'}:
        raise FormError('Invalid delivery review form.')
    if form.get('provider') != 'takaneko':
        raise FormError('Choose a Takaneko delivery to review.')
    delivery_id = number(form.get('delivery_id'), 1)
    position = number(form.get('position'), 0)
    attempt, decision = form.get('attempt', ''), form.get('decision')
    if not ATTEMPT.fullmatch(attempt) or decision not in ('sent', 'retry'):
        raise FormError('Select the confirmed outcome for this part.')
    if form.get('confirmed') != '1':
        raise FormError('Check this part with the recipient before saving a decision.')
    try:
        entry = push.providers().get('takaneko')
        if entry is None:
            raise ValueError('Missing provider')
        reply = push.control(entry, 'resolve_delivery', delivery_id=delivery_id, position=position,
                             attempt=attempt, decision=decision)
        if reply.get('ok') is not True or reply.get('decision') != decision:
            raise ValueError('Unconfirmed response')
    except push.DeliveryReviewChanged:
        raise ChangedDelivery('This part changed or is no longer eligible. Reload the review list before taking action.') from None
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        raise ServiceUnavailable('The provider could not confirm this decision. Reload the review list to check its state.') from None
