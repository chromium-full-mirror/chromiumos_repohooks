#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unittests for pre-upload.py."""

from __future__ import print_function

import datetime
import os
import sys

import mock

import errors

# pylint: disable=W0212
# We access private members of the pre_upload module all over the place.

# Make sure we can find the chromite paths.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                '..', '..'))

# The sys.path monkey patching confuses the linter.
# pylint: disable=wrong-import-position
from chromite.lib import constants
from chromite.lib import cros_build_lib
from chromite.lib import cros_test_lib
from chromite.lib import git
from chromite.lib import osutils


pre_upload = __import__('pre-upload')


def ProjectNamed(project_name):
  """Wrapper to create a Project with just the name"""
  return pre_upload.Project(project_name, None, None)


class PreUploadTestCase(cros_test_lib.MockTestCase):
  """Common test case base."""

  def setUp(self):
    pre_upload.CACHE.clear()


class TryUTF8DecodeTest(PreUploadTestCase):
  """Verify we sanely handle unicode content."""

  def setUp(self):
    self.rc_mock = self.PatchObject(cros_build_lib, 'run')

  def _run(self, content):
    """Helper for round tripping through _run_command."""
    self.rc_mock.return_value = cros_build_lib.CommandResult(
        output=content, returncode=0)
    return pre_upload._run_command([])

  def testEmpty(self):
    """Check empty output."""
    ret = self._run(b'')
    self.assertEqual('', ret)

    if sys.version_info.major < 3:
      ret = self._run('')
      self.assertEqual(u'', ret)

  def testAscii(self):
    """Check ascii output."""
    ret = self._run(b'abc')
    self.assertEqual('abc', ret)

    if sys.version_info.major < 3:
      ret = self._run('abc')
      self.assertEqual(u'abc', ret)

  def testUtf8(self):
    """Check valid UTF-8 output."""
    text = u'ä½ å¥½å¸èæ©'
    ret = self._run(text.encode('utf-8'))
    self.assertEqual(text, ret)

  def testBinary(self):
    """Check binary (invalid UTF-8) output."""
    ret = self._run(b'hi \x80 there')
    self.assertEqual(u'hi \ufffd there', ret)


class CheckNoLongLinesTest(PreUploadTestCase):
  """Tests for _check_no_long_lines."""

  def setUp(self):
    self.diff_mock = self.PatchObject(pre_upload, '_get_file_diff')

  def testCheck(self):
    self.PatchObject(pre_upload, '_get_affected_files', return_value=['x.py'])
    self.diff_mock.return_value = [
        (1, u'x' * 80),                      # OK
        (2, '\x80' * 80),                    # OK
        (3, u'x' * 81),                      # Too long
        (4, '\x80' * 81),                    # Too long
        (5, u'See http://' + (u'x' * 80)),   # OK (URL)
        (6, u'See https://' + (u'x' * 80)),  # OK (URL)
        (7, u'#  define ' + (u'x' * 80)),    # OK (compiler directive)
        (8, u'#define' + (u'x' * 74)),       # Too long
    ]
    failure = pre_upload._check_no_long_lines(ProjectNamed('PROJECT'), 'COMMIT')
    self.assertTrue(failure)
    self.assertEqual('Found lines longer than the limit (first 5 shown):',
                     failure.msg)
    self.assertEqual(['x.py, line %d, 81 chars, over 80 chars' %
                      x for x in [3, 4, 8]],
                     failure.items)

  def testCheckTreatsOwnersFilesSpecially(self):
    affected_files = self.PatchObject(pre_upload, '_get_affected_files')

    mock_files = (
        ('foo-OWNERS', False),
        ('OWNERS', True),
        ('/path/to/OWNERS', True),
        ('/path/to/OWNERS.foo', True),
    )

    mock_lines = (
        (u'x' * 81, False),
        (u'foo file:' + u'x' * 80, True),
        (u'include ' + u'x' * 80, True),
    )
    assert all(len(line) > 80 for line, _ in mock_lines)

    for file_name, is_owners in mock_files:
      affected_files.return_value = [file_name]
      for line, is_ok in mock_lines:
        self.diff_mock.return_value = [(1, line)]
        failure = pre_upload._check_no_long_lines(ProjectNamed('PROJECT'),
                                                  'COMMIT')

        assert_msg = 'file: %r; line: %r' % (file_name, line)
        if is_owners and is_ok:
          self.assertFalse(failure, assert_msg)
        else:
          self.assertTrue(failure, assert_msg)
          self.assertIn('Found lines longer than the limit', failure.msg,
                        assert_msg)

  def testIncludeOptions(self):
    self.PatchObject(pre_upload,
                     '_get_affected_files',
                     return_value=['foo.txt'])
    self.diff_mock.return_value = [(1, u'x' * 81)]
    self.assertFalse(pre_upload._check_no_long_lines(
        ProjectNamed('PROJECT'), 'COMMIT'))
    self.assertTrue(pre_upload._check_no_long_lines(
        ProjectNamed('PROJECT'), 'COMMIT', options=['--include_regex=foo']))

  def testExcludeOptions(self):
    self.PatchObject(pre_upload,
                     '_get_affected_files',
                     return_value=['foo.py'])
    self.diff_mock.return_value = [(1, u'x' * 81)]
    self.assertTrue(pre_upload._check_no_long_lines(
        ProjectNamed('PROJECT'), 'COMMIT'))
    self.assertFalse(pre_upload._check_no_long_lines(
        ProjectNamed('PROJECT'), 'COMMIT', options=['--exclude_regex=foo']))

  def testSpecialLineLength(self):
    mock_lines = (
        (u'x' * 101, True),
        (u'x' * 100, False),
        (u'x' * 81, False),
        (u'x' * 80, False),
    )
    self.PatchObject(pre_upload,
                     '_get_affected_files',
                     return_value=['foo.java'])
    for line, is_ok in mock_lines:
      self.diff_mock.return_value = [(1, line)]
      if is_ok:
        self.assertTrue(pre_upload._check_no_long_lines(
            ProjectNamed('PROJECT'), 'COMMIT'))
      else:
        self.assertFalse(pre_upload._check_no_long_lines(
            ProjectNamed('PROJECT'), 'COMMIT'))


class CheckTabbedIndentsTest(PreUploadTestCase):
  """Tests for _check_tabbed_indents."""

  def setUp(self):
    self.PatchObject(pre_upload,
                     '_get_affected_files',
                     return_value=['x.ebuild'])
    self.diff_mock = self.PatchObject(pre_upload, '_get_file_diff')

  def test_good_cases(self):
    self.diff_mock.return_value = [
        (1, u'no_tabs_anywhere'),
        (2, u'	leading_tab_only'),
        (3, u'	leading_tab another_tab'),
        (4, u'	leading_tab trailing_too	'),
        (5, u'	leading_tab  then_spaces_trailing  '),
    ]
    failure = pre_upload._check_tabbed_indents(ProjectNamed('PROJECT'),
                                               'COMMIT')
    self.assertIsNone(failure)

  def test_bad_cases(self):
    self.diff_mock.return_value = [
        (1, u' leading_space'),
        (2, u'	 tab_followed_by_space'),
        (3, u'  			space_followed_by_tab'),
        (4, u'   			  mix_em_up'),
        (5, u'	  		  			mix_on_both_sides			   '),
    ]
    failure = pre_upload._check_tabbed_indents(ProjectNamed('PROJECT'),
                                               'COMMIT')
    self.assertTrue(failure)
    self.assertEqual('Found a space in indentation (must be all tabs):',
                     failure.msg)
    self.assertEqual(['x.ebuild, line %d' % x for x in range(1, 6)],
                     failure.items)


