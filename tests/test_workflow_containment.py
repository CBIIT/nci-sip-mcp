"""The installer workflows keep caDSR and Shared SI on loopback with one shared design (#434).

Each job's containment text is pinned exactly by its own workflow test; this module ties the two
jobs to each other and to the production defaults they replace, and runs the shared request
verdict and the closed-endpoint check against the real programs, on loopback only.
"""

import ast
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import yaml

from nci_sip_mcp import installer
from nci_sip_mcp.config import BASE_URL_VARIABLES, DEFAULT_EVS_BASE_URL, PRODUCTION_BASE_URLS

ROOT = Path(__file__).parent.parent
JOBS = {"local-installer.yml": "local-installer", "local-run.yml": "local-run"}
BACKSTOP = "Resolve prohibited caDSR and Shared SI hosts to loopback (CI only)"
# Every production default not on the EVS host (which CI may reach), and its loopback recorder URL.
RECORDED = {
    "cadsr_base_url": "http://127.0.0.1:8767/cadsr",
    "cadsr_ftp_url": "http://127.0.0.1:8767/cadsr-ftp",
    "ssis_sparql_url": "http://127.0.0.1:8767/ssis-sparql",
}
# The staging caDSR API is no production default, but CI must not reach it either.
STAGE_HOST = "cadsrapi-stage.cancer.gov"
VERDICT = "<<'PY'\nimport re, sys\n"
EVS_HOST = urlsplit(DEFAULT_EVS_BASE_URL).hostname


def job(name):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    return workflow["jobs"][JOBS[name]]


def run_texts(name):
    return [step["run"] for step in job(name)["steps"] if "run" in step]


def step_run(name, marker):
    (run,) = [step["run"] for step in job(name)["steps"] if marker in step.get("run", "")]
    return run


def backstop(name):
    return next(step["run"] for step in job(name)["steps"] if step.get("name") == BACKSTOP)


def verdict(name):
    """The Python heredoc that judges the recorder log."""

    run = step_run(name, VERDICT)
    return run[run.index(VERDICT) + len("<<'PY'\n") :].split("\nPY\n", 1)[0]


def contained_hosts():
    return {urlsplit(PRODUCTION_BASE_URLS[key]).hostname for key in RECORDED} | {STAGE_HOST}


def run_verdict(code, log):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "requests.log"
        path.write_text(log, encoding="utf-8")
        return subprocess.run(  # noqa: S603 - the test's own interpreter and a temporary file
            [sys.executable, "-c", code, str(path)], capture_output=True, check=False
        ).returncode


def send(port, method, path):
    """One request to the empty recorder, which answers every path the tests send with an error."""

    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        direct.open(request, timeout=5).close()
    except HTTPError as error:
        error.close()


def recorded_log(*requests):
    """The stderr log of a real loopback http.server after the given (method, path) requests."""

    with tempfile.TemporaryDirectory() as directory:
        log_path = Path(directory) / "requests.log"
        served = Path(directory) / "empty"
        served.mkdir()
        with log_path.open("wb") as log:
            server = subprocess.Popen(  # noqa: S603 - the test's own interpreter, loopback only
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "http.server",
                    "0",
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(served),
                ],
                cwd=directory,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
            )
            try:
                banner = server.stdout.readline()  # "Serving HTTP on 127.0.0.1 port N (...)"
                port = int(banner.split(" port ")[1].split()[0])
                for method, path in requests:
                    send(port, method, path)
            finally:
                server.terminate()
                server.wait(timeout=10)
                server.stdout.close()
        return log_path.read_text(encoding="utf-8")


