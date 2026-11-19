"""Local composition separates public docs, bounded fixture workers and serving assets."""

import tomllib
import unittest
from pathlib import Path

import yaml
from scripts.companion_lock import render


class CompanionCompositionTest(unittest.TestCase):
    def setUp(self):
        self.compose = yaml.safe_load(Path("container/compose.local.yaml").read_text())

    def test_local_services_publish_only_loopback_and_have_resource_boundaries(self):
        common = {
            "platform": "linux/amd64",
            "read_only": True,
            "user": "65532:65532",
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }
        expected = {
            "documentation": common
            | {
                "image": "nci-si-docs:local",
                "ports": ["127.0.0.1:8080:8080"],
                "networks": ["documentation"],
                "pids_limit": 32,
                "mem_limit": "256m",
                "cpus": 0.5,
                "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m,mode=1777"],  # noqa: S108
            },
            "administration": common
            | {
                "image": "nci-si-admin:local",
                "networks": ["validation"],
                "pids_limit": 128,
                "mem_limit": "2g",
                "cpus": 2,
                "stop_grace_period": "20s",
                "volumes": ["evidence:/state"],
                "tmpfs": ["/tmp:rw,noexec,nosuid,size=256m,mode=1777"],  # noqa: S108
            },
            "admin-ingress": common
            | {
                "image": "nci-si-docs:local",
                "entrypoint": ["python3.14", "-m", "scripts.companion_relay"],
                "ports": ["127.0.0.1:8081:8081"],
                "networks": ["access", "validation"],
                "pids_limit": 16,
                "mem_limit": "128m",
                "cpus": 0.5,
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python3.14",
                        "-c",
                        "import urllib.request; "
                        + "urllib.request.urlopen('http://127.0.0.1:8081/health',timeout=3).read()",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                },
            },
            "mcp": common
            | {
                "image": "nci-sip-mcp:verified",
                "ports": ["127.0.0.1:8000:8000"],
                "networks": ["serving"],
                "pids_limit": 128,
                "mem_limit": "8g",
                "cpus": 4,
                "environment": {
                    "NCI_SIP_EMBEDDING_MODEL": "/model",
                    "NCI_SIP_HTTP_AUTH_MODE": "trusted-local",
                },
                "healthcheck": {
                    "test": [
                        "CMD",
                        "python",
                        "-c",
                        "import os, urllib.request; "
                        + "urllib.request.urlopen('http://127.0.0.1:' + "
                        + "os.getenv('NCI_SIP_HTTP_PORT', '8000') + '/ready', timeout=3).close()",
                    ],
                    "interval": "30s",
                    "timeout": "5s",
                    "start_period": "180s",
                    "retries": 3,
                },
                "volumes": [
                    "${NCI_SIP_LOCAL_INDEX:-../tmp/serving-index}:/data",
                    "${NCI_SIP_LOCAL_MODEL:-../tmp/serving-model}:/model:ro",
                ],
                "tmpfs": [
                    "/tmp:rw,noexec,nosuid,size=256m,mode=1777",  # noqa: S108
                    "/model-cache:rw,noexec,nosuid,size=16m,mode=1777",
                ],
            },
        }
        for name, service in self.compose["services"].items():
            with self.subTest(service=name):
                controls = {
                    key: value
                    for key, value in service.items()
                    if key not in {"profiles", "depends_on"}
                }
                self.assertEqual(controls, expected[name])

    def test_each_service_has_exactly_its_independent_profile_and_dependencies(self):
        self.assertEqual(
            {
                name: (service.get("profiles"), service.get("depends_on"))
                for name, service in self.compose["services"].items()
            },
            {
                "documentation": (["docs"], None),
                "administration": (["companion"], None),
                "admin-ingress": (["companion"], ["administration"]),
                "mcp": (["mcp"], None),
            },
        )

    def test_runbook_documents_exact_independent_start_and_stop_commands(self):
        guide = Path("docs/local-guide.md").read_text(encoding="utf-8")
        commands = [
            "docker compose -f container/compose.local.yaml --profile docs up -d",
            "docker compose -f container/compose.local.yaml --profile companion up -d",
            "docker compose -f container/compose.local.yaml --profile docs "
            + "--profile companion up -d",
            "docker compose -f container/compose.local.yaml stop documentation",
            "docker compose -f container/compose.local.yaml rm -f documentation",
            "docker compose -f container/compose.local.yaml stop admin-ingress administration",
            "docker compose -f container/compose.local.yaml rm -f admin-ingress administration",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(command, guide)
        runbook = Path("docs/companion-containers.md").read_text(encoding="utf-8")
        self.assertIn("docker compose -f container/compose.local.yaml stop mcp", runbook)
        self.assertIn("docker compose -f container/compose.local.yaml rm -f mcp", runbook)

    def test_fixture_workers_have_no_serving_network_assets_or_credentials(self):
        services = self.compose["services"]
        admin = services["administration"]
        self.assertEqual(admin["networks"], ["validation"])
        self.assertTrue(self.compose["networks"]["validation"]["internal"])
        self.assertEqual(admin["volumes"], ["evidence:/state"])
        self.assertNotIn("environment", admin)
        self.assertNotIn("ports", admin)
        self.assertEqual(services["admin-ingress"]["networks"], ["access", "validation"])
        self.assertNotIn("volumes", services["admin-ingress"])
        self.assertTrue(set(admin["networks"]).isdisjoint(services["mcp"]["networks"]))
        self.assertNotIn("volumes", services["documentation"])

    def test_serving_index_mount_is_writable_for_sqlite_wal_and_the_model_is_read_only(self):
        # A WAL database opened on a read-only directory fails startup with
        # "attempt to write a readonly database", so only the model mount may be :ro.
        self.assertEqual(
            self.compose["services"]["mcp"]["volumes"],
            [
                "${NCI_SIP_LOCAL_INDEX:-../tmp/serving-index}:/data",
                "${NCI_SIP_LOCAL_MODEL:-../tmp/serving-model}:/model:ro",
            ],
        )

    def test_the_runbook_tags_the_image_the_mcp_service_runs(self):
        # The container runbook builds :local; Compose would otherwise try to pull :verified.
        runbook = Path("docs/companion-containers.md").read_text(encoding="utf-8")
        self.assertEqual(self.compose["services"]["mcp"]["image"], "nci-sip-mcp:verified")
        self.assertIn("`docker tag nci-sip-mcp:local nci-sip-mcp:verified`", runbook)

    def test_committed_companion_dependencies_match_pdm_and_exclude_models(self):
        expected = render(tomllib.loads(Path("pdm.lock").read_text()))
        self.assertEqual(Path("container/companion-requirements.txt").read_text(), expected)
        self.assertNotIn("torch==", expected)
        self.assertNotIn("sentence-transformers==", expected)
        self.assertIn("numpy==", expected)
        self.assertIn("mcp==", expected)
