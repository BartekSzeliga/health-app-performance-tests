#!/usr/bin/env python3
"""Races (I-2): requests released at the same moment (thread barrier), on top of the world from world.py.

R1  N users generate a diet preview at once, each with its own allergy and calorie target: each answer must carry
    the user's own target and no recipe that contains the user's allergen.
R2  Committing another user's previewId must fail; committing one's own previewId twice at once: at most one wins.
R3  Specialist and client write in the same conversation at once: both see every message once, in the same order.
R4  Double click on a package order: how many orders are created.
R5  Two saves of the same plan draft with the same write revision at once: one must lose (no silent overwrite).

Race accounts (iso-race-NN@perf.diecik.invalid) are kept in world.json and removed by world.py cleanup.
Usage: scripts/isolation/races.py [--users 12]
"""
import datetime as dt
import json
import secrets
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (API_BASE, EMAIL_DOMAIN, RESULTS, WORLD_FILE, KeycloakAdmin, Tokens, guard, load_world,  # noqa: E402
                    request, save_private)

TODAY = dt.date.today()


class Ctx:
    def __init__(self):
        self.world = load_world()
        self.passwords = {a['username']: a['password'] for a in self.world['actors'].values()}
        for race in self.world.get('race', {}).values():
            self.passwords[race['username']] = race['password']
        self.tokens = Tokens(self.passwords)

    def call(self, username, method, path, body=None, headers=None):
        return request(method, API_BASE + path, self.tokens.get(username), body, headers or {}, timeout=120)

    def actor(self, name):
        return self.world['actors'][name]['username']


def together(fns):
    """Runs the callables at the same moment and returns their results in order."""
    barrier = threading.Barrier(len(fns))

    def run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(len(fns)) as pool:
        return list(pool.map(run, fns))


def with_retry(fn, attempts=8):
    for _ in range(attempts):
        res = fn()
        if res.status not in (429, 503):
            return res
        time.sleep(min(int(res.headers.get('Retry-After', '5') or 5), 20))
    return res


def race_users(ctx, count):
    admin = KeycloakAdmin()
    race = ctx.world.setdefault('race', {})
    for i in range(1, count + 1):
        name = f'race-{i:02d}'
        user = race.get(name) or {'username': f'iso-{name}', 'email': f'iso-{name}{EMAIL_DOMAIN}',
                                  'password': secrets.token_urlsafe(18)}
        user['keycloakId'] = admin.ensure_user(user['username'], user['email'], user['password'], ['premium'])
        race[name] = user
        ctx.passwords[user['username']] = user['password']
    save_private(WORLD_FILE, ctx.world)
    ctx.tokens = Tokens(ctx.passwords)
    for name, user in race.items():
        ctx.call(user['username'], 'GET', '/api/users/user/details')
        ctx.call(user['username'], 'PUT', '/api/user-details', {
            'kcalPerDay': 2000, 'proteinPerDay': 110, 'carbsPerDay': 220, 'fatPerDay': 65, 'fiberPerDay': 28,
            'activity': 'MODERATE', 'goal': 'MAINTAIN', 'weight': '70', 'height': '172', 'age': '33',
            'gender': 'FEMALE', 'healthDataConsentAccepted': True})
        grant = ctx.call(ctx.actor('admin'), 'POST', '/api/admin/subscriptions/manual-grants',
                         {'email': user['email'], 'planCode': 'premium_monthly'})
        user['premium'] = grant.status
    return race


def r1_generation(ctx, race):
    options = [o['value'] for o in request('GET', API_BASE + '/api/public/diet-demo/allergy-options').json()]
    plans = {}
    for i, (name, user) in enumerate(sorted(race.items())):
        plans[name] = {'allergy': options[i % len(options)], 'calories': 1500 + 120 * i}
    start = str(TODAY + dt.timedelta(days=50))

    def generate(name):
        user, plan = race[name], plans[name]
        body = {'startDate': start, 'dietType': 'standard', 'mealsPerDay': 3, 'days': 2, 'allergies': [plan['allergy']],
                'tastePreferences': [], 'macros': {'calories': plan['calories'], 'protein': round(plan['calories'] / 16),
                                                   'carbs': round(plan['calories'] / 8), 'fat': round(plan['calories'] / 36),
                                                   'fiber': 28}}
        return lambda: with_retry(lambda: ctx.call(user['username'], 'POST', '/api/diet/generate/preview', body))

    names = sorted(race)
    answers = together([generate(n) for n in names])
    problems, preview_ids, checks = [], {}, {'targets': 0, 'recipes': 0}
    for name, res in zip(names, answers):
        plan = plans[name]
        if not res.ok:
            problems.append(f'{name}: HTTP {res.status} {res.body[:120]}')
            continue
        data = res.json()
        preview_ids[name] = data.get('previewId')
        for day in data.get('days') or []:
            target = ((day.get('userTarget') or {}).get('macrosPerDay') or {}).get('kcal')
            checks['targets'] += 1 if target is not None else 0
            if target is None or abs(float(target) - plan['calories']) > 1:
                problems.append(f"{name}: day target {target} kcal is not the user's {plan['calories']}")
            for meal in day.get('meals') or []:
                snap = meal.get('recipeSnapshot') or {}
                if 'containsAllergens' in snap:
                    checks['recipes'] += 1
                contains = [a.lower() for a in (snap.get('containsAllergens') or [])]
                if plan['allergy'].lower() in contains:
                    problems.append(f"{name}: meal '{meal.get('name')}' contains own allergen {plan['allergy']}")
    duplicates = len(preview_ids) - len(set(preview_ids.values()))
    if duplicates:
        problems.append(f'{duplicates} users got the same previewId')
    return {'users': len(names), 'ok': sum(1 for r in answers if r.ok), 'checked': checks, 'problems': problems}, preview_ids, start