class NotFound(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_error(404)


class WorkflowContainmentTest(unittest.TestCase):
    def test_both_jobs_redirect_every_non_evs_production_endpoint_and_bypass_proxies(self):
        self.assertEqual(
            sorted(
                key
                for key, url in PRODUCTION_BASE_URLS.items()
                if urlsplit(url).hostname != EVS_HOST
            ),
            sorted(RECORDED),
        )
        expected = {BASE_URL_VARIABLES[key]: url for key, url in RECORDED.items()}
        for name in JOBS:
            with self.subTest(workflow=name):
                env = dict(job(name)["env"])
                proxies = env.pop("no_proxy")
                self.assertEqual(env, expected)
                self.assertEqual(
                    set(proxies.split(",")), {"127.0.0.1", "localhost", "::1"} | contained_hosts()
                )
        self.assertEqual(job("local-installer.yml")["env"], job("local-run.yml")["env"])

    def test_both_jobs_share_one_hosts_backstop_for_exactly_the_contained_hosts(self):
        text = backstop("local-installer.yml")
        self.assertEqual(backstop("local-run.yml"), text)
        (hosts,) = [line for line in text.splitlines() if line.startswith("hosts=")]
        self.assertEqual(set(hosts.removeprefix("hosts=").strip('"').split()), contained_hosts())

    def test_both_jobs_record_on_the_recorded_port_and_judge_exactly_the_recorded_paths(self):
        (port,) = {urlsplit(url).port for url in RECORDED.values()}
        paths = sorted(urlsplit(url).path.removeprefix("/") for url in RECORDED.values())
        start = f"python3 -m http.server {port} --bind 127.0.0.1 "
        for name in JOBS:
            with self.subTest(workflow=name):
                self.assertEqual(len([run for run in run_texts(name) if start in run]), 1)
                self.assertIn(f"/({'|'.join(paths)})", verdict(name))

    def test_the_shared_verdict_accepts_only_get_or_head_404s_on_recorded_paths(self):
        code = verdict("local-installer.yml")
        self.assertEqual(verdict("local-run.yml"), code)
        good = recorded_log(("GET", "/cadsr"), ("HEAD", "/ssis-sparql/query"))
        self.assertEqual(run_verdict(code, good), 0, good)
        failing = {
            "a POST": recorded_log(("GET", "/cadsr"), ("POST", "/cadsr")),
            "a 404 for another method": good.replace('"HEAD /ssis-sparql', '"PUT /ssis-sparql'),
            "an EVS path": recorded_log(("GET", "/cadsr"), ("GET", "/evs")),
            "no GET /cadsr": recorded_log(("GET", "/cadsr-ftp")),
            "a path beyond a recorded one": recorded_log(("GET", "/cadsr"), ("GET", "/cadsrx")),
            "an unknown line": good + "Traceback (most recent call last):\n",
            "a non-404 status": good.replace('/query HTTP/1.1" 404', '/query HTTP/1.1" 200'),
        }
        for case, log in failing.items():
            with self.subTest(case=case):
                self.assertNotEqual(run_verdict(code, log), 0, log)

    def test_a_closed_cadsr_endpoint_yields_the_exact_record_local_installer_requires(self):
        run = step_run("local-installer.yml", "unreachable.json")
        (statement,) = [line for line in run.splitlines() if line.startswith("assert record == ")]
        expected = ast.literal_eval(
            statement.removeprefix("assert record == ").removesuffix(", record")
        )
        evs = ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
        threading.Thread(target=evs.serve_forever, daemon=True).start()
        self.addCleanup(evs.server_close)
        self.addCleanup(evs.shutdown)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        # Through the config's own variable names: a renamed variable must not let this unit
        # test fall back to the production caDSR default.
        environment = {
            "no_proxy": "127.0.0.1",
            "NCI_SIP_UPSTREAM_MODE": "live",
            BASE_URL_VARIABLES["evs_base_url"]: f"http://127.0.0.1:{evs.server_address[1]}/evs",
            BASE_URL_VARIABLES["cadsr_base_url"]: f"http://127.0.0.1:{closed}/cadsr",
        }
        output = io.StringIO()
        with patch.dict(os.environ, environment), redirect_stdout(output):
            code = installer.main(["check", "--target", "local"])
        self.assertEqual((code, json.loads(output.getvalue())), (1, expected))
