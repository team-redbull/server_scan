import { QueryClient } from "@tanstack/react-query";

/**
 * Single shared TanStack Query client. `staleTime` is set above zero so
 * navigating back to an already-fetched list/detail page doesn't trigger a
 * refetch flash — appropriate for inventory data that changes on the order
 * of minutes (ingestion runs), not seconds. Retries are capped low: a
 * failed request against this API is far more likely to be a real 4xx/5xx
 * than a transient network blip worth retrying automatically.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
