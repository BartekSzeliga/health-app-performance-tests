#!/usr/bin/env python3
"""Authorization matrix (IDOR) over every backend route with an ID in the path.

1. Crawl: each actor calls the GET routes it can call without IDs (and with its company header); every UUID in the
   answers goes to that actor's ID pool, keyed by field name and by the resource the route returns.
2. Control: for each route with path variables, IDs from the owner's pool are bound to the variables; the owner's own
   call must succeed (2xx). Without such a control the route counts as "not covered", never as "safe".
3. Attack: the same URL as every other actor. 401/403/404 is fine; 2xx is a finding, confirmed when the body carries
   a canary of someone the attacker must not see; 400/409/422/5xx are noted for review.
   Writes (POST/PUT/PATCH/DELETE) are attacked with IDs that a GET control proved, deletes last.
4. Admin routes: every /api/admin/** route as a specialist and a plain user must answer 401 or 403.

Writes results/isolation/idor-<timestamp>.json and prints a summary.
Usage: scripts/isolation/idor.py [--no-writes] [--only PREFIX]
"""
import datetime as dt
import itertools
import json
import re
import sys
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import API_BASE, RESULTS, Tokens, guard, load_world, request  # noqa: E402

UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
VAR_RE = re.compile(r'\{(\w+)(?::[^}]*)?\}')
TODAY = dt.date.today()

# Routes anyone may call (SecurityConfig permitAll) or that are not about user data.
GENERIC_SEGMENTS = {'mine', 'private', 'me', 'own', 'shared', 'community', 'current', 'summary', 'list', 'all',
                    'latest', 'active', 'search', 'history', 'details'}


def route_resources(path):
    """Resource names a route returns: its last static segment, and the one before when the last is generic."""
    static = [s for s in path.split('/') if s and '{' not in s and s != 'api']
    names = [singular(static[-1])] if static else []
    if static and static[-1] in GENERIC_SEGMENTS and len(static) > 1:
        names.append(singular(static[-2]))
    return names


PUBLIC_PREFIXES = ('/api/public/', '/public/', '/.well-known/', '/api/trainer/client-invitations/',
                   '/api/recommended-trainers/', '/api/specialists/', '/api/subscriptions/providers/',
                   '/api/subscriptions/mobile-store/', '/api/internal/', '/api/trainer/booking-google/callback',
                   '/api/organization-invitations/')
SKIP_PATHS = ('/api/events',)  # live stream: never returns, covered by its own tests

QUERY_DEFAULTS = {
    'date': str(TODAY), 'day': str(TODAY), 'dayDate': str(TODAY), 'weekStart': str(TODAY - dt.timedelta(days=TODAY.weekday())),
    # Ranges inside the world's plans (tomorrow + 6 days) and under the 43-day calendar limit.
    'dateFrom': str(TODAY + dt.timedelta(days=1)), 'dateTo': str(TODAY + dt.timedelta(days=7)),
    'from': str(TODAY - dt.timedelta(days=7)), 'to': str(TODAY + dt.timedelta(days=30)),
    'start': str(TODAY - dt.timedelta(days=30)), 'end': str(TODAY + dt.timedelta(days=30)),
    'startDate': str(TODAY - dt.timedelta(days=30)), 'endDate': str(TODAY + dt.timedelta(days=30)),
    'month': TODAY.strftime('%Y-%m'), 'year': str(TODAY.year), 'page': '0', 'size': '20', 'limit': '20',
    'offset': '0', 'query': 'a', 'q': 'a', 'search': 'a', 'perspective': 'TRAINER', 'timeZone': 'Europe/Warsaw',
    'zone': 'Europe/Warsaw', 'locale': 'pl', 'lang': 'pl', 'days': '7',
}


def singular(word):
    word = word.lower()
    for suffix, repl in (('ies', 'y'), ('ses', 's'), ('s', '')):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)] + repl
    return word


def norm(name):
    return re.sub(r'[^a-z0-9]', '', name.lower())


def var_keys(path, var):
    """Pool keys that may hold values for a path variable: its own name and the resource named before it."""
    segments = path.split('/')
    index = next(i for i, s in enumerate(segments) if VAR_RE.fullmatch(s) and VAR_RE.fullmatch(s).group(1) == var)
    keys = [] if var == 'id' else [norm(var)]
    previous = next((s for s in reversed(segments[:index]) if s and not VAR_RE.fullmatch(s)), '')
    if previous:
        keys.append('id@' + singular(previous))
        keys.append(norm(singular(previous)) + 'id')
    if var.endswith('Id'):
        keys.append('id@' + singular(var[:-2]))
    if var in ('org', 'organization'):
        keys += ['organizationid', 'id@organization']
    if var in ('client',):
        keys += ['clientid', 'id@client']
    if var in ('user',):
        keys += ['userid', 'keycloakid']
    return list(dict.fromkeys(keys))


