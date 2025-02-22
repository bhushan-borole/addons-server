import os
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.files.storage import default_storage as storage
from django.utils import translation

import pytest
from freezegun import freeze_time

from waffle.testutils import override_switch

from olympia import amo, core
from olympia.addons.models import Addon
from olympia.amo.tests import (
    addon_factory,
    get_random_ip,
    req_factory_factory,
    TestCase,
    user_factory,
)
from olympia.amo.tests.test_helpers import get_image_path
from olympia.amo.utils import rm_local_tmp_dir
from olympia.applications.models import AppVersion
from olympia.devhub import forms
from olympia.files.models import FileUpload
from olympia.signing.views import VersionView
from olympia.tags.models import AddonTag, Tag
from olympia.versions.models import ApplicationsVersions


class TestNewUploadForm(TestCase):
    def test_firefox_default_selected(self):
        upload = FileUpload.objects.create(valid=False)
        data = {'upload': upload.uuid}
        request = req_factory_factory('/', post=True, data=data)
        request.user = user_factory()
        form = forms.NewUploadForm(data, request=request)
        assert form.fields['compatible_apps'].initial == [amo.FIREFOX.id]

    def test_previous_compatible_apps_initially_selected(self):
        addon = addon_factory()

        appversion = AppVersion.objects.create(
            application=amo.ANDROID.id, version='1.0'
        )
        ApplicationsVersions.objects.create(
            version=addon.current_version,
            application=amo.ANDROID.id,
            min=appversion,
            max=appversion,
        )

        upload = FileUpload.objects.create(valid=False)
        data = {'upload': upload.uuid}
        request = req_factory_factory('/', post=True, data=data)
        request.user = user_factory()

        # Without an add-on, we only pre-select the default which is Firefox
        form = forms.NewUploadForm(data, request=request)
        assert form.fields['compatible_apps'].initial == [amo.FIREFOX.id]

        # with an add-on provided we pre-select the applications based on the
        # current version
        form = forms.NewUploadForm(data, request=request, addon=addon)
        assert form.fields['compatible_apps'].initial == [
            amo.FIREFOX.id,
            amo.ANDROID.id,
        ]

    def test_compat_apps_widget_custom_label_class_rendered(self):
        """We are setting a custom class at the label
        of the compatibility apps multi-select to correctly render
        images.
        """
        upload = FileUpload.objects.create(valid=False)
        data = {'upload': upload.uuid}
        request = req_factory_factory('/', post=True, data=data)
        request.user = user_factory()
        form = forms.NewUploadForm(data, request=request)
        result = form.fields['compatible_apps'].widget.render(
            name='compatible_apps', value=amo.FIREFOX.id
        )
        assert 'class="app firefox"' in result

        result = form.fields['compatible_apps'].widget.render(
            name='compatible_apps', value=amo.ANDROID.id
        )
        assert 'class="app android"' in result

    def test_only_valid_uploads(self):
        upload = FileUpload.objects.create(valid=False)
        upload = FileUpload.objects.create(valid=False)
        data = {'upload': upload.uuid, 'compatible_apps': [amo.FIREFOX.id]}
        request = req_factory_factory('/', post=True, data=data)
        request.user = user_factory()
        form = forms.NewUploadForm(data, request=request)
        assert (
            'There was an error with your upload. Please try again.'
            in form.errors.get('__all__')
        ), (form.errors)

        # Admin override makes the form ignore the brokenness
        with mock.patch('olympia.access.acl.action_allowed_user') as acl:
            # For the 'Addons:Edit' permission check.
            acl.return_value = True
            data['admin_override_validation'] = True
            form = forms.NewUploadForm(data, request=request)
            assert (
                'There was an error with your upload. Please try'
                not in form.errors.get('__all__')
            ), form.errors

        upload.validation = '{"errors": 0}'
        upload.save()
        addon = Addon.objects.create()
        data.pop('admin_override_validation')
        form = forms.NewUploadForm(data, request=request, addon=addon)
        assert (
            'There was an error with your upload. Please try again.'
            not in form.errors.get('__all__')
        ), form.errors

    @mock.patch('olympia.devhub.forms.parse_addon')
    def test_throttling(self, parse_addon_mock):
        upload = FileUpload.objects.create(valid=True, name='foo.xpi')
        data = {'upload': upload.uuid, 'compatible_apps': [amo.FIREFOX.id]}
        request = req_factory_factory('/', post=True, data=data)
        request.user = user_factory()
        request.META['REMOTE_ADDR'] = '5.6.7.8'
        with freeze_time('2019-04-08 15:16:23.42') as frozen_time:
            for x in range(0, 6):
                self._add_fake_throttling_action(
                    view_class=VersionView,
                    url='/',
                    user=request.user,
                    remote_addr=get_random_ip(),
                )

            form = forms.NewUploadForm(data, request=request)
            assert not form.is_valid()
            assert form.errors.get('__all__') == [
                'You have submitted too many uploads recently. '
                'Please try again after some time.'
            ]

            frozen_time.tick(delta=timedelta(seconds=61))
            form = forms.NewUploadForm(data, request=request)
            assert form.is_valid()

    # Those three patches are so files.utils.parse_addon doesn't fail on a
    # non-existent file even before having a chance to call check_xpi_info.
    @mock.patch('olympia.files.utils.Extractor.parse')
    @mock.patch('olympia.files.utils.extract_xpi', lambda xpi, path: None)
    @mock.patch('olympia.files.utils.get_file', lambda xpi: None)
    # This is the one we want to test.
    @mock.patch('olympia.files.utils.check_xpi_info')
    def test_check_xpi_called(self, mock_check_xpi_info, mock_parse):
        """Make sure the check_xpi_info helper is called.

        There's some important checks made in check_xpi_info, if we ever
        refactor the form to not call it anymore, we need to make sure those
        checks are run at some point.
        """
        mock_parse.return_value = None
        mock_check_xpi_info.return_value = {'name': 'foo', 'type': 2}
        upload = FileUpload.objects.create(valid=True, name='foo.xpi')
        addon = Addon.objects.create()
        data = {'upload': upload.uuid, 'compatible_apps': [amo.FIREFOX.id]}
        request = req_factory_factory('/', post=True, data=data)
        request.user = user_factory()
        form = forms.NewUploadForm(data, addon=addon, request=request)
        form.clean()
        assert mock_check_xpi_info.called


