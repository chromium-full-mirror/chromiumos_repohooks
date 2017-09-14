#!/usr/bin/env python2
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Presubmit checks to run when doing `repo upload`.

You can add new checks by adding a functions to the HOOKS constants.
"""

from __future__ import print_function

import argparse
import collections
import ConfigParser
import fnmatch
import functools
import json
import os
import re
import sys
import stat
import StringIO

from errors import (VerifyException, HookFailure, PrintErrorForProject,
                    PrintErrorsForCommit)

# If repo imports us, the __name__ will be __builtin__, and the wrapper will
# be in $CHROMEOS_CHECKOUT/.repo/repo/main.py, so we need to go two directories
# up. The same logic also happens to work if we're executed directly.
if __name__ in ('__builtin__', '__main__'):
  sys.path.insert(0, os.path.join(os.path.dirname(sys.argv[0]), '..', '..'))

from chromite.lib import commandline
from chromite.lib import constants
from chromite.lib import cros_build_lib
from chromite.lib import git
from chromite.lib import osutils
from chromite.lib import patch
from chromite.licensing import licenses_lib

PRE_SUBMIT = 'pre-submit'

COMMON_INCLUDED_PATHS = [
    # C++ and friends
    r".*\.c$", r".*\.cc$", r".*\.cpp$", r".*\.h$", r".*\.m$", r".*\.mm$",
    r".*\.inl$", r".*\.asm$", r".*\.hxx$", r".*\.hpp$", r".*\.s$", r".*\.S$",
    # Scripts
    r".*\.js$", r".*\.py$", r".*\.sh$", r".*\.rb$", r".*\.pl$", r".*\.pm$",
    # No extension at all, note that ALL CAPS files are black listed in
    # COMMON_EXCLUDED_LIST below.
    r"(^|.*[\\\/])[^.]+$",
    # Other
    r".*\.java$", r".*\.mk$", r".*\.am$",
    r".*\.policy$", r".*\.conf$",
]


COMMON_EXCLUDED_PATHS = [
    # Avoid doing source file checks for kernel.
    r"/src/third_party/kernel/",
    r"/src/third_party/kernel-next/",
    r"/src/third_party/ktop/",
    r"/src/third_party/punybench/",
    r".*\bexperimental[\\\/].*",
    r".*\b[A-Z0-9_]{2,}$",
    r".*[\\\/]debian[\\\/]rules$",

    # For ebuild trees, ignore any caches and manifest data.
    r".*/Manifest$",
    r".*/metadata/[^/]*cache[^/]*/[^/]+/[^/]+$",

    # Ignore profiles data (like overlay-tegra2/profiles).
    r"(^|.*/)overlay-.*/profiles/.*",
    r"^profiles/.*$",

    # Ignore minified js and jquery.
    r".*\.min\.js",
    r".*jquery.*\.js",

    # Ignore license files as the content is often taken verbatim.
    r".*/licenses/.*",
]


_CONFIG_FILE = 'PRESUBMIT.cfg'


# File containing wildcards, one per line, matching files that should be
# excluded from presubmit checks. Lines beginning with '#' are ignored.
_IGNORE_FILE = '.presubmitignore'


# Exceptions


class BadInvocation(Exception):
  """An Exception indicating a bad invocation of the program."""
  pass


# General Helpers


Project = collections.namedtuple('Project', ['name', 'dir', 'remote'])


# pylint: disable=redefined-builtin
def _run_command(cmd, cwd=None, input=None,
                 redirect_stderr=False, combine_stdout_stderr=False):
  """Executes the passed in command and returns raw stdout output.

  Args:
    cmd: The command to run; should be a list of strings.
    cwd: The directory to switch to for running the command.
    input: The data to pipe into this command through stdin. If a file object
      or file descriptor, stdin will be connected directly to that.
    redirect_stderr: Redirect stderr away from console.
    combine_stdout_stderr: Combines stdout and stderr streams into stdout.

  Returns:
    The stdout from the process (discards stderr and returncode).
  """
  return cros_build_lib.RunCommand(cmd=cmd,
                                   cwd=cwd,
                                   print_cmd=False,
                                   input=input,
                                   stdout_to_pipe=True,
                                   redirect_stderr=redirect_stderr,
                                   combine_stdout_stderr=combine_stdout_stderr,
                                   error_code_ok=True).output
# pylint: enable=redefined-builtin


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


def _try_utf8_decode(data):
  """Attempts to decode a string as UTF-8.

  Returns:
    The decoded Unicode object, or the original string if parsing fails.
  """
  try:
    return unicode(data, 'utf-8', 'strict')
  except UnicodeDecodeError:
    return data


def _get_file_content(path, commit):
  """Returns the content of a file at a specific commit.

  We can't rely on the file as it exists in the filesystem as people might be
  uploading a series of changes which modifies the file multiple times.

  Note: The "content" of a symlink is just the target.  So if you're expecting
  a full file, you should check that first.  One way to detect is that the
  content will not have any newlines.
  """
  if commit == PRE_SUBMIT:
    return _run_command(['git', 'diff', 'HEAD', path])
  else:
    return _run_command(['git', 'show', '%s:%s' % (commit, path)])


def _get_file_diff(path, commit):
  """Returns a list of (linenum, lines) tuples that the commit touched."""
  if commit == PRE_SUBMIT:
    command = ['git', 'diff', '-p', '--pretty=format:', '--no-ext-diff', 'HEAD',
               path]
  else:
    command = ['git', 'show', '-p', '--pretty=format:', '--no-ext-diff', commit,
               path]
  output = _run_command(command)

  new_lines = []
  line_num = 0
  for line in output.splitlines():
    m = re.match(r'^@@ [0-9\,\+\-]+ \+([0-9]+)\,[0-9]+ @@', line)
    if m:
      line_num = int(m.groups(1)[0])
      continue
    if line.startswith('+') and not line.startswith('++'):
      new_lines.append((line_num, _try_utf8_decode(line[1:])))
    if not line.startswith('-'):
      line_num += 1
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
  files = git.RawDiff(path, '%s^!' % commit)

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
  cmd = ['git', 'log', '%s..' % _get_upstream_branch(), '--format=%H']
  return _run_command(cmd).split()


def _get_commit_desc(commit):
  """Returns the full commit message of a commit."""
  if commit == PRE_SUBMIT:
    return ''
  return _run_command(['git', 'log', '--format=%s%n%n%b', commit + '^!'])


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
      if check_callable(line):
        errors.append('%s, line %s' % (afile, line_num))
  if errors:
    return HookFailure(error_description, errors)


# Common Hooks


def _check_no_long_lines(_project, commit):
  """Checks there are no lines longer than MAX_LEN in any of the text files."""

  MAX_LEN = 80
  SKIP_REGEXP = re.compile('|'.join([
      r'https?://',
      r'^#\s*(define|include|import|pragma|if|endif)\b']))

  errors = []
  files = _filter_files(_get_affected_files(commit),
                        COMMON_INCLUDED_PATHS,
                        COMMON_EXCLUDED_PATHS)

  for afile in files:
    for line_num, line in _get_file_diff(afile, commit):
      # Allow certain lines to exceed the maxlen rule.
      if len(line) <= MAX_LEN or SKIP_REGEXP.search(line):
        continue

      errors.append('%s, line %s, %s chars' % (afile, line_num, len(line)))
      if len(errors) == 5:  # Just show the first 5 errors.
        break

  if errors:
    msg = 'Found lines longer than %s characters (first 5 shown):' % MAX_LEN
    return HookFailure(msg, errors)


def _check_no_stray_whitespace(_project, commit):
  """Checks that there is no stray whitespace at source lines end."""
  files = _filter_files(_get_affected_files(commit),
                        COMMON_INCLUDED_PATHS,
                        COMMON_EXCLUDED_PATHS)
  return _check_lines_in_diff(commit, files,
                              lambda line: line.rstrip() != line,
                              'Found line ending with white space in:')


def _check_no_tabs(_project, commit):
  """Checks there are no unexpanded tabs."""
  TAB_OK_PATHS = [
      r"/src/third_party/u-boot/",
      r".*\.ebuild$",
      r".*\.eclass$",
      r".*/[M|m]akefile$",
      r".*\.mk$"
  ]

  files = _filter_files(_get_affected_files(commit),
                        COMMON_INCLUDED_PATHS,
                        COMMON_EXCLUDED_PATHS + TAB_OK_PATHS)
  return _check_lines_in_diff(commit, files,
                              lambda line: '\t' in line,
                              'Found a tab character in:')


def _check_tabbed_indents(_project, commit):
  """Checks that indents use tabs only."""
  TABS_REQUIRED_PATHS = [
      r".*\.ebuild$",
      r".*\.eclass$",
  ]
  LEADING_SPACE_RE = re.compile('[\t]* ')

  files = _filter_files(_get_affected_files(commit),
                        TABS_REQUIRED_PATHS,
                        COMMON_EXCLUDED_PATHS)
  return _check_lines_in_diff(
      commit, files,
      lambda line: LEADING_SPACE_RE.match(line) is not None,
      'Found a space in indentation (must be all tabs):')


def _check_gofmt(_project, commit):
  """Checks that Go files are formatted with gofmt."""
  errors = []
  files = _filter_files(_get_affected_files(commit, relative=True),
                        [r'\.go$'])

  for gofile in files:
    contents = _get_file_content(gofile, commit)
    output = _run_command(cmd=['gofmt', '-l'], input=contents,
                          combine_stdout_stderr=True)
    if output:
      errors.append(gofile)
  if errors:
    return HookFailure('Files not formatted with gofmt:', errors)


def _check_change_has_test_field(_project, commit):
  """Check for a non-empty 'TEST=' field in the commit message."""
  TEST_RE = r'\nTEST=\S+'

  if not re.search(TEST_RE, _get_commit_desc(commit)):
    msg = 'Changelist description needs TEST field (after first line)'
    return HookFailure(msg)


def _check_change_has_valid_cq_depend(_project, commit):
  """Check for a correctly formatted CQ-DEPEND field in the commit message."""
  msg = 'Changelist has invalid CQ-DEPEND target.'
  example = 'Example: CQ-DEPEND=CL:1234, CL:2345'
  try:
    patch.GetPaladinDeps(_get_commit_desc(commit))
  except ValueError as ex:
    return HookFailure(msg, [example, str(ex)])


def _check_change_is_contribution(_project, commit):
  """Check that the change is a contribution."""
  NO_CONTRIB = 'not a contribution'
  if NO_CONTRIB in _get_commit_desc(commit).lower():
    msg = ('Changelist is not a contribution, this cannot be accepted.\n'
           'Please remove the "%s" text from the commit message.') % NO_CONTRIB
    return HookFailure(msg)


def _check_change_has_bug_field(project, commit):
  """Check for a correctly formatted 'BUG=' field in the commit message."""
  OLD_BUG_RE = r'\nBUG=.*chromium-os'
  if re.search(OLD_BUG_RE, _get_commit_desc(commit)):
    msg = ('The chromium-os bug tracker is now deprecated. Please use\n'
           'the chromium tracker in your BUG= line now.')
    return HookFailure(msg)

  # Android internal and external projects use "Bug: " to track bugs in
  # buganizer.
  BUG_COLON_REMOTES = (
      'aosp',
      'goog',
  )
  if project.remote in BUG_COLON_REMOTES:
    BUG_RE = r'\nBug: ?([Nn]one|\d+)'
    if not re.search(BUG_RE, _get_commit_desc(commit)):
      msg = ('Changelist description needs BUG field (after first line):\n'
             'Bug: 9999 (for buganizer)\n'
             'BUG=None')
      return HookFailure(msg)
  else:
    BUG_RE = r'\nBUG=([Nn]one|(chrome-os-partner|chromium|brillo|b):\d+)'
    if not re.search(BUG_RE, _get_commit_desc(commit)):
      msg = ('Changelist description needs BUG field (after first line):\n'
             'BUG=brillo:9999 (for Brillo tracker)\n'
             'BUG=chromium:9999 (for public tracker)\n'
             'BUG=chrome-os-partner:9999 (for partner tracker)\n'
             'BUG=b:9999 (for buganizer)\n'
             'BUG=None')
      return HookFailure(msg)


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
  whitelist = (
      'chromiumos/overlays/portage-stable',
  )
  if project.name in whitelist:
    return None

  def FinalName(obj):
    # If the file is being deleted, then the dst_file is not set.
    if obj.dst_file is None:
      return obj.src_file
    else:
      return obj.dst_file

  affected_path_objs = _get_affected_files(
      commit, include_deletes=True, include_symlinks=True, relative=True,
      full_details=True)

  # Don't yell about changes to whitelisted files...
  whitelist = ('ChangeLog', 'Manifest', 'metadata.xml', 'COMMIT-QUEUE.ini')
  affected_path_objs = [x for x in affected_path_objs
                        if os.path.basename(FinalName(x)) not in whitelist]
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
    return

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
  """Make sure we have people use EAPI=4 or newer with custom ebuilds.

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
  whitelist = (
      'chromiumos/overlays/portage-stable',
  )
  if project.name in whitelist:
    return None

  BAD_EAPIS = ('0', '1', '2', '3')

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
    url = 'http://dev.chromium.org/chromium-os/how-tos-and-troubleshooting/upgrade-ebuild-eapis'
    # pylint: enable=C0301
    return HookFailure(
        'These ebuilds are using old EAPIs.  If these are imported from\n'
        'Gentoo, then you may ignore and upload once with the --no-verify\n'
        'flag.  Otherwise, please update to 4 or newer.\n'
        '\t%s\n'
        'See this guide for more details:\n%s\n' %
        ('\n\t'.join(['%s: EAPI=%s' % x for x in bad_ebuilds]), url))


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
  WHITELIST = set(('*', '-*', '~*'))

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
        if not keywords or WHITELIST - keywords != WHITELIST:
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


