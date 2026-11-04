"""Check the TLS clients used by CI against compromised eMudhra chains."""

import argparse
import hashlib
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

COMPROMISED_URLS = (
    "https://dv-g3b-emudhra.we-have-a-problem.com/",
    "https://dv-g3b-aaa-emudhra.we-have-a-problem.com/",
)
BLOCKED_ROOTS = {
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
}
CERTIFICATE_ERRORS = (
    "certificate verify failed",
    "certificate verification failed",
    "server certificate verification failed",
    "server verification failed: certificate signer not trusted",
    "ssl certificate problem",
)
UNRELATED_CERTIFICATE_ERRORS = re.compile(
    r"expired|not.?yet.?valid|not.?valid.?yet|not.?valid.?for.?name|hostname|does not match|"
    r"no alternative certificate subject name|certificate subject name"
)
UV_TRUST_REJECTION = re.compile(
    r"invalid peer certificate:\s*(?:UnknownIssuer|unknown issuer|Revoked|"
    r"certificate revoked|Other\(OtherError\(CertDistrusted\)\))",
    re.IGNORECASE,
)
PROBE_ENVIRONMENT = (
    "PATH",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "GIT_SSL_CAPATH",
    "OPENSSL_X509_TEA_DISABLE",
)
TRUST_REJECTION_CODES = {19, 20, 21, 23, 27, 28}


def repair_linux_trust() -> None:
    """Filter the runner's CA bundle and export isolated trust for subsequent steps."""
    source = ssl.get_default_verify_paths().cafile
    if source is None:
        raise RuntimeError("Python has no default CA bundle to repair")
    certs = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        Path(source).read_text(),
        re.S,
    )
    retained = [
        cert
        for cert in certs
        if hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert)).hexdigest()
        not in BLOCKED_ROOTS
    ]
    if not retained:
        raise RuntimeError("Refusing to install an empty CA bundle")
    directory = Path(
        tempfile.mkdtemp(prefix="runner-tls-trust-", dir=os.environ["RUNNER_TEMP"])
    )
    bundle = directory / "cert.pem"
    bundle.write_text("\n".join(retained) + "\n")
    ssl.create_default_context(cafile=str(bundle))
    empty_directory = directory / "certs"
    empty_directory.mkdir()
    overrides = dict.fromkeys(
        ("SSL_CERT_FILE", "GIT_SSL_CAINFO", "PIP_CERT", "REQUESTS_CA_BUNDLE"),
        str(bundle),
    )
    # CURL_CA_BUNDLE takes precedence over SSL_CERT_DIR; curl needs both SSL_CERT_* overrides.
    overrides["CURL_CA_BUNDLE"] = ""
    overrides.update(
        SSL_CERT_DIR=str(empty_directory), GIT_SSL_CAPATH=str(empty_directory)
    )
    with open(os.environ["GITHUB_ENV"], "a") as environment:
        environment.writelines(f"{key}={value}\n" for key, value in overrides.items())
    os.environ.update(overrides)
    print(
        f"Filtered {len(certs) - len(retained)} blocked roots from {source}; "
        f"using {bundle} and empty CA directory {empty_directory}",
        flush=True,
    )


def run_probe(
    command: list[str], *, data: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run without repository Git credentials or interactive authentication."""
    with tempfile.TemporaryDirectory(prefix="runner-tls-") as temporary:
        env = {key: os.environ[key] for key in PROBE_ENVIRONMENT if key in os.environ}
        # uv must prove its bundled trust is safe independently of the macOS repair.
        if command[0] == "uv":
            env.pop("SSL_CERT_FILE", None)
            env.pop("SSL_CERT_DIR", None)
        env.update(
            HOME=temporary,
            LC_ALL="C",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            UV_HTTP_TIMEOUT="10",
            UV_HTTP_RETRIES="0",
        )
        return subprocess.run(
            command,
            input=data,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=env,
            cwd=temporary,
        )


def require_certificate_rejection(
    client: str, result: subprocess.CompletedProcess[str], *, curl: bool = False
) -> None:
    """Reject successful requests and failures unrelated to certificate validation."""
    diagnostic = result.stderr.lower()
    certificate_error = (
        result.returncode == 60
        if curl
        else any(message in diagnostic for message in CERTIFICATE_ERRORS)
        or UV_TRUST_REJECTION.search(diagnostic) is not None
    )
    if (
        result.returncode == 0
        or not certificate_error
        or UNRELATED_CERTIFICATE_ERRORS.search(diagnostic) is not None
    ):
        raise RuntimeError(
            f"{client} did not reject the compromised certificate "
            f"(exit {result.returncode}): {result.stderr[-1500:]}"
        )


def check_python() -> None:
    """Verify the selected interpreter's default SSL context."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    with opener.open("https://pypi.org/simple/certifi/", timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Python healthy HTTPS control returned {response.status}"
            )
    for url in COMPROMISED_URLS:
        try:
            with opener.open(url, timeout=30):
                pass
        except urllib.error.URLError as error:
            if (
                isinstance(error.reason, ssl.SSLCertVerificationError)
                and error.reason.verify_code in TRUST_REJECTION_CODES
            ):
                continue
            raise RuntimeError(
                f"Python failed without a certificate rejection: {url}"
            ) from error
        raise RuntimeError(f"Python accepted a compromised certificate: {url}")
    print(f"PASS: Python {sys.executable} rejects both compromised chains")


def check_command_clients() -> None:
    """Check real requests with curl, Git, and uv without installing packages."""
    commands = {
        "curl": [
            "curl",
            "--disable",
            "--silent",
            "--show-error",
            "--fail",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            "--output",
            os.devnull,
        ],
        "git": [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "http.extraHeader=",
            "ls-remote",
        ],
        "uv": [
            "uv",
            "pip",
            "compile",
            "--no-config",
            "--no-cache",
            "--no-deps",
            "--no-build",
            "--no-python-downloads",
            "--python-version",
            "3.13",
            "--index-url",
        ],
    }
    for client, command in commands.items():
        data = "certifi==2026.10.19\n" if client == "uv" else None
        healthy_url = (
            "https://github.com/curl/curl.git"
            if client == "git"
            else "https://pypi.org/simple/"
            if client == "uv"
            else "https://pypi.org/simple/certifi/"
        )
        suffix = ["-"] if client == "uv" else []
        result = run_probe([*command, healthy_url, *suffix], data=data)
        if result.returncode != 0:
            raise RuntimeError(
                f"{client} healthy HTTPS control failed: {result.stderr[-1500:]}"
            )
        for url in COMPROMISED_URLS:
            result = run_probe([*command, url, *suffix], data=data)
            require_certificate_rejection(client, result, curl=client == "curl")
        trust = " bundled trust" if client == "uv" else ""
        print(f"PASS: {client}{trust} rejects both compromised chains")


def main() -> None:
    """Check the interpreter and the network tools available on this runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repair-linux-trust",
        action="store_true",
        help="On Linux, export a filtered CA bundle in RUNNER_TEMP via GITHUB_ENV.",
    )
    arguments = parser.parse_args()
    print(
        f"Python {sys.executable}: {ssl.OPENSSL_VERSION}; "
        f"default trust: {ssl.get_default_verify_paths()}",
        flush=True,
    )
    if arguments.repair_linux_trust and sys.platform == "linux":
        repair_linux_trust()
    check_python()
    check_command_clients()


if __name__ == "__main__":
    main()
