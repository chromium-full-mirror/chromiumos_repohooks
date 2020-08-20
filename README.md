# Chromium OS Preupload Hooks

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
cros lint = cros lint ${PRESUBMIT_FILES}
pre-upload_unittest = ./pre-upload_unittest.py

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
    newlines.

## [Hook Scripts]

This section allows for completely arbitrary hooks to run on a per-repo basis.

The key can be any name (as long as the syntax is valid), as can the program
that is executed. The key is used as the name of the hook for reporting purposes,
so it should be at least somewhat descriptive.

Whitespace in the key name is OK!

The keys must be unique as duplicates will silently clobber earlier values.

You do not need to send stderr to stdout.  The tooling will take care of
merging them together for you automatically.

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
*   `branch_check`: Require all commit messages have a `BRANCH=` line.
*   `bug_field_check`: Require all commit messages have a `BUG=` line.
*   `checkpatch_check`: Run commits through Linux's `checkpatch.pl` tool.
*   `clang_format_check`: Run source code through `clang-format`.
*   `contribution_check`: Check source files for invalid "not a contribution".
*   `cros_license_check`: Require source files have a Chromium (BSD) license.
*   `filepath_chartype_check`: Check source files for FilePath::CharType use.
*   `json_check`: Check all `.json` files are valid JSON.
*   `kerneldoc_check`: Run commits through Linux's `kernel-doc` tool.
*   `long_line_check`: Do not allow lines longer than 80 cols.
*   `manifest_check`: Check all ebuild `Manifest` files.
*   `project_prefix_check`: Require all commit message have a subdir prefix.
*   `signoff_check`: Require all commit messages have a `Signed-off-by` tag.
*   `stray_whitespace_check`: Check source files for stray whitespace.
*   `tabbed_indent_required_check`: Require tabs for indentation.
*   `tab_check`: Do not allow tabs for indentation in source files.
*   `test_field_check`: Require all commit messages have a `TEST=` line.

## [Hook Overrides Options]

Some hooks accept custom options.  The key name matches the Hook Overrides
name above, so see that list for more details.

```
[Hook Overrides Options]
cros_license_check: --exclude_regex=\b(checkpatch\.pl|kernel-doc)$
```

## Blocked and Unblocked Word List

`blocked_terms.txt` contains a list of words which are blocked if
`keyword_check` is enabled in the project. `unblocked_terms.txt` is a copy of
`blocked_terms.txt`. So, by default nothing will be blocked.

`unblocked_terms.txt` in this directry is referenced only if it doesn't exist in
a project directory. To transition to the default blocked word list for project
X, one can do:

1.  Copy `unblocked_terms.txt` to X.
2.  Remove words which are not used in X.
    `$ egrep -r -i -I '(keyword1|keyword2|...)'`
3.  Test and submit CL.

Then, when blocking word 'foo' for project X, one can do:

1.  Remove 'foo' from `unblocked_terms.txt` in X.
2.  Grep and fix matches.
    `$ egrep -r -i -I foo`
3.  Test & submit CL.

# Third Party code

We have many third party repos where you probably want to disable CrOS checks.
You'll need to disable each one in your project's PRESUBMIT.cfg file.
See the reference above for which checks you probably want to disable.

# Reporting issues

You can file bugs at https://crbug.com/new with
Component=Infra>ChromeOS>Build>Platform.

If you want to ask questions, use our normal
[development groups](https://chromium.googlesource.com/chromiumos/docs/+/HEAD/contact.md).
