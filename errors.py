# Copyright 2011 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Common errors thrown when repo presubmit checks fail."""

import abc
import re
import sys


class VerifyException(Exception):
    """Basic checks failed."""


class HookResult(abc.ABC):
    """Abstract base class for results from hook execution."""

    def __init__(self, msg, items=None):
        self.msg = msg
        self.items = items

    def __str__(self) -> str:
        return _FormatHookFailure(self)

    @property
    @abc.abstractmethod
    def is_warning(self) -> bool:
        """Returns True if the result is a non-fatal warning,"""


class HookFailure(HookResult):
    """A fatal error from a hook."""

    @property
    def is_warning(self) -> bool:
        return False


class HookWarning(HookResult):
    """A non-fatal warning from a hook."""

    @property
    def is_warning(self) -> bool:
        return True


_INDENT = " " * 4
_PROJECT_INFO = "Errors in PROJECT *%s*!"


def _PrintWithIndent(msg, indent_level):
    """Print a block of text with a specified indent level to stderr.

    Args:
        msg: A string to print (may contain newlines).
        indent_level: The number of indents to prefix each line with.  Each
            indent is four characters wide.
    """
    regex = re.compile(r"^", re.M)
    msg = regex.sub(_INDENT * indent_level, msg)
    print(msg, file=sys.stderr)


def _FormatHookFailure(hook_failure):
    """Returns the properly formatted VerifyException as a string."""
    item_prefix = "\n%s* " % _INDENT
    formatted_items = ""
    if hook_failure.items:
        formatted_items = item_prefix + item_prefix.join(hook_failure.items)
    return "* " + hook_failure.msg + formatted_items


def PrintErrorForProject(project, error):
    """Prints the project and its error.

    Args:
        project: project name
        error: An instance of the HookFailure class
    """
    _PrintWithIndent(_PROJECT_INFO % project, 0)
    _PrintWithIndent(_FormatHookFailure(error), 1)
    print("", file=sys.stderr)


def PrintErrorsForCommit(color, hook, project, result_list):
    """Prints the hook error to stderr with project and commit context

    Args:
        color: terminal.Color object for colorizing output.
        hook: Hook function that generated these errors.
        project: project name
        result_list: a list of HookResult instances
    """
    contains_error = any(not r.is_warning for r in result_list)
    prefix = (
        color.Color(color.RED, "FAILED")
        if contains_error
        else color.Color(color.YELLOW, "WARNING")
    )
    print(f"[{prefix}] {project}: {hook.__name__}")

    for error in result_list:
        _PrintWithIndent(error.msg.strip(), 1)
        if error.items:
            for item in error.items:
                _PrintWithIndent(item.strip(), 1)
        print("", file=sys.stderr)
