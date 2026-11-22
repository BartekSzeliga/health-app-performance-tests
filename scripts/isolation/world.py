#!/usr/bin/env python3
"""Builds the test "world" for the isolation runs: two mirrored sides (A and B), each with an independent specialist
and their clients, a company with its own app, and an individual user; plus an admin. Every owner creates data in
each area through the API, like the apps do. Text fields carry a per-actor canary (CANARY-<actor>-<random>) so a
leak shows up in a response body even when IDs look harmless.

Writes results/isolation/world.json (0600: passwords). Rerunning reuses accounts and adds data.

Usage: scripts/isolation/world.py build | cleanup
"""
import datetime as dt
import email
import hashlib
import os
import re
import secrets
import sys
import time
import traceback
import uuid
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (API_BASE, CLIENT_ID, EMAIL_DOMAIN, RESULTS, WORLD_FILE, KeycloakAdmin, Tokens, db_execute,  # noqa: E402
                    guard, load_world, request, save_private)

PRO_PLAN = 'trainer_pro_monthly'
TERMS_VERSION = '2026-11-15-r4'  # TermsPolicy.CURRENT_VERSION in the backend
TODAY = dt.date.today()

# name: (side, kind, realm roles)
ACTORS = {
    'admin': (None, 'admin', ['admin']),
    'spec-a': ('A', 'specialist', []), 'spec-b': ('B', 'specialist', []),
    'client-a1': ('A', 'client', []), 'client-a2': ('A', 'client', []), 'client-b1': ('B', 'client', []),
    'client-b2': ('B', 'client', []),
    'org-a-owner': ('A', 'specialist', []), 'org-a-member': ('A', 'specialist', []), 'org-a-client': ('A', 'client', []),
    'org-b-owner': ('B', 'specialist', []), 'org-b-member': ('B', 'specialist', []), 'org-b-client': ('B', 'client', []),
    'user-x': ('A', 'individual', ['premium']), 'user-y': ('B', 'individual', ['premium']),
}


class World:
    def __init__(self):
        self.data = load_world() if WORLD_FILE.exists() else {'actors': {}, 'resources': {}, 'log': []}
        self.tokens = None

    def save(self):
        save_private(WORLD_FILE, self.data)

    # --- accounts -------------------------------------------------------------------------------------------
    def accounts(self):
        admin = KeycloakAdmin()
        admin.enable_password_grant(CLIENT_ID)
        for name, (side, kind, roles) in ACTORS.items():
            actor = self.data['actors'].get(name) or {
                'username': f'iso-{name}', 'email': f'iso-{name}{EMAIL_DOMAIN}',
                'password': secrets.token_urlsafe(18), 'canary': f'CANARY-{name}-{secrets.token_hex(4)}'}
            actor.update(side=side, kind=kind)
            actor['keycloakId'] = admin.ensure_user(actor['username'], actor['email'], actor['password'], roles)
            self.data['actors'][name] = actor
        self.tokens = Tokens({a['username']: a['password'] for a in self.data['actors'].values()})
        for name in self.data['actors']:
            res = self.call(name, 'GET', '/api/users/user/details')
            self.data['actors'][name]['userId'] = (res.json() or {}).get('id')
        self.save()

    def call(self, actor, method, path, body=None, org=None, expect_ok=False, headers=None):
        token = self.tokens.get(self.data['actors'][actor]['username'])
        headers = dict(headers or {})
        if org:
            headers['X-Organization-Id'] = org
        res = request(method, API_BASE + path, token, body, headers)
        if expect_ok and not res.ok:
            raise RuntimeError(f'{actor} {method} {path}: {res}')
        return res

    def ok(self, actor, method, path, body=None, org=None, headers=None):
        return self.call(actor, method, path, body, org, expect_ok=True, headers=headers).json()

    def canary(self, actor):
        return self.data['actors'][actor]['canary']

    def put(self, key, value):
        self.data['resources'][key] = value
        self.save()
        return value

    def res(self, key):
        return self.data['resources'].get(key)

    def step(self, name, fn):
        try:
            fn()
            self.data['log'].append({'step': name, 'ok': True})
            print(f'ok    {name}', flush=True)
        except Exception as error:  # noqa: BLE001 — keep building the rest of the world
            self.data['log'].append({'step': name, 'ok': False, 'error': str(error)[:500]})
            print(f'FAIL  {name}: {str(error)[:300]}', flush=True)
            if '--trace' in sys.argv:
                traceback.print_exc()
        self.save()

    # --- tokens -----------------------------------------------------------------------------------------------
    def invitation_token(self, kind, ref, recipient, after, pattern=r'token=([A-Za-z0-9_-]{20,})'):
        """The invitation token: from the local mail sink, or, on AWS dev (mail off, ISO_DB_PORT set), a known token
        written as the hash of the open invitation of a test address. Accepting and linking still use the API."""
        if not os.environ.get('ISO_DB_PORT'):
            return self.mail_token(recipient, pattern=pattern, after=after)
        if not recipient.endswith(EMAIL_DOMAIN):
            raise RuntimeError(f'not a test address: {recipient}')
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        if kind == 'client':
            sql = (f"UPDATE trainer_client_email_tokens t SET token_hash = '{digest}' FROM trainer_clients c "
                   f"WHERE t.client_id = c.id AND c.id = '{ref}' AND c.email LIKE '%{EMAIL_DOMAIN}' "
                   f"AND t.purpose = 'VERIFY_CLIENT_EMAIL' AND t.accepted_at IS NULL AND t.declined_at IS NULL")
        else:
            sql = (f"UPDATE organization_invitations SET token_hash = '{digest}' WHERE organization_id = '{ref}' "
                   f"AND lower(email) = lower('{recipient}') AND status = 'PENDING' AND email LIKE '%{EMAIL_DOMAIN}'")
        result = db_execute(sql)
        if not result.endswith('1'):
            raise RuntimeError(f'no open {kind} invitation for {recipient}: {result}')
        return raw

    def mail_token(self, recipient, pattern=r'token=([A-Za-z0-9_-]{20,})', after=0.0):
        for _ in range(30):
            for file in sorted((RESULTS / 'mail').glob('*.eml'), reverse=True):
                if int(file.stem) / 1e9 < after:
                    break
                msg = email.message_from_bytes(file.read_bytes(), policy=policy.default)
                if recipient.lower() not in str(msg['To']).lower():
                    continue
                body = ' '.join(part.get_content() for part in msg.walk()
                                if part.get_content_type() in ('text/plain', 'text/html'))
                found = re.findall(pattern, body)
                if found:
                    return found[-1]
            time.sleep(0.5)
        raise RuntimeError(f'No mail with a token for {recipient}')


