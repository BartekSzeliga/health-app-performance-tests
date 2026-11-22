#!/usr/bin/env python3
"""Inventory of the backend's REST routes, read from the controller sources (no backend run needed).

For every handler method: HTTP method, full path, path variables, query parameters, whether it takes a JSON body
and the X-Organization-Id header, and the source location. The isolation runner uses it as the list of routes to
attack; the counts also show how much of the API a run covered.

Usage: scripts/route_inventory.py [--backend ../health-app] [--out results/isolation/routes.json]
"""
import argparse
import json
import re
import sys
from pathlib import Path

MAPPING = re.compile(r'@(Get|Post|Put|Patch|Delete|Request)Mapping\b(\s*\()?')
CLASS_DECL = re.compile(r'\b(class|record|interface)\s+(\w+)')
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
PATH_VAR = re.compile(r'\{(\w+)(?::[^}]*)?\}')


def balanced(text, start):
    """Index just past the parenthesis group that opens at text[start] == '('."""
    depth, i, quoted = 0, start, False
    while i < len(text):
        c = text[i]
        if quoted:
            if c == '\\':
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError('unbalanced parentheses')


def split_args(args):
    parts, depth, current, quoted = [], 0, '', False
    for i, c in enumerate(args):
        if quoted:
            current += c
            if c == '"' and args[i - 1] != '\\':
                quoted = False
            continue
        if c == '"':
            quoted = True
        elif c in '({[':
            depth += 1
        elif c in ')}]':
            depth -= 1
        if c == ',' and depth == 0:
            parts.append(current.strip())
            current = ''
        else:
            current += c
    if current.strip():
        parts.append(current.strip())
    return parts


def annotation_paths(args, constants=None):
    """Paths named in a mapping annotation (value/path or the bare first argument); None when not a literal."""
    constants = constants or {}
    if args is None:
        return [''], None
    paths, methods, unresolved = [], None, False
    for part in split_args(args):
        key, _, value = part.partition('=') if re.match(r'^\w+\s*=', part) else ('', '', part)
        key = key.strip()
        if key in ('', 'value', 'path'):
            found = STRING.findall(value) or [constants[n] for n in re.findall(r'\b([A-Z][A-Z0-9_]*)\b', value) if n in constants]
            if found:
                paths.extend(found)
            elif value.strip():
                unresolved = True
        elif key == 'method':
            methods = re.findall(r'RequestMethod\.(\w+)', value)
    if unresolved and not paths:
        return None, methods
    return paths or [''], methods


def join(base, path):
    if not base:
        full = path
    elif not path:
        full = base
    else:
        full = base.rstrip('/') + '/' + path.lstrip('/')
    full = '/' + full.lstrip('/') if full else '/'
    return re.sub(r'/+', '/', full)


def handler_signature(text, start):
    """Text of the method declaration that follows an annotation: up to the opening brace of its body."""
    # Skip further annotations, then find the parameter list of the method itself.
    i = start
    while True:
        m = re.compile(r'\s*@\w+(\.\w+)*').match(text, i)
        if not m:
            break
        i = m.end()
        if i < len(text) and text[i] == '(':
            i = balanced(text, i)
    open_paren = text.find('(', i)
    close = balanced(text, open_paren)
    return text[i:close], text[open_paren + 1:close - 1]


KEEP = ('RequestParam', 'PathVariable', 'RequestBody', 'RequestHeader')


def strip_annotations(part):
    """Drops annotations other than the binding ones (e.g. @DateTimeFormat, @Valid), keeping their arguments out."""
    out, i = '', 0
    while i < len(part):
        m = re.compile(r'@([\w.]+)').match(part, i)
        if m:
            name = m.group(1).split('.')[-1]
            j = m.end()
            if j < len(part) and part[j] == '(':
                j = balanced(part, j)
            if name in KEEP:
                out += part[i:j]
            i = j
        else:
            out += part[i]
            i += 1
    return re.sub(r'\s+', ' ', out).strip()


def binding_name(args, fallback):
    for arg in split_args(args or ''):
        key, _, value = arg.partition('=') if re.match(r'^\w+\s*=', arg) else ('', '', arg)
        if key.strip() in ('', 'value', 'name'):
            found = STRING.findall(value)
            if found:
                return found[0]
    return fallback


