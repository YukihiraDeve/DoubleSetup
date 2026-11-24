"""Bouton « Mettre à jour » : vrais dépôts git temporaires (origine, PC, poste de dev)."""

import shutil
import subprocess

import pytest

from setupdouble.updater import Updater

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git absent")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                        "HOME": str(cwd)})


@pytest.fixture
def repos(tmp_path):
    origin, dev, pc = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "pc"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True,
                   capture_output=True)
    git(dev, "checkout", "-q", "-b", "main")
    (dev / "requirements-win.txt").write_text("numpy\n")
    (dev / "app.py").write_text("v = 1\n")
    git(dev, "add", ".")
    git(dev, "commit", "-q", "-m", "première version")
    git(dev, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(origin), str(pc)], check=True,
                   capture_output=True)
    (pc / "config.toml").write_text('secret = "local"\n')  # non suivi : jamais touché
    return dev, pc


def make_updater(pc, installs):
    up = Updater(root=pc, requirements="requirements-win.txt", python="python")
    up.install_dependencies = lambda: installs.append(True)
    return up


def test_already_up_to_date(repos):
    dev, pc = repos
    result = make_updater(pc, []).run()
    assert result.status == "up_to_date" and not result.restart_needed


def test_update_pulls_commits_and_keeps_config(repos):
    dev, pc = repos
    (dev / "app.py").write_text("v = 2\n")
    git(dev, "commit", "-q", "-am", "le raccourci réveille le Mac")
    git(dev, "push", "-q")
    installs = []
    result = make_updater(pc, installs).run()
    assert result.status == "updated" and result.restart_needed
    assert result.changes == ["le raccourci réveille le Mac"]
    assert (pc / "app.py").read_text() == "v = 2\n"
    assert (pc / "config.toml").read_text() == 'secret = "local"\n'
    assert installs == [] and not result.deps_installed  # requirements inchangé


def test_dependencies_reinstalled_when_requirements_change(repos):
    dev, pc = repos
    (dev / "requirements-win.txt").write_text("numpy\npystray\n")
    git(dev, "commit", "-q", "-am", "nouvelle dépendance")
    git(dev, "push", "-q")
    installs = []
    result = make_updater(pc, installs).run()
    assert result.status == "updated" and installs == [True] and result.deps_installed
    assert "dépendances" in result.message


def test_local_commits_block_the_update(repos):
    dev, pc = repos
    (pc / "app.py").write_text("bricolage local\n")
    git(pc, "commit", "-q", "-am", "local")
    (dev / "app.py").write_text("v = 2\n")
    git(dev, "commit", "-q", "-am", "distant")
    git(dev, "push", "-q")
    result = make_updater(pc, []).run()
    assert result.status == "error" and "commits locaux" in result.message
    assert (pc / "app.py").read_text() == "bricolage local\n"


def test_not_a_git_checkout(tmp_path):
    result = Updater(root=tmp_path, requirements="requirements-win.txt").run()
    assert result.status == "error" and "git clone" in result.message


def test_ahead_of_origin_is_up_to_date(repos):
    dev, pc = repos
    (pc / "app.py").write_text("en avance\n")
    git(pc, "commit", "-q", "-am", "pas encore poussé")
    result = make_updater(pc, []).run()
    assert result.status == "up_to_date" and "pas encore publiés" in result.message
