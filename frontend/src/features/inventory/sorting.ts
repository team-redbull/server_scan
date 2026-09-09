/** Which columns the inventory table offers a sort on.
 *
 * Its own module so the list and the type stay one declaration: when they
 * were written separately, a column could offer a sort that
 * `InventoryPage`'s URL-param guard then discarded, silently falling back
 * to the default. Every value here must also be in the backend's
 * `SORT_FIELDS` whitelist and covered by a compound index — see
 * `app.domain.services.search`.
 */
export const SORTABLE_FIELDS = [
  "name",
  "model",
  "updated_at",
  "openshift_state",
  "cluster_name",
  "mce_name",
] as const;

export type SortableField = (typeof SORTABLE_FIELDS)[number];
