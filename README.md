# ChromiumOS Preupload Hooks

[TOC]

This repo holds hooks that get run by repo during the upload phase.  They
perform various checks automatically such as running linters on your code.

Note: Currently all hooks are enabled by default.  Each repo must explicitly
turn off any hook it wishes to disable.

Note: While we still use "presubmit" in many places (including config files),
these checks only run at preupload time.

## Usage

Normally these execute automatically when you run `repo upload`.  If you want to
run them by hand, you can execute `pre-upload.py` directly.  By default, that
will scan the active repo and process all commits that haven't yet been merged.
See its help for more info.

### Bypassing

Sometimes you might want to bypass the upload checks.  While this is **strongly
discouraged** (often failures you add will affect others and block them too),
sometimes there are valid reasons for this.  You can simply use the option
`--no-verify` when running `repo upload` to skip all upload checks.  This will
skip **all** checks and not just specific ones.  It should be used only after
having run & evaluated the upload output previously.

# Config Files

## PRESUBMIT.cfg

This file is checked in the top of a specific git repository.  Stacking them
in subdirectories (to try and override parent settings) is not supported.

## Example

```
# Per-project `repo upload` hook settings.
# https://chromium.googlesource.com/chromiumos/repohooks/

[Hook Scripts]
cros format = cros format --check --commit ${PRESUBMIT_COMMIT} ${PRESUBMIT_FILES}
cros lint = cros lint --commit ${PRESUBMIT_COMMIT} ${PRESUBMIT_FILES}

[Hook Overrides]
cros_license_check: true
long_line_check: true
tab_check: true
stray_whitespace_check: true

[Hook Overrides Options]
cros_license_check: --exclude_regex=\b(checkpatch\.pl|kernel-doc)$
```

## Environment

Hooks are executed in the top directory of the git repository.  All paths should
generally be relative to that point.

A few environment variables are set so scripts don't need to discover things.

*   `PRESUBMIT_PROJECT`: The project name being changed by the commit
    (e.g. 'chromiumos/platform/firmware').
*   `PRESUBMIT_COMMIT`: The full commit hash of your change.
*   `PRESUBMIT_FILES`: A list of files affected by your commit delimited by
    newlines (e.g. 'README.md\nsub/dir/foo.py').

## Placeholders

A few keywords are recognized to pass down settings.  These are **not**
environment variables, but are expanded inline.  Files with whitespace and
such will be expanded correctly via argument positions, so do not try to
force your own quote handling.

* `${PRESUBMIT_PROJECT}`: List of files to operate on.
* `${PRESUBMIT_FILES}`: A list of files to operate on.
* `${PREUPLOAD_COMMIT}`: Commit hash.

## [Hook Scripts]

This section allows for completely arbitrary hooks to run on a per-repo basis.

The key can be any name (as long as the syntax is valid), as can the program
that is executed. The key is used as the name of the hook for reporting purposes,
so it should be at least somewhat descriptive.

Whitespace in the key name is OK!

The keys must be unique as duplicates will silently clobber earlier values.

You do not need to send stderr to stdout.  The tooling will take care of
merging them together for you automatically.

The command respects shell-like syntax, but is not actually shell code.  If you
want to run arbitrary shell scripts, you should create a dedicated shell script
and then execute that.  This allows for linting of the script.

```
[Hook Scripts]
my first hook = program --gogog ${PRESUBMIT_FILES}
another hook = funtimes --i-need "some space" ${PRESUBMIT_FILES}
some fish = linter --ate-a-cat ${PRESUBMIT_FILES}
some cat = formatter --cat-commit ${PRESUBMIT_COMMIT}
```

## [Hook Overrides]

This section allows for turning off common/builtin hooks.

Note: Not all hooks that we run may be disabled.  We only document the ones that
may be controlled by the config file here, but we run many more checks.

*   `aosp_license_check`: Require source files have an Android (Apache) license.
*   `blank_line_check`: Check for trailing blank lines.
*   `branch_check`: Require all commit messages have a `BRANCH=` line.
*   `bug_field_check`: Require all commit messages have a `BUG=` line.
*   `cargo_clippy_check`: Run Rust code through `cargo clippy`.
*   `check_change_no_include_board_phase`: Reject commit messages that refer to
    hardware board development phases (e.g. PVT).
*   `check_commit_message_style`: Check commit message style (blank second line,
    subject line under 100 chars, single sentence, no `fixup!` or `squash!`).
*   `checkpatch_check`: Run commits through Linux's `checkpatch.pl` tool.
*   `contribution_check`: Check source files for invalid "not a contribution".
*   `cros_license_check`: Require source files have a Chromium (BSD) license.
*   `cros_workon_revbump_eclass_check`: Checks packages inheriting the updated
    eclass also get revbumped.
*   `egis_license_check`: Require source files have a Egis Copyright license.
*   `exec_files_check`: Check common file types do not have +x permission bits.
*   `filepath_chartype_check`: Check source files for FilePath::CharType use.
*   `git_cl_presubmit`: Run git-cl PRESUBMIT.py logic if available.
*   `handle_eintr_close_check`: Check C++ code does not use unsafe
    `HANDLE_EINTR(close(...))` idioms.
