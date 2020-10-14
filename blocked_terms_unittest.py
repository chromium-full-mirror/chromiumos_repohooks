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
    # Test a particular keyword.
    #
    #  Args:
    #   { DIFF:[list of values to test keyword against],
    #     FAILURE:[list of expected failures] or None }
    #
    def __check_keyword(unblocked):
      self.desc_mock.return_value = 'Commit message'
      self.diff_mock.return_value = test[self.DIFF]
      failures = pre_upload._check_keywords(self.project, 'COMMIT', ())
      if test[self.MATCH] and not unblocked:
        self.assertNotEqual(failures, [])
        self.assertEqual('Found a blocked keyword in:', failures[0].msg)
        self.assertEqual(test[self.MATCH], failures[0].items)
      else:
        self.assertEqual(failures, [])

    self.rf_mock.side_effect = [self.common_keywords, str('')]
    __check_keyword(unblocked=False)

    self.rf_mock.side_effect = [self.common_keywords, self.unblocked_keywords]
    __check_keyword(unblocked=True)

  def test_mitm_keyword(self):
    # pylint: disable=C0301

    # { DIFF:[list of values to test keyword against],
    #   FAILURE:[list of expected failures] or None },}
    #
    test_instance = {
        self.DIFF:[(1, 'Line with blocked mitm '),
                   (2, 'Line with blocked (mitm)'),
                   (3, 'Line with blocked .mitm'),
                   (4, 'Line with blocked MITM'),
                   (5, 'Line with blocked mitm1'),
                   (6, 'Line with unblocked (commitment)'),
                   (7, 'Line with unblocked DailyLimitMins'),],
        self.MATCH:[
            'x.ebuild, line 1: Matched "mitm" with regex of "\\bmitm(\\b|\\d)"',
            'x.ebuild, line 2: Matched "mitm" with regex of "\\bmitm(\\b|\\d)"',
            'x.ebuild, line 3: Matched "mitm" with regex of "\\bmitm(\\b|\\d)"',
            'x.ebuild, line 4: Matched "MITM" with regex of "\\bmitm(\\b|\\d)"',
            'x.ebuild, line 5: Matched "mitm1" with regex of "\\bmitm(\\b|\\d)"'],
    }

    self.CheckKeyword(test_instance)


if __name__ == '__main__':
  cros_test_lib.main(module=__name__)
