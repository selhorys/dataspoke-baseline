import { useQuery, type UseQueryOptions } from "@tanstack/react-query";

const DEFAULT_INTERVAL_MS = 15_000;

/**
 * Wrapper around useQuery that adds a refetch interval, paused when the tab is
 * not visible. TanStack Query's refetchIntervalInBackground defaults to false,
 * which means it pauses polling when the document is hidden — no extra logic
 * required.
 */
export function usePoll<TData, TError = Error>(
  options: UseQueryOptions<TData, TError> & { pollInterval?: number },
) {
  const { pollInterval = DEFAULT_INTERVAL_MS, ...queryOptions } = options;

  return useQuery<TData, TError>({
    ...queryOptions,
    refetchInterval: pollInterval,
    refetchIntervalInBackground: false,
  });
}
