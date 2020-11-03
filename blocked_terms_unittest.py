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
# We access private members of the pre_upload module all over the place.

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

  def setUp(self):
    pre_upload.CACHE.clear()

    self.common_keywords = osutils.ReadFile(
        os.path.join(pre_upload._get_hooks_dir(),
                     pre_upload.BLOCKED_TERMS_FILE))

    self.unblocked_keywords = osutils.ReadFile(
        os.path.join(pre_upload._get_hooks_dir(),
                     pre_upload.UNBLOCKED_TERMS_FILE))

    self.PatchObject(pre_upload, '_get_affected_files',
                     return_value=['x.ebuild'])
    self.PatchObject(pre_upload, '_filter_files', return_value=['x.ebuild'])
    self.rf_mock = self.PatchObject(osutils, 'ReadFile')
    self.diff_mock = self.PatchObject(pre_upload, '_get_file_diff')
    self.desc_mock = self.PatchObject(pre_upload, '_get_commit_desc')
    self.project = pre_upload.Project(name='PROJECT', dir='./', remote=None)

  def CheckKeyword(self, test):
    """Test a particular keyword.

    Args:
      test: { DIFF: [(int, 'line to test keyword against'), ],
              MATCH: number of matched terms or None, }
    """
    def __check_keyword(unblocked):
      self.desc_mock.return_value = 'Commit message'
      self.diff_mock.return_value = test[self.DIFF]
      failures = pre_upload._check_keywords(self.project, 'COMMIT', ())
      if test[self.MATCH] and not unblocked:
        # Check that the expected number of blocked lines are found.
        self.assertNotEqual(failures, [])
        self.assertEqual(test[self.MATCH], len(failures[0].items))
      else:
        self.assertEqual(failures, [])

    # Check blocked terms.
    self.rf_mock.side_effect = [self.common_keywords, str('')]
    __check_keyword(unblocked=False)

    # Check unblocked terms.
    self.rf_mock.side_effect = [self.common_keywords, self.unblocked_keywords]
    __check_keyword(unblocked=True)

  def test_mitm_keyword(self):
    test_instance_blocked = {
        self.DIFF: [(1, 'blocked mitm '),
                    (2, 'blocked (mitm)'),
                    (3, 'blocked .mitm'),
                    (4, 'blocked MITM'),
                    (5, 'blocked mitm1'),],
        self.MATCH: 5,
    }
    self.CheckKeyword(test_instance_blocked)

    test_instance_unblocked = {
        self.DIFF: [(1, 'unblocked (commitment)'),
                    (2, 'unblocked DailyLimitMins'),],
        self.MATCH: 0,
    }
    self.CheckKeyword(test_instance_unblocked)

  def test_sane_keyword(self):
    test_instance_blocked = {
        self.DIFF: [(1, 'blocked sane '),
                    (2, 'blocked (sane)'),
                    (3, 'blocked .sane'),
                    (4, 'blocked SANE'),
                    (5, 'blocked sane1'),],
        self.MATCH: 5,
    }
    self.CheckKeyword(test_instance_blocked)

    test_instance_unblocked = {
        self.DIFF: [(1, 'unblocked asanEnabled'),],
        self.MATCH: 0,
    }
    self.CheckKeyword(test_instance_unblocked)


if __name__ == '__main__':
  cros_test_lib.main(module=__name__)
