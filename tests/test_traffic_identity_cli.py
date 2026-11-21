"""The installed core identity command reports local configuration without side effects."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 32
LABEL_SUFFIX = f" (installation={TOKEN}; +https://github.com/CBIIT/nci-sip-mcp)"
VERSION_ERROR = (
    "The installed nci-sip-mcp version cannot form a safe label. "
    "Reinstall the package with valid metadata and retry."
)


class CoreIdentityCLITest(unittest.TestCase):
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
            "def record(kind):\n"
            "    with Path(os.environ['NCI_IDENTITY_NETWORK_LOG']).open('a') as log:\n"
            "        log.write(kind + '\\n')\n"
            "def deny_connect(self, address):\n"
            "    record('connect')\n"
            "    raise OSError('network denied')\n"
            "def deny_connect_ex(self, address):\n"
            "    record('connect_ex')\n"
            "    raise OSError('network denied')\n"
            "def deny_getaddrinfo(*args, **kwargs):\n"
            "    record('getaddrinfo')\n"
            "    raise OSError('network denied')\n"
            "def deny_sendto(self, *args, **kwargs):\n"
            "    record('sendto')\n"
            "    raise OSError('network denied')\n"
            "socket.socket.connect = deny_connect\n"
            "socket.socket.connect_ex = deny_connect_ex\n"
            "socket.getaddrinfo = deny_getaddrinfo\n"
            "socket.socket.sendto = deny_sendto\n",
            encoding="utf-8",
        )
        cls.python = Path(sys.executable)
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
        result = subprocess.run(  # noqa: S603 - fixed local project path and offline wheel build
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
                str(ROOT),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=cls._environment(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"offline core wheel build failed: {result.stderr}")
        wheels = list(cls.wheelhouse.glob("nci_sip_mcp-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one core wheel, found {len(wheels)}")
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
            raise RuntimeError(f"offline core wheel install failed: {installed.stderr}")
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
        metadata = list(Path(probe.stdout.strip()).glob("nci_sip_mcp-*.dist-info/METADATA"))
        if len(metadata) != 1:
            raise RuntimeError(f"expected one installed core metadata file, found {len(metadata)}")
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
    def _write_invalid_utf8_version(cls):
        metadata = cls.metadata.read_bytes()
        version = f"Version: {cls.original_version}".encode()
        if metadata.count(version) != 1:
            raise RuntimeError("expected one installed core version")
        cls.metadata.write_bytes(metadata.replace(version, b"Version: private-\xff-canary", 1))

    @classmethod
    def _remove_version(cls):
        lines = cls.metadata.read_text(encoding="utf-8").splitlines()
        cls.metadata.write_text(
            "\n".join(line for line in lines if not line.startswith("Version: ")) + "\n",
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

    def _child(self, arguments, token=None, configured_id=None):
        environment = self._environment()
        if token is not None:
            environment["NCI_SIP_INSTALLATION_ID"] = token
        if configured_id is not None:
            environment["NCI_SIP_INSTALLATION_ID"] = configured_id
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
            "identity child used a patched Python socket API for network access",
        )
        return result

    def _module(self, *arguments, token=None, configured_id=None):
        return self._child(
            ["-m", "nci_sip_mcp.traffic_identity", *arguments],
            token,
            configured_id,
        )

    def _installed_version(self):
        result = self._child(
            [
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('nci-sip-mcp'))",
            ]
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout.rstrip("\n")

    def _error(self, result, parameter, reason, message=None):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)["error"]
        self.assertEqual(record["code"], "invalid_request")
        self.assertEqual(record["details"], {"parameter": parameter, "reason": reason})
        self.assertRegex(record["correlationId"], r"^[0-9a-f]{32}$")
        if message is not None:
            self.assertEqual(record["message"], message)
        self.assertEqual(
            result.stdout, json.dumps(json.loads(result.stdout), indent=2, sort_keys=True) + "\n"
        )
        return record

    def _assert_no_configuration_writes(self):
        self.assertEqual(
            self.observed_paths,
            {
                path: self._tree(path)
                for path in (self.home, self.configuration, self.working_directory)
            },
        )

    def test_package_import_without_an_installed_distribution_fails(self):
        environment = self._environment()
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import nci_sip_mcp"],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.working_directory,
            env=environment,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PackageNotFoundError", result.stderr)

    def test_generate_prints_a_uuid4_without_provisioning_or_writing_files(self):
        for configured_id in (None, "PRIVATE-ID-CANARY"):
            with self.subTest(configured_id=configured_id):
                generated = self._module("generate", configured_id=configured_id)

                self.assertRegex(generated.stdout, r"^[0-9a-f]{32}\n$")
                self.assertEqual((generated.returncode, generated.stderr), (0, ""))
                parsed = UUID(generated.stdout.strip())
                self.assertEqual(parsed.version, 4)
                self.assertNotIn("PRIVATE-ID-CANARY", generated.stdout + generated.stderr)
        missing = self._module("show")
        error = self._error(
            missing,
            "NCI_SIP_INSTALLATION_ID",
            "is missing",
        )
        self.assertIn("NCI_SIP_INSTALLATION_ID", error["message"])
        self._assert_no_configuration_writes()

    def test_generate_does_not_read_malformed_installed_version(self):
        self._write_invalid_utf8_version()

        generated = self._module("generate")

        self.assertRegex(generated.stdout, r"^[0-9a-f]{32}\n$")
        self.assertEqual((generated.returncode, generated.stderr), (0, ""))
        self.assertEqual(UUID(generated.stdout.strip()).version, 4)
        self.assertNotIn("private-", generated.stdout + generated.stderr)
        self._assert_no_configuration_writes()

    def test_show_uses_the_installed_version_and_configured_token_exactly(self):
        version = self._installed_version()
        shown = self._module("show", token=TOKEN)

        self.assertEqual(shown.stdout, f"nci-sip-mcp/{version}{LABEL_SUFFIX}\n")
        self.assertEqual(
            (shown.returncode, shown.stdout, shown.stderr),
            (0, f"nci-sip-mcp/{version}{LABEL_SUFFIX}\n", ""),
        )
        self._assert_no_configuration_writes()

    def test_show_rejects_installed_metadata_without_a_version(self):
        self._remove_version()
        self.assertFalse(
            any(line.startswith("Version: ") for line in self.metadata.read_text().splitlines())
        )

        refused = self._module("show", token=TOKEN)

        self._error(
            refused,
            "nci-sip-mcp installed version",
            "must be a safe printable ASCII version and keep the complete label within 256 bytes",
            VERSION_ERROR,
        )
        self.assertNotIn("Traceback", refused.stdout + refused.stderr)

    def test_show_reports_a_fixed_error_for_undecodable_installed_metadata(self):
        self._write_invalid_utf8_version()

        refused = self._module("show", token=TOKEN)

        self._error(
            refused,
            "nci-sip-mcp installed version",
            "must be a safe printable ASCII version and keep the complete label within 256 bytes",
            VERSION_ERROR,
        )
        self.assertNotIn("private-", refused.stdout + refused.stderr)
        self.assertNotIn("UnicodeDecodeError", refused.stdout + refused.stderr)

    def test_package_version_reload_tracks_valid_corrupt_and_repaired_metadata(self):
        metadata_path = json.dumps(str(self.metadata))
        code = (
            "import importlib, pathlib, nci_sip_mcp\n"
            f"path = pathlib.Path({metadata_path})\n"
            "initial = nci_sip_mcp.__version__\n"
            "initially_introspectable = '__version__' in dir(nci_sip_mcp)\n"
            "metadata = path.read_bytes()\n"
            "old = b'Version: ' + initial.encode()\n"
            "assert metadata.count(old) == 1\n"
            "metadata = metadata.replace(old, b'Version: snapshot-change', 1)\n"
            "path.write_bytes(metadata)\n"
            "snapshot = nci_sip_mcp.__version__\n"
            "metadata = path.read_bytes()\n"
            "bad_version = b'Version: private-' + bytes([255]) + b'-canary'\n"
            "metadata = metadata.replace(b'Version: snapshot-change', bad_version, 1)\n"
            "path.write_bytes(metadata)\n"
            "try:\n"
            "    importlib.reload(nci_sip_mcp)\n"
            "except UnicodeDecodeError:\n"
            "    reload_error = 'UnicodeDecodeError'\n"
            "else:\n"
            "    reload_error = 'none'\n"
            "corrupt_introspectable = '__version__' in dir(nci_sip_mcp)\n"
            "try:\n"
            "    nci_sip_mcp.__version__\n"
            "except (AttributeError, UnicodeDecodeError):\n"
            "    explicit_failed = True\n"
            "else:\n"
            "    explicit_failed = False\n"
            "metadata = path.read_bytes()\n"
            "metadata = metadata.replace(bad_version, b'Version: 7.7.7', 1)\n"
            "path.write_bytes(metadata)\n"
            "importlib.reload(nci_sip_mcp)\n"
            "repaired_introspectable = '__version__' in dir(nci_sip_mcp)\n"
            "print(f'{initial}|{snapshot}|{initially_introspectable}|{reload_error}|"
            "{corrupt_introspectable}|{explicit_failed}|{nci_sip_mcp.__version__}|"
            "{repaired_introspectable}')\n"
        )
        result = self._child(["-c", code])

        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (
                0,
                f"{self.original_version}|{self.original_version}|True|"
                f"UnicodeDecodeError|False|True|7.7.7|True\n",
                "",
            ),
        )

    def test_corrupt_first_import_repair_restores_ordinary_version_lookup(self):
        self._write_invalid_utf8_version()
        metadata_path = json.dumps(str(self.metadata))
        code = (
            "import importlib, pathlib, nci_sip_mcp\n"
            "try:\n"
            "    nci_sip_mcp.__version__\n"
            "except (AttributeError, UnicodeDecodeError):\n"
            "    initial_access_failed = True\n"
            "else:\n"
            "    initial_access_failed = False\n"
            f"path = pathlib.Path({metadata_path})\n"
            "bad_version = b'Version: private-' + bytes([255]) + b'-canary'\n"
            "metadata = path.read_bytes()\n"
            "assert metadata.count(bad_version) == 1\n"
            "path.write_bytes(metadata.replace(bad_version, b'Version: 7.7.7', 1))\n"
            "importlib.reload(nci_sip_mcp)\n"
            "repaired_version = nci_sip_mcp.__version__\n"
            "del nci_sip_mcp.__version__\n"
            "print('|'.join((str(initial_access_failed), repaired_version, "
            "str(hasattr(nci_sip_mcp, '__version__')))))\n"
        )
        result = self._child(["-c", code])

        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (0, "True|7.7.7|False\n", ""),
        )

    def test_invalid_tokens_are_refused_without_echoing_them(self):
        version = self._installed_version()
        valid = self._module("show", token=TOKEN)
        self.assertEqual(valid.stdout, f"nci-sip-mcp/{version}{LABEL_SUFFIX}\n")
        for token in ("PRIVATE-ID-CANARY", "A" * 32, "a" * 31, "a" * 33, "g" * 32, "é" * 32):
            with self.subTest(token=token):
                result = self._module("show", token=token)
                error = self._error(
                    result,
                    "NCI_SIP_INSTALLATION_ID",
                    "must be exactly 32 lowercase hexadecimal characters",
                )
                self.assertIn("32 lowercase hexadecimal characters", error["message"])
                self.assertNotIn(token, result.stdout + result.stderr)
        self._assert_no_configuration_writes()

    def test_installed_versions_are_safe_and_labels_are_neither_truncated_nor_overlong(self):
        version = self._installed_version()
        valid = self._module("show", token=TOKEN)
        self.assertEqual(valid.stdout, f"nci-sip-mcp/{version}{LABEL_SUFFIX}\n")
        for version in (
            "bad version",
            "bad); installation=other",
            "bad\tversion",
            "bad-é",
            "bad\x01version",
        ):
            with self.subTest(version=version):
                self._write_version(version)
                refused = self._module("show", token=TOKEN)
                self._error(
                    refused,
                    "nci-sip-mcp installed version",
                    "must be a safe printable ASCII version and keep the complete label within "
                    "256 bytes",
                    VERSION_ERROR,
                )
                self.assertNotIn(version, refused.stdout + refused.stderr)

        fixed_length = len("nci-sip-mcp/") + len(LABEL_SUFFIX)
        self._write_version("v" * (256 - fixed_length))
        actual = self._installed_version()
        accepted = self._module("show", token=TOKEN)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(accepted.stdout, f"nci-sip-mcp/{actual}{LABEL_SUFFIX}\n")
        self.assertEqual(len(accepted.stdout.rstrip("\n").encode("ascii")), 256)

        self._write_version("v" * (257 - fixed_length))
        refused = self._module("show", token=TOKEN)
        self._error(
            refused,
            "nci-sip-mcp installed version",
            "must be a safe printable ASCII version and keep the complete label within 256 bytes",
            VERSION_ERROR,
        )
        self.assertNotIn("v" * (257 - fixed_length), refused.stdout + refused.stderr)
        self._assert_no_configuration_writes()

    def test_socket_api_sentinel_records_distinct_attempts_even_when_caught(self):
        """Only the patched resolver and socket methods are observed.

        Unpatched APIs such as gethostbyname, gethostbyaddr, getnameinfo, and sendmsg,
        native extensions, subprocess networking, and direct syscalls are outside this sentinel.
        """
        self.network_log.unlink(missing_ok=True)
        result = subprocess.run(  # noqa: S603 - controlled socket attempt intercepted by sentinel
            [
                str(self.python),
                "-B",
                "-c",
                "import os, socket\n"
                "missing_path = '/nci-sip-identity-sentinel-' + str(os.getpid())\n"
                "for kind, attempt in (\n"
                "    ('DNS', lambda: socket.getaddrinfo('192.0.2.1', 9, "
                "flags=socket.AI_NUMERICHOST)),\n"
                "    ('connect', lambda: stream.connect(missing_path)),\n"
                "    ('connect_ex', lambda: stream.connect_ex(missing_path)),\n"
                "    ('sendto', lambda: datagram.sendto(b'controlled', missing_path)),\n"
                "):\n"
                "    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                "    datagram = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)\n"
                "    try:\n"
                "        attempt()\n"
                "    except OSError:\n"
                "        pass\n"
                "    else:\n"
                "        raise AssertionError(f'{kind} was not refused')\n"
                "    finally:\n"
                "        stream.close()\n"
                "        datagram.close()\n",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=self.working_directory,
            env=self._environment(),
        )

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(
            self.network_log.read_text(encoding="utf-8"),
            "getaddrinfo\nconnect\nconnect_ex\nsendto\n",
        )

    def test_help_and_usage_failures_do_not_evaluate_configuration(self):
        for configured_id in (None, "PRIVATE-ID-CANARY"):
            with self.subTest(configured_id=configured_id):
                help_result = self._module("--help", configured_id=configured_id)
                self.assertEqual((help_result.returncode, help_result.stderr), (0, ""))
                self.assertIn("generate", help_result.stdout)
                self.assertIn("show", help_result.stdout)
                self.assertNotIn("PRIVATE-ID-CANARY", help_result.stdout)

                invalid = self._module("not-a-command", configured_id=configured_id)
                self.assertEqual(invalid.returncode, 2)
                self.assertIn("usage:", invalid.stderr)
                self.assertEqual(invalid.stdout, "")
                self.assertNotIn("PRIVATE-ID-CANARY", invalid.stdout + invalid.stderr)
                self.assertNotIn("Traceback", invalid.stdout + invalid.stderr)
