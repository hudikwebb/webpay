import uuid

from django import http
from django.conf import settings
from django.core.urlresolvers import reverse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

import commonware.log
from mozpay.exc import InvalidJWT, RequestExpired
from mozpay.verify import verify_jwt
from session_csrf import anonymous_csrf_exempt
from tower import ugettext as _

from webpay.auth.decorators import user_verified
from webpay.base.decorators import json_view
from webpay.base.utils import _error
from webpay.pin.forms import VerifyPinForm
from webpay.pin.utils import pin_recently_entered

from lib.marketplace.api import (client as marketplace, HttpClientError,
                                 TierNotFound)
from lib.solitude import constants
from lib.solitude.api import client as solitude

from . import get_payment_url, tasks
from .forms import VerifyForm
from .utils import verify_urls

log = commonware.log.getLogger('w.pay')


def process_pay_req(request):
    form = VerifyForm(request.GET)
    if not form.is_valid():
        return _error(request, msg=form.errors.as_text(),
                      is_simulation=form.is_simulation)

    if settings.ONLY_SIMULATIONS and not form.is_simulation:
        # Real payments are currently disabled.
        # Only simulated payments are allowed.
        return render(request, 'error.html',
                      {'error': _('Payments are temporarily disabled.')},
                      status=503)

    try:
        pay_req = verify_jwt(
            form.cleaned_data['req'],
            settings.DOMAIN,  # JWT audience.
            form.secret,
            required_keys=('request.id',
                           'request.pricePoint',  # A price tier we'll lookup.
                           'request.name',
                           'request.description',
                           'request.postbackURL',
                           'request.chargebackURL'))
    except (TypeError, InvalidJWT, RequestExpired), exc:
        log.exception('calling verify_jwt')
        return _error(request, exception=exc,
                      is_simulation=form.is_simulation)

    # Verify that the URLs in it are valid.
    try:
        verify_urls(pay_req['request']['postbackURL'],
                    pay_req['request']['chargebackURL'],
                    is_simulation=form.is_simulation)
    except ValueError, exc:
        log.exception('invalid URLs')
        return _error(request, exception=exc,
                      is_simulation=form.is_simulation)

    # Assert pricePoint is valid.
    try:
        marketplace.get_price(pay_req['request']['pricePoint'])
    except (TierNotFound, HttpClientError), exc:
        log.exception('calling verifying tier')
        return _error(request, exception=exc,
                      is_simulation=form.is_simulation)

    # All validation passed, save state to the session.
    request.session['is_simulation'] = form.is_simulation
    request.session['notes'] = {'pay_request': pay_req,
                                'issuer_key': form.key}
    request.session['trans_id'] = 'webpay:%s' % uuid.uuid4()

    # Before we verify the user's PIN let's save some
    # time and get the transaction configured via Bango in the
    # background.
    if not settings.FAKE_PAYMENTS and not form.is_simulation:
        tasks.start_pay.delay(request.session['trans_id'],
                              request.session['notes'])


@anonymous_csrf_exempt
@require_GET
def lobby(request):
    if request.GET.get('req'):
        # If it returns a response there was likely
        # an error and we should return it.
        res = process_pay_req(request)
        if isinstance(res, http.HttpResponse):
            return res
    elif settings.TEST_PIN_UI:
        # This won't get you very far but it lets you create/enter PINs
        # and stops a traceback after that.
        request.session['trans_id'] = uuid.uuid4()
    elif not 'notes' in request.session:
        # A JWT was not passed in and no JWT is in the session.
        return _error(request, msg='req is required')

    pin_form = VerifyPinForm()
    sess = request.session

    if pin_recently_entered(request):
        return http.HttpResponseRedirect(get_payment_url())

    # If the buyer closed the trusted UI during reset flow, we want to unset
    # the reset pin flag. They can hit the forgot pin button if they still
    # don't remember their pin.
    if sess.get('uuid_needs_pin_reset'):
        solitude.set_needs_pin_reset(sess['uuid'], False)
        sess['uuid_needs_pin_reset'] = False

    if sess.get('is_simulation', False):
        sim_req = sess['notes']['pay_request']['request']['simulate']
        log.info('Starting simulate %s for %s'
                 % (sim_req, sess['notes']['issuer_key']))
        return render(request, 'pay/simulate.html', {
            'simulate': sim_req
        })

    return render(request, 'pay/lobby.html', {
        'action': reverse('pin.verify'),
        'form': pin_form,
        'title': _('Enter Pin')
    })


@require_POST
def simulate(request):
    if not request.session.get('is_simulation', False):
        log.info('Request to simulate without a valid session')
        return http.HttpResponseForbidden()
    tasks.simulate_notify.delay(request.session['notes']['issuer_key'],
                                request.session['notes']['pay_request'])
    return render(request, 'pay/simulate_done.html', {})


@anonymous_csrf_exempt
@require_GET
def fakepay(request):
    if not settings.FAKE_PAYMENTS:
        return http.HttpResponseForbidden()
    return render(request, 'pay/fakepay.html')


@user_verified
@require_GET
def fake_bango_url(request):
    return render(request, 'pay/fake-bango-url.html',
                  {'bill_config_id': request.GET['bcid']})


@user_verified
@require_GET
def wait_to_start(request):
    """
    Wait until the transaction is in a ready state.

    Serve JS that polls for transaction state.
    When ready, redirect to the Bango payment URL using
    the generated billing configuration ID.
    """
    try:
        trans = solitude.get_transaction(request.session['trans_id'])
    except ValueError:
        trans = {'status': None}

    if trans['status'] in constants.STATUS_ENDED:
        log.exception('Attempt to restart finished transaction.')
        return _error(request, msg=_('Transaction has already ended.'))

    if trans['status'] == constants.STATUS_PENDING:
        # The transaction is ready; no need to wait for it.
        return http.HttpResponseRedirect(_bango_start_url(trans['uid_pay']))
    return render(request, 'pay/wait-to-start.html')


@user_verified
@json_view
@require_GET
def trans_start_url(request):
    """
    JSON handler to get the Bango payment URL to start a transaction.
    """
    try:
        trans = solitude.get_transaction(request.session['trans_id'])
    except ValueError:
        trans = {'status': None}
    data = {'url': None, 'status': trans['status']}
    if trans['status'] == constants.STATUS_PENDING:
        data['url'] = _bango_start_url(trans['uid_pay'])
    return data


def _bango_start_url(bango_trans_id):
    url = settings.BANGO_PAY_URL % bango_trans_id
    log.info('Start Bango pay at: %s' % url)
    return url
