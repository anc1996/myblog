"""Regression checks for the shared third-party playback rules."""

from django.test import SimpleTestCase
from wagtail.rich_text import RichText

from content_blocks.blocks.embed import CustomEmbedBlock
from content_blocks.blocks.rich_text import CustomRichTextBlock
from content_blocks.embeds import list_supported_providers, render_embed_wrapper_html, resolve_embed_spec
from content_blocks.embeds.forms import ControlledEmbedForm
from content_blocks.sanitizer import sanitize_html


class EmbedPlaybackTests(SimpleTestCase):
    def test_spotify_share_and_player_links_resolve_to_official_addresses(self):
        spotify_id = "4cOdK2wGLETKBW3PvgPWqT"
        for kind in ("track", "playlist", "album", "artist", "episode", "show"):
            for prefix in ("", "intl-de/", "embed/"):
                url = f"https://open.spotify.com/{prefix}{kind}/{spotify_id}?si=share"
                with self.subTest(url=url):
                    spec, reason = resolve_embed_spec(url)
                    self.assertIsNotNone(spec, reason)
                    self.assertEqual(spec.kind, "audio")
                    self.assertEqual(spec.url, f"https://open.spotify.com/{kind}/{spotify_id}")
                    self.assertEqual(spec.embed_src, f"https://open.spotify.com/embed/{kind}/{spotify_id}")

    def test_spotify_rejects_unsupported_hosts_and_paths(self):
        for url in (
            "https://open.spotify.com.evil.example/track/4cOdK2wGLETKBW3PvgPWqT",
            "https://open.spotify.com/user/4cOdK2wGLETKBW3PvgPWqT",
            "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT/extra",
            "https://open.spotify.com/embed/track/invalid-id",
        ):
            with self.subTest(url=url):
                self.assertEqual(render_embed_wrapper_html(url), "")

    def test_spotify_player_is_kept_in_both_editing_paths(self):
        url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
        form = ControlledEmbedForm(data={"title": "歌曲标题", "url": url})
        self.assertTrue(form.is_valid(), form.errors)
        rich_text = RichText(f'<embed embedtype="media" url="{url}" title="歌曲标题"/>')
        for output in (
            CustomEmbedBlock().render_clean({"title": "歌曲标题", "embed_url": url}).html,
            CustomRichTextBlock().render_clean(rich_text).html,
        ):
            self.assertIn(f'data-embed-url="{url}"', output)
            self.assertIn('data-embed-title="歌曲标题"', output)
            self.assertIn(
                'data-embed-src="https://open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT"',
                output,
            )

    def test_vimeo_keeps_unlisted_hash_in_both_links(self):
        for url in (
            "https://vimeo.com/12345678/aabbcc1234",
            "https://player.vimeo.com/video/12345678?h=aabbcc1234&autoplay=1",
        ):
            with self.subTest(url=url):
                spec, reason = resolve_embed_spec(url)
                self.assertIsNotNone(spec, reason)
                self.assertEqual(spec.url, "https://vimeo.com/12345678/aabbcc1234")
                self.assertEqual(
                    spec.embed_src, "https://player.vimeo.com/video/12345678?h=aabbcc1234"
                )

    def test_existing_vimeo_sample_gets_official_embed_url(self):
        spec, _ = resolve_embed_spec("https://vimeo.com/76979871")
        self.assertEqual(
            spec.embed_src, "https://player.vimeo.com/video/76979871?h=8272103f6e"
        )

    def test_existing_tencent_placeholder_does_not_create_player(self):
        result = render_embed_wrapper_html("https://v.qq.com/x/page/abc123456.html")
        self.assertIn("无效的占位链接", result)
        self.assertNotIn("abc123456", result)
        self.assertNotIn("data-embed-src=", result)

    def test_tencent_real_video_keeps_player(self):
        spec, _ = resolve_embed_spec("https://v.qq.com/x/page/i0145yslf2c.html")
        self.assertEqual(spec.embed_src, "https://v.qq.com/txp/iframe/player.html?vid=i0145yslf2c")

    def test_qq_music_no_longer_offered_in_embed_chooser(self):
        self.assertNotIn("qq_music", {provider["key"] for provider in list_supported_providers()})
        for url in (
            "https://i.y.qq.com/v8/playsong.html?songmid=001Qu4I30eVFYb",
            "https://y.qq.com/n/ryqq/songDetail/001Qu4I30eVFYb",
        ):
            with self.subTest(url=url):
                form = ControlledEmbedForm(data={"title": "歌曲", "url": url})
                self.assertFalse(form.is_valid())
                self.assertIn("url", form.errors)
                self.assertEqual(render_embed_wrapper_html(url), "")

    def test_existing_qq_music_rich_text_does_not_render_card_but_keeps_normal_link(self):
        url = "https://y.qq.com/n/ryqq/songDetail/001Qu4I30eVFYb"
        value = RichText(
            f'<p><a href="{url}">原曲链接</a></p>'
            f'<embed embedtype="media" url="{url}" title="旧音乐卡片"/>'
        )
        html = CustomRichTextBlock().render_clean(value).html
        self.assertNotIn("data-embed-", html)
        self.assertIn(f'href="{url}"', html)

    def test_sanitizer_only_keeps_registry_player_urls(self):
        html = sanitize_html(
            '<div data-embed-src="https://example.org/player"></div>'
            '<div data-embed-src="https://player.vimeo.com/video/12345678?h=aabbcc1234"></div>'
            '<div data-embed-url="https://example.org/"></div>'
        )
        self.assertNotIn("example.org", html)
        self.assertIn('data-embed-src="https://player.vimeo.com/video/12345678?h=aabbcc1234"', html)
