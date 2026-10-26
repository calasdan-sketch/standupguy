"""What is packaged, and what the suite needs from Python 3.10.

Panel finding SG-7:
- tests/test_round3.py imported tomllib (new in Python 3.11) without a guard,
  so the whole suite failed to load on 3.10, which pyproject declares as
  supported;
- the sdist left out workflows/ (setuptools adds only tests/test*.py by
  itself), so the tests inside it that read the workflow file failed;
- building the working tree in place reuses a gitignored build/ left by an
  earlier build: setuptools packs everything it finds in build/lib - a module
  since deleted or renamed included - into the wheel.

Every build here is of clean_source_copy(), and runs offline: it needs a
setuptools >= 77 installed in this interpreter and is skipped without.
"""

import ast
import atexit
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# What tools leave in a project folder: not source, never packaged.
BUILD_LEFTOVERS = ("build", "dist", ".git", ".venv", "venv", ".tox",
                   ".pytest_cache", ".mypy_cache")

_TEMP_DIRS = []


def _rmtree(path):
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda f, p, e: onerror(f, p, e))
    else:
        shutil.rmtree(path, onerror=onerror)


@atexit.register
def _cleanup():
    for d in _TEMP_DIRS:
        try:
            _rmtree(d)
        except OSError:
            pass


def mktemp():
    d = tempfile.mkdtemp(prefix="sg")
    _TEMP_DIRS.append(d)
    return d


def have_setuptools(minimum=77):
    """Is a setuptools of at least this major version installed here, with
    what it needs to read `license = "MIT"` (packaging >= 24.2)? A build that
    needs no network can only use what is installed."""
    try:
        import setuptools
        if int(setuptools.__version__.split(".")[0]) < minimum:
            return False
        import packaging.licenses  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


def clean_source_copy(src=None, use_git=True):
    """A throwaway copy of the project with nothing a build left behind: the
    files git would commit (tracked, plus untracked and not ignored) when
    `src` is a git checkout of its own and git can say, otherwise everything
    except the usual build and tool folders at the top level, caches and
    *.pyc."""
    src = src or REPO
    dest = mktemp()
    names = None
    if (use_git and shutil.which("git")
            and os.path.exists(os.path.join(src, ".git"))):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        proc = subprocess.run(
            ["git", "-C", src, "ls-files", "-z", "--cached", "--others",
             "--exclude-standard"], capture_output=True, env=env)
        if proc.returncode == 0 and proc.stdout:
            names = [n for n in proc.stdout.decode("utf-8", "surrogateescape")
                     .split("\0") if n]
    if names is None:
        names = []
        for root, dirs, files in os.walk(src):
            top = os.path.relpath(root, src) == "."
            dirs[:] = [d for d in dirs if d != "__pycache__"
                       and not (top and (d in BUILD_LEFTOVERS
                                         or d.endswith(".egg-info")))]
            names += [os.path.relpath(os.path.join(root, f), src)
                      for f in files if not f.endswith((".pyc", ".pyo"))]
    for name in names:
        source = os.path.join(src, name)
        if os.path.isfile(source):
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
    return dest


def write(root, rel, text="x\n"):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def listing(root):
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                  for d, _dirs, files in os.walk(root) for f in files)


def child_env():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


LEFTOVERS = ["build/lib/standupguy/stale.py",
             "dist/standupguy-0.0.1-py3-none-any.whl",
             "standupguy.egg-info/PKG-INFO",
             ".git/config", ".venv/pyvenv.cfg", ".pytest_cache/README",
             "standupguy/__pycache__/cli.cpython-311.pyc",
             "standupguy/legacy.pyc",
             "tests/__pycache__/test_a.cpython-311.pyc"]
KEPT = ["pyproject.toml", "README.md", "MANIFEST.in", "standupguy/__init__.py",
        "workflows/flow.json", "tests/test_a.py"]


def project():
    root = mktemp()
    for rel in KEPT + LEFTOVERS:
        write(root, rel)
    return root


