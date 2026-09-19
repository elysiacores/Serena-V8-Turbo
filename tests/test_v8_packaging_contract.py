import tomllib
import unittest
from pathlib import Path

import serena
import serena_v8
from serena_v8._version import VERSION


ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_distribution_is_replacement_not_competing_owner(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["name"], "serena-agent")
        self.assertIn("version", data["project"]["dynamic"])
        self.assertEqual(data["tool"]["hatch"]["version"]["path"], "src/serena_v8/_version.py")

    def test_runtime_versions_share_one_source_of_truth(self):
        from serena_v8 import core
        from serena import v8_runtime

        self.assertEqual(serena.__version__, VERSION)
        self.assertEqual(serena_v8.__version__, VERSION)
        self.assertEqual(core.__version__, VERSION)
        self.assertEqual(v8_runtime.__version__, VERSION)

    def test_doctor_entrypoint_is_packaged(self):
        data = tomllib.loads((ROOT / "pyproject.toml").read_text())
        scripts = data["project"]["scripts"]
        self.assertEqual(scripts["serena-v8-doctor"], "serena_v8.install_doctor:main")

    def test_install_docs_do_not_instruct_site_packages_overlay(self):
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn('cp -r src/serena/*', readme)
        self.assertNotIn('pip install git+https://github.com/elysiacores/serena-v8.git', readme)


if __name__ == "__main__":
    unittest.main()
