# Copyright 2012 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unittests for pre-upload.py."""

import configparser
import datetime
import os
from pathlib import Path
from unittest import mock

import errors

from chromite.lib import build_query
from chromite.lib import constants
from chromite.lib import cros_test_lib
from chromite.lib import git
from chromite.lib import osutils
from chromite.lib import path_util


# We access private members of the pre_upload module.
# pylint: disable=protected-access


DIR = Path(__file__).resolve().parent


pre_upload = __import__("pre-upload")


def ProjectNamed(project_name):
    """Wrapper to create a Project with just the name"""
    return pre_upload.Project(project_name, "/does/not/exist", None)


class PreUploadTestCase(cros_test_lib.MockTestCase):
    """Common test case base."""

    def setUp(self):
        pre_upload.CACHE.clear()


class TryUTF8DecodeTest(PreUploadTestCase):
    """Verify we sanely handle unicode content."""

    def setUp(self):
        self.rc_mock = self.StartPatcher(cros_test_lib.RunCommandMock())

    def _run(self, content):
        """Helper for round tripping through _run_command."""
        self.rc_mock.SetDefaultCmdResult(0, stdout=content)
        return pre_upload._run_command(["_"])

    def testEmpty(self):
        """Check empty output."""
        ret = self._run(b"")
        self.assertEqual("", ret)

    def testAscii(self):
        """Check ascii output."""
        ret = self._run(b"abc")
        self.assertEqual("abc", ret)

    def testUtf8(self):
        """Check valid UTF-8 output."""
        text = "ä½ å¥½å¸èæ©"
        ret = self._run(text.encode("utf-8"))
        self.assertEqual(text, ret)

    def testBinary(self):
        """Check binary (invalid UTF-8) output."""
        ret = self._run(b"hi \x80 there")
        self.assertEqual("hi \ufffd there", ret)


class CacheTest(cros_test_lib.MockTestCase):
    """Tests for Cache."""

    def setUp(self):
        self.cache = pre_upload.Cache()

    def testDistinctScopes(self):
        """Keys are separated between scopes."""

        @self.cache.cache_function
        def s1(_):
            return 1

        @self.cache.cache_function
        def s2(_):
            return 2

        self.assertEqual(1, s1("value"))
        # cache_function doesn't return the value from s1
        self.assertEqual(2, s2("value"))

    def testDecorator(self):
        """Cached functions return cached values when present."""
        mock_fn = mock.Mock(return_value=123)
        mock_fn.__name__ = "decorated"

        cached_fn = self.cache.cache_function(mock_fn)
        self.assertEqual(123, cached_fn("a"))
        self.assertEqual(123, cached_fn("a"))
        mock_fn.assert_called_once_with("a")


class CheckKeywordsTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Tests for _check_keywords."""

    def setUp(self):
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["x.ebuild"]
        )
        self.PatchObject(pre_upload, "_filter_files", return_value=["x.ebuild"])
        # First call reads blocked_terms.txt, second call reads
        # unblocked_terms.txt for _get_file_diff, and third call reads
        # unblocked_terms.txt for _get_commit_desc.
        self.rf_mock = self.PatchObject(
            osutils,
            "ReadFile",
            side_effect=[
                "scruffy\nmangy\ndog.?pile\ncat.?circle",
                "fox",
                "fox",
            ],
        )
        self.diff_mock = self.PatchObject(pre_upload, "_get_file_diff")
        self.desc_mock = self.PatchObject(pre_upload, "_get_commit_desc")
        self.project = pre_upload.Project(
            name="PROJECT", dir=self.tempdir, remote=None
        )

    def test_good_cases(self):
        self.desc_mock.return_value = "Commit Message.\nLine 2"
        self.diff_mock.return_value = [
            (1, "Some text without keywords."),
            (2, "The dog is black has a partial keyword that does not count."),
        ]
        failures = pre_upload._check_keywords(self.project, "COMMIT")
        self.assertEqual(failures, [])

        self.rf_mock.assert_has_calls(
            [
                mock.call(
                    pre_upload.REPOHOOKS_DIR / pre_upload.BLOCKED_TERMS_FILE
                ),
                mock.call(pre_upload.UNBLOCKED_TERMS_FILE),
                mock.call(
                    pre_upload.REPOHOOKS_DIR / pre_upload.UNBLOCKED_TERMS_FILE
                ),
            ]
        )

    def test_bad_cases(self):
        self.desc_mock.return_value = "Commit Message.\nLine 2\nLine 3 scruffy"
        self.diff_mock.return_value = [
            (1, "Scruffy plain catch"),
            (2, "dog-pile hyphenated catch"),
            (3, "cat_circle underscored catch"),
            (4, "dog pile space catch"),
            (5, "dogpiled substring catch"),
            (6, "scruffy mangy dog, multiple in a line catch"),
        ]
        failures = pre_upload._check_keywords(self.project, "COMMIT")
        self.assertNotEqual(failures, [])
        self.assertEqual("Found a blocked keyword in:", failures[0].msg)
        self.assertEqual(
            [
                'x.ebuild, line 1: Matched "Scruffy" with regex of "scruffy"',
                (
                    'x.ebuild, line 2: Matched "dog-pile" with regex of '
                    '"dog.?pile"'
                ),
                (
                    'x.ebuild, line 3: Matched "cat_circle" with regex of '
                    '"cat.?circle"'
                ),
                (
                    'x.ebuild, line 4: Matched "dog pile" with regex of '
                    '"dog.?pile"'
                ),
                'x.ebuild, line 5: Matched "dogpile" with regex of "dog.?pile"',
                'x.ebuild, line 6: Matched "mangy" with regex of "mangy"',
            ],
            failures[0].items,
        )
        self.assertEqual("Found a blocked keyword in:", failures[1].msg)
        self.assertEqual(
            [
                (
                    'Commit message, line 3: Matched "scruffy" with regex of '
                    '"scruffy"'
                )
            ],
            failures[1].items,
        )

    def test_block_option_cases(self):
        self.desc_mock.return_value = "Commit Message.\nLine 2 voldemort"
        self.diff_mock.return_value = [
            (1, "Line with a new term voldemort."),
            (2, "Line with only they who shall not be named."),
        ]
        failures = pre_upload._check_keywords(
            self.project, "COMMIT", ["--block", "voldemort"]
        )
        self.assertNotEqual(failures, [])
        self.assertEqual("Found a blocked keyword in:", failures[0].msg)
        self.assertEqual(
            ['x.ebuild, line 1: Matched "voldemort" with regex of "voldemort"'],
            failures[0].items,
        )
        self.assertEqual("Found a blocked keyword in:", failures[1].msg)
        self.assertEqual(
            [
                "Commit message, line 2: "
                'Matched "voldemort" with regex of "voldemort"'
            ],
            failures[1].items,
        )

    def test_unblock_option_cases(self):
        self.desc_mock.return_value = "Commit message with scruffy"
        self.diff_mock.return_value = [
            (1, "Line with two unblocked terms scruffy big dog-pile"),
            (2, "Line with without any blocked terms"),
        ]
        # scruffy matches regex of 'scruffy' in block list but excluded by
        # different regex of 'scru.?fy' in unblock list.
        failures = pre_upload._check_keywords(
            self.project,
            "COMMIT",
            ["--unblock", "dog.?pile", "--unblock", "scru.?fy"],
        )
        self.assertEqual(failures, [])

    def test_unblock_and_block_option_cases(self):
        self.desc_mock.return_value = "Commit message with scruffy"
        self.diff_mock.return_value = [
            (1, "Two unblocked terms scruffy and dog-pile"),
            (2, "Without any blocked terms"),
            (3, "Blocked dogpile"),
            (4, "Unblocked m.dogpile"),
            (5, "Blocked dogpile and unblocked m.dogpile"),
            (6, "Unlocked m.dogpile and blocked dogpile"),
            (7, "Unlocked m.dogpile and unblocked dog-pile"),
        ]
        # scruffy matches regex of 'scruffy' in block list but excluded by
        # a different regex of 'scru.?fy' in unblock list.
        # dogpile, dog.pile matches regex of 'dog.?pile' in block list.
        # m.dogpile and dog-pile matches regex of 'dog.?pile' in block list but
        # excluded by different regex '\.dog.?pile' and 'dog-pile' in unblock
        # list.
        failures = pre_upload._check_keywords(
            self.project,
            "COMMIT",
            [
                "--unblock",
                r"dog-pile",
                "--unblock",
                r"scru.?fy",
                "--unblock",
                r"\.dog.?pile",
            ],
        )
        self.assertNotEqual(failures, [])
        self.assertEqual("Found a blocked keyword in:", failures[0].msg)
        self.assertEqual(
            [
                (
                    r'x.ebuild, line 3: Matched "dogpile" with regex of '
                    r'"dog.?pile"'
                ),
                (
                    r'x.ebuild, line 5: Matched "dogpile" with regex of '
                    r'"dog.?pile"'
                ),
                (
                    r'x.ebuild, line 6: Matched "dogpile" with regex of '
                    r'"dog.?pile"'
                ),
            ],
            failures[0].items,
        )

    def test_upstream_prefix_cases(self):
        """Verify upstream patches skip the keywords test."""
        self.desc_mock.return_value = "UPSTREAM: Commit message"
        self.diff_mock.return_value = [
            (1, "Some content with voldemort"),
        ]
        # The UPSTREAM: prefix should cause the terms to be ignored.
        failures = pre_upload._check_keywords(
            self.project, "COMMIT", ["--block", "voldemort"]
        )
        self.assertEqual(failures, ())
        self.desc_mock.return_value = "UPSTAIRS: Commit message"
        # The UPSTAIRS: prefix is not recognized so the term should trigger a
        # failure.
        failures = pre_upload._check_keywords(
            self.project, "COMMIT", ["--block", "voldemort"]
        )
        self.assertNotEqual(failures, ())
        self.assertEqual("Found a blocked keyword in:", failures[0].msg)
        self.assertEqual(
            ['x.ebuild, line 1: Matched "voldemort" with regex of "voldemort"'],
            failures[0].items,
        )


class CheckNoBlankLinesTest(PreUploadTestCase):
    """Tests for _check_no_extra_blank_lines."""

    MOCK_FILE_CONTENT = {
        "x0.cc": "",  # Good
        "x1.cc": "y",  # Good
        "x2.cc": "z\n",  # Good
        "x3.cc": "\n\n",  # Bad
        "x4.cc": "y\n\n",  # Bad
        "x5.cc": "z\nz\n\n\n",  # Bad
    }

    @classmethod
    def _get_file_content(cls, path, _commit):
        return cls.MOCK_FILE_CONTENT[path]

    def _run_check(self, files):
        self.PatchObject(pre_upload, "_get_affected_files", return_value=files)
        self.PatchObject(
            pre_upload, "_get_file_content", new=self._get_file_content
        )
        failure = pre_upload._check_no_extra_blank_lines(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        return failure

    def test_good_cases(self):
        failure = self._run_check(["x0.cc", "x1.cc", "x2.cc"])
        self.assertIsNone(failure)

    def test_bad_cases(self):
        failure = self._run_check(["x3.cc", "x4.cc", "x5.cc"])
        self.assertTrue(failure)
        self.assertEqual("Found extra blank line at end of file:", failure.msg)
        self.assertEqual(["x3.cc", "x4.cc", "x5.cc"], failure.items)


class CheckNoHandleEintrCloseTest(PreUploadTestCase):
    """Tests for _check_no_handle_eintr_close."""

    def setUp(self):
        self.diff_mock = self.PatchObject(pre_upload, "_get_file_diff")

    def testGoodCase(self):
        path = "x.cc"
        self.PatchObject(pre_upload, "_get_affected_files", return_value=[path])
        self.diff_mock.return_value = [
            (1, "  int fd = HANDLE_EINTR(open(path, 0));"),  # Good
            (2, "  IGNORE_EINTR(close(fd));"),  # Good
        ]
        failure = pre_upload._check_no_handle_eintr_close(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertIsNone(failure)

    def testBadCase(self):
        path = "x.cc"
        self.PatchObject(pre_upload, "_get_affected_files", return_value=[path])
        self.diff_mock.return_value = [
            (1, "  int fd = HANDLE_EINTR(open(path, 0));"),  # Good
            (2, "  HANDLE_EINTR(close(fd));"),  # Bad
        ]
        failure = pre_upload._check_no_handle_eintr_close(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertTrue(failure)
        self.assertEqual(
            "HANDLE_EINTR(close) is invalid. See http://crbug.com/269623.",
            failure.msg,
        )
        self.assertEqual(["x.cc, line 2"], failure.items)


class CheckNoLongLinesTest(PreUploadTestCase):
    """Tests for _check_no_long_lines."""

    def setUp(self):
        self.diff_mock = self.PatchObject(pre_upload, "_get_file_diff")

    def testCheck(self):
        path = "x.cc"
        self.PatchObject(pre_upload, "_get_affected_files", return_value=[path])
        self.diff_mock.return_value = [
            (1, "x" * 80),  # OK
            (2, "\x80" * 80),  # OK
            (3, "x" * 81),  # Too long
            (4, "\x80" * 81),  # Too long
            (5, "See http://" + ("x" * 80)),  # OK (URL)
            (6, "See https://" + ("x" * 80)),  # OK (URL)
            (7, "#  define " + ("x" * 80)),  # OK (compiler directive)
            (8, "#define" + ("x" * 74)),  # Too long
        ]
        failure = pre_upload._check_no_long_lines(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertTrue(failure)
        self.assertEqual(
            "Found lines longer than the limit (first 5 shown):", failure.msg
        )
        self.assertEqual(
            [
                path + ", line %d, 81 chars, over 80 chars" % x
                for x in [3, 4, 8]
            ],
            failure.items,
        )

    def testCheckTreatsOwnersFilesSpecially(self):
        affected_files = self.PatchObject(pre_upload, "_get_affected_files")

        mock_files = (
            ("foo-OWNERS", False),
            ("OWNERS", True),
            ("/path/to/OWNERS", True),
            ("/path/to/OWNERS.foo", True),
        )

        mock_lines = (
            ("x" * 81, False),
            ("foo file:" + "x" * 80, True),
            ("include " + "x" * 80, True),
        )
        assert all(len(line) > 80 for line, _ in mock_lines)

        for file_name, is_owners in mock_files:
            affected_files.return_value = [file_name]
            for line, is_ok in mock_lines:
                self.diff_mock.return_value = [(1, line)]
                failure = pre_upload._check_no_long_lines(
                    ProjectNamed("PROJECT"), "COMMIT"
                )

                assert_msg = "file: %r; line: %r" % (file_name, line)
                if is_owners and is_ok:
                    self.assertFalse(failure, assert_msg)
                else:
                    self.assertTrue(failure, assert_msg)
                    self.assertIn(
                        "Found lines longer than the limit",
                        failure.msg,
                        assert_msg,
                    )

    def testIncludeOptions(self):
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["foo.txt"]
        )
        self.diff_mock.return_value = [(1, "x" * 81)]
        self.assertFalse(
            pre_upload._check_no_long_lines(ProjectNamed("PROJECT"), "COMMIT")
        )
        self.assertTrue(
            pre_upload._check_no_long_lines(
                ProjectNamed("PROJECT"),
                "COMMIT",
                options=["--include_regex=foo"],
            )
        )

    def testExcludeOptions(self):
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["foo.cc"]
        )
        self.diff_mock.return_value = [(1, "x" * 81)]
        self.assertTrue(
            pre_upload._check_no_long_lines(ProjectNamed("PROJECT"), "COMMIT")
        )
        self.assertFalse(
            pre_upload._check_no_long_lines(
                ProjectNamed("PROJECT"),
                "COMMIT",
                options=["--exclude_regex=foo"],
            )
        )

    def testSpecialLineLength(self):
        mock_lines = (
            ("x" * 101, True),
            ("x" * 100, False),
            ("x" * 81, False),
            ("x" * 80, False),
        )
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["foo.java"]
        )
        for line, is_ok in mock_lines:
            self.diff_mock.return_value = [(1, line)]
            if is_ok:
                self.assertTrue(
                    pre_upload._check_no_long_lines(
                        ProjectNamed("PROJECT"), "COMMIT"
                    )
                )
            else:
                self.assertFalse(
                    pre_upload._check_no_long_lines(
                        ProjectNamed("PROJECT"), "COMMIT"
                    )
                )


class CheckTabbedIndentsTest(PreUploadTestCase):
    """Tests for _check_tabbed_indents."""

    def setUp(self):
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["x.ebuild"]
        )
        self.diff_mock = self.PatchObject(pre_upload, "_get_file_diff")

    def test_good_cases(self):
        self.diff_mock.return_value = [
            (1, "no_tabs_anywhere"),
            (2, "\tleading_tab_only"),
            (3, "\tleading_tab another_tab"),
            (4, "\tleading_tab trailing_too\t"),
            (5, "\tleading_tab  then_spaces_trailing  "),
        ]
        failure = pre_upload._check_tabbed_indents(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertIsNone(failure)

    def test_bad_cases(self):
        self.diff_mock.return_value = [
            (1, " leading_space"),
            (2, "\t tab_followed_by_space"),
            (3, "  \t\t\tspace_followed_by_tab"),
            (4, "   \t\t\t  mix_em_up"),
            (5, "\t  \t\t  \t\t\tmix_on_both_sides\t\t\t   "),
        ]
        failure = pre_upload._check_tabbed_indents(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertTrue(failure)
        self.assertEqual(
            "Found a space in indentation (must be all tabs):", failure.msg
        )
        self.assertEqual(
            ["x.ebuild, line %d" % x for x in range(1, 6)], failure.items
        )


class CheckProjectPrefix(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Tests for _check_project_prefix."""

    def setUp(self):
        os.chdir(self.tempdir)
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.desc_mock = self.PatchObject(pre_upload, "_get_commit_desc")

    def _WriteAliasFile(self, filename, project):
        """Writes a project name to a file, creating directories if needed."""
        os.makedirs(os.path.dirname(filename))
        osutils.WriteFile(filename, project)

    def testInvalidPrefix(self):
        """Report an error when the prefix doesn't match the base directory."""
        self.file_mock.return_value = ["foo/foo.cc", "foo/subdir/baz.cc"]
        self.desc_mock.return_value = "bar: Some commit"
        failure = pre_upload._check_project_prefix(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertTrue(failure)
        self.assertEqual(
            "The commit title for changes affecting only foo should "
            'start with "foo: "',
            failure.msg,
        )

    def testValidPrefix(self):
        """Use a prefix that matches the base directory."""
        self.file_mock.return_value = ["foo/foo.cc", "foo/subdir/baz.cc"]
        self.desc_mock.return_value = "foo: Change some files."
        self.assertFalse(
            pre_upload._check_project_prefix(ProjectNamed("PROJECT"), "COMMIT")
        )

    def testAliasFile(self):
        """Use .project_alias to override the project name."""
        self._WriteAliasFile("foo/.project_alias", "project")
        self.file_mock.return_value = ["foo/foo.cc", "foo/subdir/bar.cc"]
        self.desc_mock.return_value = "project: Use an alias."
        self.assertFalse(
            pre_upload._check_project_prefix(ProjectNamed("PROJECT"), "COMMIT")
        )

    def testAliasFileWithSubdirs(self):
        """Check that .project_alias is used when only modifying subdirs."""
        self._WriteAliasFile("foo/.project_alias", "project")
        self.file_mock.return_value = [
            "foo/subdir/foo.cc",
            "foo/subdir/bar.cc",
            "foo/subdir/blah/baz.cc",
        ]
        self.desc_mock.return_value = "project: Alias with subdirs."
        self.assertFalse(
            pre_upload._check_project_prefix(ProjectNamed("PROJECT"), "COMMIT")
        )


class CheckFilePathCharTypeTest(PreUploadTestCase):
    """Tests for _check_filepath_chartype."""

    def setUp(self):
        self.diff_mock = self.PatchObject(pre_upload, "_get_file_diff")

    def testCheck(self):
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["x.cc"]
        )
        self.diff_mock.return_value = [
            (1, "base::FilePath"),  # OK
            (2, "base::FilePath::CharType"),  # NG
            (3, "base::FilePath::StringType"),  # NG
            (4, "base::FilePath::StringPieceType"),  # NG
            (5, "base::FilePath::FromUTF8Unsafe"),  # NG
            (6, "FilePath::CharType"),  # NG
            (7, "FilePath::StringType"),  # NG
            (8, "FilePath::StringPieceType"),  # NG
            (9, "FilePath::FromUTF8Unsafe"),  # NG
            (10, "AsUTF8Unsafe"),  # NG
            (11, "FILE_PATH_LITERAL"),  # NG
        ]
        failure = pre_upload._check_filepath_chartype(
            ProjectNamed("PROJECT"), "COMMIT"
        )
        self.assertTrue(failure)
        self.assertEqual(
            "Please assume FilePath::CharType is char (crbug.com/870621):",
            failure.msg,
        )
        self.assertEqual(
            [
                "x.cc, line 2 has base::FilePath::CharType",
                "x.cc, line 3 has base::FilePath::StringType",
                "x.cc, line 4 has base::FilePath::StringPieceType",
                "x.cc, line 5 has base::FilePath::FromUTF8Unsafe",
                "x.cc, line 6 has FilePath::CharType",
                "x.cc, line 7 has FilePath::StringType",
                "x.cc, line 8 has FilePath::StringPieceType",
                "x.cc, line 9 has FilePath::FromUTF8Unsafe",
                "x.cc, line 10 has AsUTF8Unsafe",
                "x.cc, line 11 has FILE_PATH_LITERAL",
            ],
            failure.items,
        )


