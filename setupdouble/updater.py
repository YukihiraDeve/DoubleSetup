"""Mise à jour de SetupDouble depuis son dépôt git (bouton « Mettre à jour »).

    python -m setupdouble.updater        # vérifie et met à jour, sans redémarrer

Étapes : git fetch, avance rapide (git pull --ff-only), puis réinstallation
des dépendances si le fichier requirements de la plateforme a changé.
config.toml n'est pas suivi par git : il n'est jamais touché.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT

log = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class UpdateResult:
    status: str  # "up_to_date", "updated" ou "error"
    message: str
    changes: list[str] = field(default_factory=list)  # titres des commits récupérés
    deps_installed: bool = False

    @property
    def restart_needed(self) -> bool:
        return self.status == "updated"


class UpdateError(Exception):
    pass


def find_git() -> str | None:
    git = shutil.which("git")
    if git:
        return git
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            for sub in (r"Git\cmd\git.exe", r"Programs\Git\cmd\git.exe"):
                p = Path(base) / sub
                if p.exists():
                    return str(p)
    return None


def requirements_file() -> str:
    return "requirements-win.txt" if sys.platform == "win32" else "requirements-mac.txt"


class Updater:
    def __init__(self, root: Path = ROOT, git: str | None = None,
                 python: str | None = None, requirements: str | None = None):
        self.root = Path(root)
        self.git = git or find_git()
        self.python = python or _console_python()
        self.requirements = requirements or requirements_file()

    def _git(self, *args: str, timeout: float = 60) -> str:
        if self.git is None:
            raise UpdateError("Git est introuvable : installe-le (winget install Git.Git).")
        try:
            out = subprocess.run([self.git, "-C", str(self.root), *args], capture_output=True,
                                 text=True, timeout=timeout, creationflags=_NO_WINDOW,
                                 env=dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C"))
        except subprocess.TimeoutExpired:
            raise UpdateError(f"git {args[0]} : délai dépassé (réseau ?)") from None
        except OSError as exc:
            raise UpdateError(f"git introuvable : {exc}") from None
        if out.returncode != 0:
            detail = (out.stderr or out.stdout).strip().splitlines()
            raise UpdateError(f"git {args[0]} a échoué : {detail[-1] if detail else out.returncode}")
        return out.stdout.strip()

    def run(self) -> UpdateResult:
        try:
            return self._run()
        except UpdateError as exc:
            log.warning("Mise à jour impossible : %s", exc)
            return UpdateResult("error", str(exc))

    def _run(self) -> UpdateResult:
        if not (self.root / ".git").exists():
            raise UpdateError("Ce dossier n'est pas un dépôt git : récupère le projet avec "
                              "« git clone » pour pouvoir le mettre à jour.")
        try:
            upstream = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        except UpdateError:
            raise UpdateError("La branche locale ne suit aucune branche distante "
                              "(git branch -u origin/main).") from None
        remote = upstream.split("/", 1)[0]
        self._git("fetch", "--quiet", remote, timeout=60)
        old = self._git("rev-parse", "HEAD")
        new_remote = self._git("rev-parse", "@{u}")
        if old == new_remote:
            return UpdateResult("up_to_date", "SetupDouble est déjà à jour.")
        if self._is_ancestor(new_remote, old):
            return UpdateResult("up_to_date", "SetupDouble est déjà à jour "
                                "(ce dossier a des changements pas encore publiés).")
        if not self._is_ancestor(old, new_remote):
            raise UpdateError("Le dossier contient des commits locaux absents de GitHub : "
                              "mise à jour automatique impossible.")
        changes = self._git("log", "--format=%s", f"{old}..{new_remote}").splitlines()
        self._git("merge", "--ff-only", "--quiet", new_remote)
        changed_files = self._git("diff", "--name-only", old, "HEAD").splitlines()
        deps = False
        if self.requirements in changed_files:
            self.install_dependencies()
            deps = True
        n = len(changes)
        msg = f"{n} nouveauté{'s' if n > 1 else ''} installée{'s' if n > 1 else ''}"
        if deps:
            msg += ", dépendances mises à jour"
        log.info("Mise à jour %s -> %s : %s", old[:7], new_remote[:7], "; ".join(changes))
        return UpdateResult("updated", msg + ".", changes, deps)

    def _is_ancestor(self, a: str, b: str) -> bool:
        try:
            self._git("merge-base", "--is-ancestor", a, b)
            return True
        except UpdateError:
            return False

    def install_dependencies(self) -> None:
        req = str(self.root / self.requirements)
        uv = shutil.which("uv")
        cmd = ([uv, "pip", "install", "--python", self.python, "-r", req] if uv
               else [self.python, "-m", "pip", "install", "--disable-pip-version-check", "-r", req])
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                                 creationflags=_NO_WINDOW, cwd=str(self.root))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"Installation des dépendances impossible : {exc}") from None
        if out.returncode != 0:
            last = (out.stderr or out.stdout).strip().splitlines()
            raise UpdateError("Installation des dépendances échouée : "
                              + (last[-1] if last else str(out.returncode)))


def _console_python() -> str:
    """python.exe plutôt que pythonw.exe (pip a besoin d'une console)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def main() -> int:
    logging.basicConfig(level="INFO", format="%(message)s")
    result = Updater().run()
    print(result.message)
    for title in result.changes:
        print(f"  • {title}")
    return 1 if result.status == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