class CheckProjectPrefix(PreUploadTestCase, cros_test_lib.TempDirTestCase):
  """Tests for _check_project_prefix."""

  def setUp(self):
    self.orig_cwd = os.getcwd()
    os.chdir(self.tempdir)
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.desc_mock = self.PatchObject(pre_upload, '_get_commit_desc')

  def tearDown(self):
    os.chdir(self.orig_cwd)

  def _WriteAliasFile(self, filename, project):
    """Writes a project name to a file, creating directories if needed."""
    os.makedirs(os.path.dirname(filename))
    osutils.WriteFile(filename, project)

  def testInvalidPrefix(self):
    """Report an error when the prefix doesn't match the base directory."""
    self.file_mock.return_value = ['foo/foo.cc', 'foo/subdir/baz.cc']
    self.desc_mock.return_value = 'bar: Some commit'
    failure = pre_upload._check_project_prefix(ProjectNamed('PROJECT'),
                                               'COMMIT')
    self.assertTrue(failure)
    self.assertEqual('The commit title for changes affecting only foo should '
                     'start with "foo: "', failure.msg)

  def testValidPrefix(self):
    """Use a prefix that matches the base directory."""
    self.file_mock.return_value = ['foo/foo.cc', 'foo/subdir/baz.cc']
    self.desc_mock.return_value = 'foo: Change some files.'
    self.assertFalse(
        pre_upload._check_project_prefix(ProjectNamed('PROJECT'), 'COMMIT'))

  def testAliasFile(self):
    """Use .project_alias to override the project name."""
    self._WriteAliasFile('foo/.project_alias', 'project')
    self.file_mock.return_value = ['foo/foo.cc', 'foo/subdir/bar.cc']
    self.desc_mock.return_value = 'project: Use an alias.'
    self.assertFalse(
        pre_upload._check_project_prefix(ProjectNamed('PROJECT'), 'COMMIT'))

  def testAliasFileWithSubdirs(self):
    """Check that .project_alias is used when only modifying subdirectories."""
    self._WriteAliasFile('foo/.project_alias', 'project')
    self.file_mock.return_value = [
        'foo/subdir/foo.cc',
        'foo/subdir/bar.cc'
        'foo/subdir/blah/baz.cc'
    ]
    self.desc_mock.return_value = 'project: Alias with subdirs.'
    self.assertFalse(
        pre_upload._check_project_prefix(ProjectNamed('PROJECT'), 'COMMIT'))


class CheckFilePathCharTypeTest(PreUploadTestCase):
  """Tests for _check_filepath_chartype."""

  def setUp(self):
    self.diff_mock = self.PatchObject(pre_upload, '_get_file_diff')

  def testCheck(self):
    self.PatchObject(pre_upload, '_get_affected_files', return_value=['x.cc'])
    self.diff_mock.return_value = [
        (1, 'base::FilePath'),                   # OK
        (2, 'base::FilePath::CharType'),         # NG
        (3, 'base::FilePath::StringType'),       # NG
        (4, 'base::FilePath::StringPieceType'),  # NG
        (5, 'base::FilePath::FromUTF8Unsafe'),   # NG
        (6, 'FilePath::CharType'),               # NG
        (7, 'FilePath::StringType'),             # NG
        (8, 'FilePath::StringPieceType'),        # NG
        (9, 'FilePath::FromUTF8Unsafe'),         # NG
        (10, 'AsUTF8Unsafe'),                    # NG
        (11, 'FILE_PATH_LITERAL'),               # NG
    ]
    failure = pre_upload._check_filepath_chartype(ProjectNamed('PROJECT'),
                                                  'COMMIT')
    self.assertTrue(failure)
    self.assertEqual(
        'Please assume FilePath::CharType is char (crbug.com/870621):',
        failure.msg)
    self.assertEqual(['x.cc, line 2 has base::FilePath::CharType',
                      'x.cc, line 3 has base::FilePath::StringType',
                      'x.cc, line 4 has base::FilePath::StringPieceType',
                      'x.cc, line 5 has base::FilePath::FromUTF8Unsafe',
                      'x.cc, line 6 has FilePath::CharType',
                      'x.cc, line 7 has FilePath::StringType',
                      'x.cc, line 8 has FilePath::StringPieceType',
                      'x.cc, line 9 has FilePath::FromUTF8Unsafe',
                      'x.cc, line 10 has AsUTF8Unsafe',
                      'x.cc, line 11 has FILE_PATH_LITERAL'],
                     failure.items)


class CheckKernelConfig(PreUploadTestCase):
  """Tests for _kernel_configcheck."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')

  def testMixedChanges(self):
    """Mixing of changes should fail."""
    self.file_mock.return_value = [
        '/kernel/files/chromeos/config/base.config',
        '/kernel/files/arch/arm/mach-exynos/mach-exynos5-dt.c'
    ]
    failure = pre_upload._kernel_configcheck('PROJECT', 'COMMIT')
    self.assertTrue(failure)

  def testCodeOnly(self):
    """Code-only changes should pass."""
    self.file_mock.return_value = [
        '/kernel/files/Makefile',
        '/kernel/files/arch/arm/mach-exynos/mach-exynos5-dt.c'
    ]
    failure = pre_upload._kernel_configcheck('PROJECT', 'COMMIT')
    self.assertFalse(failure)

  def testConfigOnlyChanges(self):
    """Config-only changes should pass."""
    self.file_mock.return_value = [
        '/kernel/files/chromeos/config/base.config',
    ]
    failure = pre_upload._kernel_configcheck('PROJECT', 'COMMIT')
    self.assertFalse(failure)


class CheckJson(PreUploadTestCase):
  """Tests for _run_json_check."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testNoJson(self):
    """Nothing should be checked w/no JSON files."""
    self.file_mock.return_value = [
        '/foo/bar.txt',
        '/readme.md',
    ]
    ret = pre_upload._run_json_check('PROJECT', 'COMMIT')
    self.assertIsNone(ret)

  def testValidJson(self):
    """We should accept valid json files."""
    self.file_mock.return_value = [
        '/foo/bar.txt',
        '/data.json',
    ]
    self.content_mock.return_value = '{}'
    ret = pre_upload._run_json_check('PROJECT', 'COMMIT')
    self.assertIsNone(ret)
    self.content_mock.assert_called_once_with('/data.json', 'COMMIT')

  def testInvalidJson(self):
    """We should reject invalid json files."""
    self.file_mock.return_value = [
        '/foo/bar.txt',
        '/data.json',
    ]
    self.content_mock.return_value = '{'
    ret = pre_upload._run_json_check('PROJECT', 'COMMIT')
    self.assertIsNotNone(ret)
    self.content_mock.assert_called_once_with('/data.json', 'COMMIT')


class CheckManifests(PreUploadTestCase):
  """Tests _check_manifests."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testNoManifests(self):
    """Nothing should be checked w/no Manifest files."""
    self.file_mock.return_value = [
        '/foo/bar.txt',
        '/readme.md',
        '/manifest',
        '/Manifest.txt',
    ]
    ret = pre_upload._check_manifests('PROJECT', 'COMMIT')
    self.assertIsNone(ret)

  def testValidManifest(self):
    """Accept valid Manifest files."""
    self.file_mock.return_value = [
        '/foo/bar.txt',
        '/cat/pkg/Manifest',
    ]
    self.content_mock.return_value = """# Comment and blank lines.