def setup_specialist(w, name, org=None):
    dpa = w.ok(name, 'GET', '/api/trainer/dpa')
    if not dpa.get('accepted'):
        w.ok(name, 'PUT', '/api/trainer/dpa/acceptance', {
            'version': dpa['currentVersion'], 'documentSha256': dpa.get('documentSha256'),
            'controllerName': f'Gabinet {w.canary(name)}', 'controllerAddress': 'ul. Testowa 1, 00-001 Warszawa',
            'controllerTaxId': '5260250274', 'representativeConfirmed': True})
    if not w.res(f'granted:{name}'):
        grant = w.call('admin', 'POST', '/api/admin/subscriptions/manual-grants',
                       {'email': w.data['actors'][name]['email'], 'planCode': PRO_PLAN})
        if not grant.ok and 'already' not in grant.body:
            raise RuntimeError(f'grant {name}: {grant}')
        w.put(f'granted:{name}', True)
    w.ok(name, 'PUT', '/api/trainer/profile', {
        'displayName': f'Spec {name} {w.canary(name)}', 'businessName': f'Gabinet {w.canary(name)}',
        'contactEmail': w.data['actors'][name]['email'], 'pdfBrandingMode': 'DIECIK', 'workScope': 'GABINET'})


def accept_chat_policy(w, name):
    policy_status = w.ok(name, 'GET', '/api/chat/policy')
    if not policy_status.get('accepted'):
        w.ok(name, 'PUT', '/api/chat/policy/acceptance',
             {'version': policy_status.get('currentVersion') or policy_status.get('version')})


def link_client(w, specialist, client, key, org=None):
    """Card for the client's e-mail, verification mail, accept and link as the client (company: with confirmation)."""
    email_addr = w.data['actors'][client]['email']
    existing = w.res(key)
    if existing and existing.get('status') == 'LINKED':
        return existing
    w.ok(client, 'GET', '/api/user-details')
    started = time.time()
    if org:
        listing = w.ok(specialist, 'GET', f'/api/organizations/{org}/clients')
        items = listing if isinstance(listing, list) else listing.get('items') or listing.get('content') or []
        card = next((c for c in items if c.get('email') == email_addr), None) or w.ok(
            specialist, 'POST', f'/api/organizations/{org}/clients',
            {'displayName': f'Klient {w.canary(client)}', 'email': email_addr, 'phone': None})
        w.ok(specialist, 'POST', f"/api/organizations/{org}/clients/{card['id']}/invite")
        token = w.invitation_token('client', card['id'], email_addr, started - 1)
        w.call(client, 'POST', f'/api/trainer/client-invitations/{token}/accept')
        w.ok(client, 'POST', f'/api/trainer/client-invitations/{token}/link-account', {
            'confirmedOrganizationId': org,
            'confirmedAuthorizedPeople': [w.data['actors'][specialist]['keycloakId']]})
    else:
        listing = w.ok(specialist, 'GET', '/api/trainer/clients')
        items = listing if isinstance(listing, list) else listing.get('items') or listing.get('content') or []
        card = next((c for c in items if c.get('email') == email_addr), None) or w.ok(
            specialist, 'POST', '/api/trainer/clients', {
                'displayName': f'Klient {w.canary(client)}', 'email': email_addr, 'goal': f'Cel {w.canary(client)}',
                'profile': {'gender': 'FEMALE', 'age': 34, 'weightKg': 68, 'heightCm': 168, 'activity': 'MODERATE',
                            'nutritionGoal': 'MAINTAIN'}})
        w.ok(specialist, 'POST', f"/api/trainer/clients/{card['id']}/email-verification")
        token = w.invitation_token('client', card['id'], email_addr, started - 1)
        w.call(client, 'POST', f'/api/trainer/client-invitations/{token}/accept')
        w.ok(client, 'POST', f"/api/trainer/client-links/{card['id']}/link-account")
    return w.put(key, {'id': card['id'], 'status': 'LINKED', 'specialist': specialist, 'client': client,
                       'organizationId': org})