def _check_ebuild_licenses(_project, commit):
  """Check if the LICENSE field in the ebuild is correct."""
  affected_paths = _get_affected_files(commit, relative=True)
  touched_ebuilds = [x for x in affected_paths if x.endswith('.ebuild')]

  # A list of licenses to ignore for now.
  LICENSES_IGNORE = ['||', '(', ')']

  for ebuild in touched_ebuilds:
    # Skip virutal packages.
    if ebuild.split('/')[-3] == 'virtual':
      continue

    try:
      ebuild_content = _get_file_content(ebuild, commit)
      license_types = licenses_lib.GetLicenseTypesFromEbuild(ebuild_content)
    except ValueError as e:
      return HookFailure(e.message, [ebuild])

    # Also ignore licenses ending with '?'
    for license_type in [x for x in license_types
                         if x not in LICENSES_IGNORE and not x.endswith('?')]:
      try:
        licenses_lib.Licensing.FindLicenseType(license_type)
      except AssertionError as e:
        return HookFailure(e.message, [ebuild])


def _check_ebuild_virtual_pv(project, commit):
  """Enforce the virtual PV policies."""
  # If this is the portage-stable overlay, then ignore the check.
  # We want to import virtuals as-is from upstream Gentoo.
  whitelist = (
      'chromiumos/overlays/portage-stable',
  )
  if project.name in whitelist:
    return None

  # We assume the repo name is the same as the dir name on disk.
  # It would be dumb to not have them match though.
  project_base = os.path.basename(project.name)

  is_variant = lambda x: x.startswith('overlay-variant-')
  is_board = lambda x: x.startswith('overlay-')
  is_private = lambda x: x.endswith('-private')

  get_pv = re.compile(r'(.*?)virtual/([^/]+)/\2-([^/]*)\.ebuild$')

  ebuilds_re = [r'\.ebuild$']
  ebuilds = _filter_files(_get_affected_files(commit, relative=True),
                          ebuilds_re)
  bad_ebuilds = []

  for ebuild in ebuilds:
    m = get_pv.match(ebuild)
    if m:
      overlay = m.group(1)
      if not overlay or not is_board(overlay):
        overlay = project_base

      pv = m.group(3).split('-', 1)[0]

      # Virtual versions >= 4 are special cases used above the standard
      # versioning structure, e.g. if one has a board inheriting a board.
      if float(pv) >= 4:
        want_pv = pv
      elif is_private(overlay):
        want_pv = '3.5' if is_variant(overlay) else '3'
      elif is_board(overlay):
        want_pv = '2.5' if is_variant(overlay) else '2'
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


