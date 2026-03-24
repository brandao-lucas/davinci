import apiClient from './client';

export interface UserProfile {
  id: string;
  email: string;
  first_name: string;
  last_name: string;
  firebase_uid: string;
  auth_provider: string;
  orcid_id: string | null;
  institution: string;
  research_area: string;
  avatar_url: string;
}

export const authApi = {
  getMe: () =>
    apiClient.get<UserProfile>('/auth/me/'),

  updateMe: (data: Partial<Pick<UserProfile, 'first_name' | 'last_name' | 'institution' | 'research_area'>>) =>
    apiClient.patch<UserProfile>('/auth/me/', data),

  verify: () =>
    apiClient.post('/auth/verify/'),
};