CONTAINER_KEYS = {'items', 'content', 'data', 'results', 'entries', 'list', 'page', 'rows', 'elements', 'records',
                  'values', 'search', 'matches', 'hits'}


def harvest(node, resource, pool, parent_key=None):
    """Adds every UUID in a JSON answer to the pool: under its field name and, for 'id', under the resource the route
    returns and under the field that holds the object (e.g. 'snapshots' -> id@snapshot)."""
    if isinstance(node, dict):
        owners = {resource}
        if parent_key and parent_key not in CONTAINER_KEYS and not parent_key.endswith('Id'):
            owners = {singular(parent_key)}
        for key, value in node.items():
            if isinstance(value, str) and UUID_RE.match(value):
                pool[norm(key)].add(value)
                if key == 'id':
                    for owner in owners:
                        if owner:
                            pool['id@' + owner].add(value)
            elif isinstance(value, (dict, list)):
                harvest(value, resource, pool, key)
    elif isinstance(node, list):
        for item in node:
            harvest(item, resource, pool, parent_key)


def is_public(path):
    return path.startswith(PUBLIC_PREFIXES) or path in SKIP_PATHS or path == '/'


def query_for(route):
    params = {}
    for q in route['query']:
        if q['required']:
            if q['name'] not in QUERY_DEFAULTS:
                return None
            params[q['name']] = QUERY_DEFAULTS[q['name']]
    return params


def build_url(path, values, params):
    url = VAR_RE.sub(lambda m: values[m.group(1)], path)
    if params:
        url += '?' + '&'.join(f'{k}={v}' for k, v in params.items())
    return url