DIST lines
"""
    ret = pre_upload._check_manifests('PROJECT', 'COMMIT')
    self.assertIsNone(ret)
    self.content_mock.assert_called_once_with('/cat/pkg/Manifest', 'COMMIT')

  def _testReject(self, content):
    """Make sure |content| is rejected."""
    self.file_mock.return_value = ('/Manifest',)
    self.content_mock.reset_mock()
    self.content_mock.return_value = content
    ret = pre_upload._check_manifests('PROJECT', 'COMMIT')
    self.assertIsNotNone(ret)
    self.content_mock.assert_called_once_with('/Manifest', 'COMMIT')

  def testRejectBlank(self):
    """Reject empty manifests."""
    self._testReject('')

  def testNoTrailingNewLine(self):
    """Reject manifests w/out trailing newline."""
    self._testReject('DIST foo')

  def testNoLeadingBlankLines(self):
    """Reject manifests w/leading blank lines."""
    self._testReject('\nDIST foo\n')

  def testNoTrailingBlankLines(self):
    """Reject manifests w/trailing blank lines."""
    self._testReject('DIST foo\n\n')

  def testNoLeadingWhitespace(self):
    """Reject manifests w/lines w/leading spaces."""
    self._testReject(' DIST foo\n')
    self._testReject('  # Comment\n')

  def testNoTrailingWhitespace(self):
    """Reject manifests w/lines w/trailing spaces."""
    self._testReject('DIST foo \n')
    self._testReject('# Comment \n')
    self._testReject('  \n')

  def testOnlyDistLines(self):
    """Only allow DIST lines in here."""
    self._testReject('EBUILD foo\n')


class CheckPortageMakeUseVar(PreUploadTestCase):
  """Tests for _check_portage_make_use_var."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testMakeConfOmitsOriginalUseValue(self):
    """Fail for make.conf that discards the previous value of $USE."""
    self.file_mock.return_value = ['make.conf']
    self.content_mock.return_value = u'USE="foo"\nUSE="${USE} bar"'
    failure = pre_upload._check_portage_make_use_var('PROJECT', 'COMMIT')
    self.assertTrue(failure, failure)

  def testMakeConfCorrectUsage(self):
    """Succeed for make.conf that preserves the previous value of $USE."""
    self.file_mock.return_value = ['make.conf']
    self.content_mock.return_value = u'USE="${USE} foo"\nUSE="${USE}" bar'
    failure = pre_upload._check_portage_make_use_var('PROJECT', 'COMMIT')
    self.assertFalse(failure, failure)

  def testMakeDefaultsReferencesOriginalUseValue(self):
    """Fail for make.defaults that refers to a not-yet-set $USE value."""
    self.file_mock.return_value = ['make.defaults']
    self.content_mock.return_value = u'USE="${USE} foo"'
    failure = pre_upload._check_portage_make_use_var('PROJECT', 'COMMIT')
    self.assertTrue(failure, failure)

    # Also check for "$USE" without curly brackets.
    self.content_mock.return_value = u'USE="$USE foo"'
    failure = pre_upload._check_portage_make_use_var('PROJECT', 'COMMIT')
    self.assertTrue(failure, failure)

  def testMakeDefaultsOverwritesUseValue(self):
    """Fail for make.defaults that discards its own $USE value."""
    self.file_mock.return_value = ['make.defaults']
    self.content_mock.return_value = u'USE="foo"\nUSE="bar"'
    failure = pre_upload._check_portage_make_use_var('PROJECT', 'COMMIT')
    self.assertTrue(failure, failure)

  def testMakeDefaultsCorrectUsage(self):
    """Succeed for make.defaults that sets and preserves $USE."""
    self.file_mock.return_value = ['make.defaults']
    self.content_mock.return_value = u'USE="foo"\nUSE="${USE}" bar'
    failure = pre_upload._check_portage_make_use_var('PROJECT', 'COMMIT')
    self.assertFalse(failure, failure)


class CheckEbuildEapi(PreUploadTestCase):
  """Tests for _check_ebuild_eapi."""

  PORTAGE_STABLE = ProjectNamed('chromiumos/overlays/portage-stable')

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')
    self.diff_mock = self.PatchObject(pre_upload, '_get_file_diff',
                                      side_effect=Exception())

  def testSkipUpstreamOverlays(self):
    """Skip ebuilds found in upstream overlays."""
    self.file_mock.side_effect = Exception()
    ret = pre_upload._check_ebuild_eapi(self.PORTAGE_STABLE, 'HEAD')
    self.assertIsNone(ret)

    # Make sure our condition above triggers.
    self.assertRaises(Exception, pre_upload._check_ebuild_eapi, 'o', 'HEAD')

  def testSkipNonEbuilds(self):
    """Skip non-ebuild files."""
    self.content_mock.side_effect = Exception()

    self.file_mock.return_value = ['some-file', 'ebuild/dir', 'an.ebuild~']
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)

    # Make sure our condition above triggers.
    self.file_mock.return_value.append('a/real.ebuild')
    self.assertRaises(Exception, pre_upload._check_ebuild_eapi,
                      ProjectNamed('o'), 'HEAD')

  def testSkipSymlink(self):
    """Skip files that are just symlinks."""
    self.file_mock.return_value = ['a-r1.ebuild']
    self.content_mock.return_value = u'a.ebuild'
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)

  def testRejectEapiImplicit0Content(self):
    """Reject ebuilds that do not declare EAPI (so it's 0)."""
    self.file_mock.return_value = ['a.ebuild']

    self.content_mock.return_value = u"""# Header
IUSE="foo"
src_compile() { }
"""
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testRejectExplicitEapi1Content(self):
    """Reject ebuilds that do declare old EAPI explicitly."""
    self.file_mock.return_value = ['a.ebuild']

    template = u"""# Header
EAPI=%s
IUSE="foo"
src_compile() { }
"""
    # Make sure we only check the first EAPI= setting.
    self.content_mock.return_value = template % '1\nEAPI=60'
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertTrue(isinstance(ret, errors.HookFailure))

    # Verify we handle double quotes too.
    self.content_mock.return_value = template % '"1"'
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertTrue(isinstance(ret, errors.HookFailure))

    # Verify we handle single quotes too.
    self.content_mock.return_value = template % "'1'"
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testAcceptExplicitNewEapiContent(self):
    """Accept ebuilds that do declare new EAPI explicitly."""
    self.file_mock.return_value = ['a.ebuild']

    template = u"""# Header
EAPI=%s
IUSE="foo"
src_compile() { }
"""
    # Make sure we only check the first EAPI= setting.
    self.content_mock.return_value = template % '6\nEAPI=1'
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)

    # Verify we handle double quotes too.
    self.content_mock.return_value = template % '"5"'
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)

    # Verify we handle single quotes too.
    self.content_mock.return_value = template % "'5-hdepend'"
    ret = pre_upload._check_ebuild_eapi(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)


