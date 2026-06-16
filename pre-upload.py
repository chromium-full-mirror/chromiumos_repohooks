#!/usr/bin/env python3
# Copyright 2012 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Presubmit checks to run when doing `repo upload`.

You can add new checks by adding a function to the HOOKS constants.
"""

import argparse
import collections
import concurrent.futures
import configparser
import contextlib
import datetime
import enum
import fnmatch
import functools
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from typing import Optional, Sequence, Tuple

import errors


# Path to the repohooks dir itself.
REPOHOOKS_DIR = Path(__file__).resolve().parent


if __name__ in ("__builtin__", "builtins"):
    # If repo imports us, the __name__ will be __builtin__, and the cwd will be
    # in the top level of the checkout (i.e. $CHROMEOS_CHECKOUT).  chromite will
    # be in that directory, so add it to our path.  This works whether we're
    # running the repo in $CHROMEOS_CHECKOUT/.repo/repo/ or a custom version in
    # a completely different tree.
    # TODO(vapier): Python 2 used "__builtin__" while Python 3 uses "builtins".
    sys.path.insert(0, os.getcwd())

elif __name__ == "__main__":
    # If we're run directly, we'll find chromite relative to the repohooks dir
    # in $CHROMEOS_CHECKOUT/src/repohooks, so go up two dirs.
    sys.path.insert(0, str(REPOHOOKS_DIR.parent.parent))

# The sys.path monkey patching confuses the linter.
# pylint: disable=wrong-import-position
from chromite.format import formatters
from chromite.lib import build_query
from chromite.lib import commandline
from chromite.lib import constants
from chromite.lib import cros_build_lib
from chromite.lib import git
from chromite.lib import osutils
from chromite.lib import patch
from chromite.lib import path_util
from chromite.lib import portage_util
from chromite.lib import sudo
from chromite.lib import terminal
from chromite.licensing import licenses_lib
from chromite.lint import linters
from chromite.utils import timer


assert sys.version_info >= (3, 6), "This module requires Python 3.6+"


PRE_SUBMIT = "pre-submit"


# Path to bundled tools.
TOOLS_DIR = REPOHOOKS_DIR / "third_party"


# Terminal escape sequence to erase the current line after the cursor.
CSI_ERASE_LINE_AFTER = "\x1b[K"


# Link to commit message documentation for users.
DOC_COMMIT_MSG_URL = (
    "https://www.chromium.org/chromium-os/developer-library/guides"
    "/development/contributing/#commit-messages"
)


CPP_PATHS = [
    # C++ and friends
    r".*\.c$",
    r".*\.cc$",
    r".*\.cpp$",
    r".*\.h$",
    r".*\.m$",
    r".*\.mm$",
    r".*\.inl$",
    r".*\.asm$",
    r".*\.hxx$",
    r".*\.hpp$",
    r".*\.s$",
    r".*\.S$",
]


COMMON_INCLUDED_PATHS = CPP_PATHS + [
    # Scripts
    r".*\.js$",
    r".*\.ts$",
    r".*\.py$",
    r".*\.sh$",
    r".*\.rb$",
    r".*\.pl$",
    r".*\.pm$",
    # No extension at all
    r"(^|.*[\\\/])[^.]+$",
    # Other
    r".*\.gn",
    r".*\.gni",
    r".*\.java$",
    r".*\.mk$",
    r".*\.am$",
    r".*\.policy$",
    r".*\.rules$",
    r".*\.conf$",
    r".*\.go$",
    r".*\.rs$",
    r".*\.ebuild$",
    r".*\.eclass$",
    r".*\.bazel$",
    r".*\.bzl$",
    r"(^BUILD|/BUILD)",
    r"(^OWNERS|/OWNERS)",
]


COMMON_EXCLUDED_PATHS = [
    # For ebuild trees, ignore any caches and manifest data.
    r".*/Manifest$",
    r".*/files/srcuris$",
    r".*/metadata/[^/]*cache[^/]*/[^/]+/[^/]+$",
    # Ignore profiles data (like overlay-tegra2/profiles).
    r"(^|.*/)overlay-.*/profiles/.*",
    r"^profiles/.*$",
    # Ignore config files in ebuild setup.
    r"(^|.*/)overlay-.*/chromeos-base/chromeos-bsp.*/files/.*",
    r"^chromeos-base/chromeos-bsp.*/files/.*",
    # Ignore minified js and jquery.
    r".*\.min\.js",
    r".*jquery.*\.js",
    # Ignore license files as the content is often taken verbatim.
    r".*/licenses/.*",
    # Exclude generated protobuf bindings.
    r".*_pb2\.py$",
    r".*_pb2_grpc\.py$",
    r".*\.pb\.go$",
]

LICENSE_EXCLUDED_PATHS = [
    r"^(.*/)?OWNERS(\..*)?$",
    r"^(.*/)?DIR_METADATA(\..*)?$",
    # We pull a lot of Gentoo copyrighted files, so we can't enforce this.
    r".*\.ebuild$",
    r".*\.eclass$",
]

_CONFIG_FILE = "PRESUBMIT.cfg"


# File containing wildcards, one per line, matching files that should be
# excluded from presubmit checks. Lines beginning with '#' are ignored.
_IGNORE_FILE = ".presubmitignore"

BLOCKED_TERMS_FILE = "blocked_terms.txt"
UNBLOCKED_TERMS_FILE = "unblocked_terms.txt"

# Android internal and external projects use "Bug: " to track bugs in
# buganizer, so use Bug: and Test: instead of BUG= and TEST=.
TAG_COLON_REMOTES = {
    "aosp",
    "goog",
}

# Exceptions


class BadInvocation(Exception):
    """An Exception indicating a bad invocation of the program."""


# General Helpers


class Cache:
    """General helper for caching git content."""

    def __init__(self):
        self._cache = {}

    def clear(self):
        # Retain existing scopes (and their locks) while clearing all keys.
        for lock, scope in self._cache.values():
            with lock:
                scope.clear()

    def _get_or_compute(self, scope, key, fn) -> Tuple[threading.RLock, dict]:
        """Get the current value of a key, or compute its value.

        The provided scope must already exist.

        This is thread-safe and the first thread to request the
        value for a given key will block all other threads accessing the same
        scope until the value is computed, avoiding duplicate work when values
        for the same input are requested concurrently.
        """
        (lock, items) = self._cache[scope]
        with lock:
            if key not in items:
                items[key] = fn()
            return items[key]

    def cache_function(self, f):
        """Decorator to cache the return value of a function.

        The first positional argument to the function is used as the cache key.
        All other parameters are ignored and passed through.

        Cached values are partitioned by the name of the wrapped function;
        functions with unique names will never see cached values from other
        functions.

        >>> cache = Cache()
        >>> @cache.cache_function
        ... def fact(n):
        ...     if n == 1:
        ...         return 1
        ...     else:
        ...         return n * fact(n - 1)
        >>> fact(4)
        24
        >>> cache._get_or_compute('fact', 4, lambda: None)
        24
        """
        scope = f.__name__
        # Initialize the cache partition for this function.
        self._cache[scope] = (threading.RLock(), {})

        @functools.wraps(f)
        def _do_cached_function(*args, **kwargs):
            return self._get_or_compute(
                scope, args[0], lambda: f(*args, **kwargs)
            )

        return _do_cached_function


CACHE = Cache()


Project = collections.namedtuple("Project", ["name", "dir", "remote"])


def _run_command(cmd, **kwargs):
    """Executes the passed in command and returns raw stdout output.

    This is a convenience func to set some run defaults differently.

    Args:
        cmd: The command to run; should be a list of strings.
        **kwargs: Same as cros_build_lib.run.

    Returns:
        The stdout from the process (discards stderr and returncode).
    """
    kwargs.setdefault("stdout", True)
    # Run commands non-interactively by default.
    kwargs.setdefault("input", "")
    kwargs.setdefault("check", False)
    result = cros_build_lib.dbg_run(cmd, **kwargs)
    # NB: We decode this directly rather than through kwargs as our tests rely
    # on this post-processing behavior currently.
    return result.stdout.decode("utf-8", "replace")


def _match_regex_list(subject, expressions):
    """Try to match a list of regular expressions to a string.

    Args:
        subject: The string to match regexes on
        expressions: A list of regular expressions to check for matches with.

    Returns:
        Whether the passed in subject matches any of the passed in regexes.
    """
    return any(x.search(subject) for x in expressions)


def _filter_files(files, include_list, exclude_list=()):
    """Filter out files based on the conditions passed in.

    Args:
        files: list of filepaths to filter
        include_list: list of regex that when matched with a file path will
            cause it to be added to the output list unless the file is also
            matched with a regex in the exclude_list.
        exclude_list: list of regex that when matched with a file will prevent
            it from being added to the output list, even if it is also matched
            with a regex in the include_list.

    Returns:
        A list of filepaths that contain files matched in the include_list and
        not in the exclude_list.
    """
    filtered = []
    include_list_comp = [re.compile(x) for x in include_list]
    exclude_list_comp = [re.compile(x) for x in exclude_list]
    for f in files:
        if _match_regex_list(f, include_list_comp) and not _match_regex_list(
            f, exclude_list_comp
        ):
            filtered.append(f)
    return filtered


# Git Helpers


def _get_upstream_branch():
    """Returns the upstream tracking branch of the current branch.

    Raises:
        Error if there is no tracking branch
    """
    current_branch = _run_command(["git", "symbolic-ref", "HEAD"]).strip()
    current_branch = current_branch.replace("refs/heads/", "")
    if not current_branch:
        raise errors.VerifyException("Need to be on a tracking branch")

    cfg_option = "branch." + current_branch + ".%s"
    full_upstream = _run_command(
        ["git", "config", cfg_option % "merge"]
    ).strip()
    remote = _run_command(["git", "config", cfg_option % "remote"]).strip()
    if not remote or not full_upstream:
        raise errors.VerifyException("Need to be on a tracking branch")

    return full_upstream.replace("heads", "remotes/" + remote)


def _get_patch(commit):
    """Returns the patch for this commit."""
    if commit == PRE_SUBMIT:
        return _run_command(["git", "diff", "--cached", "HEAD"])
    else:
        return _run_command(
            ["git", "format-patch", "-p", "--stdout", "-1", commit]
        )


@CACHE.cache_function
def _get_file_content(path, commit):
    """Returns the content of a file at a specific commit.

    We can't rely on the file as it exists in the filesystem as people might be
    uploading a series of changes which modifies the file multiple times.

    Note: The "content" of a symlink is just the target.  So if you're expecting
    a full file, you should check that first.  One way to detect is that the
    content will not have any newlines.
    """
    # Make sure people don't accidentally pass in full paths which will never
    # work.  You need to use relative=True with _get_affected_files.
    if path.startswith("/"):
        raise ValueError(
            "_get_file_content must be called with relative paths: %s" % (path,)
        )

    if commit == PRE_SUBMIT:
        content = _run_command(["git", "diff", "HEAD", "--", path], stderr=True)
    else:
        content = _run_command(
            ["git", "show", "%s:%s" % (commit, path)], stderr=True
        )
    return content


@CACHE.cache_function
def _get_file_diff(path, commit):
    """Returns a list of (linenum, lines) tuples that the commit touched."""
    if commit == PRE_SUBMIT:
        command = [
            "git",
            "diff",
            "-p",
            "--pretty=format:",
            "--no-ext-diff",
            "HEAD",
            "--",
            path,
        ]
    else:
        command = [
            "git",
            "show",
            "-p",
            "--pretty=format:",
            "--no-ext-diff",
            commit,
            "--",
            path,
        ]
    output = _run_command(command)

    new_lines = []
    line_num = 0
    for line in output.splitlines():
        m = re.match(r"^@@ [0-9\,\+\-]+ \+([0-9]+)\,[0-9]+ @@", line)
        if m:
            line_num = int(m.groups(1)[0])
            continue
        if line.startswith("+") and not line.startswith("++"):
            new_lines.append((line_num, line[1:]))
        if not line.startswith("-"):
            line_num += 1
    return new_lines


def _get_ignore_wildcards(directory, cache):
    """Get wildcards listed in a directory's _IGNORE_FILE.

    Args:
        directory: A string containing a directory path.
        cache: A dictionary (opaque to caller) caching previously-read
            wildcards.

    Returns:
        A list of wildcards from _IGNORE_FILE or an empty list if _IGNORE_FILE
        wasn't present.
    """
    # In the cache, keys are directories and values are lists of wildcards from
    # _IGNORE_FILE within those directories (and empty if no file was present).
    if directory not in cache:
        wildcards = []
        dotfile_path = os.path.join(directory, _IGNORE_FILE)
        if os.path.exists(dotfile_path):
            # TODO(derat): Consider using _get_file_content() to get the file as
            # of this commit instead of the on-disk version. This may have a
            # noticeable performance impact, as each call to _get_file_content()
            # runs git.
            with open(dotfile_path, "r", encoding="utf-8") as dotfile:
                for line in dotfile.readlines():
                    line = line.strip()
                    if line.startswith("#"):
                        continue
                    if line.endswith("/"):
                        line += "*"
                    wildcards.append(line)
        cache[directory] = wildcards

    return cache[directory]


def _path_is_ignored(path, cache):
    """Check whether a path is ignored by _IGNORE_FILE.

    Args:
        path: A string containing a path.
        cache: A dictionary (opaque to caller) caching previously-read
            wildcards.

    Returns:
        True if a file named _IGNORE_FILE in one of the passed-in path's parent
        directories contains a wildcard matching the path.
    """
    # Skip ignore files.
    if os.path.basename(path) == _IGNORE_FILE:
        return True

    path = os.path.abspath(path)
    base = os.getcwd()

    prefix = os.path.dirname(path)
    while prefix.startswith(base):
        rel_path = path[len(prefix) + 1 :]
        for wildcard in _get_ignore_wildcards(prefix, cache):
            if fnmatch.fnmatch(rel_path, wildcard):
                return True
        prefix = os.path.dirname(prefix)

    return False


@CACHE.cache_function
def _get_all_affected_files(commit, path):
    """Return the unfiltered list of file paths affected by a commit.

    This function exists only to provide caching for _get_affected_files();
    users should call that function instead.
    """
    if commit == PRE_SUBMIT:
        return _run_command(
            ["git", "diff-index", "--cached", "--name-only", "HEAD"]
        ).split()

    return git.RawDiff(path, "%s^!" % commit)


def _get_affected_files(
    commit,
    include_deletes=False,
    relative=False,
    include_symlinks=False,
    include_adds=True,
    full_details=False,
    use_ignore_files=True,
):
    """Returns list of file paths that were modified/added, excluding symlinks.

    Args:
        commit: The commit
        include_deletes: If true, we'll include deleted files in the result
        relative: Whether to return relative or full paths to files
        include_symlinks: If true, we'll include symlinks in the result
        include_adds: If true, we'll include new files in the result
        full_details: If False, return filenames, else return structured
            results.
        use_ignore_files: Whether we ignore files matched by _IGNORE_FILE files.

    Returns:
        A list of modified/added (and perhaps deleted) files
    """
    if not relative and full_details:
        raise ValueError("full_details only supports relative paths currently")

    path = os.getcwd()
    files = _get_all_affected_files(commit, path)

    # Filter out symlinks.
    if not include_symlinks:
        files = [x for x in files if not stat.S_ISLNK(int(x.dst_mode, 8))]

    if not include_deletes:
        files = [x for x in files if x.status != "D"]

    if not include_adds:
        files = [x for x in files if x.status != "A"]

    if use_ignore_files:
        cache = {}
        is_ignored = lambda x: _path_is_ignored(x.dst_file or x.src_file, cache)
        files = [x for x in files if not is_ignored(x)]

    if full_details:
        # Caller wants the raw objects to parse status/etc... themselves.
        return files
    else:
        # Caller only cares about filenames.
        files = [x.dst_file if x.dst_file else x.src_file for x in files]
        if relative:
            return files
        else:
            return [os.path.join(path, x) for x in files]


def _get_commits(ignore_merged_commits=False):
    """Returns a list of commits for this review."""
    cmd = [
        "git",
        "log",
        "--no-merges",
        "--format=%H",
        "%s.." % _get_upstream_branch(),
    ]
    if ignore_merged_commits:
        cmd.append("--first-parent")
    return _run_command(cmd).split()


@CACHE.cache_function
def _get_commit_desc(commit):
    """Returns the full commit message of a commit."""
    if commit == PRE_SUBMIT:
        return ""

    return _run_command(["git", "log", "--format=%B", commit + "^!"])


class ChangeSize(enum.IntEnum):
    """Change size definitions.

    Definitions align with the Gerrit definitions here:
    https://gerrit.googlesource.com/gerrit/+/5ed0f9b6eef11bb2a7900c2a278d75baa2279c8f/polygerrit-ui/app/elements/change-list/gr-change-list-item/gr-change-list-item.ts#48.
    """

    XS = 10
    S = 50
    M = 250
    L = 1000


def _get_lines_changed(
    commit: str,
    include_deletes: bool = True,
    include_list: Optional[Sequence[str]] = None,
    exclude_list: Sequence[str] = (),
) -> int:
    """Return the number of lines changed in a commit.

    Args:
        commit: The commit to count lines for.
        include_deletes: If true, count the lines in deleted files.
        include_list: List of regex that when matched with a file path will
            cause it to be included in the count unless the file is also matched
            with a regex in the exclude_list. If not set, all files are
            included.
        exclude_list: List of regex that when matched with a file will prevent
            it from being included in the count, even if it is also matched with
            a regex in the include_list. include_list must be set if
            exclude_list is.

    Returns:
        The number of lines changed in the commit.
    """
    if exclude_list:
        assert include_list, "include_list must be provided if exclude_list is"

    files = _get_affected_files(
        commit,
        include_deletes=include_deletes,
        include_symlinks=False,
    )

    if include_list:
        files = _filter_files(
            files,
            include_list,
            exclude_list,
        )

    return sum(len(_get_file_diff(x, commit)) for x in files)


def _get_cl_size_string(color: terminal.Color, commit: str) -> str:
    """Return a colored string describing the CL size.

    Possible strings are "XS", "S", "M", "L", and "XL", see the CHANGE_SIZE
    constants above for the size thresholds.

    Strings are colored to encourage smaller CLs.
    """
    lines_changed = _get_lines_changed(commit)

    # Padding the strings won't work once they're wrapped in the color codes, so
    # make sure they are all 2 characters.
    if lines_changed < ChangeSize.XS:
        return color.Color(color.GREEN, "XS")
    elif lines_changed < ChangeSize.S:
        return color.Color(color.GREEN, " S")
    elif lines_changed < ChangeSize.M:
        return color.Color(color.GREEN, " M")
    elif lines_changed < ChangeSize.L:
        return color.Color(color.YELLOW, " L")
    else:
        return color.Color(color.RED, "XL")


def _check_lines_in_diff(commit, files, check_callable, error_description):
    """Checks given file for errors via the given check.

    This is a convenience function for common per-line checks. It goes through
    all files and returns a HookFailure with the error description listing all
    the failures.

    Args:
        commit: The commit we're working on.
        files: The files to check.
        check_callable: A callable that takes a line and returns True if this
            line fails the check.
        error_description: A string describing the error.
    """
    error_list = []
    for afile in files:
        for line_num, line in _get_file_diff(afile, commit):
            result = check_callable(line)
            if result:
                msg = f"{afile}, line {line_num}"
                if isinstance(result, str):
                    msg += f": {result}"
                error_list.append(msg)
    if error_list:
        return errors.HookFailure(error_description, error_list)
    return None


def _parse_common_inclusion_options(options):
    """Parses common hook options for including/excluding files.

    Args:
        options: Option string list.

    Returns:
        (included, excluded) where each one is a list of regex strings.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--exclude_regex", action="append")
    parser.add_argument("--include_regex", action="append")
    opts = parser.parse_args(options)
    included = opts.include_regex or []
    excluded = opts.exclude_regex or []
    return included, excluded