def params_of(param_text):
    query, body, org_header, path_vars, headers = [], False, False, [], []
    for part in (strip_annotations(p) for p in split_args(param_text)):
        if '@RequestBody' in part:
            body = True
        if 'X-Organization-Id' in part:
            org_header = True
        if '@RequestHeader' in part and 'X-Organization-Id' not in part:
            headers.append(part)
        m = re.search(r'@RequestParam(\s*\(([^)]*)\))?\s+(?:final\s+)?[\w<>,.?\[\] ]+?\s+(\w+)$', part)
        if m:
            args = m.group(2) or ''
            required = not re.search(r'required\s*=\s*false', args) and 'defaultValue' not in args \
                and 'Optional<' not in part
            query.append({'name': binding_name(args, m.group(3)), 'required': required})
        m = re.search(r'@PathVariable(\s*\(([^)]*)\))?\s+(?:final\s+)?[\w<>,.?\[\] ]+?\s+(\w+)$', part)
        if m:
            named = [binding_name(m.group(2), m.group(3))]
            type_match = re.search(r'\)?\s*(?:final\s+)?([\w<>.]+)\s+\w+$', part)
            path_vars.append({'name': named[0] if named else m.group(3), 'type': type_match.group(1) if type_match else ''})
    return query, body, org_header, path_vars


def scan(file, rel):
    text = file.read_text(encoding='utf-8')
    if 'Mapping' not in text or ('@RestController' not in text and '@Controller' not in text):
        return [], []
    class_match = CLASS_DECL.search(text)
    constants = dict(re.findall(r'static\s+final\s+String\s+([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"', text))
    routes, problems = [], []
    # Class-level @RequestMapping: the one before the class declaration.
    class_paths = ['']
    head = text[:class_match.start()] if class_match else ''
    for m in MAPPING.finditer(head):
        if m.group(1) == 'Request':
            args = text[m.end() - 1:balanced(text, m.end() - 1)][1:-1] if m.group(2) else None
            paths, _ = annotation_paths(args, constants)
            if paths is None:
                problems.append(f'{rel}: class path is not a literal')
                paths = ['']
            class_paths = paths
    body_start = class_match.end() if class_match else 0
    for m in MAPPING.finditer(text, body_start):
        kind = m.group(1)
        if m.group(2):
            open_paren = m.end() - 1
            close = balanced(text, open_paren)
            args = text[open_paren + 1:close - 1]
        else:
            close = m.end()
            args = None
        paths, methods = annotation_paths(args, constants)
        line = text.count('\n', 0, m.start()) + 1
        if paths is None:
            problems.append(f'{rel}:{line}: path is not a literal')
            continue
        http_methods = [kind.upper()] if kind != 'Request' else (methods or ['ANY'])
        signature, param_text = handler_signature(text, close)
        name_match = re.search(r'(\w+)\s*\($', signature[:signature.find('(') + 1])
        query, body, org_header, path_vars = params_of(param_text)
        for base in class_paths:
            for path in paths:
                full = join(base, path)
                for method in http_methods:
                    routes.append({
                        'method': method,
                        'path': full,
                        'pathVars': [v['name'] for v in path_vars] or PATH_VAR.findall(full),
                        'pathVarTypes': {v['name']: v['type'] for v in path_vars},
                        'query': query,
                        'body': body,
                        'orgHeader': org_header,
                        'handler': f'{file.stem}.{name_match.group(1) if name_match else "?"}',
                        'source': f'{rel}:{line}',
                    })
    return routes, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    here = Path(__file__).resolve().parent.parent
    parser.add_argument('--backend', default=str(here.parent / 'health-app'))
    parser.add_argument('--out', default=str(here / 'results' / 'isolation' / 'routes.json'))
    args = parser.parse_args()
    source = Path(args.backend) / 'src' / 'main' / 'java'
    routes, problems = [], []
    for file in sorted(source.rglob('*.java')):
        found, issues = scan(file, file.relative_to(Path(args.backend)).as_posix())
        routes.extend(found)
        problems.extend(issues)
    routes.sort(key=lambda r: (r['path'], r['method']))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'routes': routes, 'problems': problems}, indent=1, ensure_ascii=False) + '\n')
    with_ids = [r for r in routes if r['pathVars']]
    print(f'{len(routes)} routes ({len(with_ids)} with path variables) -> {out}')
    for problem in problems:
        print('WARN', problem, file=sys.stderr)


if __name__ == '__main__':
    main()