class TestCompatForm(TestCase):
    fixtures = ['base/addon_3615']

    def setUp(self):
        super().setUp()
        AppVersion.objects.create(application=amo.ANDROID.id, version='50.0')
        AppVersion.objects.create(application=amo.ANDROID.id, version='56.0')
        AppVersion.objects.create(application=amo.FIREFOX.id, version='56.0')
        AppVersion.objects.create(application=amo.FIREFOX.id, version='56.*')
        AppVersion.objects.create(application=amo.FIREFOX.id, version='57.0')
        AppVersion.objects.create(application=amo.FIREFOX.id, version='57.*')

    def test_forms(self):
        version = Addon.objects.get(id=3615).current_version
        formset = forms.CompatFormSet(
            None, queryset=version.apps.all(), form_kwargs={'version': version}
        )
        apps = [form.app for form in formset.forms]
        assert set(apps) == set(amo.APP_USAGE)

    def test_form_initial(self):
        version = Addon.objects.get(id=3615).current_version
        current_min = version.apps.filter(application=amo.FIREFOX.id).get().min
        current_max = version.apps.filter(application=amo.FIREFOX.id).get().max
        formset = forms.CompatFormSet(
            None, queryset=version.apps.all(), form_kwargs={'version': version}
        )
        form = formset.forms[0]
        assert form.app == amo.FIREFOX
        assert form.initial['application'] == amo.FIREFOX.id
        assert form.initial['min'] == current_min.pk
        assert form.initial['max'] == current_max.pk

    def _test_form_choices_expect_all_versions(self, version):
        expected_min_choices = [('', '---------')] + list(
            AppVersion.objects.filter(application=amo.FIREFOX.id)
            .exclude(version__contains='*')
            .values_list('pk', 'version')
            .order_by('version_int')
        )
        expected_max_choices = [('', '---------')] + list(
            AppVersion.objects.filter(application=amo.FIREFOX.id)
            .values_list('pk', 'version')
            .order_by('version_int')
        )

        formset = forms.CompatFormSet(
            None, queryset=version.apps.all(), form_kwargs={'version': version}
        )
        form = formset.forms[0]
        assert form.app == amo.FIREFOX
        assert list(form.fields['min'].choices) == expected_min_choices
        assert list(form.fields['max'].choices) == expected_max_choices

    def test_form_choices(self):
        version = Addon.objects.get(id=3615).current_version
        version.files.all().update(is_webextension=True)
        del version.all_files
        self._test_form_choices_expect_all_versions(version)

    def test_form_choices_no_compat(self):
        version = Addon.objects.get(id=3615).current_version
        version.files.all().update(is_webextension=False)
        version.addon.update(type=amo.ADDON_DICT)
        del version.all_files
        self._test_form_choices_expect_all_versions(version)

    def test_form_choices_language_pack(self):
        version = Addon.objects.get(id=3615).current_version
        version.files.all().update(is_webextension=False)
        version.addon.update(type=amo.ADDON_LPAPP)
        del version.all_files
        self._test_form_choices_expect_all_versions(version)

    def test_form_choices_legacy(self):
        version = Addon.objects.get(id=3615).current_version
        version.files.all().update(is_webextension=False)
        del version.all_files

        firefox_57 = AppVersion.objects.get(application=amo.FIREFOX.id, version='57.0')
        firefox_57_s = AppVersion.objects.get(
            application=amo.FIREFOX.id, version='57.*'
        )

        expected_min_choices = [('', '---------')] + list(
            AppVersion.objects.filter(application=amo.FIREFOX.id)
            .exclude(version__contains='*')
            .exclude(pk__in=(firefox_57.pk, firefox_57_s.pk))
            .values_list('pk', 'version')
            .order_by('version_int')
        )
        expected_max_choices = [('', '---------')] + list(
            AppVersion.objects.filter(application=amo.FIREFOX.id)
            .exclude(pk__in=(firefox_57.pk, firefox_57_s.pk))
            .values_list('pk', 'version')
            .order_by('version_int')
        )

        formset = forms.CompatFormSet(
            None, queryset=version.apps.all(), form_kwargs={'version': version}
        )
        form = formset.forms[0]
        assert form.app == amo.FIREFOX
        assert list(form.fields['min'].choices) == expected_min_choices
        assert list(form.fields['max'].choices) == expected_max_choices

        expected_an_choices = [('', '---------')] + list(
            AppVersion.objects.filter(application=amo.ANDROID.id)
            .values_list('pk', 'version')
            .order_by('version_int')
        )
        form = formset.forms[1]
        assert form.app == amo.ANDROID
        assert list(form.fields['min'].choices) == expected_an_choices
        assert list(form.fields['max'].choices) == expected_an_choices

    def test_form_choices_mozilla_signed_legacy(self):
        version = Addon.objects.get(id=3615).current_version
        version.files.all().update(
            is_webextension=False, is_mozilla_signed_extension=True
        )
        del version.all_files
        self._test_form_choices_expect_all_versions(version)

    def test_static_theme(self):
        version = Addon.objects.get(id=3615).current_version
        version.files.all().update(is_webextension=True)
        version.addon.update(type=amo.ADDON_STATICTHEME)
        del version.all_files
        self._test_form_choices_expect_all_versions(version)

        formset = forms.CompatFormSet(
            None, queryset=version.apps.all(), form_kwargs={'version': version}
        )
        assert formset.can_delete is False  # No deleting Firefox app plz.
        assert formset.extra == 0  # And lets not extra apps be added.