def _check_change_has_proper_changeid(_project, commit):
  """Verify that Change-ID is present in last paragraph of commit message."""
  CHANGE_ID_RE = r'\nChange-Id: I[a-f0-9]+\n'
  desc = _get_commit_desc(commit)
  m = re.search(CHANGE_ID_RE, desc)
  if not m:
    return HookFailure('Last paragraph of description must include Change-Id.')

  # S-o-b tags always allowed to follow Change-ID.
  allowed_tags = ['Signed-off-by']

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


def _check_commit_message_style(_project, commit):
  """Verify that the commit message matches our style.

  We do not check for BUG=/TEST=/etc... lines here as that is handled by other
  commit hooks.
  """
  desc = _get_commit_desc(commit)

  # The first line should be by itself.
  lines = desc.splitlines()
  if len(lines) > 1 and lines[1]:
    return HookFailure('The second line of the commit message must be blank.')

  # The first line should be one sentence.
  if '. ' in lines[0]:
    return HookFailure('The first line cannot be more than one sentence.')

  # The first line cannot be too long.
  MAX_FIRST_LINE_LEN = 100
  if len(lines[0]) > MAX_FIRST_LINE_LEN:
    return HookFailure('The first line must be less than %i chars.' %
                       MAX_FIRST_LINE_LEN)


