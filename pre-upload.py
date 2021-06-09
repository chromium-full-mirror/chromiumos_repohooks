#!/usr/bin/env python3
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Presubmit checks to run when doing `repo upload`.

You can add new checks by adding a function to the HOOKS constants.
"""

import argparse
import collections
import configparser
import datetime
import fnmatch
import functools
import json
import os
import pathlib
import re
import sys
import stat
import subprocess

from errors import (VerifyException, HookFailure, PrintErrorForProject,
                    PrintErrorsForCommit)

if __name__ in ('__builtin__', 'builtins'):
  # If repo imports us, the __name__ will be __builtin__, and the cwd will be in
  # the top level of the checkout (i.e. $CHROMEOS_CHECKOUT).  chromite will be
  # in that directory, so add it to our path.  This works whether we're running
  # the repo in $CHROMEOS_CHECKOUT/.repo/repo/ or a custom version in a
  # completely different tree.
  # TODO(vapier): Python 2 used "__builtin__" while Python 3 uses "builtins".
  sys.path.insert(0, os.getcwd())

elif __name__ == '__main__':
  # If we're run directly, we'll find chromite relative to the repohooks dir in
  # $CHROMEOS_CHECKOUT/src/repohooks, so go up two dirs.
  sys.path.insert(0, os.path.join(os.path.dirname(sys.argv[0]), '..', '..'))

# The sys.path monkey patching confuses the linter.
# pylint: disable=wrong-import-position
from chromite.lib import commandline
from chromite.lib import cros_build_lib
from chromite.lib import git
from chromite.lib import osutils
from chromite.lib import patch
from chromite.lib import terminal
from chromite.licensing import licenses_lib


assert sys.version_info >= (3, 6), 'This module requires Python 3.6+'


PRE_SUBMIT = 'pre-submit'


# Link to commit message documentation for users.
DOC_COMMIT_MSG_URL = ('https://chromium.googlesource.com/chromiumos/docs/+/HEAD'
                      '/contributing.md#commit-messages')


COMMON_INCLUDED_PATHS = [
    # C++ and friends
    r'.*\.c$', r'.*\.cc$', r'.*\.cpp$', r'.*\.h$', r'.*\.m$', r'.*\.mm$',
    r'.*\.inl$', r'.*\.asm$', r'.*\.hxx$', r'.*\.hpp$', r'.*\.s$', r'.*\.S$',
    # Scripts
    r'.*\.js$', r'.*\.py$', r'.*\.sh$', r'.*\.rb$', r'.*\.pl$', r'.*\.pm$',
    # No extension at all, note that ALL CAPS files are excluded by
    # COMMON_EXCLUDED_LIST below.
    r'(^|.*[\\\/])[^.]+$',
    # Other
    r'.*\.java$', r'.*\.mk$', r'.*\.am$',
    r'.*\.policy$', r'.*\.rules$', r'.*\.conf$', r'.*\.go$',
    r'(^OWNERS|/OWNERS)',
]


COMMON_EXCLUDED_PATHS = [
    # For ebuild trees, ignore any caches and manifest data.
    r'.*/Manifest$',
    r'.*/files/srcuris$',
    r'.*/metadata/[^/]*cache[^/]*/[^/]+/[^/]+$',

    # Ignore profiles data (like overlay-tegra2/profiles).
    r'(^|.*/)overlay-.*/profiles/.*',
    r'^profiles/.*$',

    # Ignore config files in ebuild setup.
    r'(^|.*/)overlay-.*/chromeos-base/chromeos-bsp.*/files/.*',
    r'^chromeos-base/chromeos-bsp.*/files/.*',

    # Ignore minified js and jquery.
    r'.*\.min\.js',
    r'.*jquery.*\.js',

    # Ignore license files as the content is often taken verbatim.
    r'.*/licenses/.*',

    # Exclude generated protobuf bindings.
    r'.*_pb2\.py$',
    r'.*\.pb\.go$',
]

LICENSE_EXCLUDED_PATHS = [
    r'^(.*/)?OWNERS(\..*)?$',
    r'^(.*/)?DIR_METADATA(\..*)?$',
]

_CONFIG_FILE = 'PRESUBMIT.cfg'


# File containing wildcards, one per line, matching files that should be
# excluded from presubmit checks. Lines beginning with '#' are ignored.
_IGNORE_FILE = '.presubmitignore'

BLOCKED_TERMS_FILE = 'blocked_terms.txt'
UNBLOCKED_TERMS_FILE = 'unblocked_terms.txt'

# Android internal and external projects use "Bug: " to track bugs in
# buganizer, so use Bug: and Test: instead of BUG= and TEST=.
TAG_COLON_REMOTES = {
    'aosp',
    'goog',
}

# Exceptions


class BadInvocation(Exception):
  """An Exception indicating a bad invocation of the program."""


# General Helpers


class Cache(object):
  """General helper for caching git content."""

  def __init__(self):
    self._cache = {}

  def get_subcache(self, scope):
    return self._cache.setdefault(scope, {})

  def clear(self):
    self._cache.clear()

CACHE = Cache()


Project = collections.namedtuple('Project', ['name', 'dir', 'remote'])


def _run_command(cmd, **kwargs):
  """Executes the passed in command and returns raw stdout output.

  This is a convenience func to set some run defaults differently.

  Args:
    cmd: The command to run; should be a list of strings.
    **kwargs: Same as cros_build_lib.run.

  Returns:
    The stdout from the process (discards stderr and returncode).
  """
  kwargs.setdefault('print_cmd', False)
  kwargs.setdefault('stdout', True)
  kwargs.setdefault('check', False)
  result = cros_build_lib.run(cmd, **kwargs)
  # NB: We decode this directly rather than through kwargs as our tests rely
  # on this post-processing behavior currently.
  return result.output.decode('utf-8', 'replace')


def _get_hooks_dir():
  """Returns the absolute path to the repohooks directory."""
  if __name__ == '__main__':
    # Works when file is run on its own (__file__ is defined)...
    return os.path.abspath(os.path.dirname(__file__))
  else:
    # We need to do this when we're run through repo.  Since repo executes
    # us with execfile(), we don't get __file__ defined.
    cmd = ['repo', 'forall', 'chromiumos/repohooks', '-c', 'pwd']
    return _run_command(cmd).strip()


def _match_regex_list(subject, expressions):
  """Try to match a list of regular expressions to a string.

  Args:
    subject: The string to match regexes on
    expressions: A list of regular expressions to check for matches with.

  Returns:
    Whether the passed in subject matches any of the passed in regexes.
  """
  for expr in expressions:
    if re.search(expr, subject):
      return True
  return False


def _filter_files(files, include_list, exclude_list=()):
  """Filter out files based on the conditions passed in.

  Args:
    files: list of filepaths to filter
    include_list: list of regex that when matched with a file path will cause it
        to be added to the output list unless the file is also matched with a
        regex in the exclude_list.
    exclude_list: list of regex that when matched with a file will prevent it
        from being added to the output list, even if it is also matched with a
        regex in the include_list.

  Returns:
    A list of filepaths that contain files matched in the include_list and not
    in the exclude_list.
  """
  filtered = []
  for f in files:
    if (_match_regex_list(f, include_list) and
        not _match_regex_list(f, exclude_list)):
      filtered.append(f)
  return filtered


# Git Helpers


def _get_upstream_branch():
  """Returns the upstream tracking branch of the current branch.

  Raises:
    Error if there is no tracking branch
  """
  current_branch = _run_command(['git', 'symbolic-ref', 'HEAD']).strip()
  current_branch = current_branch.replace('refs/heads/', '')
  if not current_branch:
    raise VerifyException('Need to be on a tracking branch')

  cfg_option = 'branch.' + current_branch + '.%s'
  full_upstream = _run_command(['git', 'config', cfg_option % 'merge']).strip()
  remote = _run_command(['git', 'config', cfg_option % 'remote']).strip()
  if not remote or not full_upstream:
    raise VerifyException('Need to be on a tracking branch')

  return full_upstream.replace('heads', 'remotes/' + remote)


def _get_patch(commit):
  """Returns the patch for this commit."""
  if commit == PRE_SUBMIT:
    return _run_command(['git', 'diff', '--cached', 'HEAD'])
  else:
    return _run_command(['git', 'format-patch', '--stdout', '-1', commit])


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
  if path.startswith('/'):
    raise ValueError('_get_file_content must be called with relative paths: %s'
                     % (path,))

  # {<commit>: {<path1>: <content>, <path2>: <content>}}
  cache = CACHE.get_subcache('get_file_content')
  if path in cache:
    return cache[path]

  if commit == PRE_SUBMIT:
    content = _run_command(['git', 'diff', 'HEAD', '--', path], stderr=True)
  else:
    content = _run_command(['git', 'show', '%s:%s' % (commit, path)],
                           stderr=True)
  cache[path] = content
  return content


def _get_file_diff(path, commit):
  """Returns a list of (linenum, lines) tuples that the commit touched."""
  # {<commit>: {<path1>: <content>, <path2>: <content>}}
  cache = CACHE.get_subcache('get_file_diff')
  if path in cache:
    return cache[path]

  if commit == PRE_SUBMIT:
    command = ['git', 'diff', '-p', '--pretty=format:', '--no-ext-diff', 'HEAD',
               '--', path]
  else:
    command = ['git', 'show', '-p', '--pretty=format:', '--no-ext-diff', commit,
               '--', path]
  output = _run_command(command)

  new_lines = []
  line_num = 0
  for line in output.splitlines():
    m = re.match(r'^@@ [0-9\,\+\-]+ \+([0-9]+)\,[0-9]+ @@', line)
    if m:
      line_num = int(m.groups(1)[0])
      continue
    if line.startswith('+') and not line.startswith('++'):
      new_lines.append((line_num, line[1:]))
    if not line.startswith('-'):
      line_num += 1
  cache[path] = new_lines
  return new_lines


def _get_ignore_wildcards(directory, cache):
  """Get wildcards listed in a directory's _IGNORE_FILE.

  Args:
    directory: A string containing a directory path.
    cache: A dictionary (opaque to caller) caching previously-read wildcards.

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
      # TODO(derat): Consider using _get_file_content() to get the file as of
      # this commit instead of the on-disk version. This may have a noticeable
      # performance impact, as each call to _get_file_content() runs git.
      with open(dotfile_path, 'r') as dotfile:
        for line in dotfile.readlines():
          line = line.strip()
          if line.startswith('#'):
            continue
          if line.endswith('/'):
            line += '*'
          wildcards.append(line)
    cache[directory] = wildcards

  return cache[directory]


