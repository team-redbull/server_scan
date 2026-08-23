"""`ServerInventoryProvider` for standalone Redfish BMCs.

The first collector whose cost is per *server* rather than per manager:
one UCS Central run costs ~11 round trips for a whole fleet, while this
costs ~25 against each BMC. Bounded concurrency, a per-host wall-clock
budget and a total-run budget are therefore correctness requirements, not
tuning knobs — see docs/adr/0016-redfish-standalone-collector.md.

Failure is normal here rather than exceptional. A run where 40 of 400
hosts do not answer is a Tuesday, so per-host failures are collected into
`collection_errors` and the run continues; only a systemic
authentication failure stops it, because continuing would lock accounts
across the estate.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.domain.enums import ManagerType
from app.domain.models.manager import Manager
from app.domain.ports.provider import ProviderServer
from app.infrastructure.providers.redfish.client import (
    RedfishAuthError,
    RedfishClient,
    RedfishError,
    RedfishForbiddenError,
    RedfishProtocolError,
    RedfishTlsError,
    RedfishUnreachableError,
    validate_odata_id,
)
from app.infrastructure.providers.redfish.mapping import system_to_provider_server
from app.infrastructure.providers.redfish.targets import RedfishTarget

logger = structlog.get_logger(__name__)

_PROVIDER_TYPE = ManagerType.REDFISH_STANDALONE.value


@dataclass(slots=True)
class _AuthGuard:
    """
    Bounds how much damage a wrong credential can do to the estate.

    Two counters, because they cover different estates. The per-credential
    threshold catches a stale shared account. The run-wide budget is what
    covers an estate where every BMC has its own login — there the
    per-credential counters all sit at one and would never trip, while
    accounts lock one at a time.

    Stated honestly, because it is easy to over-claim: this bounds damage,
    it does not prevent lockout. With hosts contacted concurrently, a
    number of logins equal to the concurrency limit are already in flight
    before the first rejection returns.

    Attributes:
        threshold (int): Distinct hosts that may reject one credential
            before it is disabled.
        budget (int): Total authentication failures before the run aborts.
    """

    threshold: int
    budget: int
    rejected_hosts: dict[str, set[str]] = field(default_factory=dict)
    total_failures: int = 0

    def record(self, *, credential: str, host: str) -> None:
        """
        Note that a host rejected a credential.

        Args:
            credential (str): The credential's name, never its value.
            host (str): The host that rejected it.
        """
        self.rejected_hosts.setdefault(credential, set()).add(host)
        self.total_failures += 1

    def is_open(self, credential: str) -> bool:
        """
        Report whether a credential has been disabled for this run.

        Args:
            credential (str): The credential's name.

        Returns:
            bool: True once enough distinct hosts have rejected it.
        """
        return len(self.rejected_hosts.get(credential, set())) >= self.threshold

    def exhausted(self) -> bool:
        """
        Report whether the run's total failure budget is spent.

        Returns:
            bool: True when the run should stop entirely.
        """
        return self.total_failures >= self.budget


class RedfishStandaloneProvider:
    """
    Collects every BMC in the configured inventory.

    See docs/adr/0016-redfish-standalone-collector.md.
    """

    provider_type = _PROVIDER_TYPE

    def __init__(
        self,
        *,
        manager: Manager,
        targets: list[RedfishTarget],
        connect_timeout: float,
        read_timeout: float,
        host_budget_seconds: float,
        run_budget_seconds: float,
        fleet_concurrency: int,
        auth_failure_threshold: int,
        auth_failure_budget: int,
        tls_min_version: str = "TLSv1_2",
        debug_http: bool = False,
        client_factory: Callable[[RedfishTarget], Any] | None = None,
    ) -> None:
        """
        Build a collector for one inventory.

        Args:
            manager (Manager): The manager this run reports under.
            targets (list[RedfishTarget]): The validated fleet list.
            connect_timeout (float): Per-connection timeout.
            read_timeout (float): Per-response timeout.
            host_budget_seconds (float): Wall clock allowed per host.
            run_budget_seconds (float): Wall clock allowed for the run.
            fleet_concurrency (int): BMCs contacted at once.
            auth_failure_threshold (int): Distinct hosts rejecting one
                credential before it is disabled.
            auth_failure_budget (int): Total authentication failures
                before the run aborts.
            tls_min_version (str): Minimum TLS version.
            debug_http (bool): Emit one redacted line per request.
            client_factory (Callable[[RedfishTarget], Any] | None): Test
                seam returning a client for a target.
        """
        self._manager = manager
        self._targets = targets
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._host_budget = host_budget_seconds
        self._run_budget = run_budget_seconds
        self._concurrency = max(1, fleet_concurrency)
        self._tls_min_version = tls_min_version
        self._debug_http = debug_http
        self._guard = _AuthGuard(threshold=auth_failure_threshold, budget=auth_failure_budget)
        self._collection_errors: list[str] = []
        self._client_factory: Callable[[RedfishTarget], Any] = client_factory or self._new_client

    def _new_client(self, target: RedfishTarget) -> RedfishClient:
        """
        Build a client for one target.

        Args:
            target (RedfishTarget): The BMC to reach.

        Returns:
            RedfishClient: An unauthenticated client; entering it logs in.
        """
        return RedfishClient(
            target=target,
            connect_timeout=self._connect_timeout,
            read_timeout=self._read_timeout,
            tls_min_version=self._tls_min_version,
            debug_http=self._debug_http,
        )

    @property
    def collection_errors(self) -> tuple[str, ...]:
        """
        Report the hosts this run could not collect.

        `tools.run_collector` turns a non-empty result into a non-zero
        exit status, which is what keeps a partial run from being
        indistinguishable from a healthy run against a smaller estate.

        Returns:
            tuple[str, ...]: One message per failure, empty for a complete
                run.
        """
        return tuple(self._collection_errors)

    async def health_check(self) -> None:
        """
        Verify the collector is configured well enough to run.

        Deliberately makes no network call. There is no single endpoint
        whose reachability means the fleet can be collected, and probing a
        canary host would reintroduce the single point of failure this
        collector exists to remove — one host being reimaged would kill
        every other host's run. Credential validity is checked instead by
        the pre-flight at the head of `list_servers`, against a host that
        actually answers.

        Raises:
            ValueError: If the inventory is empty. `load_targets` already
                rejects that, so this is a belt-and-braces check for a
                provider constructed directly.
        """
        if not self._targets:
            raise ValueError("Redfish inventory is empty; there is nothing to collect.")

    async def list_servers(self) -> AsyncIterator[ProviderServer]:
        """
        Collect every host in the inventory.

        Yields each host's servers as that host finishes rather than
        gathering the fleet, so a run killed at its deadline has already
        persisted what completed.

        Yields:
            ProviderServer: One per `ComputerSystem` found.
        """
        self._collection_errors.clear()
        self._guard = _AuthGuard(threshold=self._guard.threshold, budget=self._guard.budget)

        # Shuffled because completion order is not arrival order: without
        # this the run budget would truncate the same slow hosts every
        # run, leaving them permanently stale and invisible.
        order = list(self._targets)
        random.shuffle(order)

        semaphore = asyncio.Semaphore(self._concurrency)
        tasks = [asyncio.create_task(self._collect_host(t, semaphore)) for t in order]
        collected = 0
        try:
            async with asyncio.timeout(self._run_budget):
                for finished in asyncio.as_completed(tasks):
                    for provider_server in await finished:
                        collected += 1
                        yield provider_server
        except TimeoutError:
            unfinished = sum(1 for task in tasks if not task.done())
            logger.error(
                "redfish.run_budget_exceeded",
                budget_seconds=self._run_budget,
                hosts_total=len(order),
                hosts_unfinished=unfinished,
                hint=(
                    "The run stopped itself before the CronJob's activeDeadlineSeconds could "
                    "kill it, so this summary exists. Raise "
                    "INVENTORY_REDFISH_RUN_BUDGET_SECONDS, lower the fleet size per CronJob, "
                    "or raise INVENTORY_REDFISH_FLEET_CONCURRENCY."
                ),
            )
            self._collection_errors.append(
                f"run budget of {self._run_budget:.0f}s expired with {unfinished} host(s) "
                "not yet collected"
            )
        finally:
            for task in tasks:
                task.cancel()
            # Drained rather than abandoned, so each cancelled host still
            # runs its session teardown.
            await asyncio.gather(*tasks, return_exceptions=True)

        self._log_summary(total=len(order), collected=collected)

    def _log_summary(self, *, total: int, collected: int) -> None:
        """
        Emit one run summary, always.

        Logged even when everything succeeded: "0 collected, 0 failed" is
        the signature of an empty inventory, while "0 collected, 400
        failed" is a credential or network fault, and without this they
        look identical.

        Args:
            total (int): Hosts attempted.
            collected (int): Servers yielded.
        """
        logger.info(
            "redfish.run_summary",
            hosts_total=total,
            servers_collected=collected,
            hosts_failed=len(self._collection_errors),
            auth_failures=self._guard.total_failures,
            credentials_disabled=sorted(
                name for name in self._guard.rejected_hosts if self._guard.is_open(name)
            ),
        )

    async def _collect_host(
        self, target: RedfishTarget, semaphore: asyncio.Semaphore
    ) -> list[ProviderServer]:
        """
        Collect one BMC, containing every failure to that host.

        Args:
            target (RedfishTarget): The BMC to collect.
            semaphore (asyncio.Semaphore): Limits concurrent hosts.

        Returns:
            list[ProviderServer]: Its servers, or an empty list on failure.
        """
        # Acquired *before* the budget starts, so time spent queueing for
        # a slot is not charged against the host. Reversing these makes
        # every host past the first few "time out" without a packet sent.
        async with semaphore:
            credential = target.credential.name
            if self._guard.exhausted():
                self._collection_errors.append(
                    f"{target.host}: skipped, the run's authentication failure budget was spent"
                )
                return []
            if self._guard.is_open(credential):
                self._collection_errors.append(
                    f"{target.host}: skipped, credential {credential!r} was disabled after "
                    f"{self._guard.threshold} rejections"
                )
                return []

            if not target.verify_tls:
                logger.warning(
                    "redfish.tls_verification_disabled",
                    host=target.host,
                    reason=target.verify_tls_reason,
                    hint=(
                        "This BMC's password is sent to whatever answers at this address. "
                        "Import the issuing CA and remove the opt-out."
                    ),
                )

            try:
                async with asyncio.timeout(self._host_budget):
                    return await self._collect_systems(target)
            except TimeoutError:
                logger.warning(
                    "redfish.host_budget_exceeded",
                    host=target.host,
                    budget_seconds=self._host_budget,
                )
                self._collection_errors.append(
                    f"{target.host}: exceeded its {self._host_budget:.0f}s budget"
                )
            except RedfishAuthError as exc:
                self._record_auth_failure(target, exc)
            except RedfishTlsError as exc:
                logger.error("redfish.tls_verify_failed", host=target.host, error=str(exc))
                self._collection_errors.append(f"{target.host}: TLS verification failed — {exc}")
            except RedfishUnreachableError as exc:
                logger.warning("redfish.host_unreachable", host=target.host, error=str(exc))
                self._collection_errors.append(f"{target.host}: unreachable — {exc}")
            except (RedfishError, ValueError) as exc:
                logger.warning("redfish.host_failed", host=target.host, error=str(exc))
                self._collection_errors.append(f"{target.host}: {exc}")
            return []

    def _record_auth_failure(self, target: RedfishTarget, exc: RedfishAuthError) -> None:
        """
        Record a rejected login and, if it is the last one, say so loudly.

        Args:
            target (RedfishTarget): The host that rejected the credential.
            exc (RedfishAuthError): The rejection.
        """
        credential = target.credential.name
        self._guard.record(credential=credential, host=target.host)
        logger.error(
            "redfish.auth_rejected",
            host=target.host,
            credential=credential,
            distinct_hosts=len(self._guard.rejected_hosts[credential]),
            threshold=self._guard.threshold,
            run_failures=self._guard.total_failures,
        )
        self._collection_errors.append(
            f"{target.host}: login failed for credential {credential!r} — not retried"
        )
        if self._guard.is_open(credential):
            logger.error(
                "redfish.credential_circuit_open",
                credential=credential,
                hosts=sorted(self._guard.rejected_hosts[credential]),
                hint=(
                    "Different BMCs rejected the same credential, so it is almost certainly "
                    "wrong or already locked. Verify it by hand against one BMC before "
                    "re-running: repeating the run locks the account on Lenovo XCC and "
                    "IP-blocks this collector from every iDRAC for an hour."
                ),
            )

    async def _collect_systems(self, target: RedfishTarget) -> list[ProviderServer]:
        """
        Open a session and map every `ComputerSystem` the BMC exposes.

        One BMC can expose more than one system — OpenBMC multi-host does
        — so this returns a list rather than a single server.

        Args:
            target (RedfishTarget): The BMC to collect.

        Returns:
            list[ProviderServer]: One per system.

        Raises:
            RedfishError: Propagated to `_collect_host`, which classifies
                it.
        """
        collected: list[ProviderServer] = []
        async with self._client_factory(target) as client:
            systems_link = client.service_root.get("Systems", {})
            systems_path = systems_link.get("@odata.id") if isinstance(systems_link, dict) else None
            if not systems_path:
                self._note_no_systems(target, reason="service root advertises no Systems")
                return []

            systems = await client.get_collection(validate_odata_id(systems_path))
            if not systems:
                self._note_no_systems(target, reason="Systems collection is empty")
                return []

            bmc_mac = await self._bmc_mac(client, systems[0])
            for system in systems:
                try:
                    collected.append(
                        system_to_provider_server(
                            system,
                            host=target.host,
                            base_url=target.base_url,
                            manager_id=self._manager.id,
                            override_name=target.name,
                            processors=await self._optional(client, system, "Processors"),
                            drives=await self._drives(client, system),
                            dimms=await self._optional(client, system, "Memory"),
                            interfaces=await self._optional(client, system, "EthernetInterfaces"),
                            bmc_mac=bmc_mac,
                        )
                    )
                except ValueError as exc:
                    # A system this collector cannot identify, most often
                    # a null Manufacturer. Fails that system, not the host.
                    logger.warning("redfish.system_skipped", host=target.host, error=str(exc))
                    self._collection_errors.append(f"{target.host}: {exc}")
        return collected

    def _note_no_systems(self, target: RedfishTarget, *, reason: str) -> None:
        """
        Record a BMC that authenticated but exposes no server.

        Not silent: the inventory is the operator's own assertion that a
        server is at this address, so nothing there is a wrong address, an
        enclosure manager mistaken for a node, or a licensing limitation.

        Args:
            target (RedfishTarget): The BMC.
            reason (str): What was observed.
        """
        logger.warning(
            "redfish.no_systems",
            host=target.host,
            reason=reason,
            hint=(
                "The BMC authenticated but exposes no ComputerSystem. Check the address is a "
                "server's BMC rather than a chassis or enclosure manager, and that Redfish is "
                "licensed on this hardware."
            ),
        )
        self._collection_errors.append(f"{target.host}: authenticated but exposes no system")

    async def _optional(
        self, client: Any, system: dict[str, Any], key: str
    ) -> list[dict[str, Any]] | None:
        """
        Read a sub-collection, tolerating a BMC that cannot serve it.

        Returns None rather than an empty list on failure, and that
        distinction is load-bearing: the ingest pipeline carries a `None`
        forward, where an empty list would overwrite good stored data and
        — for drives — silently clear a failed-drive health finding.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.
            key (str): Link property to follow.

        Returns:
            list[dict[str, Any]] | None: The members, or None if the
                collection is absent or could not be read.
        """
        link = system.get(key)
        path = link.get("@odata.id") if isinstance(link, dict) else None
        if not path:
            return None
        try:
            members: list[dict[str, Any]] = await client.get_collection(validate_odata_id(path))
            return members
        except (RedfishForbiddenError, RedfishProtocolError, RedfishUnreachableError) as exc:
            logger.warning(
                "redfish.resource_skipped", host=system.get("Id"), resource=key, error=str(exc)
            )
            return None

    async def _drives(self, client: Any, system: dict[str, Any]) -> list[dict[str, Any]] | None:
        """
        Read every drive behind a system's `Storage` controllers.

        `Storage.Drives` is an inline array of links, not a sub-collection,
        so each drive is fetched by following its own `@odata.id` — the
        normative URI is served under Chassis on some vendors and under
        Systems on others, and constructing either would be wrong
        somewhere.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): The owning `ComputerSystem`.

        Returns:
            list[dict[str, Any]] | None: Every drive, deduplicated by
                `@odata.id`, or None if storage could not be read at all.
        """
        controllers = await self._optional(client, system, "Storage")
        if controllers is None:
            return None
        drives: dict[str, dict[str, Any]] = {}
        for controller in controllers:
            for link in controller.get("Drives", []) or []:
                if not isinstance(link, dict):
                    continue
                try:
                    path = validate_odata_id(link.get("@odata.id"))
                    if path not in drives:
                        drives[path] = await client.get(path)
                except (RedfishForbiddenError, RedfishProtocolError) as exc:
                    logger.warning("redfish.drive_skipped", error=str(exc))
        return list(drives.values())

    async def _bmc_mac(self, client: Any, system: dict[str, Any]) -> str | None:
        """
        Read the BMC's own MAC, once per host.

        Args:
            client (Any): The authenticated client.
            system (dict[str, Any]): Any system on this BMC, used to reach
                `Links.ManagedBy`.

        Returns:
            str | None: The manager's MAC, or None if unreachable.
        """
        links = system.get("Links", {})
        managed_by = links.get("ManagedBy", []) if isinstance(links, dict) else []
        if not managed_by or not isinstance(managed_by[0], dict):
            return None
        with contextlib.suppress(RedfishError):
            manager = await client.get(validate_odata_id(managed_by[0].get("@odata.id")))
            interfaces = await self._optional(client, manager, "EthernetInterfaces")
            for interface in interfaces or []:
                mac = interface.get("PermanentMACAddress") or interface.get("MACAddress")
                if isinstance(mac, str) and mac.strip():
                    return mac.strip()
        return None
