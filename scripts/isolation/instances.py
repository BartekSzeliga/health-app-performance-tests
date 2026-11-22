#!/usr/bin/env python3
"""Identity and consistency with two backend instances on one database (I-5).

Needs a second instance next to ISO_API_BASE (default http://localhost:8087, ISO_API_BASE_B).
1. The same token works on both instances.
2. The same chat message (clientMessageId) sent to both at once is stored once.
3. A live event published on one instance reaches a stream opened on the other.
4. Revoking a company member on one instance denies them on the other at once (no per-instance access cache).
5. Rate limiter: how many of a burst pass on one instance and spread over two.

Usage: scripts/isolation/instances.py
"""
import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import API_BASE, RESULTS, Tokens, guard, load_world, request  # noqa: E402

BASE_B = os.environ.get('ISO_API_BASE_B', 'http://localhost:8087').rstrip('/')


def main():
    guard()
    world = load_world()
    tokens = Tokens({a['username']: a['password'] for a in world['actors'].values()})
    tok = lambda name: tokens.get(world['actors'][name]['username'])  # noqa: E731
    results = {}

    # 1. Same token on both.
    results['sameToken'] = [request('GET', base + '/api/users/user/details', tok('client-a1')).status
                            for base in (API_BASE, BASE_B)]

    # 2. Same message to both instances at the same moment.
    card = world['resources']['card:spec-a:client-a1']['id']
    path = f'/api/chat/conversations/{card}/messages'
    before = len(request('GET', API_BASE + path, tok('spec-a')).json() or [])
    message = {'clientMessageId': str(uuid.uuid4()), 'body': f'dwie instancje {uuid.uuid4().hex[:6]}'}
    barrier = threading.Barrier(2)

    def send(base):
        barrier.wait()
        return request('POST', base + path, tok('spec-a'), message).status
    with ThreadPoolExecutor(2) as pool:
        statuses = list(pool.map(send, (API_BASE, BASE_B)))
    after = request('GET', API_BASE + path, tok('spec-a')).json() or []
    results['duplicateMessage'] = {'statuses': statuses, 'stored': sum(1 for m in after if m.get('body') == message['body']),
                                   'grew': len(after) - before}

    # 3. Live event across instances: stream on B, message sent on A.
    received = {}

    def listen():
        req = urllib.request.Request(BASE_B + '/api/events', headers={'Authorization': f"Bearer {tok('client-a1')}",
                                                                       'Accept': 'text/event-stream'})
        started = time.time()
        with urllib.request.urlopen(req, timeout=15) as res:
            buffer = ''
            while time.time() - started < 12:
                chunk = res.read1(4096).decode()
                buffer += chunk
                if 'event:ready' in buffer and 'ready' not in received:
                    received['ready'] = time.time()
                if 'event:chat' in buffer:
                    received['chat'] = time.time()
                    return
    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    for _ in range(50):
        if 'ready' in received:
            break
        time.sleep(0.1)
    sent_at = time.time()
    request('POST', API_BASE + path, tok('spec-a'), {'clientMessageId': str(uuid.uuid4()), 'body': 'push A->B'})
    listener.join(timeout=13)
    results['liveEventAcross'] = {'connected': 'ready' in received,
                                  'delivered': 'chat' in received,
                                  'ms': round((received['chat'] - sent_at) * 1000) if 'chat' in received else None}

    # 4. Revocation on A is seen on B at once.
    org = world['resources']['org:a']['id']
    member = world['actors']['org-a-member']['keycloakId']
    members = request('GET', f'{API_BASE}/api/organizations/{org}/members', tok('org-a-owner')).json() or []
    revision = next((m.get('revision', 0) for m in members if m.get('userId') == member), 0)
    before_b = request('GET', f'{BASE_B}/api/organizations/{org}', tok('org-a-member')).status
    revoke = request('POST', f'{API_BASE}/api/organizations/{org}/members/{member}/deactivate', tok('org-a-owner'),
                     {'revision': revision})
    after_b = request('GET', f'{BASE_B}/api/organizations/{org}', tok('org-a-member')).status
    after_b_clients = request('GET', f'{BASE_B}/api/organizations/{org}/clients', tok('org-a-member')).status
    results['revocation'] = {'memberOnBBefore': before_b, 'revokeOnA': revoke.status, 'revokeBody': revoke.body[:120],
                             'memberOnBAfter': after_b, 'clientsOnBAfter': after_b_clients}

    # 5. Rate limiter: "products:list" (GET /api/products, burst 4, 2 per second), 16 requests at once.
    def burst(bases):
        time.sleep(3)  # refill
        with ThreadPoolExecutor(16) as pool:
            codes = list(pool.map(lambda i: request('GET', bases[i % len(bases)] + '/api/products?page=0&size=1',
                                                    tok('user-x')).status, range(16)))
        return {'passed': codes.count(200), 'limited': codes.count(429)}
    results['limiter'] = {'oneInstance': burst([API_BASE]), 'twoInstances': burst([API_BASE, BASE_B])}

    out = RESULTS / f"instances-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(json.dumps(results, indent=1, ensure_ascii=False))
    print('->', out)


if __name__ == '__main__':
    main()