def _path_is_ignored(path, cache):
  """Check whether a path is ignored by _IGNORE_FILE.

  Args:
    path: A string containing a path.
    cache: A dictionary (opaque to caller) caching previously-read wildcards.

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
    rel_path = path[len(prefix) + 1:]
    for wildcard in _get_ignore_wildcards(prefix, cache):
      if fnmatch.fnmatch(rel_path, wildcard):
        return True
    prefix = os.path.dirname(prefix)

  return False


def _get_affected_files(commit, include_deletes=False, relative=False,
                        include_symlinks=False, include_adds=True,
                        full_details=False, use_ignore_files=True):
  """Returns list of file paths that were modified/added, excluding symlinks.

  Args:
    commit: The commit
    include_deletes: If true, we'll include deleted files in the result
    relative: Whether to return relative or full paths to files
    include_symlinks: If true, we'll include symlinks in the result
    include_adds: If true, we'll include new files in the result
    full_details: If False, return filenames, else return structured results.
    use_ignore_files: Whether we ignore files matched by _IGNORE_FILE files.

  Returns:
    A list of modified/added (and perhaps deleted) files
  """
  if not relative and full_details:
    raise ValueError('full_details only supports relative paths currently')

  if commit == PRE_SUBMIT:
    return _run_command(['git', 'diff-index', '--cached',
                         '--name-only', 'HEAD']).split()

  path = os.getcwd()
  # {<commit>: {<path1>: <content>, <path2>: <content>}}
  cache = CACHE.get_subcache('get_affected_files')
  if path not in cache:
    cache[path] = git.RawDiff(path, '%s^!' % commit)
  files = cache[path]

  # Filter out symlinks.
  if not include_symlinks:
    files = [x for x in files if not stat.S_ISLNK(int(x.dst_mode, 8))]

  if not include_deletes:
    files = [x for x in files if x.status != 'D']

  if not include_adds:
    files = [x for x in files if x.status != 'A']

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


def _get_commits():
  """Returns a list of commits for this review."""
  cmd = ['git', 'log', '--no-merges', '--format=%H',
         '%s..' % _get_upstream_branch()]
  return _run_command(cmd).split()


def _get_commit_desc(commit):
  """Returns the full commit message of a commit."""
  if commit == PRE_SUBMIT:
    return ''

  # {<commit>: <content>}
  cache = CACHE.get_subcache('get_commit_desc')
  if commit not in cache:
    cache[commit] = _run_command(['git', 'log', '--format=%B',
                                  commit + '^!'])
  return cache[commit]


def _check_lines_in_diff(commit, files, check_callable, error_description):
  """Checks given file for errors via the given check.

  This is a convenience function for common per-line checks. It goes through all
  files and returns a HookFailure with the error description listing all the
  failures.

  Args:
    commit: The commit we're working on.
    files: The files to check.
    check_callable: A callable that takes a line and returns True if this line
        _fails_ the check.
    error_description: A string describing the error.
  """
  errors = []
  for afile in files:
    for line_num, line in _get_file_diff(afile, commit):
      result = check_callable(line)
      if result:
        msg = f'{afile}, line {line_num}'
        if isinstance(result, str):
          msg += f': {result}'
        errors.append(msg)
  if errors:
    return HookFailure(error_description, errors)
  return None


def _parse_common_inclusion_options(options):
  """Parses common hook options for including/excluding files.

  Args:
    options: Option string list.

  Returns:
    (included, excluded) where each one is a list of regex strings.
  """
  parser = argparse.ArgumentParser()
  parser.add_argument('--exclude_regex', action='append')
  parser.add_argument('--include_regex', action='append')
  opts = parser.parse_args(options)
  included = opts.include_regex or []
  excluded = opts.exclude_regex or []
  return included, excluded


# Common Hooks


def _check_no_long_lines(_project, commit, options=()):
  """Checks there are no lines longer than MAX_LEN in any of the text files."""
  LONG_LINE_OK_PATHS = [
      # Go has no line length limit.
      # https://golang.org/doc/effective_go.html#formatting
      r'.*\.go$',
      # Python does its own long line checks via pylint.
      r'.*\.py$',
  ]

  DEFAULT_MAX_LENGTHS = [
      # Java's line length limit is 100 chars.
      # https://chromium.googlesource.com/chromium/src/+/master/styleguide/java/java.md
      (r'.*\.java$', 100),
      # Rust's line length limit is 100 chars.
      (r'.*\.rs$', 100),
  ]

  MAX_LEN = 80

  included, excluded = _parse_common_inclusion_options(options)
  files = _filter_files(_get_affected_files(commit, relative=True),
                        included + COMMON_INCLUDED_PATHS,
                        excluded + COMMON_EXCLUDED_PATHS + LONG_LINE_OK_PATHS)

  errors = []
  for afile in files:
    skip_regexps = (
        r'https?://',
        r'^#\s*(define|include|import|pragma|if|ifndef|endif)\b',
    )

    max_len = MAX_LEN

    for expr, length in DEFAULT_MAX_LENGTHS:
      if re.search(expr, afile):
        max_len = length
        break

    if os.path.basename(afile).startswith('OWNERS'):
      # File paths can get long, and there's no way to break them up into
      # multiple lines.
      skip_regexps += (
          r'^include\b',
          r'file:',
      )

    skip_regexps = [re.compile(x) for x in skip_regexps]
    for line_num, line in _get_file_diff(afile, commit):
      # Allow certain lines to exceed the maxlen rule.
      if len(line) <= max_len or any(x.search(line) for x in skip_regexps):
        continue

      errors.append('%s, line %s, %s chars, over %s chars' %
                    (afile, line_num, len(line), max_len))
      if len(errors) == 5:  # Just show the first 5 errors.
        break

  if errors:
    msg = 'Found lines longer than the limit (first 5 shown):'
    return HookFailure(msg, errors)
  return None


def _check_no_stray_whitespace(_project, commit, options=()):
  """Checks that there is no stray whitespace at source lines end."""
  included, excluded = _parse_common_inclusion_options(options)
  files = _filter_files(_get_affected_files(commit),
                        included + COMMON_INCLUDED_PATHS + [r'^.*\.md$'],
                        excluded + COMMON_EXCLUDED_PATHS)
  return _check_lines_in_diff(commit, files,
                              lambda line: line.rstrip() != line,
                              'Found line ending with white space in:')


def _check_no_tabs(_project, commit, options=()):
  """Checks there are no unexpanded tabs."""
  # Don't add entire repos here.  Update the PRESUBMIT.cfg in each repo instead.
  # We only allow known specific filetypes here that show up in all repos.
  TAB_OK_PATHS = [
      r'.*\.ebuild$',
      r'.*\.eclass$',
      r'.*\.go$',
      r'.*/[M|m]akefile$',
      r'.*\.mk$',
  ]

  included, excluded = _parse_common_inclusion_options(options)
  files = _filter_files(_get_affected_files(commit),
                        included + COMMON_INCLUDED_PATHS,
                        excluded + COMMON_EXCLUDED_PATHS + TAB_OK_PATHS)
  return _check_lines_in_diff(commit, files,
                              lambda line: '\t' in line,
                              'Found a tab character in:')


def _read_terms_file(terms_file):
  """Read list of words from file, skipping comments and blank lines."""
  file_terms = set()
  for line in osutils.ReadFile(terms_file).splitlines():
    # Allow comment and blank lines.
    line = line.split('#', 1)[0]
    if not line:
      continue
    file_terms.add(line)
  return file_terms


def _check_keywords_in_file(project, commit, file, keywords,
                            unblocked_terms_file, opts):
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
    #   'blocked':bool,    - True: matching blocked, False: matching unblocked
    #   'keyword':regex,   - block regex
    #  }, ...]
    blocked_span = []
    # Store information about each span matching unblocking regex.
    # [re.span, ...]
    unblocked_span = []

    for word in keywords:
      for match in re.finditer(word, line, flags=re.I):
        blocked_span.append({'span' : match.span(),
                             'group' : match.group(0),
                             'blocked' : True,
                             'keyword' : word})

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
        if ub[0] <= b['span'][0] and ub[1] >= b['span'][1]:
          b['blocked'] = False
      if b['blocked']:
        return f'Matched "{b["group"]}" with regex of "{b["keyword"]}"'
    return False

  if file:
    return _check_lines_in_diff(commit, [file], _check_line,
                                'Found a blocked keyword in:')

  line_num = 1
  commit_desc_errors = []
  for line in _get_commit_desc(commit).splitlines():
    result = _check_line(line)
    if result:
      commit_desc_errors.append('Commit message, line %s: %s' %
                                (line_num, result))
    line_num += 1
  if commit_desc_errors:
    return HookFailure('Found a blocked keyword in:', commit_desc_errors)

  return None


def _check_keywords(project, commit, options=()):
  """Checks there are no blocked keywords in commit content."""
  # Read options from override list.
  parser = argparse.ArgumentParser()
  parser.add_argument('--exclude_regex', action='append', default=[])
  parser.add_argument('--include_regex', action='append', default=[])
  parser.add_argument('--block', action='append', default=[])
  parser.add_argument('--unblock', action='append', default=[])
  opts = parser.parse_args(options)

  # Leave patches with an upstream source alone, we don't want to have
  # divergence in these cases so terms can be ignored from such patches.
  upstream_prefixes = ('UPSTREAM:', 'FROMGIT:', 'BACKPORT:', 'FROMLIST:')
  desc = _get_commit_desc(commit)
  if desc.startswith(upstream_prefixes):
    return ()

  # Read blocked word list.
  blocked_terms_file = os.path.join(_get_hooks_dir(), BLOCKED_TERMS_FILE)
  common_keywords = _read_terms_file(blocked_terms_file)

  # Find unblocked word list in project root directory. If not found, global
  # list is used.
  unblocked_terms_file = os.path.join(_get_hooks_dir(), UNBLOCKED_TERMS_FILE)
  if os.path.isfile(os.path.join(project.dir, UNBLOCKED_TERMS_FILE)):
    unblocked_terms_file = os.path.join(project.dir, UNBLOCKED_TERMS_FILE)

  keywords = set(common_keywords | set(opts.block))

  files = _filter_files(
      _get_affected_files(commit),
      opts.include_regex + COMMON_INCLUDED_PATHS + [r'^.*\.md$'],
      opts.exclude_regex + COMMON_EXCLUDED_PATHS)

  errors = []
  for file in files:
    errs = _check_keywords_in_file(project, commit, file,
                                   keywords, unblocked_terms_file, opts)
    if errs:
      errors.append(errs)

  errs = _check_keywords_in_file(project, commit, None,
                                 keywords, unblocked_terms_file, opts)
  if errs:
    errors.append(errs)

  return errors


def _check_tabbed_indents(_project, commit, options=()):
  """Checks that indents use tabs only."""
  TABS_REQUIRED_PATHS = [
      r'.*\.ebuild$',
      r'.*\.eclass$',
  ]
  LEADING_SPACE_RE = re.compile('[\t]* ')

  included, excluded = _parse_common_inclusion_options(options)
  files = _filter_files(_get_affected_files(commit),
                        included + TABS_REQUIRED_PATHS,
                        excluded + COMMON_EXCLUDED_PATHS)
  return _check_lines_in_diff(
      commit, files,
      lambda line: LEADING_SPACE_RE.match(line) is not None,
      'Found a space in indentation (must be all tabs):')


def _check_gofmt(_project, commit, options=()):
  """Checks that Go files are formatted with gofmt."""
  included, excluded = _parse_common_inclusion_options(options)
  errors = []
  files = _filter_files(_get_affected_files(commit, relative=True),
                        included + [r'\.go$'],
                        excluded)

  for gofile in files:
    contents = _get_file_content(gofile, commit)
    output = _run_command(cmd=['gofmt', '-l'], input=contents.encode('utf-8'),
                          stderr=subprocess.STDOUT)
    if output:
      errors.append(gofile)
  if errors:
    return HookFailure('Files not formatted with gofmt:', errors)
  return None


def _check_rustfmt(_project, commit):
  """Checks that Rust files are formatted with rustfmt."""
  errors = []
  files = _filter_files(_get_affected_files(commit, relative=True),
                        [r'\.rs$'])

  for rustfile in files:
    contents = _get_file_content(rustfile, commit)
    output = _run_command(cmd=['rustfmt', '--edition', '2018'],
                          input=contents.encode('utf-8'),
                          stderr=subprocess.STDOUT)
    if output != contents:
      errors.append(rustfile)
  if errors:
    return HookFailure('Files not formatted with rustfmt: '
                       "(run 'cargo fmt' to fix)", errors)
  return None


class CargoClippyArgumentParserError(Exception):
  """An exception indicating an invalid check_cargo_clippy option."""


class CargoClippyArgumentParser(argparse.ArgumentParser):
  """A argument parser for check_cargo_clippy."""

  def error(self, message):
    raise CargoClippyArgumentParserError(message)


# A cargo project in which clippy runs.
ClippyProject = collections.namedtuple('ClippyProject', ('root', 'script'))


class _AddClippyProjectAction(argparse.Action):
  """A callback that adds a cargo clippy setting.

  It accepts a value which is in the form of "ROOT[:SCRIPT]".
  """

  def __call__(self, parser, namespace, values, option_string=None):
    if getattr(namespace, self.dest, None) is None:
      setattr(namespace, self.dest, [])
    spec = values.split(':', 1)
    if len(spec) == 1:
      spec += [None]

    if spec[0].startswith('/'):
      raise CargoClippyArgumentParserError('root path must not start with "/"'
                                           f' but "{spec[0]}"')

    clippy = ClippyProject(root=spec[0], script=spec[1])
    getattr(namespace, self.dest).append(clippy)


def _get_cargo_clippy_parser():
  """Creates a parser for check_cargo_clippy options."""

  parser = CargoClippyArgumentParser()
  parser.add_argument('--project', action=_AddClippyProjectAction, default=[])

  return parser


def _check_cargo_clippy(project, commit, options=()):
  """Checks that a change doesn't produce cargo-clippy errors."""

  options = list(options)
  if not options:
    return []
  parser = _get_cargo_clippy_parser()

  try:
    opts = parser.parse_args(options)
  except CargoClippyArgumentParserError as e:
    return [HookFailure('invalid check_cargo_clippy option is given.'
                        f' Please check PRESUBMIT.cfg is correct: {e}')]
  files = _get_affected_files(commit)

  errors = []
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

    # Clean cargo's cache when we run clippy for this `root` for the first time.
    # We don't want to clean the cache between commits to save time when
    # multiple commits are checked.
    if root not in _check_cargo_clippy.cleaned_root:
      _run_command(['cargo', 'clean',
                    f'--manifest-path={root}/Cargo.toml'],
                   stderr=subprocess.STDOUT)
      _check_cargo_clippy.cleaned_root.add(root)

    cmd = ['cargo', 'clippy', '--all-features', '--all-targets',
           f'--manifest-path={root}/Cargo.toml',
           '--', '-D', 'warnings']
    # Overwrite the clippy command if a project-specific script is specified.
    if clippy.script:
      cmd = [os.path.join(project.dir, clippy.script)]

    output = _run_command(cmd, stderr=subprocess.STDOUT)
    error = re.search(r'^error: ', output, flags=re.MULTILINE)
    if error:
      msg = output[error.start():]
      errors.append(HookFailure(msg))

  return errors


