#!/usr/bin/env python3
"""Local SMTP sink for isolation runs: listens on 127.0.0.1 only, accepts recipients @perf.diecik.invalid only,
never relays, and saves each message to results/isolation/mail/ so the tools can read invitation tokens.

Usage: scripts/isolation/smtp_sink.py [port]   (default 1026, the port the local QA runtime sends to)
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EMAIL_DOMAIN, RESULTS  # noqa: E402

MAIL_DIR = RESULTS / 'mail'


async def handle(reader, writer):
    async def reply(line):
        writer.write((line + '\r\n').encode())
        await writer.drain()

    await reply('220 isolation-sink')
    recipients = []
    while True:
        line = await reader.readline()
        if not line:
            break
        command = line.decode(errors='replace').strip()
        upper = command.upper()
        if upper.startswith(('EHLO', 'HELO')):
            await reply('250 isolation-sink')
        elif upper.startswith('MAIL FROM'):
            recipients = []
            await reply('250 OK')
        elif upper.startswith('RCPT TO'):
            address = command.split(':', 1)[1].strip().strip('<>').lower()
            if address.endswith(EMAIL_DOMAIN):
                recipients.append(address)
                await reply('250 OK')
            else:
                await reply('550 only test recipients are accepted')
        elif upper == 'DATA':
            await reply('354 end with .')
            data = bytearray()
            while True:
                part = await reader.readline()
                if part in (b'.\r\n', b'.\n', b''):
                    break
                data.extend(part[1:] if part.startswith(b'..') else part)
            MAIL_DIR.mkdir(parents=True, exist_ok=True)
            name = MAIL_DIR / f'{time.time_ns()}.eml'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
            await reply('250 stored')
        elif upper in ('RSET', 'NOOP'):
            await reply('250 OK')
        elif upper == 'QUIT':
            await reply('221 bye')
            break
        else:
            await reply('250 OK')
    writer.close()


async def main(port):
    server = await asyncio.start_server(handle, '127.0.0.1', port)
    print(f'isolation SMTP sink on 127.0.0.1:{port}, saving to {MAIL_DIR}', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 1026))