class CheckKernelConfig(PreUploadTestCase):
    """Tests for _kernel_configcheck."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")

    def testMixedChanges(self):
        """Mixing of changes should fail."""
        self.file_mock.return_value = [
            "/kernel/files/chromeos/config/base.config",
            "/kernel/files/arch/arm/mach-exynos/mach-exynos5-dt.c",
        ]
        failure = pre_upload._kernel_configcheck("PROJECT", "COMMIT")
        self.assertTrue(failure)

    def testCodeOnly(self):
        """Code-only changes should pass."""
        self.file_mock.return_value = [
            "/kernel/files/Makefile",
            "/kernel/files/arch/arm/mach-exynos/mach-exynos5-dt.c",
        ]
        failure = pre_upload._kernel_configcheck("PROJECT", "COMMIT")
        self.assertFalse(failure)

    def testConfigOnlyChanges(self):
        """Config-only changes should pass."""
        self.file_mock.return_value = [
            "/kernel/files/chromeos/config/base.config",
        ]
        failure = pre_upload._kernel_configcheck("PROJECT", "COMMIT")
        self.assertFalse(failure)


class CheckManifests(PreUploadTestCase):
    """Tests _check_manifests."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testNoManifests(self):
        """Nothing should be checked w/no Manifest files."""
        self.file_mock.return_value = [
            "/foo/bar.txt",
            "/readme.md",
            "/manifest",
            "/Manifest.txt",
        ]
        ret = pre_upload._check_manifests("PROJECT", "COMMIT")
        self.assertIsNone(ret)

    def testValidManifest(self):
        """Accept valid Manifest files."""
        self.file_mock.return_value = [
            "/foo/bar.txt",
            "/cat/pkg/Manifest",
        ]
        self.content_mock.return_value = """# Comment and blank lines.

DIST lines
"""
        ret = pre_upload._check_manifests("PROJECT", "COMMIT")
        self.assertIsNone(ret)
        self.content_mock.assert_called_once_with("/cat/pkg/Manifest", "COMMIT")

    def _testReject(self, content):
        """Make sure |content| is rejected."""
        self.file_mock.return_value = ("/Manifest",)
        self.content_mock.reset_mock()
        self.content_mock.return_value = content
        ret = pre_upload._check_manifests("PROJECT", "COMMIT")
        self.assertIsNotNone(ret)
        self.content_mock.assert_called_once_with("/Manifest", "COMMIT")

    def testRejectBlank(self):
        """Reject empty manifests."""
        self._testReject("")

    def testNoTrailingNewLine(self):
        """Reject manifests w/out trailing newline."""
        self._testReject("DIST foo")

    def testNoLeadingBlankLines(self):
        """Reject manifests w/leading blank lines."""
        self._testReject("\nDIST foo\n")

    def testNoTrailingBlankLines(self):
        """Reject manifests w/trailing blank lines."""
        self._testReject("DIST foo\n\n")

    def testNoLeadingWhitespace(self):
        """Reject manifests w/lines w/leading spaces."""
        self._testReject(" DIST foo\n")
        self._testReject("  # Comment\n")

    def testNoTrailingWhitespace(self):
        """Reject manifests w/lines w/trailing spaces."""
        self._testReject("DIST foo \n")
        self._testReject("# Comment \n")
        self._testReject("  \n")

    def testOnlyDistLines(self):
        """Only allow DIST lines in here."""
        self._testReject("EBUILD foo\n")


class CheckPortageMakeUseVar(PreUploadTestCase):
    """Tests for _check_portage_make_use_var."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testMakeConfOmitsOriginalUseValue(self):
        """Fail for make.conf that discards the previous value of $USE."""
        self.file_mock.return_value = ["make.conf"]
        self.content_mock.return_value = 'USE="foo"\nUSE="${USE} bar"'
        failure = pre_upload._check_portage_make_use_var("PROJECT", "COMMIT")
        self.assertTrue(failure, failure)

    def testMakeConfCorrectUsage(self):
        """Succeed for make.conf that preserves the previous value of $USE."""
        self.file_mock.return_value = ["make.conf"]
        self.content_mock.return_value = 'USE="${USE} foo"\nUSE="${USE}" bar'
        failure = pre_upload._check_portage_make_use_var("PROJECT", "COMMIT")
        self.assertFalse(failure, failure)

    def testMakeDefaultsReferencesOriginalUseValue(self):
        """Fail for make.defaults that refers to a not-yet-set $USE value."""
        self.file_mock.return_value = ["make.defaults"]
        self.content_mock.return_value = 'USE="${USE} foo"'
        failure = pre_upload._check_portage_make_use_var("PROJECT", "COMMIT")
        self.assertTrue(failure, failure)

        # Also check for "$USE" without curly brackets.
        self.content_mock.return_value = 'USE="$USE foo"'
        failure = pre_upload._check_portage_make_use_var("PROJECT", "COMMIT")
        self.assertTrue(failure, failure)

    def testMakeDefaultsOverwritesUseValue(self):
        """Fail for make.defaults that discards its own $USE value."""
        self.file_mock.return_value = ["make.defaults"]
        self.content_mock.return_value = 'USE="foo"\nUSE="bar"'
        failure = pre_upload._check_portage_make_use_var("PROJECT", "COMMIT")
        self.assertTrue(failure, failure)

    def testMakeDefaultsCorrectUsage(self):
        """Succeed for make.defaults that sets and preserves $USE."""
        self.file_mock.return_value = ["make.defaults"]
        self.content_mock.return_value = 'USE="foo"\nUSE="${USE}" bar'
        failure = pre_upload._check_portage_make_use_var("PROJECT", "COMMIT")
        self.assertFalse(failure, failure)


class CheckEbuildEapi(PreUploadTestCase):
    """Tests for _check_ebuild_eapi."""

    PORTAGE_STABLE = ProjectNamed("chromiumos/overlays/portage-stable")

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")
        self.diff_mock = self.PatchObject(
            pre_upload, "_get_file_diff", side_effect=Exception()
        )

    def testSkipUpstreamOverlays(self):
        """Skip ebuilds found in upstream overlays."""
        self.file_mock.side_effect = Exception()
        ret = pre_upload._check_ebuild_eapi(self.PORTAGE_STABLE, "HEAD")
        self.assertIsNone(ret)

        # Make sure our condition above triggers.
        self.assertRaises(Exception, pre_upload._check_ebuild_eapi, "o", "HEAD")

    def testSkipNonEbuilds(self):
        """Skip non-ebuild files."""
        self.content_mock.side_effect = Exception()

        self.file_mock.return_value = ["some-file", "ebuild/dir", "an.ebuild~"]
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)

        # Make sure our condition above triggers.
        self.file_mock.return_value.append("a/real.ebuild")
        self.assertRaises(
            Exception, pre_upload._check_ebuild_eapi, ProjectNamed("o"), "HEAD"
        )

    def testSkipSymlink(self):
        """Skip files that are just symlinks."""
        self.file_mock.return_value = ["a-r1.ebuild"]
        self.content_mock.return_value = "a.ebuild"
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)

    def testRejectEapiImplicit0Content(self):
        """Reject ebuilds that do not declare EAPI (so it's 0)."""
        self.file_mock.return_value = ["a.ebuild"]

        self.content_mock.return_value = """# Header
IUSE="foo"
src_compile() { }
"""
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testRejectExplicitEapi1Content(self):
        """Reject ebuilds that do declare old EAPI explicitly."""
        self.file_mock.return_value = ["a.ebuild"]

        template = """# Header
EAPI=%s
IUSE="foo"
src_compile() { }
"""
        # Make sure we only check the first EAPI= setting.
        self.content_mock.return_value = template % "1\nEAPI=60"
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertTrue(isinstance(ret, errors.HookFailure))

        # Verify we handle double quotes too.
        self.content_mock.return_value = template % '"1"'
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertTrue(isinstance(ret, errors.HookFailure))

        # Verify we handle single quotes too.
        self.content_mock.return_value = template % "'1'"
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testAcceptExplicitNewEapiContent(self):
        """Accept ebuilds that do declare new EAPI explicitly."""
        self.file_mock.return_value = ["a.ebuild"]

        template = """# Header
EAPI=%s
IUSE="foo"
src_compile() { }
"""
        # Make sure we only check the first EAPI= setting.
        self.content_mock.return_value = template % "8\nEAPI=1"
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)

        # Verify we handle double quotes too.
        self.content_mock.return_value = template % '"7"'
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)

        # Verify we handle single quotes too.
        self.content_mock.return_value = template % "'7-hdepend'"
        ret = pre_upload._check_ebuild_eapi(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)


