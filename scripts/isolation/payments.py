#!/usr/bin/env python3
"""Payments, invoices and bookings isolation on AWS dev (Przelewy24 SANDBOX), on top of the world from world.py.

Phases (each can run alone: payments.py checkout | status | invoices | bookings | attack):
  checkout  B2C checkouts for test accounts; prints the sandbox payment URLs (paid by pay_sandbox.mjs)
  status    waits until the paid orders are PAID (P24 notification + verification on dev)
  invoices  invoice request by the buyer, PDF upload and issue by the admin, download by the buyer
  bookings  public specialist profile (admin link), online slot, booking request by a client
  attack    every payment order, invoice request and booking request against every other account; admin
            invoice routes as non-admins; the invoice download URL without its signature
State goes to results/isolation/payments.json (private: no passwords, only IDs and URLs of test data).
"""
import datetime as dt
import json
import secrets
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import API_BASE, RESULTS, Tokens, guard, load_world, request, save_private  # noqa: E402

STATE = RESULTS / 'payments.json'
TERMS = '2026-11-15-r4'
BUYERS = ('user-x', 'user-y', 'org-a-client', 'org-b-client')
TODAY = dt.date.today()


class Ctx:
    def __init__(self):
        self.world = load_world()
        self.tokens = Tokens({a['username']: a['password'] for a in self.world['actors'].values()})
        self.state = json.loads(STATE.read_text()) if STATE.exists() else {}

    def call(self, actor, method, path, body=None, headers=None, raw=False):
        return request(method, API_BASE + path, self.tokens.get(self.world['actors'][actor]['username']), body,
                       headers or {}, timeout=60)

    def save(self):
        save_private(STATE, self.state)


def buyer(actor):
    return {'companyName': f'Firma testowa {actor}', 'nip': '5260250274', 'street': 'ul. Testowa 1',
            'postalCode': '00-001', 'city': 'Warszawa', 'countryCode': 'PL',
            'invoiceEmail': f'iso-invoice-{actor}@perf.diecik.invalid'}


def checkout(ctx):
    orders = ctx.state.setdefault('orders', {})
    for actor in BUYERS:
        if actor in orders:
            continue
        res = ctx.call(actor, 'POST', '/api/subscriptions/checkout-sessions', {
            'planCode': 'premium_monthly', 'productScope': 'B2C', 'provider': 'PRZELEWY24',
            'successUrl': 'https://dev.diecik.com/subscription/result', 'cancelUrl': 'https://dev.diecik.com/premium',
            'termsVersionAccepted': TERMS, 'immediateActivationConsentVersion': TERMS, 'autoRenew': False,
            'recurringConsentVersion': None, 'invoice': None})
        print(actor, res.status, res.body[:200])
        if res.ok:
            data = res.json()
            orders[actor] = {'paymentOrderId': data['paymentOrderId'], 'checkoutUrl': data['checkoutUrl']}
    ctx.save()
    print(json.dumps({a: o['checkoutUrl'] for a, o in orders.items()}, indent=1))


def status(ctx, wait=300):
    deadline = time.time() + wait
    while True:
        states = {}
        for actor, order in ctx.state.get('orders', {}).items():
            res = ctx.call(actor, 'GET', f"/api/subscriptions/payment-orders/{order['paymentOrderId']}")
            states[actor] = (res.json() or {}).get('status') if res.ok else f'HTTP {res.status}'
        print(states, flush=True)
        if all(s == 'PAID' for s in states.values()) or time.time() > deadline:
            return states
        time.sleep(10)


def invoices(ctx):
    requests_ = ctx.state.setdefault('invoiceRequests', {})
    for actor, order in ctx.state.get('orders', {}).items():
        if actor in requests_:
            continue
        res = ctx.call(actor, 'POST', f"/api/subscriptions/payment-orders/{order['paymentOrderId']}/invoice-request",
                       buyer(actor))
        print('request', actor, res.status, res.body[:160])
        if res.ok:
            requests_[actor] = {'id': (res.json() or {}).get('id')}
    ctx.save()
    listing = ctx.call('admin', 'GET', '/api/admin/invoice-requests').json() or []
    items = listing if isinstance(listing, list) else listing.get('items') or listing.get('content') or []
    by_order = {i.get('paymentOrderId'): dict(i.get('invoice') or {}, **{k: v for k, v in i.items() if k != 'invoice'})
                for i in items}
    pdf = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'
    for actor, order in ctx.state.get('orders', {}).items():
        item = by_order.get(order['paymentOrderId'])
        if not item or requests_.get(actor, {}).get('issue') == 200:
            continue
        requests_.setdefault(actor, {})['id'] = item.get('id')
        boundary = 'iso' + secrets.token_hex(8)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="faktura-{actor}.pdf"\r\n'
                f'Content-Type: application/pdf\r\n\r\n').encode() + pdf + f'\r\n--{boundary}--\r\n'.encode()
        up = ctx.call('admin', 'PUT', f"/api/admin/invoice-requests/{item['id']}/pdf", body,
                      {'Content-Type': f'multipart/form-data; boundary={boundary}'})
        issued = ctx.call('admin', 'POST', f"/api/admin/invoice-requests/{item['id']}/issue", {
            'invoiceNumber': f'ISO/{TODAY:%Y/%m}/{actor}', 'ksefNumber': None, 'issueDate': str(TODAY)})
        own = ctx.call(actor, 'GET', f"/api/subscriptions/payment-orders/{order['paymentOrderId']}/invoice")
        url = (own.json() or {}).get('url') if own.ok else None
        requests_[actor].update({'upload': up.status, 'issue': issued.status, 'ownerDownload': own.status, 'url': url})
        print('invoice', actor, up.status, issued.status, own.status)
    ctx.save()