# Common Hooks


def _check_no_extra_blank_lines(_project, commit, options=()):
    """Checks there are no multiple blank lines at end of file."""
    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit, relative=True),
        included + COMMON_INCLUDED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS,
    )
    error_list = []
    for afile in files:
        file_content = _get_file_content(afile, commit)
        last_bytes = file_content[-2:]
        if last_bytes == "\n\n":
            error_list.append(afile)
    if error_list:
        return errors.HookFailure(
            "Found extra blank line at end of file:", error_list
        )
    return None


def _check_no_handle_eintr_close(_project, commit, options=()):
    """Checks that there is no HANDLE_EINTR(close(...))."""
    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit),
        included + CPP_PATHS,
        excluded + COMMON_EXCLUDED_PATHS,
    )
    return _check_lines_in_diff(
        commit,
        files,
        lambda line: "HANDLE_EINTR(close(" in line,
        "HANDLE_EINTR(close) is invalid. See http://crbug.com/269623.",
    )


def _check_no_long_lines(_project, commit, options=()):
    """Checks there are no lines longer than MAX_LEN in any of the files."""
    LONG_LINE_OK_PATHS = [
        # Bazel's BUILD files have no line length limit. The style guide says
        # "it should not be enforced in code reviews or presubmit scripts".
        # https://bazel.build/build/style-guide?hl=en
        r".*\.bazel$",
        # "As in BUILD files, there is no strict line length limit as labels
        # can be long. When possible, try to use at most 79 characters per line
        # (following Python's style guide, PEP 8)."
        # https://bazel.build/rules/bzl-style#line-length
        r".*\.bzl$",
        # GN format does not enforce a line length limit by the cros format.
        # https://chromium.googlesource.com/dart/dartium/src/+/HEAD/tools/gn/docs/style_guide.md
        r".*\.gn$",
        # Go has no line length limit.
        # https://golang.org/doc/effective_go.html#formatting
        r".*\.go$",
        # Python does its own long line checks via pylint.
        r".*\.py$",
        # Google TypeScript Style Guide has no line length limit.
        # https://google.github.io/styleguide/tsguide.html
        r".*\.ts$",
        # Gentoo has its own style.
        r".*(\.ebuild|\.eclass|metadata\/layout.conf)$",
    ]

    DEFAULT_MAX_LENGTHS = [
        # Java's line length limit is 100 chars.
        # https://chromium.googlesource.com/chromium/src/+/HEAD/styleguide/java/java.md
        (r".*\.java$", 100),
        # Rust's line length limit is 100 chars.
        (r".*\.rs$", 100),
    ]

    MAX_LEN = 80

    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit, relative=True),
        included + COMMON_INCLUDED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS + LONG_LINE_OK_PATHS,
    )

    error_list = []
    for afile in files:
        skip_regexps = (
            r"https?://",
            r"^#\s*(define|include|import|pragma|if|ifndef|endif)\b",
        )

        max_len = MAX_LEN

        for expr, length in DEFAULT_MAX_LENGTHS:
            if re.search(expr, afile):
                max_len = length
                break

        if os.path.basename(afile).startswith("OWNERS"):
            # File paths can get long, and there's no way to break them up into
            # multiple lines.
            skip_regexps += (
                r"^include\b",
                r"file:",
            )

        skip_regexps = [re.compile(x) for x in skip_regexps]
        for line_num, line in _get_file_diff(afile, commit):
            # Allow certain lines to exceed the maxlen rule.
            if len(line) <= max_len or any(
                x.search(line) for x in skip_regexps
            ):
                continue

            error_list.append(
                "%s, line %s, %s chars, over %s chars"
                % (afile, line_num, len(line), max_len)
            )
            if len(error_list) == 5:  # Just show the first 5 errors.
                break

    if error_list:
        msg = "Found lines longer than the limit (first 5 shown):"
        return errors.HookFailure(msg, error_list)
    return None


def _check_no_stray_whitespace(_project, commit, options=()):
    """Checks that there is no stray whitespace at source lines end."""
    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit),
        included + COMMON_INCLUDED_PATHS + [r"^.*\.md$"],
        excluded + COMMON_EXCLUDED_PATHS,
    )
    return _check_lines_in_diff(
        commit,
        files,
        lambda line: line.rstrip() != line,
        "Found line ending with white space in:",
    )


def _check_no_tabs(_project, commit, options=()):
    """Checks there are no unexpanded tabs."""
    # Don't add entire repos here.  Update the PRESUBMIT.cfg in each repo
    # instead.  We only allow known specific filetypes here that show up in all
    # repos.
    TAB_OK_PATHS = [
        r".*\.ebuild$",
        r".*\.eclass$",
        r".*\.go$",
        r".*/[M|m]akefile$",
        r".*\.mk$",
    ]

    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit),
        included + COMMON_INCLUDED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS + TAB_OK_PATHS,
    )
    return _check_lines_in_diff(
        commit, files, lambda line: "\t" in line, "Found a tab character in:"
    )


def _read_terms_file(terms_file):
    """Read list of words from file, skipping comments and blank lines."""
    file_terms = set()
    for line in osutils.ReadFile(terms_file).splitlines():
        # Allow comment and blank lines.
        line = line.split("#", 1)[0]
        if not line:
            continue
        file_terms.add(line)
    return file_terms


def _check_keywords_in_file(
    project, commit, file, keywords, unblocked_terms_file, opts
):
    """Checks there are no blocked keywords in a file being changed."""
    if file:
        # Search for UNBLOCKED_TERMS_FILE in the parent directories of the file
        # being changed.
        d = os.path.dirname(file)
        while d != project.dir:
            terms_file = os.path.join(d, UNBLOCKED_TERMS_FILE)
            if os.path.isfile(terms_file):
                unblocked_terms_file = terms_file
                break
            d = os.path.dirname(d)

    # Read unblocked word list.
    unblocked_words = _read_terms_file(unblocked_terms_file)
    unblocked_words.update(opts.unblock)
    keywords = sorted(keywords - unblocked_words)

    def _check_line(line):
        # Store information about each span matching blocking regex.
        # to match unblocked regex with blocked reg ex match.
        # [{'span':re.span,    - overlap of matching regex in line
        #   'group':re.group,  - matching term
        #   'blocked':bool,    - whether matching is blocked
        #   'keyword':regex,   - block regex
        #  }, ...]
        blocked_span = []
        # Store information about each span matching unblocking regex.
        # [re.span, ...]
        unblocked_span = []

        # Ignore lines that end with nocheck, typically in a comment.
        # This enables devs to bypass this check line by line.
        if line.endswith(" nocheck") or line.endswith(" nocheck */"):
            return False

        for word in keywords:
            for match in re.finditer(word, line, flags=re.I):
                blocked_span.append(
                    {
                        "span": match.span(),
                        "group": match.group(0),
                        "blocked": True,
                        "keyword": word,
                    }
                )

        for unblocked in unblocked_words:
            for match in re.finditer(unblocked, line, flags=re.I):
                unblocked_span.append(match.span())

        # Unblock terms that are superset of blocked terms:
        #   blocked := "this.?word"
        #   unblocked := "\.this.?word"
        # "this line is blocked because of this1word"
        # "this line is unblocked because of thenew.this1word"
        #
        for b in blocked_span:
            for ub in unblocked_span:
                if ub[0] <= b["span"][0] and ub[1] >= b["span"][1]:
                    b["blocked"] = False
            if b["blocked"]:
                return f'Matched "{b["group"]}" with regex of "{b["keyword"]}"'
        return False

    if file:
        return _check_lines_in_diff(
            commit, [file], _check_line, "Found a blocked keyword in:"
        )

    line_num = 1
    commit_desc_errors = []
    for line in _get_commit_desc(commit).splitlines():
        result = _check_line(line)
        if result:
            commit_desc_errors.append(
                "Commit message, line %s: %s" % (line_num, result)
            )
        line_num += 1
    if commit_desc_errors:
        return errors.HookFailure(
            "Found a blocked keyword in:", commit_desc_errors
        )

    return None