def build(w):
    guard()
    w.step('accounts', w.accounts)
    for spec in ('spec-a', 'spec-b', 'org-a-owner', 'org-a-member', 'org-b-owner', 'org-b-member'):
        w.step(f'specialist {spec}', lambda spec=spec: setup_specialist(w, spec))
    for name in ACTORS:
        if name != 'admin':
            w.step(f'chat policy {name}', lambda name=name: accept_chat_policy(w, name))

    # Independent practices: one linked client each plus a second one (pair on side A, plain on side B).
    w.step('link client-a1', lambda: link_client(w, 'spec-a', 'client-a1', 'card:spec-a:client-a1'))
    w.step('link client-a2', lambda: link_client(w, 'spec-a', 'client-a2', 'card:spec-a:client-a2'))
    w.step('link client-b1', lambda: link_client(w, 'spec-b', 'client-b1', 'card:spec-b:client-b1'))
    w.step('link client-b2', lambda: link_client(w, 'spec-b', 'client-b2', 'card:spec-b:client-b2'))
    for spec in ('spec-a', 'spec-b'):
        w.step(f'email-only card {spec}', lambda spec=spec: email_only_card(w, spec))

    for side in ('a', 'b'):
        w.step(f'organization {side}', lambda side=side: organization(w, side))

    for name in ('client-a1', 'client-b1', 'user-x', 'user-y', 'org-a-client', 'org-b-client'):
        w.step(f'health data {name}', lambda name=name: health_data(w, name))
        w.step(f'diet {name}', lambda name=name: diet(w, name))
        w.step(f'training {name}', lambda name=name: training(w, name))
    for spec, card in (('spec-a', 'card:spec-a:client-a1'), ('spec-b', 'card:spec-b:client-b1')):
        w.step(f'chat {spec}', lambda spec=spec, card=card: chat(w, spec, card))
        w.step(f'care {spec}', lambda spec=spec, card=card: care(w, spec, card))
        w.step(f'questionnaire {spec}', lambda spec=spec, card=card: questionnaire(w, spec, card))
        w.step(f'offers and booking {spec}', lambda spec=spec, card=card: offers_booking(w, spec, card))
        w.step(f'private recipe {spec}', lambda spec=spec: private_recipe(w, spec))
    w.step('pair spec-a', lambda: pair(w, 'spec-a', 'card:spec-a:client-a1', 'card:spec-a:client-a2', 'pair:a'))
    w.step('pair spec-b', lambda: pair(w, 'spec-b', 'card:spec-b:client-b1', 'card:spec-b:client-b2', 'pair:b'))
    for name in ('user-x', 'user-y', 'client-a1', 'client-b1'):
        w.step(f'diet meal {name}', lambda name=name: diet_meal(w, name))
        w.step(f'training template {name}', lambda name=name: training_template(w, name))
        w.step(f'shopping list {name}', lambda name=name: shopping_list(w, name))
    for spec, card in (('spec-a', 'card:spec-a:client-a1'), ('spec-b', 'card:spec-b:client-b1')):
        w.step(f'plan snapshot {spec}', lambda spec=spec, card=card: plan_snapshot(w, spec, card))
    for side in ('a', 'b'):
        w.step(f'pair plan {side}', lambda side=side: pair_plan(w, side))
    # Round 2: data for routes the first round could not reach.
    for spec, card in (('spec-a', 'card:spec-a:client-a1'), ('spec-b', 'card:spec-b:client-b1')):
        w.step(f'generated plan {spec}', lambda spec=spec, card=card: generated_plan(w, spec, card))
        w.step(f'booking settings {spec}', lambda spec=spec: w.ok(spec, 'PUT', '/api/trainer/booking-settings', {
            'enabled': True, 'onlineEnabled': True, 'onsiteEnabled': False, 'onsiteAddress': None}))
        w.step(f'follow-up {spec}', lambda spec=spec, card=card: follow_up(w, spec, card))
        w.step(f'package order {spec}', lambda spec=spec, card=card: package_order(w, spec, card))
    for name in ('user-x', 'user-y'):
        w.step(f'own generation {name}', lambda name=name: own_generation(w, name))
        w.step(f'recipe draft {name}', lambda name=name: recipe_draft(w, name))
        w.step(f'community submission {name}', lambda name=name: community_submission(w, name))
        w.step(f'training session {name}', lambda name=name: training_session(w, name))
    failed = [entry['step'] for entry in w.data['log'][-200:] if not entry['ok']]
    print(f'world saved to {WORLD_FILE}; failed steps this run: {len(set(failed))}')