def bookings(ctx):
    spec = 'spec-a'
    email = ctx.world['actors'][spec]['email']
    ctx.call(spec, 'PUT', '/api/trainer/booking-settings', {'enabled': True, 'onlineEnabled': True,
                                                           'onsiteEnabled': False, 'onsiteAddress': None})
    ctx.call(spec, 'PUT', '/api/trainer/public-profile', {
        'displayName': 'Specjalista testowy A', 'headline': 'Test izolacji', 'location': 'Online', 'summary': 'Profil testowy do sprawdzenia izolacji rezerwacji.',
        'specialties': ['Dietetyka'], 'contactEmail': email, 'phone': None, 'websiteUrl': None,
        'responseNote': None, 'published': True})
    trainer = ctx.state.get('recommendedTrainer')
    if not trainer:
        created = ctx.call('admin', 'POST', '/api/admin/recommended-trainers', {
            'displayName': 'Specjalista testowy A', 'headline': 'Test izolacji', 'location': 'Online',
            'summary': 'Profil testowy do sprawdzenia izolacji rezerwacji.', 'specialties': ['Dietetyka'], 'contactEmail': email, 'phone': None,
            'websiteUrl': None, 'responseNote': None, 'displayOrder': 999, 'published': True, 'fictional': False,
            'showInRecommended': False})
        print('recommended trainer', created.status, created.body[:160])
        trainer = (created.json() or {}).get('id')
        link = ctx.call('admin', 'POST', f'/api/admin/recommended-trainers/{trainer}/booking-link', {'trainerEmail': email})
        print('booking link', link.status, link.body[:160])
        ctx.state['recommendedTrainer'] = trainer
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    slots = []
    for hours in (0, 2):
        res = ctx.call(spec, 'POST', '/api/trainer/booking-slots', {
            'startAt': (start + dt.timedelta(hours=hours)).isoformat(),
            'endAt': (start + dt.timedelta(hours=hours + 1)).isoformat(),
            'providerUserId': ctx.world['actors'][spec]['keycloakId']})
        print('slot', res.status, res.body[:120])
        if res.ok:
            slots.append(res.json())
    ctx.state['slots'] = [s['id'] for s in slots] or ctx.state.get('slots', [])
    card = ctx.world['resources']['card:spec-a:client-a1']
    local = start.astimezone(dt.timezone(dt.timedelta(hours=1)))
    if ctx.state['slots']:
        res = ctx.call('client-a1', 'POST', '/api/booking-requests', {
            'slotId': ctx.state['slots'][0], 'clientId': card['id'],
            'providerUserId': ctx.world['actors'][spec]['keycloakId'], 'date': str(local.date()),
            'startTime': local.strftime('%H:%M'), 'visitMode': 'ONLINE', 'expectedTimeZone': 'Europe/Warsaw',
            'contactEmail': ctx.world['actors']['client-a1']['email'], 'note': ctx.world['actors']['client-a1']['canary']})
        print('booking request', res.status, res.body[:200])
        if res.ok:
            ctx.state['bookingRequest'] = (res.json() or {}).get('id')
    ctx.save()


