'use client';

import { use } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { CurationQueueTable } from '@/components/datasets/curation-queue-table';
import { useCurationQueue } from '@/lib/hooks/use-curation-queue';
import { AlertTriangle } from 'lucide-react';

function CurationQueuePageContent({ projectId }: { projectId: string }) {
  const { data: items } = useCurationQueue(projectId);
  const pending = items?.length ?? 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Fila de Curadoria"
        description={
          pending > 0
            ? `${pending} dataset${pending !== 1 ? 's' : ''} aguardando revisão manual`
            : 'Fila vazia'
        }
        actions={
          pending > 0 ? (
            <div className="flex items-center gap-1.5 text-amber-600 text-sm">
              <AlertTriangle className="h-4 w-4" />
              Classificação automática com baixa confiança (&lt; 50%)
            </div>
          ) : undefined
        }
      />

      <div className="rounded-md border">
        <CurationQueueTable projectId={projectId} />
      </div>
    </div>
  );
}

export default function CurationQueuePage({
  params,
}: {
  params: Promise<{ projectId: string }>;
}) {
  const { projectId } = use(params);
  return <CurationQueuePageContent projectId={projectId} />;
}
