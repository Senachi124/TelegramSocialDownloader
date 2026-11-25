"""Post-to-Stories author resolution uses the real extractor lifecycle, offline."""
import unittest
from unittest.mock import Mock, patch

from media_bot import instagram
from gallery_dl import config, extractor
from gallery_dl.extractor.instagram import InstagramPostExtractor


class StoriesTargetTests(unittest.TestCase):
    def tearDown(self):config.clear()

    def test_real_extractor_is_initialized_before_author_resolution(self):
        config.set(('extractor','instagram'),'cookies',{'sessionid':'fixture','csrftoken':'fixture'})
        source=extractor.find('https://www.instagram.com/p/ABCfixture/')
        def items(instance):
            self.assertEqual(instance.cookies.get('sessionid'),'fixture')
            self.assertTrue(hasattr(instance,'api'))
            yield (2,'',{'username':'author.name'})
        with patch('gallery_dl.extractor.find',return_value=source), \
             patch.object(InstagramPostExtractor,'items',items), \
             patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden')) as network:
            self.assertEqual(instagram.target(source.url,'stories'),'https://www.instagram.com/stories/author.name/')
        network.assert_not_called()

    def test_resolution_closes_iterator_and_sessions(self):
        source=Mock();closed=[]
        def items():
            try:yield (2,'',{'username':'author'})
            finally:closed.append(True)
        source.items.return_value=items()
        with patch('gallery_dl.extractor.find',return_value=source):
            self.assertEqual(instagram.target('https://instagram.com/reel/ABC/','stories'),'https://www.instagram.com/stories/author/')
        source.initialize.assert_called_once_with()
        source.finalize.assert_called_once_with(0)
        source.session.close.assert_called_once_with()
        self.assertEqual(closed,[True])

    def test_invalid_author_metadata_is_not_used_as_url(self):
        for metadata in (None,{}, {'username':None},{'username':123},{'username':'../bad'}):
            with self.subTest(metadata=metadata):
                source=Mock();source.items.return_value=iter([(2,'',metadata)])
                with patch('gallery_dl.extractor.find',return_value=source),self.assertRaisesRegex(ValueError,'無法識別貼文作者'):
                    instagram.target('https://instagram.com/p/ABC/','stories')
                source.finalize.assert_called_once_with(1)
                source.session.close.assert_called_once_with()

    def test_upstream_errors_propagate_and_release_session(self):
        source=Mock();source.items.side_effect=RuntimeError('fixture upstream error')
        with patch('gallery_dl.extractor.find',return_value=source),self.assertRaisesRegex(RuntimeError,'fixture upstream error'):
            instagram.target('https://instagram.com/p/ABC/','stories')
        source.finalize.assert_called_once_with(1)
        source.session.close.assert_called_once_with()

    def test_initialization_failure_does_not_finalize_partial_extractor(self):
        source=Mock();source.initialize.side_effect=RuntimeError('fixture init failure')
        with patch('gallery_dl.extractor.find',return_value=source),self.assertRaisesRegex(RuntimeError,'fixture init failure'):
            instagram.target('https://instagram.com/p/ABC/','stories')
        source.finalize.assert_not_called();source.session.close.assert_called_once_with()

    def test_non_story_and_profile_targets_preserved(self):
        url='https://instagram.com/p/ABC/'
        self.assertEqual(instagram.target(url,'post'),url)
        self.assertEqual(instagram.target('https://instagram.com/stories/name/123/','stories'),'https://www.instagram.com/stories/name/')

    def test_unrecognized_extractor_has_explicit_failure(self):
        with patch('gallery_dl.extractor.find',return_value=None),self.assertRaisesRegex(ValueError,'無法識別貼文作者'):
            instagram.target('https://instagram.com/p/ABC/','stories')


if __name__=='__main__':unittest.main()
