#!/usr/bin/env python3
# Copyright 2017 The ChromiumOS Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Wrapper to run git-clang-format and parse its output."""

import hashlib
import io
import os
import sys


_path = os.path.realpath(__file__ + "/../../..")
if sys.path[0] != _path:
    sys.path.insert(0, _path)
del _path

# The sys.path monkey patching confuses the linter.
# pylint: disable=wrong-import-position
from chromite.lib import commandline
from chromite.lib import constants
from chromite.lib import cros_build_lib


assert sys.version_info >= (3, 6), "This module requires Python 3.6+"


# Since we're asking git-clang-format to print a diff, all modified filenames
# that have formatting errors are printed with this prefix.
DIFF_MARKER_PREFIX = "+++ b/"

BUILDTOOLS_PATH = os.path.join(
    constants.SOURCE_ROOT, "src", "chromium", "src", "buildtools"
)


def _GetSha1Hash(path):
    """Gets the SHA-1 hash of |path|, or None if the file does not exist."""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        m = hashlib.sha1()
        while True:
            buf = f.read(io.DEFAULT_BUFFER_SIZE)
            if not buf:
                break
            m.update(buf)
        return m.hexdigest()


def _GetDefaultClangFormatPath():
    """Gets the default clang-format binary path.

    This also ensures that the binary itself is up-to-date.
    """

    clang_format_path = os.path.join(BUILDTOOLS_PATH, "linux64/clang-format")
    hash_file_path = os.path.join(BUILDTOOLS_PATH, "linux64/clang-format.sha1")
    with open(hash_file_path, "r") as f:
        expected_hash = f.read().strip()
    if expected_hash != _GetSha1Hash(clang_format_path):
        # See chromium/src/buildtools/clang_format/README.txt for more details.
        cmd = [
            os.path.join(
                constants.DEPOT_TOOLS_DIR, "download_from_google_storage.py"
            ),
            "-b",
            "chromium-clang-format",
            "-s",
            hash_file_path,
        ]
        cros_build_lib.dbg_run(cmd)
    return clang_format_path


def main(argv):
    """Checks if a project is correctly formatted with clang-format.

    Returns 1 if there are any clang-format-worthy changes in the project (or
    on a provided list of files/directories in the project), 0 otherwise.
    """

    parser = commandline.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clang-format",
        default=_GetDefaultClangFormatPath(),
        help="The path of the clang-format executable.",
    )
    parser.add_argument(
        "--git-clang-format",
        default=os.path.join(
            BUILDTOOLS_PATH, "clang_format", "script", "git-clang-format"
        ),
        help="The path of the git-clang-format executable.",
    )
    parser.add_argument(
        "--style",
        metavar="STYLE",
        type=str,
        default="file",
        help="The style that clang-format will use.",
    )
    parser.add_argument(
        "--extensions",
        metavar="EXTENSIONS",
        type=str,
        help="Comma-separated list of file extensions to " "format.",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Fix any formatting errors automatically.",
    )

    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--commit",
        type=str,
        default="HEAD",
        help="Specify the commit to validate.",
    )
    scope.add_argument(
        "--working-tree",
        action="store_true",
        help="Validates the files that have changed from "
        "HEAD in the working directory.",
    )

    parser.add_argument(
        "files",
        type=str,
        nargs="*",
        help="If specified, only consider differences in "
        "these files/directories.",
    )

    opts = parser.parse_args(argv)

    # Upstream uses `python` in its shebang.  This doesn't work on newer Debian
    # systems which dropped /usr/bin/python.  Run through vpython manually to
    # match what depot_tools does.  b/220165980
    cmd = [
        "vpython",
        opts.git_clang_format,
        "--binary",
        opts.clang_format,
        "--diff",
    ]
    if opts.style:
        cmd.extend(["--style", opts.style])
    if opts.extensions:
        cmd.extend(["--extensions", opts.extensions])
    if not opts.working_tree:
        cmd.extend(["%s^" % opts.commit, opts.commit])
    cmd.extend(["--"] + opts.files)

    # Fail gracefully if clang-format itself aborts/fails.
    try:
        result = cros_build_lib.dbg_run(
            cmd, stdout=True, encoding="utf-8", errors="replace"
        )
    except cros_build_lib.RunCommandError as e:
        print("clang-format failed:\n" + str(e), file=sys.stderr)
        print("\nPlease report this to the clang team.", file=sys.stderr)
        return 1

    stdout = result.stdout
    if stdout.rstrip("\n") == "no modified files to format":
        # This is always printed when only files that clang-format does not
        # understand were modified.
        return 0

    diff_filenames = []
    for line in stdout.splitlines():
        if line.startswith(DIFF_MARKER_PREFIX):
            diff_filenames.append(line[len(DIFF_MARKER_PREFIX) :].rstrip())

    if diff_filenames:
        if opts.fix:
            cros_build_lib.dbg_run(["git", "apply"], input=stdout)
        else:
            print("The following files have formatting errors:")
            for filename in diff_filenames:
                print("\t%s" % filename)
            print(
                "You can run `%s --fix %s` to fix this"
                % (
                    sys.argv[0],
                    " ".join(cros_build_lib.ShellQuote(arg) for arg in argv),
                )
            )
            return 1

    return 0


if __name__ == "__main__":
    commandline.ScriptWrapperMain(lambda _: main)
