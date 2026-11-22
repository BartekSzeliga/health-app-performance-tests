#!/usr/bin/env python3
"""Targeted write attacks with valid request bodies, for the writes the generic matrix cannot decide
(Spring validates the body before the service checks access, so a guessed body answers 400 for everyone).

Each case targets a side-A resource. Every attack body carries a unique marker; after the attacks the owner reads the
resource back and the marker must not be there. A positive control (the owner sending the same kind of request)
shows the body is valid, otherwise the case counts as inconclusive. Also: IDs passed in the body or the
X-Organization-Id header instead of the path.

Usage: scripts/isolation/writes.py
"""
import datetime as dt
import json
import secrets
import urllib.parse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import API_BASE, RESULTS, Tokens, guard, load_world, request  # noqa: E402

TODAY = dt.date.today()
SIDE_B = ['spec-b', 'org-b-owner', 'org-b-member', 'client-b1', 'user-y']


class Ctx:
    def __init__(self):
        self.world = load_world()
        self.tokens = Tokens({a['username']: a['password'] for a in self.world['actors'].values()})
        self.results = []

    def res(self, key):
        return self.world['resources'][key]

    def org(self, actor):
        for side in ('a', 'b'):
            if actor in (f'org-{side}-owner', f'org-{side}-member'):
                return self.res(f'org:{side}')['id']
        return None

    def call(self, actor, method, path, body=None, org=None):
        headers = {'Idempotency-Key': str(uuid.uuid4())} if method != 'GET' else {}
        if org:
            headers['X-Organization-Id'] = org
        return request(method, API_BASE + path, self.tokens.get(self.world['actors'][actor]['username']),
                       body, headers, timeout=30)

    def get(self, actor, path, org=None):
        res = self.call(actor, 'GET', path, org=org)
        return res.body if res.ok else ''


def marker():
    return f'ATTACK-{secrets.token_hex(5)}'


def run_case(ctx, name, method, path, body_fn, attackers, verify, owner=None, owner_org=None, control=True,
             own_result=None, denied=()):
    """body_fn(mark) builds a valid body; verify() returns the owner's view of the resource after the attacks.
    own_result(body) tells a 2xx that only touched the attacker's own data (e.g. a new own row) from a real hit."""
    attempts = []
    marks = []
    for attacker in attackers:
        for org in dict.fromkeys([None, ctx.org(attacker), owner_org]):
            mark = marker()
            marks.append(mark)
            res = ctx.call(attacker, method, path, body_fn(mark), org)
            attempts.append({'attacker': attacker, 'org': org, 'status': res.status, 'body': res.body[:200]})
    after = verify() if verify else ''
    tampered = [m for m in marks if m in after]
    owner_status = None
    if control and owner:
        res = ctx.call(owner, method, path, body_fn(f'OWNER-{secrets.token_hex(4)}'), owner_org)
        owner_status = res.status
    allowed = [a for a in attempts if 200 <= a['status'] < 300 and not (own_result and own_result(a['body']))]
    if tampered or allowed:
        verdict = 'FINDING'
    elif all(a['status'] in (401, 403, 404) or (own_result and 200 <= a['status'] < 300 and own_result(a['body']))
             or any(text in a['body'] for text in denied) for a in attempts):
        verdict = 'protected'
    elif owner_status is not None and 200 <= owner_status < 300:
        verdict = 'protected (4xx with a body the owner may send)'
    else:
        verdict = 'inconclusive'
    statuses = sorted({a['status'] for a in attempts})
    ctx.results.append({'case': name, 'method': method, 'path': path, 'verdict': verdict, 'ownerControl': owner_status,
                        'attackStatuses': statuses, 'tampered': tampered, 'allowed': allowed, 'attempts': attempts})
    print(f'{verdict:12.12} owner={owner_status} attackers={statuses}  {name}', flush=True)


def global_product(ctx):
    """Any approved catalogue product (shared by everyone), for request bodies that need an ingredient."""
    found = ctx.call('user-y', 'GET', '/api/products/search?nameFragment=jablko').json() or {}
    items = found if isinstance(found, list) else found.get('items') or found.get('content') or []
    return next((p['id'] for p in items if not p.get('ownerUserId') and not p.get('organizationId')), None)