def _check_cros_license(_project, commit, options=()):
  """Verifies the Chromium OS license/copyright header.

  Should be following the spec:
  http://dev.chromium.org/developers/coding-style#TOC-File-headers
  """
  # For older years, be a bit more flexible as our policy says leave them be.
  LICENSE_HEADER = (
      r'.* Copyright( \(c\))? 20[-0-9]{2,7} The Chromium OS Authors\. '
      r'All rights reserved\.' r'\n'
      r'.* Use of this source code is governed by a BSD-style license that can '
      r'be\n'
      r'.* found in the LICENSE file\.'
      r'\n'
  )
  license_re = re.compile(LICENSE_HEADER, re.MULTILINE)

  # For newer years, be stricter.
  COPYRIGHT_LINE = (
      r'.* Copyright \(c\) 20(1[5-9]|[2-9][0-9]) The Chromium OS Authors\. '
      r'All rights reserved\.' r'\n'
  )
  copyright_re = re.compile(COPYRIGHT_LINE)

  parser = argparse.ArgumentParser()
  parser.add_argument('--exclude_regex', action='append')
  parser.add_argument('--include_regex', action='append')
  opts = parser.parse_args(options)
  included = opts.include_regex or []
  excluded = opts.exclude_regex or []

  bad_files = []
  bad_copyright_files = []
  files = _filter_files(_get_affected_files(commit, relative=True),
                        included + COMMON_INCLUDED_PATHS,
                        excluded + COMMON_EXCLUDED_PATHS)

  for f in files:
    contents = _get_file_content(f, commit)
    if not contents:
      # Ignore empty files.
      continue

    if not license_re.search(contents):
      bad_files.append(f)
    elif copyright_re.search(contents):
      bad_copyright_files.append(f)

  if bad_files:
    msg = '%s:\n%s\n%s' % (
        'License must match', license_re.pattern,
        'Found a bad header in these files:')
    return HookFailure(msg, bad_files)

  if bad_copyright_files:
    msg = 'Do not use (c) in copyright headers in new files:'
    return HookFailure(msg, bad_copyright_files)


