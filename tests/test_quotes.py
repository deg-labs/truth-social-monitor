import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('monitor', str(ROOT / 'truth-social-monitor'))
spec = importlib.util.spec_from_loader(loader.name, loader)
monitor = importlib.util.module_from_spec(spec)
# Import without reading credentials or contacting external services.
with patch.dict(os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.invalid/webhook'}), \
     patch('dotenv.load_dotenv'), patch('builtins.open', unittest.mock.mock_open(
         read_data='[translation]\n{content}\n[headline]\n{content}\n')):
    loader.exec_module(monitor)


SOURCE_ID = '117471491419200330'
INLINE_URL = f'https://truthsocial.com/users/original/statuses/{SOURCE_ID}'
INLINE = f'<span class="quote-inline">RT: <a href="{INLINE_URL}"><span>source</span></a></span>'


def status(**changes):
    result = {
        'id': '117471494164657339',
        'created_at': '2026-10-20T12:00:00Z',
        'url': 'https://truthsocial.com/@realDonaldTrump/posts/117471494164657339',
        'account': {'username': 'realDonaldTrump'},
        'content': '<p>My comment</p>' + INLINE,
        'quote_id': SOURCE_ID,
        'quote': {
            'id': SOURCE_ID,
            'quote_id': None,
            'quote': None,
            'content': '<p>Original text</p>',
            'url': f'https://truthsocial.com/@original/posts/{SOURCE_ID}',
            'account': {'username': 'original', 'display_name': 'Original Author'},
            'media_attachments': [{'url': 'https://static.truthsocial.com/original.jpg'}],
        },
        'in_reply_to_id': None,
        'reblog': None,
        'media_attachments': [],
    }
    result.update(changes)
    return result


