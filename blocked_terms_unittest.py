#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2020 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for blocked_terms.txt and unblocked_terms.txt.

   Implements unit tests for the blocked terms and unblocked terms
   regex processed by pre-upload.py.
"""

from __future__ import print_function

import os
import sys


# pylint: disable=W0212
# We access private members of the pre_upload module.

# Make sure we can find the chromite paths.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                '..', '..'))

# The sys.path monkey patching confuses the linter.
# pylint: disable=wrong-import-position
from chromite.lib import cros_test_lib
from chromite.lib import osutils


assert sys.version_info >= (3, 6), 'This module requires Python 3.6+'


pre_upload = __import__('pre-upload')


class CheckFilesTest(cros_test_lib.MockTestCase, cros_test_lib.TempDirTestCase):
  """Tests for blocked and unblocked files."""

  DIFF = 'diff'
  MATCH = 'match'
  common_file_terms = None
  unblocked_file_terms = None
  hooks_dir = None

  @classmethod
  def setUpClass(cls):
    """Initialize the class instance mocks."""
    cls.hooks_dir = pre_upload._get_hooks_dir()
    cls.common_file_terms = pre_upload._read_terms_file(
        os.path.join(cls.hooks_dir, pre_upload.BLOCKED_TERMS_FILE))
    cls.unblocked_file_terms = pre_upload._read_terms_file(
        os.path.join(cls.hooks_dir, pre_upload.UNBLOCKED_TERMS_FILE))

  def setUp(self):
    """Initialize the test instance mockers."""
    pre_upload.CACHE.clear()
    self.PatchObject(pre_upload, '_get_affected_files',
                     return_value=['x.ebuild'])
    self.PatchObject(pre_upload, '_filter_files', return_value=['x.ebuild'])

    self.rf_mock = self.PatchObject(pre_upload, '_read_terms_file')
    self.diff_mock = self.PatchObject(pre_upload, '_get_file_diff')
    self.desc_mock = self.PatchObject(pre_upload, '_get_commit_desc')
    self.project = pre_upload.Project(name='PROJECT', dir='./', remote=None)
    self.hooks_dir_mock = self.PatchObject(pre_upload, '_get_hooks_dir')
    self.hooks_dir_mock.return_value = self.hooks_dir

  def CheckKeyword(self, test):
    """Test a particular keyword.

    Args:
      test: { DIFF: [(int, 'line to test keyword against'), ],
              MATCH: number of matched terms or None, }
    """
    def _check_keyword(unblocked):
      self.desc_mock.return_value = 'Commit message'
      self.diff_mock.return_value = test[self.DIFF]
      failures = pre_upload._check_keywords(self.project, 'COMMIT', ())
      if test[self.MATCH] and not unblocked:
        self.assertNotEqual(failures, [])
        self.assertEqual(test[self.MATCH], len(failures[0].items),
                         msg=failures[0].items)
      else:
        self.assertEqual(failures, [])

    # Check blocked terms.
    self.rf_mock.side_effect = [self.common_file_terms, set()]
    _check_keyword(unblocked=False)

    # Check unblocked terms.
    self.rf_mock.side_effect = [self.common_file_terms,
                                self.unblocked_file_terms]
    _check_keyword(unblocked=True)

  def check_lines(self, blocked_lines, unblocked_lines):
    """Check each lines for blocked or unblocked terms."""
    self.CheckKeyword({self.DIFF: blocked_lines,
                       self.MATCH: len(blocked_lines)})
    self.CheckKeyword({self.DIFF: unblocked_lines, self.MATCH: 0})

  def test_mitm_keyword(self):
    """Test mitm term."""
    self.check_lines([
        # blocked
        (1, 'blocked mitm '),
        (2, 'blocked (mitm)'),
        (3, 'blocked .mitm'),
        (4, 'blocked MITM'),
        (5, 'blocked mitm1'),
    ], [
        # unblocked
        (11, 'unblocked (commitment)'),
        (12, 'unblocked DailyLimitMins'),
    ])

  def test_sane_keyword(self):
    """Test sane term."""
    self.check_lines([
        # blocked
        (1, 'blocked sane '),
        (2, 'blocked (sane)'),
        (3, 'blocked .sane'),
        (4, 'blocked SANE'),
        (5, 'blocked sane1'),
        (6, 'blocked insane'),
        (7, 'blocked .insane'),
    ], [
        # unblocked
        (11, 'unblocked asanEnabled'),
    ])

  def test_whitelist_keyword(self):
    """Test whitelist term."""
    self.check_lines([
        # blocked
        (1, 'blocked white list '),
        (2, 'blocked white-list'),
        (3, 'blocked whitelist'),
        (4, 'blocked _whitelist'),
        (5, 'blocked whitelist1'),
        (6, 'blocked whitelisted'),
    ], [
        # unblocked
        (11, 'unblocked white dog list'),
    ])

  def test_sanity_keyword(self):
    """Test sanity term."""
    self.check_lines([
        # blocked
        (1, 'blocked sanity '),
        (2, 'blocked (sanity)'),
        (3, 'blocked struct.sanity'),
        (4, 'blocked SANITY'),
        (5, 'blocked AndroidSanityCheck'),
    ], [
        # unblocked
    ])


if __name__ == '__main__':
  cros_test_lib.main(module=__name__)