def _check_aosp_license(_project, commit):
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

  files = _filter_files(_get_affected_files(commit, relative=True),
                        COMMON_INCLUDED_PATHS,
                        COMMON_EXCLUDED_PATHS)

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
        #('fast caching', 'cache-format = md5-dict'),
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


# Project-specific hooks


def _run_checkpatch(_project, commit, options=()):
  """Runs checkpatch.pl on the given project"""
  hooks_dir = _get_hooks_dir()
  options = list(options)
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
  cmd = ['%s/checkpatch.pl' % hooks_dir] + options + ['-']
  cmd_result = cros_build_lib.RunCommand(cmd=cmd,
                                         print_cmd=False,
                                         input=_get_patch(commit),
                                         stdout_to_pipe=True,
                                         combine_stdout_stderr=True,
                                         error_code_ok=True)
  if cmd_result.returncode:
    return HookFailure('checkpatch.pl errors/warnings\n\n' + cmd_result.output)


def _kernel_configcheck(_project, commit):
  """Makes sure kernel config changes are not mixed with code changes"""
  files = _get_affected_files(commit)
  if not len(_filter_files(files, [r'chromeos/config'])) in [0, len(files)]:
    return HookFailure('Changes to chromeos/config/ and regular files must '
                       'be in separate commits:\n%s' % '\n'.join(files))