def email_only_card(w, spec):
    key = f'card:{spec}:email-only'
    if w.res(key):
        return
    card = w.ok(spec, 'POST', '/api/trainer/clients', {
        'displayName': f'Tylko e-mail {w.canary(spec)}', 'email': f'iso-emailonly-{spec}{EMAIL_DOMAIN}',
        'goal': f'Cel {w.canary(spec)}', 'profile': {'gender': 'MALE', 'age': 41, 'weightKg': 82, 'heightCm': 181,
                                                    'activity': 'LIGHT', 'nutritionGoal': 'LOSE_WEIGHT'}})
    w.put(key, {'id': card['id'], 'specialist': spec})


def organization(w, side):
    owner, member, client = f'org-{side}-owner', f'org-{side}-member', f'org-{side}-client'
    org = w.res(f'org:{side}')
    if not org:
        created = w.ok(owner, 'POST', '/api/organizations',
                       {'name': f'Firma {side.upper()} {w.canary(owner)}', 'contactEmail': w.data['actors'][owner]['email'],
                        'phone': None})
        org = w.put(f'org:{side}', {'id': created['organization']['id'], 'owner': owner})
    org_id = org['id']
    dpa = w.ok(owner, 'GET', f'/api/organizations/{org_id}/dpa')
    if not dpa.get('accepted'):
        w.ok(owner, 'PUT', f'/api/organizations/{org_id}/dpa/acceptance', {
            'version': dpa['currentVersion'], 'documentSha256': dpa.get('documentSha256'),
            'controllerName': f'Firma {w.canary(owner)}', 'controllerAddress': 'ul. Firmowa 2, 00-002 Warszawa',
            'controllerTaxId': '5260250274', 'representativeConfirmed': True})
    if not w.res(f'org:{side}:member'):
        # A pending invitation from an earlier attempt holds a seat of the free plan: cancel it first.
        pending = w.ok(owner, 'GET', f'/api/organizations/{org_id}/invitations') or []
        for invitation in pending if isinstance(pending, list) else pending.get('items', []):
            if invitation.get('email') == w.data['actors'][member]['email'] and invitation.get('id'):
                w.call(owner, 'POST', f"/api/organizations/{org_id}/invitations/{invitation['id']}/cancel",
                       {'revision': invitation.get('revision', 0)})
        started = time.time()
        w.ok(owner, 'POST', f'/api/organizations/{org_id}/invitations',
             {'email': w.data['actors'][member]['email'], 'role': 'DIETITIAN', 'clinicalAccess': False,
              'patientAccess': 'ASSIGNED'})
        token = w.invitation_token('org', org_id, w.data['actors'][member]['email'], started - 1,
                                   pattern=r'(?:token=|invitations?/|zaproszeni[ae]/)([A-Za-z0-9_-]{20,})')
        w.ok(member, 'PUT', f'/api/organization-invitations/{token}/profile', {
            'displayName': f'Członek {w.canary(member)}', 'businessName': f'Firma {side.upper()}',
            'contactEmail': w.data['actors'][member]['email'], 'pdfBrandingMode': 'DIECIK', 'workScope': 'GABINET'})
        w.ok(member, 'POST', f'/api/organization-invitations/{token}')
        w.put(f'org:{side}:member', {'userId': w.data['actors'][member]['keycloakId']})
    app_client = f'iso-company-{side}'
    KeycloakAdmin().ensure_public_client(app_client)
    current_app = (w.call('admin', 'GET', f'/api/admin/company-app/organizations/{org_id}').json() or {}).get('app') or {}
    w.ok('admin', 'PUT', f'/api/admin/company-app/organizations/{org_id}/app',
         {'keycloakClientId': app_client, 'bundleId': f'pl.diecik.iso.{side}', 'status': 'ACTIVE', 'brand': None,
          'revision': current_app.get('revision')})
    w.ok('admin', 'PUT', f'/api/admin/company-app/organizations/{org_id}/allowances', {'features': {'chat': True}})
    w.ok(owner, 'PUT', f'/api/company-app/organizations/{org_id}/features', {'features': {'chat': True}})
    card = link_client(w, owner, client, f'card:org-{side}:{client}', org=org_id)
    if not w.res(f'org:{side}:task'):
        task = w.ok(owner, 'POST', f"/api/organizations/{org_id}/clients/{card['id']}/tasks",
                    {'content': f'Zadanie {w.canary(owner)}', 'assigneeUserId': w.data['actors'][owner]['keycloakId'],
                     'dueOn': str(TODAY + dt.timedelta(days=3))})
        w.put(f'org:{side}:task', {'id': task.get('id') if isinstance(task, dict) else None})
    w.ok(owner, 'POST', f"/api/chat/conversations/{card['id']}/messages",
         {'clientMessageId': str(uuid.uuid4()), 'body': f'Firma pisze {w.canary(owner)}'}, org=org_id)
    w.ok(client, 'POST', f"/api/chat/conversations/{card['id']}/messages",
         {'clientMessageId': str(uuid.uuid4()), 'body': f'Klient firmy {w.canary(client)}'})