class CheckEbuildKeywords(PreUploadTestCase):
  """Tests for _check_ebuild_keywords."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testNoEbuilds(self):
    """If no ebuilds are found, do not scan."""
    self.file_mock.return_value = ['a.file', 'ebuild-is-not.foo']

    ret = pre_upload._check_ebuild_keywords(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)

    self.assertEqual(self.content_mock.call_count, 0)

  def testSomeEbuilds(self):
    """If ebuilds are found, only scan them."""
    self.file_mock.return_value = ['a.file', 'blah', 'foo.ebuild', 'cow']
    self.content_mock.return_value = u''

    ret = pre_upload._check_ebuild_keywords(ProjectNamed('overlay'), 'HEAD')
    self.assertIsNone(ret)

    self.assertEqual(self.content_mock.call_count, 1)

  def _CheckContent(self, content, fails):
    """Test helper for inputs/outputs.

    Args:
      content: The ebuild content to test.
      fails: Whether |content| should trigger a hook failure.
    """
    self.file_mock.return_value = ['a.ebuild']
    self.content_mock.return_value = content

    ret = pre_upload._check_ebuild_keywords(ProjectNamed('overlay'), 'HEAD')
    if fails:
      self.assertTrue(isinstance(ret, errors.HookFailure))
    else:
      self.assertIsNone(ret)

    self.assertEqual(self.content_mock.call_count, 1)

  def testEmpty(self):
    """Check KEYWORDS= is accepted."""
    self._CheckContent(u'# HEADER\nKEYWORDS=\nblah\n', False)

  def testEmptyQuotes(self):
    """Check KEYWORDS="" is accepted."""
    self._CheckContent(u'# HEADER\nKEYWORDS="    "\nblah\n', False)

  def testStableGlob(self):
    """Check KEYWORDS=* is accepted."""
    self._CheckContent(u'# HEADER\nKEYWORDS="\t*\t"\nblah\n', False)

  def testUnstableGlob(self):
    """Check KEYWORDS=~* is accepted."""
    self._CheckContent(u'# HEADER\nKEYWORDS="~* "\nblah\n', False)

  def testRestrictedGlob(self):
    """Check KEYWORDS=-* is accepted."""
    self._CheckContent(u'# HEADER\nKEYWORDS="\t-* arm"\nblah\n', False)

  def testMissingGlobs(self):
    """Reject KEYWORDS missing any globs."""
    self._CheckContent(u'# HEADER\nKEYWORDS="~arm x86"\nblah\n', True)


class CheckEbuildVirtualPv(PreUploadTestCase):
  """Tests for _check_ebuild_virtual_pv."""

  PORTAGE_STABLE = ProjectNamed('chromiumos/overlays/portage-stable')
  CHROMIUMOS_OVERLAY = ProjectNamed('chromiumos/overlays/chromiumos')
  BOARD_OVERLAY = ProjectNamed('chromiumos/overlays/board-overlays')
  PRIVATE_OVERLAY = ProjectNamed('chromeos/overlays/overlay-link-private')
  PRIVATE_VARIANT_OVERLAY = ProjectNamed('chromeos/overlays/'
                                         'overlay-variant-daisy-spring-private')

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')

  def testNoVirtuals(self):
    """Skip non virtual packages."""
    self.file_mock.return_value = ['some/package/package-3.ebuild']
    ret = pre_upload._check_ebuild_virtual_pv(ProjectNamed('overlay'), 'H')
    self.assertIsNone(ret)

  def testCommonVirtuals(self):
    """Non-board overlays should use PV=1."""
    template = 'virtual/foo/foo-%s.ebuild'
    self.file_mock.return_value = [template % '1']
    ret = pre_upload._check_ebuild_virtual_pv(self.CHROMIUMOS_OVERLAY, 'H')
    self.assertIsNone(ret)

    self.file_mock.return_value = [template % '2']
    ret = pre_upload._check_ebuild_virtual_pv(self.CHROMIUMOS_OVERLAY, 'H')
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testPublicBoardVirtuals(self):
    """Public board overlays should use PV=2."""
    template = 'overlay-lumpy/virtual/foo/foo-%s.ebuild'
    self.file_mock.return_value = [template % '2']
    ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, 'H')
    self.assertIsNone(ret)

    self.file_mock.return_value = [template % '2.5']
    ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, 'H')
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testPublicBoardVariantVirtuals(self):
    """Public board variant overlays should use PV=2.5."""
    template = 'overlay-variant-lumpy-foo/virtual/foo/foo-%s.ebuild'
    self.file_mock.return_value = [template % '2.5']
    ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, 'H')
    self.assertIsNone(ret)

    self.file_mock.return_value = [template % '3']
    ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, 'H')
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testPrivateBoardVirtuals(self):
    """Private board overlays should use PV=3."""
    template = 'virtual/foo/foo-%s.ebuild'
    self.file_mock.return_value = [template % '3']
    ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_OVERLAY, 'H')
    self.assertIsNone(ret)

    self.file_mock.return_value = [template % '3.5']
    ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_OVERLAY, 'H')
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testPrivateBoardVariantVirtuals(self):
    """Private board variant overlays should use PV=3.5."""
    template = 'virtual/foo/foo-%s.ebuild'
    self.file_mock.return_value = [template % '3.5']
    ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_VARIANT_OVERLAY, 'H')
    self.assertIsNone(ret)

  def testSpecialVirtuals(self):
    """Some cases require deeper versioning and can be >= 4."""
    template = 'virtual/foo/foo-%s.ebuild'
    self.file_mock.return_value = [template % '4']
    ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_VARIANT_OVERLAY, 'H')
    self.assertIsNone(ret)

    self.file_mock.return_value = [template % '4.5']
    ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_VARIANT_OVERLAY, 'H')
    self.assertIsNone(ret)

class CheckCrosLicenseCopyrightHeader(PreUploadTestCase):
  """Tests for _check_cros_license."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testOldHeaders(self):
    """Accept old header styles."""
    HEADERS = (
        (u'#!/bin/sh\n'
         u'# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.\n'
         u'# Use of this source code is governed by a BSD-style license that'
         u' can be\n'
         u'# found in the LICENSE file.\n'),
        (u'// Copyright 2010-2013 The Chromium OS Authors. All rights reserved.'
         u'\n// Use of this source code is governed by a BSD-style license that'
         u' can be\n'
         u'// found in the LICENSE file.\n'),
    )
    self.file_mock.return_value = ['file']
    for header in HEADERS:
      self.content_mock.return_value = header
      self.assertFalse(pre_upload._check_cros_license('proj', 'sha1'))

  def testNewFileYear(self):
    """Added files should have the current year in license header."""
    year = datetime.datetime.now().year
    HEADERS = (
        (u'// Copyright 2016 The Chromium OS Authors. All rights reserved.\n'
         u'// Use of this source code is governed by a BSD-style license that'
         u' can be\n'
         u'// found in the LICENSE file.\n'),
        (u'// Copyright %d The Chromium OS Authors. All rights reserved.\n'
         u'// Use of this source code is governed by a BSD-style license that'
         u' can be\n'
         u'// found in the LICENSE file.\n') % year,
    )
    want_error = (True, False)
    def fake_get_affected_files(_, relative, include_adds=True):
      _ = relative
      if include_adds:
        return ['file']
      else:
        return []

    self.file_mock.side_effect = fake_get_affected_files
    for i, header in enumerate(HEADERS):
      self.content_mock.return_value = header
      if want_error[i]:
        self.assertTrue(pre_upload._check_cros_license('proj', 'sha1'))
      else:
        self.assertFalse(pre_upload._check_cros_license('proj', 'sha1'))

  def testRejectC(self):
    """Reject the (c) in newer headers."""
    HEADERS = (
        (u'// Copyright (c) 2015 The Chromium OS Authors. All rights reserved.'
         u'\n'
         u'// Use of this source code is governed by a BSD-style license that'
         u' can be\n'
         u'// found in the LICENSE file.\n'),
        (u'// Copyright (c) 2020 The Chromium OS Authors. All rights reserved.'
         u'\n'
         u'// Use of this source code is governed by a BSD-style license that'
         u' can be\n'
         u'// found in the LICENSE file.\n'),
    )
    self.file_mock.return_value = ['file']
    for header in HEADERS:
      self.content_mock.return_value = header
      self.assertTrue(pre_upload._check_cros_license('proj', 'sha1'))

  def testNoLeadingSpace(self):
    """Allow headers without leading space (e.g., not a source comment)"""
    HEADERS = (
        (u'Copyright 2018 The Chromium OS Authors. All rights reserved.\n'
         u'Use of this source code is governed by a BSD-style license that '
         u'can be\n'
         u'found in the LICENSE file.\n'),
    )
    self.file_mock.return_value = ['file']
    for header in HEADERS:
      self.content_mock.return_value = header
      self.assertFalse(pre_upload._check_cros_license('proj', 'sha1'))

  def testNoExcludedGolang(self):
    """Don't exclude .go files for license checks."""
    self.file_mock.return_value = ['foo/main.go']
    self.content_mock.return_value = u'package main\nfunc main() {}'
    self.assertTrue(pre_upload._check_cros_license('proj', 'sha1'))

  def testIgnoreExcludedPaths(self):
    """Ignores excluded paths for license checks."""
    self.file_mock.return_value = ['foo/OWNERS']
    self.content_mock.return_value = u'owner@chromium.org'
    self.assertFalse(pre_upload._check_cros_license('proj', 'sha1'))

  def testIgnoreTopLevelExcludedPaths(self):
    """Ignores excluded paths for license checks."""
    self.file_mock.return_value = ['OWNERS']
    self.content_mock.return_value = u'owner@chromium.org'
    self.assertFalse(pre_upload._check_cros_license('proj', 'sha1'))

