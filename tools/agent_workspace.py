"""Crea un espacio de trabajo aislado (rama + carpeta de trabajo de git) para un agente.

  python tools/agent_workspace.py <tarea> [puerto]
  python tools/agent_workspace.py --list
  python tools/agent_workspace.py --remove <tarea>

Que hace:
  - git worktree add <raiz>/../FlatOut-wt/<tarea> -b <tarea>   (rama nueva desde la actual)
  - enlaza extracted/ del proyecto principal dentro de la carpeta (junction en Windows, symlink en
    otros sistemas), porque esa carpeta (7 GB de datos del juego) no esta en git y los exportadores
    la necesitan en <carpeta>/extracted
  - imprime el comando para levantar su servidor en un puerto propio (por defecto 8081 + n.o de espacios)

Las salidas generadas (web/cars, web/tracks, web/data...) tampoco estan en git: cada espacio las
regenera con los scripts de tools/ o las copia a mano del principal.
"""
import os
import subprocess
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parent.parent
WT_ROOT = MAIN.parent / "FlatOut-wt"


def run(*cmd, cwd=MAIN):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"error: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def link_extracted(dest: Path):
    src = MAIN / "extracted"
    if not src.exists():
        print("aviso: no existe", src, "- no se enlaza")
        return
    link = dest / "extracted"
    if link.exists():
        return
    if os.name == "nt":   # junction: no pide permisos de administrador
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(src)], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"no se pudo crear el enlace: {r.stderr.strip() or r.stdout.strip()}")
    else:
        link.symlink_to(src, target_is_directory=True)


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return
    if args[0] == "--list":
        print(run("git", "worktree", "list"))
        return
    if args[0] == "--remove":
        dest = WT_ROOT / args[1]
        # el junction se borra primero para que git no entre en los 7 GB de extracted/
        link = dest / "extracted"
        if link.exists():
            if os.name == "nt":
                subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
            else:
                link.unlink()
        print(run("git", "worktree", "remove", "--force", str(dest)))
        print(f"rama '{args[1]}' conservada; borrala con: git branch -D {args[1]}")
        return

    task = args[0]
    dest = WT_ROOT / task
    n = len(list(WT_ROOT.iterdir())) if WT_ROOT.exists() else 0
    port = int(args[1]) if len(args) > 1 else 8081 + n
    WT_ROOT.mkdir(exist_ok=True)
    print(run("git", "worktree", "add", str(dest), "-b", task) or f"worktree creado en {dest}")
    link_extracted(dest)
    print(f"\nEspacio listo: {dest}\n  rama: {task}\n  servidor propio:  cd {dest} && python -m http.server {port} --bind 127.0.0.1"
          f"\n  abrir:            http://127.0.0.1:{port}/web/drive.html?car=1&track=arena1"
          f"\n  (recuerda regenerar web/cars, web/tracks y web/data con tools/*.py o copiarlos del principal)")


if __name__ == "__main__":
    main()