def _check_keywords(project, commit, options=()):
    """Checks there are no blocked keywords in commit content."""
    # Read options from override list.
    parser = argparse.ArgumentParser()
    parser.add_argument("--exclude_regex", action="append", default=[])
    parser.add_argument("--include_regex", action="append", default=[])
    parser.add_argument("--block", action="append", default=[])
    parser.add_argument("--unblock", action="append", default=[])
    opts = parser.parse_args(options)

    # Leave patches with an upstream source alone, we don't want to have
    # divergence in these cases so terms can be ignored from such patches.
    upstream_prefixes = ("UPSTREAM:", "FROMGIT:", "BACKPORT:", "FROMLIST:")
    desc = _get_commit_desc(commit)
    if desc.startswith(upstream_prefixes):
        return ()

    # Read blocked word list.
    blocked_terms_file = REPOHOOKS_DIR / BLOCKED_TERMS_FILE
    common_keywords = _read_terms_file(blocked_terms_file)

    # Find unblocked word list in project root directory. If not found, global
    # list is used.
    unblocked_terms_file = REPOHOOKS_DIR / UNBLOCKED_TERMS_FILE
    if os.path.isfile(os.path.join(project.dir, UNBLOCKED_TERMS_FILE)):
        unblocked_terms_file = os.path.join(project.dir, UNBLOCKED_TERMS_FILE)

    keywords = set(common_keywords | set(opts.block))

    files = _filter_files(
        _get_affected_files(commit),
        opts.include_regex + COMMON_INCLUDED_PATHS + [r"^.*\.md$"],
        opts.exclude_regex + COMMON_EXCLUDED_PATHS,
    )

    error_list = []
    for file in files:
        errs = _check_keywords_in_file(
            project, commit, file, keywords, unblocked_terms_file, opts
        )
        if errs:
            error_list.append(errs)

    errs = _check_keywords_in_file(
        project, commit, None, keywords, unblocked_terms_file, opts
    )
    if errs:
        error_list.append(errs)

    return error_list


def _check_tabbed_indents(_project, commit, options=()):
    """Checks that indents use tabs only."""
    TABS_REQUIRED_PATHS = [
        r".*\.ebuild$",
        r".*\.eclass$",
    ]
    LEADING_SPACE_RE = re.compile("[\t]* ")

    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit),
        included + TABS_REQUIRED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS,
    )
    return _check_lines_in_diff(
        commit,
        files,
        lambda line: LEADING_SPACE_RE.match(line) is not None,
        "Found a space in indentation (must be all tabs):",
    )


class CargoClippyArgumentParserError(Exception):
    """An exception indicating an invalid check_cargo_clippy option."""


class CargoClippyArgumentParser(argparse.ArgumentParser):
    """A argument parser for check_cargo_clippy."""

    def error(self, message):
        raise CargoClippyArgumentParserError(message)


# A cargo project in which clippy runs.
ClippyProject = collections.namedtuple("ClippyProject", ("root", "script"))


class _AddClippyProjectAction(argparse.Action):
    """A callback that adds a cargo clippy setting.

    It accepts a value which is in the form of "ROOT[:SCRIPT]".
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is None:
            setattr(namespace, self.dest, [])
        spec = values.split(":", 1)
        if len(spec) == 1:
            spec += [None]

        if spec[0].startswith("/"):
            raise CargoClippyArgumentParserError(
                'root path must not start with "/"' f' but "{spec[0]}"'
            )

        clippy = ClippyProject(root=spec[0], script=spec[1])
        getattr(namespace, self.dest).append(clippy)


def _get_cargo_clippy_parser():
    """Creates a parser for check_cargo_clippy options."""

    parser = CargoClippyArgumentParser()
    parser.add_argument("--project", action=_AddClippyProjectAction, default=[])

    return parser


def _run_clippy_on_dir(root, cmd, error_list):
    """Runs clippy with cargo clean if needed.

    If clippy fails with E0460, cargo clean is run, and clippy is run again.

    If cmd is empty the default is used.
    If clippy fails, a HookFailure will be appended to error_list.
    """
    # The cwd kwarg for run_command is replaced with src/scripts when entering
    # the chroot, so specify the manifest path to cargo as a workaround since it
    # works both inside and outside the chroot.
    manifest_flag = f"--manifest-path={path_util.ToChrootPath(root)}/Cargo.toml"

    # Overwrite the clippy command if a project-specific script is specified.
    if not cmd:
        cmd = [
            "cargo",
            "clippy",
            "--all-features",
            "--all-targets",
            "--workspace",
            manifest_flag,
            "--",
            "-D",
            "warnings",
        ]

    # If Cargo.lock isn't tracked by git, regenerate it. This fixes the case a
    # dependency has been updated since the last time the presubmit checks
    # were executed. The errors look like:
    #   "error: failed to select a version for the requirement ..."
    try:
        _run_command(
            ["git", "ls-files", "--error-unmatch", "Cargo.lock"],
            check=True,
            cwd=root,
            stderr=subprocess.STDOUT,
        )
    except cros_build_lib.RunCommandError:
        _run_command(
            ["cargo", "generate-lockfile", manifest_flag],
            enter_chroot=True,
            chroot_args=["--no-update"],
        )

    tries = 0
    while tries < 2:
        output = _run_command(
            cmd,
            cwd=root,
            enter_chroot=True,
            chroot_args=["--no-update"],
            stderr=subprocess.STDOUT,
        )
        error = re.search(r"^error(\[E[0-9]+\])?:", output, flags=re.MULTILINE)
        tries += 1
        if error:
            # E0460 indicates the build is stale and clean needs to be run, but
            # only act on it for the first try.
            if tries < 2 and error.group(1) == "[E0460]":
                _run_command(
                    ["cargo", "clean", manifest_flag],
                    enter_chroot=True,
                    chroot_args=["--no-update"],
                )
                continue
            else:
                # An unexpected error so return it without retrying.
                msg = output[error.start() :]
                error_list.append(errors.HookFailure(msg))
        return


def _check_cargo_clippy(project, commit, options=()):
    """Checks that a change doesn't produce cargo-clippy errors."""

    options = list(options)
    if not options:
        return None
    parser = _get_cargo_clippy_parser()

    try:
        opts = parser.parse_args(options)
    except CargoClippyArgumentParserError as e:
        return [
            errors.HookFailure(
                "invalid check_cargo_clippy option is given."
                f" Please check PRESUBMIT.cfg is correct: {e}"
            )
        ]
    files = _filter_files(
        _get_affected_files(commit), [r"\.rs$", r"clippy\.toml$"]
    )
    if not files:
        # No rust or clippy config files modified, skip this check.
        return None

    error_list = []
    for clippy in opts.project:
        root = os.path.normpath(os.path.join(project.dir, clippy.root))

        # Check if any file under `root` was modified.
        modified = False
        for f in files:
            if f.startswith(root):
                modified = True
                break
        if not modified:
            continue

        cmd = (
            [os.path.join(project.dir, clippy.script)] if clippy.script else []
        )
        _run_clippy_on_dir(root, cmd, error_list)

    return error_list


# Stores cargo projects in which `cargo clean` ran.
_check_cargo_clippy.cleaned_root = set()


def _get_test_field_re(project):
    """Provide the regular expression that matches the test field.

    Android internal and external projects use "Bug: " in the commit messages to
    track bugs in buganizer, so "Test: " is used instead of "TEST=".
    """
    if project.remote in TAG_COLON_REMOTES:
        return r"\nTest: \S+"
    else:
        return r"\nTEST=\S+"


def _check_change_has_test_field(project, commit):
    """Check for a non-empty 'TEST=' field in the commit message."""
    SEE_ALSO = "Please review the documentation:\n%s" % (DOC_COMMIT_MSG_URL,)

    TEST_FIELD_RE = _get_test_field_re(project)
    if not re.search(TEST_FIELD_RE, _get_commit_desc(commit)):
        tag = "Test:" if project.remote in TAG_COLON_REMOTES else "TEST="
        msg = "Changelist description needs %s field (after first line)\n%s" % (
            tag,
            SEE_ALSO,
        )
        return errors.HookFailure(msg)
    return None


def _check_change_has_valid_cq_depend(_project, commit):
    """Check for a correctly formatted Cq-Depend field in the commit message."""
    desc = _get_commit_desc(commit)
    msg = "Changelist has invalid Cq-Depend target."
    example = "Example: Cq-Depend: chromium:1234, chrome-internal:2345"
    try:
        patch.GetPaladinDeps(desc)
    except ValueError as ex:
        return errors.HookFailure(msg, [example, str(ex)])
    # Check that Cq-Depend is in the same paragraph as Change-Id.
    msg = "Cq-Depend is not in the same paragraph as Change-Id."
    paragraphs = desc.split("\n\n")
    for paragraph in paragraphs:
        if re.search(r"^Cq-Depend:", paragraph, re.M) and not re.search(
            "^Change-Id:", paragraph, re.M
        ):
            return errors.HookFailure(msg)

    # Check that Cq-Depend is not multi-line.
    msg = "Cq-Depend cannot span across multiple lines."
    if re.search(r"^Cq-Depend:.*,\s*\n", desc, re.M):
        return errors.HookFailure(msg)

    # We no longer support CQ-DEPEND= lines.
    if re.search(r"^CQ-DEPEND[=:]", desc, re.M):
        return errors.HookFailure(
            "CQ-DEPEND= is no longer supported.  Please see:\n"
            "https://www.chromium.org/chromium-os/developer-library/guides"
            "/development/contributing/#cl-dependencies"
        )

    return None


def _check_change_is_contribution(_project, commit):
    """Check that the change is a contribution."""
    NO_CONTRIB = "not a contribution"
    if NO_CONTRIB in _get_commit_desc(commit).lower():
        msg = (
            "Changelist is not a contribution, this cannot be accepted.\n"
            'Please remove the "%s" text from the commit message.'
        ) % NO_CONTRIB
        return errors.HookFailure(msg)
    return None


def _check_change_has_bug_field(project, commit):
    """Check for a correctly formatted 'BUG=' field in the commit message."""
    SEE_ALSO = "Please review the documentation:\n%s" % (DOC_COMMIT_MSG_URL,)

    OLD_BUG_RE = r"\n(BUG|FIXED)=.*chromium-os"
    if re.search(OLD_BUG_RE, _get_commit_desc(commit)):
        msg = (
            "The chromium-os bug tracker is now deprecated. Please use\n"
            "the chromium tracker in your BUG= or FIXED= line now."
        )
        return errors.HookFailure(msg)

    # Android internal and external projects use "Bug: " to track bugs in
    # buganizer.
    if project.remote in TAG_COLON_REMOTES:
        BUG_RE = r"(\nBug: ?([Nn]one|\d+)|\nFixed: ?\d+)"
        if not re.search(BUG_RE, _get_commit_desc(commit)):
            msg = (
                "Changelist description needs Bug field (after first line):\n"
                "Examples:\n"
                "Bug: 9999 (for buganizer)\n"
                "Fixed: 9999\n"
                "Bug: None\n%s" % (SEE_ALSO,)
            )
            return errors.HookFailure(msg)
    else:
        BUG_BARE_NUMBER_RE = r"\n(BUG|FIXED)=(\d+)"
        bare_bug = re.search(BUG_BARE_NUMBER_RE, _get_commit_desc(commit))
        if bare_bug:
            msg = (
                "BUG/FIXED field in changelist description missing chromium: "
                "or b: prefix.\nExamples:\n"
                "BUG=chromium:%s (for public tracker)\n"
                "BUG=b:%s (for buganizer)\n%s"
                % (
                    bare_bug[1],
                    bare_bug[1],
                    SEE_ALSO,
                )
            )
            return errors.HookFailure(msg)

        BUG_RE = (
            r"(\nBUG=([Nn]one|(chromium|b|oss-fuzz):\d+)|"
            r"\nFIXED=((chromium|b|oss-fuzz):\d+))"
        )
        if not re.search(BUG_RE, _get_commit_desc(commit)):
            msg = (
                "Changelist description needs BUG or FIXED field "
                "(after first line):\n"
                "Examples:\n"
                "BUG=chromium:9999 (for public tracker)\n"
                "BUG=b:9999 (for buganizer)\n"
                "FIXED=b:9999\n"
                "BUG=oss-fuzz:9999 (for oss-fuzz)\n"
                "BUG=None\n%s" % (SEE_ALSO,)
            )
            return errors.HookFailure(msg)

    TEST_BEFORE_BUG_RE = _get_test_field_re(project) + r".*" + BUG_RE

    if re.search(TEST_BEFORE_BUG_RE, _get_commit_desc(commit), re.DOTALL):
        msg = "The BUG or FIXED field must come before the TEST field.\n%s" % (
            SEE_ALSO,
        )
        return errors.HookFailure(msg)

    return None