def health_data(w, name):
    canary = w.canary(name)
    w.ok(name, 'PUT', '/api/user-details', {
        'kcalPerDay': 2100, 'proteinPerDay': 120, 'carbsPerDay': 230, 'fatPerDay': 70, 'fiberPerDay': 30,
        'activity': 'MODERATE', 'goal': 'MAINTAIN', 'weight': '70', 'height': '170', 'age': '35', 'gender': 'FEMALE',
        'healthDataConsentAccepted': True})
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    w.ok(name, 'POST', '/api/user-weights', {'weight': '70.4', 'measuredAt': now.isoformat(), 'timeZone': 'Europe/Warsaw'})
    w.ok(name, 'POST', '/api/user-measurements', {'waistCm': 80, 'hipsCm': 98, 'note': f'Pomiar {canary}',
                                                  'measuredAt': now.isoformat(), 'timeZone': 'Europe/Warsaw'})
    w.ok(name, 'POST', '/api/user-measurement-sessions', {
        'requestId': str(uuid.uuid4()), 'measuredAt': (now - dt.timedelta(hours=1)).isoformat(), 'weightKg': 70.1,
        'waistCm': 79, 'bodyFatPercent': 24.5, 'note': f'Sesja {canary}', 'timeZone': 'Europe/Warsaw'})
    w.ok(name, 'PUT', f'/api/water-intake/{uuid.uuid4()}', {'date': str(TODAY), 'amountMl': 250})


def diet(w, name):
    canary = w.canary(name)
    if w.res(f'diet-done:{name}'):
        return
    product = w.ok(name, 'POST', '/api/products', {
        'name': f'Produkt {canary}', 'kcal': 120, 'protein': 10, 'carbs': 12, 'fat': 3, 'fiber': 2})
    w.put(f'product:{name}', {'id': product.get('id')})
    day = w.call(name, 'POST', '/api/diet-day', {'dayDate': str(TODAY + dt.timedelta(days=1)), 'name': f'Dzień {canary}'})
    if day.ok:
        w.put(f'diet-day:{name}', day.json())
    recipe = w.call(name, 'POST', '/api/recipes/mine', {
        'name': f'Przepis {canary}', 'description': f'Opis {canary}', 'category': 'DINNER', 'baseServings': 1,
        'steps': [{'position': 1, 'text': f'Krok {canary}'}],
        'ingredients': [{'position': 1, 'productId': product.get('id'), 'grams': 100, 'optional': False}],
        'tags': [], 'dietTypes': [], 'aliases': [],
        'tips': [], 'containsAllergens': [], 'mayContainAllergens': []})
    if not recipe.ok and recipe.status != 409:
        raise RuntimeError(f'recipe {name}: {recipe}')
    w.put(f'diet-done:{name}', True)


def training(w, name):
    canary = w.canary(name)
    plan = w.ok(name, 'POST', '/api/training-plans/manual', {
        'name': f'Plan {canary}', 'goal': 'GENERAL_FITNESS', 'daysPerWeek': 1, 'sessionLengthMinutes': 30,
        'startDate': str(TODAY), 'days': [{'dayIndex': 1, 'focus': f'Dzień {canary}', 'userNotes': canary,
                                           'exercises': [{'name': f'Przysiad {canary}', 'sets': 3, 'reps': 10}]}]})
    w.put(f'training-plan:{name}', {'id': plan.get('id') if isinstance(plan, dict) else None})


def chat(w, spec, card_key):
    card = w.res(card_key)
    w.ok(spec, 'POST', f"/api/chat/conversations/{card['id']}/messages",
         {'clientMessageId': str(uuid.uuid4()), 'body': f'Specjalista pisze {w.canary(spec)}'})
    w.ok(card['client'], 'POST', f"/api/chat/conversations/{card['id']}/messages",
         {'clientMessageId': str(uuid.uuid4()), 'body': f'Klient pisze {w.canary(card["client"])}'})


def care(w, spec, card_key):
    card = w.res(card_key)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    w.ok(spec, 'POST', f"/api/trainer/clients/{card['id']}/care/measurements", {
        'measuredOn': str(TODAY), 'source': 'OFFICE', 'weightKg': 69.5, 'waistCm': 78,
        'note': f'Gabinet {w.canary(spec)}'})


def questionnaire(w, spec, card_key):
    card = w.res(card_key)
    if w.res(f'questionnaire-assignment:{spec}'):
        return
    template = w.ok(spec, 'POST', '/api/trainer/questionnaire-templates', {
        'name': f'Ankieta {w.canary(spec)}', 'description': 'Opis',
        'definition': {'sections': [{'title': 'Sekcja', 'questions': [
            {'key': 'q1', 'type': 'SHORT_TEXT', 'label': f'Pytanie {w.canary(spec)}', 'required': False}]}]}})
    w.put(f'questionnaire-template:{spec}', {'id': template.get('id')})
    w.ok(spec, 'POST', f"/api/trainer/questionnaire-templates/{template['id']}/publish",
         {'expectedRevision': template.get('revision', 0)})
    sent = w.ok(spec, 'POST', f"/api/trainer/clients/{card['id']}/questionnaires", {'templateId': template['id']})
    w.put(f'questionnaire-assignment:{spec}', sent)


