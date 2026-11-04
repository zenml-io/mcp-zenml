"""Regression checks for the TLS verification gate, without network requests."""

import os
import ssl
import subprocess
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import check_runner_tls


class CertificateRejectionTests(unittest.TestCase):
    def test_success_never_counts_as_rejection(self) -> None:
        result = subprocess.CompletedProcess([], 0, "", "certificate verify failed")
        with self.assertRaises(RuntimeError):
            check_runner_tls.require_certificate_rejection("git", result)

    def test_network_and_http_failures_do_not_count_as_rejection(self) -> None:
        for diagnostic in (
            "Could not resolve host",
            "Connection timed out",
            "HTTP 404",
            "HTTP 500",
        ):
            for client in ("git", "uv"):
                result = subprocess.CompletedProcess([], 128, "", diagnostic)
                with (
                    self.subTest(client=client, diagnostic=diagnostic),
                    self.assertRaises(RuntimeError),
                ):
                    check_runner_tls.require_certificate_rejection(client, result)

    def test_explicit_certificate_failures_count_as_rejection(self) -> None:
        for diagnostic in check_runner_tls.CERTIFICATE_ERRORS:
            result = subprocess.CompletedProcess([], 128, "", diagnostic)
            check_runner_tls.require_certificate_rejection("git", result)

    def test_git_http_response_does_not_count_as_certificate_rejection(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            128,
            "",
            "fatal: https://dv-g3b-emudhra.we-have-a-problem.com/info/refs "
            "not valid: is this a git repository?",
        )
        with self.assertRaises(RuntimeError):
            check_runner_tls.require_certificate_rejection("git", result)

    def test_curl_requires_certificate_verification_exit_status(self) -> None:
        for status in (6, 7, 22, 28, 35):
            result = subprocess.CompletedProcess([], status, "", "TLS failed")
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                check_runner_tls.require_certificate_rejection(
                    "curl", result, curl=True
                )
        result = subprocess.CompletedProcess([], 60, "", "SSL certificate problem")
        check_runner_tls.require_certificate_rejection("curl", result, curl=True)

    def test_expiration_and_hostname_errors_do_not_count_as_distrust(self) -> None:
        for diagnostic in (
            "expired",
            "NotValidYet",
            "not valid yet",
            "NotYetValid",
            "hostname mismatch",
            "certificate not valid for name",
            "does not match",
        ):
            for client in ("curl", "git", "uv"):
                result = subprocess.CompletedProcess(
                    [],
                    60 if client == "curl" else 1,
                    "",
                    f"certificate verify failed: {diagnostic}",
                )
                with (
                    self.subTest(client=client, diagnostic=diagnostic),
                    self.assertRaises(RuntimeError),
                ):
                    check_runner_tls.require_certificate_rejection(
                        client, result, curl=client == "curl"
                    )

    def test_uv_requires_explicit_distrust(self) -> None:
        for diagnostic in (
            "UnknownIssuer",
            "Revoked",
            "Other(OtherError(CertDistrusted))",
        ):
            result = subprocess.CompletedProcess(
                [], 2, "", f"invalid peer certificate: {diagnostic}"
            )
            check_runner_tls.require_certificate_rejection("uv", result)
        for diagnostic in (
            "NotValidYet",
            "NotValidForName",
            "BadEncoding",
            "UnhandledCriticalExtension",
        ):
            result = subprocess.CompletedProcess(
                [], 2, "", f"invalid peer certificate: {diagnostic}"
            )
            with self.subTest(diagnostic=diagnostic), self.assertRaises(RuntimeError):
                check_runner_tls.require_certificate_rejection("uv", result)

    def test_probes_do_not_forward_credentials_or_existing_home(self) -> None:
        environment = {
            "PATH": "/tools",
            "HOME": "/private-home",
            "CI_SECRET": "secret",
            "HTTPS_PROXY": "https://user:secret@proxy",
            "UV_EXTRA_INDEX_URL": "https://user:secret@index",
            "GIT_CONFIG_COUNT": "1",
            "SSL_CERT_FILE": "/repaired.pem",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("subprocess.run") as execute,
        ):
            check_runner_tls.run_probe(["curl", "https://public.example"])
        child = execute.call_args.kwargs["env"]
        self.assertEqual(child["SSL_CERT_FILE"], "/repaired.pem")
        self.assertNotEqual(child["HOME"], "/private-home")
        self.assertEqual(execute.call_args.kwargs["cwd"], child["HOME"])
        for key in (
            "CI_SECRET",
            "HTTPS_PROXY",
            "UV_EXTRA_INDEX_URL",
            "GIT_CONFIG_COUNT",
        ):
            self.assertNotIn(key, child)

    def test_uv_uses_bundled_trust_independently_of_macos_overrides(self) -> None:
        with (
            patch.dict(
                os.environ, {"SSL_CERT_FILE": "/repaired.pem", "SSL_CERT_DIR": "/empty"}
            ),
            patch("subprocess.run") as execute,
        ):
            check_runner_tls.run_probe(["uv", "pip", "compile"])
        child = execute.call_args.kwargs["env"]
        self.assertNotIn("SSL_CERT_FILE", child)
        self.assertNotIn("SSL_CERT_DIR", child)

    def test_probes_keep_apple_keychain_fallback_disabled(self) -> None:
        for client in ("git", "curl"):
            with (
                self.subTest(client=client),
                patch.dict(os.environ, {"OPENSSL_X509_TEA_DISABLE": "1"}),
                patch("subprocess.run") as execute,
            ):
                check_runner_tls.run_probe([client, "https://public.example"])
                self.assertEqual(
                    execute.call_args.kwargs["env"].get("OPENSSL_X509_TEA_DISABLE"),
                    "1",
                )

    def test_python_network_failure_does_not_count_as_rejection(self) -> None:
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 200
        opener.open.side_effect = [
            opener.open.return_value,
            urllib.error.URLError("timed out"),
        ]
        with (
            patch("urllib.request.build_opener", return_value=opener),
            self.assertRaises(RuntimeError),
        ):
            check_runner_tls.check_python()

    def test_python_rejects_expiration_as_evidence_of_distrust(self) -> None:
        opener = MagicMock()
        response = opener.open.return_value
        response.__enter__.return_value.status = 200
        error = ssl.SSLCertVerificationError("certificate has expired")
        error.verify_code = 10
        opener.open.side_effect = [response, urllib.error.URLError(error)]
        with (
            patch("urllib.request.build_opener", return_value=opener),
            self.assertRaises(RuntimeError),
        ):
            check_runner_tls.check_python()

    def test_python_accepts_both_chains_being_distrusted(self) -> None:
        opener = MagicMock()
        response = opener.open.return_value
        response.__enter__.return_value.status = 200
        error = ssl.SSLCertVerificationError("unable to get local issuer certificate")
        error.verify_code = 20
        opener.open.side_effect = [
            response,
            urllib.error.URLError(error),
            urllib.error.URLError(error),
        ]
        with patch("urllib.request.build_opener", return_value=opener):
            check_runner_tls.check_python()

    def test_python_requires_each_compromised_chain_to_be_rejected(self) -> None:
        opener = MagicMock()
        response = opener.open.return_value
        response.__enter__.return_value.status = 200
        error = ssl.SSLCertVerificationError("certificate verify failed")
        error.verify_code = 20
        rejection = urllib.error.URLError(error)
        opener.open.side_effect = [response, rejection, response]
        with (
            patch("urllib.request.build_opener", return_value=opener),
            self.assertRaises(RuntimeError),
        ):
            check_runner_tls.check_python()


if __name__ == "__main__":
    unittest.main()