def _check_change_no_include_oem(project, commit):
    """Check that the change does not reference OEMs."""
    ALLOWLIST = {
        "chromiumos/platform/ec",
        # Used by unit tests.
        "project",
    }
    if project.name not in ALLOWLIST:
        return None

    TAGS = {
        "Reviewed-on",
        "Reviewed-by",
        "Signed-off-by",
        "Commit-Ready",
        "Tested-by",
        "Commit-Queue",
        "Acked-by",
        "Modified-by",
        "CC",
        "Suggested-by",
        "Reported-by",
        "Acked-for-chrome-by",
        "Cq-Cl-Tag",
        "Cq-Include-Trybots",
    }

    # Ignore tags, which could reasonably contain OEM names
    # (e.g. Reviewed-by: foo@oem.corp-partner.google.com).
    commit_message = " ".join(
        x
        for x in _get_commit_desc(commit).splitlines()
        if ":" not in x or x.split(":", 1)[0] not in TAGS
    )

    commit_message = re.sub(r"[\s_-]+", " ", commit_message)

    # Exercise caution when expanding these lists. Adding a name
    # could indicate a new relationship with a company!
    OEMS = [
        "hp",
        "hewlett packard",
        "dell",
        "lenovo",
        "acer",
        "asus",
        "samsung",
    ]
    ODMS = [
        "bitland",
        "compal",
        "haier",
        "huaqin",
        "inventec",
        "lg",
        "pegatron",
        "pegatron(ems)",
        "quanta",
        "samsung",
        "wistron",
    ]

    for name_type, name_list in [("OEM", OEMS), ("ODM", ODMS)]:
        # Construct regex
        name_re = r"\b(%s)\b" % "|".join([re.escape(x) for x in name_list])
        matches = [
            x[0] for x in re.findall(name_re, commit_message, re.IGNORECASE)
        ]
        if matches:
            # If there's a match, throw an error.
            error_msg = (
                "Changelist description contains the name of an"
                ' %s: "%s".' % (name_type, '","'.join(matches))
            )
            return errors.HookFailure(error_msg)

    return None


def match_board_phases(line):
    """Helper function to identify lines that contain board phases."""

    BOARD_PHASES = [r"proto\d+", "evt", "dvt", "pvt"]

    # Construct regex.
    name_re = r"\b(%s)\b" % "|".join(BOARD_PHASES)
    # Some examples:
    # 'Enabling ABC on XYZ EVT and PVT boards'      -> ['EVT', 'PVT']
    # 'Rename acpi_gpio_evt_pin to acpi_gpio_event' -> []
    matches = re.findall(name_re, line, re.IGNORECASE)

    return ",".join(matches)


def _check_change_no_include_board_phase(_project, commit):
    """Check that the change does not reference board phases."""

    commit_message = _get_commit_desc(commit).replace("\n", " ")
    commit_message = re.sub(r"[\s-]+", " ", commit_message)

    matches = match_board_phases(commit_message)
    if matches:
        # If there's a match, throw an error.
        error_msg = (
            "Changelist description contains the name of a"
            f" board phase: {matches}."
        )
        return errors.HookFailure(error_msg)

    return None


def _check_for_uprev(_project, commit, project_top=None):
    """Check that we're not missing a revbump of an ebuild in the given commit.

    If the given commit touches files in a directory that has ebuilds somewhere
    up the directory hierarchy, it's very likely that we need an ebuild revbump
    in order for those changes to take effect. Try to detect those situations
    and warn if there wasn't a version or revision bump.

    Args:
        _project: The Project to look at
        commit: The commit to look at
        project_top: Top dir to process commits in

    Returns:
        A errors.HookFailure or None.
    """

    def FinalName(obj):
        # If the file is being deleted, then the dst_file is not set.
        if obj.dst_file is None:
            return obj.src_file
        else:
            return obj.dst_file

    def AllowedPath(obj):
        allowed_files = {
            "ChangeLog",
            "DIR_METADATA",
            "Manifest",
            "METADATA",
            "metadata.xml",
            "OWNERS",
            "README.md",
        }
        allowed_directories = {"profiles"}

        affected = Path(FinalName(obj))
        if affected.name in allowed_files:
            return True

        for directory in allowed_directories:
            if directory in affected.parts:
                return True

        return False

    affected_path_objs = _get_affected_files(
        commit,
        include_deletes=True,
        include_symlinks=True,
        relative=True,
        full_details=True,
    )

    # Don't yell about changes to allowed files or directories...
    affected_path_objs = [x for x in affected_path_objs if not AllowedPath(x)]
    if not affected_path_objs:
        return None

    # If we're creating new ebuilds from scratch or renaming them, then we don't
    # need an uprev. Find all the dirs with new or renamed ebuilds and ignore
    # their files/.
    ebuild_dirs = [
        os.path.dirname(FinalName(x)) + "/"
        for x in affected_path_objs
        if FinalName(x).endswith(".ebuild") and x.status in ("A", "R")
    ]
    affected_path_objs = [
        obj
        for obj in affected_path_objs
        if not any(FinalName(obj).startswith(x) for x in ebuild_dirs)
    ]
    if not affected_path_objs:
        return None

    # We want to examine the current contents of all directories that are
    # parents of files that were touched (up to the top of the project).
    #
    # ...note: we use the current directory contents even though it may have
    # changed since the commit we're looking at.  This is just a heuristic after
    # all.  Worst case we don't flag a missing revbump.
    if project_top is None:
        project_top = os.getcwd()
    dirs_to_check = set([project_top])
    for obj in affected_path_objs:
        path = os.path.join(project_top, os.path.dirname(FinalName(obj)))
        while os.path.exists(path) and not os.path.samefile(path, project_top):
            dirs_to_check.add(path)
            path = os.path.dirname(path)

    # Look through each directory.  If it's got an ebuild in it then we'll
    # consider this as a case when we need a revbump.
    for dir_path in dirs_to_check:
        contents = os.listdir(dir_path)
        ebuilds = [
            os.path.join(dir_path, path)
            for path in contents
            if path.endswith(".ebuild")
        ]
        ebuilds_9999 = [
            path for path in ebuilds if path.endswith("-9999.ebuild")
        ]

        # If we're touching things in the same directory as a -9999.ebuild, the
        # bot will uprev for us.
        if ebuilds_9999:
            continue

        if ebuilds:
            return errors.HookFailure(
                "Changelist probably needs a revbump of an ebuild:\n"
                "%s" % dir_path
            )

    return None


def _check_ebuild_eapi(project, commit):
    """Make sure we have people use EAPI=7 or newer with custom ebuilds.

    We want to get away from older EAPI's as it makes life confusing and they
    have less builtin error checking.

    Args:
        project: The Project to look at
        commit: The commit to look at

    Returns:
        A errors.HookFailure or None.
    """
    # If this is the portage-stable overlay, then ignore the check.  It's rare
    # that we're doing anything other than importing files from upstream, and
    # we shouldn't be rewriting things fundamentally anyways.
    allowlist = ("chromiumos/overlays/portage-stable",)
    if project.name in allowlist:
        return None

    BAD_EAPIS = ("0", "1", "2", "3", "4", "5", "6")

    get_eapi = re.compile(r'^\s*EAPI=[\'"]?([^\'"]+)')

    ebuilds_re = [r"\.ebuild$"]
    ebuilds = _filter_files(
        _get_affected_files(commit, relative=True), ebuilds_re
    )
    bad_ebuilds = []

    for ebuild in ebuilds:
        # If the ebuild does not specify an EAPI, it defaults to 0.
        eapi = "0"

        lines = _get_file_content(ebuild, commit).splitlines()
        if len(lines) == 1:
            # This is most likely a symlink, so skip it entirely.
            continue

        for line in lines:
            m = get_eapi.match(line)
            if m:
                # Once we hit the first EAPI line in this ebuild, stop
                # processing.  The spec requires that there only be one and it
                # be first, so checking all possible values is pointless.  We
                # also assume that it's "the" EAPI line and not something in the
                # middle of a heredoc.
                eapi = m.group(1)
                break

        if eapi in BAD_EAPIS:
            bad_ebuilds.append((ebuild, eapi))

    if bad_ebuilds:
        # pylint: disable=C0301
        url = "https://dev.chromium.org/chromium-os/how-tos-and-troubleshooting/upgrade-ebuild-eapis"
        # pylint: enable=C0301
        return errors.HookFailure(
            "These ebuilds are using old EAPIs.  If these are imported from\n"
            "Gentoo, then you may ignore and upload once with the --no-verify\n"
            "flag.  Otherwise, please update to 7 or newer.\n"
            "\t%s\n"
            "See this guide for more details:\n%s\n"
            % ("\n\t".join(["%s: EAPI=%s" % x for x in bad_ebuilds]), url)
        )

    return None


def _check_ebuild_keywords(_project, commit):
    """Make sure we use the new style KEYWORDS when possible in ebuilds.

    If an ebuild generally does not care about the arch it is running on, then
    ebuilds should flag it with one of:
      KEYWORDS="*"       # A stable ebuild.
      KEYWORDS="~*"      # An unstable ebuild.
      KEYWORDS="-* ..."  # Is known to only work on specific arches.

    Args:
        project: The Project to look at
        commit: The commit to look at

    Returns:
        A errors.HookFailure or None.
    """
    ALLOWLIST = set(("*", "-*", "~*"))

    get_keywords = re.compile(r'^\s*KEYWORDS="(.*)"')

    ebuilds_re = [r"\.ebuild$"]
    ebuilds = _filter_files(
        _get_affected_files(commit, relative=True), ebuilds_re
    )

    bad_ebuilds = []
    for ebuild in ebuilds:
        # We get the full content rather than a diff as the latter does not work
        # on new files (like when adding new ebuilds).
        lines = _get_file_content(ebuild, commit).splitlines()
        for line in lines:
            m = get_keywords.match(line)
            if m:
                keywords = set(m.group(1).split())
                if not keywords or ALLOWLIST - keywords != ALLOWLIST:
                    continue

                bad_ebuilds.append(ebuild)

    if bad_ebuilds:
        return errors.HookFailure(
            "%s\n"
            "Please update KEYWORDS to use a glob:\n"
            "If the ebuild should be marked stable (i.e. non-9999 ebuilds):\n"
            '  KEYWORDS="*"\n'
            "If the ebuild should be marked unstable (i.e. cros-workon / 9999 "
            "ebuilds):\n"
            '  KEYWORDS="~*"\n'
            "If the ebuild needs to be marked for only specific arches, "
            "then use -* like so:\n"
            '  KEYWORDS="-* arm ..."\n' % "\n* ".join(bad_ebuilds)
        )

    return None


def _check_ebuild_licenses(_project, commit):
    """Check if the LICENSE field in the ebuild is correct."""
    affected_paths = _get_affected_files(commit, relative=True)
    touched_ebuilds = [x for x in affected_paths if x.endswith(".ebuild")]

    # A list of licenses to ignore for now.
    LICENSES_IGNORE = ["||", "(", ")"]

    error_list = []
    for ebuild in touched_ebuilds:
        # e.g. path/to/overlay/category/package/package.ebuild ->
        #      path/to/overlay
        overlay_path = os.sep.join(ebuild.split(os.sep)[:-3])
        category = ebuild.split(os.sep)[-3]

        try:
            ebuild_content = _get_file_content(ebuild, commit)
            license_types = licenses_lib.GetLicenseTypesFromEbuild(
                ebuild_content, overlay_path
            )
        except ValueError as e:
            if category not in {"acct-group", "acct-user"}:
                error_list.append(errors.HookFailure(str(e), [ebuild]))
            continue

        # Virtual packages must use "metapackage" license.
        if category == "virtual":
            if license_types != ["metapackage"]:
                error_list.append(
                    errors.HookFailure(
                        'Virtual package must use LICENSE="metapackage".',
                        [ebuild],
                    )
                )
                continue
        elif category in {"acct-group", "acct-user"}:
            if license_types:
                error_list.append(
                    errors.HookFailure(
                        "Account packages must not set LICENSE.",
                        [ebuild],
                    )
                )
                continue

        # Also ignore licenses ending with '?'
        for license_type in [
            x
            for x in license_types
            if x not in LICENSES_IGNORE and not x.endswith("?")
        ]:
            try:
                licenses_lib.Licensing.FindLicenseType(
                    license_type, overlay_path=overlay_path
                )
            except AssertionError as e:
                error_list.append(errors.HookFailure(str(e), [ebuild]))
                continue

    return error_list


def _check_ebuild_owners(project, commit):
    """Require all packages include an OWNERS file."""
    affected_files_objs = _get_affected_files(
        commit, include_symlinks=True, relative=True, full_details=True
    )

    # If this CL doesn't include any ebuilds, don't bother complaining.
    ebuilds = [x for x in affected_files_objs if x.src_file.endswith(".ebuild")]
    if not ebuilds:
        return None

    # Check each package dir.
    packages_missing_owners = []
    package_dirs = sorted(set(os.path.dirname(x.src_file) for x in ebuilds))
    for package_dir in package_dirs:
        # See if there's an OWNERS file in there already.
        data = _get_file_content(os.path.join(package_dir, "OWNERS"), commit)
        if not data:
            # Allow categories to declare OWNERS.  Some are owned by teams.
            category = os.path.dirname(package_dir)
            data = _get_file_content(os.path.join(category, "OWNERS"), commit)
            if not data:
                # Allow specific overlays to declare OWNERS for all packages.
                if (
                    project.name == "chromiumos/overlays/board-overlays"
                    or re.match(
                        (
                            r"^chromeos/overlays/"
                            r"(baseboard|chipset|project|overlay)-"
                        ),
                        project.name,
                    )
                ):
                    overlay = os.path.dirname(category)
                    data = _get_file_content(
                        os.path.join(overlay, "OWNERS"), commit
                    )

        if not data:
            packages_missing_owners.append(package_dir)
            continue

        # Require specific people and not just *.
        lines = {x for x in data.splitlines() if x.split("#", 1)[0].strip()}
        if not lines - {"*"}:
            packages_missing_owners.append(package_dir)

    if packages_missing_owners:
        return errors.HookFailure(
            "All packages must have an OWNERS file filled out.",
            packages_missing_owners,
        )

    return None


def _check_ebuild_r0(_project, commit):
    """Do not allow ebuilds to end with -r0 versions."""
    ebuilds = _filter_files(
        _get_affected_files(commit, include_symlinks=True, relative=True),
        (r"-r0\.ebuild$",),
    )
    if ebuilds:
        return errors.HookFailure(
            "The -r0 in ebuilds is redundant and confusing. Simply remove it.\n"
            "For example: git mv foo-1.0-r0.ebuild foo-1.0.ebuild",
            ebuilds,
        )

    return None


