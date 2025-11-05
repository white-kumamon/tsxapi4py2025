"""Manage cached contract universes for monitoring scripts."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from string import ascii_uppercase, digits
from typing import Dict, Iterable, List, Optional, Sequence

from tsxapipy.api.client import APIClient
from tsxapipy.api import schemas
from tsxapipy.api.exceptions import APIError
from tsxapipy.common.time_utils import UTC_TZ

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContractSummary:
    """A lightweight representation of a contract returned by the API."""

    id: str
    name: Optional[str]
    description: Optional[str]
    instrument_id: Optional[int]

    @classmethod
    def from_model(cls, contract_model: schemas.Contract) -> "ContractSummary":
        return cls(
            id=contract_model.id,
            name=contract_model.name,
            description=contract_model.description,
            instrument_id=contract_model.instrument_id,
        )


class ContractUniverseManager:
    """Fetches and caches the complete contract universe for monitoring tasks."""

    DEFAULT_CACHE_FILENAME = "contract_universe.json"

    #: Search prefixes used to build the universe when none are provided.
    DEFAULT_SEARCH_PREFIXES: Sequence[str] = (
        ["CON.F.US.", "F.US."]
        + [f"CON.F.US.{char}" for char in ascii_uppercase + digits]
        + [f"F.US.{char}" for char in ascii_uppercase + digits]
    )

    def __init__(
        self,
        api_client: APIClient,
        cache_directory: Path,
        *,
        search_prefixes: Optional[Sequence[str]] = None,
    ) -> None:
        self._api_client = api_client
        self._cache_directory = cache_directory
        self._cache_directory.mkdir(parents=True, exist_ok=True)
        self._cache_path = self._cache_directory / self.DEFAULT_CACHE_FILENAME
        self._search_prefixes = tuple(search_prefixes) if search_prefixes else tuple(
            dict.fromkeys(self.DEFAULT_SEARCH_PREFIXES)
        )
        self._cached_contracts: List[ContractSummary] = []
        self._last_loaded_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Cache management helpers
    # ------------------------------------------------------------------
    def ensure_universe(self, *, max_age_hours: int = 24) -> List[ContractSummary]:
        """Ensure a reasonably fresh universe is loaded and return it."""

        if not self._cached_contracts:
            self._load_from_disk()

        if self._cached_contracts and self._last_loaded_at:
            age = datetime.now(UTC_TZ) - self._last_loaded_at
            if age <= timedelta(hours=max_age_hours):
                return list(self._cached_contracts)

        logger.info("Refreshing contract universe from API (max_age_hours=%s)", max_age_hours)
        self.refresh_from_api()
        return list(self._cached_contracts)

    def refresh_from_api(self) -> None:
        """Force refresh of the universe by querying the API."""

        aggregated: Dict[str, ContractSummary] = {}
        for prefix in self._search_prefixes:
            try:
                contracts = self._api_client.search_contracts(search_text=prefix, live=True)
            except APIError as exc:
                logger.warning("Contract search failed for prefix '%s': %s", prefix, exc)
                continue

            for contract in contracts:
                if not contract.id:
                    continue
                if contract.id in aggregated:
                    continue
                aggregated[contract.id] = ContractSummary.from_model(contract)

        self._cached_contracts = sorted(aggregated.values(), key=lambda model: model.id)
        self._last_loaded_at = datetime.now(UTC_TZ)
        self._write_to_disk()
        logger.info("Contract universe refreshed: %d contracts stored.", len(self._cached_contracts))

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------
    def get_contracts(
        self,
        *,
        exclude_micro: bool = True,
        limit: Optional[int] = None,
    ) -> List[ContractSummary]:
        """Return cached contracts with optional filtering."""

        contracts = self.ensure_universe()
        if exclude_micro:
            contracts = [contract for contract in contracts if not self._is_micro_contract(contract)]

        if limit is not None and limit > 0:
            contracts = contracts[:limit]

        return contracts

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------
    def _write_to_disk(self) -> None:
        payload = {
            "generated_at": datetime.now(UTC_TZ).isoformat(),
            "contracts": [asdict(contract) for contract in self._cached_contracts],
        }
        with self._cache_path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2)

    def _load_from_disk(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            with self._cache_path.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except json.JSONDecodeError:
            logger.warning("Cache file %s is corrupted. Ignoring.", self._cache_path)
            return

        contracts_payload = payload.get("contracts", [])
        loaded_contracts: List[ContractSummary] = []
        for contract_dict in contracts_payload:
            try:
                loaded_contracts.append(
                    ContractSummary(
                        id=contract_dict["id"],
                        name=contract_dict.get("name"),
                        description=contract_dict.get("description"),
                        instrument_id=contract_dict.get("instrument_id"),
                    )
                )
            except KeyError:
                continue

        generated_at_str = payload.get("generated_at")
        generated_at: Optional[datetime] = None
        if isinstance(generated_at_str, str):
            try:
                generated_at = datetime.fromisoformat(generated_at_str)
                if generated_at.tzinfo is None:
                    generated_at = UTC_TZ.localize(generated_at)
            except ValueError:
                generated_at = None

        self._cached_contracts = loaded_contracts
        self._last_loaded_at = generated_at
        if loaded_contracts:
            logger.info("Loaded %d contracts from cache (%s).", len(loaded_contracts), self._cache_path)

    # ------------------------------------------------------------------
    @staticmethod
    def _is_micro_contract(contract: ContractSummary) -> bool:
        """Heuristic to remove micro contracts from the universe."""

        identifier = contract.id.upper()
        if identifier.startswith("CON.F.US.M"):
            return True

        name = (contract.name or "").upper()
        description = (contract.description or "").upper()
        if "MICRO" in name or "MICRO" in description:
            return True

        return False

    # ------------------------------------------------------------------
    def __len__(self) -> int:  # pragma: no cover - convenience method
        return len(self._cached_contracts)

    def __iter__(self) -> Iterable[ContractSummary]:  # pragma: no cover - convenience
        return iter(self._cached_contracts)
