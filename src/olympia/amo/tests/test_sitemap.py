import datetime
import os
from collections import namedtuple
from unittest import mock

from django.conf import settings
from django.urls import reverse

from olympia.amo.sitemap import (
    AddonSitemap,
    AMOSitemap,
    build_sitemap,
    get_sitemap_path,
    get_sitemap_section_pages,
    sitemaps,
)
from olympia.amo.tests import addon_factory

from .test_views import TEST_SITEMAPS_DIR


def test_addon_sitemap():
    addon_a = addon_factory()
    addon_b = addon_factory()
    sitemap = AddonSitemap()
    assert list(sitemap.items()) == [
        (addon_a.last_updated, addon_a.slug),
        (addon_b.last_updated, addon_b.slug),
    ]
    for item in sitemap.items():
        assert sitemap.location(item) == reverse('addons.detail', args=[item.slug])
        assert '/en-US/firefox/' in sitemap.location(item)
        assert sitemap.lastmod(item) == item.last_updated


def test_amo_sitemap():
    sitemap = AMOSitemap()
    for item in sitemap.items():
        assert sitemap.location(item) == reverse(item)


def test_get_sitemap_section_pages():
    addon_factory()
    addon_factory()
    addon_factory()
    assert list(sitemaps.keys()) == ['amo', 'addons']

    pages = get_sitemap_section_pages()
    assert pages == [
        ('amo', 1),
        ('addons', 1),
    ]
    with mock.patch.object(AddonSitemap, 'limit', 2):
        pages = get_sitemap_section_pages()
        assert pages == [
            ('amo', 1),
            ('addons', 1),
            ('addons', 2),
        ]


def test_build_sitemap():
    # test the index sitemap build first
    with mock.patch('olympia.amo.sitemap.get_sitemap_section_pages') as pages_mock:
        pages_mock.return_value = [
            ('amo', 1),
            ('addons', 1),
            ('addons', 2),
        ]
        built = build_sitemap()

        with open(os.path.join(TEST_SITEMAPS_DIR, 'sitemap.xml')) as sitemap:
            assert built == sitemap.read()

    # then a section build
    def items_mock(self):
        AddonValuesList = namedtuple('AddonValuesList', 'last_updated,slug')
        return [
            AddonValuesList(
                datetime.datetime(2020, 10, 2, 0, 0, 0), 'delicious-pierogi'
            ),
            AddonValuesList(datetime.datetime(2020, 10, 1, 0, 0, 0), 'swanky-curry'),
            AddonValuesList(datetime.datetime(2020, 9, 30, 0, 0, 0), 'spicy-pierogi'),
        ]

    with mock.patch.object(AddonSitemap, 'items', items_mock):
        built = build_sitemap('addons')

        with open(os.path.join(TEST_SITEMAPS_DIR, 'sitemap-addons-2.xml')) as sitemap:
            assert built == sitemap.read()


def test_get_sitemap_path():
    path = settings.SITEMAP_STORAGE_PATH
    assert get_sitemap_path() == f'{path}/sitemap.xml'
    assert get_sitemap_path('foo') == f'{path}/sitemap-foo.xml'
    assert get_sitemap_path('foo', 1) == f'{path}/sitemap-foo.xml'
    assert get_sitemap_path('foo', 2) == f'{path}/sitemap-foo-2.xml'