class CheckAOSPLicenseCopyrightHeader(PreUploadTestCase):
  """Tests for _check_aosp_license."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testHeaders(self):
    """Accept old header styles."""
    HEADERS = (
        u"""//
// Copyright (C) 2011 The Android Open Source Project
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//
""",
        u"""#
# Copyright (c) 2015 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
""",
    )
    self.file_mock.return_value = ['file']
    for header in HEADERS:
      self.content_mock.return_value = header
      self.assertIsNone(pre_upload._check_aosp_license('proj', 'sha1'))

  def testRejectNoLinesAround(self):
    """Reject headers missing the empty lines before/after the license."""
    HEADERS = (
        u"""# Copyright (c) 2015 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""",
    )
    self.file_mock.return_value = ['file']
    for header in HEADERS:
      self.content_mock.return_value = header
      self.assertIsNotNone(pre_upload._check_aosp_license('proj', 'sha1'))

  def testIgnoreExcludedPaths(self):
    """Ignores excluded paths for license checks."""
    self.file_mock.return_value = ['foo/OWNERS']
    self.content_mock.return_value = u'owner@chromium.org'
    self.assertIsNone(pre_upload._check_aosp_license('proj', 'sha1'))

  def testIgnoreTopLevelExcludedPaths(self):
    """Ignores excluded paths for license checks."""
    self.file_mock.return_value = ['OWNERS']
    self.content_mock.return_value = u'owner@chromium.org'
    self.assertIsNone(pre_upload._check_aosp_license('proj', 'sha1'))


class CheckLayoutConfTestCase(PreUploadTestCase):
  """Tests for _check_layout_conf."""

  def setUp(self):
    self.file_mock = self.PatchObject(pre_upload, '_get_affected_files')
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def assertAccepted(self, files, project='project', commit='fake sha1'):
    """Assert _check_layout_conf accepts |files|."""
    self.file_mock.return_value = files
    ret = pre_upload._check_layout_conf(project, commit)
    self.assertIsNone(ret, msg='rejected with:\n%s' % ret)

  def assertRejected(self, files, project='project', commit='fake sha1'):
    """Assert _check_layout_conf rejects |files|."""
    self.file_mock.return_value = files
    ret = pre_upload._check_layout_conf(project, commit)
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def GetLayoutConf(self, filters=()):
    """Return a valid layout.conf with |filters| lines removed."""
    all_lines = [
        u'masters = portage-stable chromiumos',
        u'profile-formats = portage-2 profile-default-eapi',
        u'profile_eapi_when_unspecified = 5-progress',
        u'repo-name = link',
        u'thin-manifests = true',
        u'use-manifests = strict',
    ]

    lines = []
    for line in all_lines:
      for filt in filters:
        if line.startswith(filt):
          break
      else:
        lines.append(line)

    return u'\n'.join(lines)

  def testNoFilesToCheck(self):
    """Don't blow up when there are no layout.conf files."""
    self.assertAccepted([])

  def testRejectRepoNameFile(self):
    """If profiles/repo_name is set, kick it out."""
    self.assertRejected(['profiles/repo_name'])

  def testAcceptValidLayoutConf(self):
    """Accept a fully valid layout.conf."""
    self.content_mock.return_value = self.GetLayoutConf()
    self.assertAccepted(['metadata/layout.conf'])

  def testAcceptUnknownKeys(self):
    """Accept keys we don't explicitly know about."""
    self.content_mock.return_value = self.GetLayoutConf() + u'\nzzz-top = ok'
    self.assertAccepted(['metadata/layout.conf'])

  def testRejectUnsorted(self):
    """Reject an unsorted layout.conf."""
    self.content_mock.return_value = u'zzz-top = bad\n' + self.GetLayoutConf()
    self.assertRejected(['metadata/layout.conf'])

  def testRejectMissingThinManifests(self):
    """Reject a layout.conf missing thin-manifests."""
    self.content_mock.return_value = self.GetLayoutConf(
        filters=[u'thin-manifests'])
    self.assertRejected(['metadata/layout.conf'])

  def testRejectMissingUseManifests(self):
    """Reject a layout.conf missing use-manifests."""
    self.content_mock.return_value = self.GetLayoutConf(
        filters=[u'use-manifests'])
    self.assertRejected(['metadata/layout.conf'])

  def testRejectMissingEapiFallback(self):
    """Reject a layout.conf missing profile_eapi_when_unspecified."""
    self.content_mock.return_value = self.GetLayoutConf(
        filters=[u'profile_eapi_when_unspecified'])
    self.assertRejected(['metadata/layout.conf'])

  def testRejectMissingRepoName(self):
    """Reject a layout.conf missing repo-name."""
    self.content_mock.return_value = self.GetLayoutConf(filters=[u'repo-name'])
    self.assertRejected(['metadata/layout.conf'])


class CommitMessageTestCase(PreUploadTestCase):
  """Test case for funcs that check commit messages."""

  def setUp(self):
    self.msg_mock = self.PatchObject(pre_upload, '_get_commit_desc')

  @staticmethod
  def CheckMessage(_project, _commit):
    raise AssertionError('Test class must declare CheckMessage')
    # This dummy return is to silence pylint warning W1111 so we don't have to
    # enable it for all the call sites below.
    return 1 # pylint: disable=W0101

  def assertMessageAccepted(self, msg, project=ProjectNamed('project'),
                            commit='1234'):
    """Assert _check_change_has_bug_field accepts |msg|."""
    self.msg_mock.return_value = msg
    ret = self.CheckMessage(project, commit)
    self.assertIsNone(ret)

  def assertMessageRejected(self, msg, project=ProjectNamed('project'),
                            commit='1234'):
    """Assert _check_change_has_bug_field rejects |msg|."""
    self.msg_mock.return_value = msg
    ret = self.CheckMessage(project, commit)
    self.assertTrue(isinstance(ret, errors.HookFailure))


class CheckCommitMessageBug(CommitMessageTestCase):
  """Tests for _check_change_has_bug_field."""

  AOSP_PROJECT = pre_upload.Project(name='overlay', dir='', remote='aosp')
  CROS_PROJECT = pre_upload.Project(name='overlay', dir='', remote='cros')

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_change_has_bug_field(project, commit)

  def testNormal(self):
    """Accept a commit message w/a valid BUG."""
    self.assertMessageAccepted('\nBUG=chromium:1234\n', self.CROS_PROJECT)
    self.assertMessageAccepted('\nBUG=b:1234\n', self.CROS_PROJECT)

    self.assertMessageAccepted('\nBug: 1234\n', self.AOSP_PROJECT)
    self.assertMessageAccepted('\nBug:1234\n', self.AOSP_PROJECT)

  def testNone(self):
    """Accept BUG=None."""
    self.assertMessageAccepted('\nBUG=None\n', self.CROS_PROJECT)
    self.assertMessageAccepted('\nBUG=none\n', self.CROS_PROJECT)
    self.assertMessageAccepted('\nBug: None\n', self.AOSP_PROJECT)
    self.assertMessageAccepted('\nBug:none\n', self.AOSP_PROJECT)

    for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
      self.assertMessageRejected('\nBUG=NONE\n', project)

  def testBlank(self):
    """Reject blank values."""
    for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
      self.assertMessageRejected('\nBUG=\n', project)
      self.assertMessageRejected('\nBUG=    \n', project)
      self.assertMessageRejected('\nBug:\n', project)
      self.assertMessageRejected('\nBug:    \n', project)

  def testNotFirstLine(self):
    """Reject the first line."""
    for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
      self.assertMessageRejected('BUG=None\n\n\n', project)

  def testNotInline(self):
    """Reject not at the start of line."""
    for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
      self.assertMessageRejected('\n BUG=None\n', project)
      self.assertMessageRejected('\n\tBUG=None\n', project)

  def testOldTrackers(self):
    """Reject commit messages using old trackers."""
    self.assertMessageRejected('\nBUG=chrome-os-partner:1234\n',
                               self.CROS_PROJECT)
    self.assertMessageRejected('\nBUG=chromium-os:1234\n', self.CROS_PROJECT)

  def testNoTrackers(self):
    """Reject commit messages w/invalid trackers."""
    self.assertMessageRejected('\nBUG=booga:1234\n', self.CROS_PROJECT)
    self.assertMessageRejected('\nBUG=br:1234\n', self.CROS_PROJECT)

  def testMissing(self):
    """Reject commit messages w/no BUG line."""
    self.assertMessageRejected('foo\n')

  def testCase(self):
    """Reject bug lines that are not BUG."""
    self.assertMessageRejected('\nbug=none\n')