class Runner:
    def __init__(self, args):
        self.world = load_world()
        self.actors = {n: a for n, a in self.world['actors'].items() if n != 'admin' and a.get('keycloakId')}
        self.tokens = Tokens({a['username']: a['password'] for a in self.world['actors'].values()})
        routes = json.loads((RESULTS / 'routes.json').read_text())['routes']
        self.routes = [r for r in routes if r['method'] in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
                       and (not args.get('only') or r['path'].startswith(args['only']))]
        self.writes = not args.get('no_writes')
        self.orgs = {}
        for side in ('a', 'b'):
            org = self.world['resources'].get(f'org:{side}')
            if org:
                for member in (f'org-{side}-owner', f'org-{side}-member'):
                    self.orgs[member] = org['id']
        self.pools = {n: defaultdict(set) for n in self.actors}
        self.bodies = {}  # (owner, url) -> owner's GET body, reused as a write body
        self.results = []
        self.requests = 0

    # --- HTTP ---------------------------------------------------------------------------------------------
    def call(self, actor, method, url, org=None, body=None):
        headers = {}
        if org:
            headers['X-Organization-Id'] = org
        if method != 'GET':
            headers['Idempotency-Key'] = str(uuid.uuid4())
        self.requests += 1
        token = self.tokens.get(self.world['actors'][actor]['username'])
        return request(method, API_BASE + url, token, body if method != 'GET' else None, headers, timeout=30)

    def org_variants(self, actor, route):
        """Calls without the company header, and with the actor's company when the route takes one."""
        variants = [None]
        if route['orgHeader'] and actor in self.orgs:
            variants.append(self.orgs[actor])
        return variants

    # --- 1. crawl -----------------------------------------------------------------------------------------
    def crawl(self):
        jobs = []
        for route in self.routes:
            if route['method'] != 'GET' or route['pathVars'] or is_public(route['path']) or route['path'].startswith('/api/admin'):
                continue
            params = query_for(route)
            if params is None:
                continue
            resource = route_resources(route['path'])
            for actor in self.actors:
                for org in self.org_variants(actor, route):
                    jobs.append((actor, route, build_url(route['path'], {}, params), org, resource))
        with ThreadPoolExecutor(8) as pool:
            for (actor, route, url, org, resource), res in zip(jobs, pool.map(lambda j: self.call(j[0], 'GET', j[2], j[3]), jobs)):
                if res.ok and res.json() is not None:
                    for name in resource:
                        harvest(res.json(), name, self.pools[actor])
        self.inject_world()
        for actor, data in self.world['actors'].items():
            if actor in self.pools:
                self.pools[actor]['keycloakid'].add(data['keycloakId'])
                self.pools[actor]['userid'].add(data['keycloakId'])
        print(f'crawl: {len(jobs)} calls, IDs per actor: ' +
              ', '.join(f'{a}={len(set().union(*p.values()))}' for a, p in self.pools.items()), flush=True)

    def inject_world(self):
        """IDs the world builder got back from writes (lists, plans, cards) that no GET list returns."""
        for key, value in self.world['resources'].items():
            parts = key.split(':')
            kind, owner = parts[0], parts[1] if len(parts) > 1 else None
            ids = []
            if isinstance(value, dict):
                harvest(value, singular(kind), self.pools.get(value.get('specialist') or owner, defaultdict(set)))
                ids = [value.get('id')]
            owner = (value.get('specialist') if isinstance(value, dict) and value.get('specialist') else owner)
            if kind == 'org':
                owner = value.get('owner') if isinstance(value, dict) else None
            if owner in ('a', 'b'):
                owner = f'spec-{owner}'
            if owner not in self.pools:
                continue
            for item in ids:
                if isinstance(item, str) and UUID_RE.match(item):
                    self.pools[owner]['id@' + singular(kind)].add(item)
                    self.pools[owner][norm(singular(kind)) + 'id'].add(item)
                    if kind == 'card':
                        self.pools[owner]['clientid'].add(item)
                        self.pools[owner]['id@client'].add(item)
                    if kind == 'org':
                        self.pools[owner]['organizationid'].add(item)
                        self.pools[owner]['org'].add(item)

    # --- 2. controls ----------------------------------------------------------------------------------------
    def candidates(self, actor, route, limit=4):
        options = []
        for var in VAR_RE.findall(route['path']):
            values = []
            for key in var_keys(route['path'], var):
                values += sorted(self.pools[actor].get(key, ()))
            values = list(dict.fromkeys(values))[:limit]
            if not values:
                return []
            options.append((var, values))
        names = [o[0] for o in options]
        return [dict(zip(names, combo)) for combo in itertools.islice(itertools.product(*[o[1] for o in options]), 12)]

    def controls(self, method='GET', rounds=3):
        found = {}
        for _ in range(rounds):
            new = 0
            for route in self.routes:
                if route['method'] != method or not route['pathVars'] or is_public(route['path']) \
                        or route['path'].startswith('/api/admin'):
                    continue
                params = query_for(route)
                if params is None:
                    continue
                for actor in self.actors:
                    key = (route['source'], route['method'], route['path'], actor)
                    if key in found:
                        continue
                    for values in self.candidates(actor, route):
                        url = build_url(route['path'], values, params)
                        hit = None
                        for org in self.org_variants(actor, route):
                            res = self.call(actor, 'GET', url, org)
                            if res.ok:
                                hit = (org, res)
                                break
                        if hit:
                            found[key] = {'route': route, 'owner': actor, 'url': url, 'org': hit[0], 'values': values}
                            body = hit[1].json()
                            if body is not None:
                                self.bodies[(actor, url)] = body
                                for name in route_resources(route['path']):
                                    harvest(body, name, self.pools[actor])
                            new += 1
                            break
            print(f'controls round: +{new} (total {len(found)})', flush=True)
            if not new:
                break
        return found

    # --- 3. attacks ---------------------------------------------------------------------------------------------
    def relation(self, owner, attacker):
        o, a = self.actors[owner], self.actors[attacker]
        if o['side'] != a['side']:
            return 'other-side'
        return 'same-side'

    def visible_canaries(self, attacker):
        """Canaries an attacker may legitimately see: its own and those of its own side."""
        side = self.actors[attacker]['side']
        return {a['canary'] for a in self.actors.values() if a['side'] == side}

    def classify(self, owner, attacker, res, values):
        if res.status in (401, 403, 404):
            return 'denied'
        if res.status == 0:
            return 'error'
        if res.ok:
            foreign = [a['canary'] for n, a in self.actors.items()
                       if a['canary'] in res.body and a['canary'] not in self.visible_canaries(attacker)]
            if foreign:
                return 'LEAK'
            own_ids = set().union(*self.pools[attacker].values())
            if all(v in own_ids for v in values.values()):
                return 'shared'
            return 'ALLOWED'
        return f'status-{res.status}'

    def attack(self, controls, method_filter=('GET',)):
        jobs = []
        for control in controls.values():
            route = control['route']
            if route['method'] not in method_filter:
                continue
            for attacker in self.actors:
                if attacker == control['owner']:
                    continue
                orgs = [None, control['org']] + ([self.orgs[attacker]] if attacker in self.orgs else [])
                for org in list(dict.fromkeys(orgs)):
                    jobs.append((control, attacker, org))

        def run(job):
            control, attacker, org = job
            route = control['route']
            body = None
            if route['method'] in ('PUT', 'PATCH', 'POST'):
                body = self.bodies.get((control['owner'], control['get_url'])) if control.get('get_url') else None
                body = body if isinstance(body, dict) else {}
            res = self.call(attacker, route['method'], control['url'], org, body)
            return job, res

        with ThreadPoolExecutor(8) as pool:
            for (control, attacker, org), res in pool.map(run, jobs):
                verdict = self.classify(control['owner'], attacker, res, control['values'])
                self.results.append({
                    'method': control['route']['method'], 'path': control['route']['path'], 'url': control['url'],
                    'handler': control['route']['handler'], 'owner': control['owner'], 'attacker': attacker,
                    'relation': self.relation(control['owner'], attacker), 'org': org, 'status': res.status,
                    'verdict': verdict, 'body': res.body[:400] if verdict not in ('denied',) else ''})

    def write_controls(self, get_controls):
        """Writes reuse IDs a GET control proved for the same owner: same path prefix up to the last variable."""
        proved = defaultdict(list)
        for control in get_controls.values():
            proved[control['owner']].append(control)
        controls = {}
        for route in self.routes:
            if route['method'] == 'GET' or not route['pathVars'] or is_public(route['path']) \
                    or route['path'].startswith('/api/admin'):
                continue
            needed = VAR_RE.findall(route['path'])
            for owner, owned in proved.items():
                for control in owned:
                    if all(v in control['values'] for v in needed) and \
                            route['path'].startswith(control['route']['path'].split('{')[0]):
                        params = query_for(route) or {}
                        url = build_url(route['path'], control['values'], params)
                        controls[(route['source'], route['method'], route['path'], owner)] = {
                            'route': route, 'owner': owner, 'url': url, 'org': control['org'],
                            'values': {v: control['values'][v] for v in needed}, 'get_url': control['url']}
                        break
        return controls

    # --- 4. admin -----------------------------------------------------------------------------------------------
    def admin_routes(self):
        jobs = []
        for route in self.routes:
            if not route['path'].startswith('/api/admin'):
                continue
            url = VAR_RE.sub(lambda m: str(uuid.uuid4()), route['path'])
            for actor in ('spec-a', 'user-x', 'org-a-owner'):
                jobs.append((route, actor, url))
        with ThreadPoolExecutor(8) as pool:
            for (route, actor, url), res in zip(jobs, pool.map(lambda j: self.call(j[1], j[0]['method'], j[2], None,
                                                                                    {} if j[0]['method'] != 'GET' else None), jobs)):
                self.results.append({'method': route['method'], 'path': route['path'], 'url': url,
                                     'handler': route['handler'], 'owner': 'admin', 'attacker': actor,
                                     'relation': 'non-admin', 'org': None, 'status': res.status,
                                     'verdict': 'denied' if res.status in (401, 403) else 'ADMIN-OPEN',
                                     'body': '' if res.status in (401, 403) else res.body[:400]})

    def run(self):
        guard()
        started = time.time()
        self.crawl()
        gets = self.controls('GET')
        self.attack(gets, ('GET',))
        covered = {(c['route']['method'], c['route']['path']) for c in gets.values()}
        writes = {}
        if self.writes:
            writes = self.write_controls(gets)
            self.attack(writes, ('POST', 'PUT', 'PATCH'))
            self.attack(writes, ('DELETE',))
            covered |= {(c['route']['method'], c['route']['path']) for c in writes.values()}
        self.admin_routes()
        with_ids = {(r['method'], r['path']) for r in self.routes if r['pathVars'] and not is_public(r['path'])
                    and not r['path'].startswith('/api/admin')}
        summary = {
            'startedAt': dt.datetime.fromtimestamp(started).isoformat(), 'seconds': round(time.time() - started),
            'requests': self.requests, 'routesWithIds': len(with_ids), 'covered': len(covered & with_ids),
            'notCovered': sorted(f'{m} {p}' for m, p in with_ids - covered),
            'verdicts': {}, 'byRelation': {},
        }
        for r in self.results:
            summary['verdicts'][r['verdict']] = summary['verdicts'].get(r['verdict'], 0) + 1
            key = f"{r['relation']}:{r['verdict']}"
            summary['byRelation'][key] = summary['byRelation'].get(key, 0) + 1
        out = RESULTS / f"idor-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        out.write_text(json.dumps({'summary': summary, 'results': self.results}, indent=1, ensure_ascii=False))
        print(json.dumps({k: v for k, v in summary.items() if k != 'notCovered'}, indent=1))
        print(f'not covered: {len(summary["notCovered"])} routes; details in {out}')


if __name__ == '__main__':
    opts = {'no_writes': '--no-writes' in sys.argv}
    if '--only' in sys.argv:
        opts['only'] = sys.argv[sys.argv.index('--only') + 1]
    Runner(opts).run()