def offers_booking(w, spec, card_key):
    offer = w.ok(spec, 'POST', '/api/trainer/offers', {
        'name': f'Pakiet {w.canary(spec)}', 'description': 'Opis', 'kind': 'PERIOD', 'durationDays': 30,
        'priceAmount': 199, 'grantsPlanAccess': True, 'grantsChat': True, 'paymentInstructions': 'Przelew',
        'sellerStatus': 'NOT_TRADER', 'sellerIdentity': {'name': f'Sprzedawca {w.canary(spec)}', 'address': 'ul. Testowa 1, 00-001 Warszawa', 'contact': w.data['actors'][spec]['email'], 'taxId': None}, 'active': True, 'currency': 'PLN', 'termsText': 'Regulamin testowy pakietu: płatność z góry, rezygnacja do 14 dni od zakupu, kontakt przez czat.'})
    w.put(f'offer:{spec}', {'id': offer.get('id')})
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
    slot = w.ok(spec, 'POST', '/api/trainer/booking-slots', {
        'startAt': start.isoformat(), 'endAt': (start + dt.timedelta(hours=1)).isoformat(),
        'providerUserId': w.data['actors'][spec]['keycloakId']})
    w.put(f'slot:{spec}', slot)


def private_recipe(w, spec):
    if w.res(f'private-recipe:{spec}'):
        return
    recipe = w.call(spec, 'POST', '/api/trainer/recipes/private', {
        'name': f'Przepis prywatny {w.canary(spec)}', 'description': 'Opis', 'category': 'DINNER', 'baseServings': 1,
        'steps': [{'position': 1, 'text': 'Krok'}],
        'ingredients': [{'position': 1, 'productId': w.ok(spec, 'POST', '/api/products', {
            'name': f'Produkt {w.canary(spec)}', 'kcal': 100, 'protein': 5, 'carbs': 10, 'fat': 2, 'fiber': 1})['id'],
            'grams': 100, 'optional': False}],
        'tags': [], 'dietTypes': [], 'aliases': [], 'tips': [],
        'containsAllergens': [], 'mayContainAllergens': []})
    if not recipe.ok and recipe.status != 409:
        raise RuntimeError(f'private recipe {spec}: {recipe}')
    w.put(f'private-recipe:{spec}', True)


def pair(w, spec, card_one, card_two, key):
    if w.res(key):
        return
    created = w.ok(spec, 'POST', '/api/trainer/pairs',
                   {'clientIds': [w.res(card_one)['id'], w.res(card_two)['id']]},
                   headers={'Idempotency-Key': str(uuid.uuid4())})
    w.put(key, created)


def diet_meal(w, name):
    if w.res(f'diet-meal:{name}'):
        return
    day, product = w.res(f'diet-day:{name}'), w.res(f'product:{name}')
    full_product = w.ok(name, 'GET', f"/api/products/{product['id']}")
    updated = w.ok(name, 'PUT', '/api/diet-day', {
        'id': day['id'], 'name': day['name'], 'dayDate': day['dayDate'],
        'meals': [{'name': f'Obiad {w.canary(name)}', 'sortOrder': 0,
                   'gramProducts': [{'grams': 150, 'product': full_product}]}]})
    w.put(f'diet-meal:{name}', {'dayId': day['id'], 'meals': [m.get('id') or m.get('mealId') for m in (updated or {}).get('meals', [])]})


def training_template(w, name):
    if w.res(f'training-template:{name}'):
        return
    template = w.ok(name, 'POST', '/api/training-templates', {
        'name': f'Szablon {w.canary(name)}', 'description': w.canary(name), 'goal': 'GENERAL_FITNESS', 'daysPerWeek': 1,
        'sessionLengthMinutes': 30, 'days': [{'dayIndex': 1, 'focus': w.canary(name),
                                              'exercises': [{'name': f'Wiosłowanie {w.canary(name)}', 'sets': 3, 'reps': 12}]}]})
    w.put(f'training-template:{name}', {'id': template.get('id') if isinstance(template, dict) else None})


def shopping_list(w, name):
    if w.res(f'shopping-list:{name}'):
        return
    grant = w.call('admin', 'POST', '/api/admin/subscriptions/manual-grants',
                   {'email': w.data['actors'][name]['email'], 'planCode': 'premium_monthly'})
    if not grant.ok and 'already' not in grant.body:
        raise RuntimeError(f'premium grant {name}: {grant}')
    listing = w.ok(name, 'POST', '/api/shopping-lists/resolve', {
        'sourceType': 'OWN_DIET', 'sourceId': None, 'dateFrom': str(TODAY + dt.timedelta(days=1)),
        'dateTo': str(TODAY + dt.timedelta(days=1))})
    w.put(f'shopping-list:{name}', {'id': (listing or {}).get('id') or ((listing or {}).get('list') or {}).get('id')})