# Stores cargo projects in which `cargo clean` ran.
_check_cargo_clippy.cleaned_root = set()


def _get_test_field_re(project):
  """Provide the regular expression that matches the test field.

  Android internal and external projects use "Bug: " in the commit messages to
  track bugs in buganizer, so "Test: " is used instead of "TEST=".
  """
  if project.remote in TAG_COLON_REMOTES:
    return r'\nTest: \S+'
  else:
    return r'\nTEST=\S+'


def _check_change_has_test_field(project, commit):
  """Check for a non-empty 'TEST=' field in the commit message."""
  SEE_ALSO = 'Please review the documentation:\n%s' % (DOC_COMMIT_MSG_URL,)

  TEST_FIELD_RE = _get_test_field_re(project)
  if not re.search(TEST_FIELD_RE, _get_commit_desc(commit)):
    msg = ('Changelist description needs TEST field (after first line)\n%s' %
           (SEE_ALSO,))
    return HookFailure(msg)
  return None


def _check_change_has_valid_cq_depend(_project, commit):
  """Check for a correctly formatted Cq-Depend field in the commit message."""
  desc = _get_commit_desc(commit)
  msg = 'Changelist has invalid Cq-Depend target.'
  example = 'Example: Cq-Depend: chromium:1234, chrome-internal:2345'
  try:
    patch.GetPaladinDeps(desc)
  except ValueError as ex:
    return HookFailure(msg, [example, str(ex)])
  # Check that Cq-Depend is in the same paragraph as Change-Id.
  msg = 'Cq-Depend is not in the same paragraph as Change-Id.'
  paragraphs = desc.split('\n\n')
  for paragraph in paragraphs:
    if (re.search(r'^Cq-Depend:', paragraph, re.M) and not
        re.search('^Change-Id:', paragraph, re.M)):
      return HookFailure(msg)

  # We no longer support CQ-DEPEND= lines.
  if re.search(r'^CQ-DEPEND[=:]', desc, re.M):
    return HookFailure(
        'CQ-DEPEND= is no longer supported.  Please see:\n'
        'https://chromium.googlesource.com/chromiumos/docs/+/HEAD/'
        'contributing.md#CQ-DEPEND')

  return None


def _check_change_is_contribution(_project, commit):
  """Check that the change is a contribution."""
  NO_CONTRIB = 'not a contribution'
  if NO_CONTRIB in _get_commit_desc(commit).lower():
    msg = ('Changelist is not a contribution, this cannot be accepted.\n'
           'Please remove the "%s" text from the commit message.') % NO_CONTRIB
    return HookFailure(msg)
  return None


def _check_change_has_bug_field(project, commit):
  """Check for a correctly formatted 'BUG=' field in the commit message."""
  SEE_ALSO = 'Please review the documentation:\n%s' % (DOC_COMMIT_MSG_URL,)

  OLD_BUG_RE = r'\nBUG=.*chromium-os'
  if re.search(OLD_BUG_RE, _get_commit_desc(commit)):
    msg = ('The chromium-os bug tracker is now deprecated. Please use\n'
           'the chromium tracker in your BUG= line now.')
    return HookFailure(msg)

  # Android internal and external projects use "Bug: " to track bugs in
  # buganizer.
  if project.remote in TAG_COLON_REMOTES:
    BUG_RE = r'\nBug: ?([Nn]one|\d+)'
    if not re.search(BUG_RE, _get_commit_desc(commit)):
      msg = ('Changelist description needs BUG field (after first line):\n'
             'Examples:\n'
             'Bug: 9999 (for buganizer)\n'
             'BUG=None\n%s' % (SEE_ALSO,))
      return HookFailure(msg)
  else:
    BUG_RE = r'\nBUG=([Nn]one|(chromium|b):\d+)'
    if not re.search(BUG_RE, _get_commit_desc(commit)):
      msg = ('Changelist description needs BUG field (after first line):\n'
             'Examples:\n'
             'BUG=chromium:9999 (for public tracker)\n'
             'BUG=b:9999 (for buganizer)\n'
             'BUG=None\n%s' % (SEE_ALSO,))
      return HookFailure(msg)

  TEST_BEFORE_BUG_RE = _get_test_field_re(project) + r'.*' + BUG_RE

  if re.search(TEST_BEFORE_BUG_RE, _get_commit_desc(commit), re.DOTALL):
    msg = ('The BUG field must come before the TEST field.\n%s' %
           (SEE_ALSO,))
    return HookFailure(msg)

  return None


