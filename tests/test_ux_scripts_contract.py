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
import re
import unittest

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
    return os.path.join(*parts)


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


class UxImageToolchainTest(unittest.TestCase):
    """The serving image carries the capture toolchain (#881).

    Stub-free recipe assertions over the real Dockerfile: node 22+
    (@axe-core/playwright needs >= 22.12), one shared pnpm pin, the
    root-only pnpm install, baked chromium, and the --check smoke
    that fails the build (not a scheduled fire) on regression.
    """

    @classmethod
    def setUpClass(cls):
        with open(_root("Dockerfile"), encoding="utf-8") as f:
            cls.dockerfile = f.read()
        with open(_root("package.json"), encoding="utf-8") as f:
            cls.root_name = json.load(f)["name"]

    def test_node_major_meets_axe_floor(self):
        versions = re.findall(r"^ARG NODE_VERSION=(\d+)\.",
                              self.dockerfile, re.M)
        self.assertTrue(versions, "Dockerfile must pin NODE_VERSION")
        for major in versions:
            self.assertGreaterEqual(int(major), 22)

    def test_no_apt_nodejs(self):
        # The node tarball replaces Debian's node 18; both installed
        # would leave PATH order deciding the version.
        for line in self.dockerfile.splitlines():
            if "apt-get install" in line:
                self.assertNotIn("nodejs", line.split())

    def test_single_shared_pnpm_pin(self):
        pins = re.findall(r"^ARG PNPM_VERSION=(\S+)",
                          self.dockerfile, re.M)
        self.assertEqual(len(pins), 1,
                         "one global PNPM_VERSION pin")
        self.assertIn("pnpm@${PNPM_VERSION}", self.dockerfile)
        self.assertNotIn("pnpm@10.", self.dockerfile.replace(
            f"PNPM_VERSION={pins[0]}", ""))

    def test_root_only_frozen_install(self):
        self.assertIn(
            f"pnpm install --frozen-lockfile --filter {self.root_name}",
            self.dockerfile)

    def test_chromium_baked(self):
        self.assertIn("playwright install", self.dockerfile)
        self.assertIn("chromium", self.dockerfile)

    def test_check_smoke_runs_in_build(self):
        runs = [ln for ln in self.dockerfile.splitlines()
                if ln.startswith("RUN ")]
        self.assertTrue(
            any("impact_pass.py --check" in ln for ln in runs),
            "a build RUN must execute the toolchain preflight")


if __name__ == "__main__":
    unittest.main()