def booking_request(w, spec, card_key):
    if w.res(f'booking-request:{spec}'):
        return
    card = w.res(card_key)
    # A slot made after online visits were switched on (older slots stay unavailable for online booking).
    start = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0)
    start = start + dt.timedelta(days=1)
    slot = w.res(f'slot-online2:{spec}') or w.put(f'slot-online2:{spec}', w.ok(spec, 'POST', '/api/trainer/booking-slots', {
        'startAt': start.isoformat(), 'endAt': (start + dt.timedelta(hours=1)).isoformat(),
        'providerUserId': w.data['actors'][spec]['keycloakId']}))
    local = start.astimezone(dt.timezone(dt.timedelta(hours=1)))  # Europe/Warsaw in November (CET)
    created = w.ok(card['client'], 'POST', '/api/booking-requests', {
        'slotId': slot.get('id'),
        'clientId': card['id'], 'providerUserId': w.data['actors'][spec]['keycloakId'], 'date': str(local.date()),
        'startTime': local.strftime('%H:%M'), 'visitMode': 'ONLINE', 'expectedTimeZone': 'Europe/Warsaw',
        'contactEmail': w.data['actors'][card['client']]['email'], 'note': w.canary(card['client'])})
    w.put(f'booking-request:{spec}', {'id': (created or {}).get('id')})


def plan_snapshot(w, spec, card_key):
    if w.res(f'snapshot:{spec}'):
        return
    card = w.res(card_key)
    # Outside the pair plan's week: the pair plan already holds an editable package there.
    start, end = TODAY + dt.timedelta(days=10), TODAY + dt.timedelta(days=16)
    draft = w.ok(spec, 'PUT', f"/api/trainer/clients/{card['id']}/diet-snapshot", {
        'title': f'Plan {w.canary(spec)}', 'periodStart': str(start), 'periodEnd': str(end),
        'dietSnapshot': [{'dayDate': str(start), 'name': w.canary(spec), 'meals': []}],
        'writePrecondition': {'type': 'NO_EDITABLE_SNAPSHOT'}})
    w.put(f'snapshot-draft:{spec}', {'id': draft.get('id')})
    published = w.ok(spec, 'POST', f"/api/trainer/clients/{card['id']}/snapshots/{draft['id']}/publish-v2", {
        'writePrecondition': {'type': 'MATCH_SNAPSHOT', 'snapshotId': draft['id'],
                              'expectedWriteRevision': draft.get('writeRevision', 0)},
        'expectedCurrentPublishedSnapshotId': None})
    w.put(f'snapshot:{spec}', {'id': (published or {}).get('id')})


def pair_plan(w, side):
    pair_res = w.res(f'pair:{side}')
    if not pair_res or w.res(f'pair-plan:{side}'):
        return
    spec = f'spec-{side}'
    start, end = TODAY + dt.timedelta(days=1), TODAY + dt.timedelta(days=7)
    plan = w.ok(spec, 'POST', f"/api/trainer/pairs/{pair_res['id']}/plans", {
        'title': f'Plan pary {w.canary(spec)}', 'periodStart': str(start), 'periodEnd': str(end),
        'expectedPairRevision': pair_res.get('revision', 0)}, headers={'Idempotency-Key': str(uuid.uuid4())})
    w.put(f'pair-plan:{side}', {'id': (plan or {}).get('planId') or (plan or {}).get('id')})


def generated_plan(w, spec, card_key):
    if w.res(f'generated-plan:{spec}'):
        return
    card = w.res(card_key)
    start = TODAY + dt.timedelta(days=20)
    draft = None
    for attempt in range(4):
        res = w.call(spec, 'POST', f"/api/trainer/clients/{card['id']}/diet-snapshot/generate", {
            'title': f'Plan z generatora {w.canary(spec)}', 'startDate': str(start), 'days': 3, 'dietType': 'standard',
            'mealsPerDay': 4, 'kcal': 2000, 'protein': 120, 'carbs': 240, 'fat': 60, 'fiber': 30, 'allergies': [],
            'tastePreferences': [], 'writePrecondition': {'type': 'NO_EDITABLE_SNAPSHOT'}})
        if res.ok:
            draft = res.json()
            break
        if res.status != 429:
            raise RuntimeError(f'generate {spec}: {res}')
        time.sleep(int(res.headers.get('Retry-After', '15')))
    w.put(f'generated-draft:{spec}', {'id': draft['id']})
    published = w.ok(spec, 'POST', f"/api/trainer/clients/{card['id']}/snapshots/{draft['id']}/publish-and-send", {
        'writePrecondition': {'type': 'MATCH_SNAPSHOT', 'snapshotId': draft['id'],
                              'expectedWriteRevision': draft.get('writeRevision', 0)},
        'expectedCurrentPublishedSnapshotId': (w.res(f'snapshot:{spec}') or {}).get('id')})
    w.put(f'generated-plan:{spec}', {'id': (published or {}).get('id') or draft['id']})