class TestPreviewForm(TestCase):
    fixtures = ['base/addon_3615']

    def setUp(self):
        super().setUp()
        self.dest = os.path.join(settings.TMP_PATH, 'preview')
        if not os.path.exists(self.dest):
            os.makedirs(self.dest)

    @mock.patch('olympia.amo.models.ModelBase.update')
    def test_preview_modified(self, update_mock):
        addon = Addon.objects.get(pk=3615)
        name = 'transparent.png'
        form = forms.PreviewForm(
            {'caption': 'test', 'upload_hash': name, 'position': 1}
        )
        with storage.open(os.path.join(self.dest, name), 'wb') as f:
            shutil.copyfileobj(open(get_image_path(name), 'rb'), f)
        assert form.is_valid()
        form.save(addon)
        assert update_mock.called

    def test_preview_transparency(self):
        addon = Addon.objects.get(pk=3615)
        name = 'transparent-cotton'
        hash = '12345678abcd'
        form = forms.PreviewForm(
            {'caption': 'test', 'upload_hash': hash, 'position': 1}
        )
        with storage.open(os.path.join(self.dest, hash), 'wb') as f:
            shutil.copyfileobj(open(get_image_path(name + '.png'), 'rb'), f)
        assert form.is_valid()
        form.save(addon)
        preview = addon.previews.all()[0]
        assert os.path.exists(preview.thumbnail_path)
        with storage.open(preview.thumbnail_path, 'rb') as thumb_file, open(
            get_image_path(name + '.jpg'), 'rb'
        ) as sample_file:
            assert thumb_file.read() == sample_file.read()

    @mock.patch('olympia.amo.utils.pngcrush_image')
    def test_preview_size(self, pngcrush_image_mock):
        addon = Addon.objects.get(pk=3615)
        name = 'teamaddons.jpg'
        form = forms.PreviewForm(
            {'caption': 'test', 'upload_hash': name, 'position': 1}
        )
        with storage.open(os.path.join(self.dest, name), 'wb') as f:
            shutil.copyfileobj(open(get_image_path(name), 'rb'), f)
        assert form.is_valid()
        form.save(addon)
        preview = addon.previews.all()[0]
        assert preview.sizes == (
            {
                'image': [2400, 1600],
                'thumbnail': [533, 355],
                'original': [3000, 2000],
                'thumbnail_format': 'jpg',
            }
        )
        assert os.path.exists(preview.image_path)
        assert os.path.exists(preview.thumbnail_path)
        assert os.path.exists(preview.original_path)

        assert pngcrush_image_mock.call_count == 1  # the thumbnail isn't a png now
        assert pngcrush_image_mock.call_args_list[0][0][0] == (preview.image_path)