def git(root, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(["git", "-C", root, *args], env=env, check=True,
                   capture_output=True)


class TestCleanSourceCopy(unittest.TestCase):
    def test_without_git_the_build_leftovers_stay_behind(self):
        copy = clean_source_copy(project(), use_git=False)
        self.assertEqual(listing(copy), sorted(KEPT))

    def test_only_the_top_level_folders_are_leftovers(self):
        root = project()
        write(root, "standupguy/build/step.py")     # a package folder of that name
        names = listing(clean_source_copy(root, use_git=False))
        self.assertIn("standupguy/build/step.py", names)
        self.assertNotIn("build/lib/standupguy/stale.py", names)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_with_git_it_is_what_git_would_commit(self):
        root = project()
        git(root, "init", "-q")
        write(root, ".gitignore",
              "build/\ndist/\n*.egg-info/\n.venv/\n.pytest_cache/\n"
              "__pycache__/\n*.py[cod]\n*.log\n")
        write(root, "standupguy/new_module.py")     # untracked, not ignored
        write(root, "debug.log")                    # ignored, and on no list
        git(root, "add", "pyproject.toml")
        names = listing(clean_source_copy(root))
        self.assertIn("standupguy/new_module.py", names)
        self.assertIn(".gitignore", names)
        self.assertNotIn("debug.log", names)
        for leftover in LEFTOVERS:
            if not leftover.startswith(".git/"):
                self.assertNotIn(leftover, names)
        self.assertFalse([n for n in names if n.startswith(".git/")], names)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_when_git_cannot_say_the_walk_is_used(self):
        # the .git folder here is not a repository
        self.assertEqual(listing(clean_source_copy(project())), sorted(KEPT))

    def test_the_real_checkout_gives_the_package_and_no_leftovers(self):
        names = listing(clean_source_copy())
        for needed in ("pyproject.toml", "MANIFEST.in", "standupguy/cli.py",
                       "workflows/activepieces-standup-digest.json",
                       "tests/test_packaging.py"):
            self.assertIn(needed, names)
        junk = [n for n in names
                if n.split("/")[0] in BUILD_LEFTOVERS
                or ".egg-info" in n or "__pycache__" in n
                or n.endswith((".pyc", ".pyo"))]
        self.assertEqual(junk, [])


class TestManifest(unittest.TestCase):
    def test_the_tests_and_the_workflow_are_listed(self):
        with open(os.path.join(REPO, "MANIFEST.in"), encoding="utf-8") as f:
            lines = [ln.split() for ln in f.read().splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertIn(["recursive-include", "tests", "*.py"], lines)
        self.assertIn(["recursive-include", "workflows", "*.json"], lines)


class TestNothingIsDownloaded(unittest.TestCase):
    """pip, asked to build with build isolation, fetches the build backend from
    the package index: the one way a test here could leave this computer."""

    def test_pip_runs_only_here_and_never_with_build_isolation(self):
        here = os.path.dirname(os.path.abspath(__file__))
        runs = []
        for name in sorted(os.listdir(here)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(here, name), encoding="utf-8") as f:
                tree = ast.parse(f.read(), name)
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)):
                    words = [e.value for e in node.elts
                             if isinstance(e, ast.Constant)]
                    if "pip" in words:
                        runs.append((name, words))
        self.assertEqual({name for name, _ in runs}, {"test_packaging.py"}, runs)
        for name, words in runs:
            self.assertIn("--no-build-isolation", words, (name, words))


@unittest.skipUnless(have_setuptools(77),
                     "needs setuptools >= 77 installed (nothing is downloaded)")
class TestBuilt(unittest.TestCase):
    def sdist(self, src):
        out = mktemp()
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; from setuptools import build_meta; "
             "print(build_meta.build_sdist(sys.argv[1]))", out],
            cwd=src, env=child_env(), capture_output=True, text=True,
            timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        return os.path.join(out, proc.stdout.strip().splitlines()[-1])

    def wheel(self, src):
        out = mktemp()
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", src, "-w", out, "--no-deps",
             "--no-build-isolation", "-q"], cwd=out, env=child_env(),
            capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        wheels = [n for n in os.listdir(out) if n.endswith(".whl")]
        self.assertEqual(len(wheels), 1, wheels)
        with zipfile.ZipFile(os.path.join(out, wheels[0])) as zf:
            return set(zf.namelist())

    def test_the_sdist_carries_every_test_and_the_workflow(self):
        src = clean_source_copy()
        want = [n for n in listing(src)
                if (n.startswith("tests/") and n.endswith(".py"))
                or (n.startswith("workflows/") and n.endswith(".json"))]
        with tarfile.open(self.sdist(src)) as tar:
            names = {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}
        self.assertIn("workflows/activepieces-standup-digest.json", want)
        self.assertEqual([n for n in want if n not in names], [])
        for needed in ("pyproject.toml", "README.md", "LICENSE", "MANIFEST.in",
                       "standupguy/cli.py"):
            self.assertIn(needed, names)
        self.assertEqual([n for n in names
                          if n.startswith(("build/", "dist/", ".git/"))
                          or "__pycache__" in n or n.endswith(".pyc")], [])

    def test_the_tests_in_the_sdist_run(self):
        path = self.sdist(clean_source_copy())
        where = mktemp()
        with tarfile.open(path) as tar:
            if sys.version_info >= (3, 12):
                tar.extractall(where, filter="data")
            else:
                tar.extractall(where)
        root = os.path.join(where, os.listdir(where)[0])
        # the ones that read the workflow file, and the one that reads
        # pyproject.toml, README.md and LICENSE
        for pattern in ("test_serve.py", "test_serve_security.py",
                        "test_round5.py"):
            with self.subTest(pattern):
                proc = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s",
                     "tests", "-p", pattern], cwd=root, env=child_env(),
                    capture_output=True, text=True, timeout=600)
                self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
                self.assertIn("OK", proc.stderr)

    def test_a_stale_build_directory_cannot_reach_the_wheel(self):
        planted = clean_source_copy()
        write(planted, "build/lib/standupguy/zzz_stale.py", "STALE = 1\n")
        write(planted, "standupguy.egg-info/PKG-INFO", "Metadata-Version: 2.1\n")
        names = self.wheel(clean_source_copy(planted, use_git=False))
        self.assertIn("standupguy/cli.py", names)
        self.assertNotIn("standupguy/zzz_stale.py", names)

    def test_the_wheel_has_the_package_and_nothing_else(self):
        src = clean_source_copy()
        want = {n for n in listing(src)
                if n.startswith("standupguy/")
                and n.endswith((".py", "/py.typed"))}
        package = {n for n in self.wheel(src)
                   if not n.split("/")[0].endswith(".dist-info")}
        self.assertEqual(package, want)