def _run_json_check(_project, commit):
  """Checks that all JSON files are syntactically valid."""
  for f in _filter_files(_get_affected_files(commit), [r'.*\.json']):
    try:
      json.load(open(f))
    except Exception, e:
      return HookFailure('Invalid JSON in %s: %s' % (f, e))


def _check_manifests(_project, commit):
  """Make sure Manifest files only have DIST lines"""
  paths = []

  for path in _get_affected_files(commit):
    if os.path.basename(path) != 'Manifest':
      continue
    if not os.path.exists(path):
      continue

    with open(path, 'r') as f:
      for line in f.readlines():
        if not line.startswith('DIST '):
          paths.append(path)
          break

  if paths:
    return HookFailure('Please remove lines that do not start with DIST:\n%s' %
                       ('\n'.join(paths),))


def _check_change_has_branch_field(_project, commit):
  """Check for a non-empty 'BRANCH=' field in the commit message."""
  if commit == PRE_SUBMIT:
    return
  BRANCH_RE = r'\nBRANCH=\S+'

  if not re.search(BRANCH_RE, _get_commit_desc(commit)):
    msg = ('Changelist description needs BRANCH field (after first line)\n'
           'E.g. BRANCH=none or BRANCH=link,snow')
    return HookFailure(msg)


def _check_change_has_signoff_field(_project, commit):
  """Check for a non-empty 'Signed-off-by:' field in the commit message."""
  if commit == PRE_SUBMIT:
    return
  SIGNOFF_RE = r'\nSigned-off-by: \S+'

  if not re.search(SIGNOFF_RE, _get_commit_desc(commit)):
    msg = ('Changelist description needs Signed-off-by: field\n'
           'E.g. Signed-off-by: My Name <me@chromium.org>')
    return HookFailure(msg)


def _check_cq_ini_well_formed(_project, commit):
  """Check that any modified COMMIT-QUEUE.ini files are well formed."""
  pattern = '.*' + constants.CQ_CONFIG_FILENAME
  files = _filter_files(_get_affected_files(commit), (pattern,))

  # TODO(akeshet): Check not only that the file is parseable, but that all the
  # pre-cq configs it requests are existing ones.
  for f in files:
    try:
      parser = ConfigParser.SafeConfigParser()
      # Prior to python3, ConfigParser has no read_string method, so we must
      # pass it either a file path or file like object. And we must use
      # _get_file_content to fetch file contents to ensure we are examining the
      # commit diff, rather than whatever's on disk.
      contents = _get_file_content(f, commit)
      parser.readfp(StringIO.StringIO(contents))
    except ConfigParser.Error as e:
      msg = ('Unable to parse COMMIT-QUEUE.ini file at %s due to %s.' %
             (f, e))
      return HookFailure(msg)


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

  cmd_result = cros_build_lib.RunCommand(cmd=script,
                                         env=env,
                                         shell=True,
                                         print_cmd=False,
                                         input=os.devnull,
                                         stdout_to_pipe=True,
                                         combine_stdout_stderr=True,
                                         error_code_ok=True)
  if cmd_result.returncode:
    stdout = cmd_result.output
    if stdout:
      stdout = re.sub('(?m)^', '  ', stdout)
    return HookFailure('Hook script "%s" failed with code %d%s' %
                       (script, cmd_result.returncode,
                        ':\n' + stdout if stdout else ''))


def _check_project_prefix(_project, commit):
  """Require the commit message have a project specific prefix as needed."""

  files = _get_affected_files(commit, relative=True)
  prefix = os.path.commonprefix(files)
  prefix = os.path.dirname(prefix)

  # If there is no common prefix, the CL span multiple projects.
  if not prefix:
    return

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
]