def _check_ebuild_virtual_pv(project, commit):
    """Enforce the virtual PV policies."""
    # If this is the portage-stable overlay, then ignore the check.
    # We want to import virtuals as-is from upstream Gentoo.
    allowlist = ("chromiumos/overlays/portage-stable",)
    if project.name in allowlist:
        return None

    # Per-project listings of packages with virtuals known to come from upstream
    # Gentoo, so we shouldn't complain about them.
    if project.name == "chromiumos/overlays/chromiumos-overlay":
        pkg_allowlist = ("rust",)
    else:
        pkg_allowlist = ()

    # We assume the repo name is the same as the dir name on disk.
    # It would be dumb to not have them match though.
    project_base = os.path.basename(project.name)

    is_variant = lambda x: x.startswith("overlay-variant-")
    is_board = lambda x: x.startswith("overlay-")
    is_baseboard = lambda x: x.startswith("baseboard-")
    is_chipset = lambda x: x.startswith("chipset-")
    is_project = lambda x: x.startswith("project-")
    is_private = lambda x: x.endswith("-private")
    is_chromeos = lambda x: x == "chromeos-overlay"

    is_special_overlay = lambda x: (
        is_board(x) or is_chipset(x) or is_baseboard(x) or is_project(x)
    )

    get_pv = re.compile(r"(.*?)virtual/([^/]+)/\2-([^/]*)\.ebuild$")

    ebuilds_re = [r"\.ebuild$"]
    ebuilds = _filter_files(
        _get_affected_files(commit, relative=True), ebuilds_re
    )
    bad_ebuilds = []

    for ebuild in ebuilds:
        m = get_pv.match(ebuild)
        if not m:
            continue

        overlay, package_name, pv = m.groups()
        if package_name in pkg_allowlist:
            continue

        pv = pv.split("-", 1)[0]
        if not overlay or not is_special_overlay(overlay):
            overlay = project_base

        # Virtual versions >= 4 are special cases used above the standard
        # versioning structure, e.g. if one has a board inheriting a board.
        if pv[0] >= "4":
            want_pv = pv
        elif is_board(overlay):
            if is_private(overlay):
                want_pv = "3.5" if is_variant(overlay) else "3"
            elif is_board(overlay):
                want_pv = "2.5" if is_variant(overlay) else "2"
        elif is_baseboard(overlay):
            want_pv = "1.9.5" if is_private(overlay) else "1.9"
        elif is_chipset(overlay):
            want_pv = "1.8.5" if is_private(overlay) else "1.8"
        elif is_project(overlay):
            want_pv = "1.7" if is_private(overlay) else "1.5"
        elif is_chromeos(overlay):
            want_pv = "1.3"
        else:
            want_pv = "1"

        if pv != want_pv:
            bad_ebuilds.append((ebuild, pv, want_pv))

    if bad_ebuilds:
        # pylint: disable=C0301
        url = "https://www.chromium.org/chromium-os/developer-library/guides/portage/ebuild-faq/"
        # pylint: enable=C0301
        return errors.HookFailure(
            "These virtuals have incorrect package versions (PVs). Please "
            "adjust:\n\t%s\n"
            "If this is an upstream Gentoo virtual, then you may ignore this\n"
            "check (and re-run w/--no-verify). Otherwise, please see this\n"
            "page for more details:\n%s\n"
            % (
                "\n\t".join(
                    [
                        "%s:\n\t\tPV is %s but should be %s" % x
                        for x in bad_ebuilds
                    ]
                ),
                url,
            )
        )

    return None


def _check_ebuild_localname_exists(_project, commit):
    """Validate CROS_WORKON_LOCALNAME values."""
    ebuilds_re = [r"-9999\.ebuild$"]
    ebuilds = _filter_files(
        _get_affected_files(commit, relative=True), ebuilds_re
    )

    bad_localnames = []
    tempdir = osutils.TempDir()
    for ebuild in ebuilds:
        ebuild_path = os.path.join(tempdir.tempdir, ebuild)
        osutils.WriteFile(
            ebuild_path, _get_file_content(ebuild, commit), makedirs=True
        )

        try:
            ebuild_obj = portage_util.EBuild(ebuild_path)
            workon_vars = portage_util.EBuild.GetCrosWorkonVars(
                ebuild_path, ebuild_obj.pkgname
            )
        except portage_util.EbuildFormatIncorrectError:
            # Skip ebuilds we can't read for now.
            continue

        if ebuild_obj.category != "chromeos-base":
            # Only checking chromeos-base until we remove the implicit
            # third-party from non-chromeos-base packages.
            continue
        if not workon_vars.localname:
            # This is a problem itself, but not what we're checking for here.
            continue

        # Localnames are relative to src/.
        src_path = Path("src")
        base_path = Path(constants.SOURCE_ROOT) / src_path
        for path in workon_vars.localname:
            full_path = base_path / path
            if full_path.exists():
                # Exists, move to the next one.
                continue

            # Check the manifest for an entry at the given path.
            checkout = git.ManifestCheckout.Cached(__file__)
            pkg_path = src_path / path
            if str(pkg_path) in checkout.checkouts_by_path:
                # Found the exact path.
                continue

            # Check if the LOCALNAME entry is in any of the manifest's project
            # paths.
            for checkout_path in checkout.checkouts_by_path:
                try:
                    pkg_path.relative_to(checkout_path)
                    break
                except ValueError:
                    continue
            else:
                # Never hit the break, not in any of the manifest projects.
                bad_localnames.append((ebuild, path, pkg_path))

    if bad_localnames:
        return errors.HookFailure(
            "The following ebuilds have the given CROS_WORKON_LOCALNAME values "
            "that do not exist at the expected location.\n"
            "chromeos-base packages should be relative to src/.\n\n"
            "%s"
            % "\n".join(
                f'\t{e}: "{p}" not found at "{f}"' for e, p, f in bad_localnames
            )
        )

    return None


def _check_portage_make_use_var(_project, commit):
    """Verify that $USE is set correctly in make.conf and make.defaults."""
    files = _filter_files(
        _get_affected_files(commit, relative=True),
        [r"(^|/)make.(conf|defaults)$"],
    )

    error_list = []
    for path in files:
        basename = os.path.basename(path)

        # Has a USE= line already been encountered in this file?
        saw_use = False

        for i, line in enumerate(
            _get_file_content(path, commit).splitlines(), 1
        ):
            if not line.startswith("USE="):
                continue

            preserves_use = "${USE}" in line or "$USE" in line

            if (
                basename == "make.conf"
                or (basename == "make.defaults" and saw_use)
            ) and not preserves_use:
                error_list.append("%s:%d: missing ${USE}" % (path, i))
            elif basename == "make.defaults" and not saw_use and preserves_use:
                error_list.append(
                    "%s:%d: ${USE} referenced in initial declaration"
                    % (path, i)
                )

            saw_use = True

    if error_list:
        return errors.HookFailure(
            "One or more Portage make files appear to set USE incorrectly.\n"
            "\n"
            "All USE assignments in make.conf and all assignments after the\n"
            'initial declaration in make.defaults should contain "${USE}" to\n'
            "preserve previously-set flags.\n"
            "\n"
            "The initial USE declaration in make.defaults should not contain\n"
            '"${USE}".\n',
            error_list,
        )

    return None


def _check_change_has_proper_changeid(_project, commit):
    """Verify that Change-ID is present in last paragraph of commit message."""
    CHANGE_ID_RE = r"\nChange-Id: I[a-f0-9]+\n"
    desc = _get_commit_desc(commit)
    m = re.search(CHANGE_ID_RE, desc)
    if not m:
        return errors.HookFailure(
            "Last paragraph of description must include Change-Id."
        )

    # Allow a-o-b and some other tags to follow Change-ID in the footer.
    allowed_tags = ["Signed-off-by", "Cq-Cl-Tag", "Cq-Include-Trybots"]

    end = desc[m.end() :].strip().splitlines()
    cherry_pick_marker = "cherry picked from commit"

    if end and cherry_pick_marker in end[-1]:
        # Cherry picked patches allow more tags in the last paragraph.
        allowed_tags += [
            "Auto-Submit",
            "Commit-Queue",
            "Commit-Ready",
            "Owners-Override",
            "Reviewed-by",
            "Reviewed-on",
            "Tested-by",
        ]
        end = end[:-1]

    # Note that descriptions could have multiple cherry pick markers.
    tag_search = r"^(%s:|\(%s) " % (":|".join(allowed_tags), cherry_pick_marker)

    if [x for x in end if not re.search(tag_search, x)]:
        return errors.HookFailure(
            'Only "%s:" tag(s) may follow the Change-Id.'
            % ':", "'.join(allowed_tags)
        )

    return None


def _check_commit_message_style(_project, commit):
    """Verify that the commit message matches our style.

    We do not check for BUG=/TEST=/etc... lines here as that is handled by other
    commit hooks.
    """
    SEE_ALSO = "Please review the documentation:\n%s" % (DOC_COMMIT_MSG_URL,)

    desc = _get_commit_desc(commit)

    # The first line should be by itself.
    lines = desc.splitlines()
    if len(lines) > 1 and lines[1]:
        return errors.HookFailure(
            "The second line of the commit message must be blank."
            "\n%s" % (SEE_ALSO,)
        )

    # The first line should be one sentence.
    if re.search(r"[^\.]\. ", lines[0]):
        return errors.HookFailure(
            "The first line cannot be more than one sentence.\n%s" % (SEE_ALSO,)
        )

    # The first line cannot be too long.
    MAX_FIRST_LINE_LEN = 100
    first_line = lines[0]
    if len(first_line) > MAX_FIRST_LINE_LEN:
        return errors.HookFailure(
            "The first line must be less than %i chars.\n%s"
            % (MAX_FIRST_LINE_LEN, SEE_ALSO)
        )

    # Don't allow random git keywords.
    if first_line.startswith("fixup!") or first_line.startswith("squash!"):
        return errors.HookFailure(
            "Git fixup/squash commit detected: rebase your local branch, or "
            "cleanup the commit message"
        )

    return None


def _check_cros_license(_project, commit, options=()):
    """Verifies the ChromiumOS license/copyright header.

    Should be following the spec:
    https://chromium.googlesource.com/chromium/src/+/HEAD/styleguide/c++/c++.md#file-headers
    """
    # For older years, be a bit more flexible as our policy says leave them be.
    # Change references.
    # b/230609017: Chromium OS -> ChromiumOS and remove all rights reserved.
    LICENSE_HEADER = (
        # Line 1 - copyright.
        r".*Copyright(?P<copyright> \(c\))? "
        r"(?P<year>20[0-9]{2})(?:-20[0-9]{2})? "
        r"The Chromium(?P<chromium_space_os> )?OS Authors(?P<period>\.)?"
        r"(?P<rights_reserved> All rights reserved\.)?\n"
        # Line 2 - License.
        r".*Use of this source code is governed by a BSD-style license that "
        r"can be\n"
        # Line 3 - License continuation.
        r".*found in the LICENSE file\.\n"
    )
    license_re = re.compile(LICENSE_HEADER, re.MULTILINE)

    included, excluded = _parse_common_inclusion_options(options)

    bad_files = []
    bad_copyright_files = []
    bad_year_files = []
    bad_chromiumos_files = []
    bad_rights_reserved_files = []
    bad_period_files = []

    files = _filter_files(
        _get_affected_files(commit, relative=True),
        included + COMMON_INCLUDED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS + LICENSE_EXCLUDED_PATHS,
    )
    existing_files = set(
        _get_affected_files(commit, relative=True, include_adds=False)
    )

    current_year = datetime.datetime.now().year
    for f in files:
        contents = _get_file_content(f, commit)
        if not contents:
            # Ignore empty files.
            continue

        license_match = license_re.search(contents)
        if not license_match:
            bad_files.append(f)
        else:
            new_file = f not in existing_files
            year = int(license_match.group("year"))
            if license_match.group("copyright"):
                bad_copyright_files.append(f)
            if new_file and year != current_year:
                bad_year_files.append(f)
            if license_match.group("chromium_space_os"):
                bad_chromiumos_files.append(f)
            if license_match.group("rights_reserved"):
                bad_rights_reserved_files.append(f)
            if license_match.group("period"):
                bad_period_files.append(f)

    error_list = []
    if bad_files:
        msg = "%s:\n%s\n%s" % (
            "License must match",
            license_re.pattern,
            "Found a bad header in these files:",
        )
        error_list.append(errors.HookFailure(msg, bad_files))
    if bad_copyright_files:
        msg = "Do not use (c) in copyright headers:"
        error_list.append(errors.HookFailure(msg, bad_copyright_files))
    if bad_year_files:
        msg = "Use current year (%s) in copyright headers in new files:" % (
            current_year
        )
        error_list.append(errors.HookFailure(msg, bad_year_files))
    if bad_chromiumos_files:
        msg = "Use ChromiumOS instead of Chromium OS:"
        error_list.append(errors.HookFailure(msg, bad_chromiumos_files))
    if bad_rights_reserved_files:
        msg = 'Do not include "All rights reserved.":'
        error_list.append(errors.HookFailure(msg, bad_rights_reserved_files))
    if bad_period_files:
        msg = 'Do not include period after "ChromiumOS Authors":'
        error_list.append(errors.HookFailure(msg, bad_period_files))

    return error_list