def _check_change_no_include_oem(project, commit):
  """Check that the change does not reference OEMs."""
  ALLOWLIST = {
      'chromiumos/platform/ec',
      # Used by unit tests.
      'project',
  }
  if project.name not in ALLOWLIST:
    return None

  TAGS = {
      'Reviewed-on',
      'Reviewed-by',
      'Signed-off-by',
      'Commit-Ready',
      'Tested-by',
      'Commit-Queue',
      'Acked-by',
      'Modified-by',
      'CC',
      'Suggested-by',
      'Reported-by',
      'Acked-for-chrome-by',
      'Cq-Cl-Tag',
      'Cq-Include-Trybots',
  }

  # Ignore tags, which could reasonably contain OEM names
  # (e.g. Reviewed-by: foo@oem.corp-partner.google.com).
  commit_message = ' '.join(
      x for x in _get_commit_desc(commit).splitlines()
      if ':' not in x or x.split(':', 1)[0] not in TAGS)

  commit_message = re.sub(r'[\s_-]+', ' ', commit_message)

  # Exercise caution when expanding these lists. Adding a name
  # could indicate a new relationship with a company!
  OEMS = ['hp', 'hewlett packard', 'dell', 'lenovo', 'acer', 'asus', 'samsung']
  ODMS = [
      'bitland', 'compal', 'haier', 'huaqin', 'inventec', 'lg', 'pegatron',
      'pegatron(ems)', 'quanta', 'samsung', 'wistron'
  ]

  for name_type, name_list in [('OEM', OEMS), ('ODM', ODMS)]:
    # Construct regex
    name_re = r'\b(%s)\b' % '|'.join([re.escape(x) for x in name_list])
    matches = [x[0] for x in re.findall(name_re, commit_message, re.IGNORECASE)]
    if matches:
      # If there's a match, throw an error.
      error_msg = ('Changelist description contains the name of an'
                   ' %s: "%s".' % (name_type, '","'.join(matches)))
      return HookFailure(error_msg)

  return None


def _check_for_uprev(project, commit, project_top=None):
  """Check that we're not missing a revbump of an ebuild in the given commit.

  If the given commit touches files in a directory that has ebuilds somewhere
  up the directory hierarchy, it's very likely that we need an ebuild revbump
  in order for those changes to take effect.

  It's not totally trivial to detect a revbump, so at least detect that an
  ebuild with a revision number in it was touched.  This should handle the
  common case where we use a symlink to do the revbump.

  TODO: it would be nice to enhance this hook to:
  * Handle cases where people revbump with a slightly different syntax.  I see
    one ebuild (puppy) that revbumps with _pN.  This is a false positive.
  * Catches cases where people aren't using symlinks for revbumps.  If they
    edit a revisioned file directly (and are expected to rename it for revbump)
    we'll miss that.  Perhaps we could detect that the file touched is a
    symlink?

  If a project doesn't use symlinks we'll potentially miss a revbump, but we're
  still better off than without this check.

  Args:
    project: The Project to look at
    commit: The commit to look at
    project_top: Top dir to process commits in

  Returns:
    A HookFailure or None.
  """
  # If this is the portage-stable overlay, then ignore the check.  It's rare
  # that we're doing anything other than importing files from upstream, so
  # forcing a rev bump makes no sense.
  allowlist = (
      'chromiumos/overlays/portage-stable',
  )
  if project.name in allowlist:
    return None

  def FinalName(obj):
    # If the file is being deleted, then the dst_file is not set.
    if obj.dst_file is None:
      return obj.src_file
    else:
      return obj.dst_file

  def AllowedPath(obj):
    allowed_files = {
        'ChangeLog', 'Manifest', 'metadata.xml', 'OWNERS', 'README.md',
    }
    allowed_directories = {'profiles'}

    affected = pathlib.Path(FinalName(obj))
    if affected.name in allowed_files:
      return True

    for directory in allowed_directories:
      if directory in affected.parts:
        return True

    return False

  affected_path_objs = _get_affected_files(
      commit, include_deletes=True, include_symlinks=True, relative=True,
      full_details=True)

  # Don't yell about changes to allowed files or directories...
  affected_path_objs = [x for x in affected_path_objs
                        if not AllowedPath(x)]
  if not affected_path_objs:
    return None

  # If we've touched any file named with a -rN.ebuild then we'll say we're
  # OK right away.  See TODO above about enhancing this.
  touched_revved_ebuild = any(re.search(r'-r\d*\.ebuild$', FinalName(x))
                              for x in affected_path_objs)
  if touched_revved_ebuild:
    return None

  # If we're creating new ebuilds from scratch, then we don't need an uprev.
  # Find all the dirs that new ebuilds and ignore their files/.
  ebuild_dirs = [os.path.dirname(FinalName(x)) + '/' for x in affected_path_objs
                 if FinalName(x).endswith('.ebuild') and x.status == 'A']
  affected_path_objs = [obj for obj in affected_path_objs
                        if not any(FinalName(obj).startswith(x)
                                   for x in ebuild_dirs)]
  if not affected_path_objs:
    return None

  # We want to examine the current contents of all directories that are parents
  # of files that were touched (up to the top of the project).
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
  affected_paths = set(os.path.join(project_top, FinalName(x))
                       for x in affected_path_objs)
  for dir_path in dirs_to_check:
    contents = os.listdir(dir_path)
    ebuilds = [os.path.join(dir_path, path)
               for path in contents if path.endswith('.ebuild')]
    ebuilds_9999 = [path for path in ebuilds if path.endswith('-9999.ebuild')]

    affected_paths_under_9999_ebuilds = set()
    for affected_path in affected_paths:
      for ebuild_9999 in ebuilds_9999:
        ebuild_dir = os.path.dirname(ebuild_9999)
        if affected_path.startswith(ebuild_dir):
          affected_paths_under_9999_ebuilds.add(affected_path)

    # If every file changed exists under a 9999 ebuild, then skip
    if len(affected_paths_under_9999_ebuilds) == len(affected_paths):
      continue

    # If the -9999.ebuild file was touched the bot will uprev for us.
    # ...we'll use a simple intersection here as a heuristic...
    if set(ebuilds_9999) & affected_paths:
      continue

    if ebuilds:
      return HookFailure('Changelist probably needs a revbump of an ebuild, '
                         'or a -r1.ebuild symlink if this is a new ebuild:\n'
                         '%s' % dir_path)

  return None


