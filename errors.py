# -*- coding: utf-8 -*-
# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Common errors thrown when repo presubmit checks fail."""

from __future__ import print_function

import re
import sys


class VerifyException(Exception):
  """Basic sanity checks failed."""


class HookFailure(object):
  """Contains an error message and a list of error details."""
  def __init__(self, msg, items=None):
    self.msg = msg
    self.items = items

  def __str__(self):
    return _FormatHookFailure(self)


_INDENT = ' ' * 4
_PROJECT_INFO = 'Errors in PROJECT *%s*!'


def _PrintWithIndent(msg, indent_level):
  """Print a block of text with a specified indent level to stderr.

  Args:
    msg: A string to print (may contain newlines).
    indent_level: The number of indents to prefix each line with.  Each indent
        is four characters wide.
  """
  regex = re.compile(r'^', re.M)
  msg = regex.sub(_INDENT * indent_level, msg)
  print(msg, file=sys.stderr)


def _FormatCommitDesc(desc):
  """Returns the properly prefixed commit description."""
  regex = re.compile(r'^', re.M)
  return regex.sub('>', desc)


def _FormatHookFailure(hook_failure):
  """Returns the properly formatted VerifyException as a string."""
  item_prefix = '\n%s* ' % _INDENT
  formatted_items = ''
  if hook_failure.items:
    formatted_items = item_prefix + item_prefix.join(hook_failure.items)
  return '* ' + hook_failure.msg + formatted_items


def PrintErrorForProject(project, error):
  """Prints the project and its error.

  Args:
    project: project name
    error: An instance of the HookFailure class
  """
  _PrintWithIndent(_PROJECT_INFO % project, 0)
  _PrintWithIndent(_FormatHookFailure(error), 1)
  print('', file=sys.stderr)


def PrintErrorsForCommit(project, commit, commit_desc, error_list):
  """Prints the hook error to stderr with project and commit context

  A sample error output for a project would be:
  ----------------------------------------------------------------------------
  Errors in PROJECT *chromiumos/repohooks*!
    COMMIT 10041758:
        Description:
            >staged
            >
            >TEST=some
            >Change-Id: I2c4f545a20a659541c02be16aa9dc440c876a604
            >
        Errors:
            * Changelist description needs BUG field (after first line)
            * Found line ending with white space in:
                * src/repohooks/pre-upload.py, line 307
            * Found lines longer than 80 characters (first 5 shown):
                * src/repohooks/pre-upload.py, line 335, 85 chars
  ----------------------------------------------------------------------------

  Args:
    project: project name
    commit: the commit hash the errors belong to
    commit_desc: a string containing the commit message
    error_list: a list of HookFailure instances
  """
  _PrintWithIndent(_PROJECT_INFO % project, 0)

  formatted_desc = _FormatCommitDesc(commit_desc)
  _PrintWithIndent('COMMIT %s:' % commit[:8], 1)
  _PrintWithIndent('Description:', 2)
  _PrintWithIndent(formatted_desc, 3)
  _PrintWithIndent('Errors:', 2)

  for error in error_list:
    _PrintWithIndent(_FormatHookFailure(error), 3)

  print('', file=sys.stderr)
