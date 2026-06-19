'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { cn } from '@/lib/utils';
import { useUIStore } from '@/lib/stores/ui-store';
import {
  FolderOpen,
  FileText,
  Database,
  FlaskConical,
  Link2,
  BarChart2,
  Briefcase,
  Download,
  Settings,
  Dna,
} from 'lucide-react';

interface NavItem {
  label: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  projectScoped?: boolean;
}

const topNavItems: NavItem[] = [
  { label: 'Projects', href: '/projects', icon: FolderOpen },
];

const projectNavItems: NavItem[] = [
  { label: 'Overview', href: '', icon: BarChart2, projectScoped: true },
  { label: 'Papers', href: '/papers', icon: FileText, projectScoped: true },
  { label: 'Datasets', href: '/datasets', icon: Database, projectScoped: true },
  { label: 'Samples', href: '/samples', icon: FlaskConical, projectScoped: true },
  { label: 'Links', href: '/links', icon: Link2, projectScoped: true },
  { label: 'Analysis', href: '/analysis', icon: BarChart2, projectScoped: true },
  { label: 'Variantes', href: '/variants', icon: Dna, projectScoped: true },
  { label: 'Jobs', href: '/jobs', icon: Briefcase, projectScoped: true },
  { label: 'Export', href: '/export', icon: Download, projectScoped: true },
];

const bottomNavItems: NavItem[] = [
  { label: 'Settings', href: '/settings', icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);

  // Extract project ID from path if present
  const projectMatch = pathname.match(/\/projects\/([^/]+)/);
  const projectId = projectMatch?.[1];

  return (
    <aside
      className={cn(
        'flex flex-col h-full bg-sidebar border-r border-border transition-all duration-200',
        sidebarOpen ? 'w-56' : 'w-14'
      )}
    >
      <div className="flex items-center h-14 px-4 border-b border-border shrink-0">
        {sidebarOpen && (
          <span className="text-lg font-bold text-davinci-500">DaVinci</span>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto py-4 space-y-1 px-2">
        {topNavItems.map((item) => (
          <NavLink key={item.href} item={item} pathname={pathname} sidebarOpen={sidebarOpen} />
        ))}

        {projectId && (
          <>
            <div className={cn('px-2 pt-4 pb-1', sidebarOpen ? 'text-xs text-muted-foreground uppercase tracking-wider' : 'hidden')}>
              Project
            </div>
            {projectNavItems.map((item) => {
              const href = `/projects/${projectId}${item.href}`;
              return (
                <NavLink
                  key={href}
                  item={{ ...item, href }}
                  pathname={pathname}
                  sidebarOpen={sidebarOpen}
                />
              );
            })}
          </>
        )}
      </nav>

      <div className="border-t border-border py-4 px-2 space-y-1">
        {bottomNavItems.map((item) => (
          <NavLink key={item.href} item={item} pathname={pathname} sidebarOpen={sidebarOpen} />
        ))}
      </div>
    </aside>
  );
}

function NavLink({
  item,
  pathname,
  sidebarOpen,
}: {
  item: NavItem;
  pathname: string;
  sidebarOpen: boolean;
}) {
  const isActive = pathname === item.href || (item.href !== '/projects' && pathname.startsWith(item.href));
  const Icon = item.icon;

  return (
    <Link
      href={item.href}
      className={cn(
        'flex items-center gap-3 rounded-md px-2 py-2 text-sm font-medium transition-colors',
        isActive
          ? 'bg-accent text-accent-foreground'
          : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
        !sidebarOpen && 'justify-center'
      )}
    >
      <Icon className="h-4 w-4 shrink-0" />
      {sidebarOpen && <span>{item.label}</span>}
    </Link>
  );
}