def _check_ebuild_eapi(project, commit):
  """Make sure we have people use EAPI=5 or newer with custom ebuilds.

  We want to get away from older EAPI's as it makes life confusing and they
  have less builtin error checking.

  Args:
    project: The Project to look at
    commit: The commit to look at

  Returns:
    A HookFailure or None.
  """
  # If this is the portage-stable overlay, then ignore the check.  It's rare
  # that we're doing anything other than importing files from upstream, and
  # we shouldn't be rewriting things fundamentally anyways.
  allowlist = (
      'chromiumos/overlays/portage-stable',
  )
  if project.name in allowlist:
    return None

  BAD_EAPIS = ('0', '1', '2', '3', '4')

  get_eapi = re.compile(r'^\s*EAPI=[\'"]?([^\'"]+)')

  ebuilds_re = [r'\.ebuild$']
  ebuilds = _filter_files(_get_affected_files(commit, relative=True),
                          ebuilds_re)
  bad_ebuilds = []

  for ebuild in ebuilds:
    # If the ebuild does not specify an EAPI, it defaults to 0.
    eapi = '0'

    lines = _get_file_content(ebuild, commit).splitlines()
    if len(lines) == 1:
      # This is most likely a symlink, so skip it entirely.
      continue

    for line in lines:
      m = get_eapi.match(line)
      if m:
        # Once we hit the first EAPI line in this ebuild, stop processing.
        # The spec requires that there only be one and it be first, so
        # checking all possible values is pointless.  We also assume that
        # it's "the" EAPI line and not something in the middle of a heredoc.
        eapi = m.group(1)
        break

    if eapi in BAD_EAPIS:
      bad_ebuilds.append((ebuild, eapi))

  if bad_ebuilds:
    # pylint: disable=C0301
    url = 'https://dev.chromium.org/chromium-os/how-tos-and-troubleshooting/upgrade-ebuild-eapis'
    # pylint: enable=C0301
    return HookFailure(
        'These ebuilds are using old EAPIs.  If these are imported from\n'
        'Gentoo, then you may ignore and upload once with the --no-verify\n'
        'flag.  Otherwise, please update to 7 or newer.\n'
        '\t%s\n'
        'See this guide for more details:\n%s\n' %
        ('\n\t'.join(['%s: EAPI=%s' % x for x in bad_ebuilds]), url))

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
    A HookFailure or None.
  """
  ALLOWLIST = set(('*', '-*', '~*'))

  get_keywords = re.compile(r'^\s*KEYWORDS="(.*)"')

  ebuilds_re = [r'\.ebuild$']
  ebuilds = _filter_files(_get_affected_files(commit, relative=True),
                          ebuilds_re)

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
    return HookFailure(
        '%s\n'
        'Please update KEYWORDS to use a glob:\n'
        'If the ebuild should be marked stable (normal for non-9999 ebuilds):\n'
        '  KEYWORDS="*"\n'
        'If the ebuild should be marked unstable (normal for '
        'cros-workon / 9999 ebuilds):\n'
        '  KEYWORDS="~*"\n'
        'If the ebuild needs to be marked for only specific arches, '
        'then use -* like so:\n'
        '  KEYWORDS="-* arm ..."\n' % '\n* '.join(bad_ebuilds))

  return None


def _check_ebuild_licenses(_project, commit):
  """Check if the LICENSE field in the ebuild is correct."""
  affected_paths = _get_affected_files(commit, relative=True)
  touched_ebuilds = [x for x in affected_paths if x.endswith('.ebuild')]

  # A list of licenses to ignore for now.
  LICENSES_IGNORE = ['||', '(', ')']

  errors = []
  for ebuild in touched_ebuilds:
    # e.g. path/to/overlay/category/package/package.ebuild -> path/to/overlay
    overlay_path = os.sep.join(ebuild.split(os.sep)[:-3])

    try:
      ebuild_content = _get_file_content(ebuild, commit)
      license_types = licenses_lib.GetLicenseTypesFromEbuild(ebuild_content,
                                                             overlay_path)
    except ValueError as e:
      errors.append(HookFailure(str(e), [ebuild]))
      continue

    # Virtual packages must use "metapackage" license.
    if ebuild.split('/')[-3] == 'virtual':
      if license_types != ['metapackage']:
        errors.append(HookFailure(
            'Virtual package must use LICENSE="metapackage".', [ebuild]))
        continue

    # Also ignore licenses ending with '?'
    for license_type in [x for x in license_types
                         if x not in LICENSES_IGNORE and not x.endswith('?')]:
      try:
        licenses_lib.Licensing.FindLicenseType(license_type,
                                               overlay_path=overlay_path)
      except AssertionError as e:
        errors.append(HookFailure(str(e), [ebuild]))
        continue

  return errors


def _check_ebuild_owners(project, commit):
  """Require all new packages include an OWNERS file."""
  # Look for all adds/removes since we're going to ignore changes that only
  # update a package.  We only want to flag new package imports for now.
  affected_files_objs = _get_affected_files(
      commit, include_deletes=True, include_symlinks=True, relative=True,
      full_details=True)

  # If this CL doesn't include any ebuilds, don't bother complaining.
  new_ebuilds = [x for x in affected_files_objs
                 if x.status == 'A' and x.src_file.endswith('.ebuild')]
  if not new_ebuilds:
    return None

  # Check each package dir.
  packages_missing_owners = []
  package_dirs = sorted(set(os.path.dirname(x.src_file) for x in new_ebuilds))
  for package_dir in package_dirs:
    package_files = [
        x for x in affected_files_objs
        if (x.src_file and x.src_file.startswith(f'{package_dir}/')) or
           (x.dst_file and x.dst_file.startswith(f'{package_dir}/'))]

    # Only complain about new ebuilds, not existing ones.  For now.
    # We'll assume that "all adds" means it's a new package.
    if any(x for x in package_files if x.status != 'A'):
      continue

    # See if there's an OWNERS file in there already.
    data = _get_file_content(os.path.join(package_dir, 'OWNERS'), commit)
    if not data:
      # Allow specific overlays to declare OWNERS for all packages.
      if (project.name == 'chromiumos/overlays/board-overlays' or
          re.match(r'^chromeos/overlays/(baseboard|chipset|project|overlay)-',
                   project.name)):
        data = _get_file_content(os.path.join(os.path.dirname(os.path.dirname(
            package_dir)), 'OWNERS'), commit)

    if not data:
      packages_missing_owners.append(package_dir)
      continue

    # Require specific people and not just *.
    lines = {x for x in data.splitlines() if x.split('#', 1)[0].strip()}
    if not lines - {'*'}:
      packages_missing_owners.append(package_dir)

  if packages_missing_owners:
    return HookFailure(
        'All new packages must have an OWNERS file filled out.',
        packages_missing_owners)

  return None


def _check_ebuild_r0(_project, commit):
  """Do not allow ebuilds to end with -r0 versions."""
  ebuilds = _filter_files(
      _get_affected_files(commit, include_symlinks=True, relative=True),
      (r'-r0\.ebuild$',))
  if ebuilds:
    return HookFailure(
        'The -r0 in ebuilds is redundant and confusing. Simply remove it.\n'
        'For example: git mv foo-1.0-r0.ebuild foo-1.0.ebuild',
        ebuilds)

  return None


def _check_ebuild_virtual_pv(project, commit):
  """Enforce the virtual PV policies."""
  # If this is the portage-stable overlay, then ignore the check.
  # We want to import virtuals as-is from upstream Gentoo.
  allowlist = (
      'chromiumos/overlays/portage-stable',
  )
  if project.name in allowlist:
    return None

  # Per-project listings of packages with virtuals known to come from upstream
  # Gentoo, so we shouldn't complain about them.
  if project.name == 'chromiumos/overlays/chromiumos':
    pkg_allowlist = ('rust',)
  else:
    pkg_allowlist = ()

  # We assume the repo name is the same as the dir name on disk.
  # It would be dumb to not have them match though.
  project_base = os.path.basename(project.name)

  is_variant = lambda x: x.startswith('overlay-variant-')
  is_board = lambda x: x.startswith('overlay-')
  is_baseboard = lambda x: x.startswith('baseboard-')
  is_chipset = lambda x: x.startswith('chipset-')
  is_project = lambda x: x.startswith('project-')
  is_private = lambda x: x.endswith('-private')

  is_special_overlay = lambda x: (is_board(x) or is_chipset(x) or
                                  is_baseboard(x) or is_project(x))

  get_pv = re.compile(r'(.*?)virtual/([^/]+)/\2-([^/]*)\.ebuild$')

  ebuilds_re = [r'\.ebuild$']
  ebuilds = _filter_files(_get_affected_files(commit, relative=True),
                          ebuilds_re)
  bad_ebuilds = []

  for ebuild in ebuilds:
    m = get_pv.match(ebuild)
    if not m:
      continue

    overlay, package_name, pv = m.groups()
    if package_name in pkg_allowlist:
      continue

    pv = pv.split('-', 1)[0]
    if not overlay or not is_special_overlay(overlay):
      overlay = project_base

    # Virtual versions >= 4 are special cases used above the standard
    # versioning structure, e.g. if one has a board inheriting a board.
    if pv[0] >= '4':
      want_pv = pv
    elif is_board(overlay):
      if is_private(overlay):
        want_pv = '3.5' if is_variant(overlay) else '3'
      elif is_board(overlay):
        want_pv = '2.5' if is_variant(overlay) else '2'
    elif is_baseboard(overlay):
      want_pv = '1.9.5' if is_private(overlay) else '1.9'
    elif is_chipset(overlay):
      want_pv = '1.8.5' if is_private(overlay) else '1.8'
    elif is_project(overlay):
      want_pv = '1.7' if is_private(overlay) else '1.5'
    else:
      want_pv = '1'

    if pv != want_pv:
      bad_ebuilds.append((ebuild, pv, want_pv))

  if bad_ebuilds:
    # pylint: disable=C0301
    url = 'http://dev.chromium.org/chromium-os/how-tos-and-troubleshooting/portage-build-faq#TOC-Virtuals-and-central-management'
    # pylint: enable=C0301
    return HookFailure(
        'These virtuals have incorrect package versions (PVs). Please adjust:\n'
        '\t%s\n'
        'If this is an upstream Gentoo virtual, then you may ignore this\n'
        'check (and re-run w/--no-verify). Otherwise, please see this\n'
        'page for more details:\n%s\n' %
        ('\n\t'.join(['%s:\n\t\tPV is %s but should be %s' % x
                      for x in bad_ebuilds]), url))

  return None


def _check_portage_make_use_var(_project, commit):
  """Verify that $USE is set correctly in make.conf and make.defaults."""
  files = _filter_files(_get_affected_files(commit, relative=True),
                        [r'(^|/)make.(conf|defaults)$'])

  errors = []
  for path in files:
    basename = os.path.basename(path)

    # Has a USE= line already been encountered in this file?
    saw_use = False

    for i, line in enumerate(_get_file_content(path, commit).splitlines(), 1):
      if not line.startswith('USE='):
        continue

      preserves_use = '${USE}' in line or '$USE' in line

      if (basename == 'make.conf' or
          (basename == 'make.defaults' and saw_use)) and not preserves_use:
        errors.append('%s:%d: missing ${USE}' % (path, i))
      elif basename == 'make.defaults' and not saw_use and preserves_use:
        errors.append('%s:%d: ${USE} referenced in initial declaration' %
                      (path, i))

      saw_use = True

  if errors:
    return HookFailure(
        'One or more Portage make files appear to set USE incorrectly.\n'
        '\n'
        'All USE assignments in make.conf and all assignments after the\n'
        'initial declaration in make.defaults should contain "${USE}" to\n'
        'preserve previously-set flags.\n'
        '\n'
        'The initial USE declaration in make.defaults should not contain\n'
        '"${USE}".\n',
        errors)

  return None


def _check_change_has_proper_changeid(_project, commit):
  """Verify that Change-ID is present in last paragraph of commit message."""
  CHANGE_ID_RE = r'\nChange-Id: I[a-f0-9]+\n'
  desc = _get_commit_desc(commit)
  m = re.search(CHANGE_ID_RE, desc)
  if not m:
    return HookFailure('Last paragraph of description must include Change-Id.')

  # S-o-b and some other tags always allowed to follow Change-ID in the footer.
  allowed_tags = ['Signed-off-by', 'Cq-Cl-Tag', 'Cq-Include-Trybots']

  end = desc[m.end():].strip().splitlines()
  cherry_pick_marker = 'cherry picked from commit'

  if end and cherry_pick_marker in end[-1]:
    # Cherry picked patches allow more tags in the last paragraph.
    allowed_tags += ['Commit-Queue', 'Commit-Ready', 'Reviewed-by',
                     'Reviewed-on', 'Tested-by']
    end = end[:-1]

  # Note that descriptions could have multiple cherry pick markers.
  tag_search = r'^(%s:|\(%s) ' % (':|'.join(allowed_tags), cherry_pick_marker)

  if [x for x in end if not re.search(tag_search, x)]:
    return HookFailure('Only "%s:" tag(s) may follow the Change-Id.' %
                       ':", "'.join(allowed_tags))

  return None


def _check_commit_message_style(_project, commit):
  """Verify that the commit message matches our style.

  We do not check for BUG=/TEST=/etc... lines here as that is handled by other
  commit hooks.
  """
  SEE_ALSO = 'Please review the documentation:\n%s' % (DOC_COMMIT_MSG_URL,)

  desc = _get_commit_desc(commit)

  # The first line should be by itself.
  lines = desc.splitlines()
  if len(lines) > 1 and lines[1]:
    return HookFailure('The second line of the commit message must be blank.'
                       '\n%s' % (SEE_ALSO,))

  # The first line should be one sentence.
  if '. ' in lines[0]:
    return HookFailure('The first line cannot be more than one sentence.\n%s' %
                       (SEE_ALSO,))

  # The first line cannot be too long.
  MAX_FIRST_LINE_LEN = 100
  first_line = lines[0]
  if len(first_line) > MAX_FIRST_LINE_LEN:
    return HookFailure('The first line must be less than %i chars.\n%s' %
                       (MAX_FIRST_LINE_LEN, SEE_ALSO))

  # Don't allow random git keywords.
  if first_line.startswith('fixup!') or first_line.startswith('squash!'):
    return HookFailure(
        'Git fixup/squash commit detected: rebase your local branch, or '
        'cleanup the commit message')

  return None


def _check_cros_license(_project, commit, options=()):
  """Verifies the Chromium OS license/copyright header.

  Should be following the spec:
  http://dev.chromium.org/developers/coding-style#TOC-File-headers
  """
  # For older years, be a bit more flexible as our policy says leave them be.
  LICENSE_HEADER = (
      r'.*Copyright(?: \(c\))? (20[0-9]{2})(?:-20[0-9]{2})? The Chromium OS '
      r'Authors\. All rights reserved\.\n'
      r'.*Use of this source code is governed by a BSD-style license that can '
      r'be\n'
      r'.*found in the LICENSE file\.'
      r'\n'
  )
  license_re = re.compile(LICENSE_HEADER, re.MULTILINE)

  # For newer years, be stricter.
  BAD_COPYRIGHT_LINE = (
      r'.*Copyright \(c\) 20(1[5-9]|[2-9][0-9]) The Chromium OS Authors\. '
      r'All rights reserved\.' r'\n'
  )
  bad_copyright_re = re.compile(BAD_COPYRIGHT_LINE)

  included, excluded = _parse_common_inclusion_options(options)

  bad_files = []
  bad_copyright_files = []
  bad_year_files = []

  files = _filter_files(
      _get_affected_files(commit, relative=True),
      included + COMMON_INCLUDED_PATHS,
      excluded + COMMON_EXCLUDED_PATHS + LICENSE_EXCLUDED_PATHS)
  existing_files = set(_get_affected_files(commit, relative=True,
                                           include_adds=False))

  current_year = str(datetime.datetime.now().year)
  for f in files:
    contents = _get_file_content(f, commit)
    if not contents:
      # Ignore empty files.
      continue

    m = license_re.search(contents)
    if not m:
      bad_files.append(f)
    elif bad_copyright_re.search(contents):
      bad_copyright_files.append(f)

    if m and f not in existing_files:
      year = m.group(1)
      if year != current_year:
        bad_year_files.append(f)

  errors = []
  if bad_files:
    msg = '%s:\n%s\n%s' % (
        'License must match', license_re.pattern,
        'Found a bad header in these files:')
    errors.append(HookFailure(msg, bad_files))
  if bad_copyright_files:
    msg = 'Do not use (c) in copyright headers in new files:'
    errors.append(HookFailure(msg, bad_copyright_files))
  if bad_year_files:
    msg = 'Use current year (%s) in copyright headers in new files:' % (
        current_year)
    errors.append(HookFailure(msg, bad_year_files))

  return errors


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
      excluded + COMMON_EXCLUDED_PATHS + LICENSE_EXCLUDED_PATHS)

  bad_files = []
  for f in files:
    contents = _get_file_content(f, commit)
    if not contents:
      # Ignore empty files.
      continue

    if not license_re.search(contents):
      bad_files.append(f)

  if bad_files:
    msg = ('License must match:\n%s\nFound a bad header in these files:' %
           license_re.pattern)
    return HookFailure(msg, bad_files)
  return None


def _check_layout_conf(_project, commit):
  """Verifies the metadata/layout.conf file."""
  repo_name = 'profiles/repo_name'
  repo_names = []
  layout_path = 'metadata/layout.conf'
  layout_paths = []

  # Handle multiple overlays in a single commit (like the public tree).
  for f in _get_affected_files(commit, relative=True):
    if f.endswith(repo_name):
      repo_names.append(f)
    elif f.endswith(layout_path):
      layout_paths.append(f)

  # Disallow new repos with the repo_name file.
  if repo_names:
    return HookFailure('%s: use "repo-name" in %s instead' %
                       (repo_names, layout_path))

  # Gather all the errors in one pass so we show one full message.
  all_errors = {}
  for layout_path in layout_paths:
    all_errors[layout_path] = errors = []

    # Make sure the config file is sorted.
    data = [x for x in _get_file_content(layout_path, commit).splitlines()
            if x and x[0] != '#']
    if sorted(data) != data:
      errors += ['keep lines sorted']

    # Require people to set specific values all the time.
    settings = (
        # TODO: Enable this for everyone.  http://crbug.com/408038
        # ('fast caching', 'cache-format = md5-dict'),
        ('fast manifests', 'thin-manifests = true'),
        ('extra features', 'profile-formats = portage-2 profile-default-eapi'),
        ('newer eapi', 'profile_eapi_when_unspecified = 5-progress'),
    )
    for reason, line in settings:
      if line not in data:
        errors += ['enable %s with: %s' % (reason, line)]

    # Require one of these settings.
    if 'use-manifests = strict' not in data:
      errors += ['enable file checking with: use-manifests = strict']

    # Require repo-name to be set.
    for line in data:
      if line.startswith('repo-name = '):
        break
    else:
      errors += ['set the board name with: repo-name = $BOARD']

  # Summarize all the errors we saw (if any).
  lines = ''
  for layout_path, errors in all_errors.items():
    if errors:
      lines += '\n\t- '.join(['\n* %s:' % layout_path] + errors)
  if lines:
    lines = 'See the portage(5) man page for layout.conf details' + lines + '\n'
    return HookFailure(lines)

  return None


def _check_no_new_gyp(_project, commit):
  """Verifies no project starts to use GYP."""
  gypfiles = _filter_files(
      _get_affected_files(commit, include_symlinks=True, relative=True),
      [r'\.gyp$'])

  if gypfiles:
    return HookFailure('GYP is deprecated and not allowed in a new project:',
                       gypfiles)
  return None


# Project-specific hooks


def _check_clang_format(_project, commit, options=()):
  """Runs clang-format on the given project"""
  hooks_dir = _get_hooks_dir()
  options = list(options)
  if commit == PRE_SUBMIT:
    options.append('--commit=HEAD')
  else:
    options.extend(['--commit', commit])
  cmd = [os.path.join(hooks_dir, 'clang-format.py')] + options
  cmd_result = cros_build_lib.run(cmd,
                                  print_cmd=False,
                                  stdout=True,
                                  encoding='utf-8',
                                  errors='replace',
                                  stderr=subprocess.STDOUT,
                                  check=False)
  if cmd_result.returncode:
    return HookFailure('clang-format.py errors/warnings\n\n' +
                       cmd_result.stdout)
  return None


def _run_checkpatch(_project, commit, options=()):
  """Runs checkpatch.pl on the given project"""
  # Bypass checkpatch for upstream or almost upstream commits, since we do not
  # intend to modify the upstream commits when landing them to our branches.
  # Any fixes should sent as independent patches.
  # The check is retained for FROMLIST and BACKPORT commits, as by definition
  # those can be still fixed up.
  desc = _get_commit_desc(commit)
  if desc.startswith('UPSTREAM:') or desc.startswith('FROMGIT:'):
    return None

  hooks_dir = _get_hooks_dir()
  options = list(options)
  if options and options[0].startswith('./') and os.path.exists(options[0]):
    cmdpath = options.pop(0)
  else:
    cmdpath = os.path.join(hooks_dir, 'checkpatch.pl')
  if commit == PRE_SUBMIT:
    # The --ignore option must be present and include 'MISSING_SIGN_OFF' in
    # this case.
    options.append('--ignore=MISSING_SIGN_OFF')
  # Always ignore the check for the MAINTAINERS file.  We do not track that
  # information on that file in our source trees, so let's suppress the
  # warning.
  options.append('--ignore=FILE_PATH_CHANGES')
  # Do not complain about the Change-Id: fields, since we use Gerrit.
  # Upstream does not want those lines (since they do not use Gerrit), but
  # we always do, so disable the check globally.
  options.append('--ignore=GERRIT_CHANGE_ID')
  cmd = [cmdpath] + options + ['-']
  cmd_result = cros_build_lib.run(
      cmd, print_cmd=False, input=_get_patch(commit).encode('utf-8'),
      stdout=True, stderr=subprocess.STDOUT, check=False, encoding='utf-8',
      errors='replace')
  if cmd_result.returncode:
    return HookFailure('%s errors/warnings\n\n%s' %
                       (cmdpath, cmd_result.stdout))
  return None


def _run_kerneldoc(_project, commit, options=()):
  """Runs kernel-doc validator on the given project"""
  included, excluded = _parse_common_inclusion_options(options)
  files = _filter_files(_get_affected_files(commit, relative=True),
                        included, excluded)
  if files:
    hooks_dir = _get_hooks_dir()
    cmd = [os.path.join(hooks_dir, 'kernel-doc'), '-none'] + files
    output = _run_command(cmd, stderr=subprocess.STDOUT)
    if output:
      return HookFailure('kernel-doc errors/warnings:',
                         items=output.splitlines())
  return None


def _kernel_configcheck(_project, commit):
  """Makes sure kernel config changes are not mixed with code changes"""
  files = _get_affected_files(commit)
  if len(_filter_files(files, [r'chromeos/config'])) not in [0, len(files)]:
    return HookFailure('Changes to chromeos/config/ and regular files must '
                       'be in separate commits:\n%s' % '\n'.join(files))
  return None


def _run_json_check(_project, commit):
  """Checks that all JSON files are syntactically valid."""
  ret = []

  files = _filter_files(_get_affected_files(commit, relative=True),
                        [r'.*\.json$'])
  for f in files:
    data = _get_file_content(f, commit)
    try:
      json.loads(data)
    except Exception as e:
      ret.append('%s: Invalid JSON: %s' % (f, e))

  if ret:
    return HookFailure('\n'.join(ret))
  return None


def _check_manifests(_project, commit):
  """Make sure Manifest files only have comments & DIST lines."""
  ret = []

  manifests = _filter_files(_get_affected_files(commit, relative=True),
                            [r'.*/Manifest$'])
  for path in manifests:
    data = _get_file_content(path, commit)

    # Disallow blank files.
    if not data.strip():
      ret.append('%s: delete empty file' % (path,))
      continue

    # Make sure the last newline isn't omitted.
    if data[-1] != '\n':
      ret.append('%s: missing trailing newline' % (path,))

    # Do not allow leading or trailing blank lines.
    lines = data.splitlines()
    if not lines[0]:
      ret.append('%s: delete leading blank lines' % (path,))
    if not lines[-1]:
      ret.append('%s: delete trailing blank lines' % (path,))

    for line in lines:
      # Disallow leading/trailing whitespace.
      if line != line.strip():
        ret.append('%s: remove leading/trailing whitespace: %s' % (path, line))

      # Allow blank lines & comments.
      line = line.split('#', 1)[0]
      if not line:
        continue

      # All other linse should start with DIST.
      if not line.startswith('DIST '):
        ret.append('%s: remove non-DIST lines: %s' % (path, line))
        break

  if ret:
    return HookFailure('\n'.join(ret))
  return None


def _check_change_has_branch_field(_project, commit):
  """Check for a non-empty 'BRANCH=' field in the commit message."""
  if commit == PRE_SUBMIT:
    return None
  BRANCH_RE = r'\nBRANCH=\S+'

  if not re.search(BRANCH_RE, _get_commit_desc(commit)):
    msg = ('Changelist description needs BRANCH field (after first line)\n'
           'E.g. BRANCH=none or BRANCH=link,snow')
    return HookFailure(msg)
  return None


def _check_change_has_no_branch_field(_project, commit):
  """Verify 'BRANCH=' field does not exist in the commit message."""
  if commit == PRE_SUBMIT:
    return None
  BRANCH_RE = r'\nBRANCH=\S+'

  if re.search(BRANCH_RE, _get_commit_desc(commit)):
    msg = 'This checkout does not use BRANCH= fields.  Delete them.'
    return HookFailure(msg)
  return None


def _check_change_has_signoff_field(_project, commit):
  """Check for a non-empty 'Signed-off-by:' field in the commit message."""
  if commit == PRE_SUBMIT:
    return None
  SIGNOFF_RE = r'\nSigned-off-by: \S+'

  if not re.search(SIGNOFF_RE, _get_commit_desc(commit)):
    msg = ('Changelist description needs Signed-off-by: field\n'
           'E.g. Signed-off-by: My Name <me@chromium.org>')
    return HookFailure(msg)
  return None


def _check_change_has_no_signoff_field(_project, commit):
  """Verify 'Signed-off-by:' field does not exist in the commit message."""
  if commit == PRE_SUBMIT:
    return None
  SIGNOFF_RE = r'\nSigned-off-by: \S+'

  if re.search(SIGNOFF_RE, _get_commit_desc(commit)):
    msg = 'This checkout does not use Signed-off-by: tags.  Delete them.'
    return HookFailure(msg)
  return None


def _run_project_hook_script(script, project, commit):
  """Runs a project hook script.

  The script is run with the following environment variables set:
    PRESUBMIT_PROJECT: The affected project
    PRESUBMIT_COMMIT: The affected commit
    PRESUBMIT_FILES: A newline-separated list of affected files

  The script is considered to fail if the exit code is non-zero.  It should
  write an error message to stdout.
  """
  env = dict(os.environ)
  env['PRESUBMIT_PROJECT'] = project.name
  env['PRESUBMIT_COMMIT'] = commit

  # Put affected files in an environment variable
  files = _get_affected_files(commit)
  env['PRESUBMIT_FILES'] = '\n'.join(files)

  cmd_result = cros_build_lib.run(cmd=script,
                                  env=env,
                                  shell=True,
                                  print_cmd=False,
                                  input=os.devnull,
                                  stdout=True,
                                  encoding='utf-8',
                                  errors='replace',
                                  stderr=subprocess.STDOUT,
                                  check=False)
  if cmd_result.returncode:
    stdout = cmd_result.stdout
    if stdout:
      stdout = re.sub('(?m)^', '  ', stdout)
    return HookFailure('Hook script "%s" failed with code %d%s' %
                       (script, cmd_result.returncode,
                        ':\n' + stdout if stdout else ''))
  return None


def _check_project_prefix(_project, commit):
  """Require the commit message have a project specific prefix as needed."""

  files = _get_affected_files(commit, include_deletes=True, relative=True)
  prefix = os.path.commonprefix(files)
  prefix = os.path.dirname(prefix)

  # If there is no common prefix, the CL span multiple projects.
  if not prefix:
    return None

  project_name = prefix.split('/')[0]

  # The common files may all be within a subdirectory of the main project
  # directory, so walk up the tree until we find an alias file.
  # _get_affected_files() should return relative paths, but check against '/' to
  # ensure that this loop terminates even if it receives an absolute path.
  while prefix and prefix != '/':
    alias_file = os.path.join(prefix, '.project_alias')

    # If an alias exists, use it.
    if os.path.isfile(alias_file):
      project_name = osutils.ReadFile(alias_file).strip()

    prefix = os.path.dirname(prefix)

  if not _get_commit_desc(commit).startswith(project_name + ': '):
    return HookFailure('The commit title for changes affecting only %s'
                       ' should start with \"%s: \"'
                       % (project_name, project_name))
  return None


def _check_filepath_chartype(_project, commit):
  """Checks that FilePath::CharType stuff is not used."""

  FILEPATH_REGEXP = re.compile('|'.join(
      [r'(?:base::)?FilePath::(?:Char|String|StringPiece)Type',
       r'(?:base::)?FilePath::FromUTF8Unsafe',
       r'AsUTF8Unsafe',
       r'FILE_PATH_LITERAL']))
  files = _filter_files(_get_affected_files(commit, relative=True),
                        [r'.*\.(cc|h)$'])

  errors = []
  for afile in files:
    for line_num, line in _get_file_diff(afile, commit):
      m = re.search(FILEPATH_REGEXP, line)
      if m:
        errors.append('%s, line %s has %s' % (afile, line_num, m.group(0)))

  if errors:
    msg = 'Please assume FilePath::CharType is char (crbug.com/870621):'
    return HookFailure(msg, errors)
  return None


def _check_exec_files(_project, commit):
  """Make +x bits on files."""
  # List of files that should never be +x.
  NO_EXEC = (
      'ChangeLog*',
      'COPYING',
      'make.conf',
      'make.defaults',
      'Manifest',
      'OWNERS',
      'package.use',
      'package.keywords',
      'package.mask',
      'parent',
      'README',
      'TODO',
      '.gitignore',
      '*.[achly]',
      '*.[ch]xx',
      '*.boto',
      '*.cc',
      '*.cfg',
      '*.conf',
      '*.config',
      '*.cpp',
      '*.css',
      '*.ebuild',
      '*.eclass',
      '*.gn',
      '*.gni',
      '*.gyp',
      '*.gypi',
      '*.htm',
      '*.html',
      '*.ini',
      '*.js',
      '*.json',
      '*.md',
      '*.mk',
      '*.patch',
      '*.policy',
      '*.proto',
      '*.raw',
      '*.rules',
      '*.service',
      '*.target',
      '*.txt',
      '*.xml',
      '*.yaml',
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
    return HookFailure('These files should not be executable.  '
                       'Please `chmod -x` them.', bad_files)
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
]

# A list of hooks that are not project-specific
_COMMON_HOOKS = [
    _check_cargo_clippy,
    _check_cros_license,
    _check_ebuild_eapi,
    _check_ebuild_keywords,
    _check_ebuild_licenses,
    _check_ebuild_owners,
    _check_ebuild_r0,
    _check_ebuild_virtual_pv,
    _check_exec_files,
    _check_for_uprev,
    _check_gofmt,
    _check_keywords,
    _check_layout_conf,
    _check_no_long_lines,
    _check_no_new_gyp,
    _check_no_stray_whitespace,
    _check_no_tabs,
    _check_portage_make_use_var,
    _check_rustfmt,
    _check_tabbed_indents,
]


# A dictionary of flags (keys) that can appear in the config file, and the hook
# that the flag controls (value).
_HOOK_FLAGS = {
    'clang_format_check': _check_clang_format,
    'checkpatch_check': _run_checkpatch,
    'kerneldoc_check': _run_kerneldoc,
    'stray_whitespace_check': _check_no_stray_whitespace,
    'json_check': _run_json_check,
    'long_line_check': _check_no_long_lines,
    'cros_license_check': _check_cros_license,
    'aosp_license_check': _check_aosp_license,
    'gofmt_check': _check_gofmt,
    'keyword_check': _check_keywords,
    'tab_check': _check_no_tabs,
    'tabbed_indent_required_check': _check_tabbed_indents,
    'branch_check': _check_change_has_branch_field,
    'signoff_check': _check_change_has_signoff_field,
    'bug_field_check': _check_change_has_bug_field,
    'test_field_check': _check_change_has_test_field,
    'manifest_check': _check_manifests,
    'contribution_check': _check_change_is_contribution,
    'project_prefix_check': _check_project_prefix,
    'filepath_chartype_check': _check_filepath_chartype,
    'cargo_clippy_check': _check_cargo_clippy,
    'exec_files_check': _check_exec_files,
    'kernel_splitconfig_check': _kernel_configcheck,
}


def _get_override_hooks(config):
  """Returns a set of hooks controlled by the current project's config file.

  Expects to be called within the project root.

  Args:
    config: A ConfigParser for the project's config file.
  """
  SECTION = 'Hook Overrides'
  SECTION_OPTIONS = 'Hook Overrides Options'

  valid_keys = set(_HOOK_FLAGS.keys())
  hooks = _HOOK_FLAGS.copy()
  hook_overrides = set(
      config.options(SECTION) if config.has_section(SECTION) else [])

  unknown_keys = hook_overrides - valid_keys
  if unknown_keys:
    raise ValueError(f'{_CONFIG_FILE}: [{SECTION}]: unknown keys: '
                     f'{unknown_keys}')

  enable_flags = []
  disable_flags = []
  for flag in valid_keys:
    if flag in hook_overrides:
      try:
        enabled = config.getboolean(SECTION, flag)
      except ValueError as e:
        raise ValueError('Error: parsing flag "%s" in "%s" failed: %s' %
                         (flag, _CONFIG_FILE, e))
    elif hooks[flag] in _COMMON_HOOKS:
      # Enable common hooks by default so we process custom options below.
      enabled = True
    else:
      # All other hooks we left as a tristate.  We use this below for a few
      # hooks to control default behavior.
      enabled = None

    if enabled:
      enable_flags.append(flag)
    elif enabled is not None:
      disable_flags.append(flag)

    # See if this hook has custom options.
    if enabled:
      try:
        options = config.get(SECTION_OPTIONS, flag)
        hooks[flag] = functools.partial(hooks[flag], options=options.split())
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
  SECTION = 'Hook Scripts'
  if not config.has_section(SECTION):
    return []

  return config.items(SECTION)


def _get_project_hooks(presubmit, config_file=None):
  """Returns a list of hooks that need to be run for a project.

  Expects to be called from within the project root.

  Args:
    presubmit: A Boolean, True if the check is run as a git pre-submit script.
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
  unwrapped_hooks = [getattr(hook, 'func', hook) for hook in enabled_hooks]
  hooks = [hook for hook in hooks if hook not in unwrapped_hooks]

  hooks = list(enabled_hooks) + hooks

  for name, script in _get_project_hook_scripts(config):
    func = functools.partial(_run_project_hook_script, script)
    func.__name__ = name
    hooks.append(func)

  return hooks


