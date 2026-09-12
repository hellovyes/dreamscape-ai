# -*- coding: utf-8 -*-
"""临时静态审查脚本：语法、重复定义、未定义名称（近似）。"""
import ast, io, os

d = os.path.dirname(os.path.abspath(__file__))
files = [f for f in os.listdir(d) if f.endswith('.py') and not f.startswith('_')]
issues = 0
for fn in files:
    p = os.path.join(d, fn)
    try:
        src = io.open(p, encoding='utf-8').read()
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f'{fn}: SYNTAX ERROR {e}')
        issues += 1
        continue
    seen = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in seen:
                print(f'{fn}: duplicate def {node.name} @ line {seen[node.name]} and {node.lineno}')
                issues += 1
            seen[node.name] = node.lineno
    # collect module-level + class-level names, then find Load names never bound in this module
    scope_stack = [set()]
    # simple two-pass: collect all bound names (approx, includes globals of other modules not imported)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != '*':
                    bound.add(a.asname or a.name)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                bound.add(node.id)
    # function args
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
                bound.add(a.arg)
            if args.vararg: bound.add(args.vararg.arg)
            if args.kwarg: bound.add(args.kwarg.arg)
    import builtins as B
    bound |= set(dir(B))
    bound |= {'self', 'cls', 'Qt', 'tr', 'QApplication'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in bound:
                # skip known cross-module refs (config.*, etc. are Attribute not Name)
                print(f'{fn}:? undefined name {node.id!r} @ line {node.lineno}')
                issues += 1
print('---')
print('issues:', issues)
