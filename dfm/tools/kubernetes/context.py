"""The active kube context and namespace.

Read with `kubectl config current-context` and a minified, *redacted* `config view`
(only the namespace field is extracted; never `--raw`). The kubeconfig is a secret
path: DFM reads these two names and nothing else from it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class KubeContext:
    name: str
    namespace: str

    def label(self) -> str:
        return f"{self.name}/{self.namespace}"


def _kubectl(
    *args: str, kubeconfig: str | None = None, environ: Mapping[str, str] | None = None
) -> str | None:
    binary = shutil.which("kubectl")
    if binary is None:
        return None
    explicit = ["--kubeconfig", kubeconfig] if kubeconfig else []
    try:
        out = subprocess.run(  # noqa: S603
            [binary, "config", *explicit, *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            # the environment the command itself gets: KUBECONFIG decides which file it reads
            env=dict(os.environ if environ is None else environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def kube_context(
    context: str | None = None,
    *,
    kubeconfig: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> KubeContext | None:
    """The context a kubectl/helm command would use (`--context` wins), or None if unknown.

    `kubeconfig` and `environ` are the command's own `--kubeconfig` and environment, so this is
    the cluster the command will actually reach.
    """
    name = context or _kubectl("current-context", kubeconfig=kubeconfig, environ=environ)
    if not name:
        return None
    namespace = _kubectl(
        "view",
        "--minify",
        "--context",
        name,
        "-o",
        "jsonpath={..namespace}",
        kubeconfig=kubeconfig,
        environ=environ,
    )
    return KubeContext(name, namespace or "default")