def _run_project_hooks(project_name, proj_dir=None,
                       commit_list=None, presubmit=False,
                       config_file=None):
  """For each project run its project specific hook from the hooks dictionary.

  Args:
    project_name: The name of project to run hooks for.
    proj_dir: If non-None, this is the directory the project is in.  If None,
        we'll ask repo.
    commit_list: A list of commits to run hooks against.  If None or empty list
        then we'll automatically get the list of commits that would be uploaded.
    presubmit: A Boolean, True if the check is run as a git pre-submit script.
    config_file: A string, the presubmit config file. If not specified,
        defaults to PRESUBMIT.cfg in the project directory.

  Returns:
    Boolean value of whether any errors were ecountered while running the hooks.
  """
  if proj_dir is None:
    proj_dirs = _run_command(
        ['repo', 'forall', project_name, '-c', 'pwd']).split()
    if not proj_dirs:
      print('%s cannot be found.' % project_name, file=sys.stderr)
      print('Please specify a valid project.', file=sys.stderr)
      return True
    if len(proj_dirs) > 1:
      print('%s is associated with multiple directories.' % project_name,
            file=sys.stderr)
      print('Please specify a directory to help disambiguate.', file=sys.stderr)
      return True
    proj_dir = proj_dirs[0]

  pwd = os.getcwd()
  # hooks assume they are run from the root of the project
  os.chdir(proj_dir)

  color = terminal.Color()

  remote_branch = _run_command(['git', 'rev-parse', '--abbrev-ref',
                                '--symbolic-full-name', '@{u}']).strip()
  if not remote_branch:
    print("Your project %s doesn't track any remote repo." % project_name,
          file=sys.stderr)
    remote = None
  else:
    branch_items = remote_branch.split('/', 1)
    if len(branch_items) != 2:
      PrintErrorForProject(
          project_name,
          HookFailure(
              'Cannot get remote and branch name (%s)' % remote_branch))
      os.chdir(pwd)
      return True
    remote, _branch = branch_items

  project = Project(name=project_name, dir=proj_dir, remote=remote)

  if not commit_list:
    try:
      commit_list = _get_commits()
    except VerifyException as e:
      PrintErrorForProject(project.name, HookFailure(str(e)))
      os.chdir(pwd)
      return True

  hooks = _get_project_hooks(presubmit, config_file)
  error_found = False
  commit_count = len(commit_list)
  hook_count = len(hooks)
  for i, commit in enumerate(commit_list):
    CACHE.clear()

    # If run with --pre-submit, then commit is PRE_SUBMIT, and not a commit.
    # Use that as the description.
    desc = commit if commit == PRE_SUBMIT else _get_commit_desc(commit)
    print('[%s %i/%i %s] %s' %
          (color.Color(color.CYAN, 'COMMIT'), i + 1, commit_count, commit[0:12],
           desc.splitlines()[0]))

    for h, hook in enumerate(hooks):
      output = ('[%s %i/%i PRESUBMIT.cfg] %s' %
                (color.Color(color.YELLOW, 'RUNNING'), h + 1, hook_count,
                 hook.__name__))
      print(output, end='\r')
      sys.stdout.flush()
      hook_error = hook(project, commit)
      print(' ' * len(output), end='\r')
      sys.stdout.flush()
      if hook_error:
        if not isinstance(hook_error, list):
          hook_error = [hook_error]
        PrintErrorsForCommit(color, hook, project.name, hook_error)
        error_found = True

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
      project_list, so that each entry in project_list matches with a directory
      in worktree_list. If None, we will attempt to calculate the directories
      automatically.
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
    msg = ('%s: Preupload failed due to above error(s).\n'
           '- To disable some source style checks, and for other hints, see '
           '<checkout_dir>/src/repohooks/README.md\n'
           "- To upload only current project, run 'repo upload .'" %
           (color.Color(color.RED, 'FATAL'),))
    print(msg, file=sys.stderr)
    sys.exit(1)
  else:
    msg = ('[%s] repohooks passed in %s' %
           (color.Color(color.GREEN, 'PASSED'), end_time - start_time))
    print(msg)