# A list of hooks that are not project-specific
_COMMON_HOOKS = [
    _check_cq_ini_well_formed,
    _check_cros_license,
    _check_ebuild_eapi,
    _check_ebuild_keywords,
    _check_ebuild_licenses,
    _check_ebuild_virtual_pv,
    _check_for_uprev,
    _check_gofmt,
    _check_layout_conf,
    _check_no_long_lines,
    _check_no_stray_whitespace,
    _check_no_tabs,
    _check_portage_make_use_var,
    _check_tabbed_indents,
]


# A dictionary of project-specific hooks(callbacks), indexed by project name.
# dict[project] = [callback1, callback2]
_PROJECT_SPECIFIC_HOOKS = {
    "chromiumos/platform2": [_check_project_prefix],
    "chromiumos/third_party/kernel": [_kernel_configcheck],
    "chromiumos/third_party/kernel-next": [_kernel_configcheck],
}


# A dictionary of flags (keys) that can appear in the config file, and the hook
# that the flag controls (value).
_HOOK_FLAGS = {
    'checkpatch_check': _run_checkpatch,
    'stray_whitespace_check': _check_no_stray_whitespace,
    'json_check': _run_json_check,
    'long_line_check': _check_no_long_lines,
    'cros_license_check': _check_cros_license,
    'aosp_license_check': _check_aosp_license,
    'tab_check': _check_no_tabs,
    'tabbed_indent_required_check': _check_tabbed_indents,
    'branch_check': _check_change_has_branch_field,
    'signoff_check': _check_change_has_signoff_field,
    'bug_field_check': _check_change_has_bug_field,
    'test_field_check': _check_change_has_test_field,
    'manifest_check': _check_manifests,
    'contribution_check': _check_change_is_contribution,
}


def _get_override_hooks(config):
  """Returns a set of hooks controlled by the current project's config file.

  Expects to be called within the project root.

  Args:
    config: A ConfigParser for the project's config file.
  """
  SECTION = 'Hook Overrides'
  SECTION_OPTIONS = 'Hook Overrides Options'
  if not config.has_section(SECTION):
    return set(), set()

  valid_keys = set(_HOOK_FLAGS.iterkeys())
  hooks = _HOOK_FLAGS.copy()

  enable_flags = []
  disable_flags = []
  for flag in config.options(SECTION):
    if flag not in valid_keys:
      raise ValueError('Error: unknown key "%s" in hook section of "%s"' %
                       (flag, _CONFIG_FILE))

    try:
      enabled = config.getboolean(SECTION, flag)
    except ValueError as e:
      raise ValueError('Error: parsing flag "%s" in "%s" failed: %s' %
                       (flag, _CONFIG_FILE, e))
    if enabled:
      enable_flags.append(flag)
    else:
      disable_flags.append(flag)

    # See if this hook has custom options.
    if enabled:
      try:
        options = config.get(SECTION_OPTIONS, flag)
        hooks[flag] = functools.partial(hooks[flag], options=options.split())
      except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
        pass

  enabled_hooks = set(hooks[x] for x in enable_flags)
  disabled_hooks = set(hooks[x] for x in disable_flags)
  return enabled_hooks, disabled_hooks


def _get_project_hook_scripts(config):
  """Returns a list of project-specific hook scripts.

  Args:
    config: A ConfigParser for the project's config file.
  """
  SECTION = 'Hook Scripts'
  if not config.has_section(SECTION):
    return []

  hook_names_values = config.items(SECTION)
  hook_names_values.sort(key=lambda x: x[0])
  return [x[1] for x in hook_names_values]


def _get_project_hooks(project, presubmit):
  """Returns a list of hooks that need to be run for a project.

  Expects to be called from within the project root.

  Args:
    project: A string, name of the project.
    presubmit: A Boolean, True if the check is run as a git pre-submit script.
  """
  config = ConfigParser.RawConfigParser()
  try:
    config.read(_CONFIG_FILE)
  except ConfigParser.Error:
    # Just use an empty config file
    config = ConfigParser.RawConfigParser()

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

  if project in _PROJECT_SPECIFIC_HOOKS:
    hooks.extend(hook for hook in _PROJECT_SPECIFIC_HOOKS[project]
                 if hook not in disabled_hooks)

  for script in _get_project_hook_scripts(config):
    hooks.append(functools.partial(_run_project_hook_script, script))

  return hooks