def _check_aosp_license(_project, commit, options=()):
    """Verifies the AOSP license/copyright header.

    AOSP uses the Apache2 License:
    https://source.android.com/source/licenses.html
    """
    LICENSE_HEADER = (
        r"""^[#/\*]*
[#/\*]* ?Copyright( \([cC]\))? 20[-0-9]{2,7} The Android Open Source Project
[#/\*]* ?
[#/\*]* ?Licensed under the Apache License, Version 2.0 \(the "License"\);
[#/\*]* ?you may not use this file except in compliance with the License\.
[#/\*]* ?You may obtain a copy of the License at
[#/\*]* ?
[#/\*]* ?      http://www\.apache\.org/licenses/LICENSE-2\.0
[#/\*]* ?
[#/\*]* ?Unless required by applicable law or agreed to in writing, software
[#/\*]* ?distributed under the License is distributed on an "AS IS" BASIS,
[#/\*]* ?WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or """
        r"""implied\.
[#/\*]* ?See the License for the specific language governing permissions and
[#/\*]* ?limitations under the License\.
[#/\*]*$
"""
    )
    license_re = re.compile(LICENSE_HEADER, re.MULTILINE)

    included, excluded = _parse_common_inclusion_options(options)

    files = _filter_files(
        _get_affected_files(commit, relative=True),
        included + COMMON_INCLUDED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS + LICENSE_EXCLUDED_PATHS,
    )

    bad_files = []
    for f in files:
        contents = _get_file_content(f, commit)
        if not contents:
            # Ignore empty files.
            continue

        if not license_re.search(contents):
            bad_files.append(f)

    if bad_files:
        msg = (
            "License must match:\n%s\nFound a bad header in these files:"
            % license_re.pattern
        )
        return errors.HookFailure(msg, bad_files)
    return None


def _check_egis_license(_project, commit, options=()):
    """Verifies the Egis license/copyright header."""

    LICENSE_HEADER = (
        # Line 1 - /*
        r"\/\*\n"
        # Line 2
        r" \* Copyright \(c\) (?P<year_range>\d{4}-\d{4}) "
        r"Egis Technology Inc\.\s+<eservice@egistec\.com>\n"
        # Line 3
        r" \*\n"
        # Line 4
        r" \* All rights are reserved.\n"
        # Line 5
        r" \* Proprietary and confidential.\n"
        # Line 6
        r" \* Unauthorized copying of this file, "
        r"via any medium is strictly prohibited.\n"
        # Line 7
        r" \* Any use is subject to an appropriate "
        r"license granted by Egis Technology Inc.\n"
        # Line 8 - */
        r" \*\n"
        # Line 9 - */
        r" \*\/\n"
    )

    license_re = re.compile(LICENSE_HEADER, re.MULTILINE)

    included, excluded = _parse_common_inclusion_options(options)

    bad_files = []
    bad_year_end_files = []
    bad_year_range_files = []

    files = _filter_files(
        _get_affected_files(commit, relative=True),
        included + COMMON_INCLUDED_PATHS,
        excluded + COMMON_EXCLUDED_PATHS + LICENSE_EXCLUDED_PATHS,
    )
    existing_files = set(
        _get_affected_files(commit, relative=True, include_adds=False)
    )

    current_year = datetime.datetime.now().year
    for f in files:
        contents = _get_file_content(f, commit)
        if not contents:
            # Ignore empty files.
            continue

        license_match = license_re.search(contents)
        if not license_match:
            bad_files.append(f)
        else:
            new_file = f not in existing_files
            year_range = license_match.group("year_range")
            start_year, end_year = [int(year) for year in year_range.split("-")]
            year_range = end_year - start_year
            if new_file and end_year != current_year:
                bad_year_end_files.append(f)
            if new_file and year_range != (current_year - 2015):
                bad_year_range_files.append(f)

    error_list = []
    if bad_files:
        msg = (
            f"License must match:\n{license_re.pattern}\n"
            "Found a bad header in these files:"
        )
        error_list.append(errors.HookFailure(msg, bad_files))
    if bad_year_end_files:
        msg = (
            f"Use current year ({current_year}) as the end year "
            "in the copyright end in new files:"
        )
        error_list.append(errors.HookFailure(msg, bad_year_end_files))
    if bad_year_range_files:
        msg = (
            f"Use current year ({current_year}) as the end year and 2015 as "
            "the start year in the copyright range in new files:"
        )
        error_list.append(errors.HookFailure(msg, bad_year_range_files))
    return error_list


def _check_layout_conf(_project, commit):
    """Verifies the metadata/layout.conf file."""
    repo_name = "profiles/repo_name"
    repo_names = []
    layout_path = "metadata/layout.conf"
    layout_paths = []

    # Handle multiple overlays in a single commit (like the public tree).
    for f in _get_affected_files(commit, relative=True):
        if f.endswith(repo_name):
            repo_names.append(f)
        elif f.endswith(layout_path):
            layout_paths.append(f)

    # Disallow new repos with the repo_name file.
    if repo_names:
        return errors.HookFailure(
            '%s: use "repo-name" in %s instead' % (repo_names, layout_path)
        )

    # Gather all the errors in one pass so we show one full message.
    all_errors = {}
    for layout_path in layout_paths:
        data = _get_file_content(layout_path, commit)
        error_list = linters.portage_layout_conf.Data(data, layout_path)
        if formatters.portage_layout_conf.Data(data, layout_path) != data:
            error_list += [
                f"File is not formatted; run `cros format --commit={commit}`"
            ]
        all_errors[layout_path] = error_list

    # Summarize all the errors we saw (if any).
    lines = ""
    for layout_path, error_list in all_errors.items():
        if error_list:
            lines += "\n\t- ".join(["\n* %s:" % layout_path] + error_list)
    if lines:
        lines = (
            "See the portage(5) man page for layout.conf details" + lines + "\n"
        )
        return errors.HookFailure(lines)

    return None


def _check_no_new_gyp(_project, commit):
    """Verifies no project starts to use GYP."""
    gypfiles = _filter_files(
        _get_affected_files(commit, include_symlinks=True, relative=True),
        [r"\.gyp$"],
    )

    if gypfiles:
        return errors.HookFailure(
            "GYP is deprecated and not allowed in a new project:", gypfiles
        )
    return None


# Project-specific hooks