*   `kernel_splitconfig_check`: Require CrOS kernel config changes are separate
    commits from kernel code changes.
*   `kerneldoc_check`: Run commits through Linux's `kernel-doc` tool.
*   `keyword_check`: Check text files & commit messages for
    [blocked terms](#blocked-terms).
*   `long_line_check`: Do not allow lines longer than 80 cols.
*   `manifest_check`: Check all ebuild `Manifest` files.
*   `project_prefix_check`: Require all commit message have a subdir prefix.
*   `signoff_check`: Require all commit messages have a `Signed-off-by` tag.
*   `stray_whitespace_check`: Check source files for stray whitespace.
*   `tab_check`: Do not allow tabs for indentation in source files.
*   `tabbed_indent_required_check`: Require tabs for indentation.
*   `test_field_check`: Require all commit messages have a `TEST=` line.

## [Hook Overrides Options]

Some hooks accept custom options.  The key name matches the Hook Overrides
name above, so see that list for more details.

```
[Hook Overrides Options]
cros_license_check: --exclude_regex=\b(checkpatch\.pl|kernel-doc)$
```

## Blocked and Unblocked Word List {#blocked-terms}

`blocked_terms.txt` contains a default list of words which are blocked by
keyword_check. `unblocked_terms.txt` is a default list of words which are
unblocked.

Repohook references the global `unblocked_terms.txt` only if a copy doesn't
exist in a project. Thus, you can copy `unblocked_terms.txt` to your project
and remove the words which are already cleared locally. That'll allow your
project to manage keyword-blocking/unblocking individually. Each project can
have multiple `unblocked_terms.txt`. The parent directories of each file being
changed are searched. The one nearest to the file has a higher priority.

1.  Copy `unblocked_terms.txt` to the project's root directory.
2.  List which words are already cleared (or not present):
    ```$ ~/chromiumos/src/platform/dev/contrib/search_blocked_words.sh```
3.  Remove the cleared words from the project's `unblocked_terms.txt`
4.  Test and submit CL.

Later you can do:

1.  Pick a word from the project's `unblocked_terms.txt`.
2.  Fix all occurrences of the word.
3.  Verify the clearance:
    ```$ ~/chromiumos/src/platform/dev/contrib/search_blocked_words.sh```
4.  Remove the word from the project's `unblocked_terms.txt`.
5.  Test and submit CL.

Additionally for terms that cannot be removed, adding a comment with nocheck at
the end of the line with the term(s) will skip the keyword_check on that line.

```
Example line skipping blocked term check. # nocheck
This line is still checked.
This line is also checked as nocheck is not on the end of the line.
```

# Third Party code

We have many third party repos where you probably want to disable CrOS checks.
You'll need to disable each one in your project's PRESUBMIT.cfg file.
See the reference above for which checks you probably want to disable.

# Guidelines for Hooks and Hook Scripts

A good hook or hook script should execute quickly and reliably, and rarely give
false-positive warnings.  Here are some things to keep in mind while you're
writing your hooks:

1.  **Don't assume you're running inside of the SDK.**  Hook scripts are
    executed outside the SDK.  Additionally, we do not require that users have
    an SDK just to upload code, so don't try to enter the SDK either, as it may
    not even exist.
2.  **Don't take too long to execute.**  Hooks that regularly take too long are
    generally skipped by users.  As a general guideline, all hooks together
    shouldn't take more than a few seconds per commit.
3.  **Don't run unit tests.**  Unit tests generally take too long to run as a
    pre-upload hook, and chances are you're going to need the SDK to do this
    anyway (so that's a double no!).
4.  **Don't modify files.**  Users expect you'll leave their Git repository
    clean.
5.  **Don't assume host tools beyond what's specified in the [developer
    guide].**  If you need additional tools to run your checks, consider using
    [cipd] or [vpython] to distribute your dependencies.
6.  **Don't prompt for user input.**  Your hooks shouldn't ask the users any
    questions, as your hooks may be run as a part of an automated script where
    user input is not expected.
7.  **Don't assume that the current `HEAD` is the commit you are to analyze.**
    You should be using `${PRESUBMIT_COMMIT}` and interfacing via the `git`
    command to get the file contents.
8.  **Don't assume you're analyzing a commit.** `${PRESUBMIT_COMMIT}` may be set
    to the special string `pre-submit` indicating you should be analyzing
    uncommitted changes.  Handle this case.
9.  **Don't report issues with files the user didn't touch.**  Use
    `${PRESUBMIT_FILES}` to determine which files you are to check.

# Reporting issues

You can file bugs
[here](https://issuetracker.google.com/issues/new?component=1037860).

If you want to ask questions, use our normal
[development groups](https://chromium.googlesource.com/chromiumos/docs/+/HEAD/contact.md).

[cipd]: https://chromium.googlesource.com/infra/luci/luci-go/+/HEAD/cipd/README.md
[developer guide]: https://chromium.googlesource.com/chromiumos/docs/+/HEAD/developer_guide.md
[vpython]: https://chromium.googlesource.com/infra/infra/+/HEAD/doc/users/vpython.md
