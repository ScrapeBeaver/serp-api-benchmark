"""A provider is a folder: `provider.yaml` (what it is, what it costs, what it can do) and
`adapter.py` (how to ask it and how to read its answer).

The registry finds providers by folder. Adding one never requires touching this package.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from serp_bench.contract import ProviderConfig
from serp_bench.models import Device, FailureClass, HttpRequest, RawResponse, Serp, Task

REDACTED = "***"


class ProviderConfigError(Exception):
    """A provider folder that cannot be loaded, with a message a person can act on."""


class Provider(ABC):
    """The adapter surface. Subclass it in `providers/<id>/adapter.py`.

    An adapter does not send anything and does not measure anything: it says what to send
    and reads what came back. The runner owns the connection, the clock and the timeout,
    so every provider is timed the same way.
    """

    def __init__(self, config: ProviderConfig, env: Mapping[str, str] | None = None) -> None:
        env = os.environ if env is None else env
        self.config = config
        self._secrets = {name: env[name] for name in config.auth.env if env.get(name)}
        adapter = config.adapter
        override = env.get(adapter.base_url_env) if adapter.base_url_env else None
        self.base_url = (override or adapter.base_url or "").rstrip("/")
        self.options = adapter.options

    # --- what an adapter implements ---------------------------------------------------

    @abstractmethod
    def build_request(self, task: Task) -> HttpRequest:
        """The HTTP request that asks this provider for `task`."""

    @abstractmethod
    def parse(self, raw: RawResponse) -> Serp:
        """Reduce a 2xx JSON response to the common shape.

        Raise KeyError, TypeError or ValueError when the body does not have the shape the
        provider documents: the runner records that as `malformed_results`.
        """

    def classify_error(self, raw: RawResponse) -> FailureClass | None:
        """A failure the provider reports inside a 2xx body, or None. Default: none."""
        return None

    def own_fault(self, raw: RawResponse) -> str | None:
        """A void reason when the response says the problem is ours (an exhausted quota
        reported with an unusual status or inside a body), or None. HTTP 401, 402 and 403
        are already treated as ours for every provider."""
        return None

    def billed_units(self, raw: RawResponse) -> float | None:
        """Units this response says were charged, or None when it does not say."""
        return None

    def transport(self) -> httpx.AsyncBaseTransport | None:
        """A custom transport. Real providers return None; the offline mock returns one."""
        return None

    # --- what the base class provides -------------------------------------------------

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def missing_env(self) -> list[str]:
        """Required environment variables that are not set."""
        return [name for name in self.config.auth.env if name not in self._secrets]

    def secret(self, name: str) -> str:
        """The value of a declared secret. Only values declared in `auth.env` are reachable,
        which is what lets `redact` guarantee they never leave the process in a log."""
        if name not in self.config.auth.env:
            raise KeyError(f"{name} is not declared under auth.env in {self.id}/provider.yaml")
        return self._secrets[name]

    def supports(self, task: Task) -> bool:
        """False only when the provider is known not to offer what the task needs.
        "unknown" is tried: finding out is part of the work."""
        caps = self.config.capabilities
        if task.device is Device.mobile and caps.mobile is False:
            return False
        if task.location and caps.city_location is False:
            return False
        return not (task.page > 1 and caps.pagination is False)

    def redact(self, text: str) -> str:
        """`text` with every secret value replaced."""
        for value in self._secrets.values():
            if value:
                text = text.replace(value, REDACTED)
        return text

    def loggable(self, request: HttpRequest) -> dict[str, Any]:
        """A request in a form that is safe to print or store.

        A header that carries credentials is masked whole, whatever is in it: a secret can
        travel encoded (HTTP Basic is base64 of login:password), and an encoded secret is
        not found by looking for the secret itself.
        """
        return {
            "method": request.method,
            "url": self.redact(request.url),
            "headers": {k: _mask_header(k, self.redact(v)) for k, v in request.headers.items()},
            "params": {k: self.redact(v) for k, v in request.params.items()},
            "json": _redact_deep(request.json_body, self.redact),
        }


_CREDENTIAL_HEADER = re.compile(r"auth|key|token|secret|cookie", re.IGNORECASE)


def _mask_header(name: str, value: str) -> str:
    """The value of a credential header with everything but its scheme word masked."""
    if not _CREDENTIAL_HEADER.search(name):
        return value
    scheme, _, rest = value.partition(" ")
    return f"{scheme} {REDACTED}" if rest else REDACTED


def _redact_deep(value: Any, redact: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_deep(v, redact) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_deep(v, redact) for v in value]
    return value


# --- registry -------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderSpec:
    """A discovered provider folder: its configuration and its adapter class."""

    config: ProviderConfig
    folder: Path
    adapter_class: type[Provider]

    def create(self, env: Mapping[str, str] | None = None) -> Provider:
        return self.adapter_class(self.config, env)

    @property
    def fixtures_dir(self) -> Path:
        return self.folder / "fixtures"


def parse_config(path: Path) -> ProviderConfig:
    """Read and validate one provider.yaml."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProviderConfigError(f"{path}: file is missing") from exc
    except yaml.YAMLError as exc:
        raise ProviderConfigError(f"{path}: not valid YAML: {exc}") from exc
    try:
        return ProviderConfig.model_validate(data)
    except ValidationError as exc:
        raise ProviderConfigError(f"{path}:\n{format_validation_error(exc)}") from exc


def load_config(folder: Path) -> ProviderConfig:
    path = folder / "provider.yaml"
    config = parse_config(path)
    if config.id != folder.name and not folder.name.startswith("_"):
        raise ProviderConfigError(
            f"{path}: id is '{config.id}' but the folder is '{folder.name}'; they must match"
        )
    return config


def load_adapter_class(folder: Path, provider_id: str) -> type[Provider]:
    path = folder / "adapter.py"
    if not path.is_file():
        raise ProviderConfigError(f"{folder}: adapter.py is missing")
    module_name = f"serp_bench_providers.{provider_id}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ProviderConfigError(f"{path}: cannot be imported")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - any import-time failure is a broken provider
        sys.modules.pop(module_name, None)
        raise ProviderConfigError(f"{path}: failed to import: {exc!r}") from exc
    classes = [
        cls
        for _, cls in inspect.getmembers(module, inspect.isclass)
        if issubclass(cls, Provider) and cls is not Provider and cls.__module__ == module_name
    ]
    if len(classes) != 1:
        raise ProviderConfigError(
            f"{path}: expected exactly one Provider subclass, found {len(classes)}"
        )
    return classes[0]


def load_spec(folder: Path) -> ProviderSpec:
    config = load_config(folder)
    return ProviderSpec(config, folder, load_adapter_class(folder, config.id))


def discover(providers_dir: Path) -> dict[str, ProviderSpec]:
    """Every provider folder under `providers_dir`, by id. Folders whose name starts with
    an underscore (the template) are not providers."""
    if not providers_dir.is_dir():
        raise ProviderConfigError(f"{providers_dir}: providers directory not found")
    specs: dict[str, ProviderSpec] = {}
    for folder in sorted(p for p in providers_dir.iterdir() if p.is_dir()):
        if folder.name.startswith(("_", ".")) or not (folder / "provider.yaml").is_file():
            continue
        spec = load_spec(folder)
        specs[spec.config.id] = spec
    return specs


def format_validation_error(exc: ValidationError) -> str:
    """One readable line per problem: where it is and what is wrong."""
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(top level)"
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)