def _run_checkpatch(_project, commit, options=()):
    """Runs checkpatch.pl on the given project"""
    # Bypass checkpatch for upstream or almost upstream commits, since we do not
    # intend to modify the upstream commits when landing them to our branches.
    # Any fixes should sent as independent patches.
    # The check is retained for FROMLIST and BACKPORT commits, as by definition
    # those can be still fixed up.
    desc = _get_commit_desc(commit)
    if desc.startswith("UPSTREAM:") or desc.startswith("FROMGIT:"):
        return None

    options = list(options)
    if options and options[0].startswith("./") and os.path.exists(options[0]):
        cmdpath = options.pop(0)
    else:
        cmdpath = os.path.join(TOOLS_DIR, "checkpatch.pl")
    if commit == PRE_SUBMIT:
        # The --ignore option must be present and include 'MISSING_SIGN_OFF' in
        # this case.
        options.append("--ignore=MISSING_SIGN_OFF")
    # Always ignore the check for the MAINTAINERS file.  We do not track that
    # information on that file in our source trees, so let's suppress the
    # warning.
    options.append("--ignore=FILE_PATH_CHANGES")
    # Do not complain about the Change-Id: fields, since we use Gerrit.
    # Upstream does not want those lines (since they do not use Gerrit), but
    # we always do, so disable the check globally.
    options.append("--ignore=GERRIT_CHANGE_ID")
    cmd = [cmdpath] + options + ["-"]
    cmd_result = cros_build_lib.dbg_run(
        cmd,
        input=_get_patch(commit).encode("utf-8"),
        stdout=True,
        stderr=subprocess.STDOUT,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if cmd_result.returncode:
        return errors.HookFailure(
            "%s errors/warnings\n\n%s" % (cmdpath, cmd_result.stdout)
        )
    return None


def _run_kerneldoc(_project, commit, options=()):
    """Runs kernel-doc validator on the given project"""
    included, excluded = _parse_common_inclusion_options(options)
    files = _filter_files(
        _get_affected_files(commit, relative=True), included, excluded
    )
    if files:
        cmd = [TOOLS_DIR / "kernel-doc", "-none"] + files
        output = _run_command(cmd, stderr=subprocess.STDOUT)
        if output:
            return errors.HookFailure(
                "kernel-doc errors/warnings:", items=output.splitlines()
            )
    return None


def _kernel_configcheck(_project, commit):
    """Makes sure kernel config changes are not mixed with code changes"""
    files = _get_affected_files(commit)
    if len(_filter_files(files, [r"chromeos/config"])) not in [0, len(files)]:
        return errors.HookFailure(
            "Changes to chromeos/config/ and regular files must "
            "be in separate commits:\n%s" % "\n".join(files)
        )
    return None


def _check_manifests(_project, commit):
    """Make sure Manifest files only have comments & DIST lines."""
    ret = []

    manifests = _filter_files(
        _get_affected_files(commit, relative=True), [r".*/Manifest$"]
    )
    for path in manifests:
        data = _get_file_content(path, commit)

        # Disallow blank files.
        if not data.strip():
            ret.append("%s: delete empty file" % (path,))
            continue

        # Make sure the last newline isn't omitted.
        if data[-1] != "\n":
            ret.append("%s: missing trailing newline" % (path,))

        # Do not allow leading or trailing blank lines.
        lines = data.splitlines()
        if not lines[0]:
            ret.append("%s: delete leading blank lines" % (path,))
        if not lines[-1]:
            ret.append("%s: delete trailing blank lines" % (path,))

        for line in lines:
            # Disallow leading/trailing whitespace.
            if line != line.strip():
                ret.append(
                    "%s: remove leading/trailing whitespace: %s" % (path, line)
                )

            # Allow blank lines & comments.
            line = line.split("#", 1)[0]
            if not line:
                continue

            # All other linse should start with DIST.
            if not line.startswith("DIST "):
                ret.append("%s: remove non-DIST lines: %s" % (path, line))
                break

    if ret:
        return errors.HookFailure("\n".join(ret))
    return None


def _check_change_has_branch_field(_project, commit, options=()):
    """Check for a non-empty 'BRANCH=' field in the commit message."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--optional", action="store_true")
    parser.add_argument("--required", action="store_false", dest="optional")
    opts = parser.parse_args(options)

    if commit == PRE_SUBMIT:
        return None
    branch_re = re.compile(r"\nBRANCH=([^\n]+)")
    branch_match = branch_re.search(_get_commit_desc(commit))

    if not branch_match and not opts.optional:
        msg = (
            "Changelist description needs BRANCH field (after first line)\n"
            "E.g. BRANCH=none or BRANCH=link,snow"
        )
        return errors.HookFailure(msg)

    if branch_match and opts.optional:
        branch = branch_match.group(1)
        if branch.lower().strip() == "none":
            msg = (
                "BRANCH is optional in this repository.  Specifying "
                f"BRANCH={branch} is not useful.  Remove this line."
            )
            return errors.HookFailure(msg)

    return None


def _check_change_has_no_branch_field(_project, commit):
    """Verify 'BRANCH=' field does not exist in the commit message."""
    if commit == PRE_SUBMIT:
        return None
    BRANCH_RE = r"\nBRANCH=\S+"

    if re.search(BRANCH_RE, _get_commit_desc(commit)):
        msg = "This checkout does not use BRANCH= fields.  Delete them."
        return errors.HookFailure(msg)
    return None


def _check_change_has_signoff_field(_project, commit):
    """Check for a non-empty 'Signed-off-by:' field in the commit message."""
    if commit == PRE_SUBMIT:
        return None
    SIGNOFF_RE = r"\nSigned-off-by: \S+"

    if not re.search(SIGNOFF_RE, _get_commit_desc(commit)):
        msg = (
            "Changelist description needs Signed-off-by: field\n"
            "E.g. Signed-off-by: My Name <me@chromium.org>"
        )
        return errors.HookFailure(msg)
    return None


def _check_change_has_no_signoff_field(_project, commit):
    """Verify 'Signed-off-by:' field does not exist in the commit message."""
    if commit == PRE_SUBMIT:
        return None
    SIGNOFF_RE = r"\nSigned-off-by: \S+"

    if re.search(SIGNOFF_RE, _get_commit_desc(commit)):
        msg = "This checkout does not use Signed-off-by: tags.  Delete them."
        return errors.HookFailure(msg)
    return None


def _run_project_hook_script(script, name, project, commit):
    """Runs a project hook script.

    The script is run with the following environment variables set:
      PRESUBMIT_PROJECT: The affected project
      PRESUBMIT_COMMIT: The affected commit
      PRESUBMIT_FILES: A newline-separated list of affected files

    The script is considered to fail if the exit code is non-zero.  It should
    write an error message to stdout.
    """
    env = dict(os.environ)
    env["PRESUBMIT_PROJECT"] = project.name
    env["PRESUBMIT_COMMIT"] = commit

    # Put affected files in an environment variable
    files = _get_affected_files(commit, relative=True)
    env["PRESUBMIT_FILES"] = "\n".join(files)

    # Replace placeholders ourselves so arguments expand correctly.
    cmd = []
    for arg in shlex.split(script):
        if arg == "${PRESUBMIT_PROJECT}":
            cmd.append(project.name)
        elif arg == "${PRESUBMIT_COMMIT}":
            cmd.append(commit)
        elif arg == "${PRESUBMIT_FILES}":
            if not files:
                if commit == PRE_SUBMIT:
                    print(
                        f"\n{name}: Skipping in {project.name} because it uses"
                        " PRESUBMIT_FILES, which is empty.",
                        file=sys.stderr,
                    )
                return
            cmd.extend(files)
        elif "${PRESUBMIT_COMMIT}" in arg:
            cmd.append(arg.replace("${PRESUBMIT_COMMIT}", commit))
        else:
            cmd.append(arg)

    cmd_result = cros_build_lib.dbg_run(
        cmd=cmd,
        env=env,
        input="",
        stdout=True,
        encoding="utf-8",
        errors="replace",
        stderr=subprocess.STDOUT,
        check=False,
    )
    if cmd_result.returncode:
        stdout = cmd_result.stdout
        if stdout:
            stdout = re.sub("(?m)^", "  ", stdout)
        return errors.HookFailure(
            'Hook script "%s" failed with code %d%s'
            % (script, cmd_result.returncode, ":\n" + stdout if stdout else "")
        )
    return None


def _check_project_prefix(_project, commit):
    """Require the commit message have a project specific prefix as needed."""

    files = _get_affected_files(commit, include_deletes=True, relative=True)
    prefix = os.path.commonprefix(files)
    prefix = os.path.dirname(prefix)

    # If there is no common prefix, the CL span multiple projects.
    if not prefix:
        return None

    project_name = prefix.split("/")[0]

    # The common files may all be within a subdirectory of the main project
    # directory, so walk up the tree until we find an alias file.
    # _get_affected_files() should return relative paths, but check against '/'
    # to ensure that this loop terminates even if it receives an absolute path.
    while prefix and prefix != "/":
        alias_file = os.path.join(prefix, ".project_alias")

        # If an alias exists, use it.
        if os.path.isfile(alias_file):
            project_name = osutils.ReadFile(alias_file).strip()

        prefix = os.path.dirname(prefix)

    if not _get_commit_desc(commit).startswith(project_name + ": "):
        return errors.HookFailure(
            "The commit title for changes affecting only %s"
            ' should start with "%s: "' % (project_name, project_name)
        )
    return None


def _check_filepath_chartype(_project, commit):
    """Checks that FilePath::CharType stuff is not used."""

    FILEPATH_REGEXP = re.compile(
        "|".join(
            [
                r"(?:base::)?FilePath::(?:Char|String|StringPiece)Type",
                r"(?:base::)?FilePath::FromUTF8Unsafe",
                r"AsUTF8Unsafe",
                r"FILE_PATH_LITERAL",
            ]
        )
    )
    files = _filter_files(
        _get_affected_files(commit, relative=True), [r".*\.(cc|h)$"]
    )

    error_list = []
    for afile in files:
        for line_num, line in _get_file_diff(afile, commit):
            m = re.search(FILEPATH_REGEXP, line)
            if m:
                error_list.append(
                    "%s, line %s has %s" % (afile, line_num, m.group(0))
                )

    if error_list:
        msg = "Please assume FilePath::CharType is char (crbug.com/870621):"
        return errors.HookFailure(msg, error_list)
    return None


def _check_cros_workon_revbump_eclass(project, commit, options=()):
    """Checks if packages inheriting the updated eclass also get rev-bumped."""

    files = _get_affected_files(commit, relative=True)
    eclass_paths = [re.escape(x) for x in options]
    if not eclass_paths:
        return None

    eclasses = _filter_files(files, eclass_paths)
    error_list = []

    for eclass in eclasses:
        eclass = Path(eclass)

        # Find all the ebuilds that are under the same project overlay
        # (e.g. src/third_party/chromiumos-overlay/) and inherit this eclass,
        # and extract their category/package string.
        pkgs = {
            ebuild.package_info.cp
            for ebuild in build_query.Overlay(Path(project.dir)).ebuilds
            if eclass.stem in ebuild.eclasses
        }

        # Filter out the packages that didn't get updated in this commit.
        pkgs_without_update = {
            pkg
            for pkg in pkgs
            if not any(x.startswith(f"{pkg}/") for x in files)
        }

        if pkgs_without_update:
            error_list.append(
                errors.HookFailure(
                    f"(b/201299127) '{eclass}' is modified.\n"
                    f"Commit changes for following package(s), preferably\n"
                    f"in 'files/revision_bump':\n",
                    sorted(pkgs_without_update),
                )
            )

    return error_list


def _check_exec_files(_project, commit):
    """Make +x bits on files."""
    # List of files that should never be +x.
    NO_EXEC = (
        "ChangeLog*",
        "COPYING",
        "make.conf",
        "make.defaults",
        "Manifest",
        "OWNERS",
        "package.use",
        "package.keywords",
        "package.mask",
        "parent",
        "README",
        "TODO",
        ".gitignore",
        "*.[achly]",
        "*.[ch]xx",
        "*.boto",
        "*.cc",
        "*.cfg",
        "*.conf",
        "*.config",
        "*.cpp",
        "*.css",
        "*.ebuild",
        "*.eclass",
        "*.gn",
        "*.gni",
        "*.gyp",
        "*.gypi",
        "*.htm",
        "*.html",
        "*.ini",
        "*.js",
        "*.json",
        "*.md",
        "*.mk",
        "*.patch",
        "*.policy",
        "*.proto",
        "*.raw",
        "*.rules",
        "*.service",
        "*.target",
        "*.txt",
        "*.xml",
        "*.yaml",
    )

    def FinalName(obj):
        # If the file is being deleted, then the dst_file is not set.
        if obj.dst_file is None:
            return obj.src_file
        else:
            return obj.dst_file

    bad_files = []
    files = _get_affected_files(commit, relative=True, full_details=True)
    for f in files:
        mode = int(f.dst_mode, 8)
        if not mode & 0o111:
            continue
        name = FinalName(f)
        for no_exec in NO_EXEC:
            if fnmatch.fnmatch(name, no_exec):
                bad_files.append(name)
                break

    if bad_files:
        return errors.HookFailure(
            "These files should not be executable.  Please `chmod -x` them.",
            bad_files,
        )
    return None


def _check_git_cl_presubmit(_project, commit):
    """Run git-cl presubmit automatically if PRESUBMIT.py exists."""
    if not os.path.exists("PRESUBMIT.py"):
        return None

    git_cl = os.path.join(constants.DEPOT_TOOLS_DIR, "git-cl")
    cmd = [git_cl, "presubmit"]
    if commit != "pre-submit":
        cmd += ["--upload"]
    result = cros_build_lib.dbg_run(
        cmd,
        input="",
        check=False,
        encoding="utf-8",
        stdout=True,
        stderr=subprocess.STDOUT,
    )
    if result.returncode:
        return errors.HookFailure(f"git-cl presubmit failed: {result.stdout}")
    return None


def _check_json_key_prefix(project: Project, commit: str):
    """Checks that modified .json files are valid battery configs for BCIC.

    This hook performs two main checks on modified .json files:
    1. Validates that the file content is parseable as valid JSON.
    2. Ensures that no pair of top-level keys in the JSON data are prefixes of
       each other.

    Args:
        project: The Project to look at
        commit: The commit to look at

    Returns:
        None if all checks pass, otherwise a HookFailure object with details
        of the errors.
    """
    if project.name != "chromiumos/project":
        return None

    files = _filter_files(
        _get_affected_files(commit, relative=True), [r".*\.json$"]
    )

    json_error_list = []
    prefix_error_dict = {}
    for file in files:
        try:
            json_file = json.loads(_get_file_content(file, commit))
            keys = list(json_file.keys())
            file_prefix_errors = []
            for i, key1 in enumerate(keys):
                for key2 in keys[i + 1 :]:
                    if key1.startswith(key2) or key2.startswith(key1):
                        file_prefix_errors.append(sorted([key1, key2]))
            if file_prefix_errors:
                prefix_error_dict[file] = file_prefix_errors

        except json.JSONDecodeError as e:
            json_error_list.append(f"{file}: {e}")

    if json_error_list:
        return errors.HookFailure("Invalid JSON file(s):", json_error_list)

    if prefix_error_dict:
        prefix_error_list = []
        for file, prefix_errors in prefix_error_dict.items():
            for key1, key2 in prefix_errors:
                prefix_error_list.append(
                    f"{file}: Key '{key1}' is a prefix of Key '{key2}'"
                )
        return errors.HookFailure(
            "JSON file(s) with prefix errors:", prefix_error_list
        )
    return None


def _check_cl_size(
    _project: Project, commit: str, options: Sequence[str] = ()
) -> None:
    """Print a warning on large CLs."""
    included, excluded = _parse_common_inclusion_options(options)

    # Exclude deletes and use the common file path filters to reduce noise on
    # CLs deleting large amounts of code, large generated file diffs, etc. Note
    # that the _get_cl_size_string function doesn't use these filters, so that
    # the size displayed on upload matches the size displayed in Gerrit as
    # closely as possible.
    lines_changed = _get_lines_changed(
        commit,
        include_deletes=False,
        include_list=included + COMMON_INCLUDED_PATHS,
        exclude_list=excluded + COMMON_EXCLUDED_PATHS,
    )

    if lines_changed >= ChangeSize.M:
        msg = (
            "Large CLs are often more error-prone and take longer to review, "
            "consider splitting this change up. See "
            "https://google.github.io/eng-practices/review/developer/"
            "small-cls.html."
        )
        return errors.HookWarning(msg)

    return None


# Base

# A list of hooks which are not project specific and check patch description
# (as opposed to patch body).
_PATCH_DESCRIPTION_HOOKS = [
    _check_change_has_bug_field,
    _check_change_has_valid_cq_depend,
    _check_change_has_test_field,
    _check_change_has_proper_changeid,
    _check_commit_message_style,
    _check_change_is_contribution,
    _check_change_no_include_oem,
    _check_change_no_include_board_phase,
]

# A list of hooks that are not project-specific
_COMMON_HOOKS = [
    _check_cargo_clippy,
    _check_cros_license,
    _check_ebuild_eapi,
    _check_ebuild_keywords,
    _check_ebuild_licenses,
    _check_ebuild_localname_exists,
    _check_ebuild_owners,
    _check_ebuild_r0,
    _check_ebuild_virtual_pv,
    _check_exec_files,
    _check_for_uprev,
    _check_git_cl_presubmit,
    _check_json_key_prefix,
    _check_keywords,
    _check_layout_conf,
    _check_no_extra_blank_lines,
    _check_no_handle_eintr_close,
    _check_no_long_lines,
    _check_no_new_gyp,
    _check_no_stray_whitespace,
    _check_no_tabs,
    _check_portage_make_use_var,
    _check_tabbed_indents,
]

# A dictionary of flags (keys) that can appear in the config file, and the hook
# that the flag controls (value).
_HOOK_FLAGS = {
    "aosp_license_check": _check_aosp_license,
    "blank_line_check": _check_no_extra_blank_lines,
    "branch_check": _check_change_has_branch_field,
    "bug_field_check": _check_change_has_bug_field,
    "cargo_clippy_check": _check_cargo_clippy,
    "check_change_no_include_board_phase": _check_change_no_include_board_phase,
    "check_commit_message_style": _check_commit_message_style,
    "checkpatch_check": _run_checkpatch,
    "contribution_check": _check_change_is_contribution,
    "cros_license_check": _check_cros_license,
    "cros_workon_revbump_eclass_check": _check_cros_workon_revbump_eclass,
    "egis_license_check": _check_egis_license,
    "exec_files_check": _check_exec_files,
    "filepath_chartype_check": _check_filepath_chartype,
    "git_cl_presubmit": _check_git_cl_presubmit,
    "handle_eintr_close_check": _check_no_handle_eintr_close,
    "kernel_splitconfig_check": _kernel_configcheck,
    "kerneldoc_check": _run_kerneldoc,
    "keyword_check": _check_keywords,
    "long_line_check": _check_no_long_lines,
    "manifest_check": _check_manifests,
    "project_prefix_check": _check_project_prefix,
    "signoff_check": _check_change_has_signoff_field,
    "stray_whitespace_check": _check_no_stray_whitespace,
    "tab_check": _check_no_tabs,
    "tabbed_indent_required_check": _check_tabbed_indents,
    "test_field_check": _check_change_has_test_field,
}


def _hooks_require_sudo(hooks) -> bool:
    """Returns true if the provided list of hooks requires sudo.

    Args:
        hooks: list of hooks to check.
    """
    for h in hooks:
        if h.__name__ == "cargo_clippy_check":
            if "options" in h.keywords:
                return True

    return False


def _get_override_hooks(config):
    """Returns a set of hooks controlled by the current project's config file.

    Expects to be called within the project root.

    Args:
        config: A ConfigParser for the project's config file.
    """
    SECTION = "Hook Overrides"
    SECTION_OPTIONS = "Hook Overrides Options"

    valid_keys = set(_HOOK_FLAGS.keys())
    hooks = _HOOK_FLAGS.copy()
    hook_overrides = set(
        config.options(SECTION) if config.has_section(SECTION) else []
    )

    unknown_keys = hook_overrides - valid_keys
    if unknown_keys:
        raise ValueError(
            f"{_CONFIG_FILE}: [{SECTION}]: unknown keys: " f"{unknown_keys}"
        )

    enable_flags = []
    disable_flags = []
    for flag in valid_keys:
        if flag in hook_overrides:
            try:
                enabled = config.getboolean(SECTION, flag)
            except ValueError as e:
                raise ValueError(
                    'Error: parsing flag "%s" in "%s" failed: %s'
                    % (flag, _CONFIG_FILE, e)
                )
        elif hooks[flag] in _COMMON_HOOKS:
            # Enable common hooks by default so we process custom options below.
            enabled = True
        else:
            # All other hooks we left as a tristate.  We use this below for a
            # few hooks to control default behavior.
            enabled = None

        if enabled:
            enable_flags.append(flag)
        elif enabled is not None:
            disable_flags.append(flag)

        # See if this hook has custom options.
        if enabled:
            try:
                options = config.get(SECTION_OPTIONS, flag)
                hooks[flag] = functools.partial(
                    hooks[flag], options=options.split()
                )
                hooks[flag].__name__ = flag
            except (configparser.NoOptionError, configparser.NoSectionError):
                pass

    enabled_hooks = set(hooks[x] for x in enable_flags)
    disabled_hooks = set(hooks[x] for x in disable_flags)

    if _check_change_has_signoff_field not in enabled_hooks:
        if _check_change_has_signoff_field not in disabled_hooks:
            enabled_hooks.add(_check_change_has_no_signoff_field)
    if _check_change_has_branch_field not in enabled_hooks:
        enabled_hooks.add(_check_change_has_no_branch_field)

    return enabled_hooks, disabled_hooks


def _get_project_hook_scripts(config):
    """Returns a list of project-specific hook scripts.

    Args:
        config: A ConfigParser for the project's config file.
    """
    SECTION = "Hook Scripts"
    if not config.has_section(SECTION):
        return []

    return config.items(SECTION)


def _get_project_hooks(presubmit, config):
    """Returns a list of hooks that need to be run for a project.

    Expects to be called from within the project root.

    Args:
        presubmit: A Boolean, True if the check is run as a git pre-submit
            script.
        config: A configparse.ConfigParser instance.
    """
    if presubmit:
        hooks = _COMMON_HOOKS
    else:
        hooks = _PATCH_DESCRIPTION_HOOKS + _COMMON_HOOKS

    enabled_hooks, disabled_hooks = _get_override_hooks(config)
    hooks = [hook for hook in hooks if hook not in disabled_hooks]

    # If a list is both in _COMMON_HOOKS and also enabled explicitly through an
    # override, keep the override only.  Note that the override may end up being
    # a functools.partial, in which case we need to extract the .func to compare
    # it to the common hooks.
    unwrapped_hooks = [getattr(hook, "func", hook) for hook in enabled_hooks]
    hooks = [hook for hook in hooks if hook not in unwrapped_hooks]

    hooks = list(enabled_hooks) + hooks

    for name, script in _get_project_hook_scripts(config):
        func = functools.partial(_run_project_hook_script, script, name)
        func.__name__ = name
        hooks.append(func)

    return hooks


OPTION_IGNORE_MERGED_COMMITS = "ignore_merged_commits"
_DEFAULT_OPTIONS = {
    OPTION_IGNORE_MERGED_COMMITS: False,
}


def _get_project_options(config):
    """Returns a dictionary of options controlled by the project's config file.

    Use the default value for each option unless specified in the config.

    Args:
        config: A configparse.ConfigParser instance.
    """
    SECTION = "Options"

    options = _DEFAULT_OPTIONS.copy()

    if not config.has_section(SECTION):
        return options

    valid_keys = set(_DEFAULT_OPTIONS.keys())
    config_options = set(config.options(SECTION))
    unknown_keys = config_options - valid_keys
    if unknown_keys:
        raise ValueError(
            f"{_CONFIG_FILE}: [{SECTION}]: unknown keys: {unknown_keys}"
        )

    for flag in config_options:
        try:
            options[flag] = config.getboolean(SECTION, flag)
        except ValueError as e:
            raise ValueError(
                f'Error: parsing flag {flag} in "{_CONFIG_FILE}" failed: {e}'
            )

    return options


def _get_project_config(config_file=None):
    """Returns a configparse.ConfigParser instance for a project.

    Args:
        config_file: A string, the config file. Defaults to _CONFIG_FILE.
    """
    config = configparser.RawConfigParser()
    if config_file is None:
        config_file = _CONFIG_FILE
    if not os.path.exists(config_file):
        # Just use an empty config file
        config = configparser.RawConfigParser()
    else:
        config.read(config_file)
    return config


def _run_project_hooks(
    project_name,
    proj_dir=None,
    commit_list=None,
    presubmit=False,
    config_file=None,
    jobs=None,
):
    """For each project run its project specific hook from the hooks dictionary.

    Args:
        project_name: The name of project to run hooks for.
        proj_dir: If non-None, this is the directory the project is in.  If
            None, we'll ask repo.
        commit_list: A list of commits to run hooks against.  If None or empty
            list then we'll automatically get the list of commits that would be
            uploaded.
        presubmit: A Boolean, True if the check is run as a git pre-submit
            script.
        config_file: A string, the presubmit config file. If not specified,
            defaults to PRESUBMIT.cfg in the project directory.
        jobs: Maximum number of tasks to run in parallel, or choose
            automatically if None.

    Returns:
        Boolean value of whether any errors were ecountered while running the
        hooks.
    """
    if proj_dir is None:
        proj_dirs = _run_command(
            ["repo", "forall", project_name, "-c", "pwd"]
        ).split()
        if not proj_dirs:
            print("%s cannot be found." % project_name, file=sys.stderr)
            print("Please specify a valid project.", file=sys.stderr)
            return True
        if len(proj_dirs) > 1:
            print(
                "%s is associated with multiple directories." % project_name,
                file=sys.stderr,
            )
            print(
                "Please specify a directory to help disambiguate.",
                file=sys.stderr,
            )
            return True
        proj_dir = proj_dirs[0]

    pwd = os.getcwd()
    # hooks assume they are run from the root of the project
    os.chdir(proj_dir)

    color = terminal.Color()

    remote_branch = _run_command(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    ).strip()
    if not remote_branch:
        print(
            "Your project %s doesn't track any remote repo." % project_name,
            file=sys.stderr,
        )
        remote = None
    else:
        branch_items = remote_branch.split("/", 1)
        if len(branch_items) != 2:
            errors.PrintErrorForProject(
                project_name,
                errors.HookFailure(
                    "Cannot get remote and branch name (%s)" % remote_branch
                ),
            )
            os.chdir(pwd)
            return True
        remote, _branch = branch_items

    project = Project(name=project_name, dir=proj_dir, remote=remote)

    config = _get_project_config(config_file)
    options = _get_project_options(config)

    if not commit_list:
        try:
            commit_list = _get_commits(options[OPTION_IGNORE_MERGED_COMMITS])
        except errors.VerifyException as e:
            errors.PrintErrorForProject(
                project.name, errors.HookFailure(str(e))
            )
            os.chdir(pwd)
            return True

    isatty = sys.stdout.isatty()
    yellow_len = len(color.Color(color.YELLOW, ""))
    hooks = _get_project_hooks(presubmit, config)
    error_found = False
    commit_count = len(commit_list)
    hook_count = len(hooks)

    def _run_hook(hook, project, commit):
        with timer.Timer() as delta:
            result = hook(project, commit)
        return (hook, result, delta)

    with contextlib.ExitStack() as stack:
        executor = stack.enter_context(
            concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
        )
        if _hooks_require_sudo(hooks):
            stack.enter_context(sudo.SudoKeepAlive())
        for i, commit in enumerate(commit_list):
            CACHE.clear()

            # If run with --pre-submit, then commit is PRE_SUBMIT, and not a
            # commit. Use that as the description.
            desc = commit if commit == PRE_SUBMIT else _get_commit_desc(commit)
            print(
                "[%s %i/%i %s %s] %s"
                % (
                    color.Color(color.CYAN, "COMMIT"),
                    i + 1,
                    commit_count,
                    commit[0:12],
                    _get_cl_size_string(color, commit),
                    desc.splitlines()[0],
                )
            )

            pending = set(hooks) | {None}
            futures = (
                executor.submit(_run_hook, hook, project, commit)
                for hook in hooks
            )
            future_results = (
                future.result()
                for future in concurrent.futures.as_completed(futures)
            )

            for h, (hook, hook_result, runtime) in enumerate(
                itertools.chain(
                    # First is a do-nothing hook allowing us to display some
                    # progress before the first future completes. It never gets
                    # reported in progress output and never fails.
                    ((None, None, datetime.timedelta(0)),),
                    # Then handle the results of hooks executed on the thread
                    # pool, in the order in which they complete.
                    future_results,
                )
            ):
                pending.remove(hook)

                # Display progress.
                output_prefix = "[%s %i/%i] " % (
                    color.Color(color.YELLOW, "RUNNING"),
                    h,
                    hook_count,
                )
                hook_names = (hook.__name__ for hook in pending)
                output = output_prefix + ", ".join(hook_names)
                if isatty:
                    cols = shutil.get_terminal_size()[0] + yellow_len
                    output = f"\r{output[0:cols]}{CSI_ERASE_LINE_AFTER}"
                print(output, end="", flush=True)

                # Display the time the hook took to run, but only if not using
                # a status line: otherwise this would hide the pending list
                # (which would flash momentarily on the next iteration) or be
                # immediately replaced by the error output.
                if not isatty:
                    print(f" {runtime}")

                # Display error returned from hook, if any.
                if hook_result:
                    # Clear status line before showing errors.
                    if isatty:
                        print(f"\r{CSI_ERASE_LINE_AFTER}", end="")
                    if not isinstance(hook_result, list):
                        hook_result = [hook_result]
                    errors.PrintErrorsForCommit(
                        color, hook, project.name, hook_result
                    )
                    error_found = any(not r.is_warning for r in hook_result)

            # Clear status line after running all hooks for this commit.
            if isatty:
                print(f"\r{CSI_ERASE_LINE_AFTER}", end="")

    os.chdir(pwd)
    return error_found


# Main


def main(project_list, worktree_list=None, **_kwargs):
    """Main function invoked directly by repo.

    This function will exit directly upon error so that repo doesn't print some
    obscure error message.

    Args:
        project_list: List of projects to run on.
        worktree_list: A list of directories. It should be the same length as
            project_list, so that each entry in project_list matches with a
            directory in worktree_list. If None, we will attempt to calculate
            the directories automatically.
        kwargs: Leave this here for forward-compatibility.
    """
    start_time = datetime.datetime.now()
    found_error = False
    if not worktree_list:
        worktree_list = [None] * len(project_list)
    for project, worktree in zip(project_list, worktree_list):
        if _run_project_hooks(project, proj_dir=worktree):
            found_error = True

    end_time = datetime.datetime.now()
    color = terminal.Color()
    if found_error:
        msg = (
            "%s: Preupload failed due to above error(s).\n"
            "- To disable some source style checks, and for other hints, see "
            "%s/src/repohooks/README.md"
            % (color.Color(color.RED, "FATAL"), constants.SOURCE_ROOT)
        )
        if len(project_list) > 1:
            msg += "\n- To upload only current project, run 'repo upload .'"
        print(msg, file=sys.stderr)
        sys.exit(1)
    else:
        msg = "[%s] repohooks passed in %s." % (
            color.Color(color.GREEN, "PASSED"),
            end_time - start_time,
        )
        print(msg)


def _identify_project(path):
    """Identify the repo project associated with the given path.

    Returns:
        A string indicating what project is associated with the path passed in
        or a blank string upon failure.
    """
    return _run_command(
        ["repo", "forall", ".", "-c", "echo ${REPO_PROJECT}"],
        stderr=True,
        cwd=path,
    ).strip()


def direct_main(argv):
    """Run hooks directly (outside of the context of repo).

    Args:
        argv: The command line args to process

    Returns:
        0 if no pre-upload failures, 1 if failures.

    Raises:
        BadInvocation: On some types of invocation errors.
    """
    parser = commandline.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=None,
        help="The directory that the project lives in.  If not "
        "specified, use the git project root based on the cwd.",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="The project repo path; this can affect how the "
        "hooks get run, since some hooks are project-specific.  "
        "For chromite this is chromiumos/chromite.  If not "
        "specified, the repo tool will be used to figure this "
        "out based on the dir.",
    )
    parser.add_argument(
        "--rerun-since",
        default=None,
        help="Rerun hooks on old commits since some point "
        "in the past.  The argument could be a date (should "
        "match git log's concept of a date, e.g. 2012-06-20), "
        "or a SHA1, or just a number of commits to check (from 1 "
        "to 99).  This option is mutually exclusive with "
        "--pre-submit.",
    )
    parser.add_argument(
        "--pre-submit",
        action="store_true",
        help="Run the check against the pending commit.  "
        "This option should be used at the 'git commit' "
        "phase as opposed to 'repo upload'. This option "
        "is mutually exclusive with --rerun-since.",
    )
    parser.add_argument(
        "--presubmit-config", help="Specify presubmit config file to be used."
    )
    parser.add_argument("commits", nargs="*", help="Check specific commits")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        help="Run up to this many hooks in parallel. Setting to 1 forces "
        "serial execution, and the default automatically chooses an "
        "appropriate number for the current system.",
    )
    opts = parser.parse_args(argv)

    if opts.rerun_since:
        if opts.commits:
            raise BadInvocation(
                "Can't pass commits and use rerun-since: %s"
                % " ".join(opts.commits)
            )

        if len(opts.rerun_since) < 3 and opts.rerun_since.isdigit():
            # This must be the number of commits to check. We don't expect the
            # user to want to check more than 99 commits.
            limit = "-n%s" % opts.rerun_since
        elif git.IsSHA1(opts.rerun_since, False):
            limit = "%s.." % opts.rerun_since
        else:
            # This better be a date.
            limit = "--since=%s" % opts.rerun_since
        cmd = ["git", "log", limit, "--pretty=%H"]
        all_commits = _run_command(cmd).splitlines()
        bot_commits = _run_command(cmd + ["--author=chrome-bot"]).splitlines()

        # Eliminate chrome-bot commits but keep ordering the same...
        bot_commits = set(bot_commits)
        opts.commits = [c for c in all_commits if c not in bot_commits]

        if opts.pre_submit:
            raise BadInvocation(
                "rerun-since and pre-submit can not be used together"
            )
    if opts.pre_submit:
        if opts.commits:
            raise BadInvocation(
                "Can't pass commits and use pre-submit: %s"
                % " ".join(opts.commits)
            )
        opts.commits = [
            PRE_SUBMIT,
        ]

    # Check/normalize git dir; if unspecified, we'll use the root of the git
    # project from CWD
    if opts.dir is None:
        git_dir = _run_command(
            ["git", "rev-parse", "--show-toplevel"], stderr=True
        ).strip()
        if not git_dir:
            raise BadInvocation(
                "The current directory is not part of a git project."
            )
        opts.dir = git_dir
    elif not os.path.isdir(opts.dir):
        raise BadInvocation("Invalid dir: %s" % opts.dir)
    elif not os.path.isdir(os.path.join(opts.dir, ".git")):
        raise BadInvocation("Not a git directory: %s" % opts.dir)

    # Identify the project if it wasn't specified; this _requires_ the repo
    # tool to be installed and for the project to be part of a repo checkout.
    if not opts.project:
        opts.project = _identify_project(opts.dir)
        if not opts.project:
            raise BadInvocation(
                "Repo couldn't identify the project of %s" % opts.dir
            )

    found_error = _run_project_hooks(
        opts.project,
        proj_dir=opts.dir,
        commit_list=opts.commits,
        presubmit=opts.pre_submit,
        config_file=opts.presubmit_config,
        jobs=opts.jobs,
    )
    if found_error:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(direct_main(sys.argv[1:]))
