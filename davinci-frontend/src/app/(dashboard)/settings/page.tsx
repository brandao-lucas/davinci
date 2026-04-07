'use client';

import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Eye, EyeOff } from 'lucide-react';
import { useAuth } from '@/lib/hooks/use-auth';
import { authApi } from '@/lib/api/auth';
import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';

const profileSchema = z.object({
  first_name: z.string().min(1),
  last_name: z.string(),
  institution: z.string(),
  research_area: z.string(),
});

const apiKeySchema = z.object({
  ncbi_api_key: z.string().optional(),
});

type ProfileFormData = z.infer<typeof profileSchema>;
type ApiKeyFormData = z.infer<typeof apiKeySchema>;

export default function SettingsPage() {
  const { user, profile } = useAuth();
  const [showNcbiKey, setShowNcbiKey] = useState(false);

  const { register: registerProfile, handleSubmit: handleProfileSubmit } = useForm<ProfileFormData>({
    resolver: zodResolver(profileSchema),
    defaultValues: {
      first_name: profile?.first_name ?? '',
      last_name: profile?.last_name ?? '',
      institution: profile?.institution ?? '',
      research_area: profile?.research_area ?? '',
    },
  });

  const { register: registerApiKey, handleSubmit: handleApiKeySubmit } = useForm<ApiKeyFormData>({
    resolver: zodResolver(apiKeySchema),
    defaultValues: { ncbi_api_key: '' },
  });

  const updateProfile = useMutation({
    mutationFn: (data: ProfileFormData) => authApi.updateMe(data).then(r => r.data),
    onSuccess: () => toast.success('Profile updated'),
  });

  const updateApiKey = useMutation({
    mutationFn: (data: ApiKeyFormData) => authApi.updateMe({ ncbi_api_key: data.ncbi_api_key }).then(r => r.data),
    onSuccess: () => toast.success('API key saved'),
  });

  const initials = profile
    ? `${profile.first_name?.[0] ?? ''}${profile.last_name?.[0] ?? ''}`.toUpperCase()
    : user?.email?.[0]?.toUpperCase() ?? '?';

  return (
    <div className="max-w-2xl space-y-6">
      <PageHeader title="Settings" description="Manage your profile" />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Profile</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleProfileSubmit((d) => updateProfile.mutate(d))} className="space-y-4">
            <div className="flex items-center gap-4">
              <Avatar className="h-16 w-16">
                <AvatarImage src={profile?.avatar_url} />
                <AvatarFallback>{initials}</AvatarFallback>
              </Avatar>
              <div>
                <p className="font-medium">{user?.email}</p>
                <p className="text-sm text-muted-foreground">{profile?.auth_provider}</p>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <Label>First name</Label>
                <Input {...registerProfile('first_name')} />
              </div>
              <div className="space-y-1.5">
                <Label>Last name</Label>
                <Input {...registerProfile('last_name')} />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Institution</Label>
              <Input {...registerProfile('institution')} placeholder="University of São Paulo" />
            </div>

            <div className="space-y-1.5">
              <Label>Research area</Label>
              <Input {...registerProfile('research_area')} placeholder="Cardiology, Genomics…" />
            </div>

            <Button type="submit" disabled={updateProfile.isPending}>
              {updateProfile.isPending ? 'Saving…' : 'Save changes'}
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">API Keys</CardTitle>
          <CardDescription>
            Your personal NCBI API key increases the rate limit from 3 to 10 requests/second.{' '}
            <a
              href="https://www.ncbi.nlm.nih.gov/account/"
              target="_blank"
              rel="noopener noreferrer"
              className="underline underline-offset-2"
            >
              Get your key at ncbi.nlm.nih.gov
            </a>
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleApiKeySubmit((d) => updateApiKey.mutate(d))} className="space-y-4">
            <div className="space-y-1.5">
              <Label>NCBI API Key</Label>
              <div className="relative">
                <Input
                  {...registerApiKey('ncbi_api_key')}
                  type={showNcbiKey ? 'text' : 'password'}
                  placeholder="Paste your NCBI API key here"
                  className="pr-10"
                />
                <button
                  type="button"
                  onClick={() => setShowNcbiKey(v => !v)}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  tabIndex={-1}
                >
                  {showNcbiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            <Button type="submit" disabled={updateApiKey.isPending}>
              {updateApiKey.isPending ? 'Saving…' : 'Save key'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
