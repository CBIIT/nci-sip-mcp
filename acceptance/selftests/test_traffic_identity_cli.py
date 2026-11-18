"""The independently installed acceptance identity command is local and fail-safe."""

import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

ACCEPTANCE = Path(__file__).resolve().parents[1]
TOKEN = "b" * 32
LABEL_SUFFIX = f" (installation={TOKEN}; +https://github.com/CBIIT/nci-sip-mcp)"
ID_ERROR = (
    "Set NCI_SI_ACCEPTANCE_INSTALLATION_ID to exactly 32 lowercase hexadecimal characters "
    "and retry."
)
VERSION_ERROR = (
    "The installed nci-si-acceptance version cannot form a safe label. "
    "Reinstall the acceptance package with valid metadata and retry."
)


class AcceptanceIdentityCLITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.root = Path(temporary.name)
        cls.wheelhouse = cls.root / "wheelhouse"
        cls.wheelhouse.mkdir()
        cls.home = cls.root / "home"
        cls.home.mkdir()
        cls.configuration = cls.root / "xdg"
        cls.configuration.mkdir()
        cls.working_directory = cls.root / "work"
        cls.working_directory.mkdir()
        cls.hooks = cls.root / "hooks"
        cls.hooks.mkdir()
        cls.network_log = cls.root / "network-attempts.txt"
        (cls.hooks / "sitecustomize.py").write_text(
            "import os\n"
            "import socket\n"
            "from pathlib import Path\n"
            "def deny(self, address):\n"
            "    with Path(os.environ['NCI_IDENTITY_NETWORK_LOG']).open('a') as log:\n"
            "        log.write('attempt\\n')\n"
            "    raise OSError('network denied')\n"
            "socket.socket.connect = deny\n"
            "socket.socket.connect_ex = deny\n",
            encoding="utf-8",
        )
        cls._build_and_install()
        cls.metadata = cls._metadata_path()
        cls.original_metadata = cls.metadata.read_text(encoding="utf-8")
        cls.original_version = next(
            line.removeprefix("Version: ")
            for line in cls.original_metadata.splitlines()
            if line.startswith("Version: ")
        )

    def setUp(self):
        self.network_log.unlink(missing_ok=True)
        self._write_version(self.original_version)
        self.addCleanup(self._restore_version)
        self.observed_paths = {
            path: self._tree(path)
            for path in (self.home, self.configuration, self.working_directory)
        }

    @classmethod
    def _environment(cls):
        return {
            "HOME": str(cls.home),
            "XDG_CONFIG_HOME": str(cls.configuration),
            "PYTHONPATH": str(cls.hooks),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NCI_IDENTITY_NETWORK_LOG": str(cls.network_log),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PATH": os.environ.get("PATH", ""),
        }

    @classmethod
    def _build_and_install(cls):
        result = subprocess.run(  # noqa: S603 - fixed acceptance path and offline wheel build
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--wheel-dir",
                str(cls.wheelhouse),
                str(ACCEPTANCE),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=cls._environment(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"offline acceptance wheel build failed: {result.stderr}")
        wheels = list(cls.wheelhouse.glob("nci_si_acceptance-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one acceptance wheel, found {len(wheels)}")
        cls.environment = cls.root / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(cls.environment)
        cls.python = cls.environment / "bin/python"
        installed = subprocess.run(  # noqa: S603 - owned wheel in isolated temporary environment
            [
                str(cls.python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                "--no-cache-dir",
                str(wheels[0]),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=cls._environment(),
        )
        if installed.returncode != 0:
            raise RuntimeError(f"offline acceptance wheel install failed: {installed.stderr}")
        if cls.network_log.exists():
            raise RuntimeError("network sentinel recorded an attempt during offline setup")

    @classmethod
    def _metadata_path(cls):
        probe = subprocess.run(  # noqa: S603 - fixed Python executable and metadata probe
            [
                str(cls.python),
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=cls._environment(),
        )
        if probe.returncode != 0:
            raise RuntimeError(f"could not locate isolated site-packages: {probe.stderr}")
        metadata = list(Path(probe.stdout.strip()).glob("nci_si_acceptance-*.dist-info/METADATA"))
        if len(metadata) != 1:
            raise RuntimeError(
                f"expected one installed acceptance metadata file, found {len(metadata)}"
            )
        return metadata[0]

    @classmethod
    def _write_version(cls, version):
        lines = cls.metadata.read_text(encoding="utf-8").splitlines()
        cls.metadata.write_text(
            "\n".join(
                f"Version: {version}" if line.startswith("Version: ") else line for line in lines
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _restore_version(cls):
        cls.metadata.write_text(cls.original_metadata, encoding="utf-8")

    @classmethod
    def _tree(cls, directory):
        return {
            str(path.relative_to(directory)): ("directory" if path.is_dir() else path.read_bytes())
            for path in sorted(directory.rglob("*"))
        }

    def _child(self, arguments, token=None):
        environment = self._environment()
        if token is not None:
            environment["NCI_SI_ACCEPTANCE_INSTALLATION_ID"] = token
        result = subprocess.run(  # noqa: S603 - fixed module and owned isolated wheel
            [str(self.python), "-B", *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.working_directory,
            env=environment,
        )
        self.assertFalse(
            self.network_log.exists() and self.network_log.read_text(encoding="utf-8"),
            "acceptance identity child attempted a network connection",
        )
        return result

    def _module(self, *arguments, token=None):
        return self._child(["-m", "nci_si_acceptance.traffic_identity", *arguments], token)

    def _installed_version(self):
        result = self._child(
            [
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('nci-si-acceptance'))",
            ]
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout.rstrip("\n")

    def _assert_no_configuration_writes(self):
        self.assertEqual(
            self.observed_paths,
            {
                path: self._tree(path)
                for path in (self.home, self.configuration, self.working_directory)
            },
        )

    def test_generate_prints_a_uuid4_without_provisioning_or_writing_files(self):
        generated = self._module("generate")

        self.assertRegex(generated.stdout, r"^[0-9a-f]{32}\n$")
        self.assertEqual((generated.returncode, generated.stderr), (0, ""))
        missing = self._module("show")
        self.assertEqual(
            (missing.returncode, missing.stdout, missing.stderr), (1, "", ID_ERROR + "\n")
        )
        self._assert_no_configuration_writes()

    def test_show_uses_acceptance_metadata_and_rejects_ids_without_echoing_them(self):
        version = self._installed_version()
        valid = self._module("show", token=TOKEN)

        self.assertEqual(valid.stdout, f"nci-si-acceptance/{version}{LABEL_SUFFIX}\n")
        self.assertEqual((valid.returncode, valid.stderr), (0, ""))
        for token in ("PRIVATE-ID-CANARY", "A" * 32, "a" * 31, "a" * 33, "g" * 32, "é" * 32):
            with self.subTest(token=token):
                refused = self._module("show", token=token)
                self.assertEqual(
                    (refused.returncode, refused.stdout, refused.stderr),
                    (1, "", ID_ERROR + "\n"),
                )
                self.assertNotIn(token, refused.stdout + refused.stderr)
        self._assert_no_configuration_writes()

    def test_installed_version_is_safe_and_the_complete_label_is_capped_at_256_bytes(self):
        version = self._installed_version()
        valid = self._module("show", token=TOKEN)
        self.assertEqual(valid.stdout, f"nci-si-acceptance/{version}{LABEL_SUFFIX}\n")

        unsafe_versions = (
            "bad version",
            "bad); installation=other",
            "bad\tversion",
            "bad-é",
            "bad\x01version",
        )
        for value in unsafe_versions:
            with self.subTest(version=value):
                self._write_version(value)
                refused = self._module("show", token=TOKEN)
                self.assertEqual(
                    (refused.returncode, refused.stdout, refused.stderr),
                    (1, "", VERSION_ERROR + "\n"),
                )
                self.assertNotIn(value, refused.stdout + refused.stderr)

        fixed_length = len("nci-si-acceptance/") + len(LABEL_SUFFIX)
        self._write_version("v" * (256 - fixed_length))
        actual = self._installed_version()
        accepted = self._module("show", token=TOKEN)
        label = f"nci-si-acceptance/{actual}{LABEL_SUFFIX}"
        self.assertEqual(
            (accepted.returncode, accepted.stdout, accepted.stderr), (0, label + "\n", "")
        )
        self.assertEqual(len(label.encode("ascii")), 256)

        too_long = "v" * (257 - fixed_length)
        self._write_version(too_long)
        refused = self._module("show", token=TOKEN)
        self.assertEqual(
            (refused.returncode, refused.stdout, refused.stderr),
            (1, "", VERSION_ERROR + "\n"),
        )
        self.assertNotIn(too_long, refused.stdout + refused.stderr)
        self._assert_no_configuration_writes()

    def test_child_has_no_core_package_and_network_sentinel_records_caught_attempts(self):
        isolated = self._child(
            [
                "-c",
                "import importlib.metadata, importlib.util\n"
                "assert importlib.util.find_spec('nci_si_mcp') is None\n"
                "try:\n"
                "    importlib.metadata.distribution('nci-si-mcp')\n"
                "except importlib.metadata.PackageNotFoundError:\n"
                "    pass\n"
                "else:\n"
                "    raise AssertionError('core distribution is installed')\n",
            ]
        )
        self.assertEqual((isolated.returncode, isolated.stderr), (0, ""))

        controlled = subprocess.run(  # noqa: S603 - controlled socket attempt intercepted by sentinel
            [
                str(self.python),
                "-B",
                "-c",
                "import socket\n"
                "try:\n"
                "    socket.create_connection(('192.0.2.1', 9), timeout=0.1)\n"
                "except OSError:\n"
                "    pass\n",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.working_directory,
            env=self._environment(),
        )

        self.assertEqual((controlled.returncode, controlled.stderr), (0, ""))
        self.assertEqual(self.network_log.read_text(encoding="utf-8"), "attempt\n")