class CheckCommitMessageCqDepend(CommitMessageTestCase):
  """Tests for _check_change_has_valid_cq_depend."""

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_change_has_valid_cq_depend(project, commit)

  def testNormal(self):
    """Accept valid Cq-Depends line."""
    self.assertMessageAccepted('\nCq-Depend: chromium:1234\nChange-Id: I123')

  def testInvalid(self):
    """Reject invalid Cq-Depends line."""
    self.assertMessageRejected('\nCq-Depend=chromium=1234\n')
    self.assertMessageRejected('\nCq-Depend: None\n')
    self.assertMessageRejected('\nCq-Depend: chromium:1234\n\nChange-Id: I123')
    self.assertMessageRejected('\nCQ-DEPEND=1234\n')


class CheckCommitMessageContribution(CommitMessageTestCase):
  """Tests for _check_change_is_contribution."""

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_change_is_contribution(project, commit)

  def testNormal(self):
    """Accept a commit message which is a contribution."""
    self.assertMessageAccepted('\nThis is cool code I am contributing.\n')

  def testFailureLowerCase(self):
    """Deny a commit message which is not a contribution."""
    self.assertMessageRejected('\nThis is not a contribution.\n')

  def testFailureUpperCase(self):
    """Deny a commit message which is not a contribution."""
    self.assertMessageRejected('\nNOT A CONTRIBUTION\n')


class CheckCommitMessageTest(CommitMessageTestCase):
  """Tests for _check_change_has_test_field."""

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_change_has_test_field(project, commit)

  def testNormal(self):
    """Accept a commit message w/a valid TEST."""
    self.assertMessageAccepted('\nTEST=i did it\n')

  def testNone(self):
    """Accept TEST=None."""
    self.assertMessageAccepted('\nTEST=None\n')
    self.assertMessageAccepted('\nTEST=none\n')

  def testBlank(self):
    """Reject blank values."""
    self.assertMessageRejected('\nTEST=\n')
    self.assertMessageRejected('\nTEST=     \n')

  def testNotFirstLine(self):
    """Reject the first line."""
    self.assertMessageRejected('TEST=None\n\n\n')

  def testNotInline(self):
    """Reject not at the start of line."""
    self.assertMessageRejected('\n TEST=None\n')
    self.assertMessageRejected('\n\tTEST=None\n')

  def testMissing(self):
    """Reject commit messages w/no TEST line."""
    self.assertMessageRejected('foo\n')

  def testCase(self):
    """Reject bug lines that are not TEST."""
    self.assertMessageRejected('\ntest=none\n')


class CheckCommitMessageChangeId(CommitMessageTestCase):
  """Tests for _check_change_has_proper_changeid."""

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_change_has_proper_changeid(project, commit)

  def testNormal(self):
    """Accept a commit message w/a valid Change-Id."""
    self.assertMessageAccepted('foo\n\nChange-Id: I1234\n')

  def testBlank(self):
    """Reject blank values."""
    self.assertMessageRejected('\nChange-Id:\n')
    self.assertMessageRejected('\nChange-Id:       \n')

  def testNotFirstLine(self):
    """Reject the first line."""
    self.assertMessageRejected('TEST=None\n\n\n')

  def testNotInline(self):
    """Reject not at the start of line."""
    self.assertMessageRejected('\n Change-Id: I1234\n')
    self.assertMessageRejected('\n\tChange-Id: I1234\n')

  def testMissing(self):
    """Reject commit messages missing the line."""
    self.assertMessageRejected('foo\n')

  def testCase(self):
    """Reject bug lines that are not Change-Id."""
    self.assertMessageRejected('\nchange-id: I1234\n')
    self.assertMessageRejected('\nChange-id: I1234\n')
    self.assertMessageRejected('\nChange-ID: I1234\n')

  def testEnd(self):
    """Reject Change-Id's that are not last."""
    self.assertMessageRejected('\nChange-Id: I1234\nbar\n')

  def testSobTag(self):
    """Permit s-o-b tags to follow the Change-Id."""
    self.assertMessageAccepted('foo\n\nChange-Id: I1234\nSigned-off-by: Hi\n')


class CheckCommitMessageNoOEM(CommitMessageTestCase):
  """Tests for _check_change_no_include_oem."""

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_change_no_include_oem(project, commit)

  def testNormal(self):
    """Accept a commit message w/o reference to an OEM/ODM."""
    self.assertMessageAccepted('foo\n')

  def testHasOEM(self):
    """Catch commit messages referencing OEMs."""
    self.assertMessageRejected('HP Project\n\n')
    self.assertMessageRejected('hewlett-packard\n')
    self.assertMessageRejected('Hewlett\nPackard\n')
    self.assertMessageRejected('Dell Chromebook\n\n\n')
    self.assertMessageRejected('product@acer.com\n')
    self.assertMessageRejected('This is related to Asus\n')
    self.assertMessageRejected('lenovo machine\n')

  def testHasODM(self):
    """Catch commit messages referencing ODMs."""
    self.assertMessageRejected('new samsung laptop\n\n')
    self.assertMessageRejected('pegatron(ems) project\n')
    self.assertMessageRejected('new Wistron device\n')

  def testContainsOEM(self):
    """Check that the check handles word boundaries properly."""
    self.assertMessageAccepted('oheahpohea')
    self.assertMessageAccepted('Play an Asus7 guitar chord.\n\n')

  def testTag(self):
    """Check that the check ignores tags."""
    self.assertMessageAccepted(
        'Harmless project\n'
        'Reviewed-by: partner@asus.corp-partner.google.com\n'
        'Tested-by: partner@hp.corp-partner.google.com\n'
        'Signed-off-by: partner@dell.corp-partner.google.com\n'
        'Commit-Queue: partner@lenovo.corp-partner.google.com\n'
        'CC: partner@acer.corp-partner.google.com\n'
        'Acked-by: partner@hewlett-packard.corp-partner.google.com\n')
    self.assertMessageRejected(
        'Asus project\n'
        'Reviewed-by: partner@asus.corp-partner.google.com')
    self.assertMessageRejected(
        'my project\n'
        'Bad-tag: partner@asus.corp-partner.google.com')


class CheckCommitMessageStyle(CommitMessageTestCase):
  """Tests for _check_commit_message_style."""

  @staticmethod
  def CheckMessage(project, commit):
    return pre_upload._check_commit_message_style(project, commit)

  def testNormal(self):
    """Accept valid commit messages."""
    self.assertMessageAccepted('one sentence.\n')
    self.assertMessageAccepted('some.module: do it!\n')
    self.assertMessageAccepted('one line\n\nmore stuff here.')

  def testNoBlankSecondLine(self):
    """Reject messages that have stuff on the second line."""
    self.assertMessageRejected('one sentence.\nbad fish!\n')

  def testFirstLineMultipleSentences(self):
    """Reject messages that have more than one sentence in the summary."""
    self.assertMessageRejected('one sentence. two sentence!\n')

  def testFirstLineTooLone(self):
    """Reject first lines that are too long."""
    self.assertMessageRejected('o' * 200)


