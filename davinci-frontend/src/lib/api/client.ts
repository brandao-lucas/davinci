import axios from 'axios';
import { getFirebaseAuth } from '@/lib/firebase';

const apiClient = axios.create({
  baseURL: '/api/v1',
  headers: {
    'Content-Type': 'application/json',
  },
});

apiClient.interceptors.request.use(async (config) => {
  const user = getFirebaseAuth().currentUser;
  if (user) {
    const token = await user.getIdToken();
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      // Only force-redirect when Firebase has no current user (session truly expired).
      // If Firebase user exists but backend returns 401, let the component handle it —
      // redirecting here would kick the user out right after login while the token is
      // still being verified on the first request.
      const hasFirebaseUser = !!getFirebaseAuth().currentUser;
      if (!hasFirebaseUser && window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    }
    return Promise.reject(error);
  }
);

export default apiClient;
