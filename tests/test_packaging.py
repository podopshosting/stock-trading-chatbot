"""
Deployment packaging regression tests.

The outage class these guard against: a deployment path that zips only
handler.py, so `from ml_agent_lite import ...` fails at cold start with
Runtime.ImportModuleError and every request 502s.

These are static checks on the deployment configuration, so they run fast
and offline. The runtime guarantee is enforced by
scripts/build_lambda_package.sh, which refuses to emit a package that is
missing a required module or that does not import.
"""
import ast
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTER_DIR = os.path.join(REPO_ROOT, "lambda-micro", "chatbot-router")
BUILD_SCRIPT = os.path.join(REPO_ROOT, "scripts", "build_lambda_package.sh")
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "deploy.yml")
DEPLOY_SCRIPT = os.path.join(REPO_ROOT, "scripts", "deploy_microservices.sh")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def local_imports(py_path, service_dir):
    """Top-level imports that resolve to a sibling .py file in the service."""
    tree = ast.parse(read(py_path))
    found = set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        for name in names:
            if os.path.exists(os.path.join(service_dir, f"{name}.py")):
                found.add(f"{name}.py")
    return found


class TestChatbotPackaging(unittest.TestCase):
    def test_handler_local_imports_are_declared_required(self):
        """Every sibling module handler.py imports must be in the build
        script's required list for this service."""
        needed = local_imports(os.path.join(ROUTER_DIR, "handler.py"), ROUTER_DIR)
        self.assertIn(
            "ml_agent_lite.py", needed,
            msg="expected handler.py to import ml_agent_lite",
        )

        script = read(BUILD_SCRIPT)
        match = re.search(r"\*chatbot-router\)\s*REQUIRED_MODULES=\(([^)]*)\)", script)
        self.assertIsNotNone(
            match, "chatbot-router entry missing from build script manifest"
        )
        declared = set(re.findall(r'"([^"]+)"', match.group(1)))
        declared.discard("$ENTRY_MODULE")
        declared.add("handler.py")  # $ENTRY_MODULE

        missing = needed - declared
        self.assertFalse(
            missing,
            msg=f"modules imported but not declared for packaging: {sorted(missing)}",
        )

    def test_build_script_verifies_contents_and_import(self):
        script = read(BUILD_SCRIPT)
        self.assertIn("refusing to deploy", script,
                      "build script must refuse to emit a bad artifact")
        self.assertIn("importlib.import_module", script,
                      "build script must verify the artifact imports")
        self.assertIn("mktemp -d", script,
                      "build script must build in a clean temp dir")

    def test_all_deploy_paths_use_the_shared_builder(self):
        """Both deployment paths must build via the same script, or they can
        drift apart again."""
        for path in (WORKFLOW, DEPLOY_SCRIPT):
            with self.subTest(path=os.path.basename(path)):
                self.assertIn(
                    "build_lambda_package.sh", read(path),
                    msg=f"{os.path.basename(path)} must use the shared builder",
                )

    def test_no_deploy_path_zips_handler_alone(self):
        """The original defect, in regex form: packaging handler.py by itself."""
        # handler.py zipped with no other .py module after it. Tolerates
        # trailing redirections, which an anchored pattern would let through.
        bad = re.compile(r"zip\s+(?:-\w+\s+)*\S*\.zip\s+handler\.py(?!\s+\S*\.py)", re.M)
        for path in (WORKFLOW, DEPLOY_SCRIPT):
            with self.subTest(path=os.path.basename(path)):
                self.assertIsNone(
                    bad.search(read(path)),
                    msg=f"{os.path.basename(path)} zips handler.py without its modules",
                )

    def test_backup_handlers_are_excluded_from_packages(self):
        script = read(BUILD_SCRIPT)
        self.assertIn("handler-*.py", script,
                      "superseded handlers must not ship")


if __name__ == "__main__":
    unittest.main(verbosity=2)
