"""Refuse git that would modify the real checkout while the suite runs.

api/updates.py runs git against REPO_ROOT and the hermes-agent checkout
(`checkout .`, `clean -fd`, `reset --hard`, `stash`, `fetch`). Tests keep that
off the real checkouts by patching `_run_git`, `REPO_ROOT` or `_AGENT_DIR`; a
patch that fails to bind (a test that re-imports `api.updates` gets a second
module object) runs the commands for real. `install()` (called from
conftest.py, next to the hermetic network guard) replaces `subprocess.Popen`,
which `run`, `call` and `check_output` go through, with a subclass that refuses
a git command that is not read-only when it targets the real webui checkout, a
directory above or inside it, or the agent checkout. Each refusal is recorded
in `REFUSALS`; the `pytest_runtest_makereport` wrapper below (re-exported by
conftest.py) fails the test, even when the refusal happened on a worker thread
or inside a fixture finalizer. This is a safety net for the suite's own code
and tests, not a security boundary: it reads what Popen is given (argv, shell,
executable, cwd, env), nothing a program does once started. Read-only git and every other command pass
through, a temporary repository is never affected, and a test's own
`mock.patch("subprocess.run")` still wins. The guard covers this process only:
the test server is a separate process. The `allow_real_repo_git` fixture opts
a test out. This lives in its own module so that a conftest re-executed under
another name (tests do that) shares one guard, one record and one exception.
"""
import os
import pathlib
import re
import subprocess

import pytest

REAL_POPEN = subprocess.Popen
REFUSALS = []
_ROOTS = []
_EXEMPT = []  # pytest's --basetemp: a path under it with no guarded root above it is not the checkout (see is_real_checkout)

_READ_ONLY_VERBS = frozenset({
    'blame', 'cat-file', 'check-ignore', 'check-ref-format', 'describe', 'diff', 'diff-index',
    'for-each-ref', 'log', 'ls-files', 'ls-tree', 'merge-base', 'name-rev', 'rev-list',
    'rev-parse', 'show', 'show-ref', 'status', 'var', 'version', '--version', '--help', '-h',
})
_TAG_WRITING_FLAGS = {'-a', '-d', '-f', '-m', '-s', '-u', '--annotate', '--delete', '--force', '--sign'}
_TAG_LISTING_FLAGS = {'--list', '-l', '--contains', '--no-contains', '--merged', '--no-merged', '--points-at'}
_BRANCH_LISTING_FLAGS = {
    '--show-current', '--list', '-l', '-a', '--all', '-r', '--remotes', '-v', '-vv', '--verbose',
    '--contains', '--no-contains', '--merged', '--no-merged', '--points-at',
}
# git's own options that precede the verb: those taking a value, and bare flags.
_GLOBAL_WITH_VALUE = {'-c', '-C', '--config-env', '--exec-path', '--git-dir', '--namespace', '--work-tree'}
_GLOBAL_FLAGS = {
    '-p', '--paginate', '--no-pager', '--literal-pathspecs', '--glob-pathspecs',
    '--noglob-pathspecs', '--icase-pathspecs', '--no-optional-locks', '--no-replace-objects',
}
# `git` as a command: at the start of a shell line or after a separator, allowing the usual
# prefixes (`exec git`, `if git`, `time git`, `VAR=1 git`), not the word inside a comment.
_GIT_COMMAND = re.compile(
    r'(^|[;&|(`\n]\s*)((exec|if|elif|while|until|then|else|do|time|!|[A-Za-z_][A-Za-z0-9_]*=\S*)\s+)*'
    r'([^\s=]*/)?git(\.exe|\.cmd)?(\s|$)',
    re.IGNORECASE,
)
# values these verbs' options take, so a value is not mistaken for the directory they act on
_DIRECTORY_VERB_OPTIONS_WITH_VALUE = {
    '-b', '-o', '-u', '--branch', '--depth', '--filter', '--origin', '--reference', '--reference-if-able',
    '--separate-git-dir', '--shallow-since', '--shallow-exclude', '--template', '--upload-pack', '-c',
    '--config', '--initial-branch', '--object-format', '--shared', '--base-path', '--listen', '--port',
    '--pid-file', '--user', '--group', '--interpolated-path', '--access-hook', '--init-timeout', '--timeout',
    '--max-connections', '--log-destination',
}
_BRANCH_FORCED_LISTING_FLAGS = {'--list', '-l', '--contains', '--no-contains', '--merged', '--no-merged', '--points-at'}
_GIT_PROGRAMS = ('git', 'git.exe', 'git.cmd')
_LAUNCHERS = {'sh', 'bash', 'dash', 'zsh', 'env', 'timeout', 'nice', 'nohup', 'xargs', 'sudo', 'doas'}
# Verbs that write only inside the repository directory, never the work tree: with --git-dir (or
# GIT_DIR) naming another repository and no work tree named, they cannot touch cwd's files or index.
_REPOSITORY_ONLY_VERBS = frozenset({
    'bundle', 'commit-tree', 'config', 'count-objects', 'fetch', 'fsck', 'gc', 'hash-object', 'mktag',
    'mktree', 'notes', 'pack-refs', 'prune', 'push', 'receive-pack', 'reflog', 'remote', 'repack',
    'replace', 'symbolic-ref', 'tag', 'update-ref', 'upload-pack', 'write-tree',
})