class TestDistributionChoiceForm(TestCase):
    @pytest.mark.needs_locales_compilation
    def test_lazy_choice_labels(self):
        """Tests that the labels in `choices` are still lazy

        We had a problem that the labels weren't properly marked as lazy
        which led to labels being returned in mixed languages depending
        on what server we hit in production.
        """
        with translation.override('en-US'):
            form = forms.DistributionChoiceForm()
            label = form.fields['channel'].choices[0][1]

            expected = 'On this site.'
            label = str(label)
            assert label.startswith(expected)

        with translation.override('de'):
            form = forms.DistributionChoiceForm()
            label = form.fields['channel'].choices[0][1]

            expected = 'Auf dieser Website.'
            label = str(label)
            assert label.startswith(expected)

    def test_choices_addon(self):
        # No add-on passed, all choices are present.
        form = forms.DistributionChoiceForm()
        assert len(form.fields['channel'].choices) == 2
        assert form.fields['channel'].choices[0][0] == 'listed'
        assert form.fields['channel'].choices[1][0] == 'unlisted'

        # Regular add-on, all choices are present.
        addon = addon_factory()
        form = forms.DistributionChoiceForm(addon=addon)
        assert len(form.fields['channel'].choices) == 2
        assert form.fields['channel'].choices[0][0] == 'listed'
        assert form.fields['channel'].choices[1][0] == 'unlisted'

        # "Invisible" addons don't get to choose "On this site.".
        addon.disabled_by_user = True
        form = forms.DistributionChoiceForm(addon=addon)
        assert len(form.fields['channel'].choices) == 1
        assert form.fields['channel'].choices[0][0] == 'unlisted'

        # Back to normal.
        addon.disabled_by_user = False
        form = forms.DistributionChoiceForm(addon=addon)
        assert len(form.fields['channel'].choices) == 2
        assert form.fields['channel'].choices[0][0] == 'listed'
        assert form.fields['channel'].choices[1][0] == 'unlisted'


