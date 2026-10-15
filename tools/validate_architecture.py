"""Recursive objective architecture checks; no fixed module/file counts."""
import ast
from pathlib import Path


def validate(root):
    errors=[]
    root=Path(root)
    def module(path):
        for name in ("index.py","README.md","components"):
            if not (path/name).exists():
                errors.append(str(path)+": missing "+name)
        readme=(path/"README.md").read_text(encoding="utf-8") if (path/"README.md").exists() else ""
        for heading in ("Purpose","Responsibilities","Non-responsibilities","Public boundary","Allowed dependencies"):
            if "## "+heading not in readme:
                errors.append(str(path)+": missing contract "+heading)
        for py in path.glob("*.py"):
            if py.name not in ("index.py","__init__.py","__main__.py"):
                errors.append(str(py)+": module-root production implementation")
            if py.name=="index.py":
                tree=ast.parse(py.read_text(encoding="utf-8"))
                if any(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) for n in tree.body):
                    errors.append(str(py)+": boundary contains implementation definitions")
        comp=path/"components"
        if comp.exists():
            for child in sorted(comp.iterdir()):
                if child.is_dir() and child.name!="__pycache__":
                    module(child)
    module(root)
    for py in root.rglob("*.py"):
        tree=ast.parse(py.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n,ast.ImportFrom) and n.module:
                # Cross-component dependencies must terminate at an index boundary.
                if n.level>=3 and not n.module.endswith(".index"):
                    errors.append(str(py)+": cross-owner deep import "+n.module)
    return errors


if __name__=="__main__":
    issues=validate(Path(__file__).resolve().parents[1]/"patrol")
    print("\n".join(issues) if issues else "RSMA recursive structure and public imports: PASS")
    raise SystemExit(bool(issues))

