/**
 * Hand-written types mirroring the backend's `/api/v1/servers` JSON shapes.
 * Deliberately decoupled from any generated OpenAPI client: the UI only
 * needs a subset of the persisted document, and hand-written types let us
 * keep that subset honest instead of importing the backend's full internal
 * model.
 *
 * Slice 1 note: `classification` and `health` are mostly UNKNOWN/UNCLASSIFIED
 * placeholders until the classification/health engines land in a later
 * slice. Fields are typed as always-present (matching what the backend
 * contract says it returns today) but the *values* are expected to be
 * placeholder-ish for now — don't build UI that assumes rich data here.
 */

/** The three vendors this platform ingests from. There is no "unknown":
 * every server arrives through a vendor-specific collector, so the
 * vendor is known by construction. */
export type Vendor = "dell" | "cisco" | "hp" | "standalone";

/** A site code. A server's site is parsed from its name
 * (`ocp4-prod-tlv-infra-01` -> "tlv"); `null` means the name carries no
 * site token and is surfaced as "Unassigned".
 *
 * Deliberately not a union of the current codes: the closed set is the
 * backend's `SiteCode` enum, and `GET /api/v1/sites` is what tells the UI
 * which codes exist and what each is called. A union here would be a
 * second copy of that list, free to disagree with it — as it did when the
 * sites were renamed and the filter dropdown kept offering the old ones. */
export type SiteCode = string;

export type HealthSeverity =
  "UNKNOWN" | "HEALTHY" | "INFO" | "WARNING" | "CRITICAL";

/** Link/operational state as reported for a physical/logical network link. */
export type LinkState = "UP" | "DOWN" | "UNKNOWN" | "DISABLED";

/** Administrative enable state of a fabric attachment port. */
export type AdminState = "ENABLED" | "DISABLED" | "UNKNOWN";

export type InstallationType = "HOSTED_CLUSTER" | "UPI" | "UNCLASSIFIED";

export interface Classification {
  installation_type: InstallationType;
  matched_rule_id?: string | null;
}

export interface HealthSummary {
  overall: HealthSeverity;
  cpu?: HealthSeverity;
  memory?: HealthSeverity;
  storage?: HealthSeverity;
  network?: HealthSeverity;
  connectivity?: HealthSeverity;
  power?: HealthSeverity;
}

export interface MaintenanceState {
  enabled: boolean;
  reason?: string | null;
}

export interface ConnectivityFacts {
  fabric_paths_total: number;
  fabric_paths_up: number;
  fabric_paths_down: number;
  fabrics_present: string[];
}

export interface ConnectivitySummary {
  facts: ConnectivityFacts;
}

/** Row shape returned by `GET /api/v1/servers` (list). */
export interface ServerSummary {
  id: string;
  name: string;
  vendor: Vendor;
  model: string;
  site_id: SiteCode | null;
  manager_id: string;
  /** Which collector produced this record — `REDFISH_STANDALONE`
   * means the machine has no manager and is reached at its own BMC. */
  source_provider: string | null;
  classification: Classification;
  health: HealthSummary;
  maintenance: MaintenanceState;
  connectivity: ConnectivitySummary;
  last_seen_at: string | null;
  updated_at: string;
}

export interface PageMeta {
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
  count: number | null;
  count_capped: boolean;
}

export interface ServerListResponse {
  items: ServerSummary[];
  page: PageMeta;
}

// ---------------------------------------------------------------------------
// Detail shape — GET /api/v1/servers/{id}
// ---------------------------------------------------------------------------

export interface ServerIdentity {
  vendor: Vendor;
  serial: string;
  system_uuid?: string | null;
  nic_macs?: string[];
}

export interface CpuInfo {
  sockets: number;
  cores: number;
  threads: number;
  model: string;
}

export interface MemoryModule {
  slot?: string;
  size_bytes?: number;
  speed_mts?: number;
}

export interface MemoryInfo {
  total_bytes: number;
  modules: MemoryModule[];
}

export interface StorageDrive {
  id: string;
  model: string;
  serial: string;
  media_type: string;
  capacity_bytes: number;
  health: HealthSeverity;
}

export interface StorageInfo {
  total_bytes: number;
  drives: StorageDrive[];
}

export interface GpuInfo {
  vendor?: string;
  model?: string;
  serial?: string;
  memory_bytes?: number;
  health?: HealthSeverity;
  pci_address?: string;
  firmware_version?: string;
  memory_type?: string;
  ecc_mode_enabled?: boolean;
  correctable_error_count?: number;
  uncorrectable_error_count?: number;
  temperature_celsius?: number;
  power_watts?: number;
}

export interface PsuInfo {
  id?: string;
  status?: string;
  watts?: number;
}

export interface PowerInfo {
  psus: PsuInfo[];
}

export interface HardwareInfo {
  cpu?: CpuInfo;
  memory?: MemoryInfo;
  storage?: StorageInfo;
  gpus?: GpuInfo[];
  power?: PowerInfo;
}

export interface BmcInfo {
  address_raw: string;
  scheme: string;
  host: string;
  port: number;
  mac?: string | null;
}

export interface NetworkInterface {
  name: string;
  mac: string;
  speed_mbps?: number | null;
  link_state: LinkState;
}

export interface NetworkInfo {
  bmc?: BmcInfo;
  interfaces: NetworkInterface[];
}

/**
 * One physical/logical link between a server and a fabric interconnect (or
 * other connectivity provider). `fabric` is a free-form label (commonly "A"
 * / "B" for dual-fabric UCS setups, but the UI must not assume exactly two —
 * `fabric: null` means "ungrouped" and should render under an "Other"
 * section rather than being dropped or crashing).
 */
export interface ConnectivityAttachment {
  type: string;
  provider: string;
  fabric: string | null;
  fabric_name?: string | null;
  fabric_id?: string | null;
  fabric_model?: string | null;
  fabric_serial?: string | null;
  server_interface?: string | null;
  server_port?: string | null;
  fabric_port?: string | null;
  admin_state: AdminState;
  oper_state: LinkState;
  speed_mbps?: number | null;
  last_seen?: string | null;
}

export interface ConnectivityDetail {
  attachments: ConnectivityAttachment[];
  facts: ConnectivityFacts;
}

/** Full document returned by `GET /api/v1/servers/{id}`. */
export interface ServerDetail {
  id: string;
  name: string;
  model: string;
  identity?: ServerIdentity;
  hardware?: HardwareInfo;
  network?: NetworkInfo;
  connectivity?: ConnectivityDetail;
  classification: Classification;
  health: HealthSummary;
  maintenance: MaintenanceState;
  site_id: SiteCode | null;
  manager_id: string;
  source_provider: string | null;
  /** Dotted paths into this same response that the most recent collection
   * could not read (`hardware.storage.drives`). The stored value at such
   * a path is either carried over from an earlier run or the model's zero
   * — never a reading from this run, which is why the UI must not present
   * a `0`/`[]` there as fact. */
  unread_fields?: string[];
  tags?: string[];
  last_seen_at: string | null;
  updated_at: string;
  created_at?: string;
}
