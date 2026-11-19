#!/usr/bin/env python3
"""Install this standalone skill for a user or one project."""
import argparse
from pathlib import Path
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=["codex", "claude"])
    parser.add_argument("--project", type=Path, help="Install only in this existing project directory")
    args = parser.parse_args()
    root = args.project.expanduser().resolve() if args.project else Path.home()
    if not root.is_dir():
        parser.error(f"Каталог не найден: {root}")
    source = Path(__file__).resolve().parent / "skills" / "artem-questioning"
    folder = root / (".agents" if args.client == "codex" else ".claude") / "skills"
    target = folder / source.name
    if target.exists() or target.is_symlink():
        parser.exit(2, f"Этот путь уже занят: {target}. Сверьте установленную версию.\n")
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as stage:
        candidate = Path(stage) / source.name
        shutil.copytree(source, candidate, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        candidate.rename(target)
    print(f"Установлен {target}")


if __name__ == "__main__":
    main()