class CheckEbuildKeywords(PreUploadTestCase):
    """Tests for _check_ebuild_keywords."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testNoEbuilds(self):
        """If no ebuilds are found, do not scan."""
        self.file_mock.return_value = ["a.file", "ebuild-is-not.foo"]

        ret = pre_upload._check_ebuild_keywords(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)

        self.assertEqual(self.content_mock.call_count, 0)

    def testSomeEbuilds(self):
        """If ebuilds are found, only scan them."""
        self.file_mock.return_value = ["a.file", "blah", "foo.ebuild", "cow"]
        self.content_mock.return_value = ""

        ret = pre_upload._check_ebuild_keywords(ProjectNamed("overlay"), "HEAD")
        self.assertIsNone(ret)

        self.assertEqual(self.content_mock.call_count, 1)

    def _CheckContent(self, content, fails):
        """Test helper for inputs/outputs.

        Args:
            content: The ebuild content to test.
            fails: Whether |content| should trigger a hook failure.
        """
        self.file_mock.return_value = ["a.ebuild"]
        self.content_mock.return_value = content

        ret = pre_upload._check_ebuild_keywords(ProjectNamed("overlay"), "HEAD")
        if fails:
            self.assertTrue(isinstance(ret, errors.HookFailure))
        else:
            self.assertIsNone(ret)

        self.assertEqual(self.content_mock.call_count, 1)

    def testEmpty(self):
        """Check KEYWORDS= is accepted."""
        self._CheckContent("# HEADER\nKEYWORDS=\nblah\n", False)

    def testEmptyQuotes(self):
        """Check KEYWORDS="" is accepted."""
        self._CheckContent('# HEADER\nKEYWORDS="    "\nblah\n', False)

    def testStableGlob(self):
        """Check KEYWORDS=* is accepted."""
        self._CheckContent('# HEADER\nKEYWORDS="\t*\t"\nblah\n', False)

    def testUnstableGlob(self):
        """Check KEYWORDS=~* is accepted."""
        self._CheckContent('# HEADER\nKEYWORDS="~* "\nblah\n', False)

    def testRestrictedGlob(self):
        """Check KEYWORDS=-* is accepted."""
        self._CheckContent('# HEADER\nKEYWORDS="\t-* arm"\nblah\n', False)

    def testMissingGlobs(self):
        """Reject KEYWORDS missing any globs."""
        self._CheckContent('# HEADER\nKEYWORDS="~arm x86"\nblah\n', True)


class CheckEbuildLicense(PreUploadTestCase):
    """Tests for _check_ebuild_licenses."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testNoEbuilds(self):
        """If no ebuilds are found, do not scan."""
        self.file_mock.return_value = ["a.file", "ebuild-is-not.foo"]

        ret = pre_upload._check_ebuild_licenses(ProjectNamed("overlay"), "HEAD")
        self.assertFalse(ret)

        self.assertEqual(self.content_mock.call_count, 0)

    def testSomeEbuilds(self):
        """If ebuilds are found, only scan them."""
        self.file_mock.return_value = [
            "a.file",
            "blah",
            "cow",
            "overlay/category/pkg/pkg.ebuild",
        ]
        self.content_mock.return_value = '# HEADER\nLICENSE="GPL-3"\nblah\n'

        ret = pre_upload._check_ebuild_licenses(ProjectNamed("overlay"), "HEAD")
        self.assertFalse(ret)

        self.assertEqual(self.content_mock.call_count, 1)

    def _CheckContent(self, license_field, ebuild_path, fails):
        """Test helper for inputs/outputs.

        Args:
            license_field: Contents of LICENSE variable in the tested ebuild.
            ebuild_path: The path to the tested ebuild.
            fails: Whether inputs should trigger a hook failure.
        """
        self.file_mock.return_value = [ebuild_path]
        self.content_mock.return_value = (
            f'# blah\nLICENSE="{license_field}"\nbla\n'
        )

        ret = pre_upload._check_ebuild_licenses(ProjectNamed("overlay"), "HEAD")
        if fails:
            self.assertIsInstance(ret, list)
            self.assertIsInstance(ret[0], errors.HookFailure)
        else:
            self.assertFalse(ret)

        self.assertEqual(self.content_mock.call_count, 1)

    def testEmpty(self):
        """Check empty license is not accepted."""
        self._CheckContent("", "overlay/category/pkg/pkg.ebuild", True)

    def testValid(self):
        """Check valid license is accepted."""
        self._CheckContent("GPL-3", "overlay/category/pkg/pkg.ebuild", False)

    def testVirtualNotMetapackage(self):
        """Check virtual package not using metapackage is not accepted."""
        self._CheckContent("GPL-3", "overlay/virtual/pkg/pkg.ebuild", True)

    def testVirtualMetapackage(self):
        """Check virtual package using metapackage is accepted."""
        self._CheckContent(
            "metapackage", "overlay/virtual/pkg/pkg.ebuild", False
        )

    def testAcctUserPackageNoLicense(self):
        """Check acct-user packages allow LICENSE=""."""
        self._CheckContent("", "overlay/acct-user/foo/foo.ebuild", False)

    def testAcctUserPackageLicense(self):
        """Check acct-user packages reject LICENSE."""
        self._CheckContent("GPL-2", "overlay/acct-user/foo/foo.ebuild", True)

    def testAcctGroupPackageNoLicense(self):
        """Check acct-group packages allow LICENSE=""."""
        self._CheckContent("", "overlay/acct-group/foo/foo.ebuild", False)

    def testAcctGroupPackageLicense(self):
        """Check acct-group packages reject LICENSE."""
        self._CheckContent("GPL-2", "overlay/acct-group/foo/foo.ebuild", True)


class CheckEbuildLocalnameExists(
    PreUploadTestCase, cros_test_lib.MockTempDirTestCase
):
    """Tests for _check_ebuild_localname_exists."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")
        self.PatchObject(constants, "SOURCE_ROOT", new=str(self.tempdir))

        src = self.tempdir / "src"
        src_exists = src / "src_exists"
        osutils.SafeMakedirs(src_exists)

        third_party = src / "third_party"
        third_party_exists = third_party / "tp_exists"
        osutils.SafeMakedirs(third_party_exists)

    def testNoEbuilds(self):
        """If no ebuilds are found, do not scan."""
        self.file_mock.return_value = ["a.file", "ebuild-is-not.foo"]

        ret = pre_upload._check_ebuild_localname_exists(
            ProjectNamed("overlay"), "HEAD"
        )

        self.assertIsNone(ret)
        self.assertEqual(self.content_mock.call_count, 0)

    def testStableEbuilds(self):
        """If stable ebuilds are found, do not scan."""
        self.file_mock.return_value = ["a.file", "blah", "foo.ebuild", "cow"]
        self.content_mock.return_value = ""

        ret = pre_upload._check_ebuild_localname_exists(
            ProjectNamed("overlay"), "HEAD"
        )

        self.assertIsNone(ret)
        self.assertEqual(self.content_mock.call_count, 0)

    def testRevision9999Ebuilds(self):
        """If stable ebuilds are found, do not scan."""
        self.file_mock.return_value = ["a.file", "foo-1.0-r9999.ebuild", "cow"]
        self.content_mock.return_value = ""

        ret = pre_upload._check_ebuild_localname_exists(
            ProjectNamed("overlay"), "HEAD"
        )

        self.assertIsNone(ret)
        self.assertEqual(self.content_mock.call_count, 0)

    def testUnstableEbuilds(self):
        """If unstable ebuilds are found, scan them."""
        self.file_mock.return_value = [
            "a.file",
            "blah",
            "foo-9999.ebuild",
            "cow",
        ]
        self.content_mock.return_value = ""

        ret = pre_upload._check_ebuild_localname_exists(
            ProjectNamed("overlay"), "HEAD"
        )

        self.assertIsNone(ret)
        self.assertEqual(self.content_mock.call_count, 1)

    def _CheckContent(self, ebuild, content, fails):
        """Test helper for inputs/outputs.

        Args:
            ebuild: The ebuild path.
            content: The ebuild content to test.
            fails: Whether |content| should trigger a hook failure.
        """
        self.file_mock.return_value = [ebuild]
        self.content_mock.return_value = content

        ret = pre_upload._check_ebuild_localname_exists(
            ProjectNamed("overlay"), "HEAD"
        )
        if fails:
            self.assertTrue(isinstance(ret, errors.HookFailure))
        else:
            self.assertIsNone(ret)

        self.assertEqual(self.content_mock.call_count, 1)

    def testNotSet(self):
        """Check not set is not an error."""
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            '# HEADER\nEAPI=7\nKEYWORDS="*"',
            False,
        )

    def testNoValue(self):
        """Check no value is not an error."""
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            "# HEADER\nCROS_WORKON_PROJECT=\nCROS_WORKON_LOCALNAME=",
            False,
        )

    def testChromeosBaseExists(self):
        """Check a chromeos-base package localname that does exist."""
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            '# HEADER\nCROS_WORKON_PROJECT="project"\n'
            'CROS_WORKON_LOCALNAME="src_exists"',
            False,
        )

    def testChromeosBaseDoesNotExist(self):
        """Check a chromeos-base package localname that does not exist."""
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            '# HEADER\nCROS_WORKON_PROJECT="project"\n'
            'CROS_WORKON_LOCALNAME="does_not_exist"',
            True,
        )

    def testOtherCategoryExists(self):
        """Check a non chromeos-base package localname that does exist."""
        self._CheckContent(
            "overlay/category/a/a-9999.ebuild",
            '# HEADER\nCROS_WORKON_PROJECT="project"\n'
            'CROS_WORKON_LOCALNAME="tp_exists"',
            False,
        )

    def testOtherCategoryDoesNotExist(self):
        """Check a non chromeos-base package localname that does not exist."""
        self._CheckContent(
            "overlay/category/a/a-9999.ebuild",
            '# HEADER\nCROS_WORKON_PROJECT="project"\n'
            'CROS_WORKON_LOCALNAME="does_not_exist"',
            False,
        )

    def testNestedPathExists(self):
        """Check a nested package localname that does exist."""
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            '# HEADER\nCROS_WORKON_PROJECT="project"\n'
            'CROS_WORKON_LOCALNAME="third_party/tp_exists"',
            False,
        )

    def testMultipleExists(self):
        """Check multiple values that exist."""
        localname = '("src_exists" "third_party/tp_exists")'
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            f'# HEADER\nCROS_WORKON_PROJECT=("project" "project2")\n'
            f"CROS_WORKON_LOCALNAME={localname}",
            False,
        )

    def testMultipleDoesNotExist(self):
        """Check multiple values where one does not exist."""
        localname = '("src_exists" "does_not_exist")'
        self._CheckContent(
            "overlay/chromeos-base/a/a-9999.ebuild",
            f'# HEADER\nCROS_WORKON_PROJECT=("project" "project2")\n'
            f"CROS_WORKON_LOCALNAME={localname}",
            True,
        )


class CheckEbuildOwners(PreUploadTestCase):
    """Tests for _check_ebuild_owners."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(
            pre_upload, "_get_file_content", return_value=None
        )

    def testNoMatches(self):
        """Handle no matching files."""
        self.file_mock.return_value = []
        ret = pre_upload._check_ebuild_owners(ProjectNamed("project"), "HEAD")
        self.assertIsNone(ret)

    def testNoEbuilds(self):
        """Handle CLs w/no ebuilds."""
        self.file_mock.return_value = [
            DiffEntry(src_file="profiles/package.mask", status="M"),
            DiffEntry(src_file="dev-util/pkg/README.md", status="A"),
            DiffEntry(src_file="scripts/sync_sonic.py", status="D"),
        ]
        ret = pre_upload._check_ebuild_owners(ProjectNamed("project"), "HEAD")
        self.assertIsNone(ret)

    def testMissingOwnersFailure(self):
        """Test cases that should flag missing OWNERS."""
        TESTS = (
            [
                DiffEntry(src_file="dev-util/pkg/foo.ebuild", status="A"),
                DiffEntry(src_file="dev-util/pkg/README.md", status="A"),
                DiffEntry(src_file="dev-util/pkg/Manifest", status="A"),
            ],
            [
                DiffEntry(src_file="dev-util/pkg/foo-9999.ebuild", status="M"),
            ],
            [
                DiffEntry(src_file="dev-util/pkg/foo-0.ebuild", status="M"),
                DiffEntry(src_file="dev-util/pkg/foo-0-r1.ebuild", status="A"),
            ],
            [
                DiffEntry(src_file="dev-util/pkg/foo-0-r1.ebuild", status="D"),
                DiffEntry(src_file="dev-util/pkg/foo-0-r2.ebuild", status="A"),
            ],
        )
        for test in TESTS:
            self.file_mock.return_value = test
            ret = pre_upload._check_ebuild_owners(
                ProjectNamed("project"), "HEAD"
            )
            self.assertIsNotNone(ret)

    def testMissingOwnersIgnore(self):
        """Test cases that should ignore missing OWNERS."""
        TESTS = (
            [
                DiffEntry(src_file="dev-util/pkg/metadata.xml", status="M"),
            ],
        )
        for test in TESTS:
            self.file_mock.return_value = test
            ret = pre_upload._check_ebuild_owners(
                ProjectNamed("project"), "HEAD"
            )
            self.assertIsNone(ret)

    def testOwnersExist(self):
        """Test cases where OWNERS exist."""
        TESTS = (
            [
                DiffEntry(src_file="dev-util/pkg/foo-9999.ebuild", status="A"),
            ],
        )
        self.content_mock.return_value = "foo"
        for test in TESTS:
            self.file_mock.return_value = test
            ret = pre_upload._check_ebuild_owners(
                ProjectNamed("project"), "HEAD"
            )
            self.assertIsNone(ret)
        # This should be the # of package dirs across all tests.  This makes
        # sure we actually called the owners check logic and didn't return
        # early.
        self.assertEqual(1, self.content_mock.call_count)

    def testCommonPublicBoardOverlays(self):
        """Allow an OWNERS in the top of the overlay to satisfy requirements."""
        # The public repo that has all public overlays.
        project = ProjectNamed("chromiumos/overlays/board-overlays")

        def _get_content(path, _commit):
            if path == "overlay-oak/dev-util/pkg/OWNERS":
                return None
            elif path == "overlay-oak/dev-util/OWNERS":
                return None
            elif path == "overlay-oak/OWNERS":
                return overlay_owners
            else:
                raise AssertionError(f"Unhandled test path: {path}")

        self.content_mock.side_effect = _get_content
        self.file_mock.return_value = [
            DiffEntry(
                src_file="overlay-oak/dev-util/pkg/pkg-0-r1.ebuild", status="A"
            ),
        ]

        # OWNERS doesn't exist.
        overlay_owners = None
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

        # OWNERS exists, but is empty.
        overlay_owners = ""
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

        # OWNERS exists, but is too permissive.
        overlay_owners = "# Everyone is an owner!\n*\n"
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

        # OWNERS exists, and is good.
        overlay_owners = "foo@chromium.org"
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNone(ret)

    def testCommonPrivateBoardOverlays(self):
        """Allow an OWNERS in the top of the overlay to satisfy requirements."""
        # A private repo that holds one board.
        project = ProjectNamed("chromeos/overlays/baseboard-boo")

        def _get_content(path, _commit):
            if path == "dev-util/pkg/OWNERS":
                return None
            elif path == "dev-util/OWNERS":
                return None
            elif path == "OWNERS":
                return overlay_owners
            else:
                raise AssertionError(f"Unhandled test path: {path}")

        self.content_mock.side_effect = _get_content
        self.file_mock.return_value = [
            DiffEntry(src_file="dev-util/pkg/pkg-0-r1.ebuild", status="A"),
        ]

        # OWNERS doesn't exist.
        overlay_owners = None
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

        # OWNERS exists, but is empty.
        overlay_owners = ""
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

        # OWNERS exists, but is too permissive.
        overlay_owners = "# Everyone is an owner!\n*\n"
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

        # OWNERS exists, and is good.
        overlay_owners = "foo@chromium.org"
        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNone(ret)

    def testSharedOverlays(self):
        """Do not allow top-level OWNERS for shared overlays."""
        project = ProjectNamed("chromiumos/overlays/portage-stable")

        def _get_content(path, _commit):
            if path == "dev-util/pkg/OWNERS":
                return None
            elif path == "dev-util/OWNERS":
                return None
            elif path == "OWNERS":
                return "# Global owners.\nfoo@bar\n"
            else:
                raise AssertionError(f"Unhandled test path: {path}")

        self.content_mock.side_effect = _get_content
        self.file_mock.return_value = [
            DiffEntry(src_file="dev-util/pkg/pkg-0-r1.ebuild", status="A"),
        ]

        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNotNone(ret)

    def testCategoryOwners(self):
        """Allow OWNERS in categories."""
        project = ProjectNamed("chromiumos/overlays/portage-stable")

        def _get_content(path, _commit):
            if path == "dev-util/pkg/OWNERS":
                return None
            elif path == "dev-util/OWNERS":
                return "# Global owners.\nfoo@bar\n"
            else:
                raise AssertionError(f"Unhandled test path: {path}")

        self.content_mock.side_effect = _get_content
        self.file_mock.return_value = [
            DiffEntry(src_file="dev-util/pkg/pkg-0-r1.ebuild", status="A"),
        ]

        ret = pre_upload._check_ebuild_owners(project, "HEAD")
        self.assertIsNone(ret)