def _run_project_hooks(project_name, proj_dir=None,
                       commit_list=None, presubmit=False):
  """For each project run its project specific hook from the hooks dictionary.

  Args:
    project_name: The name of project to run hooks for.
    proj_dir: If non-None, this is the directory the project is in.  If None,
        we'll ask repo.
    commit_list: A list of commits to run hooks against.  If None or empty list
        then we'll automatically get the list of commits that would be uploaded.
    presubmit: A Boolean, True if the check is run as a git pre-submit script.

  Returns:
    Boolean value of whether any errors were ecountered while running the hooks.
  """
  if proj_dir is None:
    proj_dirs = _run_command(
        ['repo', 'forall', project_name, '-c', 'pwd']).split()
    if len(proj_dirs) == 0:
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

  remote_branch = _run_command(['git', 'rev-parse', '--abbrev-ref',
                                '--symbolic-full-name', '@{u}']).strip()
  if not remote_branch:
    print('Your project %s doesn\'t track any remote repo.' % project_name,
          file=sys.stderr)
    remote = None
  else:
    remote, _branch = remote_branch.split('/', 1)

  project = Project(name=project_name, dir=proj_dir, remote=remote)

  if not commit_list:
    try:
      commit_list = _get_commits()
    except VerifyException as e:
      PrintErrorForProject(project.name, HookFailure(str(e)))
      os.chdir(pwd)
      return True

  hooks = _get_project_hooks(project.name, presubmit)
  error_found = False
  for commit in commit_list:
    error_list = []
    for hook in hooks:
      hook_error = hook(project, commit)
      if hook_error:
        error_list.append(hook_error)
        error_found = True
    if error_list:
      PrintErrorsForCommit(project.name, commit, _get_commit_desc(commit),
                           error_list)

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
  found_error = False
  if not worktree_list:
    worktree_list = [None] * len(project_list)
  for project, worktree in zip(project_list, worktree_list):
    if _run_project_hooks(project, proj_dir=worktree):
      found_error = True

  if found_error:
    msg = ('Preupload failed due to errors in project(s). HINTS:\n'
           '- To disable some source style checks, and for other hints, see '
           '<checkout_dir>/src/repohooks/README\n'
           '- To upload only current project, run \'repo upload .\'')
    print(msg, file=sys.stderr)
    sys.exit(1)


def _identify_project(path):
  """Identify the repo project associated with the given path.

  Returns:
    A string indicating what project is associated with the path passed in or
    a blank string upon failure.
  """
  return _run_command(['repo', 'forall', '.', '-c', 'echo ${REPO_PROJECT}'],
                      redirect_stderr=True, cwd=path).strip()


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
                      help='Rerun hooks on old commits since the given date.  '
                      'The date should match git log\'s concept of a date.  '
                      'e.g. 2012-06-20. This option is mutually exclusive '
                      'with --pre-submit.')
  parser.add_argument('--pre-submit', action="store_true",
                      help='Run the check against the pending commit.  '
                      'This option should be used at the \'git commit\' '
                      'phase as opposed to \'repo upload\'. This option '
                      'is mutually exclusive with --rerun-since.')
  parser.add_argument('commits', nargs='*',
                      help='Check specific commits')
  opts = parser.parse_args(argv)

  if opts.rerun_since:
    if opts.commits:
      raise BadInvocation('Can\'t pass commits and use rerun-since: %s' %
                          ' '.join(opts.commits))

    cmd = ['git', 'log', '--since="%s"' % opts.rerun_since, '--pretty=%H']
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
      raise BadInvocation('Can\'t pass commits and use pre-submit: %s' %
                          ' '.join(opts.commits))
    opts.commits = [PRE_SUBMIT,]

  # Check/normlaize git dir; if unspecified, we'll use the root of the git
  # project from CWD
  if opts.dir is None:
    git_dir = _run_command(['git', 'rev-parse', '--git-dir'],
                           redirect_stderr=True).strip()
    if not git_dir:
      raise BadInvocation('The current directory is not part of a git project.')
    opts.dir = os.path.dirname(os.path.abspath(git_dir))
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
                                   presubmit=opts.pre_submit)
  if found_error:
    return 1
  return 0


if __name__ == '__main__':
  sys.exit(direct_main(sys.argv[1:]))