def follow_up(w, spec, card_key):
    if w.res(f'follow-up:{spec}'):
        return
    card = w.res(card_key)
    created = w.ok(spec, 'POST', f"/api/trainer/clients/{card['id']}/follow-ups", {
        'title': f'Kontrola {w.canary(spec)}', 'dueOn': str(TODAY + dt.timedelta(days=9)), 'startTime': '10:00',
        'visitMode': 'ONLINE', 'meetingUrl': 'https://meet.example.com/iso-follow-up',
        'expectedTimeZone': 'Europe/Warsaw'})
    w.put(f'follow-up:{spec}', {'id': (created or {}).get('id')})


def package_order(w, spec, card_key):
    if w.res(f'package-order:{spec}'):
        return
    card = w.res(card_key)
    offers = w.ok(card['client'], 'GET', '/api/client/specialist-packages')
    offer = None
    for item in (offers if isinstance(offers, list) else [offers]):
        if item.get('clientId') == card['id'] and item.get('offers'):
            offer = item['offers'][0]
            break
    if not offer:
        raise RuntimeError(f'no offer visible to {card["client"]}: {str(offers)[:200]}')
    created = w.ok(card['client'], 'POST', '/api/client/specialist-packages/orders', {
        'clientId': card['id'], 'offerId': offer['id'], 'termsVersion': TERMS_VERSION,
        'acceptedTerms': True, 'requestedImmediateStart': False})
    w.put(f'package-order:{spec}', {'id': (created or {}).get('id') or (created or {}).get('orderId')})


def own_generation(w, name):
    if w.res(f'own-generation:{name}'):
        return
    config = {'startDate': str(TODAY + dt.timedelta(days=30)), 'dietType': 'standard', 'mealsPerDay': 3, 'days': 2,
              'allergies': [], 'tastePreferences': [],
              'macros': {'calories': 2100, 'protein': 120, 'carbs': 240, 'fat': 65, 'fiber': 30}}
    preview = w.ok(name, 'POST', '/api/diet/generate/preview', config)
    w.put(f'own-preview:{name}', {'previewId': preview.get('previewId') or preview.get('id')})
    committed = w.ok(name, 'POST', '/api/diet/generate/commit', {
        'previewId': preview.get('previewId') or preview.get('id'), 'replaceExisting': True, 'configuration': config,
        'days': preview.get('days')})
    w.put(f'own-generation:{name}', {'committed': bool(committed)})


def recipe_draft(w, name):
    if w.res(f'recipe-draft:{name}'):
        return
    draft = w.ok(name, 'POST', '/api/recipes/mine/drafts', {'recipeId': None, 'data': {'name': f'Szkic {w.canary(name)}'}})
    w.put(f'recipe-draft:{name}', {'id': (draft or {}).get('id')})


def community_submission(w, name):
    if w.res(f'community:{name}'):
        return
    listing = w.ok(name, 'GET', '/api/recipes/mine')
    items = listing.get('items') or listing.get('content') or []
    recipe_id = items[0]['id']
    w.ok(name, 'PUT', '/api/recipes/community/me/author', {'displayName': f'Autor {name}'})
    detail = w.ok(name, 'GET', f'/api/recipes/mine/{recipe_id}')
    version = detail.get('currentVersionId') or detail.get('versionId') or (detail.get('version') or {}).get('id')
    submitted = w.call(name, 'POST', f'/api/recipes/mine/{recipe_id}/submit', {'expectedCurrentVersionId': version})
    if not submitted.ok:
        raise RuntimeError(f'submit {name}: {submitted}')
    w.put(f'community:{name}', {'recipeId': recipe_id, 'submission': (submitted.json() or {}).get('id')})


def training_session(w, name):
    if w.res(f'training-session:{name}'):
        return
    plans = w.ok(name, 'GET', '/api/training-plans')
    items = plans if isinstance(plans, list) else plans.get('items') or plans.get('plans') or []
    plan_id = items[0]['id']
    session = w.ok(name, 'POST', f'/api/training-plans/{plan_id}/sessions', {
        'weekNumber': 1, 'dayIndex': 2, 'focus': f'Sesja {w.canary(name)}', 'trainingDate': str(TODAY + dt.timedelta(days=2)),
        'userNotes': w.canary(name), 'sets': [{'exerciseName': f'Martwy ciąg {w.canary(name)}', 'sets': 3, 'reps': 8}]})
    w.put(f'training-session:{name}', {'planId': plan_id, 'session': session})


def cleanup(w):
    guard()
    admin = KeycloakAdmin()
    for name, actor in w.data['actors'].items():
        if actor.get('keycloakId'):
            print(name, admin.delete_user(actor['keycloakId'], actor['username']))
    for name, actor in w.data.get('race', {}).items():
        if actor.get('keycloakId'):
            print(name, admin.delete_user(actor['keycloakId'], actor['username']))
    for side in ('a', 'b'):
        admin.delete_client(f'iso-company-{side}')
    WORLD_FILE.unlink(missing_ok=True)
    print('accounts and company app clients removed; world file deleted')


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'build'
    world = World()
    if command == 'build':
        build(world)
    elif command == 'cleanup':
        world.tokens = None
        cleanup(world)
    else:
        raise SystemExit(__doc__)
