#!/usr/bin/env python3
"""Canaries under concurrent load (I-1) and per-request language under concurrency (part of I-2).

1. Every owner GET proved by the matrix runs again from 32 threads for --seconds, mixed with writes (chat
   messages, measurements). No response may contain a canary of the other side; any hit is saved in full.
2. The same not-found request goes out in parallel with Accept-Language pl and en; each answer must be in its own
   language (the locale must not leak between concurrent requests).

Usage: scripts/isolation/canaries.py [--seconds 60]
"""
import datetime as dt
import json
import random
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import idor  # noqa: E402
from common import API_BASE, RESULTS, guard, request  # noqa: E402


def main(seconds):
    guard()
    runner = idor.Runner({'no_writes': True})
    runner.crawl()
    controls = list(runner.controls('GET').values())
    world = runner.world
    other_side = {name: [a['canary'] for a in runner.actors.values() if a['side'] != actor['side']]
                  for name, actor in runner.actors.items()}
    cards = [(world['resources'][k]['specialist'], world['resources'][k]['id']) for k in
             ('card:spec-a:client-a1', 'card:spec-b:client-b1')]
    stop = time.time() + seconds
    lock = threading.Lock()
    stats = {'reads': 0, 'writes': 0, 'errors': 0, 'leaks': [], 'statuses': {}}

    def worker(seed):
        rnd = random.Random(seed)
        while time.time() < stop:
            if rnd.random() < 0.15:
                spec, card = rnd.choice(cards)
                if rnd.random() < 0.5:
                    res = runner.call(spec, 'POST', f'/api/chat/conversations/{card}/messages', None,
                                      {'clientMessageId': str(uuid.uuid4()),
                                       'body': f"load {world['actors'][spec]['canary']}"})
                else:
                    res = runner.call(spec, 'POST', f'/api/trainer/clients/{card}/care/measurements', None,
                                      {'measuredOn': str(dt.date.today()), 'source': 'OFFICE', 'weightKg': 70,
                                       'note': world['actors'][spec]['canary']})
                with lock:
                    stats['writes'] += 1
                    stats['errors'] += 0 if res.ok else 1
                    key = f'write {res.status}'
                    stats['statuses'][key] = stats['statuses'].get(key, 0) + 1
                continue
            control = rnd.choice(controls)
            res = runner.call(control['owner'], 'GET', control['url'], control['org'])
            hits = [c for c in other_side[control['owner']] if c in res.body]
            with lock:
                stats['reads'] += 1
                key = f'read {res.status}'
                stats['statuses'][key] = stats['statuses'].get(key, 0) + 1
                if res.status >= 500 or res.status == 0:
                    stats['errors'] += 1
                if hits:
                    stats['leaks'].append({'owner': control['owner'], 'url': control['url'], 'canaries': hits,
                                           'body': res.body[:2000]})

    with ThreadPoolExecutor(32) as pool:
        list(pool.map(worker, range(32)))
    print(f"load: {stats['reads']} reads, {stats['writes']} writes in {seconds}s, errors {stats['errors']}, "
          f"cross-side canary hits {len(stats['leaks'])}, statuses {dict(sorted(stats['statuses'].items()))}", flush=True)

    # Language per request under concurrency.
    token = runner.tokens.get(world['actors']['user-x']['username'])
    path = f'/api/products/{uuid.uuid4()}'
    samples = {lang: request('GET', API_BASE + path, token, headers={'Accept-Language': lang}).body for lang in ('pl', 'en')}
    if samples['pl'] == samples['en']:
        print('language check skipped: this message is not localized')
        language = {'skipped': True}
    else:
        def ask(i):
            lang = 'pl' if i % 2 else 'en'
            body = request('GET', API_BASE + path, token, headers={'Accept-Language': lang}).body
            return lang, body == samples[lang]
        with ThreadPoolExecutor(32) as pool:
            answers = list(pool.map(ask, range(2000)))
        wrong = sum(1 for _, ok in answers if not ok)
        language = {'requests': len(answers), 'wrongLanguage': wrong, 'pl': samples['pl'][:80], 'en': samples['en'][:80]}
        print(f'language: {len(answers)} parallel requests, wrong language in {wrong}', flush=True)
    out = RESULTS / f"canaries-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({'load': stats, 'language': language}, indent=1, ensure_ascii=False))
    print('->', out)


if __name__ == '__main__':
    main(int(sys.argv[sys.argv.index('--seconds') + 1]) if '--seconds' in sys.argv else 60)
