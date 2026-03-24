'use client';

import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { useAuth } from '@/lib/hooks/use-auth';
import { authApi } from '@/lib/api/auth';
import { useMutation } from '@tanstack/react-query';
import { toast } from 'sonner';

const schema = z.object({
  first_name: z.string().min(1),
  last_name: z.string(),
  institution: z.string(),
  research_area: z.string(),
});

type FormData = z.infer<typeof schema>;

export default function SettingsPage() {
  const { user, profile } = useAuth();

  const { register, handleSubmit } = useForm<FormData>({
    resolver: zodResolver(schema),
    defaultValues: {
      first_name: profile?.first_name ?? '',
      last_name: profile?.last_name ?? '',
      institution: profile?.institution ?? '',
      research_area: profile?.research_area ?? '',
    },
  });

  const update = useMutation({
    mutationFn: (data: FormData) => authApi.updateMe(data).then(r => r.data),
    onSuccess: () => toast.success('Profile updated'),
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
          <form onSubmit={handleSubmit((d) => update.mutate(d))} className="space-y-4">
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
                <Input {...register('first_name')} />
              </div>
              <div className="space-y-1.5">
                <Label>Last name</Label>
                <Input {...register('last_name')} />
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Institution</Label>
              <Input {...register('institution')} placeholder="University of São Paulo" />
            </div>

            <div className="space-y-1.5">
              <Label>Research area</Label>
              <Input {...register('research_area')} placeholder="Cardiology, Genomics…" />
            </div>

            <Button type="submit" disabled={update.isPending}>
              {update.isPending ? 'Saving…' : 'Save changes'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