class CheckEbuildR0(PreUploadTestCase):
    """Tests for _check_ebuild_r0."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")

    def testNoMatches(self):
        """Handle no matching files."""
        self.file_mock.return_value = []
        ret = pre_upload._check_ebuild_r0(ProjectNamed("project"), "HEAD")
        self.assertIsNone(ret)

    def testBadEbuilds(self):
        """Handle matching r0 files."""
        self.file_mock.return_value = ["foo-1-r0.ebuild"]
        ret = pre_upload._check_ebuild_r0(ProjectNamed("project"), "HEAD")
        self.assertIsNotNone(ret)


class CheckEbuildVirtualPv(PreUploadTestCase):
    """Tests for _check_ebuild_virtual_pv."""

    PORTAGE_STABLE = ProjectNamed("chromiumos/overlays/portage-stable")
    CHROMEOS_OVERLAY = ProjectNamed("chromeos/overlays/chromeos-overlay")
    CHROMIUMOS_OVERLAY = ProjectNamed("chromiumos/overlays/chromiumos-overlay")
    BOARD_OVERLAY = ProjectNamed("chromiumos/overlays/board-overlays")
    PRIVATE_OVERLAY = ProjectNamed("chromeos/overlays/overlay-link-private")
    PRIVATE_VARIANT_OVERLAY = ProjectNamed(
        "chromeos/overlays/overlay-variant-daisy-spring-private"
    )

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")

    def testNoVirtuals(self):
        """Skip non virtual packages."""
        self.file_mock.return_value = ["some/package/package-3.ebuild"]
        ret = pre_upload._check_ebuild_virtual_pv(ProjectNamed("overlay"), "H")
        self.assertIsNone(ret)

    def testCommonVirtuals(self):
        """Non-board overlays should use PV=1."""
        template = "virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "1"]
        ret = pre_upload._check_ebuild_virtual_pv(self.CHROMIUMOS_OVERLAY, "H")
        self.assertIsNone(ret)

        self.file_mock.return_value = [template % "2"]
        ret = pre_upload._check_ebuild_virtual_pv(self.CHROMIUMOS_OVERLAY, "H")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testKnownUpstreamVirtuals(self):
        """Known upstream packages should be exempted from these checks."""
        self.file_mock.return_value = ["virtual/rust/rust-1.47.0-r6.ebuild"]
        ret = pre_upload._check_ebuild_virtual_pv(self.CHROMIUMOS_OVERLAY, "H")
        self.assertIsNone(ret)

    def testChromeVirtuals(self):
        """ChromeOS overlays should use PV=1.3."""
        template = "virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "1.3"]
        ret = pre_upload._check_ebuild_virtual_pv(self.CHROMEOS_OVERLAY, "H")
        self.assertIsNone(ret)

        self.file_mock.return_value = [template % "1.5"]
        ret = pre_upload._check_ebuild_virtual_pv(self.CHROMEOS_OVERLAY, "H")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testPublicBoardVirtuals(self):
        """Public board overlays should use PV=2."""
        template = "overlay-lumpy/virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "2"]
        ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, "H")
        self.assertIsNone(ret)

        self.file_mock.return_value = [template % "2.5"]
        ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, "H")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testPublicBoardVariantVirtuals(self):
        """Public board variant overlays should use PV=2.5."""
        template = "overlay-variant-lumpy-foo/virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "2.5"]
        ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, "H")
        self.assertIsNone(ret)

        self.file_mock.return_value = [template % "3"]
        ret = pre_upload._check_ebuild_virtual_pv(self.BOARD_OVERLAY, "H")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testPrivateBoardVirtuals(self):
        """Private board overlays should use PV=3."""
        template = "virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "3"]
        ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_OVERLAY, "H")
        self.assertIsNone(ret)

        self.file_mock.return_value = [template % "3.5"]
        ret = pre_upload._check_ebuild_virtual_pv(self.PRIVATE_OVERLAY, "H")
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testPrivateBoardVariantVirtuals(self):
        """Private board variant overlays should use PV=3.5."""
        template = "virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "3.5"]
        ret = pre_upload._check_ebuild_virtual_pv(
            self.PRIVATE_VARIANT_OVERLAY, "H"
        )
        self.assertIsNone(ret)

    def testSpecialVirtuals(self):
        """Some cases require deeper versioning and can be >= 4."""
        template = "virtual/foo/foo-%s.ebuild"
        self.file_mock.return_value = [template % "4"]
        ret = pre_upload._check_ebuild_virtual_pv(
            self.PRIVATE_VARIANT_OVERLAY, "H"
        )
        self.assertIsNone(ret)

        self.file_mock.return_value = [template % "4.5"]
        ret = pre_upload._check_ebuild_virtual_pv(
            self.PRIVATE_VARIANT_OVERLAY, "H"
        )
        self.assertIsNone(ret)


class CheckCrosLicenseCopyrightHeader(PreUploadTestCase):
    """Tests for _check_cros_license."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testNewFileYear(self):
        """Added files should have the current year in license header."""
        year = datetime.datetime.now().year
        HEADERS = (
            (
                "// Copyright 2016 The ChromiumOS Authors\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
            (
                "// Copyright %d The ChromiumOS Authors\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            )
            % year,
        )
        want_error = (True, False)

        def fake_get_affected_files(_, relative, include_adds=True):
            _ = relative
            if include_adds:
                return ["file"]
            else:
                return []

        self.file_mock.side_effect = fake_get_affected_files
        for i, header in enumerate(HEADERS):
            self.content_mock.return_value = header
            if want_error[i]:
                self.assertTrue(pre_upload._check_cros_license("proj", "sha1"))
            else:
                self.assertFalse(pre_upload._check_cros_license("proj", "sha1"))

    def testRejectC(self):
        """Reject the (c) in headers."""
        HEADERS = (
            (
                "// Copyright (c) 2015 The Chromium OS Authors. "
                "All rights reserved.\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
            (
                "// Copyright (c) 2020 The Chromium OS Authors. "
                "All rights reserved.\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
        )
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertTrue(pre_upload._check_cros_license("proj", "sha1"))

    def testRejectChromiumSpaceOs(self):
        """Reject Chromium OS in headers."""
        HEADERS = (
            (
                "// Copyright 2023 The Chromium OS Authors\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
            (
                "// Copyright 2030 The Chromium OS Authors\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
        )
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertTrue(pre_upload._check_cros_license("proj", "sha1"))

    def testRejectAllRightsReserved(self):
        """Reject all rights reserved in headers."""
        HEADERS = (
            (
                "// Copyright 2023 The ChromiumOS Authors. "
                "All rights reserved.\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
            (
                "// Copyright 2030 The ChromiumOS Authors. "
                "All rights reserved.\n"
                "// Use of this source code is governed by a BSD-style license "
                "that can be\n"
                "// found in the LICENSE file.\n"
            ),
        )
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertTrue(pre_upload._check_cros_license("proj", "sha1"))

    def testNoExcludedGolang(self):
        """Don't exclude .go files for license checks."""
        self.file_mock.return_value = ["foo/main.go"]
        self.content_mock.return_value = "package main\nfunc main() {}"
        self.assertTrue(pre_upload._check_cros_license("proj", "sha1"))

    def testIgnoreExcludedPaths(self):
        """Ignores excluded paths for license checks."""
        self.file_mock.return_value = ["foo/OWNERS"]
        self.content_mock.return_value = "owner@chromium.org"
        self.assertFalse(pre_upload._check_cros_license("proj", "sha1"))

    def testIgnoreMetadataFiles(self):
        """Ignores metadata files for license checks."""
        self.file_mock.return_value = ["foo/DIR_METADATA"]
        self.content_mock.return_value = 'team_email: "team@chromium.org"'
        self.assertFalse(pre_upload._check_cros_license("proj", "sha1"))

    def testIgnoreTopLevelExcludedPaths(self):
        """Ignores excluded paths for license checks."""
        self.file_mock.return_value = ["OWNERS"]
        self.content_mock.return_value = "owner@chromium.org"
        self.assertFalse(pre_upload._check_cros_license("proj", "sha1"))


class CheckAOSPLicenseCopyrightHeader(PreUploadTestCase):
    """Tests for _check_aosp_license."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testHeaders(self):
        """Accept old header styles."""
        HEADERS = (
            """//
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
            """#
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
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertIsNone(pre_upload._check_aosp_license("proj", "sha1"))

    def testRejectNoLinesAround(self):
        """Reject headers missing the empty lines before/after the license."""
        HEADERS = (
            """# Copyright (c) 2015 The Android Open Source Project
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
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertIsNotNone(pre_upload._check_aosp_license("proj", "sha1"))

    def testIgnoreExcludedPaths(self):
        """Ignores excluded paths for license checks."""
        self.file_mock.return_value = ["foo/OWNERS"]
        self.content_mock.return_value = "owner@chromium.org"
        self.assertIsNone(pre_upload._check_aosp_license("proj", "sha1"))

    def testIgnoreTopLevelExcludedPaths(self):
        """Ignores excluded paths for license checks."""
        self.file_mock.return_value = ["OWNERS"]
        self.content_mock.return_value = "owner@chromium.org"
        self.assertIsNone(pre_upload._check_aosp_license("proj", "sha1"))


class CheckEgisLicenseCopyrightHeader(cros_test_lib.MockTestCase):
    """Tests for _check_egis_license."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testHeaders(self):
        """Correct Egis Header."""
        HEADERS = (
            """/*
 * Copyright (c) 2015-2024 Egis Technology Inc.  <eservice@egistec.com>
 *
 * All rights are reserved.
 * Proprietary and confidential.
 * Unauthorized copying of this file, via any medium is strictly prohibited.
 * Any use is subject to an appropriate license granted by Egis Technology Inc.
 *
 */
""",
        )
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertFalse(pre_upload._check_egis_license("proj", "sha1"))

    def testRejectNoLinesAround(self):
        """Reject headers missing the empty lines before/after the license."""
        HEADERS = (
            """/*
 * Copyright (c) 2015-2024 Egis Technology Inc.  <eservice@egistec.com>
 * All rights are reserved.
 * Proprietary and confidential.
 * Unauthorized copying of this file, via any medium is strictly prohibited.
 * Any use is subject to an appropriate license granted by Egis Technology Inc.
 */
""",
        )
        self.file_mock.return_value = ["file"]
        for header in HEADERS:
            self.content_mock.return_value = header
            self.assertTrue(pre_upload._check_egis_license("proj", "sha1"))


class CheckLayoutConfTestCase(PreUploadTestCase):
    """Tests for _check_layout_conf."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def assertAccepted(
        self, files, project=ProjectNamed("project"), commit="fake sha1"
    ):
        """Assert _check_layout_conf accepts |files|."""
        self.file_mock.return_value = files
        ret = pre_upload._check_layout_conf(project, commit)
        self.assertIsNone(ret, msg="rejected with:\n%s" % ret)

    def assertRejected(
        self, files, project=ProjectNamed("project"), commit="fake sha1"
    ):
        """Assert _check_layout_conf rejects |files|."""
        self.file_mock.return_value = files
        ret = pre_upload._check_layout_conf(project, commit)
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def GetLayoutConf(self, filters=()):
        """Return a valid layout.conf with |filters| lines removed."""
        all_lines = [
            "cache-format = md5-dict",
            "eapis-banned = 0 1 2 3 4 5 6",
            "masters = portage-stable chromiumos eclass-overlay",  # nocheck
            "profile-formats = portage-2 profile-default-eapi",
            "profile_eapi_when_unspecified = 5-progress",
            "repo-name = link",
            "thin-manifests = true",
            "use-manifests = strict",
        ]

        lines = []
        for line in all_lines:
            for filt in filters:
                if line.startswith(filt):
                    break
            else:
                lines.append(line)

        return "\n".join(lines) + "\n"

    def testNoFilesToCheck(self):
        """Don't blow up when there are no layout.conf files."""
        self.assertAccepted([])

    def testRejectRepoNameFile(self):
        """If profiles/repo_name is set, kick it out."""
        self.assertRejected(["profiles/repo_name"])

    def testAcceptValidLayoutConf(self):
        """Accept a fully valid layout.conf."""
        self.content_mock.return_value = self.GetLayoutConf()
        self.assertAccepted(["metadata/layout.conf"])

    def testAcceptUnknownKeys(self):
        """Accept keys we don't explicitly know about."""
        self.content_mock.return_value = self.GetLayoutConf() + "zzz-top = ok\n"
        self.assertAccepted(["metadata/layout.conf"])

    def testRejectUnsorted(self):
        """Reject an unsorted layout.conf."""
        self.content_mock.return_value = (
            "zzz-top = bad\n" + self.GetLayoutConf()
        )
        self.assertRejected(["metadata/layout.conf"])

    def testRejectMissingEapisBanned(self):
        """Reject a layout.conf missing thin-manifests."""
        self.content_mock.return_value = self.GetLayoutConf(
            filters=["eapis-banned"]
        )
        self.assertRejected(["metadata/layout.conf"])

    def testRejectMissingThinManifests(self):
        """Reject a layout.conf missing thin-manifests."""
        self.content_mock.return_value = self.GetLayoutConf(
            filters=["thin-manifests"]
        )
        self.assertRejected(["metadata/layout.conf"])

    def testRejectMissingUseManifests(self):
        """Reject a layout.conf missing use-manifests."""
        self.content_mock.return_value = self.GetLayoutConf(
            filters=["use-manifests"]
        )
        self.assertRejected(["metadata/layout.conf"])

    def testRejectMissingEapiFallback(self):
        """Reject a layout.conf missing profile_eapi_when_unspecified."""
        self.content_mock.return_value = self.GetLayoutConf(
            filters=["profile_eapi_when_unspecified"]
        )
        self.assertRejected(["metadata/layout.conf"])

    def testRejectMissingRepoName(self):
        """Reject a layout.conf missing repo-name."""
        self.content_mock.return_value = self.GetLayoutConf(
            filters=["repo-name"]
        )
        self.assertRejected(["metadata/layout.conf"])


class CommitMessageTestCase(PreUploadTestCase):
    """Test case for funcs that check commit messages."""

    def setUp(self):
        self.msg_mock = self.PatchObject(pre_upload, "_get_commit_desc")

    @staticmethod
    def CheckMessage(_project, _commit):
        raise AssertionError("Test class must declare CheckMessage")
        # This return is to silence pylint warning W1111 so we don't have to
        # enable it for all the call sites below.
        return 1  # pylint: disable=unreachable

    def assertMessageAccepted(
        self, msg, project=ProjectNamed("project"), commit="1234"
    ):
        """Assert _check_change_has_bug_field accepts |msg|."""
        self.msg_mock.return_value = msg
        ret = self.CheckMessage(project, commit)
        self.assertIsNone(ret, f"Error: {ret}")

    def assertMessageRejected(
        self, msg, project=ProjectNamed("project"), commit="1234"
    ):
        """Assert _check_change_has_bug_field rejects |msg|."""
        self.msg_mock.return_value = msg
        ret = self.CheckMessage(project, commit)
        self.assertTrue(isinstance(ret, errors.HookFailure))


class CheckCommitMessageBug(CommitMessageTestCase):
    """Tests for _check_change_has_bug_field."""

    AOSP_PROJECT = pre_upload.Project(name="overlay", dir="", remote="aosp")
    CROS_PROJECT = pre_upload.Project(name="overlay", dir="", remote="cros")

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_has_bug_field(project, commit)

    def testNormal(self):
        """Accept a commit message w/a valid BUG."""
        self.assertMessageAccepted("\nBUG=chromium:1234\n", self.CROS_PROJECT)
        self.assertMessageAccepted("\nBUG=b:1234\n", self.CROS_PROJECT)
        self.assertMessageAccepted("\nBUG=oss-fuzz:1234\n", self.CROS_PROJECT)

        self.assertMessageAccepted("\nBug: 1234\n", self.AOSP_PROJECT)
        self.assertMessageAccepted("\nBug:1234\n", self.AOSP_PROJECT)

    def testNormalFixed(self):
        """Accept a commit message w/a valid FIXED."""
        self.assertMessageAccepted("\nFIXED=chromium:1234\n", self.CROS_PROJECT)
        self.assertMessageAccepted("\nFIXED=b:1234\n", self.CROS_PROJECT)
        self.assertMessageAccepted("\nFIXED=oss-fuzz:1234\n", self.CROS_PROJECT)

        self.assertMessageAccepted("\nFixed: 1234\n", self.AOSP_PROJECT)
        self.assertMessageAccepted("\nFixed:1234\n", self.AOSP_PROJECT)

    def testNone(self):
        """Accept BUG=None."""
        self.assertMessageAccepted("\nBUG=None\n", self.CROS_PROJECT)
        self.assertMessageAccepted("\nBUG=none\n", self.CROS_PROJECT)
        self.assertMessageAccepted("\nBug: None\n", self.AOSP_PROJECT)
        self.assertMessageAccepted("\nBug:none\n", self.AOSP_PROJECT)

        for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
            self.assertMessageRejected("\nBUG=NONE\n", project)

    def testFixedNone(self):
        """Reject FIXED=None."""
        self.assertMessageRejected("\nFIXED=None\n", self.CROS_PROJECT)
        self.assertMessageRejected("\nFIXED=none\n", self.CROS_PROJECT)
        self.assertMessageRejected("\nFixed: None\n", self.AOSP_PROJECT)
        self.assertMessageRejected("\nFixed:none\n", self.AOSP_PROJECT)

        for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
            self.assertMessageRejected("\nFIXED=NONE\n", project)

    def testBlank(self):
        """Reject blank values."""
        for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
            self.assertMessageRejected("\nBUG=\n", project)
            self.assertMessageRejected("\nBUG=    \n", project)
            self.assertMessageRejected("\nBug:\n", project)
            self.assertMessageRejected("\nBug:    \n", project)

    def testNotFirstLine(self):
        """Reject the first line."""
        for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
            self.assertMessageRejected("BUG=None\n\n\n", project)

    def testNotInline(self):
        """Reject not at the start of line."""
        for project in (self.AOSP_PROJECT, self.CROS_PROJECT):
            self.assertMessageRejected("\n BUG=None\n", project)
            self.assertMessageRejected("\n\tBUG=None\n", project)

    def testOldTrackers(self):
        """Reject commit messages using old trackers."""
        self.assertMessageRejected(
            "\nBUG=chrome-os-partner:1234\n", self.CROS_PROJECT
        )
        self.assertMessageRejected(
            "\nBUG=chromium-os:1234\n", self.CROS_PROJECT
        )

    def testNoTrackers(self):
        """Reject commit messages w/invalid trackers."""
        self.assertMessageRejected("\nBUG=booga:1234\n", self.CROS_PROJECT)
        self.assertMessageRejected("\nBUG=br:1234\n", self.CROS_PROJECT)

    def testMissing(self):
        """Reject commit messages w/no BUG line."""
        self.assertMessageRejected("foo\n")

    def testCase(self):
        """Reject bug lines that are not BUG."""
        self.assertMessageRejected("\nbug=none\n")

    def testNotAfterTest(self):
        """Reject any TEST line before any BUG line."""
        middle_field = "A random between line\n"
        for project, bug_field, test_field in (
            (self.AOSP_PROJECT, "Bug:none\n", "Test: i did not do it\n"),
            (self.CROS_PROJECT, "BUG=None\n", "TEST=i did not do it\n"),
        ):
            self.assertMessageRejected(
                "\n" + test_field + middle_field + bug_field, project
            )
            self.assertMessageRejected("\n" + test_field + bug_field, project)


class CheckCommitMessageBranch(CommitMessageTestCase):
    """Tests for branch_check."""

    def setUp(self):
        self.options = []

    def CheckMessage(self, project, commit):  # pylint: disable=arguments-differ
        return pre_upload._check_change_has_branch_field(
            project,
            commit,
            options=self.options,
        )

    def testBranchRequiredOk(self):
        """Test that, when BRANCH= is required, BRANCH=none is accepted."""
        self.assertMessageAccepted("\nBRANCH=none\n")

    def testBranchRequiredMissing(self):
        """Test that, when BRANCH= is required and missing, it's rejected."""
        self.assertMessageRejected("\n")

    def testBranchOptionalOk(self):
        """Test that, when BRANCH= is optional, it's accepted."""
        self.options = ["--optional"]
        self.assertMessageAccepted("\n")
        self.assertMessageAccepted("\nBRANCH=link,snow\n")

    def testBranchOptionalNone(self):
        """Test that, when BRANCH= is optional, BRANCH=none is rejected."""
        self.options = ["--optional"]
        self.assertMessageRejected("\nBRANCH=None\n")


class CheckCommitMessageCqDepend(CommitMessageTestCase):
    """Tests for _check_change_has_valid_cq_depend."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_has_valid_cq_depend(project, commit)

    def testNormal(self):
        """Accept valid Cq-Depends line."""
        self.assertMessageAccepted(
            "\nCq-Depend: chromium:1234\nChange-Id: I123"
        )

    def testInvalid(self):
        """Reject invalid Cq-Depends line."""
        self.assertMessageRejected("\nCq-Depend=chromium=1234\n")
        self.assertMessageRejected("\nCq-Depend: None\n")
        self.assertMessageRejected(
            "\nCq-Depend: chromium:1234\n\nChange-Id: I123"
        )
        self.assertMessageRejected("\nCQ-DEPEND=1234\n")


class CheckCommitMessageContribution(CommitMessageTestCase):
    """Tests for _check_change_is_contribution."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_is_contribution(project, commit)

    def testNormal(self):
        """Accept a commit message which is a contribution."""
        self.assertMessageAccepted("\nThis is cool code I am contributing.\n")

    def testFailureLowerCase(self):
        """Deny a commit message which is not a contribution."""
        self.assertMessageRejected("\nThis is not a contribution.\n")

    def testFailureUpperCase(self):
        """Deny a commit message which is not a contribution."""
        self.assertMessageRejected("\nNOT A CONTRIBUTION\n")


class CheckCommitMessageTest(CommitMessageTestCase):
    """Tests for _check_change_has_test_field."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_has_test_field(project, commit)

    def testNormal(self):
        """Accept a commit message w/a valid TEST."""
        self.assertMessageAccepted("\nTEST=i did it\n")

    def testNone(self):
        """Accept TEST=None."""
        self.assertMessageAccepted("\nTEST=None\n")
        self.assertMessageAccepted("\nTEST=none\n")

    def testBlank(self):
        """Reject blank values."""
        self.assertMessageRejected("\nTEST=\n")
        self.assertMessageRejected("\nTEST=     \n")

    def testNotFirstLine(self):
        """Reject the first line."""
        self.assertMessageRejected("TEST=None\n\n\n")

    def testNotInline(self):
        """Reject not at the start of line."""
        self.assertMessageRejected("\n TEST=None\n")
        self.assertMessageRejected("\n\tTEST=None\n")

    def testMissing(self):
        """Reject commit messages w/no TEST line."""
        self.assertMessageRejected("foo\n")

    def testCase(self):
        """Reject bug lines that are not TEST."""
        self.assertMessageRejected("\ntest=none\n")


class CheckCommitMessageChangeId(CommitMessageTestCase):
    """Tests for _check_change_has_proper_changeid."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_has_proper_changeid(project, commit)

    def testNormal(self):
        """Accept a commit message w/a valid Change-Id."""
        self.assertMessageAccepted("foo\n\nChange-Id: I1234\n")

    def testBlank(self):
        """Reject blank values."""
        self.assertMessageRejected("\nChange-Id:\n")
        self.assertMessageRejected("\nChange-Id:       \n")

    def testNotFirstLine(self):
        """Reject the first line."""
        self.assertMessageRejected("TEST=None\n\n\n")

    def testNotInline(self):
        """Reject not at the start of line."""
        self.assertMessageRejected("\n Change-Id: I1234\n")
        self.assertMessageRejected("\n\tChange-Id: I1234\n")

    def testMissing(self):
        """Reject commit messages missing the line."""
        self.assertMessageRejected("foo\n")

    def testCase(self):
        """Reject bug lines that are not Change-Id."""
        self.assertMessageRejected("\nchange-id: I1234\n")
        self.assertMessageRejected("\nChange-id: I1234\n")
        self.assertMessageRejected("\nChange-ID: I1234\n")

    def testEnd(self):
        """Reject Change-Id's that are not last."""
        self.assertMessageRejected("\nChange-Id: I1234\nbar\n")

    def testSobTag(self):
        """Permit s-o-b tags to follow the Change-Id."""
        self.assertMessageAccepted(
            "foo\n\nChange-Id: I1234\nSigned-off-by: Hi\n"
        )

    def testCqClTag(self):
        """Permit Cq-Cl-Tag tags to follow the Change-Id."""
        self.assertMessageAccepted("foo\n\nChange-Id: I1234\nCq-Cl-Tag: Hi\n")

    def testCqIncludeTrybotsTag(self):
        """Permit Cq-Include-Trybots tags to follow the Change-Id."""
        self.assertMessageAccepted(
            "foo\n\nChange-Id: I1234\nCq-Include-Trybots: chromeos/cq:foo\n"
        )


class CheckCommitMessageNoOEM(CommitMessageTestCase):
    """Tests for _check_change_no_include_oem."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_no_include_oem(project, commit)

    def testNormal(self):
        """Accept a commit message w/o reference to an OEM/ODM."""
        self.assertMessageAccepted("foo\n")

    def testHasOEM(self):
        """Catch commit messages referencing OEMs."""
        self.assertMessageRejected("HP Project\n\n")
        self.assertMessageRejected("hewlett-packard\n")
        self.assertMessageRejected("Hewlett\nPackard\n")
        self.assertMessageRejected("Dell Chromebook\n\n\n")
        self.assertMessageRejected("product@acer.com\n")
        self.assertMessageRejected("This is related to Asus\n")
        self.assertMessageRejected("lenovo machine\n")

    def testHasODM(self):
        """Catch commit messages referencing ODMs."""
        self.assertMessageRejected("new samsung laptop\n\n")
        self.assertMessageRejected("pegatron(ems) project\n")
        self.assertMessageRejected("new Wistron device\n")

    def testContainsOEM(self):
        """Check that the check handles word boundaries properly."""
        self.assertMessageAccepted("oheahpohea")
        self.assertMessageAccepted("Play an Asus7 guitar chord.\n\n")

    def testTag(self):
        """Check that the check ignores tags."""
        self.assertMessageAccepted(
            "Harmless project\n"
            "Reviewed-by: partner@asus.corp-partner.google.com\n"
            "Tested-by: partner@hp.corp-partner.google.com\n"
            "Signed-off-by: partner@dell.corp-partner.google.com\n"
            "Commit-Queue: partner@lenovo.corp-partner.google.com\n"
            "CC: partner@acer.corp-partner.google.com\n"
            "Acked-by: partner@hewlett-packard.corp-partner.google.com\n"
        )
        self.assertMessageRejected(
            "Asus project\nReviewed-by: partner@asus.corp-partner.google.com"
        )
        self.assertMessageRejected(
            "my project\nBad-tag: partner@asus.corp-partner.google.com"
        )


class CheckCommitMessageNoBoardPhase(CommitMessageTestCase):
    """Tests for _check_change_no_include_board_phase."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_change_no_include_board_phase(project, commit)

    def testNormal(self):
        """Accept a commit message w/o reference to a board phase."""
        self.assertMessageAccepted("foo\n")

    def testContainsBoardPhase(self):
        """Catch commit messages referencing board phase."""
        self.assertMessageAccepted("proto")
        self.assertMessageRejected("proTo33")
        self.assertMessageRejected("evt")
        self.assertMessageRejected("DVT\n")
        self.assertMessageRejected("PvT")
        self.assertMessageAccepted("mp")
        self.assertMessageAccepted("hci_sync_conn_complete_evt")
        self.assertMessageAccepted("skip virtio_net_hdr_set_proto")


class CheckCommitMessageStyle(CommitMessageTestCase):
    """Tests for _check_commit_message_style."""

    @staticmethod
    def CheckMessage(project, commit):
        return pre_upload._check_commit_message_style(project, commit)

    def testNormal(self):
        """Accept valid commit messages."""
        self.assertMessageAccepted("one sentence.\n")
        self.assertMessageAccepted("some.module: do it!\n")
        self.assertMessageAccepted("one line\n\nmore stuff here.")
        self.assertMessageAccepted("command ... --with-ellipsis")
        self.assertMessageAccepted("one function().")

    def testNoBlankSecondLine(self):
        """Reject messages that have stuff on the second line."""
        self.assertMessageRejected("one sentence.\nbad fish!\n")

    def testFirstLineMultipleSentences(self):
        """Reject messages that have more than one sentence in the summary."""
        self.assertMessageRejected("one sentence. two sentence!\n")
        self.assertMessageRejected("one function(). two sentence!\n")

    def testFirstLineTooLone(self):
        """Reject first lines that are too long."""
        self.assertMessageRejected("o" * 200)

    def testGitKeywords(self):
        """Reject git special keywords."""
        BAD_MESSAGES = (
            "fixup!",
            "fixup! that old commit",
            "squash!",
            "squash! that old commit",
        )
        GOOD_MESSAGES = (
            "this change needs a fixup!",
            "you should eat your squash!",
        )
        for msg in BAD_MESSAGES:
            self.assertMessageRejected(msg)
        for msg in GOOD_MESSAGES:
            self.assertMessageAccepted(msg)


class CheckClSizeTest(PreUploadTestCase):
    """Tests for _check_cl_size."""

    MOCK_FILE_CONTENT = {
        "large.cc": [(i, "a") for i in range(300)],
        "small.cc": [(i, "a") for i in range(200)],
        "generated.cfg": [(i, "a") for i in range(300)],
    }

    @classmethod
    def _get_file_diff(cls, path, _commit):
        return cls.MOCK_FILE_CONTENT[path]

    def _run_check(self, files):
        self.PatchObject(pre_upload, "_get_affected_files", return_value=files)
        self.PatchObject(pre_upload, "_get_file_diff", new=self._get_file_diff)
        warning = pre_upload._check_cl_size(ProjectNamed("PROJECT"), "COMMIT")
        return warning

    def test_small_cl(self):
        warning = self._run_check(["small.cc"])
        self.assertIsNone(warning)

    def test_large_cl(self):
        warning = self._run_check(["small.cc", "large.cc"])
        self.assertTrue(warning)
        self.assertIsInstance(warning, errors.HookWarning)
        self.assertTrue(
            warning.msg.startswith(
                "Large CLs are often more error-prone and take longer to review"
            )
        )

    def test_large_generated_cl(self):
        warning = self._run_check(["small.cc", "generated.cfg"])
        self.assertIsNone(warning)


def DiffEntry(
    src_file=None, dst_file=None, src_mode=None, dst_mode="100644", status="M"
):
    """Helper to create a stub RawDiffEntry object"""
    if src_mode is None:
        if status == "A":
            src_mode = "000000"
        elif status == "M":
            src_mode = dst_mode
        elif status == "D":
            src_mode = dst_mode
            dst_mode = "000000"

    src_sha = dst_sha = "abc"
    if status == "D":
        dst_sha = "000000"
    elif status == "A":
        src_sha = "000000"

    return git.RawDiffEntry(
        src_mode=src_mode,
        dst_mode=dst_mode,
        src_sha=src_sha,
        dst_sha=dst_sha,
        status=status,
        score=None,
        src_file=src_file,
        dst_file=dst_file,
    )


class HelpersTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Various tests for utility functions."""

    def setUp(self):
        os.chdir(self.tempdir)

        self.PatchObject(
            git,
            "RawDiff",
            return_value=[
                # A modified normal file.
                DiffEntry(src_file="buildbot/constants.py", status="M"),
                # A new symlink file.
                DiffEntry(
                    dst_file="scripts/cros_env_allowlist",
                    dst_mode="120000",
                    status="A",
                ),
                # A deleted file.
                DiffEntry(src_file="scripts/sync_sonic.py", status="D"),
            ],
        )

    def _WritePresubmitIgnoreFile(self, subdir, data):
        """Writes to a .presubmitignore file in the passed-in subdirectory."""
        osutils.WriteFile(
            self.tempdir / subdir / pre_upload._IGNORE_FILE, data, makedirs=True
        )

    def testGetAffectedFilesNoDeletesNoRelative(self):
        """Verify _get_affected_files() works w/no delete & not relative."""
        path = Path.cwd()
        files = pre_upload._get_affected_files(
            "HEAD", include_deletes=False, relative=False
        )
        exp_files = [os.path.join(path, "buildbot/constants.py")]
        self.assertEqual(files, exp_files)

    def testGetAffectedFilesDeletesNoRelative(self):
        """Verify _get_affected_files() works w/delete & not relative."""
        path = Path.cwd()
        files = pre_upload._get_affected_files(
            "HEAD", include_deletes=True, relative=False
        )
        exp_files = [
            os.path.join(path, "buildbot/constants.py"),
            os.path.join(path, "scripts/sync_sonic.py"),
        ]
        self.assertEqual(files, exp_files)

    def testGetAffectedFilesNoDeletesRelative(self):
        """Verify _get_affected_files() works w/no delete & relative."""
        files = pre_upload._get_affected_files(
            "HEAD", include_deletes=False, relative=True
        )
        exp_files = ["buildbot/constants.py"]
        self.assertEqual(files, exp_files)

    def testGetAffectedFilesDeletesRelative(self):
        """Verify _get_affected_files() works w/delete & relative."""
        files = pre_upload._get_affected_files(
            "HEAD", include_deletes=True, relative=True
        )
        exp_files = ["buildbot/constants.py", "scripts/sync_sonic.py"]
        self.assertEqual(files, exp_files)

    def testGetAffectedFilesDetails(self):
        """Verify _get_affected_files() works w/full_details."""
        files = pre_upload._get_affected_files(
            "HEAD", full_details=True, relative=True
        )
        self.assertEqual(files[0].src_file, "buildbot/constants.py")

    def testGetAffectedFilesPresubmitIgnoreDirectory(self):
        """Verify .presubmitignore can be used to exclude a directory."""
        self._WritePresubmitIgnoreFile(".", "buildbot/")
        self.assertEqual(
            pre_upload._get_affected_files("HEAD", relative=True), []
        )

    def testGetAffectedFilesPresubmitIgnoreDirectoryWildcard(self):
        """Verify .presubmitignore can be used with a directory wildcard."""
        self._WritePresubmitIgnoreFile(".", "*/constants.py")
        self.assertEqual(
            pre_upload._get_affected_files("HEAD", relative=True), []
        )

    def testGetAffectedFilesPresubmitIgnoreWithinDirectory(self):
        """Verify .presubmitignore can be placed in a subdirectory."""
        self._WritePresubmitIgnoreFile("buildbot", "*.py")
        self.assertEqual(
            pre_upload._get_affected_files("HEAD", relative=True), []
        )

    def testGetAffectedFilesPresubmitIgnoreDoesntMatch(self):
        """Verify .presubmitignore has no effect when it doesn't match."""
        self._WritePresubmitIgnoreFile("buildbot", "*.txt")
        self.assertEqual(
            pre_upload._get_affected_files("HEAD", relative=True),
            ["buildbot/constants.py"],
        )

    def testGetAffectedFilesPresubmitIgnoreAddedFile(self):
        """Verify .presubmitignore matches added files."""
        self._WritePresubmitIgnoreFile(".", "buildbot/\nscripts/")
        self.assertEqual(
            pre_upload._get_affected_files(
                "HEAD", relative=True, include_symlinks=True
            ),
            [],
        )

    def testGetAffectedFilesPresubmitIgnoreSkipIgnoreFile(self):
        """Verify .presubmitignore files are automatically skipped."""
        self.PatchObject(
            git,
            "RawDiff",
            return_value=[DiffEntry(src_file=".presubmitignore", status="M")],
        )
        self.assertEqual(
            pre_upload._get_affected_files("HEAD", relative=True), []
        )


class CrosWorkonRevbumpEclassTest(PreUploadTestCase):
    """Tests for _check_cros_workon_revbump_eclass."""

    def setUp(self):
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.bq_mock = self.PatchObject(build_query, "Overlay")
        self.project = ProjectNamed("PROJECT")

    def _constructEbuilds(self, attrs):
        """Construct .ebuild attribute for Overlay().

        Construct a .ebuilds attribute that holds a list of Ebuild mock
        objects with .package_info.cp and .eclasses attributes.
        """
        Attr = cros_test_lib.EasyAttr
        ebuilds = []
        for x in attrs:
            ebuilds.append(Attr(package_info=Attr(cp=x[0]), eclasses=x[1]))
        return Attr(ebuilds=ebuilds)

    def testRevbumpAllPkgs(self):
        """All packages revbumped along with eclass."""
        self.file_mock.return_value = [
            "eclass/base.eclass",
            "category/pkg1/files/revision_bump",
            "category/pkg2/pkg2.ebuild",
        ]
        self.bq_mock.return_value = self._constructEbuilds(
            attrs=[
                ("category/pkg1", ["base"]),
                ("category/pkg2", ["base", "bar"]),
                ("foo/bar", ["bar"]),
            ]
        )
        result = pre_upload._check_cros_workon_revbump_eclass(
            self.project,
            "COMMIT",
            options=("base.eclass",),
        )
        self.assertFalse(result)

    def testRevbumpSomePkgs(self):
        """Some packages didn't revbump along with eclass."""
        self.file_mock.return_value = [
            "eclass/base.eclass",
            "category/pkg1/files/revision_bump",
            "category/pkg2-with-suffix/files/revision_bump",
        ]
        self.bq_mock.return_value = self._constructEbuilds(
            attrs=[
                ("category/pkg1", ["base"]),
                ("category/pkg2", ["base", "bar"]),
                ("foo/bar", ["bar"]),
            ]
        )
        result = pre_upload._check_cros_workon_revbump_eclass(
            self.project,
            "COMMIT",
            options=("base.eclass",),
        )
        self.assertEqual(len(result), 1)
        self.assertTrue("base.eclass" in result[0].msg)
        self.assertEqual(result[0].items, ["category/pkg2"])

    def testRevbumpNoPkgs(self):
        """No packages revbumped along with eclass."""
        self.file_mock.return_value = [
            "eclass/base.eclass",
        ]
        self.bq_mock.return_value = self._constructEbuilds(
            attrs=[
                ("category/pkg1", ["base"]),
                ("category/pkg2", ["base", "bar"]),
                ("foo/bar", ["bar"]),
            ]
        )
        result = pre_upload._check_cros_workon_revbump_eclass(
            self.project,
            "COMMIT",
            options=("base.eclass",),
        )
        self.assertEqual(len(result), 1)
        self.assertTrue("base.eclass" in result[0].msg)
        self.assertEqual(result[0].items, ["category/pkg1", "category/pkg2"])

    def testMultiEclass(self):
        """Multiple eclasses were modified in one commit."""
        self.file_mock.return_value = [
            "eclass/one.eclass",
            "eclass/two.eclass",
            "eclass/three.eclass",
        ]
        self.bq_mock.return_value = self._constructEbuilds(
            attrs=[
                ("category/pkg1", ["one"]),
                ("category/pkg2", ["two", "three"]),
            ]
        )
        result = pre_upload._check_cros_workon_revbump_eclass(
            self.project,
            "COMMIT",
            options=("one.eclass", "two.eclass"),
        )
        self.assertEqual(len(result), 2)
        self.assertTrue("one.eclass" in result[0].msg)
        self.assertEqual(result[0].items, ["category/pkg1"])
        self.assertTrue("two.eclass" in result[1].msg)
        self.assertEqual(result[1].items, ["category/pkg2"])

    def testNoMatchedEclass(self):
        """No matched eclass was modified."""
        self.file_mock.return_value = [
            "eclass/one.eclass",
            "eclass/two.eclass",
        ]
        self.bq_mock.return_value = self._constructEbuilds(
            attrs=[
                ("category/pkg1", ["one"]),
                ("category/pkg2", ["two", "three"]),
            ]
        )
        result = pre_upload._check_cros_workon_revbump_eclass(
            self.project,
            "COMMIT",
            options=("foo.eclass", "bar.eclass"),
        )
        self.assertFalse(result)

    def testNoEclass(self):
        """No eclass was provided in hook override options."""
        self.file_mock.return_value = [
            "eclass/one.eclass",
            "eclass/two.eclass",
        ]
        self.bq_mock.return_value = self._constructEbuilds(
            attrs=[
                ("category/pkg1", ["one"]),
                ("category/pkg2", ["two", "three"]),
            ]
        )
        result = pre_upload._check_cros_workon_revbump_eclass(
            self.project,
            "COMMIT",
            options=(),
        )
        self.assertFalse(result)


class ExecFilesTest(PreUploadTestCase):
    """Tests for _check_exec_files."""

    def setUp(self):
        self.diff_mock = self.PatchObject(git, "RawDiff")

    def testNotExec(self):
        """Do not flag files that are not executable."""
        self.diff_mock.return_value = [
            DiffEntry(src_file="make.conf", dst_mode="100644", status="A"),
        ]
        self.assertIsNone(pre_upload._check_exec_files("proj", "commit"))

    def testExec(self):
        """Flag files that are executable."""
        self.diff_mock.return_value = [
            DiffEntry(src_file="make.conf", dst_mode="100755", status="A"),
        ]
        self.assertIsNotNone(pre_upload._check_exec_files("proj", "commit"))

    def testDeletedExec(self):
        """Ignore bad files that are being deleted."""
        self.diff_mock.return_value = [
            DiffEntry(src_file="make.conf", dst_mode="100755", status="D"),
        ]
        self.assertIsNone(pre_upload._check_exec_files("proj", "commit"))

    def testModifiedExec(self):
        """Flag bad files that weren't exec, but now are."""
        self.diff_mock.return_value = [
            DiffEntry(
                src_file="make.conf",
                src_mode="100644",
                dst_mode="100755",
                status="M",
            ),
        ]
        self.assertIsNotNone(pre_upload._check_exec_files("proj", "commit"))

    def testNormalExec(self):
        """Don't flag normal files (e.g. scripts) that are executable."""
        self.diff_mock.return_value = [
            DiffEntry(src_file="foo.sh", dst_mode="100755", status="A"),
        ]
        self.assertIsNone(pre_upload._check_exec_files("proj", "commit"))


class CheckForUprev(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Tests for _check_for_uprev."""

    def setUp(self):
        self.file_mock = self.PatchObject(git, "RawDiff")

    def _Files(self, files):
        """Create |files| in the tempdir and return full paths to them."""
        for obj in files:
            if obj.status == "D":
                continue
            if obj.dst_file is None:
                f = obj.src_file
            else:
                f = obj.dst_file
            osutils.Touch(self.tempdir / f, makedirs=True)
        return files

    def assertAccepted(self, files, project="project", commit="fake sha1"):
        """Assert _check_for_uprev accepts |files|."""
        self.file_mock.return_value = self._Files(files)
        ret = pre_upload._check_for_uprev(
            ProjectNamed(project), commit, project_top=self.tempdir
        )
        self.assertIsNone(ret)

    def assertRejected(self, files, project="project", commit="fake sha1"):
        """Assert _check_for_uprev rejects |files|."""
        self.file_mock.return_value = self._Files(files)
        ret = pre_upload._check_for_uprev(
            ProjectNamed(project), commit, project_top=self.tempdir
        )
        self.assertTrue(isinstance(ret, errors.HookFailure))

    def testAllowlistFiles(self):
        """Skip checks on allowed files."""
        files = ["ChangeLog", "Manifest", "metadata.xml"]
        self.assertAccepted(
            [
                DiffEntry(src_file=os.path.join("c", "p", x), status="M")
                for x in files
            ]
        )

    def testRejectBasic(self):
        """Reject ebuilds missing uprevs."""
        self.assertRejected([DiffEntry(src_file="c/p/p-0.ebuild", status="M")])

    def testRejectBasicRevision(self):
        """Reject -rN ebuilds missing uprevs."""
        self.assertRejected(
            [DiffEntry(src_file="c/p/p-0-r1.ebuild", status="M")]
        )

    def testNewPackage(self):
        """Accept new ebuilds w/out uprevs."""
        self.assertAccepted([DiffEntry(src_file="c/p/p-0.ebuild", status="A")])
        self.assertAccepted(
            [DiffEntry(src_file="c/p/p-0-r12.ebuild", status="A")]
        )

    def testNewPackageNoSymlink(self):
        """Accept new ebuilds w/out symlinks."""
        self.assertAccepted([DiffEntry(src_file="c/p/p-0.ebuild", status="A")])

    def testUprevNoSymlink(self):
        """Accept uprev'd ebuilds w/out symlinks."""
        self.assertAccepted(
            [
                DiffEntry(
                    src_file="c/p/p-0.ebuild",
                    src_mode="120000",
                    dst_file="c/p/p-1.ebuild",
                    dst_mode="120000",
                    status="R",
                )
            ]
        )

    def testModifiedFilesOnly(self):
        """Reject ebuilds w/out uprevs and changes in files/."""
        osutils.Touch(self.tempdir / "cat/pkg/pkg-0.ebuild", makedirs=True)
        self.assertRejected([DiffEntry(src_file="cat/pkg/files/f", status="A")])
        self.assertRejected([DiffEntry(src_file="cat/pkg/files/g", status="M")])

    def testFilesNoEbuilds(self):
        """Ignore changes to paths w/out ebuilds."""
        self.assertAccepted([DiffEntry(src_file="cat/pkg/files/f", status="A")])
        self.assertAccepted([DiffEntry(src_file="cat/pkg/files/g", status="M")])

    def testModifiedFilesWithUprev(self):
        """Accept ebuilds w/uprevs and changes in files/."""
        self.assertAccepted(
            [
                DiffEntry(src_file="c/p/files/f", status="A"),
                DiffEntry(src_file="c/p/p-0.ebuild", status="A"),
            ]
        )
        self.assertAccepted(
            [
                DiffEntry(src_file="c/p/files/f", status="M"),
                DiffEntry(
                    src_file="c/p/p-0-r1.ebuild",
                    src_mode="120000",
                    dst_file="c/p/p-0-r2.ebuild",
                    dst_mode="120000",
                    status="R",
                ),
            ]
        )

    def testModifiedFilesWith9999(self):
        """Accept 9999 ebuilds and changes in files/."""
        self.assertAccepted(
            [
                DiffEntry(src_file="c/p/files/f", status="M"),
                DiffEntry(src_file="c/p/p-9999.ebuild", status="M"),
            ]
        )

    def testModifiedFilesIn9999SubDirWithout9999Change(self):
        """Accept changes in files/ with a parent 9999 ebuild"""
        ebuild_9999_file = self.tempdir / "c/p/p-9999.ebuild"
        osutils.WriteFile(ebuild_9999_file, "fake", makedirs=True)
        self.assertAccepted([DiffEntry(src_file="c/p/files/f", status="M")])

    def testModifiedFilesIn9999SubDirsWithout9999Change(self):
        """Accept changes in multiple files/ with parent 9999 ebuilds"""
        EBUILDS = (
            self.tempdir / "c/p/p-9999.ebuild",
            self.tempdir / "c/p2/p2-9999.ebuild",
        )
        for ebuild_9999_file in EBUILDS:
            osutils.WriteFile(ebuild_9999_file, "fake", makedirs=True)
        self.assertAccepted(
            [
                DiffEntry(src_file="c/p/files/f", status="M"),
                DiffEntry(src_file="c/p2/files/g", status="M"),
            ]
        )

    def testModifiedFilesAndProfilesWith9999(self):
        """Accept changes in files/ with a 9999 ebuild and profile change"""
        ebuild_9999_file = self.tempdir / "c/p/p-9999.ebuild"
        os.makedirs(ebuild_9999_file.parent)
        osutils.WriteFile(ebuild_9999_file, "fake")
        self.assertAccepted(
            [
                DiffEntry(src_file="c/p/files/f", status="M"),
                DiffEntry(src_file="c/profiles/base/make.defaults", status="M"),
            ]
        )


class DirectMainTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Tests for direct_main()"""

    def setUp(self):
        self.hooks_mock = self.PatchObject(
            pre_upload, "_run_project_hooks", return_value=None
        )

    def testNoArgs(self):
        """If run w/no args, should check the current dir."""
        ret = pre_upload.direct_main([])
        self.assertEqual(ret, 0)
        self.hooks_mock.assert_called_once_with(
            mock.ANY,
            proj_dir=str(Path.cwd()),
            commit_list=[],
            presubmit=mock.ANY,
            config_file=mock.ANY,
            jobs=mock.ANY,
        )

    def testExplicitDir(self):
        """Verify we can run on a diff dir."""
        # Use the chromite dir since we know it exists.
        ret = pre_upload.direct_main(["--dir", str(constants.CHROMITE_DIR)])
        self.assertEqual(ret, 0)
        self.hooks_mock.assert_called_once_with(
            mock.ANY,
            proj_dir=str(constants.CHROMITE_DIR),
            commit_list=[],
            presubmit=mock.ANY,
            config_file=mock.ANY,
            jobs=mock.ANY,
        )

    def testBogusProject(self):
        """A bogus project name should be fine (use default settings)."""
        # Use the chromite dir since we know it exists.
        ret = pre_upload.direct_main(
            ["--dir", str(constants.CHROMITE_DIR), "--project", "foooooooooo"]
        )
        self.assertEqual(ret, 0)
        self.hooks_mock.assert_called_once_with(
            "foooooooooo",
            proj_dir=str(constants.CHROMITE_DIR),
            commit_list=[],
            presubmit=mock.ANY,
            config_file=mock.ANY,
            jobs=mock.ANY,
        )

    def testBogusProjectNoDir(self):
        """Make sure --dir is detected even with --project."""
        ret = pre_upload.direct_main(["--project", "foooooooooo"])
        self.assertEqual(ret, 0)
        self.hooks_mock.assert_called_once_with(
            "foooooooooo",
            proj_dir=str(DIR),
            commit_list=[],
            presubmit=mock.ANY,
            config_file=mock.ANY,
            jobs=mock.ANY,
        )

    def testNoGitDir(self):
        """We should die when run on a non-git dir."""
        self.assertRaises(
            pre_upload.BadInvocation,
            pre_upload.direct_main,
            ["--dir", str(self.tempdir)],
        )

    def testNoDir(self):
        """We should die when run on a missing dir."""
        self.assertRaises(
            pre_upload.BadInvocation,
            pre_upload.direct_main,
            ["--dir", str(self.tempdir / "foooooooo")],
        )

    def testCommitList(self):
        """Any args on the command line should be treated as commits."""
        commits = ["sha1", "sha2", "shaaaaaaaaaaaan"]
        ret = pre_upload.direct_main(commits)
        self.assertEqual(ret, 0)
        self.hooks_mock.assert_called_once_with(
            mock.ANY,
            proj_dir=mock.ANY,
            commit_list=commits,
            presubmit=mock.ANY,
            config_file=mock.ANY,
            jobs=mock.ANY,
        )

    def testJobs(self):
        """--jobs flag should be passed through."""
        ret = pre_upload.direct_main(["-j", "1"])
        self.assertEqual(ret, 0)
        self.hooks_mock.assert_called_once_with(
            mock.ANY,
            proj_dir=mock.ANY,
            commit_list=[],
            presubmit=mock.ANY,
            config_file=mock.ANY,
            jobs=1,
        )


class GetCargoClippyParserTest(cros_test_lib.TestCase):
    """Tests for _get_cargo_clippy_parser."""

    def testSingleProject(self):
        parser = pre_upload._get_cargo_clippy_parser()
        args = parser.parse_args(["--project", "foo"])
        self.assertEqual(
            args.project, [pre_upload.ClippyProject(root="foo", script=None)]
        )

    def testMultipleProjects(self):
        parser = pre_upload._get_cargo_clippy_parser()
        args = parser.parse_args(["--project", "foo:bar", "--project", "baz"])
        self.assertCountEqual(
            args.project,
            [
                pre_upload.ClippyProject(root="foo", script="bar"),
                pre_upload.ClippyProject(root="baz", script=None),
            ],
        )


class CheckCargoClippyTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Tests for _check_cargo_clippy."""

    def setUp(self):
        self.project = pre_upload.Project(
            name="PROJECT", dir=self.tempdir, remote=None
        )

        # Ugly hack since self.tempdir is not in the chroot.
        self.PatchObject(path_util, "ToChrootPath", side_effect=lambda x: x)

    def testClippy(self):
        """Verify clippy is called when a monitored file was changed."""
        rc_mock = self.PatchObject(pre_upload, "_run_command", return_value="")

        self.PatchObject(
            pre_upload,
            "_get_affected_files",
            return_value=[f"{self.project.dir}/repo_a/a.rs"],
        )

        ret = pre_upload._check_cargo_clippy(
            self.project,
            "COMMIT",
            options=["--project=repo_a", "--project=repo_b:foo"],
        )
        self.assertFalse(ret)

        # Check if `cargo clippy` ran.
        called = False
        for args, _ in rc_mock.call_args_list:
            cmd = args[0]
            if len(cmd) > 1 and cmd[0] == "cargo" and cmd[1] == "clippy":
                called = True
                break

        self.assertTrue(called)

    def testDontRun(self):
        """Skip checks when no monitored files are modified."""
        rc_mock = self.PatchObject(pre_upload, "_run_command", return_value="")

        # A file under `repo_a` was monitored.
        self.PatchObject(
            pre_upload,
            "_get_affected_files",
            return_value=[f"{self.project.dir}/repo_a/a.rs"],
        )
        # But, we only care about files under `repo_b`.
        errs = pre_upload._check_cargo_clippy(
            self.project, "COMMIT", options=["--project=repo_b:foo"]
        )

        self.assertFalse(errs)

        rc_mock.assert_not_called()

    def testDontRunNonRust(self):
        """Skip checks when no monitored files are modified."""
        rc_mock = self.PatchObject(pre_upload, "_run_command", return_value="")

        # A C++ file was changed.
        self.PatchObject(
            pre_upload,
            "_get_affected_files",
            return_value=[f"{self.project.dir}/repo_b/a.cc"],
        )
        # But, we only care about Rust files.
        errs = pre_upload._check_cargo_clippy(
            self.project, "COMMIT", options=["--project=repo_b:foo"]
        )

        self.assertFalse(errs)

        rc_mock.assert_not_called()

    def testCustomScript(self):
        """Verify project-specific script is used."""
        rc_mock = self.PatchObject(pre_upload, "_run_command", return_value="")

        self.PatchObject(
            pre_upload,
            "_get_affected_files",
            return_value=[f"{self.project.dir}/repo_b/b.rs"],
        )

        errs = pre_upload._check_cargo_clippy(
            self.project,
            "COMMIT",
            options=["--project=repo_a", "--project=repo_b:foo"],
        )
        self.assertFalse(errs)

        # Check if the script `foo` ran.
        called = False
        for args, _ in rc_mock.call_args_list:
            cmd = args[0]
            if len(cmd) > 0 and cmd[0] == os.path.join(self.project.dir, "foo"):
                called = True
                break

        self.assertTrue(called)


class OverrideHooksProcessing(PreUploadTestCase):
    """Verify _get_override_hooks processing."""

    @staticmethod
    def parse(data):
        """Helper to create a config & parse it."""
        cfg = configparser.ConfigParser()
        cfg.read_string(data)
        return pre_upload._get_override_hooks(cfg)

    def testHooks(self):
        """Verify we reject unknown hook names (e.g. typos)."""
        with self.assertRaises(ValueError) as e:
            self.parse(
                """
[Hook Overrides]
foobar: true
"""
            )
        self.assertIn("foobar", str(e.exception))

    def testImplicitDisable(self):
        """Verify non-common hooks aren't enabled by default."""
        enabled, _ = self.parse("")
        self.assertNotIn(pre_upload._run_checkpatch, enabled)

    def testExplicitDisable(self):
        """Verify hooks disabled are disabled."""
        _, disabled = self.parse(
            """
[Hook Overrides]
tab_check: false
"""
        )
        self.assertIn(pre_upload._check_no_tabs, disabled)

    def testExplicitEnable(self):
        """Verify hooks enabled are enabled."""
        enabled, _ = self.parse(
            """
[Hook Overrides]
tab_check: true
"""
        )
        self.assertIn(pre_upload._check_no_tabs, enabled)

    def testOptions(self):
        """Verify hook options are loaded."""
        enabled, _ = self.parse(
            """
[Hook Overrides Options]
keyword_check: --kw
"""
        )
        for func in enabled:
            if func.__name__ == "keyword_check":
                self.assertIn("options", func.keywords)
                self.assertEqual(func.keywords["options"], ["--kw"])
                break
        else:
            self.fail('could not find "keyword_check" enabled hook')

    def testSignOffField(self):
        """Verify signoff field handling."""
        # Enforce no s-o-b by default.
        enabled, disabled = self.parse("")
        self.assertIn(pre_upload._check_change_has_no_signoff_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_signoff_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_signoff_field, disabled)

        # If disabled, don't enforce either policy.
        enabled, disabled = self.parse(
            """
[Hook Overrides]
signoff_check: false
"""
        )
        self.assertNotIn(pre_upload._check_change_has_no_signoff_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_signoff_field, enabled)
        self.assertIn(pre_upload._check_change_has_signoff_field, disabled)

        # If enabled, require s-o-b.
        enabled, disabled = self.parse(
            """
[Hook Overrides]
signoff_check: true
"""
        )
        self.assertNotIn(pre_upload._check_change_has_no_signoff_field, enabled)
        self.assertIn(pre_upload._check_change_has_signoff_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_signoff_field, disabled)

    def testBranchField(self):
        """Verify branch field enabling."""
        # Should be disabled by default.
        enabled, disabled = self.parse("")
        self.assertIn(pre_upload._check_change_has_no_branch_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_branch_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_branch_field, disabled)

        # Should be disabled if requested.
        enabled, disabled = self.parse(
            """
[Hook Overrides]
branch_check: false
"""
        )
        self.assertIn(pre_upload._check_change_has_no_branch_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_branch_field, enabled)
        self.assertIn(pre_upload._check_change_has_branch_field, disabled)

        # Should be enabled if requested.
        enabled, disabled = self.parse(
            """
[Hook Overrides]
branch_check: true
"""
        )
        self.assertNotIn(pre_upload._check_change_has_no_branch_field, enabled)
        self.assertIn(pre_upload._check_change_has_branch_field, enabled)
        self.assertNotIn(pre_upload._check_change_has_branch_field, disabled)

    def testBoardPhase(self):
        """Verify board phase name disabling."""
        # Should be disabled by default.
        enabled, disabled = self.parse("")
        self.assertNotIn(
            pre_upload._check_change_no_include_board_phase, enabled
        )
        self.assertNotIn(
            pre_upload._check_change_no_include_board_phase, disabled
        )
        enabled, disabled = self.parse(
            """
[Hook Overrides]
check_change_no_include_board_phase: false
"""
        )
        self.assertIn(pre_upload._check_change_no_include_board_phase, disabled)
        self.assertNotIn(
            pre_upload._check_change_no_include_board_phase, enabled
        )
        enabled, disabled = self.parse(
            """
[Hook Overrides]
check_change_no_include_board_phase: true
"""
        )
        self.assertIn(pre_upload._check_change_no_include_board_phase, enabled)
        self.assertNotIn(
            pre_upload._check_change_no_include_board_phase, disabled
        )


class ProjectHooksProcessing(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Verify _get_project_hooks processing."""

    def parse(self, data, select=None):
        """Helper to write config and parse it."""
        filename = self.tempdir / "config"
        osutils.WriteFile(filename, data)
        config = pre_upload._get_project_config(filename)
        hooks = pre_upload._get_project_hooks(presubmit=True, config=config)
        if not select:
            return hooks
        return next(h for h in hooks if h.__name__ == select)

    def set_presubmit_files(self, files):
        """Helper to mock PRESUBMIT_FILES passed to hooks."""
        self.PatchObject(pre_upload, "_get_affected_files", return_value=files)

    def testPresubmitFiles(self):
        """Verify custom hook with PRESUBMIT_FILES is passed affected files."""
        self.set_presubmit_files(["/does_not_exist"])
        hook = self.parse(
            """\
[Hook Scripts]
uses files: /bin/cat ${PRESUBMIT_FILES}
""",
            "uses files",
        )
        result = hook(ProjectNamed("project"), "commit")
        self.assertEndsWith(
            result.msg, "cat: /does_not_exist: No such file or directory\n  "
        )

    def testPresubmitFilesSkipped(self):
        """Skips custom hook with PRESUBMIT_FILES when no files affected."""
        self.set_presubmit_files([])
        hook = self.parse(
            """\
[Hook Scripts]
uses files: /bin/false ${PRESUBMIT_FILES}
""",
            "uses files",
        )
        result = hook(ProjectNamed("project"), "commit")
        self.assertIsNone(result)

    def testNoPresubmitFilesNotSkipped(self):
        """Custom hook without PRESUBMIT_FILES not skipped when no files."""
        self.set_presubmit_files([])
        hook = self.parse(
            """\
[Hook Scripts]
no files: /bin/false
""",
            "no files",
        )
        result = hook(ProjectNamed("project"), "commit")
        self.assertEqual(
            result.msg, 'Hook script "/bin/false" failed with code 1'
        )


class ProjectOptionsProcessing(
    PreUploadTestCase, cros_test_lib.TempDirTestCase
):
    """Verify _get_project_hooks processing."""

    def parse(self, data):
        """Helper to write config and parse it."""
        filename = self.tempdir / "config"
        osutils.WriteFile(filename, data)
        config = pre_upload._get_project_config(filename)
        return pre_upload._get_project_options(config=config)

    def testIgnoreMergedCommitsDefault(self):
        """Verify ignore_merged_commits option disabled by default."""
        options = self.parse("")
        self.assertFalse(options["ignore_merged_commits"])

    def testIgnoreMergedCommitsDisabled(self):
        """Verify ignore_merged_commits option disabled when requested."""
        options = self.parse(
            """
[Options]
ignore_merged_commits: false
"""
        )
        self.assertFalse(options["ignore_merged_commits"])

    def testIgnoreMergedCommitsEnabled(self):
        """Verify ignore_merged_commits option enabled when requested."""
        options = self.parse(
            """
[Options]
ignore_merged_commits: true
"""
        )
        self.assertIn("ignore_merged_commits", options)
        self.assertTrue(options["ignore_merged_commits"])


class ReadmeTests(cros_test_lib.TestCase):
    """Tests to check README.md content."""

    def setUp(self):
        self.readme = (DIR / "README.md").read_text(encoding="utf-8")

    def testHookOverridesDocs(self):
        """Make sure [Hook Overrides] is kept up-to-date."""
        # Extract documented sections from the readme.
        start = end = None
        lines = self.readme.splitlines()
        for i, line in enumerate(lines):
            if start is None:
                if line == "## [Hook Overrides]":
                    start = i
            elif line.startswith("## "):
                end = i - 1
                break

        # See which hooks we support.
        doc_sections = [
            x.split("`")[1]
            for x in lines[start : start + end]
            if x.startswith("*")
        ]
        all_sections = sorted(pre_upload._HOOK_FLAGS.keys())
        sorted_docs = sorted(doc_sections)

        # Hook docs must be sorted.
        self.assertEqual(
            doc_sections,
            sorted_docs,
            msg="Keep README.md:[Hook Overrides] sorted",
        )

        # All hooks must be documented.
        self.assertEqual(
            sorted_docs,
            all_sections,
            msg=(
                "All configurable hooks must be documented in "
                "README.md:[Hook Overrides]"
            ),
        )


class CheckJsonKeyPrefixTest(PreUploadTestCase, cros_test_lib.TempDirTestCase):
    """Tests for _check_json_key_prefix."""

    def setUp(self):
        self.project = pre_upload.Project(
            name="chromiumos/project", dir=self.tempdir, remote=None
        )
        # Ugly hack since self.tempdir is not in the chroot.
        self.PatchObject(path_util, "ToChrootPath", side_effect=lambda x: x)
        self.file_mock = self.PatchObject(pre_upload, "_get_affected_files")
        self.content_mock = self.PatchObject(pre_upload, "_get_file_content")

    def testJsonFormat(self):
        """Verify that we can check the format of a json file."""
        self.file_mock.return_value = [
            f"{self.project.dir}/path/to/my/json_files/foo.json"
        ]
        self.content_mock.return_value = """
{
    "a": 1,
    "b": 2,
    "c": 3
}
        """
        ret = pre_upload._check_json_key_prefix(self.project, "COMMIT")
        self.assertIsNone(ret)

    def testInvalidJsonFormat(self):
        """Verify that we can check the format of a json file."""
        self.file_mock.return_value = [
            f"{self.project.dir}/path/to/my/json_files/foo.json"
        ]
        self.content_mock.return_value = """
{
    "a": 1,
    "b": 2,
    "c": 3,
}
        """
        ret = pre_upload._check_json_key_prefix(self.project, "COMMIT")
        self.assertTrue(ret)
        self.assertEqual("Invalid JSON file(s):", ret.msg)

    def testPrefixError(self):
        """Verify that no pair of keys in the json file are prefixes."""
        self.file_mock.return_value = [
            f"{self.project.dir}/path/to/my/json_files/foo.json"
        ]
        self.content_mock.return_value = """
{
    "abc": 1,
    "ab": 2,
    "c": 3
}
        """
        ret = pre_upload._check_json_key_prefix(self.project, "COMMIT")
        self.assertTrue(ret)
        self.assertEqual(
            ret.items,
            [
                f"{self.project.dir}/path/to/my/json_files/foo.json: "
                "Key 'ab' is a prefix of Key 'abc'"
            ],
        )
