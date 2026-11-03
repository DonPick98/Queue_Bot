"""Read exact archive totals just before a recap; never fall back when configured."""
import asyncio
import json
import math
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _read(channel_id):
    url=os.getenv('PREVIEW_ARCHIVE_COUNTS_URL','').strip()
    if not url:
        return None  # Standalone installations can keep using their local ledger.
    try:
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        token=Path(os.environ['PREVIEW_ARCHIVE_COUNTS_TOKEN_FILE']).read_text().strip()
        if not token or not token.isascii() or '\n' in token or '\r' in token:
            raise ValueError()
        channel=str(channel_id or '').strip()
        if not channel:
            raise ValueError()
        request=urllib.request.Request(url,data=json.dumps({'channel_id':channel}).encode(),
            headers={'Content-Type':'application/json','X-Berry-X-Token':token},method='POST')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=20) as response:
            if response.status!=200:
                raise ValueError()
            raw=response.read(8193)
        if len(raw)>8192:
            raise ValueError()
        data=json.loads(raw)
        if (not isinstance(data,dict) or data.get('source')!='telegram' or data.get('exact') is not True
                or data.get('channel_id')!=channel):
            raise ValueError()
        checked=data.get('checked_at')
        if type(checked) not in (int,float) or not math.isfinite(checked) or not -5<=time.time()-checked<=30:
            raise ValueError()
        if any(type(data.get(k)) is not int or data[k]<0 for k in ('photo','video')):
            raise ValueError()
        return {k:data[k] for k in ('photo','video')}
    except Exception:
        raise ValueError('Conteggi Telegram aggiornati non disponibili: il riepilogo sarà ritentato.') from None


async def live_archive_counts(channel_id):
    return await asyncio.to_thread(_read,channel_id)