class RealCheckoutGitRefused(RuntimeError):
    """Raised by the guard in place of running the command."""


def is_read_only(verb_args) -> bool:
    """True only for git commands that cannot change a repository or write a file."""
    if not verb_args:
        return True  # bare `git` prints usage
    verb, rest = verb_args[0], verb_args[1:]
    # after `--` everything is an operand, so `config -- core.x --get` writes and `tag -- --list` creates
    end_of_options = rest.index('--') if '--' in rest else len(rest)
    options, operands = rest[:end_of_options], rest[end_of_options + 1:]
    if any(a.startswith('--output') for a in options):
        return False  # `log --output=<file>` and friends write
    flags = {a for a in options if a.startswith('-')}
    positional = [a for a in options if not a.startswith('-')] + operands
    if verb in _READ_ONLY_VERBS:
        return True
    if verb == 'symbolic-ref':  # `symbolic-ref <name> <ref>` and `--delete` write
        return len(positional) <= 1 and not ({'-d', '--delete'} & flags)
    if verb == 'tag':  # listings read (`--list <glob>`, by ref, bare); `tag <name>` writes
        return bool(_TAG_LISTING_FLAGS & flags) or (not positional and not (_TAG_WRITING_FLAGS & flags))
    if verb == 'config':
        return any(a.startswith('--get') or a in ('--list', '-l') for a in options)
    if verb == 'remote':  # `remote show <name>` contacts the remote unless -n
        return not positional or positional[0] == 'get-url' or (
            positional[0] == 'show' and bool({'-n', '--no-query'} & flags))
    if verb == 'branch':  # listings read; `branch <name>` writes, even with -v/-a/-r
        return flags <= _BRANCH_LISTING_FLAGS and (not positional or bool(_BRANCH_FORCED_LISTING_FLAGS & flags))
    if verb == 'stash':  # the subcommand is the first word: `stash -m list` and `stash -- list` push
        return rest[:1] in (['list'], ['show'])
    if verb == 'worktree':
        return rest[:1] == ['list']
    if verb == 'ls-remote':  # `--get-url` prints the configured URL without contacting it
        return '--get-url' in flags
    return False


def _nearest(p, dirs):
    """The deepest of *dirs* that is *p* or an ancestor of it, else None."""
    hits = [d for d in dirs if p == d or d in p.parents]
    return max(hits, key=lambda d: len(d.parts)) if hits else None


