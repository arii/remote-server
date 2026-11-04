"""td ux scripts contract guards (#880).

`td ux audit` shells (cwd=checkout) to scripts/ux-discover-routes.ts
then scripts/ux-audit-runner.ts, which read
scripts/config/ux-audit.config.json; impact-pass (#869) captures the
console through that chain, so a stock checkout must carry all of
it plus the node deps `pnpm exec tsx` resolves. These tests fail CI
if the contract drifts: missing scripts, a config whose viewport
names no longer match td's --desktop/--mobile mapping, missing
root devDeps, or Dockerfile/.dockerignore dropping the deploy half.
"""
import json
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")

REQUIRED_SCRIPTS = (
    "scripts/ux-discover-routes.ts",
    "scripts/ux-audit-runner.ts",
    "scripts/ux-lighthouse-runner.ts",
    "scripts/ux-config.ts",
    "scripts/config/ux-audit.config.json",
)

# td's --desktop/--mobile viewport mapping
# (dev_tools/orchestrator.py run_ux_audit): the runner filters config
# viewports by these exact names, so the config must carry them.
TD_DESKTOP_VIEWPORTS = ("desktop-1280", "desktop-1440")
TD_MOBILE_VIEWPORTS = ("mobile-375", "mobile-390", "mobile-430")

REQUIRED_DEV_DEPS = ("tsx", "playwright", "@axe-core/playwright",
                      "@types/node")


def _root(*parts):
    return os.path.join(ROOT, *parts)


class UxScriptsPresentTest(unittest.TestCase):
    def test_required_scripts_exist(self):
        for rel in REQUIRED_SCRIPTS:
            with self.subTest(script=rel):
                path = _root(*rel.split("/"))
                self.assertTrue(os.path.isfile(path), f"missing {rel}")
                self.assertGreater(
                    os.path.getsize(path), 0, f"empty {rel}")

    def test_scripts_reference_ux_audit_layout(self):
        # impact.py reads artifacts/ux-audit/{routes.json,results/,
        # screenshots/,lighthouse/}: a rewritten script must keep
        # writing that layout.
        layout = {
            "scripts/ux-discover-routes.ts": "routes.json",
            "scripts/ux-audit-runner.ts": "ux-audit",
            "scripts/ux-lighthouse-runner.ts": "lighthouse",
        }
        for rel, needle in layout.items():
            with self.subTest(script=rel):
                with open(_root(*rel.split("/")), encoding="utf-8") as f:
                    self.assertIn(needle, f.read())


class UxConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_root("scripts", "config", "ux-audit.config.json"),
                  encoding="utf-8") as f:
            cls.config = json.load(f)

    def test_base_url_http(self):
        base = self.config.get("baseUrl", "")
        self.assertTrue(base.startswith(("http://", "https://")),
                        f"baseUrl must be http(s): {base!r}")

    def test_viewport_names_match_td_mapping(self):
        vps = self.config.get("viewports", {})
        desktop = tuple(v.get("name")
                        for v in vps.get("desktop", []))
        mobile = tuple(v.get("name")
                       for v in vps.get("mobile", []))
        for name in TD_DESKTOP_VIEWPORTS:
            self.assertIn(name, desktop)
        for name in TD_MOBILE_VIEWPORTS:
            self.assertIn(name, mobile)

    def test_viewport_dimensions_sane(self):
        vps = self.config.get("viewports", {})
        for group in ("desktop", "mobile"):
            for vp in vps.get(group, []):
                with self.subTest(viewport=vp.get("name")):
                    self.assertGreater(vp.get("width", 0), 0)
                    self.assertGreater(vp.get("height", 0), 0)


class UxToolchainTest(unittest.TestCase):
    def test_root_dev_deps_declared(self):
        with open(_root("package.json"), encoding="utf-8") as f:
            dev = json.load(f).get("devDependencies", {})
        for dep in REQUIRED_DEV_DEPS:
            with self.subTest(dep=dep):
                self.assertIn(dep, dev)

    def test_lockfile_tracks_deps(self):
        with open(_root("pnpm-lock.yaml"), encoding="utf-8") as f:
            lock = f.read()
        for dep in REQUIRED_DEV_DEPS:
            with self.subTest(dep=dep):
                self.assertIn(dep, lock)


class UxDeployWiringTest(unittest.TestCase):
    def test_dockerfile_copies_scripts(self):
        with open(_root("Dockerfile"), encoding="utf-8") as f:
            copies = [ln for ln in f.read().splitlines()
                      if ln.startswith("COPY ")]
        for rel in REQUIRED_SCRIPTS:
            with self.subTest(script=rel):
                self.assertTrue(
                    any(rel in ln for ln in copies),
                    f"Dockerfile COPYs nothing for {rel}")

    def test_dockerignore_keeps_scripts(self):
        with open(_root(".dockerignore"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn("!scripts/", lines)
        self.assertIn("!scripts/**", lines)


if __name__ == "__main__":
    unittest.main()
