"""Shared pieces of the data isolation toolkit: settings, the production guard, HTTP and tokens.

Settings come from environment variables (ISO_*); defaults point at the local QA runtime:
  ISO_API_BASE        http://localhost:8086      backend (paths in the tools start with /api)
  ISO_KEYCLOAK_BASE   http://localhost:8080      Keycloak
  ISO_REALM           health-realm
  ISO_CLIENT_ID       health-app-remediation-qa  public client with the password grant (test realm only)
  ISO_RESULTS         results/isolation          world file (0600, has passwords) and run results
  ISO_KC_ADMIN_USER / ISO_KC_ADMIN_PASSWORD      Keycloak admin; if absent, read in memory from the local
                                                 keycloak-dev container (never printed or written)
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API_BASE = os.environ.get('ISO_API_BASE', 'http://localhost:8086').rstrip('/')
KEYCLOAK_BASE = os.environ.get('ISO_KEYCLOAK_BASE', 'http://localhost:8080').rstrip('/')
REALM = os.environ.get('ISO_REALM', 'health-realm')
CLIENT_ID = os.environ.get('ISO_CLIENT_ID', 'health-app-remediation-qa')
RESULTS = Path(os.environ.get('ISO_RESULTS', ROOT / 'results' / 'isolation'))
WORLD_FILE = RESULTS / 'world.json'
EMAIL_DOMAIN = '@perf.diecik.invalid'

# Same rule as ProdGuard in the Java toolkit: production hosts are refused, whatever the flags.
PRODUCTION_HOSTS = ('diecik.com', 'api.diecik.com', 'auth.diecik.com', 'www.diecik.com')


def guard():
    for url in (API_BASE, KEYCLOAK_BASE):
        host = urllib.parse.urlparse(url).hostname or ''
        if host in PRODUCTION_HOSTS or host.endswith('.elb.amazonaws.com') and 'prod' in host:
            raise SystemExit(f'Refusing to run against production: {url}')
        if host not in ('localhost', '127.0.0.1') and not host.endswith('.dev.diecik.com'):
            raise SystemExit(f'Isolation runs only target the local QA runtime or AWS dev: {url}')


class Response:
    def __init__(self, status, body, headers):
        self.status, self.body, self.headers = status, body, headers

    @property
    def ok(self):
        return 200 <= self.status < 300

    def json(self):
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            return None

    def __repr__(self):
        return f'HTTP {self.status} {self.body[:300]!r}'


def request(method, url, token=None, body=None, headers=None, timeout=60):
    data = None
    all_headers = {'Accept': 'application/json, */*;q=0.5'}
    if token:
        all_headers['Authorization'] = f'Bearer {token}'
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body).encode()
            all_headers['Content-Type'] = 'application/json'
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return Response(res.status, res.read().decode('utf-8', 'replace'), dict(res.headers))
    except urllib.error.HTTPError as e:
        return Response(e.code, e.read().decode('utf-8', 'replace'), dict(e.headers or {}))
    except urllib.error.URLError as e:
        return Response(0, f'connection error: {e.reason}', {})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


urllib.request.install_opener(urllib.request.build_opener(NoRedirect()))


class Tokens:
    """Password-grant tokens per username, refreshed shortly before they expire."""

    def __init__(self, passwords):
        self.passwords, self.cache = passwords, {}

    def get(self, username):
        cached = self.cache.get(username)
        if cached and cached[1] > time.time() + 30:
            return cached[0]
        form = urllib.parse.urlencode({'client_id': CLIENT_ID, 'grant_type': 'password', 'username': username,
                                       'password': self.passwords[username]}).encode()
        req = urllib.request.Request(f'{KEYCLOAK_BASE}/realms/{REALM}/protocol/openid-connect/token', data=form,
                                     method='POST')
        with urllib.request.urlopen(req, timeout=30) as res:
            payload = json.loads(res.read())
        self.cache[username] = (payload['access_token'], time.time() + payload['expires_in'])
        return payload['access_token']


def keycloak_admin_credentials():
    user, password = os.environ.get('ISO_KC_ADMIN_USER'), os.environ.get('ISO_KC_ADMIN_PASSWORD')
    if user and password:
        return user, password
    raw = subprocess.check_output(['docker', 'inspect', 'keycloak-dev', '--format', '{{json .Config.Env}}'], text=True)
    env = dict(item.split('=', 1) for item in json.loads(raw))
    return (env.get('KC_BOOTSTRAP_ADMIN_USERNAME') or env.get('KEYCLOAK_ADMIN'),
            env.get('KC_BOOTSTRAP_ADMIN_PASSWORD') or env.get('KEYCLOAK_ADMIN_PASSWORD'))


class KeycloakAdmin:
    def __init__(self):
        user, password = keycloak_admin_credentials()
        form = urllib.parse.urlencode({'client_id': 'admin-cli', 'grant_type': 'password', 'username': user,
                                       'password': password}).encode()
        req = urllib.request.Request(f'{KEYCLOAK_BASE}/realms/master/protocol/openid-connect/token', data=form,
                                     method='POST')
        with urllib.request.urlopen(req, timeout=30) as res:
            self.token = json.loads(res.read())['access_token']
        self.base = f'{KEYCLOAK_BASE}/admin/realms/{REALM}'

    def call(self, method, path, body=None):
        return request(method, self.base + path, self.token, body)

    def ensure_user(self, username, email, password, roles=()):
        if not email.endswith(EMAIL_DOMAIN):
            raise SystemExit(f'Not a test account: {email}')
        found = self.call('GET', '/users?exact=true&username=' + urllib.parse.quote(username)).json() or []
        if found:
            user_id = found[0]['id']
        else:
            res = self.call('POST', '/users', {
                'username': username, 'email': email, 'emailVerified': True, 'enabled': True,
                'firstName': 'Iso', 'lastName': username.split('-')[-1].capitalize(),
                'credentials': [{'type': 'password', 'value': password, 'temporary': False}]})
            if res.status not in (201, 409):
                raise SystemExit(f'Create user {username}: {res}')
            user_id = self.call('GET', '/users?exact=true&username=' + urllib.parse.quote(username)).json()[0]['id']
        self.call('PUT', f'/users/{user_id}/reset-password', {'type': 'password', 'value': password, 'temporary': False})
        if roles:
            available = {r['name']: r for r in self.call('GET', '/roles').json()}
            self.call('POST', f'/users/{user_id}/role-mappings/realm', [available[r] for r in roles if r in available])
        return user_id

    def delete_user(self, user_id, username):
        if not username.startswith('iso-'):
            raise SystemExit(f'Refusing to delete a non-isolation account: {username}')
        return self.call('DELETE', f'/users/{user_id}').status

    def ensure_public_client(self, client_id, redirect_uris=()):
        found = self.call('GET', '/clients?clientId=' + urllib.parse.quote(client_id)).json() or []
        if found:
            return found[0]['id']
        res = self.call('POST', '/clients', {
            'clientId': client_id, 'name': 'Isolation test (local only)', 'enabled': True, 'publicClient': True,
            'standardFlowEnabled': True, 'directAccessGrantsEnabled': True, 'redirectUris': list(redirect_uris),
            'attributes': {'qa-fixture-owner': 'isolation-20261122'}})
        if res.status != 201:
            raise SystemExit(f'Create client {client_id}: {res}')
        return self.call('GET', '/clients?clientId=' + urllib.parse.quote(client_id)).json()[0]['id']

    def delete_client(self, client_id):
        for client in self.call('GET', '/clients?clientId=' + urllib.parse.quote(client_id)).json() or []:
            if client.get('attributes', {}).get('qa-fixture-owner') != 'isolation-20261122':
                raise SystemExit(f'Refusing to delete a client this toolkit did not create: {client_id}')
            self.call('DELETE', f"/clients/{client['id']}")


def save_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(data, out, indent=1, ensure_ascii=False)


def load_world():
    return json.loads(WORLD_FILE.read_text())