def _identify_project(path):
  """Identify the repo project associated with the given path.

  Returns:
    A string indicating what project is associated with the path passed in or
    a blank string upon failure.
  """
  return _run_command(['repo', 'forall', '.', '-c', 'echo ${REPO_PROJECT}'],
                      stderr=True, cwd=path).strip()


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
  parser.add_argument('--dir', default=None,
                      help='The directory that the project lives in.  If not '
                      'specified, use the git project root based on the cwd.')
  parser.add_argument('--project', default=None,
                      help='The project repo path; this can affect how the '
                      'hooks get run, since some hooks are project-specific.  '
                      'For chromite this is chromiumos/chromite.  If not '
                      'specified, the repo tool will be used to figure this '
                      'out based on the dir.')
  parser.add_argument('--rerun-since', default=None,
                      help='Rerun hooks on old commits since some point '
                      'in the past.  The argument could be a date (should '
                      "match git log's concept of a date, e.g. 2012-06-20), "
                      'or a SHA1, or just a number of commits to check (from 1 '
                      'to 99).  This option is mutually exclusive with '
                      '--pre-submit.')
  parser.add_argument('--pre-submit', action='store_true',
                      help='Run the check against the pending commit.  '
                      "This option should be used at the 'git commit' "
                      "phase as opposed to 'repo upload'. This option "
                      'is mutually exclusive with --rerun-since.')
  parser.add_argument('--presubmit-config',
                      help='Specify presubmit config file to be used.')
  parser.add_argument('commits', nargs='*',
                      help='Check specific commits')
  opts = parser.parse_args(argv)

  if opts.rerun_since:
    if opts.commits:
      raise BadInvocation("Can't pass commits and use rerun-since: %s" %
                          ' '.join(opts.commits))

    if len(opts.rerun_since) < 3 and opts.rerun_since.isdigit():
      # This must be the number of commits to check. We don't expect the user
      # to want to check more than 99 commits.
      limit = '-n%s' % opts.rerun_since
    elif git.IsSHA1(opts.rerun_since, False):
      limit = '%s..' %  opts.rerun_since
    else:
      # This better be a date.
      limit = '--since=%s' % opts.rerun_since
    cmd = ['git', 'log', limit, '--pretty=%H']
    all_commits = _run_command(cmd).splitlines()
    bot_commits = _run_command(cmd + ['--author=chrome-bot']).splitlines()

    # Eliminate chrome-bot commits but keep ordering the same...
    bot_commits = set(bot_commits)
    opts.commits = [c for c in all_commits if c not in bot_commits]

    if opts.pre_submit:
      raise BadInvocation('rerun-since and pre-submit can not be '
                          'used together')
  if opts.pre_submit:
    if opts.commits:
      raise BadInvocation("Can't pass commits and use pre-submit: %s" %
                          ' '.join(opts.commits))
    opts.commits = [PRE_SUBMIT,]

  # Check/normlaize git dir; if unspecified, we'll use the root of the git
  # project from CWD
  if opts.dir is None:
    git_dir = _run_command(['git', 'rev-parse', '--show-toplevel'],
                           stderr=True).strip()
    if not git_dir:
      raise BadInvocation('The current directory is not part of a git project.')
    opts.dir = git_dir
  elif not os.path.isdir(opts.dir):
    raise BadInvocation('Invalid dir: %s' % opts.dir)
  elif not os.path.isdir(os.path.join(opts.dir, '.git')):
    raise BadInvocation('Not a git directory: %s' % opts.dir)

  # Identify the project if it wasn't specified; this _requires_ the repo
  # tool to be installed and for the project to be part of a repo checkout.
  if not opts.project:
    opts.project = _identify_project(opts.dir)
    if not opts.project:
      raise BadInvocation("Repo couldn't identify the project of %s" % opts.dir)

  found_error = _run_project_hooks(opts.project, proj_dir=opts.dir,
                                   commit_list=opts.commits,
                                   presubmit=opts.pre_submit,
                                   config_file=opts.presubmit_config)
  if found_error:
    return 1
  return 0


if __name__ == '__main__':
  sys.exit(direct_main(sys.argv[1:]))
