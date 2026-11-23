"""Committed locks enforce rolling dependency ages and temporary security exceptions."""

import importlib.util
import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dependency_age.py"
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def checker():
    if not SCRIPT.is_file():
        raise AssertionError("scripts/dependency_age.py must provide the dependency-age gate")
    spec = importlib.util.spec_from_file_location("dependency_age", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("scripts/dependency_age.py must be loadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pypi(uploaded):
    return {"urls": [{"upload_time_iso_8601": uploaded}]}


def security_exception(module, version="2026.10.19", advisory="GHSA-6q48-6c7w-9gw2"):
    return {"certifi": module.SecurityException(version, advisory)}


def lock_fixture(root):
    contents = {
        "pdm.lock": '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n',
        "container/requirements-amd64.txt": "demo==1\n",
        "container/companion-requirements.txt": "demo==1\n",
        "container/pdm-bootstrap-requirements.txt": "demo==1\n",
        "docs/site-assets/package-lock.json": json.dumps(
            {
                "packages": {
                    "node_modules/tool": {
                        "version": "1",
                        "resolved": "https://registry.npmjs.org/tool/-/tool-1.tgz",
                    }
                }
            }
        ),
    }
    for name, text in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return contents


class DependencyAgeTest(unittest.TestCase):
    def test_direct_wheel_inventory_preserves_filename_url_and_hash(self):
        module = checker()
        wheels = [
            package
            for package in module.locked_packages()
            if isinstance(package, module.DirectWheel)
        ]
        self.assertEqual(
            wheels,
            [
                module.DirectWheel(
                    "torch-2.14.1+cpu-cp314-cp314-manylinux_2_28_x86_64.whl",
                    "35103e180214793207f95d7e12e100ecba3ace6f5fb37e1fcefbb07e096ed180",
                    "https://download.pytorch.org/whl/cpu/torch/",
                )
            ],
        )

    def test_direct_wheels_use_index_filename_hash_and_upload_time(self):
        module = checker()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_fixture(root)
            root.joinpath("container/requirements-amd64.txt").write_text(
                "demo==1\ntorch @ https://download.example/whl/cpu/torch-1%2Bcpu.whl \\\n"
                "    --hash=sha256:" + "a" * 64 + "\n"
            )
            (wheel,) = [
                package
                for package in module.locked_packages(root)
                if isinstance(package, module.DirectWheel)
            ]
            path = root / "container/requirements-amd64.txt"
            path.write_text("demo==1\ntorch @ https://download.example/torch.whl\n")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                status = module.check(root, {}, NOW, lambda _: pypi(NOW.isoformat()))
            self.assertEqual(status, 1)
            self.assertEqual(errors.getvalue(), f"{path}: torch direct URL needs a sha256 hash\n")
            self.assertEqual(output.getvalue(), "")
        label = "wheel:torch-1+cpu.whl"
        for uploaded, expected in (
            (NOW - timedelta(days=5), []),
            (
                NOW - timedelta(days=5) + timedelta(microseconds=1),
                [f"{label} was uploaded inside the five-day window"],
            ),
        ):
            with self.subTest(uploaded=uploaded):
                page = (
                    '<a href="https://cdn.example/torch-1%2Bcpu.whl#sha256='
                    + "a" * 64
                    + f'" data-upload-time="{uploaded.isoformat()}">wheel</a>'
                )
                self.assertEqual(
                    module.age_findings(
                        [wheel],
                        NOW,
                        {"https://download.example/whl/cpu/torch/": page}.__getitem__,
                        {"torch": module.SecurityException("torch-1+cpu.whl", "CVE-2026-12345")},
                    ),
                    expected,
                )

        cases = (
            ("", "has no matching index filename"),
            (
                '<a href="other.whl#sha256=' + "a" * 64 + '">wheel</a>',
                "has no matching index filename",
            ),
            (
                '<a href="torch-1%2Bcpu.whl#sha256=' + "b" * 64 + '">wheel</a>',
                "index sha256 differs from the lock",
            ),
            (
                '<a href="torch-1%2Bcpu.whl#sha256=' + "a" * 64 + '">wheel</a>',
                "has no distribution upload time",
            ),
        )
        for page, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    module.age_findings([wheel], NOW, lambda _, page=page: page),
                    [f"{label} {reason}"],
                )

        def missing(_url):
            raise OSError("not found")

        self.assertEqual(
            module.age_findings([wheel], NOW, missing),
            [f"{label} metadata could not be verified (OSError)"],
        )

    def test_five_day_boundary_is_inclusive(self):
        module = checker()
        packages = [module.Package("pypi", "old", "1"), module.Package("pypi", "new", "2")]
        metadata = {
            "https://pypi.org/pypi/old/1/json": pypi((NOW - timedelta(days=5)).isoformat()),
            "https://pypi.org/pypi/new/2/json": pypi(
                (NOW - timedelta(days=5) + timedelta(microseconds=1)).isoformat()
            ),
        }

        self.assertEqual(
            module.age_findings(packages, NOW, metadata.__getitem__),
            ["pypi:new==2 was uploaded inside the five-day window"],
        )

    def test_all_committed_lock_formats_are_read_and_duplicates_are_checked_once(self):
        module = checker()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pdm.lock").write_text(
                '[[package]]\nname = "Shared_Name"\nversion = "1.0"\n'
                '[[package]]\nname = "shared-name"\nversion = "1.0"\n'
                '[[package]]\nname = "local-project"\nversion = "1.0"\n'
                'editable = true\npath = "./local"\n',
                encoding="utf-8",
            )
            container = root / "container"
            container.mkdir()
            requirement = "other_pkg==2.0 \\\n+    --hash=sha256:a\n"
            for name in ("requirements-amd64.txt", "companion-requirements.txt"):
                (container / name).write_text(requirement, encoding="utf-8")
            (container / "pdm-bootstrap-requirements.txt").write_text(
                "bootstrap_pkg==3.0 \\\n    --hash=sha256:a\n", encoding="utf-8"
            )
            assets = root / "docs" / "site-assets"
            assets.mkdir(parents=True)
            (assets / "package-lock.json").write_text(
                json.dumps(
                    {
                        "packages": {
                            "": {"name": "root"},
                            "node_modules/a/node_modules/@scope/tool": {
                                "version": "3.0",
                                "resolved": "https://registry.npmjs.org/@scope/tool/-/tool-3.tgz",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                module.locked_packages(root),
                [
                    module.Package("npm", "@scope/tool", "3.0"),
                    module.Package("pypi", "bootstrap-pkg", "3.0"),
                    module.Package("pypi", "other-pkg", "2.0"),
                    module.Package("pypi", "shared-name", "1.0"),
                ],
            )

    def test_pypi_and_npm_use_their_registry_metadata(self):
        module = checker()
        packages = [
            module.Package("pypi", "demo", "1.0"),
            module.Package("npm", "@scope/tool", "2.0"),
        ]
        seen = []

        def fetch(url):
            seen.append(url)
            if urlsplit(url).hostname == "pypi.org":
                return pypi("2026-10-01T00:00:00Z")
            return {"time": {"2.0": "2026-10-01T00:00:00Z"}}

        self.assertEqual(module.age_findings(packages, NOW, fetch), [])
        self.assertEqual(
            seen,
            [
                "https://pypi.org/pypi/demo/1.0/json",
                "https://registry.npmjs.org/%40scope%2Ftool",
            ],
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_fixture(root)
            root.joinpath("docs/site-assets/package-lock.json").write_text(
                json.dumps(
                    {
                        "packages": {
                            "node_modules/alias": {
                                "name": "real-package",
                                "version": "2.0",
                                "resolved": "https://registry.npmjs.org/real-package/-/real-package-2.tgz",
                            }
                        }
                    }
                )
            )
            self.assertEqual(
                module.inventories(root)["docs/site-assets/package-lock.json"],
                [module.Package("npm", "real-package", "2.0")],
            )

    def test_unsupported_external_lock_entries_fail_instead_of_disappearing(self):
        module = checker()
        for filename, text, offending in (
            (
                "pdm.lock",
                '[[package]]\nname="external"\nversion="1"\nurl="https://example/wheel.whl"\n',
                "external",
            ),
            (
                "pdm.lock",
                '[[package]]\nname="external"\nversion="1"\ngit="https://example/repo"\n',
                "external",
            ),
            (
                "container/requirements-amd64.txt",
                "demo==1\nnew-package[extra]==2\n",
                "new-package[extra]==2",
            ),
            (
                "container/requirements-amd64.txt",
                "demo==1\n-e git+https://example/repo#egg=y\n",
                "-e git+https://example/repo#egg=y",
            ),
            ("container/requirements-amd64.txt", "demo==1\n-r other.txt\n", "-r other.txt"),
        ):
            with (
                self.subTest(filename=filename, offending=offending),
                TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                lock_fixture(root)
                path = root / filename
                path.write_text(text)
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    status = module.check(root, {}, NOW, lambda _: pypi("2026-10-01T00:00:00Z"))
                self.assertEqual(status, 1)
                self.assertEqual(
                    errors.getvalue(), f"{path}: unsupported dependency entry: {offending}\n"
                )
                self.assertEqual(output.getvalue(), "")

        for resolved in (
            None,
            "git+https://example/repo",
            "https://example/package.tgz",
            "http://registry.npmjs.org/package/-/package-1.tgz",
        ):
            with self.subTest(resolved=resolved), TemporaryDirectory() as directory:
                root = Path(directory)
                lock_fixture(root)
                path = root / "docs/site-assets/package-lock.json"
                row = {"version": "1"}
                if resolved is not None:
                    row["resolved"] = resolved
                path.write_text(json.dumps({"packages": {"node_modules/external": row}}))
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    status = module.check(
                        root, {}, NOW, lambda _: {"time": {"1": "2026-10-01T00:00:00Z"}}
                    )
                self.assertEqual(status, 1)
                self.assertEqual(
                    errors.getvalue(),
                    f"{path}: unsupported dependency entry: node_modules/external\n",
                )
                self.assertEqual(output.getvalue(), "")

    def test_unverifiable_release_fails_closed(self):
        module = checker()
        cases = (
            ({"urls": []}, "has no distribution upload time"),
            ({"urls": [{"upload_time_iso_8601": "bad"}]}, "has an invalid upload time"),
            ({"time": {}}, "has no release upload time"),
        )
        packages = (
            module.Package("pypi", "empty", "1"),
            module.Package("pypi", "bad", "1"),
            module.Package("npm", "missing", "1"),
        )
        for package, (metadata, reason) in zip(packages, cases, strict=True):
            with self.subTest(package=package.name):
                self.assertEqual(
                    module.age_findings([package], NOW, lambda _url, metadata=metadata: metadata),
                    [f"{package.ecosystem}:{package.name}==1 {reason}"],
                )

    def test_fetch_failure_is_a_finding_without_package_index_content(self):
        module = checker()

        def unavailable(_url):
            raise OSError("PRIVATE RESPONSE")

        self.assertEqual(
            module.age_findings([module.Package("pypi", "demo", "1")], NOW, unavailable),
            ["pypi:demo==1 metadata could not be verified (OSError)"],
        )

    def test_any_too_new_distribution_rejects_the_release(self):
        module = checker()
        metadata = pypi((NOW - timedelta(days=6)).isoformat())
        metadata["urls"].append({"upload_time_iso_8601": (NOW - timedelta(days=1)).isoformat()})

        self.assertEqual(
            module.age_findings([module.Package("pypi", "demo", "1")], NOW, lambda _: metadata),
            ["pypi:demo==1 was uploaded inside the five-day window"],
        )
        metadata["urls"].append({"upload_time_iso_8601": "invalid"})
        self.assertEqual(
            module.age_findings([module.Package("pypi", "demo", "1")], NOW, lambda _: metadata),
            ["pypi:demo==1 has an invalid upload time"],
        )

    def test_security_exception_is_exact_and_expires_itself(self):
        module = checker()
        package = module.Package("pypi", "certifi", "2026.10.19")
        recent = pypi((NOW - timedelta(days=1)).isoformat())
        old = pypi((NOW - timedelta(days=5)).isoformat())

        self.assertEqual(
            module.age_findings([package], NOW, lambda _: recent, security_exception(module)),
            [],
        )
        self.assertEqual(
            module.age_findings([package], NOW, lambda _: old, security_exception(module)),
            ["pypi:certifi==2026.10.19 is five days old; remove this exception"],
        )
        mixed = {"urls": old["urls"] + recent["urls"]}
        self.assertEqual(
            module.age_findings([package], NOW, lambda _: mixed, security_exception(module)), []
        )
        self.assertEqual(
            module.age_findings(
                [module.Package("pypi", "certifi", "2026.10.20")],
                NOW,
                lambda _: recent,
                security_exception(module),
            ),
            ["pypi:certifi==2026.10.20 was uploaded inside the five-day window"],
        )

    def test_exception_advisory_is_required_and_closed(self):
        module = checker()
        for records, message in (
            ([], "Dependency age exceptions must be a table"),
            ({"requests": "1.0"}, "Each dependency age exception must be a package table"),
            (
                {"demo": {"version": 1, "advisory": "CVE-2026-12345"}},
                "demo exception needs a version",
            ),
            (
                {"demo": {"version": "1", "advisory": "CVE-2024-123"}},
                "demo exception needs a valid advisory",
            ),
        ):
            with (
                self.subTest(records=records),
                self.assertRaisesRegex(ValueError, "^" + re.escape(message) + "$"),
            ):
                module.parse_exceptions(records)
        for record in (
            {"version": "1"},
            {"version": "1", "advisory": "issue-1"},
            {"version": "1", "advisory": "CVE-bad"},
        ):
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "advisory"):
                module.parse_exceptions({"demo": record})
        for advisory in ("GHSA-6q48-6c7w-9gw2", "CVE-2026-12345", "PYSEC-2026-12"):
            with self.subTest(advisory=advisory):
                self.assertEqual(
                    module.parse_exceptions({"demo": {"version": "1", "advisory": advisory}}),
                    {"demo": module.SecurityException("1", advisory)},
                )

    def test_malformed_and_naive_registry_metadata_fail_closed(self):
        module = checker()
        cases = (
            ([], "has malformed registry metadata"),
            ({"urls": "not-a-list"}, "has malformed registry metadata"),
            ({"urls": ["not-an-object"]}, "has malformed registry metadata"),
            (
                {"urls": [{"upload_time_iso_8601": "2026-10-01T00:00:00"}]},
                "has an invalid upload time",
            ),
            ({"time": []}, "has malformed registry metadata"),
        )
        packages = (
            module.Package("pypi", "bad-body", "1"),
            module.Package("pypi", "bad-list", "1"),
            module.Package("pypi", "bad-row", "1"),
            module.Package("pypi", "naive", "1"),
            module.Package("npm", "bad-time", "1"),
        )
        for package, (metadata, reason) in zip(packages, cases, strict=True):
            with self.subTest(package=package.name):
                self.assertEqual(
                    module.age_findings([package], NOW, lambda _, data=metadata: data),
                    [f"{package.ecosystem}:{package.name}==1 {reason}"],
                )

    def test_cli_prints_each_finding_and_fails(self):
        module = checker()
        output = io.StringIO()
        packages = [module.Package("pypi", "new", "1")]

        with redirect_stderr(output):
            status = module.run(packages, {}, NOW, lambda _: pypi(NOW.isoformat()))

        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "pypi:new==1 was uploaded inside the five-day window\n")

        for young in ("certifi", "server", "companion", "tool"):
            with self.subTest(young=young):
                self.assert_young_lock_fails(module, young)

    def assert_young_lock_fails(self, module, young):
        output, errors = io.StringIO(), io.StringIO()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_fixture(root)
            root.joinpath("container/requirements-amd64.txt").write_text("server==1\n")
            root.joinpath("container/companion-requirements.txt").write_text("companion==1\n")
            root.joinpath("container/pdm-bootstrap-requirements.txt").write_text("bootstrap==1\n")

            def fetch(url):
                uploaded = NOW.isoformat() if young in url else "2026-10-01T00:00:00Z"
                if urlsplit(url).hostname == "registry.npmjs.org":
                    return {"time": {"1": uploaded}}
                return pypi(uploaded)

            with redirect_stdout(output), redirect_stderr(errors):
                status = module.check(root, {}, NOW, fetch)
        self.assertEqual(status, 1)
        ecosystem, version = (
            ("npm", "1")
            if young == "tool"
            else ("pypi", "2026.10.19" if young == "certifi" else "1")
        )
        self.assertEqual(
            errors.getvalue(),
            f"{ecosystem}:{young}=={version} was uploaded inside the five-day window\n",
        )
        self.assertEqual(
            output.getvalue(),
            "pdm.lock: 1 releases checked, 0 exceptions\n"
            "container/requirements-amd64.txt: 1 releases checked, 0 exceptions\n"
            "container/companion-requirements.txt: 1 releases checked, 0 exceptions\n"
            "container/pdm-bootstrap-requirements.txt: 1 releases checked, 0 exceptions\n"
            "docs/site-assets/package-lock.json: 1 releases checked, 0 exceptions\n",
        )

    def test_each_lock_reports_checked_releases_and_exact_exceptions(self):
        module = checker()
        output, errors = io.StringIO(), io.StringIO()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_fixture(root)

            root.joinpath("container/requirements-amd64.txt").write_text(
                "demo==1\ntorch @ https://download.example/torch-1.whl \\\n    --hash=sha256:"
                + "a" * 64
                + "\n"
            )

            def fetch(url):
                if url == "https://download.example/torch/":
                    return (
                        '<a href="torch-1.whl#sha256='
                        + "a" * 64
                        + '" data-upload-time="2026-10-01T00:00:00Z">wheel</a>'
                    )
                if urlsplit(url).hostname == "registry.npmjs.org":
                    return {"time": {"1": "2026-10-01T00:00:00Z"}}
                return pypi("2026-10-21T00:00:00Z" if "certifi" in url else "2026-10-01T00:00:00Z")

            with redirect_stdout(output), redirect_stderr(errors):
                status = module.check(root, security_exception(module), NOW, fetch)

        self.assertEqual(status, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(
            output.getvalue(),
            "pdm.lock: 1 releases checked, 1 exception (certifi 2026.10.19, GHSA-6q48-6c7w-9gw2)\n"
            "container/requirements-amd64.txt: 2 releases checked, 0 exceptions\n"
            "container/companion-requirements.txt: 1 releases checked, 0 exceptions\n"
            "container/pdm-bootstrap-requirements.txt: 1 releases checked, 0 exceptions\n"
            "docs/site-assets/package-lock.json: 1 releases checked, 0 exceptions\n",
        )

    def test_each_missing_or_empty_required_lock_fails_with_its_path(self):
        module = checker()
        empty = {
            "pdm.lock": "package = []\n",
            "container/requirements-amd64.txt": "# no releases\n",
            "container/companion-requirements.txt": "# no releases\n",
            "container/pdm-bootstrap-requirements.txt": "# no releases\n",
            "docs/site-assets/package-lock.json": '{"packages": {}}',
        }
        for name, contents in empty.items():
            for missing in (False, True):
                with self.subTest(name=name, missing=missing), TemporaryDirectory() as directory:
                    root = Path(directory)
                    lock_fixture(root)
                    path = root / name
                    if missing:
                        path.unlink()
                    else:
                        path.write_text(contents, encoding="utf-8")
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        status = module.check(root, {}, NOW, lambda _: pypi(NOW.isoformat()))
                    self.assertEqual(status, 1)
                    reason = "is missing" if missing else "contains no registry releases"
                    self.assertEqual(errors.getvalue(), f"{name}: {reason}\n")
                    self.assertEqual(output.getvalue(), "")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_fixture(root)
            path = root / "pdm.lock"
            path.unlink()
            path.mkdir()
            errors = io.StringIO()
            with redirect_stderr(errors):
                status = module.check(root, {}, NOW, lambda _: {})
            self.assertEqual(status, 1)
            self.assertEqual(errors.getvalue(), "pdm.lock: is missing\n")
            errors = io.StringIO()
            with (
                patch.object(module, "inventories", side_effect=OSError("lock unreadable")),
                redirect_stderr(errors),
            ):
                status = module.check(root, {}, NOW, lambda _: {})
            self.assertEqual(status, 1)
            self.assertEqual(errors.getvalue(), "lock unreadable\n")

    def test_unmatched_exception_fails_with_removal_instruction(self):
        module = checker()
        output, errors = io.StringIO(), io.StringIO()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock_fixture(root)
            exceptions = {"certifi": module.SecurityException("different", "GHSA-6q48-6c7w-9gw2")}
            with redirect_stdout(output), redirect_stderr(errors):
                status = module.check(root, exceptions, NOW, lambda _: {})

        self.assertEqual(status, 1)
        self.assertEqual(
            errors.getvalue(),
            "certifi==different matches no locked PyPI release; remove this exception\n",
        )
        self.assertEqual(output.getvalue(), "")

    def test_npm_release_cannot_use_a_python_security_exception(self):
        module = checker()
        package = module.Package("npm", "certifi", "2026.10.19")
        self.assertEqual(
            module.age_findings(
                [package],
                NOW,
                lambda _: {"time": {package.version: NOW.isoformat()}},
                security_exception(module),
            ),
            ["npm:certifi==2026.10.19 was uploaded inside the five-day window"],
        )
        self.assertEqual(
            module._unmatched_exceptions([package], security_exception(module)),
            ["certifi==2026.10.19 matches no locked PyPI release; remove this exception"],
        )

    def test_summary_matches_the_exact_exception_version(self):
        module = checker()
        self.assertEqual(
            module._summary(
                "pdm.lock",
                [module.Package("pypi", "certifi", "other")],
                security_exception(module),
            ),
            "pdm.lock: 1 releases checked, 0 exceptions",
        )

    def test_exception_names_are_normalized_and_versions_cannot_be_empty(self):
        module = checker()
        self.assertEqual(
            module.parse_exceptions(
                {"Opentelemetry_API": {"version": "1", "advisory": "CVE-2026-12345"}}
            ),
            {"opentelemetry-api": module.SecurityException("1", "CVE-2026-12345")},
        )
        with self.assertRaisesRegex(ValueError, "^demo exception needs a version$"):
            module.parse_exceptions({"demo": {"version": "", "advisory": "CVE-2026-12345"}})

    def test_registry_fetch_rejects_plain_http(self):
        module = checker()
        with self.assertRaisesRegex(ValueError, "^Registry metadata URL must use HTTPS$"):
            module.fetch_json("http://example.invalid/metadata")