def r2_preview(ctx, race, preview_ids, start):
    names = [n for n in sorted(race) if preview_ids.get(n)]
    if len(names) < 2:
        return {'skipped': 'not enough previews'}
    a, b = names[0], names[1]
    config = {'startDate': start, 'dietType': 'standard', 'mealsPerDay': 3, 'days': 2, 'allergies': [],
              'tastePreferences': [], 'macros': {'calories': 2000, 'protein': 125, 'carbs': 250, 'fat': 55, 'fiber': 28}}
    foreign = ctx.call(race[a]['username'], 'POST', '/api/diet/generate/commit',
                       {'previewId': preview_ids[b], 'replaceExisting': True, 'configuration': config, 'days': []})
    preview = ctx.call(race[a]['username'], 'POST', '/api/diet/generate/preview', config).json() or {}
    body = {'previewId': preview.get('previewId'), 'replaceExisting': True, 'configuration': config,
            'days': preview.get('days')}
    doubles = together([lambda: ctx.call(race[a]['username'], 'POST', '/api/diet/generate/commit', body)] * 2)
    return {'foreignCommit': foreign.status, 'foreignBody': foreign.body[:120],
            'doubleCommit': sorted(r.status for r in doubles)}


def r3_chat(ctx):
    card = ctx.world['resources']['card:spec-a:client-a1']['id']
    spec, client = ctx.actor('spec-a'), ctx.actor('client-a1')
    tag = secrets.token_hex(3)
    fns = []
    for i in range(15):
        for who, label in ((spec, 'S'), (client, 'C')):
            fns.append(lambda who=who, label=label, i=i: ctx.call(
                who, 'POST', f'/api/chat/conversations/{card}/messages',
                {'clientMessageId': str(uuid.uuid4()), 'body': f'race {tag} {label}{i:02d}'}))
    answers = together(fns)
    views = {}
    for who in (spec, client):
        messages = ctx.call(who, 'GET', f'/api/chat/conversations/{card}/messages').json() or []
        views[who] = [m['body'] for m in messages if f'race {tag} ' in (m.get('body') or '')]
    sent = sum(1 for r in answers if r.ok)
    same = views[spec] == views[client]
    return {'sent': sent, 'rateLimited': sum(1 for r in answers if r.status == 429), 'specialistSees': len(views[spec]),
            'clientSees': len(views[client]), 'sameOrder': same,
            'duplicates': len(views[spec]) - len(set(views[spec]))}


def r4_order(ctx):
    card = ctx.world['resources']['card:spec-a:client-a2']
    client = ctx.actor('client-a2')
    listing = ctx.call(client, 'GET', '/api/client/specialist-packages').json() or []
    offers = next((item['offers'] for item in listing if item.get('clientId') == card['id']), [])
    if not offers:
        return {'skipped': 'no offer'}
    body = {'clientId': card['id'], 'offerId': offers[0]['id'], 'termsVersion': '2026-11-15-r4',
            'acceptedTerms': True, 'requestedImmediateStart': False}
    answers = together([lambda: ctx.call(client, 'POST', '/api/client/specialist-packages/orders', body)] * 2)
    after = ctx.call(client, 'GET', '/api/client/specialist-packages').json() or []
    orders = next((item.get('orders') or [] for item in after if item.get('clientId') == card['id']), [])
    return {'statuses': sorted(r.status for r in answers), 'ordersAfter': len(orders)}


def r5_lost_update(ctx):
    spec = ctx.actor('spec-a')
    card = ctx.world['resources']['card:spec-a:client-a2']['id']
    start, end = TODAY + dt.timedelta(days=60), TODAY + dt.timedelta(days=62)
    draft = ctx.call(spec, 'PUT', f'/api/trainer/clients/{card}/diet-snapshot', {
        'title': 'Wyścig', 'periodStart': str(start), 'periodEnd': str(end),
        'dietSnapshot': [{'dayDate': str(start), 'meals': []}], 'writePrecondition': {'type': 'NO_EDITABLE_SNAPSHOT'}})
    if not draft.ok:
        return {'skipped': f'draft {draft.status} {draft.body[:100]}'}
    d = draft.json()

    def save(title):
        return lambda: ctx.call(spec, 'PUT', f'/api/trainer/clients/{card}/diet-snapshot', {
            'title': title, 'periodStart': str(start), 'periodEnd': str(end),
            'dietSnapshot': [{'dayDate': str(start), 'meals': []}],
            'writePrecondition': {'type': 'MATCH_SNAPSHOT', 'snapshotId': d['id'],
                                  'expectedWriteRevision': d.get('writeRevision', 0)}})
    answers = together([save('Wersja A'), save('Wersja B')])
    return {'statuses': sorted(r.status for r in answers)}


if __name__ == '__main__':
    guard()
    count = int(sys.argv[sys.argv.index('--users') + 1]) if '--users' in sys.argv else 12
    context = Ctx()
    users = race_users(context, count)
    r1, previews, start_date = r1_generation(context, users)
    print('R1 generation:', json.dumps(r1, ensure_ascii=False), flush=True)
    results = {'R1': r1, 'R2': r2_preview(context, users, previews, start_date), 'R3': r3_chat(context),
               'R4': r4_order(context), 'R5': r5_lost_update(context)}
    for key in ('R2', 'R3', 'R4', 'R5'):
        print(f'{key}:', json.dumps(results[key], ensure_ascii=False), flush=True)
    out = RESULTS / f"races-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print('->', out)