def cases(ctx):
    w = ctx
    card_a1 = w.res('card:spec-a:client-a1')['id']
    card_a2 = w.res('card:spec-a:client-a2')['id']
    email_only_a = w.res('card:spec-a:email-only')['id']
    org_a = w.res('org:a')['id']
    card_org_a = w.res('card:org-a:org-a-client')['id']
    template_a = w.res('questionnaire-template:spec-a')['id']
    assignment_a = (w.res('questionnaire-assignment:spec-a') or {}).get('id')
    draft_a = w.res('snapshot-draft:spec-a')['id']
    published_a = w.res('snapshot:spec-a')['id']
    pair_a = w.res('pair:a')['id']
    product_x = w.res('product:user-x')['id']
    day_x = w.res('diet-day:user-x')
    shopping_x = w.res('shopping-list:user-x')['id']
    side_a_unrelated = ['client-a2', 'org-a-member', 'user-x']  # same side, but no access to client A1's file
    every = SIDE_B + side_a_unrelated

    # --- Clinical data of client A1 (specialist A's card) ---
    measurements = f'/api/trainer/clients/{card_a1}/care/measurements'
    run_case(w, 'care: add a measurement to a foreign client', 'POST', measurements,
             lambda m: {'measuredOn': str(TODAY), 'source': 'OFFICE', 'weightKg': 50, 'note': m}, every,
             lambda: w.get('spec-a', measurements), owner='spec-a')
    journal = f'/api/trainer/clients/{card_a1}/journal-entries'
    run_case(w, 'care: add a journal entry to a foreign client', 'POST', journal,
             lambda m: {'category': 'CONSULTATION', 'entryDate': str(TODAY), 'title': m, 'content': m}, every,
             lambda: w.call('spec-a', 'POST', journal + '/search', {}).body, owner='spec-a')
    run_case(w, 'questionnaire: send to a foreign client', 'POST', f'/api/trainer/clients/{card_a1}/questionnaires',
             lambda m: {'templateId': template_a, 'message': m}, every,
             lambda: w.get('spec-a', f'/api/trainer/clients/{card_a1}/questionnaires'), owner='spec-a')
    snapshots = f'/api/trainer/clients/{card_a1}/snapshots'
    run_case(w, 'plan: create a plan draft for a foreign client', 'PUT', f'/api/trainer/clients/{card_a1}/diet-snapshot',
             lambda m: {'title': m, 'periodStart': str(TODAY + dt.timedelta(days=40)),
                        'periodEnd': str(TODAY + dt.timedelta(days=46)),
                        'dietSnapshot': [{'dayDate': str(TODAY + dt.timedelta(days=40)), 'meals': []}],
                        'writePrecondition': {'type': 'NO_EDITABLE_SNAPSHOT'}}, SIDE_B + ['client-a1', 'client-a2'],
             lambda: w.get('spec-a', snapshots), owner=None)
    draft = json.loads(w.get('spec-a', f'{snapshots}/{draft_a}') or '{}')
    run_case(w, 'plan: publish a foreign draft', 'POST', f'{snapshots}/{draft_a}/publish-v2',
             lambda m: {'writePrecondition': {'type': 'MATCH_SNAPSHOT', 'snapshotId': draft_a,
                                              'expectedWriteRevision': draft.get('writeRevision', 0)},
                        'expectedCurrentPublishedSnapshotId': published_a}, SIDE_B + ['client-a1'],
             None, control=False)

    # --- Chat of client A1 ---
    messages = f'/api/chat/conversations/{card_a1}/messages'
    run_case(w, 'chat: write into a foreign conversation', 'POST', messages,
             lambda m: {'clientMessageId': str(uuid.uuid4()), 'body': m}, every,
             lambda: w.get('spec-a', messages), owner='spec-a')
    latest = (json.loads(w.get('spec-a', messages) or '[]') or [{}])[0].get('id')
    run_case(w, 'chat: mark a foreign conversation as read', 'PUT', f'/api/chat/conversations/{card_a1}/read',
             lambda m: {'messageId': latest}, every, None, owner='spec-a')
    org_messages = f'/api/chat/conversations/{card_org_a}/messages'
    run_case(w, 'chat: write into a company conversation of another company', 'POST', org_messages,
             lambda m: {'clientMessageId': str(uuid.uuid4()), 'body': m}, SIDE_B + ['org-a-member', 'client-a1'],
             lambda: w.get('org-a-owner', org_messages, org_a), owner='org-a-owner', owner_org=org_a)

    # --- Client side: questionnaire answers of client A1 ---
    if assignment_a:
        client_q = f'/api/client/questionnaires/{assignment_a}'
        run_case(w, 'questionnaire: answer a foreign assignment', 'PUT', client_q + '/draft',
                 lambda m: {'answers': {'q1': m}, 'version': 0}, SIDE_B + ['client-a2', 'spec-a'],
                 lambda: w.get('client-a1', client_q), owner='client-a1')

    # --- Individual data of user X ---
    product = json.loads(w.get('user-x', f'/api/products/{product_x}') or '{}')
    run_case(w, 'product: change a foreign private product', 'PUT', f'/api/products/{product_x}',
             lambda m: dict(product, name=m), SIDE_B + ['client-a1', 'spec-a'],
             lambda: w.get('user-x', f'/api/products/{product_x}'), owner='user-x')
    run_case(w, 'product: delete a foreign private product', 'DELETE', f'/api/products/{product_x}', lambda m: None,
             SIDE_B + ['client-a1'], lambda: w.get('user-x', f'/api/products/{product_x}') or 'GONE', control=False)
    recipes = json.loads(w.get('user-x', '/api/recipes/mine') or '{}')
    items = recipes.get('items') or recipes.get('content') or (recipes if isinstance(recipes, list) else [])
    if items:
        recipe_id = items[0]['id']
        recipe = json.loads(w.get('user-x', f'/api/recipes/mine/{recipe_id}') or '{}')
        body = {k: recipe.get(k) for k in ('description', 'category', 'tags', 'dietTypes', 'aliases', 'baseServings',
                                            'steps', 'tips', 'containsAllergens', 'mayContainAllergens')}
        version_id = recipe.get('currentVersionId') or recipe.get('versionId')
        body['ingredients'] = [{'position': 1, 'productId': global_product(w), 'grams': 100, 'optional': False}]
        body['steps'] = [{'position': 1, 'text': 'Krok'}]
        body['baseServings'] = body.get('baseServings') or 1
        body['category'] = body.get('category') or 'DINNER'
        body['expectedCurrentVersionId'] = version_id
        run_case(w, 'recipe: change a foreign private recipe', 'PUT', f'/api/recipes/mine/{recipe_id}',
                 lambda m: dict(body, name=m), SIDE_B + ['client-a1', 'spec-a'],
                 lambda: w.get('user-x', f'/api/recipes/mine/{recipe_id}'), owner='user-x')
        run_case(w, 'recipe: delete a foreign private recipe', 'DELETE',
                 f'/api/recipes/mine/{recipe_id}?expectedCurrentVersionId={version_id}',
                 lambda m: None, SIDE_B, lambda: w.get('user-x', f'/api/recipes/mine/{recipe_id}') or 'GONE',
                 control=False)
    run_case(w, 'diet: overwrite a foreign day (ID in the body)', 'PUT', '/api/diet-day',
             lambda m: {'id': day_x['id'], 'name': m, 'dayDate': day_x['dayDate'], 'meals': []},
             SIDE_B + ['client-a1', 'spec-a'], lambda: w.get('user-x', f"/api/diet-day?date={day_x['dayDate']}"),
             control=False, own_result=lambda body: day_x['id'] not in body)
    shopping = json.loads(w.get('user-x', f'/api/shopping-lists/{shopping_x}') or '{}')
    version = shopping.get('version') or (shopping.get('list') or {}).get('version') or 0
    run_case(w, 'shopping: add an item to a foreign list', 'POST', f'/api/shopping-lists/{shopping_x}/custom-items',
             lambda m: {'id': str(uuid.uuid4()), 'name': m, 'quantity': 1, 'unit': 'szt', 'checked': False,
                        'expectedVersion': version},
             SIDE_B + ['client-a1', 'spec-a'], lambda: w.get('user-x', f'/api/shopping-lists/{shopping_x}'),
             owner='user-x')
    plans = json.loads(w.get('user-x', '/api/training-plans') or '[]')
    plan_items = plans if isinstance(plans, list) else plans.get('items') or plans.get('plans') or []
    if plan_items:
        plan_id = plan_items[0]['id']
        run_case(w, 'training: delete a foreign plan', 'POST', f'/api/training-plans/{plan_id}/delete', lambda m: {},
                 SIDE_B + ['client-a1', 'spec-a'], lambda: w.get('user-x', f'/api/training-plans/{plan_id}') or 'GONE',
                 control=False, denied=('Plan not found',))

    # --- Company A product (company catalogue) ---
    org_product = w.call('org-a-owner', 'POST', '/api/products', {
        'name': f"Produkt firmy {w.world['actors']['org-a-owner']['canary']}", 'kcal': 90, 'protein': 4, 'carbs': 12,
        'fat': 2, 'fiber': 1}, org_a)
    if org_product.ok:
        pid = org_product.json()['id']
        current = json.loads(w.get('org-a-owner', f'/api/products/{pid}', org_a) or '{}')
        run_case(w, 'company product: change a product of another company', 'PUT', f'/api/products/{pid}',
                 lambda m: dict(current, name=m, expectedUpdatedAt=current.get('updatedAt')),
                 ['org-b-owner', 'org-b-member', 'spec-b', 'user-y', 'org-a-client'],
                 lambda: w.get('org-a-owner', f'/api/products/{pid}', org_a), owner='org-a-owner', owner_org=org_a)
        run_case(w, 'company product: delete a product of another company', 'DELETE',
                 f"/api/products/{pid}?expectedUpdatedAt={urllib.parse.quote(current.get('updatedAt') or '')}",
                 lambda m: None, ['org-b-owner', 'org-b-member', 'spec-b', 'user-y', 'org-a-client'],
                 lambda: w.get('org-a-owner', f'/api/products/{pid}', org_a) or 'GONE', control=False)

    # --- Company A settings ---
    features = f'/api/company-app/organizations/{org_a}/features'
    run_case(w, 'company: switch off chat in another company', 'PUT', features,
             lambda m: {'features': {'chat': False}}, SIDE_B + ['org-a-member', 'org-a-client', 'spec-a'],
             lambda: w.get('org-a-owner', features), control=False)
    run_case(w, 'company: change client features in another company', 'PUT',
             f'/api/company-app/organizations/{org_a}/clients/{card_org_a}/features',
             lambda m: {'features': {'chat': False}}, SIDE_B + ['org-a-member', 'org-a-client'], None, control=False)

    # --- IDs in the body or header ---
    run_case(w, 'pair: create a pair from foreign cards (IDs in the body)', 'POST', '/api/trainer/pairs',
             lambda m: {'clientIds': [card_a1, card_a2]}, ['spec-b', 'org-b-owner', 'client-a1'],
             lambda: w.get('spec-a', f'/api/trainer/pairs/{pair_a}'), control=False)
    run_case(w, 'booking: book for a foreign client card (ID in the body)', 'POST', '/api/booking-requests',
             lambda m: {'clientId': card_a1, 'providerUserId': w.world['actors']['spec-a']['keycloakId'],
                        'date': str(TODAY + dt.timedelta(days=5)), 'startTime': '12:00', 'visitMode': 'ONLINE',
                        'expectedTimeZone': 'Europe/Warsaw', 'contactEmail': 'x@perf.diecik.invalid', 'note': m},
             ['client-b1', 'user-y', 'client-a2'], None, control=False)
    run_case(w, 'shopping: list from a foreign published plan (ID in the body)', 'POST', '/api/shopping-lists/resolve',
             lambda m: {'sourceType': 'TRAINER_PLAN', 'sourceId': published_a, 'dateFrom': str(TODAY + dt.timedelta(days=10)),
                        'dateTo': str(TODAY + dt.timedelta(days=11))}, ['client-b1', 'user-y', 'client-a2', 'spec-b'],
             None, control=False)
    run_case(w, 'card: take over a foreign e-mail-only client card', 'POST',
             f'/api/trainer/client-links/{email_only_a}/link-account', lambda m: None,
             ['user-y', 'client-b1', 'user-x', 'client-a2'], lambda: w.get('spec-a', f'/api/trainer/clients/{email_only_a}'),
             control=False, denied=('ten sam email co zaproszenie',))

    # --- Foreign X-Organization-Id on list routes (no ID in the path) ---
    routes = json.loads((RESULTS / 'routes.json').read_text())['routes']
    canaries_a = [a['canary'] for a in w.world['actors'].values() if a['side'] == 'A']
    for route in routes:
        if route['method'] != 'GET' or route['pathVars'] or not route['orgHeader'] \
                or any(q['required'] for q in route['query']):
            continue
        for attacker in ('org-b-owner', 'spec-b', 'user-y', 'org-a-member'):
            res = w.call(attacker, 'GET', route['path'], org=org_a)
            leaked = [c for c in canaries_a if c in res.body]
            verdict = 'FINDING' if res.ok and leaked else ('protected' if not res.ok or not leaked else 'protected')
            if attacker == 'org-a-member' and res.ok and leaked:
                verdict = 'review (same company)'
            w.results.append({'case': f'foreign X-Organization-Id: GET {route["path"]}', 'attacker': attacker,
                              'status': res.status, 'verdict': verdict, 'leaked': leaked})
            if verdict != 'protected':
                print(f'{verdict:12.12} {attacker} {res.status} GET {route["path"]} leaked={leaked}', flush=True)


if __name__ == '__main__':
    guard()
    context = Ctx()
    cases(context)
    out = RESULTS / f"writes-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(context.results, indent=1, ensure_ascii=False))
    summary = {}
    for r in context.results:
        summary[r['verdict']] = summary.get(r['verdict'], 0) + 1
    print(json.dumps(summary, indent=1), '->', out)