def DiffEntry(src_file=None, dst_file=None, src_mode=None, dst_mode='100644',
              status='M'):
  """Helper to create a stub RawDiffEntry object"""
  if src_mode is None:
    if status == 'A':
      src_mode = '000000'
    elif status == 'M':
      src_mode = dst_mode
    elif status == 'D':
      src_mode = dst_mode
      dst_mode = '000000'

  src_sha = dst_sha = 'abc'
  if status == 'D':
    dst_sha = '000000'
  elif status == 'A':
    src_sha = '000000'

  return git.RawDiffEntry(src_mode=src_mode, dst_mode=dst_mode, src_sha=src_sha,
                          dst_sha=dst_sha, status=status, score=None,
                          src_file=src_file, dst_file=dst_file)


class HelpersTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
  """Various tests for utility functions."""

  def setUp(self):
    self.orig_cwd = os.getcwd()
    os.chdir(self.tempdir)

    self.PatchObject(git, 'RawDiff', return_value=[
        # A modified normal file.
        DiffEntry(src_file='buildbot/constants.py', status='M'),
        # A new symlink file.
        DiffEntry(dst_file='scripts/cros_env_whitelist', dst_mode='120000',
                  status='A'),
        # A deleted file.
        DiffEntry(src_file='scripts/sync_sonic.py', status='D'),
    ])

  def tearDown(self):
    os.chdir(self.orig_cwd)

  def _WritePresubmitIgnoreFile(self, subdir, data):
    """Writes to a .presubmitignore file in the passed-in subdirectory."""
    directory = os.path.join(self.tempdir, subdir)
    if not os.path.exists(directory):
      os.makedirs(directory)
    osutils.WriteFile(os.path.join(directory, pre_upload._IGNORE_FILE), data)

  def testGetAffectedFilesNoDeletesNoRelative(self):
    """Verify _get_affected_files() works w/no delete & not relative."""
    path = os.getcwd()
    files = pre_upload._get_affected_files('HEAD', include_deletes=False,
                                           relative=False)
    exp_files = [os.path.join(path, 'buildbot/constants.py')]
    self.assertEqual(files, exp_files)

  def testGetAffectedFilesDeletesNoRelative(self):
    """Verify _get_affected_files() works w/delete & not relative."""
    path = os.getcwd()
    files = pre_upload._get_affected_files('HEAD', include_deletes=True,
                                           relative=False)
    exp_files = [os.path.join(path, 'buildbot/constants.py'),
                 os.path.join(path, 'scripts/sync_sonic.py')]
    self.assertEqual(files, exp_files)

  def testGetAffectedFilesNoDeletesRelative(self):
    """Verify _get_affected_files() works w/no delete & relative."""
    files = pre_upload._get_affected_files('HEAD', include_deletes=False,
                                           relative=True)
    exp_files = ['buildbot/constants.py']
    self.assertEqual(files, exp_files)

  def testGetAffectedFilesDeletesRelative(self):
    """Verify _get_affected_files() works w/delete & relative."""
    files = pre_upload._get_affected_files('HEAD', include_deletes=True,
                                           relative=True)
    exp_files = ['buildbot/constants.py', 'scripts/sync_sonic.py']
    self.assertEqual(files, exp_files)

  def testGetAffectedFilesDetails(self):
    """Verify _get_affected_files() works w/full_details."""
    files = pre_upload._get_affected_files('HEAD', full_details=True,
                                           relative=True)
    self.assertEqual(files[0].src_file, 'buildbot/constants.py')

  def testGetAffectedFilesPresubmitIgnoreDirectory(self):
    """Verify .presubmitignore can be used to exclude a directory."""
    self._WritePresubmitIgnoreFile('.', 'buildbot/')
    self.assertEqual(pre_upload._get_affected_files('HEAD', relative=True), [])

  def testGetAffectedFilesPresubmitIgnoreDirectoryWildcard(self):
    """Verify .presubmitignore can be used with a directory wildcard."""
    self._WritePresubmitIgnoreFile('.', '*/constants.py')
    self.assertEqual(pre_upload._get_affected_files('HEAD', relative=True), [])

  def testGetAffectedFilesPresubmitIgnoreWithinDirectory(self):
    """Verify .presubmitignore can be placed in a subdirectory."""
    self._WritePresubmitIgnoreFile('buildbot', '*.py')
    self.assertEqual(pre_upload._get_affected_files('HEAD', relative=True), [])

  def testGetAffectedFilesPresubmitIgnoreDoesntMatch(self):
    """Verify .presubmitignore has no effect when it doesn't match a file."""
    self._WritePresubmitIgnoreFile('buildbot', '*.txt')
    self.assertEqual(pre_upload._get_affected_files('HEAD', relative=True),
                     ['buildbot/constants.py'])

  def testGetAffectedFilesPresubmitIgnoreAddedFile(self):
    """Verify .presubmitignore matches added files."""
    self._WritePresubmitIgnoreFile('.', 'buildbot/\nscripts/')
    self.assertEqual(pre_upload._get_affected_files('HEAD', relative=True,
                                                    include_symlinks=True),
                     [])

  def testGetAffectedFilesPresubmitIgnoreSkipIgnoreFile(self):
    """Verify .presubmitignore files are automatically skipped."""
    self.PatchObject(git, 'RawDiff', return_value=[
        DiffEntry(src_file='.presubmitignore', status='M')
    ])
    self.assertEqual(pre_upload._get_affected_files('HEAD', relative=True), [])


class ExecFilesTest(PreUploadTestCase):
  """Tests for _check_exec_files."""

  def setUp(self):
    self.diff_mock = self.PatchObject(git, 'RawDiff')

  def testNotExec(self):
    """Do not flag files that are not executable."""
    self.diff_mock.return_value = [
        DiffEntry(src_file='make.conf', dst_mode='100644', status='A'),
    ]
    self.assertIsNone(pre_upload._check_exec_files('proj', 'commit'))

  def testExec(self):
    """Flag files that are executable."""
    self.diff_mock.return_value = [
        DiffEntry(src_file='make.conf', dst_mode='100755', status='A'),
    ]
    self.assertIsNotNone(pre_upload._check_exec_files('proj', 'commit'))

  def testDeletedExec(self):
    """Ignore bad files that are being deleted."""
    self.diff_mock.return_value = [
        DiffEntry(src_file='make.conf', dst_mode='100755', status='D'),
    ]
    self.assertIsNone(pre_upload._check_exec_files('proj', 'commit'))

  def testModifiedExec(self):
    """Flag bad files that weren't exec, but now are."""
    self.diff_mock.return_value = [
        DiffEntry(src_file='make.conf', src_mode='100644', dst_mode='100755',
                  status='M'),
    ]
    self.assertIsNotNone(pre_upload._check_exec_files('proj', 'commit'))

  def testNormalExec(self):
    """Don't flag normal files (e.g. scripts) that are executable."""
    self.diff_mock.return_value = [
        DiffEntry(src_file='foo.sh', dst_mode='100755', status='A'),
    ]
    self.assertIsNone(pre_upload._check_exec_files('proj', 'commit'))


