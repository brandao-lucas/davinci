'use client';

import { Button } from '@/components/ui/button';
import { extractApiErrorMessage } from '@/lib/utils/api-error';

interface QueryErrorStateProps {
  error: unknown;
  onRetry?: () => void;
}

export function QueryErrorState({ error, onRetry }: QueryErrorStateProps) {
  const message = extractApiErrorMessage(error, 'Falha ao carregar');

  return (
    <div className="flex flex-col items-center gap-2 p-8 text-center">
      <p className="text-destructive text-sm">{message}</p>
      {onRetry && (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Tentar novamente
        </Button>
      )}
    </div>
  );
}