def is_real_checkout(cwd) -> bool:
    """True when *cwd* is a real checkout, inside one, or above one.

    A path under pytest's ``--basetemp`` is exempt only when no guarded root is
    above it: a plain directory under a basetemp INSIDE the checkout is still
    discovered by git as the checkout, so it stays guarded (that layout is not one
    the suite supports; its runners pass no basetemp or one outside the checkout).
    A root installed under a basetemp (a test's fake checkout) is guarded, and so
    is a linked worktree of a guarded checkout wherever it was made. An
    unresolvable cwd counts as the real checkout (fail closed).
    """
    try:
        p = pathlib.Path(os.getcwd() if cwd is None else cwd).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return True
    if _nearest(p, _ROOTS) is not None:
        return True
    if _nearest(p, _EXEMPT) is None and any(p in r.parents for r in _ROOTS):
        return True
    # A linked worktree of a guarded checkout made after install() (its `.git` points into that checkout).
    return any(_linked_to_guarded_checkout(d) for d in (p, *p.parents))


def _linked_to_guarded_checkout(path) -> bool:
    main = _main_checkout(path)
    return main is not None and _nearest(main, _ROOTS) is not None


def _join(base, path):
    return os.path.join(os.getcwd() if base is None else base, path)


def _program_named_git(token) -> bool:
    """A launcher argument that is the git program (not an option value or a directory)."""
    return (
        not token.startswith('-') and '=' not in token
        and os.path.basename(token).lower() in _GIT_PROGRAMS and not os.path.isdir(token)
    )