class CheckForUprev(PreUploadTestCase, cros_test_lib.TempDirTestCase):
  """Tests for _check_for_uprev."""

  def setUp(self):
    self.file_mock = self.PatchObject(git, 'RawDiff')

  def _Files(self, files):
    """Create |files| in the tempdir and return full paths to them."""
    for obj in files:
      if obj.status == 'D':
        continue
      if obj.dst_file is None:
        f = obj.src_file
      else:
        f = obj.dst_file
      osutils.Touch(os.path.join(self.tempdir, f), makedirs=True)
    return files

  def assertAccepted(self, files, project='project', commit='fake sha1'):
    """Assert _check_for_uprev accepts |files|."""
    self.file_mock.return_value = self._Files(files)
    ret = pre_upload._check_for_uprev(ProjectNamed(project), commit,
                                      project_top=self.tempdir)
    self.assertIsNone(ret)

  def assertRejected(self, files, project='project', commit='fake sha1'):
    """Assert _check_for_uprev rejects |files|."""
    self.file_mock.return_value = self._Files(files)
    ret = pre_upload._check_for_uprev(ProjectNamed(project), commit,
                                      project_top=self.tempdir)
    self.assertTrue(isinstance(ret, errors.HookFailure))

  def testWhitelistOverlay(self):
    """Skip checks on whitelisted overlays."""
    self.assertAccepted([DiffEntry(src_file='cat/pkg/pkg-0.ebuild')],
                        project='chromiumos/overlays/portage-stable')

  def testWhitelistFiles(self):
    """Skip checks on whitelisted files."""
    files = ['ChangeLog', 'Manifest', 'metadata.xml']
    self.assertAccepted([DiffEntry(src_file=os.path.join('c', 'p', x),
                                   status='M')
                         for x in files])

  def testRejectBasic(self):
    """Reject ebuilds missing uprevs."""
    self.assertRejected([DiffEntry(src_file='c/p/p-0.ebuild', status='M')])

  def testNewPackage(self):
    """Accept new ebuilds w/out uprevs."""
    self.assertAccepted([DiffEntry(src_file='c/p/p-0.ebuild', status='A')])
    self.assertAccepted([DiffEntry(src_file='c/p/p-0-r12.ebuild', status='A')])

  def testModifiedFilesOnly(self):
    """Reject ebuilds w/out uprevs and changes in files/."""
    osutils.Touch(os.path.join(self.tempdir, 'cat/pkg/pkg-0.ebuild'),
                  makedirs=True)
    self.assertRejected([DiffEntry(src_file='cat/pkg/files/f', status='A')])
    self.assertRejected([DiffEntry(src_file='cat/pkg/files/g', status='M')])

  def testFilesNoEbuilds(self):
    """Ignore changes to paths w/out ebuilds."""
    self.assertAccepted([DiffEntry(src_file='cat/pkg/files/f', status='A')])
    self.assertAccepted([DiffEntry(src_file='cat/pkg/files/g', status='M')])

  def testModifiedFilesWithUprev(self):
    """Accept ebuilds w/uprevs and changes in files/."""
    self.assertAccepted([DiffEntry(src_file='c/p/files/f', status='A'),
                         DiffEntry(src_file='c/p/p-0.ebuild', status='A')])
    self.assertAccepted([
        DiffEntry(src_file='c/p/files/f', status='M'),
        DiffEntry(src_file='c/p/p-0-r1.ebuild', src_mode='120000',
                  dst_file='c/p/p-0-r2.ebuild', dst_mode='120000', status='R')])

  def testModifiedFilesWith9999(self):
    """Accept 9999 ebuilds and changes in files/."""
    self.assertAccepted([DiffEntry(src_file='c/p/files/f', status='M'),
                         DiffEntry(src_file='c/p/p-9999.ebuild', status='M')])

  def testModifiedFilesIn9999SubDirWithout9999Change(self):
    """Accept changes in files/ with a parent 9999 ebuild"""
    ebuild_9999_file = os.path.join(self.tempdir, 'c/p/p-9999.ebuild')
    os.makedirs(os.path.dirname(ebuild_9999_file))
    osutils.WriteFile(ebuild_9999_file, 'fake')
    self.assertAccepted([DiffEntry(src_file='c/p/files/f', status='M')])


class DirectMainTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
  """Tests for direct_main()"""

  def setUp(self):
    self.hooks_mock = self.PatchObject(pre_upload, '_run_project_hooks',
                                       return_value=None)

  def testNoArgs(self):
    """If run w/no args, should check the current dir."""
    ret = pre_upload.direct_main([])
    self.assertEqual(ret, 0)
    self.hooks_mock.assert_called_once_with(
        mock.ANY, proj_dir=os.getcwd(), commit_list=[], presubmit=mock.ANY)

  def testExplicitDir(self):
    """Verify we can run on a diff dir."""
    # Use the chromite dir since we know it exists.
    ret = pre_upload.direct_main(['--dir', constants.CHROMITE_DIR])
    self.assertEqual(ret, 0)
    self.hooks_mock.assert_called_once_with(
        mock.ANY, proj_dir=constants.CHROMITE_DIR, commit_list=[],
        presubmit=mock.ANY)

  def testBogusProject(self):
    """A bogus project name should be fine (use default settings)."""
    # Use the chromite dir since we know it exists.
    ret = pre_upload.direct_main(['--dir', constants.CHROMITE_DIR,
                                  '--project', 'foooooooooo'])
    self.assertEqual(ret, 0)
    self.hooks_mock.assert_called_once_with(
        'foooooooooo', proj_dir=constants.CHROMITE_DIR, commit_list=[],
        presubmit=mock.ANY)

  def testBogustProjectNoDir(self):
    """Make sure --dir is detected even with --project."""
    ret = pre_upload.direct_main(['--project', 'foooooooooo'])
    self.assertEqual(ret, 0)
    self.hooks_mock.assert_called_once_with(
        'foooooooooo', proj_dir=os.getcwd(), commit_list=[],
        presubmit=mock.ANY)

  def testNoGitDir(self):
    """We should die when run on a non-git dir."""
    self.assertRaises(pre_upload.BadInvocation, pre_upload.direct_main,
                      ['--dir', self.tempdir])

  def testNoDir(self):
    """We should die when run on a missing dir."""
    self.assertRaises(pre_upload.BadInvocation, pre_upload.direct_main,
                      ['--dir', os.path.join(self.tempdir, 'foooooooo')])

  def testCommitList(self):
    """Any args on the command line should be treated as commits."""
    commits = ['sha1', 'sha2', 'shaaaaaaaaaaaan']
    ret = pre_upload.direct_main(commits)
    self.assertEqual(ret, 0)
    self.hooks_mock.assert_called_once_with(
        mock.ANY, proj_dir=mock.ANY, commit_list=commits, presubmit=mock.ANY)


class CheckRustfmtTest(PreUploadTestCase):
  """Tests for _check_rustfmt."""

  def setUp(self):
    self.content_mock = self.PatchObject(pre_upload, '_get_file_content')

  def testBadRustFile(self):
    self.PatchObject(pre_upload, '_get_affected_files', return_value=['a.rs'])
    # Bad because it's missing trailing newline.
    content = 'fn main() {}'
    self.content_mock.return_value = content
    self.PatchObject(pre_upload, '_run_command', return_value=content + '\n')
    failure = pre_upload._check_rustfmt(ProjectNamed('PROJECT'), 'COMMIT')
    self.assertIsNotNone(failure)
    self.assertEqual('Files not formatted with rustfmt: '
                     "(run 'cargo fmt' to fix)",
                     failure.msg)
    self.assertEqual(['a.rs'], failure.items)

  def testGoodRustFile(self):
    self.PatchObject(pre_upload, '_get_affected_files', return_value=['a.rs'])
    content = 'fn main() {}\n'
    self.content_mock.return_value = content
    self.PatchObject(pre_upload, '_run_command', return_value=content)
    failure = pre_upload._check_rustfmt(ProjectNamed('PROJECT'), 'COMMIT')
    self.assertIsNone(failure)

  def testFilterNonRustFiles(self):
    self.PatchObject(pre_upload, '_get_affected_files',
                     return_value=['a.cc', 'a.rsa', 'a.irs', 'rs.cc'])
    self.content_mock.return_value = 'fn main() {\n}'
    failure = pre_upload._check_rustfmt(ProjectNamed('PROJECT'), 'COMMIT')
    self.assertIsNone(failure)


if __name__ == '__main__':
  cros_test_lib.main(module=__name__)
