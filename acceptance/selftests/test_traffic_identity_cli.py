"""One installed-wheel smoke test of the acceptance identity command, built from the exact tree."""

import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "exact_tree_wheel", ROOT / "scripts/exact_tree_wheel.py"
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
exact_tree_wheel = _MODULE.exact_tree_wheel
TOKEN = "b" * 32
LABEL = re.compile(
    r"nci-sip-acceptance/[A-Za-z0-9][A-Za-z0-9.!+_-]* "
    rf"\(installation={TOKEN}; \+https://github\.com/CBIIT/nci-sip-mcp\)\n"
)
SENTINEL = (
    "import socket\n"
    "for attempt in (\n"
    "    lambda: socket.getaddrinfo('192.0.2.1', 9, flags=socket.AI_NUMERICHOST),\n"
    "    lambda: stream.connect('/nci-sip-identity-sentinel'),\n"
    "    lambda: stream.connect_ex('/nci-sip-identity-sentinel'),\n"
    "    lambda: stream.sendto(b'x', '/nci-sip-identity-sentinel'),\n"
    "):\n"
    "    stream = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)\n"
    "    try:\n"
    "        attempt()\n"
    "    except OSError:\n"
    "        pass\n"
    "    else:\n"
    "        raise AssertionError('not refused')\n"
    "    finally:\n"
    "        stream.close()\n"
)
MODULE = "nci_sip_acceptance.traffic_identity"
HOOK = "".join(
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
    "socket.socket.sendto = deny_sendto\n"
)


class AcceptanceIdentityWheelSmokeTest(unittest.TestCase):
    def test_installed_wheel_shows_and_generates_an_identity_without_network_use(self):
        """Only the patched resolver and socket methods are observed.

        Unpatched APIs such as gethostbyname, gethostbyaddr, getnameinfo, and sendmsg,
        native extensions, subprocess networking, and direct syscalls are outside this sentinel.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hooks").mkdir()
            (root / "hooks/sitecustomize.py").write_text(HOOK, encoding="utf-8")
            network_log = root / "network-attempts.txt"
            environment = {
                "HOME": str(root),
                "PYTHONPATH": str(root / "hooks"),
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "NCI_IDENTITY_NETWORK_LOG": str(network_log),
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INDEX": "1",
                "PATH": os.environ.get("PATH", ""),
            }
            wheel = exact_tree_wheel(ROOT, root / "wheelhouse", "acceptance")
            venv.EnvBuilder(with_pip=True, system_site_packages=False).create(root / "venv")
            python = str(root / "venv/bin/python")
            subprocess.run(  # noqa: S603 - owned wheel in an isolated temporary environment
                [python, "-m", "pip", "install", "--no-deps", "--no-cache-dir", str(wheel)],
                check=True,
                capture_output=True,
                env=environment,
            )

            def child(*arguments, **extra):
                return subprocess.run(  # noqa: S603 - fixed module in an owned isolated venv
                    [python, "-B", *arguments],
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=root,
                    env={**environment, **extra},
                )

            shown = child("-m", MODULE, "show", NCI_SIP_ACCEPTANCE_INSTALLATION_ID=TOKEN)
            generated = child("-m", MODULE, "generate")
            self.assertFalse(network_log.exists(), "the identity command used the network")
            sentinel = child("-c", SENTINEL)
            isolated = child(
                "-c",
                "import importlib.metadata, importlib.util\n"
                "assert importlib.util.find_spec('nci_sip_mcp') is None\n"
                "try:\n"
                "    importlib.metadata.distribution('nci-sip-mcp')\n"
                "except importlib.metadata.PackageNotFoundError:\n"
                "    pass\n"
                "else:\n"
                "    raise AssertionError('core distribution is installed')\n",
            )

            self.assertEqual((shown.returncode, shown.stderr), (0, ""))
            self.assertRegex(shown.stdout, LABEL)
            self.assertEqual((generated.returncode, generated.stderr), (0, ""))
            self.assertRegex(generated.stdout, r"[0-9a-f]{32}\n")
            self.assertEqual((isolated.returncode, isolated.stderr), (0, ""))
            self.assertEqual((sentinel.returncode, sentinel.stderr), (0, ""))
            self.assertEqual(
                network_log.read_text(encoding="utf-8"),
                "getaddrinfo\nconnect\nconnect_ex\nsendto\n",
            )
