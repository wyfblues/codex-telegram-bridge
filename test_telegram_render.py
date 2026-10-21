"""Telegram HTML safety, graceful Markdown fallback, and Unicode chunking."""
from html.parser import HTMLParser
import unittest

import telegram_render


class StrictHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.visible, self.anchors = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ('b', 'a', 'code', 'pre'):
            raise AssertionError('Unexpected HTML tag: ' + tag)
        if tag == 'a':
            if self.stack and 'a' in self.stack:
                raise AssertionError('Nested link')
            if len(attrs) != 1 or attrs[0][0] != 'href':
                raise AssertionError('Unexpected HTML attribute')
            self.anchors.append(attrs[0][1])
        elif attrs:
            raise AssertionError('Unexpected HTML attribute')
        if tag in ('code', 'pre') and self.stack:
            raise AssertionError('Code overlaps other formatting')
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise AssertionError('Mismatched HTML tags')

    def handle_data(self, data):
        self.visible.append(data)

    def finish(self):
        self.close()
        if self.stack:
            raise AssertionError('Unclosed HTML tags')
        return ''.join(self.visible)


class TelegramRenderTests(unittest.TestCase):
    def render(self, source, limit=3400):
        parts = telegram_render.render_parts(source, limit)
        for part in parts:
            parser = StrictHTML()
            parser.feed(part['html'])
            self.assertEqual(parser.finish(), part['text'])
            self.assertLessEqual(len(part['text'].encode('utf-16-le')) // 2, limit)
            self.assertTrue(part['text'])
        return parts

    def visible(self, source, limit=3400):
        return ''.join(part['text'] for part in self.render(source, limit))

    def test_plain_text_unicode_and_every_literal_html_character_are_preserved(self):
        source = '你好 😀 & <tag attr="quote">\nsecond\r\nthird\tend'
        self.assertEqual(self.visible(source), source)
        part = self.render(source)[0]
        self.assertIn('&amp; &lt;tag attr="quote"&gt;', part['html'])

    def test_supported_bold_and_inline_code(self):
        part = self.render('**重点**、__重要__，运行 `echo "<safe>"`。')[0]
        self.assertEqual(part['text'], '重点、重要，运行 echo "<safe>"。')
        self.assertIn('<b>重点</b>', part['html'])
        self.assertIn('<code>echo "&lt;safe&gt;"</code>', part['html'])

    def test_code_escapes_markup_and_never_overlaps_bold(self):
        part = self.render('**执行 `a < b && **literal**` 完成**')[0]
        self.assertEqual(part['text'], '执行 a < b && **literal** 完成')
        self.assertIn('</b><code>', part['html'])
        self.assertIn('</code><b>', part['html'])

    def test_double_backtick_code_can_contain_single_backticks(self):
        self.assertEqual(self.visible('``a ` b``'), 'a ` b')
        self.assertEqual(self.visible('``a ``` b``'), 'a ``` b')

    def test_fenced_code_is_escaped_and_language_metadata_not_executed(self):
        source = '说明\n```python\n<script>alert("x")</script>\n**literal**\n```\n后续'
        part = self.render(source)[0]
        self.assertEqual(part['text'], '说明\n<script>alert("x")</script>\n**literal**\n后续')
        self.assertIn('<pre>&lt;script&gt;', part['html'])
        self.assertNotIn('<script>', part['html'])
        self.assertNotIn('<b>literal', part['html'])

    def test_tilde_fences_and_longer_closing_fences(self):
        self.assertEqual(self.visible('~~~text\na < b\n~~~~\nafter'), 'a < b\nafter')
        self.assertEqual(self.visible('```\n```\nafter'), '\nafter')

    def test_broken_fences_and_unmatched_inline_delimiters_remain_literal(self):
        for source in ('```python\n<a>\n**unfinished', '**unfinished', '__unfinished',
                       '`unfinished', '``unfinished`', '[unfinished](https://example.com',
                       '** whitespace **', 'ordinary [ bracket'):
            with self.subTest(source=source):
                self.assertEqual(self.visible(source), source)

    def test_http_links_retain_destination_in_matching_plain_fallback(self):
        part = self.render('[官网](https://example.com/path?a=1&b=2)')[0]
        self.assertEqual(part['text'], '官网 (https://example.com/path?a=1&b=2)')
        self.assertEqual(part['html'], '<a href="https://example.com/path?a=1&amp;b=2">官网</a>'
                         ' (https://example.com/path?a=1&amp;b=2)')

    def test_balanced_parentheses_and_bold_link_labels_are_safe(self):
        part = self.render('[**详情**](https://example.com/a_(b))')[0]
        self.assertEqual(part['text'], '详情 (https://example.com/a_(b))')
        self.assertIn('<a href="https://example.com/a_(b)"><b>详情</b></a>', part['html'])

    def test_unsafe_schemes_credentials_and_local_paths_never_become_anchors(self):
        sources = ['[x](javascript:alert(1))', '[x](data:text/html,<script>)',
                   '[x](file:///Users/private/a.py)', '[a.py](/Users/private/a.py:2)',
                   '[x](codex://threads/private)', '[x](https://user:secret@example.com)',
                   '[x](https://example.com:bad)', '[x](https://example.com/line\nbreak)',
                   '[x](https://example.com\\@other.example)', '[x](//example.com)']
        for source in sources:
            with self.subTest(source=source):
                parts = self.render(source)
                self.assertEqual(''.join(part['text'] for part in parts), source)
                self.assertFalse(any('<a ' in part['html'] for part in parts))

    def test_url_attribute_injection_is_escaped(self):
        source = '[safe](https://example.com/\"onmouseover=\"alert)'  # No whitespace.
        part = self.render(source)[0]
        parser = StrictHTML()
        parser.feed(part['html'])
        parser.finish()
        self.assertEqual(parser.anchors, ['https://example.com/"onmouseover="alert'])
        self.assertIn('&quot;', part['html'])

    def test_raw_html_and_entities_are_literal_including_xss_payload(self):
        source = '<a href="javascript:alert(1)">click</a><img src=x onerror=alert(1)> &amp; &#60;'
        part = self.render(source)[0]
        self.assertEqual(part['text'], source)
        self.assertNotIn('<img', part['html'])
        self.assertNotIn('<a href=', part['html'])
        self.assertIn('&amp;amp;', part['html'])
        self.assertIn('&amp;#60;', part['html'])

    def test_lists_keep_line_order_and_readable_markers(self):
        source = '- **第一项**\n- `第二项`\n1. 第三项\n2. 第四项\n'
        self.assertEqual(self.visible(source), '- 第一项\n- 第二项\n1. 第三项\n2. 第四项\n')

    def test_simple_tables_degrade_to_readable_rows_without_alignment_rule(self):
        source = '| 功能 | 状态 |\n| :--- | ---: |\n| **图片** | 已支持 |\n| 进度 | `正常` |\n'
        self.assertEqual(self.visible(source), '功能 | 状态\n图片 | 已支持\n进度 | 正常\n')
        ordinary = 'this | is just a line\nnot a table\n'
        self.assertEqual(self.visible(ordinary), ordinary)

    def test_unsupported_images_headings_and_italics_remain_readable_literal(self):
        source = '# Heading\n*italic* ~~strike~~ ![preview](https://example.com/image.png)\n'
        self.assertEqual(self.visible(source), source)

    def test_backslash_escapes_are_visible_text_without_triggering_formatting(self):
        source = r'\*\*literal\*\* \`code\` \[notalink\](url) C:\folder'
        self.assertEqual(self.visible(source), '**literal** `code` [notalink](url) C:\\folder')
        self.assertNotIn('<b>', self.render(source)[0]['html'])

    def test_long_unicode_codeblock_chunks_close_and_reopen_pre_without_loss(self):
        code = ('😀&<>中文\n' * 2500) + 'tail\n'
        parts = self.render('```python\n' + code + '```', limit=127)
        self.assertGreater(len(parts), 100)
        self.assertEqual(''.join(part['text'] for part in parts), code)
        self.assertTrue(all(part['html'].startswith('<pre>') and part['html'].endswith('</pre>')
                            for part in parts))

    def test_entity_escaping_and_styled_chunks_never_split_an_entity_or_tag(self):
        source = '**' + ('<&😀"' * 100) + '**'
        parts = self.render(source, limit=7)
        self.assertEqual(''.join(part['text'] for part in parts), '<&😀"' * 100)
        self.assertTrue(all(part['html'].startswith('<b>') and part['html'].endswith('</b>')
                            for part in parts))

    def test_long_links_and_nested_formatting_reopen_valid_tags_per_part(self):
        source = '**[' + '标签😀' * 20 + '](https://example.com/a?x=1&y=2)**'
        parts = self.render(source, limit=11)
        self.assertEqual(''.join(part['text'] for part in parts),
                         '标签😀' * 20 + ' (https://example.com/a?x=1&y=2)')

    def test_unusually_long_destination_stays_literal(self):
        source = '[label](https://example.com/' + 'a' * 3000 + ')'
        self.assertEqual(self.visible(source, limit=100), source)

    def test_long_broken_bracket_sequence_is_preserved_in_one_pass(self):
        source = '[' * 20000 + '](https://example.com/no-close'
        self.assertEqual(self.visible(source, limit=500), source)

    def test_small_limits_and_exact_boundaries_do_not_drop_characters(self):
        for limit in (2, 3, 4, 7, 17):
            with self.subTest(limit=limit):
                self.assertEqual(self.visible('a😀中😀b\nend', limit), 'a😀中😀b\nend')
        self.assertEqual(self.visible('abc', 1), 'abc')
        with self.assertRaises(ValueError):
            telegram_render.render_parts('😀', 1)

    def test_empty_malformed_and_invalid_limits_are_explicit(self):
        for value in ('', None, [], {}):
            self.assertEqual(telegram_render.render_parts(value), [])
        for limit in (0, -1, None, True, 2.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                telegram_render.render_parts('text', limit)


if __name__ == '__main__':
    unittest.main()