def attack(ctx):
    results = []
    actors = [n for n in ctx.world['actors'] if n != 'admin']

    def probe(owner, method, path, body=None, label=''):
        for attacker in actors:
            if attacker == owner:
                continue
            res = ctx.call(attacker, method, path, body)
            leak = ctx.world['actors'][owner]['canary'] in res.body or owner in res.body
            results.append({'case': label, 'owner': owner, 'attacker': attacker, 'status': res.status,
                            'verdict': 'denied' if res.status in (401, 403, 404) else
                            ('FINDING' if res.ok else f'status-{res.status}'), 'leak': leak, 'body': res.body[:160]})

    for owner, order in ctx.state.get('orders', {}).items():
        oid = order['paymentOrderId']
        probe(owner, 'GET', f'/api/subscriptions/payment-orders/{oid}', label='payment order: read')
        probe(owner, 'GET', f'/api/subscriptions/payment-orders/{oid}/invoice', label='invoice: download link')
        probe(owner, 'POST', f'/api/subscriptions/payment-orders/{oid}/invoice-request', buyer('attacker'),
              label='invoice: request on a foreign order')
        probe(owner, 'POST', f'/api/subscriptions/payment-orders/{oid}/cancellations', {}, label='payment order: cancel')
        for attacker in actors:
            if attacker != owner:
                history = ctx.call(attacker, 'GET', '/api/subscriptions/payment-orders')
                if oid in history.body:
                    results.append({'case': 'payment history lists a foreign order', 'owner': owner,
                                    'attacker': attacker, 'status': history.status, 'verdict': 'FINDING'})
    for owner, req in ctx.state.get('invoiceRequests', {}).items():
        url = req.get('url')
        if url:
            unsigned = url.split('?')[0]
            res = request('GET', unsigned, None)
            signed = request('GET', url, None)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            results.append({'case': 'invoice PDF: URL without signature', 'owner': owner, 'status': res.status,
                            'verdict': 'denied' if res.status in (401, 403) else 'FINDING',
                            'signedStatus': signed.status, 'expiresSeconds': (query.get('X-Amz-Expires') or [None])[0]})
        if req.get('id'):
            for actor in ('spec-a', 'user-y', 'org-a-owner'):
                for method, path in (('GET', f"/api/admin/invoice-requests/{req['id']}/pdf"),
                                     ('POST', f"/api/admin/invoice-requests/{req['id']}/cancel")):
                    res = ctx.call(actor, method, path, {} if method == 'POST' else None)
                    results.append({'case': f'admin invoice route as {actor}: {method} {path.split("/")[-1]}',
                                    'owner': owner, 'attacker': actor, 'status': res.status,
                                    'verdict': 'denied' if res.status in (401, 403) else 'FINDING'})
    booking = ctx.state.get('bookingRequest')
    if booking:
        for method, path, body in (('GET', f'/api/trainer/booking-requests/{booking}', None),
                                   ('POST', f'/api/trainer/booking-requests/{booking}/accept', {}),
                                   ('POST', f'/api/trainer/booking-requests/{booking}/decline', {}),
                                   ('POST', f'/api/trainer/booking-requests/{booking}/cancel', {})):
            for attacker in actors:
                if attacker in ('spec-a', 'client-a1'):
                    continue
                res = ctx.call(attacker, method, path, body)
                results.append({'case': f'booking: {method} {path.rsplit("/", 1)[-1]}', 'owner': 'spec-a',
                                'attacker': attacker, 'status': res.status,
                                'verdict': 'denied' if res.status in (401, 403, 404) else
                                ('FINDING' if res.ok else f'status-{res.status}'), 'body': res.body[:160]})
        mine = ctx.call('client-b1', 'GET', '/api/booking-requests/mine')
        results.append({'case': 'booking: foreign request in /mine', 'attacker': 'client-b1', 'status': mine.status,
                        'verdict': 'FINDING' if booking in mine.body else 'denied'})
    if len(ctx.state.get('slots', [])) > 1:
        slot = ctx.state['slots'][1]
        cards = [('client-a1', ctx.world['resources']['card:spec-a:client-a1']['id']),
                 ('client-a2', ctx.world['resources']['card:spec-a:client-a2']['id'])]
        import threading
        barrier = threading.Barrier(2)
        answers = []

        def book(actor, card_id):
            barrier.wait()
            answers.append(ctx.call(actor, 'POST', '/api/booking-requests', {
                'slotId': slot, 'clientId': card_id, 'visitMode': 'ONLINE', 'expectedTimeZone': 'Europe/Warsaw',
                'contactEmail': ctx.world['actors'][actor]['email']}).status)
        threads = [threading.Thread(target=book, args=c) for c in cards]
        [t.start() for t in threads]
        [t.join() for t in threads]
        results.append({'case': 'booking: two clients book one slot at once', 'statuses': sorted(answers),
                        'verdict': 'FINDING' if sorted(answers).count(201) + sorted(answers).count(200) > 1 else 'protected'})
    out = RESULTS / f"payments-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    summary = {}
    for r in results:
        summary[r['verdict']] = summary.get(r['verdict'], 0) + 1
    print(json.dumps(summary, indent=1))
    for r in results:
        if r['verdict'] not in ('denied', 'protected'):
            print(r)
    print('->', out)


if __name__ == '__main__':
    guard()
    phase = sys.argv[1] if len(sys.argv) > 1 else 'attack'
    context = Ctx()
    {'checkout': checkout, 'status': status, 'invoices': invoices, 'bookings': bookings, 'attack': attack}[phase](context)
