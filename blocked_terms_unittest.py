# Copyright 2020 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for blocked_terms.txt and unblocked_terms.txt.

Implements unit tests for the blocked terms and unblocked terms
regex processed by pre-upload.py.
"""

from chromite.lib import cros_test_lib


# We access private members of the pre_upload module.
# pylint: disable=protected-access

pre_upload = __import__("pre-upload")


class CheckFilesTest(cros_test_lib.MockTestCase, cros_test_lib.TempDirTestCase):
    """Tests for blocked and unblocked files."""

    DIFF = "diff"
    MATCH = "match"
    common_file_terms = None
    unblocked_file_terms = None
    hooks_dir = None

    @classmethod
    def setUpClass(cls):
        """Initialize the class instance mocks."""
        cls.common_file_terms = pre_upload._read_terms_file(
            pre_upload.REPOHOOKS_DIR / pre_upload.BLOCKED_TERMS_FILE
        )
        # With the unblocked file effectively empty, to test unblocking we must
        # have our own list here.
        cls.unblocked_file_terms = {
            "\\bmitm(\\b|\\d)",
            "\\b(in)?sane(\\b|\\d)",  # nocheck
            "sanity",  # nocheck
            "white.?list",
        }

    def setUp(self):
        """Initialize the test instance mockers."""
        pre_upload.CACHE.clear()
        self.PatchObject(
            pre_upload, "_get_affected_files", return_value=["x.ebuild"]
        )
        self.PatchObject(pre_upload, "_filter_files", return_value=["x.ebuild"])

        self.rf_mock = self.PatchObject(pre_upload, "_read_terms_file")
        self.diff_mock = self.PatchObject(pre_upload, "_get_file_diff")
        self.desc_mock = self.PatchObject(pre_upload, "_get_commit_desc")
        self.project = pre_upload.Project(name="PROJECT", dir="./", remote=None)

    def CheckKeyword(self, test):
        """Test a particular keyword.

        Args:
            test: { DIFF: [(int, 'line to test keyword against'), ],
                    MATCH: number of matched terms or None, }
        """

        def _check_keyword(unblocked):
            self.desc_mock.return_value = "Commit message"
            self.diff_mock.return_value = test[self.DIFF]
            failures = pre_upload._check_keywords(self.project, "COMMIT", ())
            if test[self.MATCH] and not unblocked:
                self.assertNotEqual(failures, [])
                self.assertEqual(
                    test[self.MATCH],
                    len(failures[0].items),
                    msg=failures[0].items,
                )
            else:
                self.assertEqual(failures, [])

        # Check blocked terms.
        self.rf_mock.side_effect = [self.common_file_terms, set(), set()]
        _check_keyword(unblocked=False)

        # Check unblocked terms.
        self.rf_mock.side_effect = [
            self.common_file_terms,
            self.unblocked_file_terms,
            self.unblocked_file_terms,
        ]
        _check_keyword(unblocked=True)

    def check_lines(self, blocked_lines, unblocked_lines):
        """Check each lines for blocked or unblocked terms."""
        self.CheckKeyword(
            {self.DIFF: blocked_lines, self.MATCH: len(blocked_lines)}
        )
        self.CheckKeyword({self.DIFF: unblocked_lines, self.MATCH: 0})

    def test_mitm_keyword(self):  # nocheck
        """Test mitm term."""  # nocheck
        self.check_lines(
            [
                # blocked
                (1, "blocked mitm "),  # nocheck
                (2, "blocked (mitm)"),  # nocheck
                (3, "blocked .mitm"),  # nocheck
                (4, "blocked MITM"),  # nocheck
                (5, "blocked mitm1"),  # nocheck
                (6, "blocked mitm //nocheck"),  # nocheck
                (7, "blocked mitm nocheck."),  # nocheck
                (7, "blocked mitm FLAG_nocheck"),  # nocheck
            ],
            [
                # unblocked
                (11, "unblocked (commitment)"),
                (12, "unblocked DailyLimitMins"),
                (13, "unblocked mitm // nocheck"),  # nocheck
            ],
        )

    def test_sane_keyword(self):  # nocheck
        """Test sane term."""  # nocheck
        self.check_lines(
            [
                # blocked
                (1, "blocked sane "),  # nocheck
                (2, "blocked (sane)"),  # nocheck
                (3, "blocked .sane"),  # nocheck
                (4, "blocked SANE"),  # nocheck
                (5, "blocked sane1"),  # nocheck
                (6, "blocked insane"),  # nocheck
                (7, "blocked .insane"),  # nocheck
            ],
            [
                # unblocked
                (11, "unblocked asanEnabled"),
            ],
        )

    def test_whitelist_keyword(self):  # nocheck
        """Test whitelist term."""  # nocheck
        self.check_lines(
            [
                # blocked
                (1, "blocked white list "),  # nocheck
                (2, "blocked white-list"),  # nocheck
                (3, "blocked whitelist"),  # nocheck
                (4, "blocked _whitelist"),  # nocheck
                (5, "blocked whitelist1"),  # nocheck
                (6, "blocked whitelisted"),  # nocheck
            ],
            [
                # unblocked
                (11, "unblocked white dog list"),
            ],
        )

    def test_sanity_keyword(self):  # nocheck
        """Test sanity term."""  # nocheck
        self.check_lines(
            [
                # blocked
                (1, "blocked sanity "),  # nocheck
                (2, "blocked (sanity)"),  # nocheck
                (3, "blocked struct.sanity"),  # nocheck
                (4, "blocked SANITY"),  # nocheck
                (5, "blocked AndroidSanityCheck"),  # nocheck
            ],
            [
                # unblocked
            ],
        )
