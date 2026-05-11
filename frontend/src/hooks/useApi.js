import { useState, useEffect, useCallback } from 'react';
import api from '../api/client';

export function useGet(url, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [refetchCount, setRefetchCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.get(url)
      .then((res) => { if (!cancelled) setData(res.data); })
      .catch((err) => { if (!cancelled) setError(err); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [refetchCount, ...deps]);

  return { data, loading, error, refetch: () => setRefetchCount((c) => c + 1) };
}

export function usePost() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const post = useCallback(async (url, payload, config) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.post(url, payload, config);
      return res.data;
    } catch (err) {
      setError(err);
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  return { post, loading, error };
}