def refusal(args, cwd, *, shell=False, executable=None, env=None):
    """Return why this Popen call must not run, or None when it may."""
    cwd_unreadable = False
    if cwd is not None:
        try:
            cwd = os.fsdecode(cwd)
        except (TypeError, ValueError):
            cwd_unreadable = True  # only matters for a git command: see below
    if isinstance(args, os.PathLike):
        args = [os.fspath(args)]
    if shell and not isinstance(args, (str, bytes)):
        try:
            args = ' '.join(os.fsdecode(a) for a in args)  # the shell sees them joined
        except (TypeError, ValueError):
            return 'unparseable argv' if is_real_checkout(cwd) else None
    if isinstance(args, (str, bytes)):  # a shell command line: opaque when it runs git
        text = args.decode(errors='replace') if isinstance(args, bytes) else args
        argv = None if _GIT_COMMAND.search(text) else []
    else:
        try:
            argv = [os.fsdecode(a) for a in args]
        except (TypeError, ValueError):
            return 'unparseable argv' if is_real_checkout(cwd) else None
        program = os.path.basename(os.fsdecode(executable) if executable else (argv[0] if argv else '')).lower()
        if program in _LAUNCHERS:  # `sh -c "git ..."`, `env X=1 git ...`: opaque when git is named
            # a single token is a program or not; only a command line (with spaces) is searched
            argv = None if any(_program_named_git(a) or (re.search(r'\s', a) and _GIT_COMMAND.search(a)) for a in argv[1:]) else []
        elif program not in _GIT_PROGRAMS:
            argv = []
    if argv == []:
        return None  # not a git command
    if cwd_unreadable:
        return 'unparseable cwd'  # cannot tell where it acts: fail closed
    # Where the command may act: cwd after any -C (`here`) plus every --git-dir/--work-tree and
    # GIT_DIR/GIT_WORK_TREE from the child's environment; `init`/`clone`/`daemon` act on the
    # directory they name instead of cwd.
    # git resolves a relative --git-dir/--work-tree against the directory the last -C leaves it in.
    child_env = os.environ if env is None else env
    here, relative, targets, verb_args, opaque = cwd, [], [], [], argv is None
    repository_named = work_tree_named = False
    i = 1
    while argv is not None and i < len(argv):
        a = argv[i]
        if a in _GLOBAL_WITH_VALUE:
            if a == '-C' and i + 1 < len(argv):
                here = _join(here, argv[i + 1])
                targets.append(here)
            elif a in ('--git-dir', '--work-tree') and i + 1 < len(argv):
                relative.append(argv[i + 1])
                repository_named |= a == '--git-dir'
                work_tree_named |= a == '--work-tree'
            elif a == '-c' and i + 1 < len(argv) and argv[i + 1].startswith('alias.'):
                opaque = True  # an alias can expand to anything
                break
            i += 2
        elif a.startswith(('--git-dir=', '--work-tree=')):
            relative.append(a.split('=', 1)[1])
            repository_named |= a.startswith('--git-dir=')
            work_tree_named |= a.startswith('--work-tree=')
            i += 1
        elif a in _GLOBAL_FLAGS or a.startswith(('--config-env=', '--exec-path=', '--namespace=')):
            i += 1
        elif a.startswith('-c') and len(a) > 2:
            if a[2:].startswith('alias.'):
                opaque = True
                break
            i += 1
        elif a in ('--version', '--help', '-h'):
            verb_args = [a]
            break
        elif a.startswith('-'):  # anything unknown before the verb: opaque
            opaque = True
            break
        else:
            verb_args = argv[i:]
            break
    targets += [_join(here, r) for r in relative]
    for key, value in child_env.items():
        try:
            key, value = os.fsdecode(key), os.fsdecode(value)
        except (TypeError, ValueError):
            continue
        if key in ('GIT_DIR', 'GIT_WORK_TREE') and value:
            targets.append(_join(here, value))
            repository_named |= key == 'GIT_DIR'
            work_tree_named |= key == 'GIT_WORK_TREE'
        elif key in ('GIT_COMMON_DIR', 'GIT_CONFIG', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM') and value:
            targets.append(_join(here, value))  # `GIT_CONFIG=<real>/.git/config git config k v` writes it
    # `here` is a target too: with only one of the repository or the work tree named, git takes
    # the other half from cwd (`--git-dir=<tmp>/.git reset --hard` rewrites cwd's files, and
    # `--work-tree=<tmp> reset --hard` rewrites cwd's index). The one exception: another
    # repository named and the verb writes only inside a repository, so cwd is not involved.
    repository_only = bool(verb_args) and verb_args[0] in _REPOSITORY_ONLY_VERBS
    if not (repository_named and not work_tree_named and repository_only):
        targets.append(here)
    # A verb argument that names a protected place is a target too (`push <real> HEAD:x`,
    # `config --file <real>/.git/config`, `remote add o <real>`): an absolute path, or a
    # relative one that exists from `here`; a ref such as refs/heads/x is neither.
    path_args = []
    if verb_args and verb_args[0] == 'config':
        # keys and values are not places (`config core.fsmonitor <checkout>/.venv/bin/python hook.py`
        # in a temp repo is fine): only `--file <path>`, `-f <path>` and `--file=<path>` name one
        for j, arg in enumerate(verb_args[1:], 1):
            option, attached, value = arg.partition('=')
            if len(option) >= 5 and '--file'.startswith(option):  # git takes any unambiguous prefix: `--fil`
                if attached:
                    path_args.append(value)  # --file=<path>, --fil=<path>
                elif j + 1 < len(verb_args):
                    path_args.append(verb_args[j + 1])
                continue
            short = re.match(r'-[^-]*?f(.*)', arg, re.S)  # a short cluster ending in f: -f, -zf, -zf<path>
            if short is None or arg.startswith('--'):
                continue
            if short.group(1):
                path_args.append(short.group(1))  # -f<path>, -zf<path>
            elif j + 1 < len(verb_args):
                path_args.append(verb_args[j + 1])  # -f <path>, -zf <path>
    else:
        for arg in verb_args[1:]:
            if arg.startswith('-'):
                if '=' not in arg:
                    continue
                arg = arg.split('=', 1)[1]  # an attached value, such as --output=<path>
            path_args.append(arg)
    for arg in path_args:
        if not arg:
            continue
        candidate = _join(here, arg)
        if os.path.isabs(arg) or os.path.exists(candidate):
            targets.append(candidate)
    if verb_args and verb_args[0] in ('init', 'clone', 'daemon'):
        # these act on the directory they name, not on cwd
        rest, positional, j = verb_args[1:], [], 0
        while j < len(rest):
            if rest[j] in _DIRECTORY_VERB_OPTIONS_WITH_VALUE:
                if rest[j] == '--separate-git-dir' and j + 1 < len(rest):
                    targets.append(_join(here, rest[j + 1]))
                j += 2
                continue
            if rest[j].startswith('--separate-git-dir='):
                targets.append(_join(here, rest[j].split('=', 1)[1]))
            elif rest[j].startswith('--base-path='):
                positional.append(rest[j].split('=', 1)[1])
            elif not rest[j].startswith('-'):
                positional.append(rest[j])
            j += 1
        named = None
        if verb_args[0] == 'init' and positional:
            named = positional[-1]
        elif verb_args[0] == 'clone' and len(positional) >= 2:
            named = positional[1]
        elif verb_args[0] == 'daemon' and positional:
            named = positional[-1]
        if named is not None:
            targets = [t for t in targets if t != here] + [_join(here, named)]
    if not any(is_real_checkout(t) for t in targets):
        return None
    if opaque:
        return 'cannot tell what this git command does'
    if is_read_only(verb_args):
        return None
    return f'`git {" ".join(verb_args)}` is not in the read-only allowlist'


def _popen_argument(popen_args, popen_kwargs, index, name):
    """A Popen argument given positionally (``index`` after ``args``) or by keyword."""
    return popen_args[index] if len(popen_args) > index else popen_kwargs.get(name)


class GuardedPopen(REAL_POPEN):
    def __init__(self, args, *popen_args, **popen_kwargs):
        cwd = _popen_argument(popen_args, popen_kwargs, 8, 'cwd')
        why = refusal(
            args, cwd,
            shell=bool(_popen_argument(popen_args, popen_kwargs, 7, 'shell')),
            executable=_popen_argument(popen_args, popen_kwargs, 1, 'executable'),
            env=_popen_argument(popen_args, popen_kwargs, 9, 'env'),
        )
        if why is not None:
            message = (
                f"hermes test repo guard: refusing to run {args!r} in {cwd!r}: {why}. "
                "That is the real checkout, not a temporary repository. Patch "
                "api.updates._run_git, REPO_ROOT or _AGENT_DIR for this test (and check the "
                "patch binds), or request the allow_real_repo_git fixture."
            )
            REFUSALS.append(message)
            raise RealCheckoutGitRefused(message)
        super().__init__(args, *popen_args, **popen_kwargs)


def _main_checkout(root):
    """The main checkout owning a linked worktree's shared refs (its `.git` is a `gitdir:` file), else None."""
    try:
        dotgit = root / '.git'
        if not dotgit.is_file():
            return None
        pointer = dotgit.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return None
    if not pointer.startswith('gitdir:'):
        return None
    gitdir = (root / pointer[len('gitdir:'):].strip()).resolve()  # <main>/.git/worktrees/<name>
    if gitdir.parent.name == 'worktrees' and gitdir.parent.parent.name == '.git':
        return gitdir.parent.parent.parent
    return None


def install(*roots):
    """Guard *roots* (resolved; None entries ignored) in addition to any guarded already.

    A root that is a linked worktree also guards the main checkout its refs live in.
    """
    resolved = [pathlib.Path(r).resolve() for r in roots if r is not None]
    resolved += [main for main in map(_main_checkout, resolved) if main is not None]
    _ROOTS[:] = sorted({*_ROOTS, *resolved})
    subprocess.Popen = GuardedPopen


def pytest_configure(config):
    """Record pytest's ``--basetemp`` (see ``is_real_checkout``)."""
    basetemp = config.getoption('basetemp', default=None)
    _EXEMPT[:] = [pathlib.Path(basetemp).expanduser().resolve()] if basetemp else []


def pytest_runtest_logstart(nodeid, location):
    """A refusal raised at collection or import time already failed that module; don't blame the next test."""
    del REFUSALS[:]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Fail the test whose phase recorded a refusal, wherever the refusal was raised.

    Runs after the phase (so fixture finalizers have run), attributes the refusal
    to the item that caused it, and does not report twice when the refusal was
    already this phase's exception.
    """
    outcome = yield
    if not REFUSALS:
        return
    refusals = REFUSALS[:]
    del REFUSALS[:len(refusals)]  # a worker thread may append meanwhile; keep that for the next report
    report = outcome.get_result()
    if report.failed:
        return
    report.__dict__.pop('wasxfail', None)  # an xfail mark must not turn this into "unexpectedly passed"
    report.outcome = 'failed'
    report.longrepr = '\n'.join(refusals)