class QuoteTests(unittest.TestCase):
    def test_live_api_examples(self):
        posts = json.loads((ROOT / 'tests/fixtures/quotes.json').read_text())
        with patch.object(monitor, 'generate_headline_with_gemini', return_value='見出し'):
            for post, expected_id in zip(posts, (SOURCE_ID, '117471471550327950', None)):
                with self.subTest(post_id=post['id']):
                    quote = monitor.get_quote_info(post)
                    self.assertTrue(monitor.is_target_user(post))
                    if expected_id is None:
                        self.assertIsNone(quote)
                        continue
                    self.assertEqual(quote['id'], expected_id)
                    content = monitor.clean_html(''.join(monitor.QuoteInlineParser(post['content']).body))
                    self.assertNotIn('RT:', content)
                    embed = monitor.build_discord_embed(content, '訳', post['url'], media_urls=quote['media_urls'], quote=quote)
                    self.assertIn('引用元を見る', embed['fields'][0]['value'])
                    if post['id'] == '117471494164657339':
                        self.assertEqual(len(quote['media_urls']), 4)
                        self.assertEqual(embed['image']['url'], quote['media_urls'][0])
                        self.assertIn('メディア投稿', embed['fields'][0]['value'])
                    # The API can omit the embedded source while retaining quote_id.
                    post.pop('quote', None)
                    self.assertEqual(monitor.get_quote_info(post)['id'], expected_id)

    def test_outer_quote_id_is_independent_of_embedded_quote_id(self):
        quote = monitor.get_quote_info(status())
        self.assertEqual(quote['id'], SOURCE_ID)
        self.assertEqual(quote['content'], 'Original text')
        self.assertTrue(monitor.is_target_user(status()))
        self.assertIsNone(monitor.get_quote_info(status(content='Original text', quote_id=None, quote=None)))

    def test_quote_id_without_embedded_status_or_inline(self):
        quote = monitor.get_quote_info(status(quote_id='117471471550327950', quote=None, content='Comment'))
        self.assertEqual(quote['id'], '117471471550327950')
        self.assertEqual(quote['content'], '')
        self.assertTrue(monitor.is_target_user(status(quote=None, content='Comment')))

    def test_embedded_object_alone_identifies_quote(self):
        self.assertEqual(monitor.get_quote_info(status(quote_id=None))['id'], SOURCE_ID)
        self.assertIsNotNone(monitor.get_quote_info(status(quote_id=None, quote={}, content='')))

    def test_inline_fallback_and_nested_markup_removal(self):
        html = "<p>Before &amp; &#39;</p>" + INLINE.replace('class="quote-inline"', "class='extra quote-inline'") + '<p>After</p>'
        parsed = monitor.QuoteInlineParser(html)
        self.assertEqual(''.join(parsed.body), '<p>Before &amp; &#39;</p><p>After</p>')
        quote = monitor.get_quote_info(status(quote_id=None, quote=None, content=html))
        self.assertEqual(quote['id'], SOURCE_ID)
        self.assertEqual(quote['url'], INLINE_URL)

    def test_unrelated_status_link_does_not_identify_quote(self):
        post = status(quote_id=None, quote=None, content=f'<a href="{INLINE_URL}">link</a>')
        self.assertIsNone(monitor.get_quote_info(post))
        self.assertFalse(monitor.is_target_user(post))

    def test_authoritative_id_rejects_mismatched_embed_and_inline(self):
        quote = monitor.get_quote_info(status(quote_id='999'))
        self.assertEqual(quote['id'], '999')
        self.assertEqual(quote['content'], '')
        self.assertEqual(quote['url'], '')

    def test_nested_quote_is_not_used_as_outer_quote(self):
        post = status()
        post['quote'].update(quote_id='999', quote={'id': '999', 'content': 'Nested'}, content='<p>Original text</p>' + INLINE)
        quote = monitor.get_quote_info(post)
        self.assertEqual(quote['id'], SOURCE_ID)
        self.assertEqual(quote['content'], 'Original text')

    def test_quote_and_reply_or_reblog_flags_are_independent(self):
        self.assertTrue(monitor.is_target_user(status(in_reply_to_id='123')))
        self.assertTrue(monitor.is_target_user(status(reblog={'id': '123'})))
        self.assertFalse(monitor.is_target_user(status(account={'username': 'someone_else'})))
        self.assertFalse(monitor.is_target_user(status(quote=None, quote_id=None, content='Reply', in_reply_to_id='123')))
        self.assertFalse(monitor.is_target_user(status(quote=None, quote_id=None, content='Boost', reblog={'id': '123'})))
        self.assertTrue(monitor.is_target_user(status(quote=None, quote_id=None, content='Normal')))

    def test_embed_handles_missing_source_and_discord_limits(self):
        quote = monitor.get_quote_info(status(quote=None, content='Comment'))
        quote['translated_content'] = '[' * 5000
        quote['author'] = '[' * 500
        with patch.object(monitor, 'generate_headline_with_gemini', return_value='Headline'):
            embed = monitor.build_discord_embed('Comment', '[' * 5000, 'https://truthsocial.com/post', quote=quote)
        self.assertTrue(embed['title'].startswith('引用ポスト:'))
        self.assertIn(SOURCE_ID, embed['fields'][0]['value'])
        self.assertLessEqual(len(embed['fields'][0]['value']), 1024)
        self.assertLessEqual(len(embed['fields'][0]['name']), 256)
        total = len(embed['title']) + len(embed['description']) + len(embed['footer']['text'])
        total += sum(len(f['name']) + len(f['value']) for f in embed['fields'])
        self.assertLessEqual(total, 6000)
        quote.pop('translated_content')
        with patch.object(monitor, 'generate_headline_with_gemini', return_value='Headline'):
            embed = monitor.build_discord_embed('Comment', '訳', 'https://truthsocial.com/post', quote=quote)
        self.assertIn('引用元の本文を取得できません', embed['fields'][0]['value'])

    def test_quote_link_cannot_inject_discord_markdown(self):
        post = status()
        post['quote']['url'] += ')[bad](https://evil.example)'
        quote = monitor.get_quote_info(post)
        self.assertNotIn(')', quote['url'])
        self.assertNotIn('[', quote['url'])

    def test_process_translates_separate_texts_preserves_outer_identity_and_cache(self):
        for own_media in ([], [{'url': 'https://static.truthsocial.com/own.jpg'}]):
            with self.subTest(own_media=own_media), tempfile.TemporaryDirectory() as cache_dir, \
                 patch.object(monitor, 'CACHE_DIR', cache_dir), \
                 patch.object(monitor, 'fetch_posts', return_value=[status(media_attachments=own_media)]), \
                 patch.object(monitor, 'translate_with_gemini', side_effect=lambda text: '訳: ' + text) as translate, \
                 patch.object(monitor, 'send_to_discord') as send:
                self.assertEqual(monitor.process_posts(), ['117471494164657339'])
                args, kwargs = send.call_args
                self.assertEqual(args[0:2], ('My comment', '訳: My comment'))
                self.assertEqual(args[2], status()['url'])
                self.assertEqual(args[3], [own_media[0]['url']] if own_media else ['https://static.truthsocial.com/original.jpg'])
                self.assertEqual(kwargs['quote']['translated_content'], '訳: Original text')
                self.assertEqual(translate.call_count, 2)
                self.assertEqual(monitor.process_posts(), [])
                self.assertEqual(send.call_count, 1)


if __name__ == '__main__':
    unittest.main()
