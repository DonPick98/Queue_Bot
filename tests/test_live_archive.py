import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
from telegram_channel_scheduler_bot.live_archive import _read,NoRedirect


class LiveArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        token=Path(tmp.name)/'token';token.write_text('private-token')
        p=patch.dict(os.environ,{'PREVIEW_ARCHIVE_COUNTS_URL':'http://x-publisher:8092/archive/premium-counts',
            'PREVIEW_ARCHIVE_COUNTS_TOKEN_FILE':str(token)})
        p.start();self.addCleanup(p.stop)
        p=patch('urllib.request.build_opener');self.opener=p.start();self.addCleanup(p.stop)
        self.response=self.opener.return_value.open.return_value.__enter__.return_value
        self.response.status=200
        self.data={'photo':1496,'video':516,'checked_at':time.time(),'channel_id':'-100123',
                   'source':'telegram','exact':True}
        self.response.read.side_effect=lambda *_:json.dumps(self.data).encode()

    def test_reads_the_live_totals_and_scopes_request(self):
        self.assertEqual(_read('-100123'),{'photo':1496,'video':516})
        request=self.opener.return_value.open.call_args.args[0]
        self.assertEqual(json.loads(request.data),{'channel_id':'-100123'})
        self.assertEqual(request.get_header('X-berry-x-token'),'private-token')
        self.assertEqual(self.opener.return_value.open.call_args.kwargs['timeout'],20)
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_rejects_old_inexact_missing_wrong_channel_and_invalid_counts(self):
        for change in ({'checked_at':time.time()-31},{'checked_at':time.time()+10},
                       {'exact':False},{'channel_id':'-100456'},{'photo':-1},
                       {'video':True},{'video':None},{'checked_at':float('nan')},{'source':'cache'}):
            with self.subTest(change=change):
                saved=dict(self.data);self.data.update(change)
                with self.assertRaisesRegex(ValueError,'ritentato'):_read('-100123')
                self.data=saved

    def test_zero_counts_are_valid_and_not_replaced(self):
        self.data.update(photo=0,video=0)
        self.assertEqual(_read('-100123'),{'photo':0,'video':0})

    def test_network_failures_do_not_fall_back_or_leak_secrets(self):
        self.opener.return_value.open.side_effect=RuntimeError('private-token http://secret')
        with self.assertRaises(ValueError) as result:_read('-100123')
        self.assertNotIn('private-token',str(result.exception))

    def test_standalone_without_provider_retains_local_behavior(self):
        with patch.dict(os.environ,{'PREVIEW_ARCHIVE_COUNTS_URL':''}):
            self.assertIsNone(_read('-100123'))
        self.opener.assert_not_called()
