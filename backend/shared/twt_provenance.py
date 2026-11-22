"""Provenance and preflight verification for Tokens with Taste (TwT).

Guarantees:
1. Verify actual running gateway provenance and provider-model capabilities before paid inference.
2. Abort clearly on mismatch rather than declaring the requested SHA installed.
3. Detect and handle unchanged-package-version VCS upgrades (where version=0.1.0 is unchanged
   between commits, preventing pip from silently retaining an older SHA).
"""

import importlib.metadata
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


class TwTPreflightError(RuntimeError):
    """Raised when TwT gateway capabilities, reachability, or provenance fail preflight."""

    pass


class TwTVCSMismatchError(TwTPreflightError):
    """Raised when an installed VCS package SHA does not match the requested commit."""

    pass


def get_installed_provenance(
    package_name: str = "tokens-with-taste",
    dist_info_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Read PEP 610 direct_url.json provenance for an installed package."""
    version = None
    raw_direct_url = None

    if dist_info_dir is not None:
        p = Path(dist_info_dir)
        direct_url_file = p / "direct_url.json"
        if direct_url_file.exists():
            raw_direct_url = direct_url_file.read_text(encoding="utf-8")
        metadata_file = p / "METADATA"
        if metadata_file.exists():
            for line in metadata_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                    break
    else:
        try:
            dist = importlib.metadata.distribution(package_name)
            version = dist.version
            try:
                raw_direct_url = dist.read_text("direct_url.json")
            except Exception:
                raw_direct_url = None
        except importlib.metadata.PackageNotFoundError:
            return None

    provenance: Dict[str, Any] = {
        "package": package_name,
        "version": version or "0.0.0",
        "commit_id": None,
        "requested_revision": None,
        "vcs": None,
        "url": None,
        "subdirectory": None,
    }

    if raw_direct_url:
        try:
            data = json.loads(raw_direct_url)
            vcs_info = data.get("vcs_info") or {}
            provenance.update(
                commit_id=vcs_info.get("commit_id"),
                requested_revision=vcs_info.get("requested_revision"),
                vcs=vcs_info.get("vcs"),
                url=data.get("url"),
                subdirectory=data.get("subdirectory"),
            )
        except Exception:
            pass

    return provenance


def verify_installed_vcs_sha(
    package_name: str,
    expected_sha: str,
    provenance: Optional[Dict[str, Any]] = None,
    dist_info_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify that an installed package matches the expected VCS commit.

    Aborts clearly with TwTVCSMismatchError if:
    - Package is not installed.
    - Package was not installed from VCS (missing commit_id and requested_revision).
    - Neither commit_id nor requested_revision matches expected_sha (allowing >=7 char prefix).
    """
    if provenance is None:
        provenance = get_installed_provenance(package_name, dist_info_dir=dist_info_dir)

    if not provenance:
        raise TwTVCSMismatchError(f"Package '{package_name}' is not installed")

    commit_id = provenance.get("commit_id")
    requested_rev = provenance.get("requested_revision")
    if not commit_id and not requested_rev:
        raise TwTVCSMismatchError(
            f"Package '{package_name}' is installed (v{provenance.get('version')}) "
            f"but lacks VCS provenance in direct_url.json"
        )

    expected = expected_sha.strip().lower()
    matches = False
    for candidate in (commit_id, requested_rev):
        if candidate:
            cand = candidate.strip().lower()
            if (
                cand == expected
                or (len(expected) >= 7 and cand.startswith(expected))
                or (len(cand) >= 7 and expected.startswith(cand))
            ):
                matches = True
                break

    if not matches:
        raise TwTVCSMismatchError(
            f"VCS SHA mismatch for '{package_name}': installed commit is '{commit_id}' "
            f"(requested revision '{requested_rev}'), which does not match expected '{expected_sha}'. "
            f"Refusing to declare requested SHA installed."
        )

    return provenance


def query_gateway_models(
    base_url: str = "http://127.0.0.1:8000/v1",
    uds: Optional[str] = None,
    timeout: float = 10.0,
    client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Query /v1/models from running TwT gateway via UDS or HTTP."""
    import httpx

    url = base_url.rstrip("/") + "/models"

    def _parse_response(resp):
        if resp.status_code != 200:
            raise TwTPreflightError(
                f"TwT gateway /v1/models returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        if (
            not isinstance(payload, dict)
            or "data" not in payload
            or not isinstance(payload["data"], list)
        ):
            raise TwTPreflightError(
                f"TwT gateway /v1/models returned invalid schema: {resp.text[:200]}"
            )
        return payload["data"]

    try:
        if client is not None:
            resp = client.get(url)
            return _parse_response(resp)

        transport = None
        if uds:
            transport = httpx.HTTPTransport(uds=uds)

        client_kwargs: Dict[str, Any] = {"timeout": timeout, "trust_env": False}
        if transport:
            client_kwargs["transport"] = transport

        with httpx.Client(**client_kwargs) as http_client:
            resp = http_client.get(url)
            return _parse_response(resp)
    except httpx.HTTPError as exc:
        raise TwTPreflightError(
            f"Cannot reach TwT gateway at {uds or base_url}: {exc}"
        ) from exc


def query_gateway_provenance(
    base_url: str = "http://127.0.0.1:8000/v1",
    uds: Optional[str] = None,
    timeout: float = 10.0,
    client: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Query /v1/provenance from running TwT gateway if available."""
    import httpx

    url = base_url.rstrip("/") + "/provenance"

    def _parse_response(resp):
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        return None

    if client is not None:
        try:
            resp = client.get(url)
            return _parse_response(resp)
        except Exception:
            return None

    transport = None
    if uds:
        transport = httpx.HTTPTransport(uds=uds)

    client_kwargs: Dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if transport:
        client_kwargs["transport"] = transport

    try:
        with httpx.Client(**client_kwargs) as http_client:
            resp = http_client.get(url)
            return _parse_response(resp)
    except Exception:
        return None


def verify_gateway_capabilities(
    base_url: str = "http://127.0.0.1:8000/v1",
    uds: Optional[str] = None,
    provider_models: Optional[Dict[str, str]] = None,
    expected_sha: Optional[str] = None,
    timeout: float = 10.0,
    client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Fail-proof preflight: verify running gateway capabilities and provenance before paid inference.

    Aborts clearly with TwTPreflightError if:
    - Gateway is unreachable or returns non-200.
    - Any requested provider-model in provider_models is missing from the running gateway catalog.
    - expected_sha is provided and the running gateway provenance fails to match it.
    """
    models = query_gateway_models(
        base_url=base_url, uds=uds, timeout=timeout, client=client
    )

    available_pairs: Set[Tuple[str, str]] = set()
    available_ids: Set[str] = set()
    for m in models:
        mid = m.get("id") or ""
        owned = m.get("owned_by") or ""
        available_ids.add(mid)
        if owned:
            available_pairs.add((owned, mid))
            if mid.startswith(f"{owned}/"):
                available_pairs.add((owned, mid[len(owned) + 1 :]))

    if provider_models:
        for provider, req_model in provider_models.items():
            qualified = f"{provider}/{req_model}"
            if (
                qualified not in available_ids
                and (provider, req_model) not in available_pairs
                and req_model not in available_ids
            ):
                matching_provider_models = sorted(
                    m for p, m in available_pairs if p == provider
                )
                raise TwTPreflightError(
                    f"TwT gateway capability preflight failed: requested provider model '{provider}={req_model}' "
                    f"is not served by running gateway at {uds or base_url}. "
                    f"Available {provider} models: {matching_provider_models}"
                )

    if expected_sha:
        prov = query_gateway_provenance(
            base_url=base_url, uds=uds, timeout=timeout, client=client
        )
        if not prov:
            raise TwTVCSMismatchError(
                f"TwT gateway provenance preflight failed: gateway at {uds or base_url} "
                f"reported no provenance, so expected SHA '{expected_sha}' cannot be confirmed"
            )
        try:
            verify_installed_vcs_sha(
                prov.get("package") or "tokens-with-taste",
                expected_sha,
                provenance=prov,
            )
        except TwTVCSMismatchError as exc:
            raise TwTVCSMismatchError(
                f"TwT gateway provenance preflight failed: {exc}"
            ) from exc

    return models
