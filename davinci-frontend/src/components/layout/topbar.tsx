'use client';

import { PanelLeft } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { UserMenu } from '@/components/auth/user-menu';
import { useUIStore } from '@/lib/stores/ui-store';

export function Topbar() {
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);

  return (
    <header className="flex items-center h-14 px-4 border-b border-border bg-background shrink-0 gap-4">
      <Button variant="ghost" size="icon" onClick={toggleSidebar} className="shrink-0">
        <PanelLeft className="h-4 w-4" />
      </Button>

      <div className="flex-1" />

      <UserMenu />
    </header>
  );
}