class TestDescribeForm(TestCase):
    fixtures = ('base/addon_3615', 'addons/denied')

    def setUp(self):
        super().setUp()
        self.existing_name = 'Delicious Bookmarks'
        self.non_existing_name = 'Does Not Exist'
        self.error_msg = 'This name is already in use. Please choose another.'
        self.request = req_factory_factory('/')

    def test_slug_deny(self):
        delicious = Addon.objects.get()
        form = forms.DescribeForm(
            {'slug': 'submit'}, request=self.request, instance=delicious
        )
        assert not form.is_valid()
        assert form.errors['slug'] == (
            ['The slug cannot be "submit". Please choose another.']
        )

    def test_name_trademark_mozilla(self):
        delicious = Addon.objects.get()
        form = forms.DescribeForm(
            {'name': 'Delicious Mozilla', 'summary': 'foô', 'slug': 'bar'},
            request=self.request,
            instance=delicious,
        )

        assert not form.is_valid()
        assert (
            form.errors['name']
            .data[0]
            .message.startswith(
                'Add-on names cannot contain the Mozilla or Firefox trademarks.'
            )
        )

    def test_name_trademark_firefox(self):
        delicious = Addon.objects.get()
        form = forms.DescribeForm(
            {'name': 'Delicious Firefox', 'summary': 'foö', 'slug': 'bar'},
            request=self.request,
            instance=delicious,
        )
        assert not form.is_valid()
        assert (
            form.errors['name']
            .data[0]
            .message.startswith(
                'Add-on names cannot contain the Mozilla or Firefox trademarks.'
            )
        )

    def test_name_trademark_allowed_for_prefix(self):
        delicious = Addon.objects.get()
        form = forms.DescribeForm(
            {'name': 'Delicious for Mozilla', 'summary': 'foø', 'slug': 'bar'},
            request=self.request,
            instance=delicious,
        )

        assert form.is_valid()

    def test_name_no_trademark(self):
        delicious = Addon.objects.get()
        form = forms.DescribeForm(
            {'name': 'Delicious Dumdidum', 'summary': 'đoo', 'slug': 'bar'},
            request=self.request,
            instance=delicious,
        )

        assert form.is_valid()

    def test_slug_isdigit(self):
        delicious = Addon.objects.get()
        form = forms.DescribeForm(
            {'slug': '123'}, request=self.request, instance=delicious
        )
        assert not form.is_valid()
        assert form.errors['slug'] == (
            ['The slug cannot be "123". Please choose another.']
        )

    def test_bogus_support_url(self):
        form = forms.DescribeForm(
            {'support_url': 'javascript://something.com'},
            request=self.request,
            instance=Addon.objects.get(),
        )
        assert not form.is_valid()
        assert form.errors['support_url'] == ['Enter a valid URL.']

    def test_ftp_support_url(self):
        form = forms.DescribeForm(
            {'support_url': 'ftp://foo.com'},
            request=self.request,
            instance=Addon.objects.get(),
        )
        assert not form.is_valid()
        assert form.errors['support_url'] == ['Enter a valid URL.']

    def test_http_support_url(self):
        form = forms.DescribeForm(
            {
                'name': 'Delicious Dumdidum',
                'summary': 'foo',
                'slug': 'bar',
                'support_url': 'http://foo.com',
            },
            request=self.request,
            instance=Addon.objects.get(),
        )
        assert form.is_valid(), form.errors

    def test_description_optional(self):
        delicious = Addon.objects.get()
        assert delicious.type == amo.ADDON_EXTENSION

        with override_switch('content-optimization', active=False):
            form = forms.DescribeForm(
                {'name': 'Delicious for everyone', 'summary': 'foo', 'slug': 'bar'},
                request=self.request,
                instance=delicious,
            )
            assert form.is_valid(), form.errors

        with override_switch('content-optimization', active=True):
            form = forms.DescribeForm(
                {'name': 'Delicious for everyone', 'summary': 'foo', 'slug': 'bar'},
                request=self.request,
                instance=delicious,
            )
            assert not form.is_valid()

            # But only extensions are required to have a description
            delicious.update(type=amo.ADDON_STATICTHEME)
            form = forms.DescribeForm(
                {'name': 'Delicious for everyone', 'summary': 'foo', 'slug': 'bar'},
                request=self.request,
                instance=delicious,
            )
            assert form.is_valid(), form.errors

            #  Do it again, but this time with a description
            delicious.update(type=amo.ADDON_EXTENSION)
            form = forms.DescribeForm(
                {
                    'name': 'Delicious for everyone',
                    'summary': 'foo',
                    'slug': 'bar',
                    'description': 'its a description',
                },
                request=self.request,
                instance=delicious,
            )
            assert form.is_valid(), form.errors

    def test_description_min_length(self):
        delicious = Addon.objects.get()
        assert delicious.type == amo.ADDON_EXTENSION

        with override_switch('content-optimization', active=False):
            form = forms.DescribeForm(
                {
                    'name': 'Delicious for everyone',
                    'summary': 'foo',
                    'slug': 'bar',
                    'description': '123456789',
                },
                request=self.request,
                instance=delicious,
            )
            assert form.is_valid(), form.errors

        with override_switch('content-optimization', active=True):
            form = forms.DescribeForm(
                {
                    'name': 'Delicious for everyone',
                    'summary': 'foo',
                    'slug': 'bar',
                    'description': '123456789',
                },
                request=self.request,
                instance=delicious,
            )
            assert not form.is_valid()

            # But only extensions have a minimum length
            delicious.update(type=amo.ADDON_STATICTHEME)
            form = forms.DescribeForm(
                {
                    'name': 'Delicious for everyone',
                    'summary': 'foo',
                    'slug': 'bar',
                    'description': '123456789',
                },
                request=self.request,
                instance=delicious,
            )
            assert form.is_valid()

            #  Do it again, but this time with a longer description
            delicious.update(type=amo.ADDON_EXTENSION)
            form = forms.DescribeForm(
                {
                    'name': 'Delicious for everyone',
                    'summary': 'foo',
                    'slug': 'bar',
                    'description': '1234567890',
                },
                request=self.request,
                instance=delicious,
            )
            assert form.is_valid(), form.errors

    def test_name_summary_lengths(self):
        delicious = Addon.objects.get()
        short_data = {
            'name': 'n',
            'summary': 's',
            'slug': 'bar',
            'description': '1234567890',
        }
        over_70_data = {
            'name': 'this is a name that hits the 50 char limit almost',
            'summary': 'this is a summary that doesn`t get close to the '
            'existing 250 limit but is over 70',
            'slug': 'bar',
            'description': '1234567890',
        }
        under_70_data = {
            'name': 'this is a name that is over the 50 char limit by a few',
            'summary': 'ab',
            'slug': 'bar',
            'description': '1234567890',
        }

        # short name and summary - both allowed with DescribeForm
        form = forms.DescribeForm(short_data, request=self.request, instance=delicious)
        assert form.is_valid()
        # but not with DescribeFormContentOptimization
        form = forms.DescribeFormContentOptimization(
            short_data, request=self.request, instance=delicious
        )
        assert not form.is_valid()
        assert form.errors['name'] == [
            'Ensure this value has at least 2 characters (it has 1).'
        ]
        assert form.errors['summary'] == [
            'Ensure this value has at least 2 characters (it has 1).'
        ]

        # As are long names and summaries
        form = forms.DescribeForm(
            over_70_data, request=self.request, instance=delicious
        )
        assert form.is_valid()
        # but together are over 70 chars so no longer allowed
        form = forms.DescribeFormContentOptimization(
            over_70_data, request=self.request, instance=delicious
        )
        assert not form.is_valid()
        assert len(over_70_data['name']) + len(over_70_data['summary']) == 130
        assert form.errors['name'] == [
            'Ensure name and summary combined are at most 70 characters '
            '(they have 130).'
        ]
        assert 'summary' not in form.errors

        # DescribeForm has a lower limit for name length
        form = forms.DescribeForm(
            under_70_data, request=self.request, instance=delicious
        )
        assert not form.is_valid()
        assert form.errors['name'] == [
            'Ensure this value has at most 50 characters (it has 54).'
        ]
        # DescribeFormContentOptimization only cares that the total is <= 70
        form = forms.DescribeFormContentOptimization(
            under_70_data, request=self.request, instance=delicious
        )
        assert form.is_valid()
        assert len(under_70_data['name']) + len(under_70_data['summary']) == 56

    def test_name_summary_auto_cropping(self):
        delicious = Addon.objects.get()
        assert delicious.default_locale == 'en-US'

        summary_needs_cropping = {
            'name_en-us': 'a' * 25,
            'name_fr': 'b' * 30,
            'summary_en-us': 'c' * 45,
            'summary_fr': 'd' * 45,  # 30 + 45 is > 70
            'slug': 'slug',
            'description_en-us': 'z' * 10,
        }
        form = forms.DescribeFormContentOptimization(
            summary_needs_cropping,
            request=self.request,
            instance=delicious,
            should_auto_crop=True,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data['name']['en-us'] == 'a' * 25  # no change
        assert form.cleaned_data['summary']['en-us'] == 'c' * 45  # no change
        assert form.cleaned_data['name']['fr'] == 'b' * 30  # no change
        assert form.cleaned_data['summary']['fr'] == 'd' * 40  # 45 to 40

        summary_needs_cropping_no_name = {
            'name_en-us': 'a' * 25,
            'summary_en-us': 'c' * 45,
            'summary_fr': 'd' * 50,
            'slug': 'slug',
            'description_en-us': 'z' * 10,
        }
        form = forms.DescribeFormContentOptimization(
            summary_needs_cropping_no_name,
            request=self.request,
            instance=delicious,
            should_auto_crop=True,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data['name']['en-us'] == 'a' * 25
        assert form.cleaned_data['summary']['en-us'] == 'c' * 45
        assert 'fr' not in form.cleaned_data['name']  # we've not added it
        assert form.cleaned_data['summary']['fr'] == 'd' * 45  # 50 to 45

        name_needs_cropping = {
            'name_en-us': 'a' * 67,
            'name_fr': 'b' * 69,
            'summary_en-us': 'c' * 2,
            'summary_fr': 'd' * 3,
            'slug': 'slug',
            'description_en-us': 'z' * 10,
        }
        form = forms.DescribeFormContentOptimization(
            name_needs_cropping,
            request=self.request,
            instance=delicious,
            should_auto_crop=True,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data['name']['en-us'] == 'a' * 67  # no change
        assert form.cleaned_data['summary']['en-us'] == 'c' * 2  # no change
        assert form.cleaned_data['name']['fr'] == 'b' * 68  # 69 to 68
        assert form.cleaned_data['summary']['fr'] == 'd' * 2  # 3 to 2

        name_needs_cropping_no_summary = {
            'name_en-us': 'a' * 50,
            'name_fr': 'b' * 69,
            'summary_en-us': 'c' * 20,
            'slug': 'slug',
            'description_en-us': 'z' * 10,
        }
        form = forms.DescribeFormContentOptimization(
            name_needs_cropping_no_summary,
            request=self.request,
            instance=delicious,
            should_auto_crop=True,
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data['name']['en-us'] == 'a' * 50  # no change
        assert form.cleaned_data['summary']['en-us'] == 'c' * 20  # no change
        assert form.cleaned_data['name']['fr'] == 'b' * 50  # 69 to 50
        assert 'fr' not in form.cleaned_data['summary']


class TestAdditionalDetailsForm(TestCase):
    fixtures = ['base/addon_3615', 'base/users']

    def setUp(self):
        super().setUp()
        self.addon = Addon.objects.get(pk=3615)

        self.data = {
            'default_locale': 'en-US',
            'homepage': str(self.addon.homepage),
        }

        self.user = self.addon.authors.all()[0]
        core.set_user(self.user)
        self.request = req_factory_factory('/')

    def test_locales(self):
        form = forms.AdditionalDetailsForm(request=self.request, instance=self.addon)
        assert form.fields['default_locale'].choices[0][0] == 'af'

    def _get_tag_text(self):
        return [t.tag_text for t in self.addon.tags.all()]

    def test_change_tags(self):
        tag_old = Tag.objects.create(tag_text='old')
        AddonTag.objects.create(tag=tag_old, addon=self.addon)
        assert self._get_tag_text() == ['old']

        tag_bar = Tag.objects.create(tag_text='bar')
        tag_foo = Tag.objects.create(tag_text='foo')
        data = {**self.data, 'tags': ['bar', 'foo']}
        form = forms.AdditionalDetailsForm(
            data=data, request=self.request, instance=self.addon
        )
        assert form.is_valid()
        form.save(self.addon)
        assert self._get_tag_text() == ['bar', 'foo']
        assert tag_bar.reload().num_addons == 1
        assert tag_foo.reload().num_addons == 1
        assert tag_old.reload().num_addons == 0

    def test_cannot_create_new_tags(self):
        data = {**self.data, 'tags': ['bar']}
        form = forms.AdditionalDetailsForm(
            data=data, request=self.request, instance=self.addon
        )
        assert not form.is_valid()
        assert form.errors['tags'] == [
            'Select a valid choice. bar is not one of the available choices.'
        ]

    def test_tags_limit(self):
        extra = Tag.objects.count() - amo.MAX_TAGS
        data = {**self.data, 'tags': [tag.tag_text for tag in Tag.objects.all()]}
        form = forms.AdditionalDetailsForm(
            data=data, request=self.request, instance=self.addon
        )
        assert not form.is_valid()
        assert form.errors['tags'] == [f'You have {extra} too many tags.']

    def test_bogus_homepage(self):
        form = forms.AdditionalDetailsForm(
            {'homepage': 'javascript://something.com'},
            request=self.request,
            instance=self.addon,
        )
        assert not form.is_valid()
        assert form.errors['homepage'] == ['Enter a valid URL.']

    def test_ftp_homepage(self):
        form = forms.AdditionalDetailsForm(
            {'homepage': 'ftp://foo.com'}, request=self.request, instance=self.addon
        )
        assert not form.is_valid()
        assert form.errors['homepage'] == ['Enter a valid URL.']

    def test_homepage_is_not_required(self):
        form = forms.AdditionalDetailsForm(
            {'default_locale': 'en-US'}, request=self.request, instance=self.addon
        )
        assert form.is_valid()


class TestIconForm(TestCase):
    fixtures = ['base/addon_3615']

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.mkdtemp(dir=settings.TMP_PATH)
        self.addon = Addon.objects.get(pk=3615)

        class DummyRequest:
            FILES = None

        self.request = DummyRequest()
        self.icon_path = os.path.join(settings.TMP_PATH, 'icon')
        if not os.path.exists(self.icon_path):
            os.makedirs(self.icon_path)

    def tearDown(self):
        rm_local_tmp_dir(self.temp_dir)
        super().tearDown()

    def get_icon_paths(self):
        path = os.path.join(self.addon.get_icon_dir(), str(self.addon.id))
        return [f'{path}-{size}.png' for size in amo.ADDON_ICON_SIZES]

    @mock.patch('olympia.amo.models.ModelBase.update')
    def test_icon_modified(self, update_mock):
        name = 'transparent.png'
        form = forms.AddonFormMedia(
            {'icon_upload_hash': name}, request=self.request, instance=self.addon
        )

        dest = os.path.join(self.icon_path, name)
        with storage.open(dest, 'wb') as f:
            shutil.copyfileobj(open(get_image_path(name), 'rb'), f)
        assert form.is_valid()
        form.save(addon=self.addon)
        assert update_mock.called


class TestCategoryForm(TestCase):
    def test_no_possible_categories(self):
        addon = addon_factory(type=amo.ADDON_DICT)
        request = req_factory_factory('/')
        form = forms.CategoryFormSet(addon=addon, request=request)
        apps = [f.app for f in form.forms]
        assert apps == [amo.FIREFOX]
