import ast
import re
import shlex
import unittest
from pathlib import Path, PurePath

from tests.subprocess_helpers import CHILD_HANG_TIMEOUT

ARETE_IMPORT = re.compile(r"\b(?:from\s+arete\b|import\s+arete\b|(?:import_module|__import__)\(['\"]arete\b)")
GUARD = "CHILD_HANG_TIMEOUT"


def argument(call, name):
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def uses_guard(timeout):
    if isinstance(timeout, ast.Name):
        return timeout.id == GUARD
    return (isinstance(timeout, ast.Call) and ast.unparse(timeout.func) == "max" and len(timeout.args) > 1
            and any(isinstance(value, ast.Name) and value.id == GUARD for value in timeout.args))


def imports_arete(source):
    try:
        code = ast.parse(source)
    except SyntaxError:
        return bool(ARETE_IMPORT.search(source))
    return bool(ARETE_IMPORT.search(source)) or any(
        isinstance(node, ast.Import) and any(name.name.split(".")[0] == "arete" for name in node.names)
        for node in ast.walk(code)
    )


def guarded_command(executable, args):
    if PurePath(executable).name == "env" and args:
        executable, *args = args
    if PurePath(executable).name in {"git", "arete", "arete-cli"}:
        return True
    for index, arg in enumerate(args[:-1]):
        payload = args[index + 1]
        if arg == "-c" and imports_arete(payload):
            return True
        if arg == "-m" and (payload == "tests.parallel" or payload.split(".")[0] == "arete"):
            return True
    return "serve" in args and any(PurePath(arg).name == "arete-cli" for arg in args)


class SubprocessAudit(ast.NodeVisitor):
    def __init__(self, filename=None):
        self.filename = filename
        self.scope = None
        self.bindings = [{}]
        self.violations = []
        self.expects_timeout = False

    def visit_scope(self, node):
        previous = self.scope
        self.scope = node.name
        self.bindings.append({})
        self.generic_visit(node)
        self.bindings.pop()
        self.scope = previous

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = visit_scope

    def visit_Assign(self, node):
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.bindings[-1][target.id] = node.value

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name == "subprocess" and alias.asname:
                self.bindings[-1][alias.asname] = ast.Name(id="subprocess")

    def visit_ImportFrom(self, node):
        if node.module == "subprocess":
            for alias in node.names:
                self.bindings[-1][alias.asname or alias.name] = ast.Attribute(
                    value=ast.Name(id="subprocess"), attr=alias.name)

    def resolve(self, node, seen=()):
        if isinstance(node, ast.Name) and node.id not in seen:
            for scope in reversed(self.bindings):
                if node.id in scope:
                    return self.resolve(scope[node.id], (*seen, node.id))
        return node

    def subprocess_member(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Attribute):
            owner = self.resolve(node.value)
            if isinstance(owner, ast.Name) and owner.id == "subprocess":
                return node.attr
        return None

    def text(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "str" and node.args:
            return self.text(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return str(PurePath(self.text(node.left)) / self.text(node.right))
        return "".join(part.value for part in ast.walk(node)
                       if isinstance(part, ast.Constant) and isinstance(part.value, str))

    def command_needs_guard(self, node):
        node = self.resolve(node)
        if isinstance(node, ast.Subscript):
            return self.text(node.slice) == "ProgramArguments"
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            executable, *args = node.elts
            return guarded_command(self.text(executable), [self.text(arg) for arg in args])
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                words = shlex.split(node.value)
            except ValueError:
                return True
            return bool(words and guarded_command(words[0], words[1:]))
        # A fixture can take a command from its caller. Require the guard when its origin is unknown.
        return True

    def visit_With(self, node):
        previous = self.expects_timeout
        self.expects_timeout |= any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr in {"assertRaises", "assertRaisesRegex"}
            and item.context_expr.args
            and (self.subprocess_member(item.context_expr.args[0]) == "TimeoutExpired"
                 or ast.unparse(self.resolve(item.context_expr.args[0])) == "TimeoutExpired")
            for item in node.items
        )
        self.generic_visit(node)
        self.expects_timeout = previous

    def visit_Call(self, node):
        method = self.resolve(node.func)
        if isinstance(method, ast.Attribute) and method.attr == "communicate":
            unbound = self.subprocess_member(method.value) == "Popen"
            process = self.resolve(node.args[0] if unbound and node.args else method.value)
            timeout = argument(node, "timeout")
            if timeout is None and len(node.args) > 1 + unbound:
                timeout = node.args[1 + unbound]
            needs_guard = self.process_needs_guard(process, unbound)
        elif self.subprocess_member(method) in {"run", "check_output"}:
            command = node.args[0] if node.args else argument(node, "args")
            timeout = argument(node, "timeout")
            needs_guard = self.command_needs_guard(command)
        else:
            timeout = None
            needs_guard = False
        if needs_guard and not self.expects_timeout and not uses_guard(timeout):
            self.violations.append(node.lineno)
        self.generic_visit(node)

    def process_needs_guard(self, process, unbound):
        if isinstance(process, ast.Call) and self.subprocess_member(process.func) == "Popen":
            return self.command_needs_guard(process.args[0] if process.args else argument(process, "args"))
        # These mocked callbacks force a Git timeout to exercise deploy recovery.
        return not (unbound and self.filename == "test_workflow.py"
                    and self.scope in {"expire_after_writes", "expire_while_git_holds_lock"})


class SubprocessHangGuardTests(unittest.TestCase):
    def test_arete_git_and_serve_children_use_shared_hang_guard(self):
        self.assertEqual(CHILD_HANG_TIMEOUT, 30)
        root = Path(__file__).resolve().parent
        violations = []
        for path in sorted(root.rglob("*.py")):
            audit = SubprocessAudit(path.name)
            audit.visit(ast.parse(path.read_bytes(), filename=str(path)))
            violations.extend(f"{path.relative_to(root)}:{line}" for line in audit.violations)
        self.assertFalse(violations, f"Use {GUARD} for these subprocess calls:\n" + "\n".join(violations))