class TestOneVersion(unittest.TestCase):
    def test_pyproject_the_cli_and_the_mcp_server_agree(self):
        with open(os.path.join(REPO, "pyproject.toml"), encoding="utf-8") as f:
            found = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
        self.assertTrue(found, "no version in pyproject.toml")
        version = found.group(1)
        from standupguy import cli, mcp
        self.assertEqual(cli.VERSION, version)
        self.assertEqual(mcp.SERVER_INFO["version"], version)
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}}, ["."])
        self.assertEqual(reply["result"]["serverInfo"]["version"], version)


class TestPython310IsStillSupported(unittest.TestCase):
    """pyproject says >= 3.10: no module of the package or of the suite may
    need something newer. Run on every interpreter, this fails on the first
    one where the guard is missing."""

    NEWER_NAMES = {"ExceptionGroup", "BaseExceptionGroup", "StrEnum",
                   "TaskGroup", "Self", "override", "LiteralString",
                   "NotRequired", "Required", "assert_never", "assert_type",
                   "reveal_type"}

    def sources(self):
        for folder in ("standupguy", "tests"):
            base = os.path.join(REPO, folder)
            for name in sorted(os.listdir(base)):
                if name.endswith(".py"):
                    yield os.path.join(folder, name)

    def problems(self, tree):
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def guarded(node):
            """Is this import the body of a try that catches ImportError?"""
            child, parent = node, parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.Try) and child in parent.body:
                    for handler in parent.handlers:
                        kinds = handler.type
                        names = {n.id for n in ast.walk(kinds)
                                 if isinstance(n, ast.Name)} if kinds else {
                                     "ImportError"}
                        if names & {"ImportError", "ModuleNotFoundError",
                                    "Exception"}:
                            return True
                child, parent = parent, parents.get(parent)
            return False

        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
                found += [f"line {node.lineno}: from {node.module} import "
                          f"{a.name}" for a in node.names
                          if a.name in self.NEWER_NAMES]
            else:
                modules = []
                if isinstance(node, ast.Name) and node.id in self.NEWER_NAMES:
                    found.append(f"line {node.lineno}: {node.id}")
                if (isinstance(node, ast.Attribute) and node.attr == "UTC"
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "datetime"):
                    found.append(f"line {node.lineno}: datetime.UTC")
                if type(node).__name__ == "TryStar":
                    found.append(f"line {node.lineno}: except*")
            if any(m.split(".")[0] == "tomllib" for m in modules) \
                    and not guarded(node):
                found.append(f"line {node.lineno}: import tomllib without a "
                             "try/except ImportError")
        return found

    def test_nothing_needs_python_3_11(self):
        bad = {}
        for rel in self.sources():
            with open(os.path.join(REPO, rel), encoding="utf-8") as f:
                tree = ast.parse(f.read(), rel)
            problems = self.problems(tree)
            if problems:
                bad[rel] = problems
        self.assertEqual(bad, {})

    def test_the_check_itself_sees_the_cases_it_is_for(self):
        def seen(source):
            return self.problems(ast.parse(source))

        self.assertTrue(seen("import tomllib\n"))
        self.assertTrue(seen("from tomllib import loads\n"))
        self.assertTrue(seen("try:\n    pass\nexcept OSError:\n    pass\n"
                             "import tomllib\n"))
        self.assertTrue(seen("try:\n    import tomllib\nexcept KeyError:\n"
                             "    tomllib = None\n"))
        self.assertEqual(seen("try:\n    import tomllib\nexcept ImportError:\n"
                              "    tomllib = None\n"), [])
        self.assertEqual(seen("try:\n    import tomllib\nexcept "
                              "(ImportError, OSError):\n    tomllib = None\n"),
                         [])
        self.assertTrue(seen("raise ExceptionGroup('x', [])\n"))
        self.assertTrue(seen("import datetime\nx = datetime.UTC\n"))
        self.assertTrue(seen("from typing import Self\n"))
        self.assertEqual(seen("import datetime\nx = datetime.timezone.utc\n"), [])


if __name__ == "__main__":
    unittest.main()
