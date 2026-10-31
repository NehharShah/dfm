"""DFM must never depend on, or import, a hosted-inference client."""

import ast
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Distribution names of hosted-model SDKs and gateways.
DENIED_DISTRIBUTIONS = {
    "openai",
    "anthropic",
    "google-generativeai",
    "google-genai",
    "google-cloud-aiplatform",
    "vertexai",
    "groq",
    "together",
    "fireworks-ai",
    "openrouter",
    "cohere",
    "mistralai",
    "litellm",
    "langchain-openai",
    "langchain-anthropic",
    "langchain-google-genai",
    "replicate",
}

# Import roots that are hosted-inference clients.
DENIED_IMPORT_ROOTS = {
    "openai",
    "anthropic",
    "google.generativeai",
    "google.genai",
    "vertexai",
    "groq",
    "together",
    "fireworks",
    "cohere",
    "mistralai",
    "litellm",
    "langchain_openai",
    "langchain_anthropic",
    "langchain_google_genai",
    "replicate",
}

# huggingface_hub is needed to read local MLX weights; only its remote inference client is denied.
DENIED_IMPORT_NAMES = {
    ("huggingface_hub", "InferenceClient"),
    ("huggingface_hub", "AsyncInferenceClient"),
}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(req: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
    assert match, req
    return _normalize(match.group(1))


def test_runtime_dependencies_exclude_hosted_inference_sdks() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    requirements = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    names = {_requirement_name(r) for r in requirements}
    assert names.isdisjoint(DENIED_DISTRIBUTIONS), names & DENIED_DISTRIBUTIONS


def test_lockfile_excludes_hosted_inference_sdks() -> None:
    lock = ROOT / "uv.lock"
    if not lock.exists():
        return
    packages = tomllib.loads(lock.read_text()).get("package", [])
    names = {_normalize(p["name"]) for p in packages}
    assert names.isdisjoint(DENIED_DISTRIBUTIONS), names & DENIED_DISTRIBUTIONS


def _denied(module: str) -> bool:
    return any(module == root or module.startswith(root + ".") for root in DENIED_IMPORT_ROOTS)


def test_dfm_source_never_imports_hosted_inference_clients() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "dfm").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [f"{path}: {a.name}" for a in node.names if _denied(a.name)]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if _denied(node.module):
                    offenders.append(f"{path}: {node.module}")
                offenders += [
                    f"{path}: {node.module}.{a.name}"
                    for a in node.names
                    if (node.module.split(".")[0], a.name) in DENIED_IMPORT_NAMES
                ]
    assert not offenders, offenders
