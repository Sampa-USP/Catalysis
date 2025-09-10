#!/usr/bin/env python3
import argparse, os, shutil, json, html
from pathlib import Path
import subprocess
from datetime import datetime

# Tipos de arquivo estáticos seguros para copiar junto com o site
SAFE_ASSETS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".css", ".js", ".ico", ".txt", ".pdf"
}

# ========================= HTML TEMPLATE (token-based) =========================
# Use tokens {{TITLE}}, {{TIMESTAMP}}, {{NBCOUNT}}, {{TREE_JSON}} que serão
# substituídos via .replace(...) em write_index (evita conflitos com chaves JS/CSS).
INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>{{TITLE}}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {
    --bg:#0f172a; --fg:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8;
    --card:#111827; --link:#93c5fd;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family:system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, 'Helvetica Neue', Arial, 'Noto Sans', 'Apple Color Emoji','Segoe UI Emoji'; background:var(--bg); color:var(--fg); }
  header { padding:16px 20px; border-bottom:1px solid #1f2937; background:#0b1220; position:sticky; top:0; z-index:1; }
  h1 { margin:0; font-size:1.1rem; letter-spacing:.5px; }
  .meta { color:var(--muted); font-size:.85rem; margin-top:4px; }
  main { display:grid; grid-template-columns: 320px 1fr; min-height: calc(100vh - 64px); }
  nav { border-right:1px solid #1f2937; padding:16px; overflow:auto; }
  section { padding:0; }
  .search { width:100%; padding:8px 10px; border-radius:8px; border:1px solid #334155; background:#0b1220; color:var(--fg); }
  ul.tree { list-style:none; padding-left:0; }
  .node { margin:2px 0; }
  .dir > .label::before { content:"▸"; display:inline-block; width:1em; color:var(--muted); }
  .dir.open > .label::before { content:"▾"; }
  .label { cursor:pointer; padding:2px 4px; border-radius:6px; }
  .label:hover { background:#111827; }
  .children { margin-left:18px; display:none; }
  .dir.open > .children { display:block; }
  a { color:var(--link); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .file-notebook::after { content:" .ipynb"; color:var(--muted); font-size:.8rem; margin-left:6px; }
  footer { color:var(--muted); font-size:.85rem; border-top:1px solid #1f2937; padding:10px 16px; }
  #viewer { width:100%; height: calc(100vh - 64px); border:none; display:block; background:#0b1220; }
</style>
</head>
<body>
<header>
  <h1>{{TITLE}}</h1>
  <div class="meta">Gerado em {{TIMESTAMP}} • Branch: main • Total de notebooks: <span id="nbcount">{{NBCOUNT}}</span></div>
</header>
<main>
  <nav>
    <input id="q" class="search" placeholder="Filtrar (nome do arquivo ou pasta)…" />
    <ul id="tree" class="tree"></ul>
  </nav>
  <section>
    <iframe id="viewer" src=""></iframe>
  </section>
</main>
<footer>
  Site estático gerado por <code>build_site.py</code> (nbconvert).
</footer>
<script>
const data = {{TREE_JSON}};  // JSON com a árvore
const elTree = document.getElementById('tree');
const q = document.getElementById('q');

function mkNode(node) {
  const li = document.createElement('li');
  li.className = 'node ' + (node.type === 'dir' ? 'dir' : 'file');
  const label = document.createElement('span');
  label.className = 'label';

  if (node.type === 'dir') {
    label.textContent = node.name;
    label.onclick = () => li.classList.toggle('open');
    li.appendChild(label);
    const ul = document.createElement('ul');
    ul.className = 'children';
    node.children.forEach(ch => ul.appendChild(mkNode(ch)));
    li.appendChild(ul);
  } else {
    if (node.nb_html) {
      const a = document.createElement('a');
      a.textContent = node.name;
      a.href = "#";
      a.className = 'file-notebook';
      a.onclick = (e) => {
        e.preventDefault();
        document.getElementById('viewer').src = node.nb_html;
      };
      label.appendChild(a);
    } else {
      label.textContent = node.name;
    }
    li.appendChild(label);
  }
  return li;
}

function render(filter='') {
  elTree.innerHTML = '';
  const norm = s => s.toLowerCase();
  function pass(node) {
    if (!filter) return true;
    return norm(node.name).includes(filter) || (node.path && norm(node.path).includes(filter));
  }
  function cloneFiltered(node) {
    if (node.type === 'file') return pass(node) ? node : null;
    const kids = node.children.map(cloneFiltered).filter(Boolean);
    if (kids.length) return {...node, children:kids};
    return pass(node) ? {...node, children:[]} : null;
  }
  const filtered = cloneFiltered(data);
  if (!filtered) {
    elTree.innerHTML = '<li class="node">Nada encontrado…</li>';
    return;
  }
  filtered.children.forEach(ch => elTree.appendChild(mkNode(ch)));
}

q.addEventListener('input', (e) => render(e.target.value.trim().toLowerCase()));
render();
</script>
</body>
</html>
"""
# =============================================================================


def collect_tree(src: Path, out: Path, execute: bool):
    """
    Varre src; converte .ipynb -> .html em out mantendo a árvore.
    Retorna (tree_dict, nb_count).
    """
    nb_count = 0
    root = {"type": "dir", "name": src.name, "path": "", "children": []}
    dir_map = {str(src.resolve()): root}

    for path in sorted(src.rglob("*")):
        # pula a própria pasta de saída e seus filhos
        if out in path.parents or path == out:
            continue

        parts = path.relative_to(src).parts
        if not parts:
            continue

        # ignore .git e .github por padrão (ajuste se quiser listá-los)
        if any(p.startswith(".git") for p in parts):
            continue
        if parts[0] in (".github",):
            continue

        # garantir nós de diretório
        cur_src_dir = src
        cur_node = root
        for i, p in enumerate(parts[:-1]):
            cur_src_dir = cur_src_dir / p
            key = str(cur_src_dir.resolve())
            if key not in dir_map:
                node = {
                    "type": "dir",
                    "name": p,
                    "path": str(Path(*parts[: i + 1])),
                    "children": [],
                }
                cur_node["children"].append(node)
                dir_map[key] = node
            cur_node = dir_map[key]

        if path.is_dir():
            # diretorios já mapeados acima
            continue

        # arquivo
        ext = path.suffix.lower()
        rel = path.relative_to(src)
        file_node = {"type": "file", "name": rel.name, "path": str(rel)}

        if ext == ".ipynb":
            nb_count += 1
            out_html = (out / rel).with_suffix(".html")
            out_html.parent.mkdir(parents=True, exist_ok=True)

            cmd = [
                "jupyter", "nbconvert",
                "--to", "html",
                "--output", out_html.name,
                "--output-dir", str(out_html.parent),
                str(path)
            ]
            if execute:
                cmd.insert(3, "--execute")  # após "html" funciona também usar no fim
                cmd.append("--execute")

            # Execução do nbconvert
            subprocess.run(cmd, check=True)

            # link relativo para o HTML gerado
            file_node["nb_html"] = str(out_html.relative_to(out)).replace(os.sep, "/")

        else:
            # copia ativos "seguros"
            if ext in SAFE_ASSETS:
                dst = out / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dst)

        # anexa arquivo ao nó pai
        parent_key = str(path.parent.resolve())
        parent_node = dir_map.get(parent_key, root)
        parent_node["children"].append(file_node)

    return root, nb_count


def write_index(out: Path, tree: dict, nb_count: int, title: str):
    out.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html_doc = (
        INDEX_HTML_TEMPLATE
        .replace("{{TITLE}}", html.escape(title))
        .replace("{{TIMESTAMP}}", timestamp)
        .replace("{{NBCOUNT}}", str(nb_count))
        .replace("{{TREE_JSON}}", json.dumps(tree, ensure_ascii=False))
    )
    (out / "index.html").write_text(html_doc, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=str, required=True, help="Raiz do repositório (onde estão os notebooks)")
    ap.add_argument("--out", type=str, required=True, help="Diretório de saída do site estático (será publicado no Pages)")
    ap.add_argument("--execute", type=str, default="false", help="true/false: executar notebooks antes de converter")
    args = ap.parse_args()

    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    execute = args.execute.lower() == "true"

    tree, nb_count = collect_tree(src, out, execute)
    write_index(out, tree, nb_count, title=f"Árvore de Notebooks — {src.name}")


if __name__ == "__main__":
    main()